"""Host-only operations acceptance boundaries; no real VM, network, or actor."""

import base64
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
from io import StringIO
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import demo_operations as operations
import demo_evaluator
import mcp_client

lab, demo = operations.lab, operations.demo


def containers():
    result = {}
    for name in ("app", "traefik", "lgtm"):
        port = {"app": {}, "traefik": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]},
                "lgtm": {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3000"}]}}[name]
        result[name] = {"Id": name + "-container", "Image": "sha256:original", "State": {"Running": True},
                        "Config": {"Image": demo_evaluator.LGTM_IMAGE if name == "lgtm" else name,
                                   "Env": ["OTEL_EXPORTER_OTLP_ENDPOINT=http://lgtm:4318"],
                                   "Labels": {"com.docker.compose.project.working_dir": "/home/mcp-test/chosen",
                                              "com.docker.compose.project.config_files": "/home/mcp-test/chosen/compose.yaml"}},
                        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "PortBindings": port},
                        "Mounts": []}
    result["lgtm"]["Mounts"] = [{"Type": "volume", "Name": "actual-history", "Destination": "/data", "RW": True}]
    return result


class OperationsAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.addCleanup(lab.select_profile, "control")

    def test_changed_image_or_private_source_cannot_pass_rollback(self):
        original = {"image_id": "sha256:original", "files": [{"path": "main.go", "sha256": "original-source"},
                                                               {"path": ".env", "sha256": "private-config-hash"}]}
        baseline = containers()
        restored = deepcopy(original)
        operations.require_rollback(original, baseline, restored)
        for change in ("image", "source", "private-env", "extra-source", "missing-source"):
            with self.subTest(change=change):
                current, source = deepcopy(baseline), deepcopy(restored)
                if change == "image":
                    current["app"]["Image"] = "sha256:rebuilt-identical-source"
                elif change == "source":
                    source["files"][0]["sha256"] = "modified-source"
                elif change == "private-env":
                    source["files"][1]["sha256"] = "modified-private-config"
                elif change == "extra-source":
                    source["files"].append({"path": "version.go", "sha256": "new"})
                else:
                    source["files"].pop()
                with self.assertRaisesRegex(lab.LabError, "Rollback"):
                    operations.require_rollback(original, current, source)

    def test_version_status_and_raw_body_are_both_required(self):
        baseline = {"status": 404, "body_base64": base64.b64encode(b"old\n").decode()}
        operations.require_version(dict(baseline, observed_ns=10), baseline)
        for current in (dict(baseline, status=200), dict(baseline, body_base64=base64.b64encode(b"old").decode())):
            with self.assertRaisesRegex(lab.LabError, "status/body"):
                operations.require_version(current, baseline)

    def test_failed_run_can_repeat_only_after_independently_matching_its_original_baseline(self):
        source = {"files": [{"path": ".env", "sha256": "private-config-hash"}]}
        version = operations.expected_version("prior")
        record = {"run_id": "failed-run", "accepted_run_id": "accepted", "image_id": "sha256:original",
                  "source_hashes": {".env": "private-config-hash"}, "version": version}
        lab.write_private(self.directory / "operations-failed-original.json", json.dumps(record))
        with patch.object(demo, "PRIVATE", self.directory):
            self.assertEqual(operations.require_recovered_baseline("accepted", containers(), source, version), ["failed-run"])
            with self.assertRaisesRegex(lab.LabError, "Recover its recorded"):
                operations.require_recovered_baseline("accepted", containers(), {"files": [{"path": ".env", "sha256": "partial-change"}]}, version)
            self.assertEqual(operations.require_recovered_baseline("new-successfully-accepted-run", containers(), source, version), [])
        self.assertEqual(json.loads((self.directory / "operations-failed-original.json").read_text()), record)

    def test_application_override_is_allowed_while_proxy_ids_and_history_volumes_must_stay(self):
        baseline = containers()
        current = deepcopy(baseline)
        current["app"]["Config"]["Labels"]["com.docker.compose.project.config_files"] += ",/home/mcp-test/chosen/upgrade.yaml"
        operations.require_preserved_infrastructure(baseline, current)
        self.assertEqual(len(operations.deployment_context(current)["config_files"]), 2)
        for change in ("proxy", "volume", "new-service"):
            altered = deepcopy(current)
            if change == "proxy":
                altered["traefik"]["Id"] = "replacement"
            elif change == "volume":
                altered["lgtm"]["Mounts"][0]["Name"] = "fresh-history"
            else:
                altered["extra"] = deepcopy(altered["app"])
            with self.subTest(change=change), self.assertRaises(lab.LabError):
                operations.require_preserved_infrastructure(baseline, altered)

    def test_phase_requires_full_fresh_evaluator_and_historical_proof(self):
        baseline = containers()
        current = deepcopy(baseline)
        current["app"]["Image"] = "sha256:upgraded"
        version = operations.expected_version("unique-version")
        with patch.object(demo, "inspect_deployment", return_value=current), \
             patch.object(operations, "http_observations", return_value={"/version": version}), \
             patch.object(demo, "evaluator") as factory, patch.object(operations, "prove_history", return_value={"retained": True}) as history:
            factory.return_value.evaluate.return_value = {"probe": {"trace_id": "fresh"}}
            _, proof = operations.evaluate_phase(self.directory, baseline, version, {"marker": "old"})
        factory.return_value.evaluate.assert_called_once_with()
        history.assert_called_once_with(factory.return_value, {"marker": "old"})
        self.assertEqual(proof["fresh"]["probe"]["trace_id"], "fresh")

    def release_fixture(self):
        release = self.directory.resolve() / "actor-preserved"
        release.mkdir(mode=0o700)
        source = release / "old.go"
        source.write_text("original app")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest = {"image_id": "sha256:old", "files": [{"path": "main.go", "saved_path": "old.go", "sha256": digest}]}
        record = release / "release.json"
        record.write_text(json.dumps(manifest))
        record.chmod(0o600)
        source_root = self.directory.resolve() / "active-source"
        source_root.mkdir()
        original = {"root": str(source_root), "image_id": "sha256:old", "files": [{"path": "main.go", "sha256": digest}]}
        return release, manifest, original

    def verify_fixture_release(self, release, original):
        def operator(directory, name, script):
            output = StringIO()
            with patch("subprocess.check_output", return_value='[{"Id":"sha256:old"}]'), redirect_stdout(output):
                exec(compile(script, "recorded-private-operator", "exec"), {})
            return json.loads(output.getvalue())

        with patch.object(demo, "operator", side_effect=operator):
            return operations.verify_release(self.directory, str(release), original)

    def test_release_manifest_claim_cannot_replace_actual_preserved_bytes(self):
        release, manifest, original = self.release_fixture()
        self.assertTrue(self.verify_fixture_release(release, original)["verified"])
        (release / "old.go").write_text("modified bytes, unchanged index")
        with self.assertRaisesRegex(RuntimeError, "differs from pre-upgrade"):
            self.verify_fixture_release(release, original)
        manifest["files"] = []
        (release / "release.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, "omit pre-upgrade"):
            self.verify_fixture_release(release, original)

    def test_safe_extra_release_text_has_separate_proof_counts(self):
        release, manifest, original = self.release_fixture()
        extra = ".sshmcp-fault-record.json.disabled"
        body = b'{"services":{}}\n'
        (release / "disabled-config").write_bytes(body)
        current = Path(original["root"]) / extra
        current.write_bytes(body)
        manifest["files"].append({"path": extra, "saved_path": "disabled-config", "sha256": hashlib.sha256(body).hexdigest()})
        (release / "release.json").write_text(json.dumps(manifest))
        result = self.verify_fixture_release(release, original)
        self.assertEqual((result["required_files"], result["extra_files"], result["preserved_files"],
                          result["extra_current_originals_matched"]), (1, 1, 2, 1))
        current.unlink()
        self.assertEqual(self.verify_fixture_release(release, original)["extra_current_originals_matched"], 0)
        current.write_text("changed after snapshot")
        with self.assertRaisesRegex(RuntimeError, "differs from its current original"):
            self.verify_fixture_release(release, original)

    def test_extra_release_paths_hashes_types_and_bounds_cannot_weaken_required_proof(self):
        release, manifest, original = self.release_fixture()
        (release / "extra").write_text("extra text")
        extra = {"path": "extra.disabled", "saved_path": "extra", "sha256": hashlib.sha256(b"extra text").hexdigest()}
        for field in ("path", "saved_path"):
            for value in ("/absolute", "../escape", "nested/../../escape", "", ".", "./extra", "a//b", "a\x00b"):
                with self.subTest(field=field, value=value):
                    entry = dict(extra, **{field: value})
                    (release / "release.json").write_text(json.dumps(dict(manifest, files=[*manifest["files"], entry])))
                    with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                        self.verify_fixture_release(release, original)
        for change in ("original-duplicate", "saved-duplicate", "hash", "saved-symlink", "original-symlink",
                       "saved-parent-symlink", "original-parent-symlink", "directory", "utf8", "binary", "size", "count", "total"):
            with self.subTest(change=change):
                entry = dict(extra)
                entries = [*manifest["files"], entry]
                (release / "extra").unlink()
                (release / "extra").write_text("extra text")
                if change == "original-duplicate":
                    entry["path"] = "main.go"
                elif change == "saved-duplicate":
                    entry["saved_path"] = "old.go"
                elif change == "hash":
                    entry["sha256"] = "invented"
                elif change == "saved-symlink":
                    (release / "extra").unlink()
                    (release / "extra").symlink_to(release / "old.go")
                elif change == "original-symlink":
                    (Path(original["root"]) / "extra.disabled").symlink_to(release / "extra")
                elif change.endswith("parent-symlink"):
                    parent = release if change.startswith("saved") else Path(original["root"])
                    (parent / "link").symlink_to(release, target_is_directory=True)
                    entry["saved_path" if change.startswith("saved") else "path"] = "link/extra"
                elif change == "directory":
                    entry["saved_path"] = "folder"
                    (release / "folder").mkdir()
                elif change in {"utf8", "binary", "size"}:
                    (release / "extra").write_bytes({"utf8": b"\xff", "binary": b"a\x00b", "size": b"x" * 262145}[change])
                elif change == "count":
                    entries = [extra] * 129
                elif change == "total":
                    entries = list(manifest["files"])
                    for index in range(16):
                        name = "large-" + str(index)
                        (release / name).write_bytes(b"x" * 262144)
                        entries.append({"path": name, "saved_path": name, "sha256": hashlib.sha256(b"x" * 262144).hexdigest()})
                (release / "release.json").write_text(json.dumps(dict(manifest, files=entries)))
                with self.assertRaises((RuntimeError, UnicodeError)):
                    self.verify_fixture_release(release, original)
                for path in (Path(original["root"]) / "extra.disabled", release / "link", Path(original["root"]) / "link"):
                    if path.is_symlink():
                        path.unlink()

    def test_new_python_capture_is_bounded_and_old_export_contract_is_unchanged(self):
        root = self.directory / "source"
        root.mkdir()
        (root / "main.go").write_text("package main\n")
        (root / "verify.py").write_text("print('actual actor-created script')\n")
        for script, python_expected in ((demo.EXPORT_SCRIPT, False), (operations.SOURCE_SCRIPT, True)):
            output = StringIO()
            with patch.object(Path, "home", return_value=self.directory.resolve()), redirect_stdout(output):
                exec(compile(script % str(root), "bounded-source-capture", "exec"), {})
            captured = json.loads(output.getvalue())
            self.assertEqual("verify.py" in {entry["path"] for entry in captured["files"]}, python_expected)
        (root / "verify.py").write_bytes(b"x" * 262145)
        with patch.object(Path, "home", return_value=self.directory.resolve()), self.assertRaisesRegex(RuntimeError, "256KiB"):
            exec(compile(operations.SOURCE_SCRIPT % str(root), "bounded-source-capture", "exec"), {})

    def test_failure_saves_before_diagnostics_and_shutdown_without_changing_ready(self):
        ready = self.directory / "existing-ready.json"
        ready.write_text("original accepted pointer")
        events = []
        real_save = operations.save_json

        def save(path, value):
            events.append("save")
            real_save(path, value)

        def diagnostics(directory):
            self.assertIn("save", events)
            events.append("diagnostics")
            raise lab.LabError("diagnostics failed")

        def stop():
            self.assertIn("diagnostics", events)
            events.append("stop")
            raise lab.LabError("shutdown failed")

        with patch.object(lab, "new_results", return_value=("run", self.directory)), \
             patch.object(operations, "exercise", side_effect=lab.LabInterrupted(signal.SIGTERM)), \
             patch.object(operations, "save_json", side_effect=save), patch.object(demo, "READY", ready), \
             patch.object(demo, "diagnostics", side_effect=diagnostics), patch.object(demo, "down", side_effect=stop), patch("builtins.print"):
            with self.assertRaises(lab.LabError) as caught:
                operations.run_operation()
        self.assertEqual(caught.exception.exit_code, 143)
        self.assertEqual(ready.read_text(), "original accepted pointer")
        result = json.loads((self.directory / "result.json").read_text())
        self.assertIn("SIGTERM", result["primary_error"])
        self.assertEqual(result["cleanup_errors"], ["diagnostics failed", "shutdown failed"])
        self.assertFalse(result["success"])


def tool_event(identity, arguments, body=None, *, failed=False, started=False):
    item = {"id": identity, "type": "mcp_tool_call", "server": operations.mcp_lab.SERVER_NAME, "tool": "run_command", "arguments": arguments,
            "status": "failed" if failed else "completed", "result": {"structured_content": body or {}}}
    if failed:
        item["error"] = {"message": "real gateway connection closed"}
    return {"type": "item.started" if started else "item.completed", "item": item}


def output_stream(data=b"", *, cursor=0, eof=True, final=True, binary=False):
    value = {"next_cursor": cursor + len(data), "eof": eof, "final": final}
    if binary:
        value.update(encoding="base64", base64=base64.b64encode(data).decode())
    else:
        value.update(encoding="utf-8", text=data.decode())
    return value


class InterruptedActivationTest(unittest.TestCase):
    def setUp(self):
        self.arguments = {"action": "execute", "connection_id": "original-shell", "idempotency_key": "original-key",
                          "command": "actor-chosen activation source", "timeout_ms": 120000, "wait_ms": 10000}
        self.final_job = {"job_id": "original-job", "state": "completed", "exit_code": 0}
        self.events = [tool_event("activation", self.arguments, started=True), tool_event("activation", self.arguments, failed=True),
                       tool_event("lookup", {"action": "lookup", "idempotency_key": "original-key"}, {"job": self.final_job}),
                       tool_event("status", {"action": "status", "job_id": "original-job"}, {
                           "job": self.final_job, "output": {name: output_stream() for name in ("stdout", "stderr")}})]
        self.fault = {"success": True, "specification": {"key": "original-key"}, "marker_observed": True, "execute_in_flight": True,
                      "barrier_state": "running", "recovered_state": "running", "outage_transport_error": {"type": "ConnectionError"},
                      "original_item_id": "activation", "original_job_id": "original-job", "recovered_job_id": "original-job",
                      "stopped_ns": 1, "restored_ns": 5_000_000_001, "read_only_retries": 1}
        self.receipt = {"exists": True, "lines": 1, "matching_lines": 1}

    def validate(self):
        return operations.validate_interruption(self.events, self.fault, self.final_job, self.receipt)

    def test_original_failure_lookup_status_and_single_receipt_pass(self):
        proof = self.validate()
        self.assertEqual(proof["original_job_id"], "original-job")
        self.assertEqual(proof["same_key_visible_retries"], 0)
        self.assertEqual(proof["cli_internal_transport_retries"], "not exposed")

    def test_exact_same_key_deduplication_is_recorded_as_a_retry(self):
        self.events.insert(3, tool_event("retry", deepcopy(self.arguments), {"job": {"job_id": "original-job", "state": "running"}, "duplicate": True}))
        proof = self.validate()
        self.assertEqual(proof["same_key_visible_retries"], 1)
        self.assertEqual(proof["duplicate_results"], 1)

    def test_final_empty_reads_cannot_skip_unobserved_prefixes(self):
        for stdout_cursor, stderr_cursor in ((4096, 2048), (4096, 0), (0, 2048)):
            self.events[3] = tool_event("status", {"action": "status", "job_id": "original-job",
                                                    "stdout_cursor": stdout_cursor, "stderr_cursor": stderr_cursor}, {
                "job": self.final_job, "output": {"stdout": output_stream(cursor=stdout_cursor),
                                                   "stderr": output_stream(cursor=stderr_cursor)}})
            with self.subTest(stdout=stdout_cursor, stderr=stderr_cursor), self.assertRaisesRegex(lab.LabError, "original activation exactly once"):
                self.validate()

    def test_returned_cursor_must_match_actual_decoded_bytes(self):
        baseline = deepcopy(self.events)
        for start, end in ((0, 3), (0, -1), (2, 1), (-1, 1), (False, 2), (0, True), (operations.demo_actor.STREAM_LIMIT + 1,) * 2):
            self.events = deepcopy(baseline)
            self.events[3]["item"]["arguments"]["stdout_cursor"] = start
            stream = output_stream(b"\xc3\xa9", binary=True)
            stream["next_cursor"] = end
            self.events[3]["item"]["result"]["structured_content"]["output"]["stdout"] = stream
            with self.subTest(start=start, end=end), self.assertRaisesRegex(lab.LabError, "output cursor"):
                self.validate()

    def test_execute_prefix_incremental_binary_and_utf8_output_all_count(self):
        self.events[1] = tool_event("activation", self.arguments, {
            "job": {"job_id": "original-job", "state": "running"}, "wait_error": {"code": "status_unavailable"},
            "output": {"stdout": output_stream(b"\xc3", eof=False, final=False, binary=True),
                       "stderr": output_stream(b"\x00", eof=False, final=False, binary=True)}})
        self.events.insert(3, tool_event("partial", {"action": "status", "job_id": "original-job", "stdout_cursor": 1, "stderr_cursor": 1}, {
            "job": {"job_id": "original-job", "state": "running"},
            "output": {"stdout": output_stream(b"\xa9\n", cursor=1, eof=False, final=False, binary=True),
                       "stderr": output_stream(b"\xff", cursor=1, eof=False, final=False, binary=True)}}))
        self.events[4] = tool_event("status", {"action": "status", "job_id": "original-job", "stdout_cursor": 3, "stderr_cursor": 2}, {
            "job": self.final_job,
            "output": {"stdout": output_stream("ready: 日本語\n".encode(), cursor=3), "stderr": output_stream(cursor=2)}})
        self.assertTrue(self.validate()["actor_lookup_reconciled"])

    def test_out_of_order_chunks_and_consistent_overlapping_reads_are_complete_proof(self):
        self.events[3:] = [
            tool_event("tail", {"action": "status", "job_id": "original-job", "stdout_cursor": 3, "stderr_cursor": 1}, {
                "job": self.final_job, "output": {"stdout": output_stream(b"def", cursor=3),
                                                   "stderr": output_stream(b"\xff", cursor=1, binary=True)}}),
            tool_event("prefix", {"action": "status", "job_id": "original-job"}, {
                "job": self.final_job, "output": {"stdout": output_stream(b"abc", eof=False),
                                                   "stderr": output_stream(b"\x00", eof=False, binary=True)}}),
            tool_event("overlap", {"action": "status", "job_id": "original-job", "stdout_cursor": 1, "stderr_cursor": 1}, {
                "job": self.final_job, "output": {"stdout": output_stream(b"bcde", cursor=1, eof=False),
                                                   "stderr": output_stream(b"\xff", cursor=1, binary=True)}}),
        ]
        self.assertTrue(self.validate()["actor_lookup_reconciled"])
        output = self.events[-1]["item"]["result"]["structured_content"]["output"]
        output["stdout"]["text"] = "XXXX"
        with self.assertRaisesRegex(lab.LabError, "overlapping"):
            self.validate()

    def test_final_cursor_cannot_shrink_or_output_extend_past_closed_stream(self):
        self.events[3]["item"]["result"]["structured_content"]["output"]["stdout"] = output_stream(b"abc")
        for data, eof in ((b"abcd", False), (b"ab", True)):
            extra = tool_event("inconsistent", {"action": "status", "job_id": "original-job"}, {
                "job": self.final_job, "output": {"stdout": output_stream(data, eof=eof), "stderr": output_stream()}})
            with self.subTest(data=data), self.assertRaisesRegex(lab.LabError, "final cursor"):
                operations.validate_interruption([*self.events, extra], self.fault, self.final_job, self.receipt)

    def test_incomplete_outage_and_successful_uninterrupted_call_cannot_pass(self):
        baseline = deepcopy(self.fault)
        for key, value in (("success", False), ("marker_observed", False), ("execute_in_flight", False),
                           ("barrier_state", "completed"), ("recovered_state", "completed"),
                           ("outage_transport_error", None), ("restored_ns", 2_000_000_000)):
            self.fault = dict(baseline, **{key: value})
            with self.subTest(field=key), self.assertRaisesRegex(lab.LabError, "Incomplete"):
                self.validate()
        self.fault = baseline
        self.events[1] = tool_event("activation", self.arguments, {"job": {"job_id": "original-job", "state": "running"}})
        with self.assertRaisesRegex(lab.LabError, "no preserved tool/transport failure"):
            self.validate()

    def test_new_key_replay_or_changed_retry_identity_is_rejected(self):
        original = deepcopy(self.events)
        scenarios = (
            (2, dict(self.arguments, idempotency_key="new-key", command="another activation spelling"), "before.*reconciled"),
            (4, dict(self.arguments, idempotency_key="new-key"), "replayed"),
            (2, dict(self.arguments, connection_id="new-shell"), "different execution identity"),
        )
        for index, arguments, error in scenarios:
            self.events = deepcopy(original)
            self.events.insert(index, tool_event("new-activation", arguments, {"job": self.final_job}))
            with self.subTest(arguments=arguments), self.assertRaisesRegex(lab.LabError, error):
                self.validate()

    def test_other_job_or_missing_reconciliation_or_duplicate_receipt_is_rejected(self):
        for case in ("wrong-job", "no-lookup", "no-status", "undrained-output", "repeated-receipt", "missing-receipt"):
            events, job, receipt = deepcopy(self.events), deepcopy(self.final_job), deepcopy(self.receipt)
            if case == "wrong-job":
                job["job_id"] = "replacement-job"
            elif case == "no-lookup":
                events.pop(2)
            elif case == "no-status":
                events.pop(3)
            elif case == "undrained-output":
                events[3]["item"]["result"]["structured_content"]["output"]["stdout"]["eof"] = False
            elif case == "repeated-receipt":
                receipt.update(lines=2, matching_lines=2)
            else:
                receipt["exists"] = False
            with self.subTest(case=case), self.assertRaisesRegex(lab.LabError, "original activation exactly once"):
                operations.validate_interruption(events, self.fault, job, receipt)


class FaultObserverLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        with patch.object(operations, "MCPClient"):
            self.observer = operations.GatewayFault(self.directory, self.directory / "actor.jsonl", {"key": "key", "marker": "marker"},
                                                   ["ssh", "-F", "/captured/control/config", "control-vm"], timeout=60)
        self.observer.wait_for_barrier = MagicMock(return_value=({"id": "original-call"}, {"job_id": "original-job", "state": "running"}))

    def test_stop_failure_still_restores_and_aborts_actor_without_profile_changes(self):
        self.observer.service = MagicMock(side_effect=[lab.LabError("stop uncertain"), lab.LabError("restore failed")])
        with patch.object(lab, "select_profile") as profile:
            self.observer.observe()
        self.assertEqual([call.args[0] for call in self.observer.service.call_args_list], ["stop", "start"])
        self.assertTrue(self.observer.cancel_actor.is_set())
        self.assertIn("stop uncertain", self.observer.record["primary_error"])
        self.assertEqual(self.observer.record["cleanup_errors"], ["restore failed"])
        self.assertFalse(self.observer.record["success"])
        profile.assert_not_called()

    def test_real_outage_requires_transport_failure_then_same_running_job_recovery(self):
        self.observer.service = MagicMock()
        self.observer.client.request.side_effect = ConnectionError("gateway is down")
        self.observer.lookup = MagicMock(return_value={"job_id": "original-job", "state": "running"})
        with patch.object(self.observer.stop_observing, "wait", return_value=False) as wait:
            self.observer.observe()
        self.assertTrue(self.observer.record["success"])
        self.assertEqual(self.observer.record["outage_transport_error"]["type"], "ConnectionError")
        self.assertEqual(self.observer.record["recovered_job_id"], "original-job")
        wait.assert_called_once_with(operations.OUTAGE_SECONDS)
        self.observer.client.request.assert_called_once_with("tools/list", deadline=self.observer.deadline)
        self.assertLessEqual(self.observer.lookup.call_args.kwargs["deadline"], self.observer.deadline)

    def test_no_transport_outage_aborts_and_still_restores(self):
        self.observer.service = MagicMock()
        self.observer.lookup = MagicMock(return_value={"job_id": "original-job", "state": "running"})
        self.observer.observe()
        self.assertTrue(self.observer.cancel_actor.is_set())
        self.assertIn("remained reachable", self.observer.record["primary_error"])
        self.assertEqual([call.args[0] for call in self.observer.service.call_args_list], ["stop", "start"])

    def test_observer_deadline_failure_prompts_actor_abort_and_finish_joins_to_termination(self):
        self.observer.wait_for_barrier.side_effect = lab.LabError("readiness deadline")
        self.observer.observe()
        self.assertTrue(self.observer.cancel_actor.is_set())
        self.observer.thread = MagicMock()
        with self.assertRaisesRegex(lab.LabError, "readiness deadline"):
            self.observer.finish()
        self.observer.thread.join.assert_called_once_with()

    def test_stop_after_lookup_prevents_another_status_or_service_call(self):
        event = tool_event("activation", {"action": "execute", "idempotency_key": "key", "wait_ms": 10000}, started=True)
        self.observer.actor_path.write_text(json.dumps(event) + "\n")

        def lookup(**kwargs):
            self.assertEqual(kwargs["deadline"], self.observer.deadline)
            self.observer.stop_observing.set()
            return {"job_id": "original-job", "state": "running"}

        self.observer.lookup = lookup
        self.observer.wait_for_barrier = lambda: operations.GatewayFault.wait_for_barrier(self.observer)
        self.observer.service = MagicMock()
        self.observer.observe()
        self.observer.client.tool.assert_not_called()
        self.observer.service.assert_not_called()
        self.assertIn("Actor ended", self.observer.record["primary_error"])

    def test_service_receives_only_its_remaining_lifecycle_budget(self):
        def service_run(command, *, timeout, log_path, **kwargs):
            self.assertAlmostEqual(timeout, .25)
            self.assertEqual(command[:4], ["ssh", "-F", "/captured/control/config", "control-vm"])
            log_path.write_text("active\n")

        with patch.object(operations.time, "monotonic", return_value=100), patch.object(lab, "run", side_effect=service_run):
            self.observer.service("start", deadline=100.25)
        with patch.object(operations.time, "monotonic", return_value=101), patch.object(lab, "run") as run:
            with self.assertRaisesRegex(lab.LabError, "deadline"):
                self.observer.service("start", deadline=100.25)
            run.assert_not_called()

    def test_handled_interrupt_restores_and_joins_before_outer_diagnostics_and_shutdown(self):
        stopping, restoring, release = threading.Event(), threading.Event(), threading.Event()
        diagnostics_started = threading.Event()
        observed = {}

        def service(action, **kwargs):
            if action == "stop":
                stopping.set()
                if not self.observer.stop_observing.wait(2):
                    raise lab.LabError("test actor did not stop")
            else:
                restoring.set()
                if not release.wait(2):
                    raise lab.LabError("test restoration was not released")

        def actor(*args, **kwargs):
            self.assertTrue(stopping.wait(2))
            raise lab.LabInterrupted(signal.SIGTERM)

        def release_restoration():
            observed["restoring"] = restoring.wait(2)
            observed["cleanup_raced_restoration"] = diagnostics_started.wait(.05)
            release.set()

        def diagnostics(directory):
            diagnostics_started.set()
            self.assertFalse(self.observer.thread.is_alive())
            self.assertIn("restored_ns", self.observer.record)

        self.observer.service = service
        controller = threading.Thread(target=release_restoration)
        self.addCleanup(lambda: (release.set(), controller.join(2)))
        self.addCleanup(lambda: (release.set(), self.observer.thread.join(2)))
        controller.start()
        # The old timed join would leave the worker alive after this tiny bound.
        with patch.object(operations, "OBSERVER_JOIN_SECONDS", .01, create=True), \
             patch.object(operations, "GatewayFault", return_value=self.observer), \
             patch.object(operations.demo_actor, "run_actor", side_effect=actor), \
             patch.object(operations, "exercise", side_effect=lambda *args: operations.interrupted_actor(self.directory, "goal", {}, [], timeout=60)), \
             patch.object(lab, "new_results", return_value=("run", self.directory)), \
             patch.object(demo, "diagnostics", side_effect=diagnostics), \
             patch.object(demo, "down", side_effect=lambda: diagnostics(self.directory)), patch("builtins.print"):
            with self.assertRaises(lab.LabError) as caught:
                operations.run_operation()
        controller.join(2)
        self.assertEqual(caught.exception.exit_code, 143)
        self.assertTrue(observed["restoring"])
        self.assertFalse(observed["cleanup_raced_restoration"])
        self.assertFalse(self.observer.thread.is_alive())
        interrupted = json.loads((self.directory / "interrupted-actor.json").read_text())
        self.assertIn("SIGTERM", interrupted["primary_error"])
        self.assertIn("Actor ended", self.observer.record["primary_error"])

    def test_partial_live_json_is_ignored_until_the_whole_event_is_flushed(self):
        path = self.directory / "actor.jsonl"
        first = tool_event("first", {"action": "lookup", "idempotency_key": "key"})
        path.write_text(json.dumps(first) + '\n{"type":"item.started","item":')
        self.assertEqual(operations.live_events(path), [first])


class ObserverMCPBudgetTest(unittest.TestCase):
    def client(self):
        with patch.object(mcp_client.lab_oauth, "tokens", return_value={"access_token": "old", "refresh_token": "private", "expires_in": 0}):
            client = operations.MCPClient(request_timeout=5)
        client.expires = 0
        return client

    def test_oauth_refresh_and_mcp_dispatch_share_the_remaining_request_budget(self):
        for deadline, budget in ((103, 3), (1000, 5)):
            client, clock = self.client(), [100]

            def refresh(fields, *, timeout):
                self.assertEqual(timeout, budget)
                clock[0] += 2
                return {"access_token": "new", "expires_in": 60}

            with patch.object(mcp_client.time, "monotonic", side_effect=lambda: clock[0]), \
                 patch.object(mcp_client.lab_oauth, "token_request", side_effect=refresh), \
                 patch.object(mcp_client.urllib.request, "urlopen", return_value=StringIO('{"result":{"tools":[]}}')) as dispatch:
                self.assertEqual(client.request("tools/list", deadline=deadline), {"tools": []})
            self.assertEqual(dispatch.call_args.kwargs["timeout"], budget - 2)

    def test_expired_budget_or_refresh_exhaustion_cannot_dispatch_another_request(self):
        for expires_during_refresh in (False, True):
            client, clock = self.client(), [100]

            def refresh(fields, *, timeout):
                clock[0] = 104
                return {"access_token": "new", "expires_in": 60}

            with patch.object(mcp_client.time, "monotonic", side_effect=lambda: clock[0]), \
                 patch.object(mcp_client.lab_oauth, "token_request", side_effect=refresh) as token, \
                 patch.object(mcp_client.urllib.request, "urlopen") as dispatch:
                with self.assertRaisesRegex(lab.LabError, "deadline"):
                    client.request("tools/list", deadline=103 if expires_during_refresh else 99)
                self.assertEqual(token.call_count, int(expires_during_refresh))
                dispatch.assert_not_called()

    def test_waiting_for_oauth_lock_is_also_bounded(self):
        client = self.client()
        client.refresh_lock.acquire()
        self.addCleanup(client.refresh_lock.release)
        with patch.object(mcp_client.lab_oauth, "token_request") as token, patch.object(mcp_client.urllib.request, "urlopen") as dispatch:
            with self.assertRaisesRegex(lab.LabError, "deadline expired waiting for OAuth"):
                client.request("tools/list", deadline=operations.time.monotonic() + .01)
            token.assert_not_called()
            dispatch.assert_not_called()

    def test_token_exchange_passes_the_callers_timeout_to_http(self):
        with patch.object(mcp_client.lab_oauth.urllib.request, "urlopen", return_value=StringIO('{"access_token":"new"}')) as dispatch:
            self.assertEqual(mcp_client.lab_oauth.token_request({}, timeout=.25), {"access_token": "new"})
        self.assertEqual(dispatch.call_args.kwargs["timeout"], .25)


if __name__ == "__main__":
    unittest.main()
