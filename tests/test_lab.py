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


if __name__ == "__main__":
    unittest.main()
