"""Focused safety checks that never run Multipass or touch local lab state."""

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
                        readiness.assert_called_once_with(state, wait=True)


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
