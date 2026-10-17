"""Opt-in real-VM lifecycle/security checks: python3 tests/integration_lab.py -v.

All cases start/stop the owned VM. The lifecycle cases also run the public-image
smoke fixture or restore its recorded clean baseline; the security cases do
neither. They never run during normal unittest discovery.
"""

import copy
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
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

    def test_unapproved_ssh_key_cannot_execute(self):
        marker = ".sshmcp-unapproved-key-" + uuid.uuid4().hex
        with lab.workspace_lock():
            state = lab.load_state()
            existing = lab.inventory()
            self.assertTrue(
                state is not None and existing is not None
                and state["created_here"] and state["host_key"],
                "Security checks require the existing enrolled VM; run lab-up first.",
            )
            self.assertEqual(state.get("baseline", {}).get("phase"), "ready")
            state = lab.start_owned(state, existing)
            try:
                lab.check_ready(state, wait=True)
                lab.verify_baseline(state)
                lab.strict_ssh(
                    f'test ! -e "$HOME/{marker}"',
                    label="Integration setup: unique unapproved-key marker is absent", timeout=30,
                )
                with tempfile.TemporaryDirectory(prefix="unapproved-key-", dir=lab.LOCAL) as temporary:
                    unapproved_key = Path(temporary) / "id_ed25519"
                    lab.run(
                        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", unapproved_key],
                        label="Integration setup: disposable unapproved key", timeout=15,
                    )
                    unapproved_key.chmod(0o600)
                    unapproved_key.with_suffix(".pub").chmod(0o600)
                    try:
                        # IdentityFile accumulates in SSH config. Bypass all
                        # config and the agent so only this unapproved key is tried.
                        result = subprocess.run(
                            [
                                "ssh", "-F", "/dev/null", "-T", "-i", str(unapproved_key),
                                "-o", "IdentityAgent=none",
                                "-o", "IdentitiesOnly=yes",
                                "-o", "BatchMode=yes",
                                "-o", "PreferredAuthentications=publickey",
                                "-o", "PasswordAuthentication=no",
                                "-o", "KbdInteractiveAuthentication=no",
                                "-o", "StrictHostKeyChecking=yes",
                                "-o", f"HostKeyAlias={lab.VM}",
                                "-o", "HostKeyAlgorithms=ssh-ed25519",
                                "-o", f'UserKnownHostsFile="{lab.KNOWN_HOSTS}"',
                                "-o", "GlobalKnownHostsFile=/dev/null",
                                "-o", "UpdateHostKeys=no",
                                "-o", "ConnectTimeout=8",
                                "-o", "ConnectionAttempts=1",
                                "-o", "ForwardAgent=no",
                                "-o", "ForwardX11=no",
                                "-o", "ClearAllForwardings=yes",
                                "-o", "PermitLocalCommand=no",
                                "-l", lab.USER, "-p", "22", state["last_ip"],
                                f': > "$HOME/{marker}"',
                            ],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=20, check=False, cwd=lab.ROOT,
                            env={**os.environ, "LC_ALL": "C"},
                        )
                    finally:
                        # Check even after a timeout. An unexpected execution
                        # removes only this case's marker, then fails the test.
                        lab.strict_ssh(
                            f'if test -e "$HOME/{marker}"; then '
                            f'rm -- "$HOME/{marker}"; exit 1; fi',
                            label="Integration assertion: unapproved command did not execute", timeout=30,
                        )
                    self.assertEqual(result.returncode, 255, "Expected SSH authentication refusal.")
                    self.assertIn(
                        "Permission denied (publickey).", result.stderr,
                        "SSH failed without a specific public-key authentication denial.",
                    )
            finally:
                lab.down(state, lab.inventory())

    def test_down_refuses_owner_and_host_pin_mismatches(self):
        with lab.workspace_lock():
            state = lab.load_state()
            existing = lab.inventory()
            self.assertTrue(
                state is not None and existing is not None
                and state["created_here"] and state["host_key"],
                "Security checks require the existing enrolled VM; run lab-up first.",
            )
            self.assertEqual(state.get("baseline", {}).get("phase"), "ready")
            state = lab.start_owned(state, existing)
            try:
                lab.check_ready(state, wait=True)
                lab.verify_baseline(state)
                with tempfile.TemporaryDirectory(prefix="wrong-pin-", dir=lab.LOCAL) as temporary:
                    alternative_host_key = Path(temporary) / "id_ed25519"
                    lab.run(
                        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", alternative_host_key],
                        label="Integration setup: disposable alternative host pin", timeout=15,
                    )
                    alternative_host_key.chmod(0o600)
                    alternative_host_key.with_suffix(".pub").chmod(0o600)
                    wrong_pin = lab.public_key(alternative_host_key.with_suffix(".pub").read_text())
                    self.assertTrue(wrong_pin != state["host_key"], "Disposable pin must differ from the enrolled pin.")
                    wrong_owner = "0" * 32 if state["owner"] != "0" * 32 else "1" * 32
                    protected_file_contents = {
                        path: path.read_bytes()
                        for path in (lab.MANIFEST, lab.KEY, lab.KEY.with_suffix(".pub"), lab.KNOWN_HOSTS, lab.SSH_CONFIG)
                    }
                    for field, value, error in (
                        ("owner", wrong_owner, "owner marker does not match"),
                        ("host_key", wrong_pin, "SSH host key differs from its saved pin"),
                    ):
                        with self.subTest(guard=field):
                            mismatched_state = copy.deepcopy(state)
                            mismatched_state[field] = value
                            with self.assertRaisesRegex(lab.LabError, error):
                                lab.down(mismatched_state, lab.inventory())
                            self.assertEqual(lab.instance_info()["state"], "Running")
                            for path, expected_contents in protected_file_contents.items():
                                self.assertTrue(
                                    path.read_bytes() == expected_contents,
                                    f"A refused stop changed {path.name}.",
                                )
            finally:
                # The altered copies never become trusted state for shutdown.
                lab.down(state, lab.inventory())


if __name__ == "__main__":
    unittest.main()
