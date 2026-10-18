#!/usr/bin/env python3
"""Opt-in upgrade, exact rollback, and interrupted upgrade of the accepted demo.

Actors receive goals and the original three-tool documentation. The harness
only observes the app through MCP/forwarded HTTP and controls the gateway fault
through an explicitly captured, ownership-checked management SSH command.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
import uuid

import agent_demo as demo
import demo_actor
from demo_actor import save_json
from demo_evaluator import APP_SERVICE, PROXY_SERVICE, connected_trace, proxy_json_log, verify_architecture
import lab
import mcp_lab
from mcp_client import MCPClient, stream_bytes


SOURCE_SCRIPT = demo.EXPORT_SCRIPT.replace("suffixes = {'.go'", "suffixes = {'.py', '.go'", 1)
OUTAGE_SECONDS = 4
GATEWAY_SERVICE_SECONDS = 12
RECOVERY_SECONDS = 15


def deployment_context(containers):
    """Discover the active directory/configuration exclusively from Compose labels."""
    contexts = []
    for container in containers.values():
        labels = container["Config"]["Labels"]
        root = Path(labels.get("com.docker.compose.project.working_dir", ""))
        files = labels.get("com.docker.compose.project.config_files", "").split(",")
        if (not root.is_absolute() or root == Path("/home/mcp-test") or not root.is_relative_to("/home/mcp-test")
                or ".." in root.parts or not files
                or any(not Path(path).is_absolute() or ".." in Path(path).parts or not Path(path).is_relative_to(root) for path in files)):
            raise lab.LabError("Unsafe or missing actor-created Compose directory/configuration labels.")
        contexts.append({"root": str(root), "config_files": files})
    if any(value["root"] != contexts[0]["root"] for value in contexts):
        raise lab.LabError("Demo containers disagree about the active Compose directory.")
    # An app-only Compose override need not appear on unchanged proxy/backend
    # containers. Its own labels identify the currently active app config.
    return contexts[list(containers).index("app")]


def infrastructure(containers):
    return {
        "container_ids": {name: containers[name]["Id"] for name in ("traefik", "lgtm")},
        "named_volumes": {name: sorted((mount.get("Name"), mount["Destination"], mount.get("RW"))
                                        for mount in value.get("Mounts", []) if mount["Type"] == "volume")
                          for name, value in containers.items()},
    }


def require_preserved_infrastructure(baseline, containers):
    verify_architecture(containers)
    deployment_context(containers)
    if infrastructure(containers) != infrastructure(baseline):
        raise lab.LabError("An operations actor replaced Traefik/LGTM or changed the existing named volumes.")


def http_observations(directory):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    observations = {}
    for route in ("/", "/version"):
        request = urllib.request.Request("http://127.0.0.1:18090" + route,
                                         headers={"X-SSHMCP-Probe": "operations-" + uuid.uuid4().hex})
        try:
            response = opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read(65537)
            if len(body) > 65536:
                raise lab.LabError("Version/Hello response exceeded the independent HTTP evidence bound.")
            observations[route] = {"status": response.code, "body_base64": base64.b64encode(body).decode(),
                                   "observed_ns": time.time_ns()}
    save_json(directory / "http.json", observations)
    if observations["/"]["status"] != 200 or base64.b64decode(observations["/"]["body_base64"]).strip() != b"Hello, world!":
        raise lab.LabError("Operations phase did not retain Hello, world! through Traefik.")
    return observations


def require_version(observation, expected):
    if any(observation[key] != expected[key] for key in ("status", "body_base64")):
        raise lab.LabError("Independent /version status/body does not match this operations phase.")


def expected_version(marker):
    return {"status": 200, "body_base64": base64.b64encode(marker.encode()).decode()}


def historical_probe(record):
    if record.get("historical_probe"):
        return record["historical_probe"]
    directory = Path(record["evidence_directory"])
    if not directory.resolve().is_relative_to((lab.ROOT / ".lab" / "results").resolve()):
        raise lab.LabError("Accepted demo evidence directory is outside private lab results.")
    path = directory / "repair-evaluation" / "evaluation.json"
    lab.ensure_private_path(path)
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise lab.LabError("Accepted demo has no bounded historical repair probe to retain.")
    return json.loads(path.read_text())["probe"]


def prove_history(evaluator, probe):
    trace = evaluator.trace(probe)
    app_logs = evaluator.logs(APP_SERVICE, probe, trace=True)
    proxy_logs = evaluator.logs(PROXY_SERVICE, probe)
    evidence = {"probe": probe, "trace_ancestry": connected_trace(trace or {}, probe["trace_id"], probe["started_ns"]),
                "app_log_count": len(app_logs), "proxy_json_log": proxy_json_log(proxy_logs, probe["marker"])}
    save_json(evaluator.directory / "historical.json", evidence)
    if not evidence["trace_ancestry"] or not app_logs or not evidence["proxy_json_log"]:
        raise lab.LabError("Existing historical telemetry was not retained across the operations phase.")
    return evidence


def evaluate_phase(directory, baseline, version, history):
    containers = demo.inspect_deployment(directory, "inspect")
    require_preserved_infrastructure(baseline, containers)
    observed = http_observations(directory)
    require_version(observed["/version"], version)
    evaluator = demo.evaluator(directory, "evaluation", containers)
    # evaluate() establishes new counter baselines after an app recreation;
    # counters from an earlier process cannot satisfy this phase.
    fresh = evaluator.evaluate()
    retained = prove_history(evaluator, history)
    evidence = {"app_image_id": containers["app"]["Image"], "http": observed,
                "fresh": fresh, "historical": retained, "infrastructure": infrastructure(containers)}
    save_json(directory / "phase.json", evidence)
    return containers, evidence


def installed_context():
    config = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    value = {"model_selection": "installed default", "quota": "unknown"}
    if config.is_file():
        body = config.read_bytes()
        settings = tomllib.loads(body.decode())
        value.update(config_sha256=hashlib.sha256(body).hexdigest(), model=settings.get("model"),
                     reasoning_effort=settings.get("model_reasoning_effort"))
    return value


def verify_release(directory, release, original):
    """Verify all required pre-upgrade hashes and safe additional text snapshots."""
    script = r'''import hashlib,json,pathlib,stat,subprocess
root = pathlib.Path(%r)
if any(parent.is_symlink() for parent in (root, *root.parents)) or not root.is_dir() or root.stat().st_mode & 0o077:
    raise RuntimeError('Actor release directory must be private and nonsymlink')
path = root / 'release.json'
if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
    raise RuntimeError('Missing bounded private release manifest')
record = json.loads(path.read_text())
if record.get('image_id') != %r:
    raise RuntimeError('Actor did not preserve the original runtime image ID')
image = json.loads(subprocess.check_output(['sudo','-n','docker','image','inspect',record['image_id']],text=True))
if len(image) != 1 or image[0]['Id'] != record['image_id']:
    raise RuntimeError('Original runtime image is no longer available')
required_hashes = %r
source_root = pathlib.Path(%r)
if any(parent.is_symlink() for parent in (source_root, *source_root.parents)) or not source_root.is_dir():
    raise RuntimeError('Original source directory is missing or contains a symlink')
if not isinstance(record.get('files'), list) or len(record['files']) > 128:
    raise RuntimeError('Release snapshots exceed finite file-count bound')

def manifest_relative_path(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise RuntimeError('Unsafe release manifest path')
    path = pathlib.Path(value)
    if path.is_absolute() or '..' in path.parts or value != path.as_posix() or path == pathlib.Path('.'):
        raise RuntimeError('Unsafe release manifest path')
    return path

def nonsymlink_path(parent, relative):
    current = parent
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError('Release snapshot path contains a symlink')
    return current

def read_bounded_text(path):
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 262144:
        raise RuntimeError('Missing, nonregular, or oversized source/config snapshot')
    body = path.read_bytes()
    body.decode('utf-8')
    if b'\x00' in body:
        raise RuntimeError('Source/config snapshot is not text')
    return body

original_paths, saved_paths = set(), set()
total_bytes, extra_current_originals_matched = 0, 0
for entry in record['files']:
    original_path = manifest_relative_path(entry['path']).as_posix()
    saved_path = manifest_relative_path(entry['saved_path'])
    if original_path in original_paths or saved_path in saved_paths:
        raise RuntimeError('Duplicate release manifest original or saved path')
    original_paths.add(original_path)
    saved_paths.add(saved_path)
    body = read_bounded_text(nonsymlink_path(root, saved_path))
    total_bytes += len(body)
    if total_bytes > 4194304:
        raise RuntimeError('Release snapshots exceed finite total-byte bound')
    digest = hashlib.sha256(body).hexdigest()
    if entry['sha256'] != digest or (original_path in required_hashes and digest != required_hashes[original_path]):
        raise RuntimeError('Actor rollback snapshot differs from pre-upgrade source/config')
    # Extras have no independent pre-upgrade hash. Check their current originals
    # when still present, and report that weaker evidence separately.
    if original_path not in required_hashes:
        current_original = nonsymlink_path(source_root, pathlib.Path(original_path))
        if current_original.exists():
            if hashlib.sha256(read_bounded_text(current_original)).hexdigest() != digest:
                raise RuntimeError('Extra rollback snapshot differs from its current original')
            extra_current_originals_matched += 1
if not set(required_hashes).issubset(original_paths):
    raise RuntimeError('Actor rollback snapshots omit pre-upgrade source/config')
print(json.dumps({
    'image_id': record['image_id'],
    'preserved_files': len(original_paths),
    'required_files': len(required_hashes),
    'extra_files': len(original_paths) - len(required_hashes),
    'extra_current_originals_matched': extra_current_originals_matched,
    'verified': True,
}))
''' % (release, original["image_id"], {item["path"]: item["sha256"] for item in original["files"]}, original["root"])
    return demo.operator(directory, "preserved-release", script)


def require_rollback(original, containers, restored):
    if containers["app"]["Image"] != original["image_id"]:
        raise lab.LabError("Rollback did not restore the exact pre-upgrade runtime image ID.")
    before = {entry["path"]: entry["sha256"] for entry in original["files"]}
    after = {entry["path"]: entry["sha256"] for entry in restored["files"]}
    if len(before) != len(original["files"]) or len(after) != len(restored["files"]) or before != after:
        raise lab.LabError("Rollback source/configuration hashes differ from the pre-upgrade private snapshot.")


def require_recovered_baseline(accepted_run_id, containers, source, version):
    """An explicit repeat cannot silently adopt a failed actor's partial state."""
    checked = []
    hashes = {item["path"]: item["sha256"] for item in source["files"]}
    for path in sorted(demo.PRIVATE.glob("operations-failed-*.json")):
        lab.ensure_private_path(path)
        if path.stat().st_size > 1024 * 1024:
            raise lab.LabError("Prior failed operations record exceeds its private evidence bound.")
        failed = json.loads(path.read_text())
        if failed["accepted_run_id"] != accepted_run_id:
            continue
        if (containers["app"]["Image"] != failed["image_id"] or hashes != failed["source_hashes"]
                or any(version[key] != failed["version"][key] for key in ("status", "body_base64"))):
            raise lab.LabError("A prior failed operations attempt changed the baseline. Recover its recorded image/source/config/HTTP state through a separate recorded MCP operator before another fresh attempt; no automatic recovery was performed.")
        checked.append(failed["run_id"])
    return checked


def record_failed_attempt(directory, run_id):
    path = directory / "intent.json"
    if not path.exists():
        return
    intent = json.loads(path.read_text())
    save_json(demo.PRIVATE / ("operations-failed-" + run_id + ".json"), {
        "run_id": run_id, "accepted_run_id": intent["accepted"]["run_id"], "evidence_directory": str(directory),
        "image_id": intent["baseline_image_id"], "source_hashes": intent["baseline_source_hashes"],
        "version": intent["baseline_version"],
    })


def receipt_observation(directory, receipt, marker, *, absent=False):
    script = r'''import json,pathlib,stat
p = pathlib.Path(%r)
if any(parent.is_symlink() for parent in (p, *p.parents)):
    raise RuntimeError('Receipt path contains a symlink')
result = {'exists':p.exists()}
if p.exists():
    if not p.is_file() or p.stat().st_size > 4096 or p.stat().st_mode & 0o077:
        raise RuntimeError('Receipt must be a bounded private regular file')
    lines = p.read_text().splitlines()
    result.update(lines=len(lines), matching_lines=lines.count(%r))
print(json.dumps(result))
''' % (receipt, marker)
    result = demo.operator(directory, "receipt-before" if absent else "receipt-after", script)
    if absent:
        if result["exists"]:
            raise lab.LabError("Per-run activation receipt already exists; no stale proof is accepted.")
    elif not result["exists"] or result["lines"] != 1 or result["matching_lines"] != 1:
        raise lab.LabError("Original activation receipt does not contain exactly one readiness marker.")
    return result


def goal(phase, *, version, release, baseline_version, image_id, fault=None):
    brief = (
        "Operate the existing accepted Go/Traefik/LGTM demo on lab-target. Discover its active application directory and "
        "Compose configuration from the existing deployment. Use only the three private MCP tools. Preserve the current "
        "Traefik and LGTM containers, all existing named volumes, telemetry configuration, dashboard, and historical data. "
        "Keep GET / returning Hello, world! through Traefik and keep the full traces/logs/request-counter behavior working. "
        "Do not reset, reseed, replace, or install a prepared application. Choose and implement the application changes yourself.\n\n"
    )
    if phase == "upgrade":
        brief += (
            f"Upgrade the application so GET /version through Traefik returns HTTP 200 with exactly the UTF-8 bytes {version!r} "
            f"(no newline). Before changing it, preserve its previous source/configuration and runtime image for an independent "
            f"fresh rollback operator in the private directory {release!r}. Preserve every existing source/config text file "
            "including private env/config files, outside caches, dependencies, data/log directories and binaries; keep image "
            "and snapshots available. Create a private release.json in that directory with image_id (original runtime image "
            "ID) and files (array of objects with path relative to original app root, saved_path relative to this release "
            "directory, and sha256 of the preserved bytes). This is a verification index; choose the snapshot layout and "
            "upgrade implementation yourself. Close your MCP connections after verifying the release."
        )
    elif phase == "rollback":
        brief += (
            f"Roll back the application using the previous operator's preserved release at {release!r}. Restore its exact "
            f"pre-upgrade runtime image ID {image_id!r} and prior application source/configuration. Restore GET /version "
            f"through Traefik to HTTP {baseline_version['status']} and exactly the response bytes whose base64 is "
            f"{baseline_version['body_base64']!r}. Do not rebuild a replacement image. Verify the rollback and close your "
            "MCP connections. Remove source/config files introduced by the upgrade from the active app directory; keep "
            "rollback snapshots and operation receipts in the supplied separate private release directory."
        )
    else:
        brief += (
            f"Upgrade GET /version through Traefik to HTTP 200 with exactly the UTF-8 bytes {version!r} (no newline). "
            "Stage and build your own changes before final activation. There is a controlled gateway outage during this "
            "operation; the target and its durable jobs continue running. Submit exactly one final activation job with "
            f"idempotency_key={fault['key']!r}, wait_ms=10000, and a sufficient timeout. In that same job, before actual "
            f"activation, append the line {fault['marker']!r} exactly once to the private 0600 receipt file "
            f"{fault['receipt']!r}, emit that same line on stdout with immediate flushing, and wait 25 seconds. Then "
            "perform the actual activation in that original job. Do not activate before the barrier or use another "
            "activation job/key. These are fault-timing constraints, not application or deployment commands. If the MCP "
            "acknowledgement is uncertain, reconcile with lookup using the original supplied key and status on its original "
            "job ID. Never replay the activation under a new key. Do not submit other execute jobs while activation is "
            "unresolved; after it finishes, perform read-only verification and connection cleanup. Preserve the original "
            "failure and report any exact same-key deduplication retries honestly."
        )
    return brief + "\n\n" + (lab.ROOT / "lab" / "agent-tools.v1.md").read_text()


def live_events(path):
    if not path.exists():
        return []
    with path.open("rb") as source:
        body = source.read(demo_actor.STREAM_LIMIT + 1)
    if len(body) > demo_actor.STREAM_LIMIT:
        raise lab.LabError("Live actor evidence exceeded its quota.")
    # The writer may currently be halfway through an event. Only committed
    # complete lines participate in the fault barrier.
    return [json.loads(line) for line in body.split(b"\n")[:-1] if line]


def activation_items(events, key):
    started, completed = {}, {}
    for event in events:
        item = event.get("item") or {}
        if (item.get("type") == "mcp_tool_call" and item.get("server") == mcp_lab.SERVER_NAME
                and item.get("tool") == "run_command" and item.get("arguments", {}).get("action") == "execute"
                and item["arguments"].get("idempotency_key") == key):
            if event["type"] == "item.started":
                started[item["id"]] = item
            elif event["type"] == "item.completed":
                completed[item["id"]] = item
    return started, completed


def gateway_ssh():
    state, existing = mcp_lab.profile("control")
    if not state or not existing or existing.get("state") != "Running":
        raise lab.LabError("Owned control VM must be running before the gateway fault.")
    lab.verify_identity(state)
    # Capture once on the lock-owning main thread. The observer must never
    # change lab's process-global profile or read its mutable SSH_CONFIG/VM.
    profile = lab.PROFILES["control"]
    return ["ssh", "-F", str(profile.directory / "ssh_config"), profile.vm]


class GatewayFault:
    """A bounded observer; only fixed service lifecycle commands use SSH."""

    def __init__(self, directory, actor_path, specification, ssh, *, timeout):
        self.directory, self.actor_path, self.specification, self.ssh = directory, actor_path, specification, list(ssh)
        # The owner stops observation after the actor exits; observer failure
        # travels in the other direction and asks the launcher to cancel it.
        self.stop_observing = threading.Event()
        self.cancel_actor = threading.Event()
        self.deadline = time.monotonic() + timeout
        self.client = MCPClient(transcript=directory / "fault-observer.mcp.jsonl", request_timeout=5)
        self.record = {"success": False, "specification": specification, "control_ssh": self.ssh,
                       "intent_ns": time.time_ns(), "cleanup_errors": [], "read_only_retries": 0}
        self.thread = threading.Thread(target=self.observe, name="demo-gateway-fault")
        self.save()

    def save(self):
        save_json(self.directory / "gateway-fault.json", self.record)

    def service(self, action, *, deadline):
        if action not in {"start", "stop"}:
            raise ValueError("Only start/stop of the owned gateway are supported")
        timeout = min(GATEWAY_SERVICE_SECONDS, deadline - time.monotonic())
        if timeout <= 0:
            raise lab.LabError("Gateway service lifecycle deadline expired.")
        command = ("export XDG_RUNTIME_DIR=/run/user/$(id -u); "
                   "export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus; "
                   f"systemctl --user {action} sshmcp-gateway.service && "
                   "systemctl --user show sshmcp-gateway.service --property=ActiveState --value")
        lab.run(self.ssh + [command], label="Controlled gateway " + action, timeout=timeout,
                log_path=self.directory / ("gateway-" + action + ".log"))
        # lab.run writes stdout to the private log when log_path is supplied.
        text = (self.directory / ("gateway-" + action + ".log")).read_text().splitlines()
        expected = "active" if action == "start" else "inactive"
        if not text or text[-1].strip() != expected:
            raise lab.LabError("Gateway service did not reach the controlled " + expected + " state.")

    def lookup(self, *, deadline=None):
        result = self.client.request("tools/call", {"name": "run_command", "arguments": {
            "action": "lookup", "idempotency_key": self.specification["key"]}}, deadline=deadline)
        body = demo_actor.tool_body(result)
        if result.get("isError"):
            if body.get("code") == "not_found":
                return None
            raise lab.LabError("Read-only fault lookup failed; inspect private MCP evidence.")
        return body["job"]

    def require_observing(self):
        if self.stop_observing.is_set() or time.monotonic() >= self.deadline:
            raise lab.LabError("Actor ended or fault observation deadline expired.")

    def wait_for_barrier(self):
        cursors, output = {"stdout": 0, "stderr": 0}, bytearray()
        while not self.stop_observing.is_set() and time.monotonic() < self.deadline:
            started, completed = activation_items(live_events(self.actor_path), self.specification["key"])
            active = [item for identity, item in started.items() if identity not in completed]
            if active:
                if len(active) != 1 or active[0]["arguments"].get("wait_ms") != 10000:
                    raise lab.LabError("Fault activation must have one in-flight execute with wait_ms=10000.")
                job = self.lookup(deadline=self.deadline)
                self.require_observing()
                if job is not None:
                    observed = self.client.tool("run_command", action="status", job_id=job["job_id"],
                                                stdout_cursor=cursors["stdout"], stderr_cursor=cursors["stderr"], limit=16384,
                                                request_deadline=self.deadline)
                    self.require_observing()
                    if observed["job"]["job_id"] != job["job_id"]:
                        raise lab.LabError("Fault status did not identify the looked-up original job.")
                    for name, stream in observed["output"].items():
                        cursors[name] = stream["next_cursor"]
                        if name == "stdout":
                            output.extend(stream_bytes(stream))
                    if len(output) > demo_actor.STREAM_LIMIT:
                        raise lab.LabError("Fault readiness output exceeded its evidence bound.")
                    marker = self.specification["marker"].encode()
                    if marker in output.splitlines() and observed["job"]["state"] == "running":
                        # Recheck after MCP observation; an already-returned
                        # execute cannot be counted as an interrupted call.
                        latest_started, latest_completed = activation_items(live_events(self.actor_path), self.specification["key"])
                        if active[0]["id"] in latest_started and active[0]["id"] not in latest_completed:
                            return active[0], observed["job"]
            self.stop_observing.wait(.15)
        raise lab.LabError("Actor ended or fault deadline expired before the in-flight readiness barrier.")

    def observe(self):
        restoration_needed = False
        try:
            item, job = self.wait_for_barrier()
            self.require_observing()
            self.record.update(original_item_id=item["id"], original_job_id=job["job_id"],
                               barrier_state=job["state"], barrier_observed_ns=time.time_ns(),
                               marker_observed=True, execute_in_flight=True)
            self.save()
            restoration_needed = True
            self.record["stop_requested_ns"] = time.time_ns()
            self.service("stop", deadline=self.deadline)
            self.record["stopped_ns"] = time.time_ns()
            self.require_observing()
            try:
                self.client.request("tools/list", deadline=self.deadline)
            except Exception as error:
                self.record["outage_transport_error"] = {"type": type(error).__name__, "observed_ns": time.time_ns()}
            else:
                raise lab.LabError("MCP remained reachable during the supposed gateway outage.")
            self.save()
            self.require_observing()
            if self.stop_observing.wait(min(OUTAGE_SECONDS, self.deadline - time.monotonic())):
                raise lab.LabError("Actor ended while the gateway outage was in progress.")
            self.require_observing()
        except Exception as error:
            self.record["primary_error"] = lab.error_text(error)
            self.cancel_actor.set()
        finally:
            if restoration_needed:
                try:
                    self.record["restore_requested_ns"] = time.time_ns()
                    # Restoration is mandatory even after actor cancellation;
                    # optional read-only recovery stops between bounded calls.
                    self.service("start", deadline=time.monotonic() + GATEWAY_SERVICE_SECONDS)
                    self.record["restored_ns"] = time.time_ns()
                    # Explicit bounded retries of read-only recovery queries.
                    # No execute submission is made by this observer.
                    deadline = min(self.deadline, time.monotonic() + RECOVERY_SECONDS)
                    while True:
                        self.require_observing()
                        if time.monotonic() >= deadline:
                            raise lab.LabError("Gateway read-only recovery deadline expired.")
                        try:
                            job = self.lookup(deadline=deadline)
                            self.require_observing()
                            if job is None or job["job_id"] != self.record["original_job_id"]:
                                raise lab.LabError("Original activation key resolved to a missing/different job.")
                            self.record.update(recovered_job_id=job["job_id"], recovered_state=job["state"])
                            break
                        except Exception:
                            self.require_observing()
                            if time.monotonic() >= deadline:
                                raise
                            self.record["read_only_retries"] += 1
                            self.stop_observing.wait(min(.2, max(0, deadline - time.monotonic())))
                    if job["state"] != "running":
                        raise lab.LabError("Original activation was no longer running when the gateway recovered.")
                except Exception as error:
                    self.record["cleanup_errors"].append(lab.error_text(error))
            self.record["success"] = restoration_needed and "primary_error" not in self.record and not self.record["cleanup_errors"]
            self.record["finished_ns"] = time.time_ns()
            if not self.record["success"]:
                self.cancel_actor.set()
            try:
                self.save()
            except Exception as error:
                self.record["success"] = False
                self.record["cleanup_errors"].append(lab.error_text(error))
                self.cancel_actor.set()

    def finish(self):
        self.stop_observing.set()
        # Never let diagnostics or VM shutdown race this worker. Each pending
        # MCP call has a deadline, stop is checked before the next call, and
        # mandatory service restoration has its own finite cleanup budget.
        self.thread.join()
        if not self.record["success"]:
            raise lab.LabError(self.record.get("primary_error", "Gateway fault/recovery did not complete.") +
                               (" Cleanup: " + "; ".join(self.record["cleanup_errors"]) if self.record["cleanup_errors"] else ""))


def failed_tool(item):
    result = item.get("result") or {}
    body = demo_actor.tool_body(result)
    return bool(item.get("error") or item.get("status") != "completed" or result.get("isError") or result.get("is_error")
                or body.get("code") or body.get("wait_error"))


def retain_activation_output(coverage, final_cursors, arguments, output):
    """Union byte ranges so repeated or out-of-order reads can prove a full drain."""
    if set(output) != {"stdout", "stderr"}:
        raise lab.LabError("Original activation output omitted a stream.")
    for name, stream in output.items():
        start = arguments.get(name + "_cursor", 0) if arguments["action"] == "status" else 0
        end = stream.get("next_cursor")
        try:
            if stream.get("encoding") == "utf-8":
                data = stream["text"].encode("utf-8")
            elif stream.get("encoding") == "base64":
                data = base64.b64decode(stream["base64"], validate=True)
            else:
                raise ValueError("missing output encoding")
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            raise lab.LabError("Original activation output has invalid byte encoding.") from error
        if (type(start) is not int or type(end) is not int or start < 0 or end < start
                or end - start != len(data) or end > demo_actor.STREAM_LIMIT):
            raise lab.LabError("Original activation output cursor does not match its bounded returned bytes.")
        if stream.get("eof") is True and stream.get("final") is True:
            if final_cursors[name] is not None and final_cursors[name] != end:
                raise lab.LabError("Original activation output has inconsistent final cursors.")
            final_cursors[name] = end
        if data:
            merged = []
            for previous_start, previous_data in coverage[name]:
                previous_end = previous_start + len(previous_data)
                if previous_end < start or end < previous_start:
                    merged.append((previous_start, previous_data))
                    continue
                # Touching ranges merge too. Overlapping bytes must agree,
                # while any earlier prefix or later suffix remains retained.
                overlap_start, overlap_end = max(start, previous_start), min(end, previous_end)
                if data[overlap_start - start:overlap_end - start] != previous_data[overlap_start - previous_start:overlap_end - previous_start]:
                    raise lab.LabError("Original activation output changed across overlapping reads.")
                prefix = previous_data[:max(0, start - previous_start)]
                suffix = previous_data[max(0, end - previous_start):]
                data = prefix + data + suffix
                start, end = min(start, previous_start), max(end, previous_end)
            coverage[name] = sorted([*merged, (start, data)])
        if final_cursors[name] is not None and any(start + len(data) > final_cursors[name] for start, data in coverage[name]):
            raise lab.LabError("Original activation output extends beyond its final cursor.")


def activation_output_drained(coverage, final_cursors):
    for name in ("stdout", "stderr"):
        ranges, final_cursor = coverage[name], final_cursors[name]
        if final_cursor == 0 and not ranges:
            continue
        if len(ranges) != 1 or ranges[0][0] != 0 or len(ranges[0][1]) != final_cursor:
            return False
    return True


def validate_interruption(events, fault, final_job, receipt):
    """Require the actual lost acknowledgement and recovery of the same job."""
    if (not fault.get("success") or not fault.get("marker_observed") or not fault.get("execute_in_flight")
            or fault.get("barrier_state") != "running" or fault.get("recovered_state") != "running"
            or not fault.get("outage_transport_error")
            or fault.get("restored_ns", 0) - fault.get("stopped_ns", 0) < OUTAGE_SECONDS * 1_000_000_000):
        raise lab.LabError("Incomplete real gateway outage/in-flight execution evidence.")
    key, job_id = fault["specification"]["key"], fault["original_job_id"]
    started, completed = activation_items(events, key)
    original = completed.get(fault["original_item_id"])
    if original is None or not failed_tool(original):
        raise lab.LabError("Original in-flight activation has no preserved tool/transport failure.")
    if original["arguments"].get("wait_ms") != 10000 or fault.get("recovered_job_id") != job_id:
        raise lab.LabError("Interrupted execute/recovery does not identify the original waited job.")
    identity_fields = ("connection_id", "command", "commands", "timeout_ms", "cwd", "env")
    identity = {name: original["arguments"].get(name) for name in identity_fields}
    if any({name: item["arguments"].get(name) for name in identity_fields} != identity for item in (*started.values(), *completed.values())):
        raise lab.LabError("Activation was retried with a different execution identity.")
    lookup_seen, completion_seen, activation_seen, terminal_status_seen = False, False, False, False
    coverage = {name: [] for name in ("stdout", "stderr")}
    final_cursors = dict.fromkeys(coverage)
    source = (identity["command"], identity["commands"])
    for event in events:
        item = event.get("item") or {}
        if item.get("type") != "mcp_tool_call" or item.get("tool") != "run_command":
            continue
        args = item.get("arguments", {})
        if args.get("action") == "execute":
            same_key = args.get("idempotency_key") == key
            if same_key:
                activation_seen = True
            elif activation_seen and not completion_seen:
                raise lab.LabError("A new execute was submitted before the original activation was reconciled.")
            elif (args.get("command"), args.get("commands")) == source:
                raise lab.LabError("Activation source was replayed with a new idempotency key.")
        if event["type"] != "item.completed":
            continue
        body = demo_actor.tool_body(item.get("result"))
        job = body.get("job", {})
        original_output = (args.get("action") == "status" and args.get("job_id") == job_id
                           or args.get("action") == "execute" and args.get("idempotency_key") == key)
        if original_output and job.get("job_id") == job_id and "output" in body:
            # A waited execute can return real output with wait_error. Its
            # retained bytes still count even though its acknowledgement failed.
            retain_activation_output(coverage, final_cursors, args, body["output"])
        if failed_tool(item):
            continue
        if args.get("action") == "lookup" and args.get("idempotency_key") == key and job.get("job_id") == job_id:
            lookup_seen = True
        if (args.get("action") == "status" and args.get("job_id") == job_id and job.get("job_id") == job_id
                and job.get("state") == "completed" and job.get("exit_code") == 0):
            terminal_status_seen = True
        completion_seen = terminal_status_seen and activation_output_drained(coverage, final_cursors)
    if (not lookup_seen or not completion_seen or final_job.get("job_id") != job_id or final_job.get("state") != "completed"
            or final_job.get("exit_code") != 0 or not receipt.get("exists") or receipt.get("matching_lines") != 1 or receipt.get("lines") != 1):
        raise lab.LabError("Actor did not reconcile/complete the original activation exactly once.")
    attempts = len(set(started) | set(completed))
    return {"original_key": key, "original_job_id": job_id, "original_state": final_job["state"],
            "visible_activation_tool_calls": attempts, "same_key_visible_retries": attempts - 1,
            "duplicate_results": sum(bool(demo_actor.tool_body(item.get("result")).get("duplicate")) for item in completed.values()),
            "cli_internal_transport_retries": "not exposed",
            "original_failure_retained": True, "actor_lookup_reconciled": True, "receipt_lines": 1,
            "fault_stopped_ns": fault["stopped_ns"], "fault_restored_ns": fault["restored_ns"],
            "observer_read_only_retries": fault["read_only_retries"]}


def interrupted_actor(directory, prompt, specification, ssh, *, timeout):
    observer = GatewayFault(directory, directory / "actor" / "actor.jsonl", specification, ssh, timeout=timeout)
    observer.thread.start()
    primary, actor, cleanup_errors = None, None, []
    try:
        actor = demo_actor.run_actor(directory, "actor", prompt, timeout=timeout, cancel_event=observer.cancel_actor)
    except (Exception, KeyboardInterrupt) as error:
        primary = error
    finally:
        with lab.interrupt_handling(cleanup=True):
            lab.cleanup_step(observer.finish, cleanup_errors)
            save_json(directory / "interrupted-actor.json", {"primary_error": lab.error_text(primary) if primary else None,
                                                            "cleanup_errors": cleanup_errors})
    if primary is not None:
        if cleanup_errors and not isinstance(primary, (lab.LabInterrupted, KeyboardInterrupt)):
            raise lab.LabError(lab.error_text(primary) + " Cleanup: " + "; ".join(cleanup_errors)) from primary
        raise primary
    if cleanup_errors:
        raise lab.LabError("Gateway observer failed: " + "; ".join(cleanup_errors))
    # This final read does not submit or retry application work.
    job = observer.lookup()
    receipt = receipt_observation(directory, specification["receipt"], specification["marker"])
    proof = validate_interruption(list(demo_actor.transcript_events(directory / "actor" / "actor.jsonl")), observer.record, job or {}, receipt)
    save_json(directory / "interruption-proof.json", proof)
    return actor, proof


def exercise(directory, run_id, timeout):
    accepted = demo.ready_record()
    history = historical_probe(accepted)
    mcp_lab.require_enrolled_profiles()
    mcp_lab.up(directory)
    demo.tunnel_up(directory)
    baseline_dir = directory / "baseline"
    baseline_dir.mkdir(mode=0o700)
    baseline = demo.inspect_deployment(baseline_dir, "inspect")
    verify_architecture(baseline)
    context = deployment_context(baseline)
    baseline_http = http_observations(baseline_dir)
    original = demo.operator(baseline_dir, "source-before", SOURCE_SCRIPT % context["root"])
    original["image_id"] = baseline["app"]["Image"]
    prior_failures = require_recovered_baseline(accepted["run_id"], baseline, original, baseline_http["/version"])
    evaluator = demo.evaluator(baseline_dir, "evaluation", baseline)
    evaluator.evaluate()
    prove_history(evaluator, history)
    release = str(Path(context["root"]).parent / (".sshmcp-operations-" + run_id))
    version = "sshmcp-upgrade-" + run_id
    interrupted_version = "sshmcp-interrupted-" + run_id
    specification = {"key": "operations-activation-" + run_id, "marker": "sshmcp-ready-" + run_id,
                     "receipt": release + "/activation.receipt"}
    identity = demo.owners()
    before = installed_context()
    save_json(directory / "intent.json", {"run_id": run_id, "accepted": accepted, "owners": identity,
                                          "context": context, "baseline_image_id": original["image_id"],
                                          "baseline_source_hashes": {item["path"]: item["sha256"] for item in original["files"]},
                                          "baseline_version": baseline_http["/version"], "release": release,
                                          "upgrade_version": version, "interrupted_version": interrupted_version,
                                          "fault": specification, "installed_context": before, "human_interventions": 0})
    phases, actors = {}, []
    for phase, marker in (("upgrade", version), ("rollback", None), ("interrupted", interrupted_version)):
        folder = directory / phase
        folder.mkdir(mode=0o700)
        prompt = goal(phase, version=marker, release=release, baseline_version=baseline_http["/version"],
                      image_id=original["image_id"], fault=specification)
        print("Operations phase=" + phase, flush=True)
        if phase == "interrupted":
            receipt_observation(folder, specification["receipt"], specification["marker"], absent=True)
            actor, proof = interrupted_actor(folder, prompt, specification, gateway_ssh(), timeout=timeout)
            phases["interruption"] = proof
        else:
            actor = demo_actor.run_actor(folder, "actor", prompt, timeout=timeout)
        if any(previous["thread_id"] == actor["thread_id"] for previous in actors):
            raise lab.LabError("Operations phases reused an actor session.")
        actors.append({key: value for key, value in actor.items() if key != "feedback"})
        final, evidence = evaluate_phase(folder, baseline, baseline_http["/version"] if phase == "rollback" else expected_version(marker), history)
        if phase == "rollback":
            restored = demo.operator(folder, "source-after-rollback", SOURCE_SCRIPT % context["root"])
            require_rollback(original, final, restored)
            phases["rollback_source_hashes_restored"] = True
        if phase != "rollback" and final["app"]["Image"] == original["image_id"]:
            raise lab.LabError("Upgrade version was served without a new application runtime image.")
        if phase == "upgrade":
            phases["preserved_release"] = verify_release(folder, release, original)
        phases[phase] = {"app_image_id": evidence["app_image_id"], "version": evidence["http"]["/version"],
                         "fresh_trace_id": evidence["fresh"]["probe"]["trace_id"], "history_retained": True}
    sources = demo.export_sources(directory, final, include_python=True)
    after = installed_context()
    save_json(directory / "installed-context.after.json", after)
    summary = {"run_id": run_id, "success": True, "human_interventions": 0, "actors": actors, "phases": phases,
               "baseline_image_id": original["image_id"], "source_files": len(sources),
               "prior_failed_baselines_verified": prior_failures,
               "installed_context": before, "installed_config_unchanged": before.get("config_sha256") == after.get("config_sha256"),
               "quota": "unknown",
               "limits": "One private accepted demo, three fresh actors, bounded observations; raw sources/configuration and failures remain private."}
    save_json(directory / "export" / "report.json", summary)
    proposed_ready = {"run_id": run_id, "owners": identity, "evidence_directory": str(directory),
                      "accepted_deployment_run_id": accepted.get("accepted_deployment_run_id", accepted["run_id"]), "historical_probe": history}
    return summary, proposed_ready


def run_operation(*, actor_timeout=1800):
    lab.select_profile("control")
    run_id, directory = lab.new_results("demo-operations-test")
    result = {"operation": "operations-test", "run_id": run_id, "success": False,
              "started_ns": time.time_ns(), "cleanup_errors": []}
    error = None
    try:
        result["exercise"], ready = exercise(directory, run_id, actor_timeout)
        result["success"] = True
        save_json(directory / "result.json", result)
        # Advance the ready pointer only after every actor, evaluator, export,
        # and report has succeeded. The old attempt/evidence are never edited.
        save_json(demo.READY, ready)
    except (Exception, KeyboardInterrupt) as caught:
        error = caught
        result["primary_error"] = lab.error_text(caught)
    finally:
        with lab.interrupt_handling(cleanup=True):
            result["finished_ns"] = time.time_ns()
            result["success"] = error is None
            lab.cleanup_step(lambda: save_json(directory / "result.json", result), result["cleanup_errors"])
            if error is not None or result["cleanup_errors"]:
                lab.cleanup_step(lambda: record_failed_attempt(directory, run_id), result["cleanup_errors"])
                lab.cleanup_step(lambda: demo.diagnostics(directory), result["cleanup_errors"])
                lab.cleanup_step(lambda: save_json(directory / "result.json", result), result["cleanup_errors"])
                lab.cleanup_step(demo.down, result["cleanup_errors"])
            result["success"] = error is None and not result["cleanup_errors"]
            lab.cleanup_step(lambda: save_json(directory / "result.json", result), result["cleanup_errors"])
            lab.select_profile("control")
    print(f"Private operations evidence: {directory}", flush=True)
    if error is not None or result["cleanup_errors"]:
        code = 128 + error.signum if isinstance(error, lab.LabInterrupted) else 130 if isinstance(error, KeyboardInterrupt) else 1
        raise lab.LabError(result.get("primary_error", "Operations cleanup failed.") +
                           (" Cleanup: " + "; ".join(result["cleanup_errors"]) if result["cleanup_errors"] else ""), exit_code=code)
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor-timeout", type=int, default=1800)
    arguments = parser.parse_args()
    if not 60 <= arguments.actor_timeout <= 3600:
        parser.error("actor timeout must be between 60 and 3600 seconds")
    os.umask(0o077)
    try:
        lab.select_profile("control")
        with lab.interrupt_handling(), lab.workspace_lock():
            demo.private_directory()
            run_operation(actor_timeout=arguments.actor_timeout)
    except (lab.LabError, OSError) as error:
        print(f"Demo operations error: {error}", file=sys.stderr)
        return error.exit_code if isinstance(error, lab.LabError) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
