"""Opt-in real-VM lifecycle checks: python3 tests/integration_lab.py -v.

These start/stop the owned VM, run the public-image smoke fixture, and restore its
recorded clean baseline. They never run during normal unittest discovery.
"""

import json
import os
from pathlib import Path
import signal
import stat
import sys
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import lab


class RealLabLifecycleTest(unittest.TestCase):
    def invoke(self, command):
        with patch.object(sys, "argv", ["tools/lab.py", command]):
            return lab.main()

    def assert_run_cleaned(self, run_id):
        with lab.workspace_lock():
            state = lab.load_state()
            self.assertEqual(lab.instance_info()["state"], "Stopped")
            state = lab.start_owned(state, lab.inventory())
            try:
                lab.check_ready(state, wait=True)
                lab.strict_ssh(
                    f'test ! -e "$HOME/.sshmcp-lab-runs/{run_id}" && '
                    f'test -z "$(sudo -n docker ps --all --quiet '
                    f'--filter label=com.docker.compose.project=sshmcp-smoke-{run_id})" && '
                    f'test -z "$(sudo -n docker network ls --quiet '
                    f'--filter label=com.docker.compose.project=sshmcp-smoke-{run_id})" && '
                    'export XDG_RUNTIME_DIR=/run/user/$(id -u) && '
                    'export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus && '
                    f'test "$(systemctl --user show sshmcp-workload-{run_id}.service '
                    '--property=LoadState --value)" = not-found && '
                    f'test "$(systemctl --user show sshmcp-smoke-{run_id}.service '
                    '--property=LoadState --value)" = not-found',
                    label="Integration assertion: only this run was removed", timeout=30,
                )
                lab.verify_baseline(state)
            finally:
                lab.down(state, lab.inventory())

    def assert_fault_cleanup(self, *, signum=None):
        before = set((lab.LOCAL / "results").glob("*-test-*"))
        workload = lab.run_workload

        def fault_after_workload(run_id, directory):
            workload(run_id, directory)
            # Normal smoke includes explicit shutdown. Restart that same run's
            # real fixture so the injected failure must clean active resources.
            lab.strict_ssh(
                f'sudo -n docker compose --project-name sshmcp-smoke-{run_id} '
                f'--file "$HOME/.sshmcp-lab-runs/{run_id}/compose.yaml" '
                'up --detach --wait --wait-timeout 90',
                label="Integration setup: active fixture before fault", timeout=120,
                log_path=directory / "fault-setup.log",
            )
            if signum is not None:
                # main's real handler is installed; this exercises signal
                # delivery rather than synthesizing a KeyboardInterrupt.
                os.kill(os.getpid(), signum)
            raise lab.LabError("Expected integration harness workload failure.")

        with patch.object(lab, "run_workload", side_effect=fault_after_workload):
            self.assertEqual(self.invoke("test"), 128 + signum if signum is not None else 1)
        created = set((lab.LOCAL / "results").glob("*-test-*")) - before
        self.assertEqual(len(created), 1)
        directory = created.pop()
        result = json.loads((directory / "result.json").read_text())
        self.assertFalse(result["success"])
        expected_error = f"Interrupted by {signal.Signals(signum).name}." if signum is not None else "Expected integration harness workload failure."
        self.assertEqual(result["primary_error"], expected_error)
        self.assertEqual(result["cleanup_errors"], [])
        for path in (directory, *directory.rglob("*")):
            expected_mode = 0o700 if path.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected_mode)
        self.assert_run_cleaned(result["run_id"])

    def test_failure_collects_logs_cleans_and_stops(self):
        self.assert_fault_cleanup()

    def test_sigterm_collects_logs_cleans_and_stops(self):
        self.assert_fault_cleanup(signum=signal.SIGTERM)

    def test_reset_removes_marker_and_repairs_stopped_docker(self):
        marker = ".sshmcp-reset-marker-" + uuid.uuid4().hex
        with lab.workspace_lock():
            state = lab.start_owned(lab.load_state(), lab.inventory())
            try:
                lab.check_ready(state, wait=True)
                lab.verify_baseline(state)
                identity = (state["owner"], state["host_key"], dict(state["baseline"]))
                lab.strict_ssh(
                    f"printf 'reset integration marker\\n' > {marker} && "
                    "sudo -n systemctl stop docker.service docker.socket",
                    label="Integration setup: marker and unhealthy Docker", timeout=45,
                )
            finally:
                lab.down(state, lab.inventory())
        self.assertEqual(self.invoke("reset"), 0)
        with lab.workspace_lock():
            state = lab.load_state()
            self.assertEqual((state["owner"], state["host_key"], state["baseline"]), identity)
            self.assertEqual(lab.instance_info()["state"], "Stopped")
            state = lab.start_owned(state, lab.inventory())
            try:
                lab.check_ready(state, wait=True)
                lab.strict_ssh(f"test ! -e {marker}", label="Integration assertion: reset removed marker", timeout=30)
                lab.verify_baseline(state)
            finally:
                lab.down(state, lab.inventory())


if __name__ == "__main__":
    unittest.main()
