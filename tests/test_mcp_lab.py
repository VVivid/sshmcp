"""Host-only MCP diagnostic/cleanup safety checks; never operate either VM."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import integration_mcp
import mcp_lab

lab = mcp_lab.lab


class MCPDiagnosticSafetyTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lab.select_profile, "control")
        self.directory = Path("/unused-private-results")

    def running_profile(self, name):
        lab.select_profile(name)
        return {"name": name}, {"state": "Running"}

    def test_both_profiles_are_verified_before_bounded_private_inspection(self):
        events = []
        commands = {}

        def inspect(command, **options):
            name = lab.PROFILE.name
            events.append(("inspect", name))
            commands[name] = command
            self.assertLessEqual(options["timeout"], 30)
            self.assertEqual(options["log_path"].parent, self.directory)

        with (patch.object(mcp_lab, "profile", side_effect=self.running_profile),
              patch.object(lab, "verify_identity", side_effect=lambda state: events.append(("verify", state["name"]))),
              patch.object(lab, "strict_ssh", side_effect=inspect)):
            mcp_lab.diagnostic(self.directory)
        self.assertEqual(events, [("verify", "control"), ("inspect", "control"), ("verify", "target"), ("inspect", "target")])
        self.assertIn("journalctl --user -u sshmcp-runner.service", commands["target"])
        self.assertIn("systemctl --user cat sshmcp-runner.service", commands["target"])
        self.assertEqual(lab.PROFILE.name, "control")

    def test_unowned_profile_is_not_inspected_and_does_not_skip_the_other(self):
        def verify(state):
            if state["name"] == "control":
                raise lab.LabError("Owner mismatch.")

        inspected = []
        with (patch.object(mcp_lab, "profile", side_effect=self.running_profile),
              patch.object(lab, "verify_identity", side_effect=verify),
              patch.object(lab, "strict_ssh", side_effect=lambda *args, **kwargs: inspected.append(lab.PROFILE.name))):
            with self.assertRaisesRegex(lab.LabError, "control: Owner mismatch"):
                mcp_lab.diagnostic(self.directory)
        self.assertEqual(inspected, ["target"])
        self.assertEqual(lab.PROFILE.name, "control")

    def test_both_diagnostic_failures_are_retained(self):
        with (patch.object(mcp_lab, "profile", side_effect=self.running_profile),
              patch.object(lab, "verify_identity"),
              patch.object(lab, "strict_ssh", side_effect=[lab.LabError("Gateway inspection failed."), lab.LabError("Runner inspection failed.")]) as ssh):
            with self.assertRaises(lab.LabError) as caught:
                mcp_lab.diagnostic(self.directory)
        self.assertEqual(ssh.call_count, 2)
        self.assertIn("control: Gateway inspection failed.", str(caught.exception))
        self.assertIn("target: Runner inspection failed.", str(caught.exception))

    def test_stopped_or_absent_profiles_have_no_guest_operations(self):
        for existing in (None, {"state": "Stopped"}):
            with (
                self.subTest(existing=existing),
                patch.object(mcp_lab, "profile", return_value=({"enrolled": True}, existing)),
                patch.object(lab, "verify_identity") as identity,
                patch.object(lab, "strict_ssh") as ssh,
            ):
                with self.assertRaisesRegex(lab.LabError, "diagnostics not collected"):
                    mcp_lab.diagnostic(self.directory)
                identity.assert_not_called()
                ssh.assert_not_called()


class MCPIntegrationCleanupSafetyTest(unittest.TestCase):
    def test_setup_retains_primary_and_all_cleanup_errors(self):
        class FailedSetup(integration_mcp.PrivateMCPTest):
            pass

        primary = lab.LabError("Initial readiness failed.")
        lock = MagicMock()
        with (patch.object(lab, "workspace_lock", return_value=lock),
              patch.object(lab, "new_results", return_value=("unused", Path("/unused-private-results"))),
              patch.object(mcp_lab, "profile", side_effect=primary),
              patch.object(mcp_lab, "diagnostic", side_effect=lab.LabError("Diagnostics failed.")),
              patch.object(mcp_lab, "down", side_effect=lab.LabError("Stop failed.")),
              patch.object(lab, "write_private") as write):
            with self.assertRaises(AssertionError) as caught:
                FailedSetup.setUpClass()
        self.assertIs(caught.exception.__cause__, primary)
        self.assertIs(lock.__exit__.call_args.args[1], primary)
        evidence = json.loads(write.call_args.args[1])
        self.assertEqual(evidence["primary_error"], "Initial readiness failed.")
        self.assertEqual(evidence["cleanup_errors"], ["Diagnostics failed.", "Stop failed."])
        self.assertIn("Initial readiness failed.", str(caught.exception))
        self.assertIn("Diagnostics failed.; Stop failed.", str(caught.exception))

    def test_teardown_collects_before_shutdown_and_persists_all_cleanup_errors(self):
        class FailedCleanup(integration_mcp.PrivateMCPTest):
            directory = Path("/unused-private-results")
            marker = "/home/mcp-test/.sshmcp-cp5-unused"
            connections = []
            evidence = ["workload checked"]
            cleanup_errors = ["Short-timer restoration failed."]
            lock = MagicMock()

        events = []

        def fail(step):
            events.append(step)
            raise lab.LabError(step + " failed.")

        def save(path, content):
            events.append("evidence")
            self.assertEqual(json.loads(content)["cleanup_errors"], [
                "Short-timer restoration failed.", "Restore failed.", "Diagnostics failed.", "Stop failed.",
            ])

        with (patch.object(mcp_lab, "profile", return_value=({"enrolled": True}, {"state": "Running"})),
              patch.object(lab, "verify_identity"),
              patch.object(lab, "install_runner", side_effect=lambda state: fail("Restore")),
              patch.object(lab, "check_ready"),
              patch.object(lab, "strict_ssh") as marker_cleanup,
              patch.object(mcp_lab, "diagnostic", side_effect=lambda directory: fail("Diagnostics")),
              patch.object(mcp_lab, "down", side_effect=lambda: fail("Stop")),
              patch.object(lab, "write_private", side_effect=save),
              patch("builtins.print")):
            with self.assertRaisesRegex(AssertionError, "MCP cleanup failed"):
                FailedCleanup.tearDownClass()
        self.assertEqual(events, ["Restore", "Diagnostics", "Stop", "evidence"])
        marker_cleanup.assert_called_once()
        FailedCleanup.lock.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
