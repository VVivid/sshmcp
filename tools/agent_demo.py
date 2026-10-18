#!/usr/bin/env python3
"""Owned demo lifecycle, independent actors, operator fault, and evidence.

The test is intentionally opt-in: it runs real coding-agent sessions. No path
automatically restores a baseline. Failed attempts are saved before shutdown.
"""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import threading
import time
import uuid

import lab
import mcp_lab
from mcp_client import MCPClient
import demo_actor
from demo_actor import save_json
from demo_evaluator import Evaluator, environment, verify_architecture


PRIVATE = lab.ROOT / ".lab" / "demo"
READY = PRIVATE / "ready.json"
ATTEMPT = PRIVATE / "attempt.json"
TUNNEL = PRIVATE / "target-tunnel.sock"
PROJECT = "sshmcp-demo"
BAD_ENDPOINT = "http://127.0.0.1:1"


def private_directory():
    lab.ensure_private_path(PRIVATE, directory=True)
    PRIVATE.mkdir(mode=0o700, exist_ok=True)


def target():
    state, existing = mcp_lab.profile("target")
    if not state or not existing or existing.get("state") != "Running":
        raise lab.LabError("Owned target must be running for this operator action.")
    lab.verify_identity(state)
    return state


def operator(directory, name, script, *, timeout=45):
    """Run a separate recorded MCP operator after infrastructure ownership checks."""
    target()
    folder = directory / "operator"
    folder.mkdir(mode=0o700, exist_ok=True)
    lab.write_private(folder / (name + ".input.py"), script)
    stdout_path, stderr_path = folder / (name + ".jsonl"), folder / (name + ".stderr.log")
    lab.write_private(stdout_path, "")
    lab.write_private(stderr_path, "")
    started = time.monotonic()
    outcome = {"transport": "mcp", "success": False, "timeout_seconds": timeout,
               "started_ns": time.time_ns(),
               "idempotency_key": "demo-operator-" + uuid.uuid4().hex, "cleanup_errors": []}
    save_json(folder / (name + ".process.json"), outcome)
    client, connection_id, error, value = None, None, None, None
    try:
        client = MCPClient(transcript=folder / (name + ".mcp.jsonl"), request_timeout=min(timeout, 15))
        connection = client.tool("connect", host_alias="lab-target")["connection"]
        connection_id = connection["connection_id"]
        if connection["state"] != "active":
            raise lab.LabError("Independent operator did not obtain an active MCP connection.")
        accepted = client.tool("run_command", action="execute", connection_id=connection_id,
                               idempotency_key=outcome["idempotency_key"], command="python3 -c " + shlex.quote(script),
                               timeout_ms=int(timeout * 1000))
        save_json(folder / (name + ".job.json"), accepted["job"])
        job, stdout, stderr = client.wait(accepted["job"]["job_id"], timeout=timeout + 10)
        save_json(folder / (name + ".job.json"), job)
        bound = 8 * 1024 * 1024
        outcome.update({"stdout_bytes": len(stdout), "stderr_bytes": len(stderr),
                        "output_truncated": len(stdout) > bound or len(stderr) > bound})
        for path, body in ((stdout_path, stdout), (stderr_path, stderr)):
            descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "wb") as output:
                output.write(body[:bound])
                output.flush()
                os.fsync(output.fileno())
        if outcome["output_truncated"]:
            raise lab.LabError("Operator evidence exceeded the finite export bound; bounded prefixes were retained.")
        if job["state"] != "completed" or job["exit_code"] != 0:
            raise lab.LabError(f"Independent MCP operator {name} failed; private input, output, job, and transcript were saved.")
        value = json.loads(stdout)
    except (Exception, KeyboardInterrupt) as caught:
        error = caught
        outcome["primary_error"] = lab.error_text(caught)
    finally:
        with lab.interrupt_handling(cleanup=True):
            if connection_id is not None:
                def disconnect():
                    closed = client.tool("disconnect", connection_id=connection_id)
                    if closed["connection"]["state"] != "closed":
                        raise lab.LabError("Independent operator MCP connection did not close.")
                lab.cleanup_step(disconnect, outcome["cleanup_errors"])
            outcome["duration_seconds"] = time.monotonic() - started
            outcome["success"] = error is None and not outcome["cleanup_errors"]
            lab.cleanup_step(lambda: save_json(folder / (name + ".process.json"), outcome), outcome["cleanup_errors"])
    if error is not None:
        # Preserve interruption types for the outer lifecycle's signal exit code.
        # Both primary and disconnect failures remain in the private outcome.
        if outcome["cleanup_errors"] and not isinstance(error, (lab.LabInterrupted, KeyboardInterrupt)):
            raise lab.LabError(outcome["primary_error"] + " Cleanup: " + "; ".join(outcome["cleanup_errors"])) from error
        raise error
    if outcome["cleanup_errors"]:
        raise lab.LabError("Independent operator cleanup failed: " + "; ".join(outcome["cleanup_errors"]))
    return value


def fresh_target(directory):
    result = operator(directory, "fresh-target", r'''import json,pathlib,shutil,subprocess
docker = shutil.which('docker')
packages = subprocess.check_output(
    ['dpkg-query', '-W', '-f=${Package} ${db:Status-Status}\n'], text=True)
installed = [
    line for line in packages.splitlines()
    if line.endswith(' installed') and line.split()[0] in {
        'docker-ce', 'docker-ce-cli', 'docker.io', 'containerd.io',
        'docker-compose-plugin', 'docker-compose'}
]
artifacts = []
for root in (pathlib.Path.home(), pathlib.Path('/opt'), pathlib.Path('/srv')):
    for pattern in ('*.go', 'compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml', 'Dockerfile'):
        artifacts.extend(str(path) for path in root.rglob(pattern) if path.is_file())
print(json.dumps({
    'docker_binary': docker,
    'docker_packages': installed,
    'application_artifacts': artifacts,
    'docker_data_exists': pathlib.Path('/var/lib/docker').exists(),
    'os_release': pathlib.Path('/etc/os-release').read_text(),
}))
''')
    if result["docker_binary"] or result["docker_packages"] or result["application_artifacts"] or result["docker_data_exists"]:
        raise lab.LabError("Target is not fresh: Docker or application artifacts exist. Preserve evidence and use explicit agent-demo-reset before another attempt.")
    return result


def demo_tunnel_command(*arguments):
    return ["ssh", "-F", str(lab.PROFILES["target"].directory / "ssh_config"), "-o", "ClearAllForwardings=no", "-o", "ExitOnForwardFailure=yes",
            "-o", "ControlMaster=yes", "-o", "ControlPersist=no", "-S", str(TUNNEL), *arguments, lab.PROFILES["target"].vm]


def check_tunnel():
    if TUNNEL.is_symlink():
        raise lab.LabError("Demo tunnel socket is a symlink.")
    if TUNNEL.exists():
        info = TUNNEL.stat()
        if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise lab.LabError("Demo tunnel socket has an unexpected owner, type, or mode.")


def tunnel_up(directory):
    target()
    check_tunnel()
    if TUNNEL.exists():
        lab.run(demo_tunnel_command("-O", "check"), label="Check exact demo tunnel", timeout=15)
    else:
        lab.run(demo_tunnel_command("-f", "-N", "-L", "127.0.0.1:18090:127.0.0.1:8080", "-L", "127.0.0.1:13000:127.0.0.1:3000"),
                label="Forward private demo HTTP and Grafana", timeout=20, log_path=directory / "demo-tunnel.log")


def tunnel_down():
    check_tunnel()
    if TUNNEL.exists():
        lab.run(demo_tunnel_command("-O", "exit"), label="Close exact demo tunnel", timeout=15)


def down():
    errors = []
    lab.cleanup_step(tunnel_down, errors)
    lab.cleanup_step(mcp_lab.down, errors)
    if errors:
        raise lab.LabError("; ".join(errors))


def benchmark(directory):
    client = MCPClient(transcript=directory / "benchmark-mcp.jsonl", request_timeout=8)
    connections, cleanup_errors = [], []
    summary = {}
    try:
        for _ in range(16):
            connections.append(client.tool("connect", host_alias="lab-target")["connection"]["connection_id"])

        def warm(connection):
            started = time.monotonic()
            job, stdout, stderr = client.execute(connection, "printf warm", timeout=10)
            if job["state"] != "completed" or job["exit_code"] != 0 or stdout != b"warm" or stderr:
                raise lab.LabError("Warm MCP command failed; no benchmark success was inferred.")
            return time.monotonic() - started

        with ThreadPoolExecutor(max_workers=16) as pool:
            warm_times = list(pool.map(warm, connections))
        gate = threading.Event()
        jobs = []

        def work(item):
            index, connection = item
            gate.wait()
            accepted = client.tool("run_command", action="execute", connection_id=connection,
                                   idempotency_key="demo-bench-" + uuid.uuid4().hex,
                                   command=f"sleep 2; printf bench-{index}", timeout_ms=10000)
            job, stdout, stderr = client.wait(accepted["job"]["job_id"], timeout=12)
            if job["state"] != "completed" or job["exit_code"] != 0 or stdout != f"bench-{index}".encode() or stderr:
                raise lab.LabError("A concurrent MCP job did not complete with its own exact result.")
            return job

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(work, item) for item in enumerate(connections)]
            started = time.monotonic()
            gate.set()
            jobs = [future.result() for future in futures]
            elapsed = time.monotonic() - started
        # The durable guest timestamps independently prove overlap; elapsed
        # includes dispatch, acknowledgements, execution, and final output drain.
        from datetime import datetime
        parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        overlap = max(parse(job["started_at"]) for job in jobs) < min(parse(job["finished_at"]) for job in jobs)
        summary = {"jobs": jobs, "dispatch_to_drained_seconds": elapsed, "all_jobs_overlap": overlap,
                   "warm_command_seconds": warm_times, "warm_median_seconds": sorted(warm_times)[8], "warm_max_seconds": max(warm_times)}
        save_json(directory / "benchmark.json", summary)
        if elapsed > 8 or not overlap:
            raise lab.LabError("Sixteen warmed sleep-2 MCP jobs did not overlap and complete within eight seconds from dispatch.")
        return summary
    finally:
        def close(connection):
            lab.cleanup_step(lambda: client.tool("disconnect", connection_id=connection), cleanup_errors)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(close, connections))
        save_json(directory / "benchmark-cleanup.json", {"errors": cleanup_errors})
        events = [json.loads(line) for line in (directory / "benchmark-mcp.jsonl").read_text().splitlines()]
        summary["tool_calls"] = sum(event["event"] == "request" and event["method"] == "tools/call" for event in events)
        summary["transport_errors"] = sum(event["event"] == "error" for event in events)
        summary["automatic_retries"] = 0
        summary["human_interventions"] = 0
        save_json(directory / "benchmark.json", summary)
        if cleanup_errors and sys.exc_info()[0] is None:
            raise lab.LabError("Benchmark connection cleanup failed; private errors retained.")


INSPECT_SCRIPT = r'''import json,subprocess
ids = subprocess.check_output(['sudo', '-n', 'docker', 'ps', '-aq'], text=True).split()
if not ids:
    raise RuntimeError('No actor-created containers exist')
values = json.loads(subprocess.check_output(['sudo', '-n', 'docker', 'inspect', *ids], text=True))
containers = {}
for container in values:
    labels = container['Config'].get('Labels') or {}
    if labels.get('com.docker.compose.project') != 'sshmcp-demo':
        raise RuntimeError('Unexpected container outside the demo project')
    service = labels.get('com.docker.compose.service')
    if service in containers:
        raise RuntimeError('Duplicate Compose service')
    containers[service] = container
print(json.dumps(containers))
'''


def inspect_deployment(directory, name):
    return operator(directory, name, INSPECT_SCRIPT)


def evaluator(directory, name, containers):
    env = environment(containers["lgtm"])
    password = env.get("GF_SECURITY_ADMIN_PASSWORD", "")
    if len(password) < 16 or password == "admin":
        raise lab.LabError("Actor must set a generated private Grafana password, available only in private operator evidence.")
    return Evaluator(directory / name, username=env.get("GF_SECURITY_ADMIN_USER", "admin"), password=password)


def inject_fault(directory, containers, run_id):
    labels = containers["app"]["Config"]["Labels"]
    root = labels.get("com.docker.compose.project.working_dir", "")
    files = labels.get("com.docker.compose.project.config_files", "").split(",")
    # An extra Compose override changes exactly one environment value; the
    # original agent-created source/config remain intact for diagnosis/export.
    script = r'''import json,pathlib,subprocess
root = pathlib.Path(%r).resolve()
if not root.is_relative_to(pathlib.Path.home()) or root == pathlib.Path.home():
    raise RuntimeError('Unsafe deployment root')
files = %r
if not files or any(not pathlib.Path(path).is_file() or not pathlib.Path(path).resolve().is_relative_to(root) for path in files):
    raise RuntimeError('Unsafe Compose paths')
override = root / %r
if override.exists() or override.is_symlink():
    raise RuntimeError('Fault override already exists')
with override.open('x') as output:
    json.dump({'services': {'app': {'environment': {'OTEL_EXPORTER_OTLP_ENDPOINT': %r}}}}, output)
override.chmod(0o600)
command = ['sudo', '-n', 'docker', 'compose', '-p', 'sshmcp-demo']
for path in files + [str(override)]:
    command += ['-f', path]
result = subprocess.run(
    command + ['up', '-d', '--no-deps', '--force-recreate', 'app'],
    cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
print(json.dumps({
    'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
    'override': str(override), 'endpoint': %r,
}))
if result.returncode:
    raise RuntimeError('Fault recreation failed')
''' % (root, files, ".sshmcp-fault-" + run_id + ".json", BAD_ENDPOINT, BAD_ENDPOINT)
    result = operator(directory, "inject-fault", script, timeout=140)
    observed = inspect_deployment(directory, "after-fault")
    verify_architecture(observed)
    if environment(observed["app"]).get("OTEL_EXPORTER_OTLP_ENDPOINT") != BAD_ENDPOINT or observed["app"]["Id"] == containers["app"]["Id"]:
        raise lab.LabError("Fault operator did not recreate only the app with the controlled generic OTLP endpoint.")
    for name in ("traefik", "lgtm"):
        if observed[name]["Id"] != containers[name]["Id"]:
            raise lab.LabError("Fault operator unexpectedly replaced an independent telemetry/proxy service.")
    return observed, result


EXPORT_SCRIPT = r'''import base64,hashlib,json,os,pathlib
root = pathlib.Path(%r).resolve()
if not root.is_relative_to(pathlib.Path.home()) or root == pathlib.Path.home():
    raise RuntimeError('Unsafe source export root')
excluded_dirs = {'.git', 'vendor', 'node_modules', 'data', 'logs', 'cache', '.cache', '__pycache__'}
suffixes = {'.go', '.mod', '.sum', '.yaml', '.yml', '.json', '.toml', '.conf', '.sh', '.md', '.env'}
files = []
excluded = []
total = 0
for current, directories, names in os.walk(root, followlinks=False):
    for name in list(directories):
        path = pathlib.Path(current) / name
        if path.is_symlink():
            raise RuntimeError('Source export refuses directory symlinks')
        if name in excluded_dirs:
            directories.remove(name)
            excluded.append({'path': str(path.relative_to(root)), 'reason': 'data/cache/dependency directory'})
    for name in sorted(names):
        path = pathlib.Path(current) / name
        relative = str(path.relative_to(root))
        if path.is_symlink():
            raise RuntimeError('Source export refuses file symlinks')
        if not path.is_file():
            raise RuntimeError('Unexpected source file type')
        if path.suffix not in suffixes and not name.startswith(('Dockerfile', '.env')) and name not in {'Makefile', '.dockerignore', '.gitignore'}:
            excluded.append({'path': relative, 'reason': 'not source/config text'})
            continue
        if path.stat().st_size > 262144:
            raise RuntimeError('Source/config file exceeds 256KiB export bound')
        body = path.read_bytes()
        body.decode('utf-8')
        total += len(body)
        if len(files) >= 128 or total > 4194304:
            raise RuntimeError('Source/config export exceeds finite bound')
        files.append({'path': relative, 'body': base64.b64encode(body).decode(), 'sha256': hashlib.sha256(body).hexdigest()})
print(json.dumps({'root': str(root), 'files': files, 'excluded': excluded}))
'''


def redact(text, secrets):
    for value in sorted(secrets, key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED]")
    # Credentials should already live only in excluded private env files. This
    # second check also removes accidental literal password/token assignments.
    pattern = r'(?im)((?:password|secret|api[_-]?key|access[_-]?token)\s*[=:]\s*[\x22\x27]?)([^\s\x22\x27,}]+)'
    return re.sub(pattern, lambda match: match[0] if match[2].startswith(("${", "$", "[REDACTED]")) else match[1] + "[REDACTED]", text)


def export_sources(directory, containers, *, include_python=False):
    labels = containers["app"]["Config"]["Labels"]
    root = labels.get("com.docker.compose.project.working_dir", "")
    for container in containers.values():
        if container["Config"]["Labels"].get("com.docker.compose.project.working_dir") != root:
            raise lab.LabError("Demo source/config do not share a bounded deployment directory.")
        for mount in container.get("Mounts", []):
            if mount["Type"] == "bind" and mount.get("Destination", "").startswith(("/data", "/var/lib", "/var/log")):
                raise lab.LabError("Demo data/logs must use named volumes, separate from source/config export.")
    script = EXPORT_SCRIPT
    if include_python:
        script = script.replace("suffixes = {'.go'", "suffixes = {'.py', '.go'", 1)
    original = operator(directory, "source-export", script % root)
    if not any(item["path"].endswith(".go") for item in original["files"]):
        raise lab.LabError("Actual agent-created export contains no Go source.")
    secrets = [value for container in containers.values() for key, value in environment(container).items() if re.search("PASSWORD|SECRET|TOKEN|API_KEY", key) and len(value) >= 8]
    exported = directory / "export"
    exported.mkdir(mode=0o700, exist_ok=True)
    source = exported / "source"
    source.mkdir(mode=0o700)
    manifest = []
    for item in original["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise lab.LabError("Unsafe path in actual source export.")
        if any(re.search(r"(^\.env($|\.)|secret|credential|password|private|\.pem$|\.key$)", part, re.I) for part in relative.parts):
            continue
        text = base64.b64decode(item["body"], validate=True).decode()
        sanitized = redact(text, secrets)
        path = source / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lab.write_private(path, sanitized)
        manifest.append({"path": str(relative), "original_sha256": item["sha256"], "export_sha256": hashlib.sha256(sanitized.encode()).hexdigest(), "redacted": sanitized != text})
    save_json(exported / "source-manifest.json", manifest)
    return manifest


def diagnostics(directory):
    errors = []
    lab.cleanup_step(lambda: mcp_lab.diagnostic(directory), errors)
    # Each inspection independently verifies target identity and never starts a
    # stopped VM. Raw logs/config can contain secrets and remain private.
    script = r'''import json,shutil,subprocess
result = {}
if shutil.which('docker'):
    process = subprocess.run(
        ['sudo', '-n', 'docker', 'ps', '-a', '--no-trunc'],
        capture_output=True, text=True, timeout=15)
    result['ps'] = {'stdout': process.stdout, 'stderr': process.stderr, 'exit_code': process.returncode}
    ids = subprocess.check_output(
        ['sudo', '-n', 'docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=sshmcp-demo'],
        text=True).split()
    result['containers'] = {}
    for identity in ids[:8]:
        process = subprocess.run(
            ['sudo', '-n', 'docker', 'logs', '--tail', '100', identity],
            capture_output=True, text=True, timeout=10)
        result['containers'][identity] = {'stdout': process.stdout, 'stderr': process.stderr, 'exit_code': process.returncode}
print(json.dumps(result))
'''
    observed = None
    try:
        observed = operator(directory, "failure-containers", script, timeout=100)
    except (Exception, KeyboardInterrupt) as error:
        errors.append(lab.error_text(error))
    if observed and observed.get("containers"):
        try:
            containers = inspect_deployment(directory, "diagnostic-inspect")
            if "app" in containers and not (directory / "export" / "source").exists():
                export_sources(directory, containers)
        except (Exception, KeyboardInterrupt) as error:
            errors.append(lab.error_text(error))
    save_json(directory / "diagnostic-errors.json", errors)
    if errors:
        raise lab.LabError("; ".join(errors))


def owners():
    result = {}
    for name in ("control", "target"):
        lab.select_profile(name)
        state = lab.load_state()
        if state is None:
            raise lab.LabError("Both enrolled profile records are required.")
        result[name] = {"owner": state["owner"], "host_key": state["host_key"], "baseline": state["baseline"]}
    return result


def invalidate_ready():
    lab.ensure_private_path(READY)
    if READY.exists():
        READY.unlink()


def ready_record():
    lab.ensure_private_path(READY)
    if not READY.exists():
        raise lab.LabError("No evaluated demo exists. Run agent-demo-test for a new exercise; up only resumes an existing demo.")
    record = json.loads(READY.read_text())
    if record.get("owners") != owners():
        raise lab.LabError("Demo-ready ownership/baseline does not match current profiles.")
    return record


def test_demo(directory, run_id, timeout):
    lab.ensure_private_path(ATTEMPT)
    if ATTEMPT.exists():
        raise lab.LabError("An actor attempt already exists. Preserve its evidence and explicitly agent-demo-reset before another attempt.")
    mcp_lab.require_enrolled_profiles()
    identity = owners()
    mcp_lab.up(directory)
    fresh_target(directory)
    invalidate_ready()
    measured = benchmark(directory)
    # Persist intent before the first model run; a failed/partial actor never
    # becomes an implicit baseline reset or a coached continuation.
    save_json(ATTEMPT, {"run_id": run_id, "owners": identity})
    prompt = "\n\n".join((lab.ROOT / "lab" / name).read_text() for name in ("agent-goal.v1.md", "agent-tools.v1.md"))
    deployed = demo_actor.run_actor(directory, "deployment-actor", prompt, timeout=timeout)
    tunnel_up(directory)
    containers = inspect_deployment(directory, "deployment-inspect")
    verify_architecture(containers)
    deployed_evidence = evaluator(directory, "deployment-evaluation", containers).evaluate()
    broken, _ = inject_fault(directory, containers, run_id)
    fault = evaluator(directory, "fault-evaluation", broken).prove_fault(deployed_evidence["counters"])
    repaired = demo_actor.run_actor(directory, "repair-actor", demo_actor.REPAIR_PROMPT, timeout=timeout)
    if repaired["thread_id"] == deployed["thread_id"]:
        raise lab.LabError("Troubleshooting reused the deployment session.")
    final = inspect_deployment(directory, "repair-inspect")
    verify_architecture(final)
    if environment(final["app"])["OTEL_EXPORTER_OTLP_ENDPOINT"] == BAD_ENDPOINT:
        raise lab.LabError("Repair left the generic endpoint at the controlled fault value.")
    repaired_evidence = evaluator(directory, "repair-evaluation", final).evaluate()
    sources = export_sources(directory, final)
    summary = {"run_id": run_id, "success": True, "human_interventions": 0,
               "benchmark_seconds": measured["dispatch_to_drained_seconds"], "warm_command_max_seconds": measured["warm_max_seconds"],
               "actors": [{key: value for key, value in actor.items() if key not in {"feedback"}} for actor in (deployed, repaired)],
               "deployment_trace_id": deployed_evidence["probe"]["trace_id"], "fault_trace_id": fault["probe"]["trace_id"],
               "repair_trace_id": repaired_evidence["probe"]["trace_id"], "source_files": len(sources),
               "demo_http": "http://127.0.0.1:18090/", "grafana": "http://127.0.0.1:13000/",
               "limits": "One real run, private VMs, bounded export waits; actor feedback and raw operator/backend/tool evidence remain private."}
    save_json(directory / "export" / "report.json", summary)
    save_json(READY, {"run_id": run_id, "owners": identity, "evidence_directory": str(directory)})
    return summary


def resume_demo(directory):
    record = ready_record()
    mcp_lab.require_enrolled_profiles()
    mcp_lab.up(directory)
    tunnel_up(directory)
    containers = inspect_deployment(directory, "resumed-inspect")
    verify_architecture(containers)
    result = evaluator(directory, "resumed-evaluation", containers).evaluate()
    return {"resumed_run_id": record["run_id"], "fresh_trace_id": result["probe"]["trace_id"]}


def reset_demo(directory):
    # Invalidate before any possibly destructive target operation. Prior result
    # directories, source exports, accounts, control disk, and OAuth are retained.
    invalidate_ready()
    tunnel_down()
    state, existing = mcp_lab.profile("target")
    lab.reset(state, existing)
    lab.ensure_private_path(ATTEMPT)
    if ATTEMPT.exists():
        ATTEMPT.unlink()
    return {"reset_profile": "target", "prior_evidence_preserved": True}


def run_operation(command, *, actor_timeout=1800):
    """One lock owner coordinates direct lifecycle functions, never child CLIs."""
    lab.select_profile("control")
    run_id, directory = lab.new_results("demo-" + command)
    result = {"operation": command, "run_id": run_id, "success": False, "started_ns": time.time_ns(), "cleanup_errors": []}
    error = None
    try:
        if command == "test":
            result["exercise"] = test_demo(directory, run_id, actor_timeout)
        elif command == "up":
            result["exercise"] = resume_demo(directory)
        elif command == "reset":
            result["exercise"] = reset_demo(directory)
        elif command == "down":
            down()
        elif command == "benchmark":
            mcp_lab.require_enrolled_profiles()
            mcp_lab.up(directory)
            fresh_target(directory)
            result["benchmark"] = benchmark(directory)
        elif command == "isolation-check":
            folder = directory / "isolation"
            folder.mkdir(mode=0o700)
            workspace = demo_actor.new_workspace(folder)
            arguments = demo_actor.isolated_arguments(folder, workspace)
            probe = demo_actor.capture(arguments + ["debug", "prompt-input", demo_actor.REPAIR_PROMPT], folder, "prompt-input", cwd=workspace)
            if probe["exit_code"]:
                raise lab.LabError("Installed-Codex prompt check failed.")
            demo_actor.assert_isolated(json.loads((folder / "prompt-input.jsonl").read_text()))
        result["success"] = True
    except (Exception, KeyboardInterrupt) as caught:
        error = caught
        result["primary_error"] = lab.error_text(caught)
    finally:
        with lab.interrupt_handling(cleanup=True):
            # Persist the attempt and any partial transcripts BEFORE diagnostics
            # and ownership-checked shutdown. A later save adds cleanup errors.
            result["finished_ns"] = time.time_ns()
            lab.cleanup_step(lambda: save_json(directory / "result.json", result), result["cleanup_errors"])
            failed = error is not None or bool(result["cleanup_errors"])
            if failed and command == "test":
                lab.cleanup_step(invalidate_ready, result["cleanup_errors"])
            if failed and command in {"test", "up", "benchmark"}:
                lab.cleanup_step(lambda: diagnostics(directory), result["cleanup_errors"])
                lab.cleanup_step(lambda: save_json(directory / "result.json", result), result["cleanup_errors"])
                lab.cleanup_step(down, result["cleanup_errors"])
            elif command == "benchmark":
                lab.cleanup_step(lambda: diagnostics(directory), result["cleanup_errors"])
                lab.cleanup_step(lambda: save_json(directory / "result.json", result), result["cleanup_errors"])
                lab.cleanup_step(down, result["cleanup_errors"])
            result["success"] = error is None and not result["cleanup_errors"]
            save_json(directory / "result.json", result)
            lab.select_profile("control")
    print(f"Private demo evidence: {directory}", flush=True)
    if error is not None or result["cleanup_errors"]:
        code = 128 + error.signum if isinstance(error, lab.LabInterrupted) else 130 if isinstance(error, KeyboardInterrupt) else 1
        raise lab.LabError(result.get("primary_error", "Demo cleanup failed.") + (" Cleanup: " + "; ".join(result["cleanup_errors"]) if result["cleanup_errors"] else ""), exit_code=code)
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("test", "up", "down", "reset", "benchmark", "isolation-check"))
    parser.add_argument("--actor-timeout", type=int, default=1800)
    arguments = parser.parse_args()
    if not 60 <= arguments.actor_timeout <= 3600:
        parser.error("actor timeout must be between 60 and 3600 seconds")
    os.umask(0o077)
    try:
        lab.select_profile("control")
        with lab.interrupt_handling(), lab.workspace_lock():
            private_directory()
            run_operation(arguments.command, actor_timeout=arguments.actor_timeout)
    except (lab.LabError, OSError) as error:
        print(f"Demo error: {error}", file=sys.stderr)
        return error.exit_code if isinstance(error, lab.LabError) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
