"""Opt-in current-runner checks on sshmcp-target; never resets either VM.

python3 tests/integration_runner.py -v
The restart and reboot cases intentionally invalidate live target shells.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import lab


class RealRunnerTest(unittest.TestCase):
    def setUp(self):
        lab.select_profile("target")
        self.lock = lab.workspace_lock()
        self.lock.__enter__()
        self.addCleanup(self.lock.__exit__, None, None, None)
        self.state = lab.load_state()
        self.assertIsNotNone(self.state, "Provision target with make target-up first.")
        self.assertEqual(self.state.get("baseline", {}).get("phase"), "ready")
        self.baseline = dict(self.state["baseline"])
        self.state = lab.start_owned(self.state, lab.inventory())
        lab.check_ready(self.state, wait=True)
        lab.verify_baseline(self.state)
        self.owner = dict(issuer="integration://checkpoint-4", subject=uuid.uuid4().hex)
        self.connections = []
        self.marker = f"/home/{lab.USER}/.sshmcp-cp4-{self.owner['subject']}"
        self.control = {
            path: path.read_bytes()
            for name in ("state.json", "id_ed25519", "id_ed25519.pub", "known_hosts", "ssh_config")
            if (path := lab.ROOT / ".lab" / name).exists()
        }
        self.addCleanup(self.cleanup_target)

    def cleanup_target(self):
        errors = []
        for connection in self.connections:
            try:
                self.rpc("disconnect", connection_id=connection)
            except Exception as error:
                errors.append(str(error))
        try:
            lab.strict_ssh(f"rm -f -- {self.marker}", label="Remove this runner test's marker", timeout=20)
            lab.verify_baseline(self.state)
            self.assertEqual(lab.load_state()["baseline"], self.baseline)
        finally:
            lab.down(self.state, lab.inventory())
        for path, data in self.control.items():
            self.assertEqual(path.read_bytes(), data, f"Control VM record changed: {path.name}")
        self.assertEqual(errors, [], f"Target cleanup failed: {errors}")

    def rpc(self, action, expected_error=None, **fields):
        request = dict(action=action, owner=self.owner, **fields)
        result = subprocess.run(
            ["ssh", "-F", str(lab.SSH_CONFIG), lab.VM,
             "XDG_RUNTIME_DIR=/run/user/$(id -u) sshmcp-runner rpc"],
            input=json.dumps(request), text=True, capture_output=True, timeout=40, check=False,
        )
        response = json.loads(result.stdout)
        if expected_error:
            self.assertEqual(response.get("code"), expected_error, response)
        else:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("error", response)
        return response

    def connect(self):
        connection = self.rpc("connect")["connection"]["id"]
        self.connections.append(connection)
        return connection

    def run_job(self, connection, key, command, **fields):
        return self.rpc("run", connection_id=connection, idempotency_key=key, command=command, **fields)["job"]

    def wait_job(self, job):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            result = self.rpc("job", job_id=job["id"])["job"]
            if result["state"] not in ("accepted", "running"):
                return result
            time.sleep(.1)
        self.fail(f"Job never terminated: {job['id']}")

    def stdout(self, job):
        return base64.b64decode(self.rpc("output", job_id=job["id"])["output"]["stdout"])

    def wait_started(self, job):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if b"started" in self.stdout(job):
                return
            time.sleep(.1)
        self.fail("Job never began")

    def test_persistent_state_output_and_sixteen_jobs(self):
        one, two = self.connect(), self.connect()
        job = self.run_job(one, "state", "cd /tmp; retained=live-target; greet() { printf '%s' \"$retained\"; }; printf ready; printf diagnostic >&2")
        self.assertEqual(self.wait_job(job)["state"], "completed")
        output = self.rpc("output", job_id=job["id"], limit=3)["output"]
        self.assertEqual(base64.b64decode(output["stdout"]), b"rea")
        self.assertEqual(base64.b64decode(output["stderr"]), b"dia")
        persisted = self.run_job(one, "persistent", "printf '%s|' \"$PWD\"; greet")
        self.assertEqual(self.wait_job(persisted)["state"], "completed")
        self.assertEqual(self.stdout(persisted), b"/tmp|live-target")
        isolated = self.run_job(two, "isolated", "printf '%s' \"${retained-unset}\"")
        self.assertEqual(self.wait_job(isolated)["state"], "completed")
        self.assertEqual(self.stdout(isolated), b"unset")
        connections = [one, two] + [self.connect() for _ in range(15)]
        def submit(index):
            return self.run_job(connections[index], f"parallel-{index}", "sleep 4; printf parallel")
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(submit, range(16)))
        self.rpc("run", expected_error="capacity", connection_id=connections[16], idempotency_key="overflow", command="true")
        self.rpc("run", expected_error="connection_busy", connection_id=one, idempotency_key="busy", command="true")
        duplicate = self.rpc("run", connection_id=one, idempotency_key="parallel-0", command=jobs[0]["command"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["job"]["id"], jobs[0]["id"])
        self.rpc("run", expected_error="idempotency_conflict", connection_id=one, idempotency_key="parallel-0", command="false")
        for job in jobs:
            self.assertEqual(self.wait_job(job)["state"], "completed")
        cancel = self.run_job(one, "cancel", "trap '' INT; printf started; while :; do sleep 20; done")
        self.wait_started(cancel)
        before = time.monotonic()
        self.rpc("cancel", job_id=cancel["id"])
        self.assertLess(time.monotonic() - before, 3, "Independent cancel RPC waited for SIGINT grace")
        other = self.run_job(two, "independent", "printf independent")
        self.assertEqual(self.wait_job(other)["state"], "completed")
        cancelled = self.wait_job(cancel)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertTrue(cancelled["shell_state_lost"])
        timeout = self.run_job(two, "deadline", "sleep 30", timeout_ms=100)
        self.assertEqual(self.wait_job(timeout)["reason"], "command_timeout")

    def test_ssh_loss_and_runner_crash_do_not_replay(self):
        connection = self.connect()
        survives = self.run_job(connection, "ssh-loss", "sleep 1; printf survived-client-exit")
        # Each RPC's real SSH connection has already exited. Work is supervised
        # independently on the target, so no open SSH channel keeps it alive.
        self.assertEqual(self.wait_job(survives)["state"], "completed")
        self.assertEqual(self.stdout(survives), b"survived-client-exit")
        job = self.run_job(connection, "restart", f"printf started; printf once >> {self.marker}; sleep 40; printf should-not-run")
        self.wait_started(job)
        lab.strict_ssh(
            "export XDG_RUNTIME_DIR=/run/user/$(id -u); export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus; "
            "systemctl --user kill --kill-whom=main --signal=KILL sshmcp-runner.service; "
            "sleep 3; systemctl --user is-active sshmcp-runner.service",
            label="Crash runner daemon and verify automatic restart", timeout=20,
        )
        lost = self.wait_job(job)
        self.assertEqual(lost["state"], "lost")
        self.assertEqual(lost["reason"], "runner_restarted")
        retry = self.rpc("run", connection_id=connection, idempotency_key="restart", command=job["command"])
        self.assertTrue(retry["duplicate"])
        self.assertEqual(retry["job"]["id"], job["id"])
        self.assertEqual(self.stdout(job), b"started")
        self.assertEqual(lab.strict_ssh(f"cat {self.marker}", label="Check command was never replayed", timeout=20), "once")

    def test_target_reboot_invalidates_live_shell(self):
        connection = self.connect()
        job = self.run_job(connection, "reboot", f"printf started; printf once >> {self.marker}; sleep 120; printf should-not-run")
        self.wait_started(job)
        boot = lab.guest("cat", "/proc/sys/kernel/random/boot_id", label="Record target boot identity").strip()
        lab.guest("sudo", "-n", "reboot", label="Reboot only the owned target", timeout=20)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                current = lab.guest("cat", "/proc/sys/kernel/random/boot_id", label="Wait for target reboot", timeout=15).strip()
                if current != boot:
                    lab.verify_identity(self.state)
                    lab.check_ready(self.state, wait=True)
                    break
            except lab.LabError:
                pass
        else:
            self.fail("Target did not finish rebooting")
        self.assertEqual(self.wait_job(job)["state"], "lost")
        self.assertTrue(self.rpc("run", connection_id=connection, idempotency_key="reboot", command=job["command"])["duplicate"])
        self.assertEqual(self.stdout(job), b"started")
        self.assertEqual(lab.strict_ssh(f"cat {self.marker}", label="Check reboot did not replay command", timeout=20), "once")


if __name__ == "__main__":
    unittest.main()
