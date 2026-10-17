#!/usr/bin/env python3
"""Opt-in real OAuth -> MCP -> native SSH -> target runner checks.

Requires make mcp-lab-up. Does not restore snapshots or deploy an application.
The fault cases restart the gateway/runner and reboot the target. Its finally
block restores the standard runner service, disconnects this run's shells, and
stops both ownership-verified VMs. Evidence is private under .lab/results/.
"""

import base64
from concurrent.futures import ThreadPoolExecutor
import itertools
import json
from pathlib import Path
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import lab
import lab_oauth
import mcp_lab


class MCPClient:
    def __init__(self, account="admin", scope="sshmcp:admin"):
        self.token = lab_oauth.tokens(account, scope)
        self.expires = time.monotonic() + self.token["expires_in"]
        self.ids = itertools.count(1)
        self.refresh_lock = threading.Lock()

    def request(self, method, params=None, token=None):
        with self.refresh_lock:
            if token is None and time.monotonic() > self.expires - 5:
                result = lab_oauth.token_request({"grant_type": "refresh_token", "refresh_token": self.token["refresh_token"], "resource": mcp_lab.RESOURCE})
                if "access_token" not in result:
                    raise AssertionError("OAuth refresh rejected: " + result.get("error", "unknown"))
                self.token = result
                self.expires = time.monotonic() + result["expires_in"]
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
        if method == "tools/call":
            headers["Mcp-Name"] = params["name"]
        if token != "":
            headers["Authorization"] = "Bearer " + (self.token["access_token"] if token is None else token)
        params = dict(params or {})
        params["_meta"] = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}, "io.modelcontextprotocol/clientInfo": {"name": "cp5-integration", "version": "1"}}
        request = urllib.request.Request(mcp_lab.RESOURCE, data=json.dumps({"jsonrpc": "2.0", "id": next(self.ids), "method": method, "params": params}).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            error.close()
            raise
        if "error" in result:
            raise AssertionError(result["error"])
        return result["result"]

    def tool(self, name, expected_error=None, **arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        body = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        if expected_error:
            if not result.get("isError") or body.get("code") != expected_error:
                raise AssertionError((expected_error, result))
        elif result.get("isError"):
            raise AssertionError(body)
        return body


def stream_bytes(stream):
    return stream["text"].encode() if stream["encoding"] == "utf-8" else base64.b64decode(stream["base64"])


class PrivateMCPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lab.select_profile("control")
        cls.lock = lab.workspace_lock()
        cls.lock.__enter__()
        cls.connections = []
        # Ubuntu clears /tmp at boot; this run's side-effect proof must survive.
        cls.marker = f"/home/{lab.USER}/.sshmcp-cp5-{uuid.uuid4().hex}"
        _, cls.directory = lab.new_results("mcp-integration")
        cls.evidence = []
        cls.cleanup_errors = []
        try:
            for name in ("control", "target"):
                state, existing = mcp_lab.profile(name)
                if not state or not existing or existing.get("state") != "Running":
                    raise RuntimeError("Run make mcp-lab-up before this opt-in harness")
                lab.verify_identity(state)
                lab.check_ready(state, wait=False)
            mcp_lab.readiness()
            cls.client = MCPClient()
            cls.other = MCPClient("other")
        except BaseException:
            original = sys.exc_info()
            errors = []
            try:
                lab.cleanup_step(lambda: mcp_lab.diagnostic(cls.directory), errors)
                lab.cleanup_step(mcp_lab.down, errors)
                lab.cleanup_step(lambda: lab.write_private(cls.directory / "evidence.json", json.dumps({
                    "checks": cls.evidence, "primary_error": lab.error_text(original[1]),
                    "cleanup_errors": errors,
                }, indent=2) + "\n"), errors)
            finally:
                cls.lock.__exit__(*original)
            if errors:
                raise AssertionError("MCP setup failed: " + lab.error_text(original[1]) + "; cleanup failed: " + "; ".join(errors)) from original[1]
            raise

    @classmethod
    def tearDownClass(cls):
        errors = cls.cleanup_errors
        try:
            try:
                state, existing = mcp_lab.profile("target")
                if not state or not existing or existing.get("state") != "Running":
                    raise lab.LabError("Target is unavailable or stopped; service restoration and marker cleanup not completed.")
                lab.verify_identity(state)
            except Exception as error:
                errors.append(str(error))
            else:
                lab.cleanup_step(lambda: lab.install_runner(state), errors)
                lab.cleanup_step(lambda: lab.check_ready(state, wait=True), errors)
                lab.cleanup_step(lambda: lab.strict_ssh("rm -f -- " + cls.marker, label="Remove this MCP run marker", timeout=20), errors)
            for connection in cls.connections:
                lab.cleanup_step(lambda: cls.client.tool("disconnect", connection_id=connection), errors)
            lab.cleanup_step(lambda: mcp_lab.diagnostic(cls.directory), errors)
            lab.cleanup_step(mcp_lab.down, errors)
            lab.cleanup_step(lambda: lab.write_private(cls.directory / "evidence.json", json.dumps({"checks": cls.evidence, "cleanup_errors": errors}, indent=2) + "\n"), errors)
            print(f"Private MCP integration evidence: {cls.directory}")
        finally:
            cls.lock.__exit__(None, None, None)
        if errors:
            raise AssertionError("MCP cleanup failed: " + "; ".join(errors))

    def connect(self):
        connection = self.client.tool("connect", host_alias="lab-target")["connection"]["connection_id"]
        self.connections.append(connection)
        return connection

    def submit(self, connection, command, key=None, **fields):
        key = key or uuid.uuid4().hex
        return self.client.tool("run_command", action="execute", connection_id=connection, idempotency_key=key, command=command, **fields)

    def wait(self, job_id, timeout=40):
        deadline = time.monotonic() + timeout
        output = [bytearray(), bytearray()]
        cursors = [0, 0]
        while time.monotonic() < deadline:
            try:
                result = self.client.tool("run_command", action="status", job_id=job_id, stdout_cursor=cursors[0], stderr_cursor=cursors[1])
            except AssertionError as error:
                # Read-only polling can retry after an intentionally dropped SSH
                # acknowledgement. The execute path above never retries.
                if error.args and isinstance(error.args[0], dict) and error.args[0].get("code") == "acknowledgement_uncertain":
                    time.sleep(.1)
                    continue
                raise
            streams = result["output"]
            for index, name in enumerate(("stdout", "stderr")):
                output[index].extend(stream_bytes(streams[name]))
                cursors[index] = streams[name]["next_cursor"]
            if result["job"]["state"] not in ("accepted", "running") and all(streams[name]["eof"] and streams[name]["final"] for name in ("stdout", "stderr")):
                return result["job"], bytes(output[0]), bytes(output[1])
            time.sleep(.05)
        self.fail("Job did not finish before the integration deadline")

    def wait_started(self, job_id):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            result = self.client.tool("run_command", action="status", job_id=job_id)
            if b"started" in stream_bytes(result["output"]["stdout"]):
                return
            time.sleep(.05)
        self.fail("Job did not emit its start marker")

    def test_01_oauth_resource_scope_and_owner(self):
        initialized = self.client.request("server/discover")
        self.assertIn("2026-07-28", initialized["supportedVersions"])
        self.assertEqual({tool["name"] for tool in self.client.request("tools/list")["tools"]}, {"connect", "run_command", "disconnect"})
        for token in ("", "invalid.jwt.signature"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.client.request("tools/list", token=token)
            self.assertEqual(error.exception.code, 401)
        no_scope = MCPClient(scope="openid")
        with self.assertRaises(urllib.error.HTTPError) as error:
            no_scope.request("tools/list")
        self.assertEqual(error.exception.code, 403)
        try:
            unknown, unknown_verifier = lab_oauth.authorization_code(resource="http://127.0.0.1:18080/unknown")
        except RuntimeError as error:
            self.assertIn("HTTP 400", str(error))
        else:
            if "error" in unknown:
                self.assertEqual(unknown["error"], ["invalid_target"])
            else:
                # The pinned experimental implementation may defer resource
                # validation until exchange; no access token may be accepted.
                rejected = lab_oauth.token_request({"grant_type": "authorization_code", "code": unknown["code"][0], "redirect_uri": mcp_lab.CALLBACK, "code_verifier": unknown_verifier, "resource": "http://127.0.0.1:18080/unknown"})
                self.assertEqual(rejected.get("error"), "invalid_target")
        parameters, verifier = lab_oauth.authorization_code()
        mismatch = lab_oauth.token_request({"grant_type": "authorization_code", "code": parameters["code"][0], "redirect_uri": mcp_lab.CALLBACK, "code_verifier": verifier, "resource": "http://127.0.0.1:18080/unknown"})
        self.assertEqual(mismatch.get("error"), "invalid_target")
        for resource, expected in ((mcp_lab.RESOURCE, None), ("http://127.0.0.1:18080/unknown", "invalid_target"), (None, None), ([mcp_lab.RESOURCE, mcp_lab.RESOURCE], "invalid_request")):
            original = lab_oauth.tokens(scope="sshmcp:admin offline_access")
            fields = {"grant_type": "refresh_token", "refresh_token": original["refresh_token"], "scope": "sshmcp:admin offline_access"}
            if resource is not None:
                fields["resource"] = resource
            refreshed = lab_oauth.token_request(fields)
            if expected:
                self.assertEqual(refreshed.get("error"), expected)
            else:
                refreshed_claims = json.loads(base64.urlsafe_b64decode(refreshed["access_token"].split(".")[1] + "=="))
                self.assertEqual(refreshed_claims["aud"], mcp_lab.RESOURCE)
                self.assertTrue(refreshed_claims.get("sub"))
        claims = json.loads(base64.urlsafe_b64decode(self.client.token["access_token"].split(".")[1] + "=="))
        self.assertEqual(claims["aud"], mcp_lab.RESOURCE)
        self.assertEqual(claims["iss"], mcp_lab.ISSUER)
        connection = self.connect()
        job = self.submit(connection, "printf owned")["job"]["job_id"]
        self.wait(job)
        self.other.tool("run_command", expected_error="not_found", action="status", job_id=job)
        self.other.tool("disconnect", expected_error="not_found", connection_id=connection)
        self.evidence.append("exactly 3 tools; anonymous/bad token denied; scope enforced; exact resource audience; unknown/mismatched authorization-code resources rejected; refresh intended/omitted audience exact and unknown/duplicate resources rejected; principal isolation")

    def test_02_shell_state_incremental_output_and_dedup(self):
        one, two = self.connect(), self.connect()
        batch = self.client.tool("run_command", action="execute", connection_id=one, idempotency_key=uuid.uuid4().hex, commands=["cd /tmp", "retained=cp5; greet() { printf '%s' \"$retained\"; }", "printf ready; printf diagnostic >&2"])
        state, out, err = self.wait(batch["job"]["job_id"])
        self.assertEqual((state["state"], out, err), ("completed", b"ready", b"diagnostic"))
        job = self.submit(one, 'printf "%s|" "$PWD"; greet')["job"]["job_id"]
        self.assertEqual(self.wait(job)[1], b"/tmp|cp5")
        job = self.submit(two, 'printf "%s" "${retained-unset}"')["job"]["job_id"]
        self.assertEqual(self.wait(job)[1], b"unset")
        key = uuid.uuid4().hex
        command = "printf first; sleep 1; printf second; printf '\\000\\377' >&2"
        before = time.monotonic()
        job = self.submit(one, command, key)["job"]["job_id"]
        self.assertLess(time.monotonic() - before, 2, "long execute must return promptly")
        self.wait_started_if_output(job, b"first")
        first = self.client.tool("run_command", action="status", job_id=job, limit=3)
        self.assertEqual(stream_bytes(first["output"]["stdout"]), b"fir")
        self.assertNotIn(command, json.dumps(first))
        duplicate = self.submit(one, command, key)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["job"]["job_id"], job)
        self.client.tool("run_command", expected_error="idempotency_conflict", action="execute", connection_id=one, idempotency_key=key, command="false")
        self.assertEqual(self.client.tool("run_command", action="lookup", idempotency_key=key)["job"]["job_id"], job)
        state, out, err = self.wait(job)
        self.assertEqual((out, err), (b"firstsecond", b"\0\xff"))
        self.evidence.append("persistent cwd/variables/functions; independent shells; serial batch; prompt acceptance; byte cursors/text/binary; no command echo; dedup and conflict")

        # These valid sources previously crossed different JSON relay limits:
        # tabs nearly filled a 2 MiB acknowledgement before adding output, while
        # Go's HTML escaping expanded the > comment to more than 6 MiB on SSH.
        tail = '\nprintf "%8192s" x'
        for label, source, source_bytes in (
            ("tabs", ":" + "\t" * 1043498 + tail, 1043517),
            ("html", "#" + ">" * (1048014 - 1 - len(tail)) + tail, 1048014),
        ):
            with self.subTest(command_boundary=label):
                self.assertEqual(len(source.encode()), source_bytes)
                key = uuid.uuid4().hex
                accepted = self.submit(one, source, key)
                job = accepted["job"]["job_id"]
                state, out, err = self.wait(job)
                self.assertEqual((state["state"], out, err), ("completed", b" " * 8191 + b"x", b""))
                duplicate = self.submit(one, source, key)
                self.assertTrue(duplicate["duplicate"])
                self.assertEqual(duplicate["job"]["job_id"], job)
                self.assertEqual(self.client.tool("run_command", action="lookup", idempotency_key=key)["job"]["job_id"], job)
                status = self.client.tool("run_command", action="status", job_id=job)
                self.assertEqual(stream_bytes(status["output"]["stdout"]), out)
                self.assertLess(len(json.dumps(status)), 65536)
                self.assertNotIn("command", status["job"])
                self.evidence.append(f"{label} source boundary: {source_bytes} bytes through MCP/SSH/CLI/runner/worker; completed with 8192 stdout bytes; duplicate and lookup retain ID; bounded source-free status")

    def wait_started_if_output(self, job, expected):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if expected in stream_bytes(self.client.tool("run_command", action="status", job_id=job)["output"]["stdout"]):
                return
            time.sleep(.05)
        self.fail("Missing incremental output")

    def test_03_sixteen_jobs_cancel_deadline(self):
        connections = [self.connect() for _ in range(17)]
        def submit(index):
            return self.submit(connections[index], "printf started; sleep 7; printf parallel")["job"]["job_id"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(submit, range(16)))
        self.client.tool("run_command", expected_error="capacity", action="execute", connection_id=connections[16], idempotency_key=uuid.uuid4().hex, command="true")
        self.client.tool("run_command", expected_error="connection_busy", action="execute", connection_id=connections[1], idempotency_key=uuid.uuid4().hex, command="true")
        before = time.monotonic()
        self.client.tool("run_command", action="cancel", job_id=jobs[0])
        self.assertLess(time.monotonic() - before, 3)
        self.assertEqual(self.wait(jobs[0])[0]["state"], "cancelled")
        for job in jobs[1:]:
            self.assertEqual(self.wait(job)[0]["state"], "completed")
        deadline = self.submit(connections[16], "sleep 30", timeout_ms=100)["job"]["job_id"]
        self.assertEqual(self.wait(deadline)[0]["reason"], "command_timeout")
        self.evidence.append("16 active jobs; 17th and per-connection overlap denied; independent prompt cancel; execution deadline")

    def test_04_gateway_restart_ssh_loss_and_target_reboot(self):
        connection = self.connect()
        key = uuid.uuid4().hex
        command = f"umask 077; printf once >> {self.marker}; printf started; sleep 6; printf survived"
        job = self.submit(connection, command, key)["job"]["job_id"]
        self.wait_started(job)
        state, _ = mcp_lab.profile("control")
        lab.verify_identity(state)
        lab.strict_ssh("export XDG_RUNTIME_DIR=/run/user/$(id -u); export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus; systemctl --user restart sshmcp-gateway.service", label="Restart only private MCP gateway", timeout=20)
        mcp_lab.readiness()
        # A fresh HTTP client has no transport session, but owns the same IDs.
        self.client = MCPClient()
        self.assertEqual(self.wait(job)[0]["state"], "completed")
        job = self.submit(connection, "printf started; sleep 3; printf ssh-survived")["job"]["job_id"]
        self.wait_started(job)
        target_state = json.loads((lab.PROFILES["target"].directory / "state.json").read_text())
        lab.strict_ssh("sudo -n ss -K dst " + target_state["last_ip"] + " dport = :22", label="Drop only control-to-target SSH transports", timeout=20)
        self.assertEqual(self.wait(job)[1], b"startedssh-survived")
        reboot_key = uuid.uuid4().hex
        reboot_command = f"printf once >> {self.marker}; printf started; sleep 120; printf forbidden"
        reboot_job = self.submit(connection, reboot_command, reboot_key)["job"]["job_id"]
        self.wait_started(reboot_job)
        state, _ = mcp_lab.profile("target")
        lab.verify_identity(state)
        boot_before = lab.guest("cat", "/proc/sys/kernel/random/boot_id", label="Record target boot ID").strip()
        lab.guest("sudo", "-n", "reboot", label="Reboot owned target", timeout=20)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                boot_after = lab.guest("cat", "/proc/sys/kernel/random/boot_id", label="Wait for target reboot", timeout=15).strip()
                if boot_after != boot_before:
                    lab.verify_identity(state)
                    lab.check_ready(state, wait=True)
                    break
            except lab.LabError:
                pass
        else:
            self.fail("Target did not recover after reboot")
        lost = self.wait(reboot_job)[0]
        self.assertEqual(lost["state"], "lost")
        self.assertTrue(lost["shell_state_lost"])
        self.assertTrue(self.submit(connection, reboot_command, reboot_key)["duplicate"])
        observed_marker = lab.strict_ssh("cat -- " + self.marker, label="Verify no replay after restart/reboot", timeout=20)
        lab.write_private(self.directory / "replay-marker.json", json.dumps({
            "marker": self.marker, "boot_before": boot_before, "boot_after": boot_after,
            "expected": "onceonce", "observed": observed_marker,
        }, indent=2) + "\n")
        self.assertEqual(observed_marker, "onceonce", "See private replay-marker.json for boot and side-effect evidence")
        # Target restart does not reset the issuer or persistent identity state.
        self.assertEqual(MCPClient().request("tools/list")["tools"][0]["name"] in {"connect", "run_command", "disconnect"}, True)
        self.evidence.append("gateway restart retains application IDs; fresh OAuth client reuse; scoped SSH transport loss; target reboot produces lost without replay; auth survives target reboot")

    def test_05_abandoned_shell_and_active_lifetime_expire(self):
        state, _ = mcp_lab.profile("target")
        lab.verify_identity(state)
        short_timer_unit = lab.RUNNER_UNIT.replace(
            "daemon --socket %t/sshmcp-runner.sock",
            "daemon --socket %t/sshmcp-runner.sock --idle-timeout 2s --max-lifetime 5s --cancel-grace 100ms",
        )
        install_short_timer_script = (
            "export XDG_RUNTIME_DIR=/run/user/$(id -u); "
            "export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus; "
            "cat > \"$HOME/.config/systemd/user/sshmcp-runner.service\" <<'UNIT'\n"
            + short_timer_unit
            + "UNIT\nsystemctl --user daemon-reload\nsystemctl --user restart sshmcp-runner.service\n"
        )
        try:
            lab.strict_ssh("sh -s", label="Install short lab-only abandonment timers", timeout=30, input_text=install_short_timer_script)
            lab.check_runner_ready(wait=True)
            idle_connection = self.connect()
            time.sleep(4)
            idle_result = self.client.tool("run_command", action="status", connection_id=idle_connection)["connection"]
            self.assertEqual((idle_result["state"], idle_result["reason"]), ("lost", "idle_timeout"))
            self.evidence.append("abandoned persistent shell becomes lost/idle_timeout under short lab-only timers")
            active_connection = self.connect()
            job = self.submit(active_connection, "printf started; sleep 30; printf forbidden")["job"]["job_id"]
            self.wait_started(job)
            expired_job, stdout, _ = self.wait(job, timeout=15)
            self.assertEqual((expired_job["state"], expired_job["reason"]), ("failed", "max_lifetime"))
            self.assertEqual(stdout, b"started")
            active_result = self.client.tool("run_command", action="status", connection_id=active_connection)["connection"]
            self.assertEqual((active_result["state"], active_result["reason"]), ("lost", "max_lifetime"))
            self.evidence.append("active job becomes failed/max_lifetime and its connection lost/max_lifetime; trailing command never runs")
        finally:
            # Keep a test failure intact; class teardown reports restoration
            # errors separately and persists them after all cleanup attempts.
            lab.cleanup_step(lambda: lab.install_runner(state), self.cleanup_errors)
            lab.cleanup_step(lambda: lab.check_runner_ready(wait=True), self.cleanup_errors)


if __name__ == "__main__":
    unittest.main()
