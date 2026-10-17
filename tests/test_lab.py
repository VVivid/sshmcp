"""Focused safety checks that never run Multipass or touch local lab state."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import lab


class UpMountSafetyTest(unittest.TestCase):
    def test_stopped_vm_mount_check_precedes_start(self):
        for name, mount_fields, can_start in (
            ("empty mapping", {"mounts": {}}, True),
            ("missing", {}, False),
            ("null", {"mounts": None}, False),
            ("list", {"mounts": []}, False),
            ("string", {"mounts": ""}, False),
            ("number", {"mounts": 0}, False),
            ("boolean", {"mounts": False}, False),
            ("host mount", {"mounts": {"/mnt/host": {"source_path": "/tmp/host-share"}}}, False),
        ):
            with self.subTest(mounts=name):
                events = []

                def instance_info():
                    events.append("info")
                    return {"state": "Stopped", **mount_fields}

                def multipass(*arguments, **kwargs):
                    events.append(arguments)

                state = {"created_here": True, "host_key": "enrolled"}
                with (
                    patch.object(lab, "instance_info", side_effect=instance_info),
                    patch.object(lab, "multipass", side_effect=multipass) as start,
                    patch.object(lab, "verify_identity") as identity,
                    patch.object(lab, "check_ready") as readiness,
                    patch.object(lab, "ensure_baseline", return_value=False),
                ):
                    if not can_start:
                        with self.assertRaisesRegex(lab.LabError, "host mounts"):
                            lab.up(state, {"state": "Stopped"})
                        self.assertEqual(events, ["info"])
                        start.assert_not_called()
                        identity.assert_not_called()
                        readiness.assert_not_called()
                    else:
                        lab.up(state, {"state": "Stopped"})
                        self.assertEqual(events, ["info", ("start", lab.VM, "--timeout", "180")])
                        start.assert_called_once_with(
                            "start", lab.VM, "--timeout", "180", label="VM verification start", timeout=200
                        )
                        identity.assert_called_once_with(state, enroll=True)
                        readiness.assert_called_once_with(state, wait=True, install=True)


class TargetUpMountSafetyTest(UpMountSafetyTest):
    def setUp(self):
        lab.select_profile("target")
        self.addCleanup(lab.select_profile, "control")


class ProfileIsolationTest(unittest.TestCase):
    def test_target_refuses_control_manifest_even_with_matching_workspace(self):
        lab.select_profile("target")
        self.addCleanup(lab.select_profile, "control")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            manifest = directory / "state.json"
            state = dict(schema=1, workspace=str(lab.ROOT), vm_name=lab.VM,
                         profile="control", owner="1" * 32, created_here=True,
                         phase="stopped", host_key=None)
            manifest.write_text(json.dumps(state))
            manifest.chmod(0o600)
            with patch.object(lab, "LOCAL", directory), patch.object(lab, "MANIFEST", manifest):
                with self.assertRaisesRegex(lab.LabError, "foreign local lab manifest"):
                    lab.load_state()

    def test_profile_keys_pins_manifests_and_resources_are_distinct(self):
        try:
            lab.select_profile("control")
            control = (lab.VM, lab.LOCAL, lab.KEY, lab.KNOWN_HOSTS, lab.MANIFEST)
            lab.select_profile("target")
            target = (lab.VM, lab.LOCAL, lab.KEY, lab.KNOWN_HOSTS, lab.MANIFEST)
            self.assertEqual(lab.VM, "sshmcp-target")
            self.assertEqual((lab.PROFILE.cpus, lab.PROFILE.memory), ("4", "8G"))
            for left, right in zip(control, target):
                self.assertNotEqual(left, right)
            self.assertEqual(lab.LOCAL, lab.ROOT / ".lab" / "target")
        finally:
            lab.select_profile("control")


class RunnerInstallSafetyTest(unittest.TestCase):
    def setUp(self):
        lab.select_profile("target")
        self.addCleanup(lab.select_profile, "control")

    def test_guest_hashes_determine_upgrade_despite_saved_installed_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binary = directory / "sshmcp-runner"
            binary.write_bytes(b"current native build")
            binary.chmod(0o600)
            digest = hashlib.sha256(binary.read_bytes()).hexdigest()
            unit_digest = hashlib.sha256(lab.RUNNER_UNIT.encode()).hexdigest()
            matching = f"{digest}  /usr/local/bin/sshmcp-runner\n{unit_digest}  unit\n"
            for installed, expected_calls in ((matching, 1), ("0" * 64 + "  old-binary\n", 2)):
                with self.subTest(installed=installed == matching):
                    state = {"runner_installed": True, "runner_sha256": digest}
                    with (patch.object(lab, "LOCAL", directory),
                          patch.object(lab, "run"),
                          patch.object(lab, "strict_ssh", side_effect=[installed, ""]) as ssh,
                          patch.object(lab, "save_state")):
                        lab.install_runner(state)
                    self.assertEqual(ssh.call_count, expected_calls)
                    self.assertEqual(state["runner_sha256"], digest)

    def test_symlink_output_is_refused_before_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            destination = directory / "unrelated"
            destination.write_text("preserve")
            (directory / "sshmcp-runner").symlink_to(destination)
            with patch.object(lab, "LOCAL", directory), patch.object(lab, "run") as build:
                with self.assertRaisesRegex(lab.LabError, "symlink"):
                    lab.install_runner({})
                build.assert_not_called()
            self.assertEqual(destination.read_text(), "preserve")


class TargetReadinessSafetyTest(unittest.TestCase):
    def setUp(self):
        lab.select_profile("target")
        self.addCleanup(lab.select_profile, "control")
        self.state = {"phase": "enrolled", "last_ip": "192.168.64.2"}
        for name, value in (
            ("instance_info", {"state": "Running", "mounts": {}}),
            ("verify_identity", None),
            ("configure_ssh", None),
            ("guest", '{"status":"done"}'),
            ("run", ""),
            ("multipass", ""),
            ("strict_ssh", "{}"),
            ("install_runner", None),
            ("save_state", None),
        ):
            operation = patch.object(lab, name, return_value=value)
            setattr(self, name, operation.start())
            self.addCleanup(operation.stop)

    def test_status_never_installs_when_runner_record_is_missing_or_false(self):
        for runner_record in ({}, {"runner_installed": False}):
            with self.subTest(runner_record=runner_record):
                state = dict(self.state, **runner_record)
                lab.status(state, {"state": "Running"})
                self.assertEqual(state["phase"], "ready")
        self.install_runner.assert_not_called()
        self.multipass.assert_not_called()
        self.assertEqual([call.args for call in self.guest.call_args_list], [
            ("cloud-init", "status", "--format", "json"), ("sh", "-s"),
            ("cloud-init", "status", "--format", "json"), ("sh", "-s"),
        ])

    def test_active_service_without_rpc_is_not_ready_and_status_does_not_retry(self):
        self.strict_ssh.side_effect = lab.LabError("RPC socket is unavailable.")
        with self.assertRaisesRegex(lab.LabError, "RPC is not ready.*user journal"):
            lab.status(self.state, {"state": "Running"})
        self.assertEqual(self.state["phase"], "enrolled")
        self.strict_ssh.assert_called_once()
        self.install_runner.assert_not_called()
        self.save_state.assert_not_called()

    def test_lifecycle_install_waits_for_rpc_and_readiness_wait_is_bounded(self):
        self.strict_ssh.side_effect = [lab.LabError("Still recovering."), "{}"]
        with patch.object(lab.time, "sleep") as sleep:
            lab.check_ready(self.state, wait=True, install=True)
        self.install_runner.assert_called_once_with(self.state)
        self.assertEqual(self.strict_ssh.call_count, 2)
        sleep.assert_called_once()
        self.strict_ssh.reset_mock(side_effect=True)
        self.strict_ssh.side_effect = lab.LabError("Still recovering.")
        with (patch.object(lab.time, "monotonic", side_effect=[0, 0, 45]),
              patch.object(lab.time, "sleep") as sleep):
            with self.assertRaisesRegex(lab.LabError, "RPC is not ready"):
                lab.check_runner_ready(wait=True)
        self.strict_ssh.assert_called_once()
        self.assertLessEqual(self.strict_ssh.call_args.kwargs["timeout"], 10)
        sleep.assert_not_called()


class CleanupSafetyTest(unittest.TestCase):
    def test_unavailable_vm_records_incomplete_cleanup_without_guest_operations(self):
        for current in (None, {"state": "Stopped"}):
            for workload_attempted in (False, True):
                with self.subTest(current=current, workload_attempted=workload_attempted):
                    errors = []
                    with (
                        patch.object(lab, "inventory", return_value=current),
                        patch.object(lab, "verify_identity") as identity,
                        patch.object(lab, "collect_diagnostics") as diagnostics,
                        patch.object(lab, "cleanup_workload") as fixture,
                        patch.object(lab, "guest") as guest,
                        patch.object(lab, "strict_ssh") as ssh,
                        patch.object(lab, "multipass") as multipass,
                        patch.object(lab, "down") as stop,
                    ):
                        lab.cleanup_verified_vm(
                            {}, "unused", None, workload_attempted=workload_attempted, errors=errors
                        )
                    self.assertTrue(errors)
                    self.assertIn("unable to complete diagnostics", errors[0])
                    self.assertEqual("fixture cleanup" in errors[0], workload_attempted)
                    for operation in (identity, diagnostics, fixture, guest, ssh, multipass, stop):
                        operation.assert_not_called()

    def test_current_identity_failure_prevents_guest_commands_and_stop(self):
        errors = []
        with (
            patch.object(lab, "inventory", return_value={"state": "Running"}),
            patch.object(lab, "verify_identity", side_effect=lab.LabError("Owner mismatch.")),
            patch.object(lab, "collect_diagnostics") as diagnostics,
            patch.object(lab, "cleanup_workload") as fixture,
            patch.object(lab, "down") as stop,
        ):
            lab.cleanup_verified_vm({}, "unused", None, workload_attempted=True, errors=errors)
        self.assertEqual(errors, ["Owner mismatch."])
        diagnostics.assert_not_called()
        fixture.assert_not_called()
        stop.assert_not_called()

    def test_diagnostic_and_fixture_errors_still_reach_owned_stop(self):
        events = []
        errors = []

        def diagnostics(*args):
            events.append("diagnostics")
            raise lab.LabError("Diagnostic failure.")

        def fixture(*args):
            events.append("fixture cleanup")
            raise lab.LabError("Fixture cleanup failure.")

        with (
            patch.object(lab, "inventory", return_value={"state": "Running"}),
            patch.object(lab, "verify_identity"),
            patch.object(lab, "collect_diagnostics", side_effect=diagnostics),
            patch.object(lab, "cleanup_workload", side_effect=fixture),
            patch.object(lab, "down", side_effect=lambda *args: events.append("owned stop")),
        ):
            lab.cleanup_verified_vm({}, "unused", None, workload_attempted=True, errors=errors)
        self.assertEqual(events, ["diagnostics", "fixture cleanup", "owned stop"])
        self.assertEqual(errors, ["Diagnostic failure.", "Fixture cleanup failure."])


if __name__ == "__main__":
    unittest.main()
