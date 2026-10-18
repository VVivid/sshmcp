"""Focused host-only demo boundaries; no VM, network, or model invocation."""

import base64
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import signal
from subprocess import CompletedProcess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import agent_demo as demo
import demo_actor as actor
import demo_evaluator as evaluate
from mcp_client import MCPClient

lab = demo.lab


class ActorBoundaryTest(unittest.TestCase):
    def test_launch_keeps_mcp_transport_host_without_model_code_or_host_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)

            def capture(command, directory, name, **kwargs):
                value = [{"name": "sshmcp-private", "enabled": True}] if name.endswith("mcp-inventory") else []
                (directory / (name + ".jsonl")).write_text(json.dumps(value))
                return {"exit_code": 0}

            with patch.object(actor, "capture", side_effect=capture), \
                 patch.object(actor.mcp_lab, "codex_arguments", side_effect=lambda *args: ["codex", *args]):
                arguments = actor.isolated_arguments(folder, folder)
            pairs = list(zip(arguments, arguments[1:]))
            self.assertIn(("--enable", "code_mode_host"), pairs)
            for feature in ("code_mode", "shell_tool", "apps", "computer_use", "browser_use", "view_image"):
                self.assertIn(("--disable", feature), pairs)
            self.assertNotIn(("--disable", "code_mode_host"), pairs)
            self.assertIn("mcp_servers.sshmcp-private.required=true", arguments)
            self.assertFalse(any(argument.startswith("tools.view_image=") for argument in arguments))

    def test_skill_aliases_expand_to_exact_files_and_unresolved_aliases_fail(self):
        text = '- `r0` = `/private/skills`\n- example (file: r0/example/SKILL.md)'
        self.assertEqual(actor.catalog_paths(text), ["/private/skills/example/SKILL.md"])
        with self.assertRaises(lab.LabError):
            actor.catalog_paths("- unknown (file: r9/example/SKILL.md)")
        with self.assertRaises(lab.LabError):
            actor.assert_isolated([{"content": [{"text": "## Skills\nAvailable skills"}]}])

    def test_failed_local_preflight_prevents_prompt_or_model_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with patch.object(actor, "new_workspace", return_value=folder), \
                 patch.object(actor, "preflight", side_effect=lab.LabError("not authenticated")), \
                 patch.object(actor, "isolated_arguments") as isolate, patch.object(actor, "capture") as launch:
                with self.assertRaisesRegex(lab.LabError, "not authenticated"):
                    actor.run_actor(folder, "fresh", "goal")
            isolate.assert_not_called()
            launch.assert_not_called()
            self.assertEqual(json.loads((folder / "fresh/outcome.json").read_text())["failure_category"], "preflight")

    def test_preflight_requires_cli_auth_and_exact_authenticated_catalog(self):
        for auth_exit, names, expected in ((1, [], "authentication"), (0, ["connect", "run_command", "disconnect", "extra"], "mcp_catalog"),
                                           (0, ["connect", "run_command", "disconnect", "connect"], "mcp_catalog"),
                                           (0, ["connect", "run_command", "disconnect"], None)):
            with self.subTest(auth_exit=auth_exit, names=names), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)

                def capture(command, directory, name, **kwargs):
                    (directory / (name + ".jsonl")).write_text("codex-cli 0.162.1\n" if name == "cli-version" else "")
                    return {"exit_code": auth_exit if name == "cli-auth" else 0}

                with patch.object(actor.shutil, "which", return_value="/installed/codex"), patch.object(actor, "capture", side_effect=capture), \
                     patch("mcp_client.MCPClient") as client:
                    client.return_value.request.return_value = {"tools": [{"name": name} for name in names]}
                    if expected:
                        with self.assertRaises(lab.LabError):
                            actor.preflight(folder, folder)
                    else:
                        result = actor.preflight(folder, folder)
                        self.assertTrue(result["required_mcp"])
                        self.assertEqual(result["quota"], "unknown")
                    if auth_exit:
                        client.assert_not_called()
                record = json.loads((folder / "preflight.json").read_text())
                self.assertEqual(record.get("failure_category"), expected)

    def test_private_output_and_progress_are_visible_before_child_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            line = json.dumps({"type": "thread.started", "thread_id": "private-secret-text"}) + "\n"
            script = ("import json,os,pathlib,sys,time\n"
                      f"root=pathlib.Path({str(folder)!r})\n"
                      f"sys.stdout.write({line!r});sys.stdout.flush()\n"
                      "sys.stderr.write('private stderr');sys.stderr.flush()\n"
                      "deadline=time.monotonic()+2\n"
                      "while time.monotonic()<deadline:\n"
                      " try:\n"
                      "  record=json.loads((root/'actor.process.json').read_text())\n"
                      "  if (record['running'] and record['pid']==os.getpid() and record['event_types'].get('thread.started')==1 "
                      "and (root/'actor.jsonl').read_text() and (root/'actor.stderr.log').read_text()=='private stderr'):sys.exit(0)\n"
                      " except (OSError,ValueError,KeyError):pass\n"
                      " time.sleep(.01)\n"
                      "sys.exit(3)\n")
            visible = StringIO()
            with redirect_stdout(visible):
                result = actor.capture([sys.executable, "-c", script], folder, "actor", cwd=folder, timeout=4)
            self.assertEqual(result["exit_code"], 0, "child could not read its retained stdout/stderr and live metadata before exit")
            self.assertFalse(result["running"])
            self.assertNotIn("private-secret-text", visible.getvalue())
            self.assertNotIn("private stderr", visible.getvalue())

    def test_observer_cancellation_stops_owned_child_without_waiting_for_session_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            cancel = threading.Event()
            cancel.set()
            with self.assertRaisesRegex(lab.LabError, "fault observer failed"):
                actor.capture([sys.executable, "-c", "import time;time.sleep(60)"], folder, "actor", cwd=folder, cancel_event=cancel)
            record = json.loads((folder / "actor.process.json").read_text())
            self.assertFalse(record["running"])
            self.assertLess(record["duration_seconds"], 5)

    def transcript(self, path, *, host_action=False, completed=True):
        def call(identity, tool, args, body):
            return {"type": "item.completed", "item": {"id": identity, "type": "mcp_tool_call", "server": "sshmcp-private", "tool": tool,
                                                        "arguments": args, "status": "completed", "result": {"structured_content": body}}}
        events = [{"type": "thread.started", "thread_id": "fresh-session"},
                  call("1", "connect", {"host_alias": "lab-target"}, {"connection": {"state": "active", "connection_id": "shell"}}),
                  call("2", "run_command", {"action": "execute", "idempotency_key": "key", "command": "printf ok"}, {"job": {"job_id": "job", "state": "accepted"}})]
        if completed:
            events.append(call("3", "run_command", {"action": "status", "job_id": "job"}, {"job": {"job_id": "job", "state": "completed", "exit_code": 0}}))
        if host_action:
            events.append({"type": "item.completed", "item": {"type": "command_execution", "command": "ssh target"}})
        events.extend([call("4", "disconnect", {"connection_id": "shell"}, {"connection": {"connection_id": "shell", "state": "closed"}}), {"type": "turn.completed"}])
        path.write_text("\n".join(json.dumps(event) for event in events))

    def test_exit_zero_and_agent_claims_cannot_replace_actual_remote_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "actor.jsonl"
            self.transcript(path, completed=False)
            with self.assertRaisesRegex(lab.LabError, "exit status alone"):
                actor.audit_transcript(path)
            self.transcript(path)
            self.assertEqual(actor.audit_transcript(path)["completed_jobs"], 1)
            self.transcript(path, host_action=True)
            with self.assertRaisesRegex(lab.LabError, "disallowed host action"):
                actor.audit_transcript(path)

    def test_runtime_diagnostics_are_counted_without_accepting_missing_mcp_work(self):
        diagnostic = {"type": "item.completed", "item": {"id": "diagnostic", "type": "error", "message": "transport unavailable"}}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "actor.jsonl"
            self.transcript(path)
            with path.open("a") as output:
                output.write("\n" + json.dumps(diagnostic))
            self.assertEqual(actor.audit_transcript(path)["runtime_errors"], 1)
            events = [{"type": "thread.started", "thread_id": "fresh-session"}, diagnostic, {"type": "turn.completed"}]
            path.write_text("\n".join(json.dumps(event) for event in events))
            with self.assertRaisesRegex(lab.LabError, "1 runtime errors recorded; exit status alone"):
                actor.audit_transcript(path)
            events.append({"type": "item.completed", "item": {"type": "command_execution", "command": "ssh target"}})
            path.write_text("\n".join(json.dumps(event) for event in events))
            with self.assertRaisesRegex(lab.LabError, "disallowed host action"):
                actor.audit_transcript(path)

    def test_uncertain_execute_is_counted_when_the_same_key_is_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "actor.jsonl"
            self.transcript(path)
            events = [json.loads(line) for line in path.read_text().splitlines()]
            failed = json.loads(json.dumps(events[2]))
            failed["item"].update(id="uncertain", status="failed", error={"message": "transport closed"}, result=None)
            events.insert(2, failed)
            path.write_text("\n".join(map(json.dumps, events)))
            result = actor.audit_transcript(path)
            self.assertEqual(result["tool_errors"], 1)
            self.assertEqual(result["repeated_execute_keys"], 1)
            self.assertEqual(result["repeated_source_submissions"], 1)

    def test_disconnect_accepts_only_same_idle_terminal_connection_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "actor.jsonl"
            self.transcript(path)
            original = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(actor.audit_transcript(path)["closed_disconnects"], 1)
            for state, identity, busy, accepted in (
                    ("lost", "shell", "", True),
                    ("active", "shell", "", False),
                    ("unknown", "shell", "", False),
                    ("lost", "other-shell", "", False),
                    ("closed", "other-shell", "", False),
                    ("lost", "shell", "unfinished-job", False),
                    ("closed", "shell", "unfinished-job", False)):
                with self.subTest(state=state, identity=identity, busy=busy):
                    events = json.loads(json.dumps(original))
                    result = events[-2]["item"]["result"]
                    result["structured_content"]["connection"].update(state=state, connection_id=identity, busy_job_id=busy)
                    path.write_text("\n".join(json.dumps(event) for event in events))
                    if accepted:
                        summary = actor.audit_transcript(path)
                        self.assertEqual(summary["lost_disconnects"], 1)
                        self.assertEqual(summary["closed_disconnects"], 0)
                    else:
                        with self.assertRaisesRegex(lab.LabError, "matching idle terminal result"):
                            actor.audit_transcript(path)
            events = json.loads(json.dumps(original))
            events[-2]["item"]["result"]["isError"] = True
            path.write_text("\n".join(json.dumps(event) for event in events))
            with self.assertRaisesRegex(lab.LabError, "exit status alone"):
                actor.audit_transcript(path)

    def test_child_timeout_stops_its_group_before_recording_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with patch.object(actor.subprocess, "Popen", wraps=actor.subprocess.Popen) as spawn, \
                 patch.object(actor, "terminate_group", wraps=actor.terminate_group) as terminate:
                with self.assertRaisesRegex(lab.LabError, "deadline"):
                    actor.capture([sys.executable, "-c", "import time; time.sleep(60)"], folder, "actor", cwd=folder, prompt="goal", timeout=.05)
                self.assertTrue(spawn.call_args.kwargs["start_new_session"])
                self.assertIsNotNone(terminate.call_args.args[0].poll())
                self.assertIn("failure", json.loads((folder / "actor.process.json").read_text()))

    def test_capture_caps_each_stream_in_bytes_and_marks_the_retained_prefix(self):
        for stream, descriptor in (("stdout", 1), ("stderr", 2)):
            with self.subTest(stream=stream), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                command = [sys.executable, "-c", f"import os,time; os.write({descriptor}, 'é'.encode()*100); time.sleep(60)"]
                with patch.object(actor, "STREAM_LIMIT", 31), self.assertRaisesRegex(lab.LabError, stream + ".*quota"):
                    actor.capture(command, folder, "actor", cwd=folder)
                prefix = folder / ("actor.jsonl" if stream == "stdout" else "actor.stderr.log")
                self.assertEqual(prefix.read_bytes(), ("é" * 100).encode()[:31])
                outcome = json.loads((folder / "actor.process.json").read_text())
                self.assertTrue(outcome["output_truncated"])
                self.assertFalse(outcome["output_complete"])
                self.assertEqual(outcome["streams"][stream], {"retained_bytes": 31, "observed_bytes": 32, "truncated": True})
                self.assertLessEqual((folder / "actor.jsonl").stat().st_size, 31)
                self.assertLessEqual((folder / "actor.stderr.log").stat().st_size, 31)

    def test_capture_drains_both_exact_quotas_while_sending_a_large_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            prompt = "é" * 65536
            size = len(prompt.encode())
            script = "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.stderr.buffer.write(data)"
            with patch.object(actor, "STREAM_LIMIT", size):
                result = actor.capture([sys.executable, "-c", script], folder, "actor", cwd=folder, prompt=prompt)
            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(result["output_complete"])
            self.assertFalse(result["output_truncated"])
            self.assertEqual((folder / "actor.jsonl").read_bytes(), prompt.encode())
            self.assertEqual((folder / "actor.stderr.log").read_bytes(), prompt.encode())

    def test_overflow_kills_a_real_descendant_that_ignores_sigterm(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            identity, heartbeat = folder / "child.json", folder / "heartbeat"
            child = ("import json,os,pathlib,signal,time\n"
                     "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                     f"pathlib.Path({str(identity)!r}).write_text(json.dumps({{'pid':os.getpid(),'group':os.getpgrp()}}))\n"
                     f"output=open({str(heartbeat)!r},'ab',buffering=0)\n"
                     "while True:\n output.write(b'.');time.sleep(.01)\n")
            parent = ("import pathlib,subprocess,sys,time\n"
                      f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
                      f"while not pathlib.Path({str(heartbeat)!r}).exists():time.sleep(.01)\n"
                      "sys.stdout.buffer.write(b'x'*65);sys.stdout.buffer.flush();time.sleep(60)\n")
            try:
                with patch.object(actor.os, "killpg", wraps=os.killpg) as kill_group, patch.object(actor, "STREAM_LIMIT", 64):
                    with self.assertRaisesRegex(lab.LabError, "quota"):
                        actor.capture([sys.executable, "-c", parent], folder, "actor", cwd=folder, timeout=5)
                child_identity = json.loads(identity.read_text())
                self.assertEqual(kill_group.call_args_list, [call(child_identity["group"], signal.SIGTERM), call(child_identity["group"], signal.SIGKILL)])
                size = heartbeat.stat().st_size
                time.sleep(.1)
                self.assertEqual(heartbeat.stat().st_size, size)
            finally:
                if identity.exists():
                    try:
                        os.kill(json.loads(identity.read_text())["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_audit_streams_and_rejects_oversized_complete_prefixes_and_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "actor.jsonl"
            self.transcript(path)
            limit = path.stat().st_size
            with patch.object(actor, "STREAM_LIMIT", limit), patch.object(Path, "read_text", side_effect=AssertionError("whole-file read")):
                self.assertEqual(actor.audit_transcript(path)["completed_jobs"], 1)
                with path.open("ab") as output:
                    output.write(b'\n{"type":"turn.completed"}\n')
                with self.assertRaisesRegex(lab.LabError, "byte quota"):
                    actor.audit_transcript(path)
                path.write_bytes(b" " * (limit + 1))
                with self.assertRaisesRegex(lab.LabError, "byte quota"):
                    actor.audit_transcript(path)


class EvaluatorBoundaryTest(unittest.TestCase):
    def trace(self, *, linked=True, base64_ids=False, started=100):
        trace, proxy, app = "a" * 32, "b" * 16, "c" * 16
        encode = lambda value: base64.b64encode(bytes.fromhex(value)).decode() if base64_ids else value
        batches = []
        for service, identity, parent in (("traefik", proxy, ""), ("hello-go", app, proxy if linked else "d" * 16)):
            batches.append({"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                            "scopeSpans": [{"spans": [{"traceId": encode(trace), "spanId": encode(identity), "parentSpanId": encode(parent) if parent else "",
                                                       "kind": "SPAN_KIND_SERVER", "startTimeUnixNano": started}]}]})
        return {"batches": batches}

    def test_trace_requires_actual_fresh_cross_service_ancestry(self):
        self.assertTrue(evaluate.connected_trace(self.trace(base64_ids=True), "a" * 32, 100))
        self.assertIsNone(evaluate.connected_trace(self.trace(linked=False), "a" * 32, 100))
        self.assertIsNone(evaluate.connected_trace(self.trace(), "e" * 32, 100))
        self.assertIsNone(evaluate.connected_trace(self.trace(), "a" * 32, 10_000_000_000))

    def test_loki_metadata_filtered_results_need_not_render_trace_id(self):
        document = {"status": "success", "data": {"resultType": "streams", "result": [{"values": [["10000000000", "request handled", {"trace_id": "a" * 32}]]}]}}
        self.assertEqual(len(evaluate.loki_entries(document, 10_000_000_000)), 1)
        self.assertFalse(evaluate.loki_entries(document, 20_000_000_000))
        self.assertFalse(evaluate.proxy_json_log([["0", "request succeeded"]], "probe"))
        self.assertTrue(evaluate.proxy_json_log([["0", json.dumps({"RequestMethod": "GET", "DownstreamStatus": 200, "request_X-Sshmcp-Probe": "probe"})]], "probe"))

    def test_histogram_or_unrelated_counter_cannot_satisfy_application_counter(self):
        instance = object.__new__(evaluate.Evaluator)
        flags = {"status": "success", "data": {"enable-feature": "type-and-unit-labels"}}
        vector = {"status": "success", "data": {"result": [
            {"metric": {"__name__": "http_requests_total", "__type__": "counter", "job": "other-service"}},
            {"metric": {"__name__": "http_requests_count", "__type__": "counter", "job": "hello-go"}},
            {"metric": {"__name__": "http_requests", "__type__": "histogram", "job": "hello-go"}},
            {"metric": {"__name__": "http_requests_gauge_total", "__type__": "gauge", "job": "hello-go"}},
            {"metric": {"__name__": "http_requests_unknown_total", "job": "hello-go"}},
            {"metric": {"__name__": "http_requests_bytes_total", "__type__": "counter", "job": "hello-go"}},
            {"metric": {"__name__": "http_request_volume_total", "__type__": "counter", "__unit__": "By", "job": "hello-go"}},
        ]}}
        instance.backend = lambda kind, path, *args: flags if path.endswith("flags") else vector
        instance.sample = lambda selector: 100
        self.assertEqual(instance.counters(), {})

    def test_received_otlp_counter_types_work_without_scrape_metadata(self):
        instance = object.__new__(evaluate.Evaluator)
        queries = []

        def backend(kind, path, params=None):
            queries.append((path, params))
            if path.endswith("metadata"):
                return {"status": "success", "data": {}}
            if path.endswith("flags"):
                return {"status": "success", "data": {"enable-feature": "exemplar-storage,type-and-unit-labels"}}
            if params["query"].startswith("sum("):
                return {"status": "success", "data": {"result": [{"value": [10, "100"]}]}}
            return {"status": "success", "data": {"result": [
                {"metric": {"__name__": "aa_requests_bytes_total", "__type__": "counter", "job": "hello-go"}},
                {"metric": {"__name__": "http_requests_total", "__type__": "counter", "job": "demo/hello-go"}},
                {"metric": {"__name__": "traefik_service_requests_total", "__type__": "counter", "job": "traefik"}},
            ]}}

        instance.backend = backend
        counters = instance.counters(fresh_since=10_000_000_000)
        self.assertEqual(set(counters), {"hello-go", "traefik"})
        self.assertEqual(counters["hello-go"]["selector"], 'http_requests_total{__type__="counter",job="demo/hello-go"}')
        self.assertEqual(counters["traefik"]["selector"], 'traefik_service_requests_total{__type__="counter",job="traefik"}')
        self.assertTrue(all(counter["before"] == 100 for counter in counters.values()))
        self.assertFalse(any(path.endswith("metadata") for path, _ in queries))
        self.assertTrue(all('and ignoring(__type__,__unit__) (timestamp(' in params["query"] and '>= 10.0' in params["query"]
                            for path, params in queries if params and params["query"].startswith("sum(")))

    def test_missing_type_feature_fails_before_counter_polling(self):
        instance = object.__new__(evaluate.Evaluator)
        for features in ("", "exemplar-storage", "not-type-and-unit-labels"):
            with self.subTest(features=features), \
                 patch.object(instance, "backend", return_value={"status": "success", "data": {"enable-feature": features}}) as backend:
                with self.assertRaisesRegex(lab.LabError, "Enable.*type-and-unit-labels"):
                    instance.counters()
                self.assertEqual(backend.call_args_list, [call("prometheus", "/api/v1/status/flags")])

    def test_typed_counter_acceptance_still_requires_the_request_burst_delta(self):
        for delta in (11, 12):
            with self.subTest(delta=delta), tempfile.TemporaryDirectory() as temporary:
                instance = object.__new__(evaluate.Evaluator)
                instance.directory, instance.wait = Path(temporary), 1
                warm = {"started_ns": 1_000_000_000}
                probe = {"started_ns": 2_000_000_000, "trace_id": "a" * 32, "marker": "fresh-request", "count": 12}

                def backend(kind, path, params=None):
                    if path.endswith("flags"):
                        return {"status": "success", "data": {"enable-feature": "type-and-unit-labels"}}
                    if params["query"].startswith("sum("):
                        value = 100 + delta if ">= 2.0" in params["query"] else 100
                        return {"status": "success", "data": {"result": [{"value": [2, str(value)]}]}}
                    return {"status": "success", "data": {"result": [
                        {"metric": {"__name__": "http_requests_total", "__type__": "counter", "job": "hello-go"}},
                        {"metric": {"__name__": "traefik_requests_total", "__type__": "counter", "job": "traefik"}},
                    ]}}

                instance.backend = backend
                log = [["2000000000", json.dumps({"RequestMethod": "GET", "DownstreamStatus": 200, "probe": probe["marker"]})]]
                with patch.object(instance, "discover"), patch.object(instance, "traffic", side_effect=[warm, probe]) as traffic, \
                     patch.object(instance, "trace", return_value=self.trace(started=probe["started_ns"])), \
                     patch.object(instance, "logs", return_value=log), patch.object(instance, "dashboard", return_value={"working": True}), \
                     patch.object(evaluate.time, "monotonic", side_effect=[0, 0, 1, 1, 2]), patch.object(evaluate.time, "sleep"):
                    if delta == 12:
                        self.assertTrue(instance.evaluate()["counters_increased"])
                    else:
                        with self.assertRaisesRegex(lab.LabError, "acceptance timed out"):
                            instance.evaluate()
                    self.assertEqual(traffic.call_args_list, [call(count=3), call(count=12)])
                evidence = json.loads((instance.directory / "evaluation.json").read_text())
                self.assertEqual(evidence["counters_increased"], delta == 12)
                self.assertTrue(all(counter["after"] == 100 + delta for counter in evidence["counters"].values()))

    def test_dashboard_requires_populated_app_and_proxy_streams(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance = object.__new__(evaluate.Evaluator)
            instance.directory = Path(temporary)
            instance.sources = {"prometheus": {"uid": "metrics"}, "tempo": {"uid": "traces"},
                                "loki": {"uid": "logs", "jsonData": {"derivedFields": [{"datasourceUid": "traces", "url": "${__value.raw}",
                                                                                            "matcherType": "label", "matcherRegex": "trace_id"}]}}}
            counters = {"hello-go": {"name": "http_requests_total"}}
            probe = {"trace_id": "a" * 32, "started_ns": 10_000_000_000}
            panels = [{"type": "timeseries", "datasource": {"uid": "metrics"}, "targets": [
                {"expr": "sum(increase(http_requests_total[$__range])) / ${__range_s}"},
                {"expr": "rate(http_duration_seconds[$__rate_interval]) + $__interval_ms"}]},
                {"type": "logs", "datasource": {"uid": "logs"}, "targets": [{"expr": '{service_name=~"hello-go|traefik"} |= "${probe}" |= "$probe"'}]}]
            declaration = {"name": "probe", "type": "textbox", "current": {"value": ""}}
            dashboard = {"panels": panels, "templating": {"list": [declaration]}}
            instance.json = lambda path, *args: [{"uid": "dashboard"}] if path == "/api/search" else {"dashboard": dashboard}
            expected_log_query = '{service_name=~"hello-go|traefik"} |= "" |= ""'
            for proxy_service, proxy_values, expected in ((None, [], False), ("traefik", [], False),
                                                           ("traefik", [["0", "old proxy log"]], False),
                                                           ("other-service", [["10000000000", "unrelated log"]], False),
                                                           ("traefik", [["10000000000", "proxy log"]], True)):
                with self.subTest(proxy_service=proxy_service, values=proxy_values):
                    queries = []

                    def backend(kind, path, params):
                        queries.append((kind, params["query"]))
                        if kind == "prometheus":
                            self.assertIn(params["query"], ("sum(increase(http_requests_total[5m])) / 300", "rate(http_duration_seconds[5m]) + 60000"))
                            return {"status": "success", "data": {"result": [{"value": [10, "1"]}]}}
                        self.assertEqual(params["query"], expected_log_query)
                        streams = [{"stream": {"service_name": "hello-go"}, "values": [["10000000000", "app log"]]}]
                        if proxy_service:
                            streams.append({"stream": {"service_name": proxy_service}, "values": proxy_values})
                        return {"status": "success", "data": {"resultType": "streams", "result": streams}}

                    instance.backend = backend
                    if expected:
                        checks = instance.dashboard(counters, [["10000000000", "correlated app log"]], probe)
                        self.assertTrue(checks["app_logs"] and checks["proxy_logs"])
                    else:
                        with self.assertRaisesRegex(lab.LabError, "application/proxy logs"):
                            instance.dashboard(counters, [["10000000000", "correlated app log"]], probe)
                        checks = json.loads((instance.directory / "dashboard-checks.json").read_text())
                        self.assertTrue(checks["app_logs"])
                        self.assertFalse(checks["proxy_logs"])
            for declarations in ([], [{**declaration, "current": {"value": "nonempty"}}],
                                 [{**declaration, "current": {"value": []}}], [{**declaration, "type": "custom"}]):
                with self.subTest(unsupported_declaration=declarations):
                    dashboard["templating"]["list"] = declarations
                    queries.clear()
                    with self.assertRaisesRegex(lab.LabError, "unsupported variable.*probe"):
                        instance.dashboard(counters, [["10000000000", "correlated app log"]], probe)
                    self.assertNotIn("loki", [kind for kind, _ in queries])
            panels[-1]["targets"][0]["expr"] = '{service_name=~"hello-go|traefik"} |~ "${probe:raw}"'
            for value in ("", ".*", r"(hello|proxy)\1", "${literal}"):
                with self.subTest(raw_textbox=value):
                    dashboard["templating"]["list"] = [{**declaration, "current": {"value": value}}]
                    expected_log_query = '{service_name=~"hello-go|traefik"} |~ "' + value + '"'
                    checks = instance.dashboard(counters, [["10000000000", "correlated app log"]], probe)
                    self.assertTrue(checks["app_logs"] and checks["proxy_logs"])
            for declarations in ([], [{**declaration, "current": {}}], [{**declaration, "current": {"value": []}}],
                                 [{**declaration, "current": {"value": 123}}], [{**declaration, "current": {"value": "x" * 4097}}],
                                 [{**declaration, "type": "custom"}]):
                with self.subTest(unsupported_raw_declaration=declarations):
                    dashboard["templating"]["list"] = declarations
                    queries.clear()
                    with self.assertRaisesRegex(lab.LabError, "unsupported variable.*probe"):
                        instance.dashboard(counters, [["10000000000", "correlated app log"]], probe)
                    self.assertNotIn("loki", [kind for kind, _ in queries])
            dashboard["templating"]["list"] = [declaration]
            for expression in ('${probe:regex}', '${probe:raw:invalid}'):
                with self.subTest(unsupported_format=expression):
                    panels[-1]["targets"][0]["expr"] = expression
                    queries.clear()
                    with self.assertRaisesRegex(lab.LabError, "unsupported.*variable"):
                        instance.dashboard(counters, [["10000000000", "correlated app log"]], probe)
                    self.assertNotIn("loki", [kind for kind, _ in queries])

    def test_counter_baseline_filters_underlying_sample_time_and_nan_panels_fail(self):
        instance = object.__new__(evaluate.Evaluator)
        queries = []

        def backend(kind, path, params):
            queries.append(params["query"])
            return {"status": "success", "data": {"result": []}}

        instance.backend = backend
        self.assertIsNone(instance.sample('requests_total{job="hello-go"}', after_ns=10_000_000_000))
        self.assertIn('timestamp(requests_total{job="hello-go"}) >= 10.0', queries[0])
        self.assertFalse(evaluate.numeric_result({"status": "success", "data": {"result": [{"value": [10, "NaN"]}]}}))


class OperatorBoundaryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.folder = self.directory / "operator"
        target = patch.object(demo, "target")
        self.target = target.start()
        self.addCleanup(target.stop)
        factory = patch.object(demo, "MCPClient")
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        capture = patch.object(actor, "capture")
        self.capture = capture.start()
        self.addCleanup(capture.stop)
        self.client = self.factory.return_value
        self.active = {"connection": {"connection_id": "operator-shell", "state": "active"}}
        self.accepted = {"job": {"job_id": "operator-job", "state": "accepted"}}
        self.closed = {"connection": {"state": "closed"}}
        self.job = {"job_id": "operator-job", "state": "completed", "exit_code": 0}
        self.client.tool.side_effect = [self.active, self.accepted, self.closed]
        self.client.wait.return_value = self.job, b'{"observed":true}\n', b"private stderr\xff\n"

    def outcome(self):
        return json.loads((self.folder / "probe.process.json").read_text())

    def test_operator_uses_only_mcp_and_preserves_separate_private_evidence(self):
        script = "print(\"literal 'quotes', $HOME, and `text`\")\n"
        self.assertEqual(demo.operator(self.directory, "probe", script, timeout=60), {"observed": True})
        self.target.assert_called_once_with()
        self.capture.assert_not_called()
        self.factory.assert_called_once_with(transcript=self.folder / "probe.mcp.jsonl", request_timeout=15)
        self.assertEqual([item.args[0] for item in self.client.tool.call_args_list], ["connect", "run_command", "disconnect"])
        self.assertEqual(self.client.tool.call_args_list[0], call("connect", host_alias="lab-target"))
        submission = self.client.tool.call_args_list[1].kwargs
        self.assertEqual(shlex.split(submission["command"]), ["python3", "-c", script])
        self.assertEqual((submission["action"], submission["connection_id"], submission["timeout_ms"]), ("execute", "operator-shell", 60000))
        self.assertEqual(submission["idempotency_key"], self.outcome()["idempotency_key"])
        self.client.wait.assert_called_once_with("operator-job", timeout=70)
        self.assertEqual(self.client.tool.call_args_list[-1], call("disconnect", connection_id="operator-shell"))
        self.assertEqual((self.folder / "probe.input.py").read_text(), script)
        self.assertEqual((self.folder / "probe.jsonl").read_bytes(), b'{"observed":true}\n')
        self.assertEqual((self.folder / "probe.stderr.log").read_bytes(), b"private stderr\xff\n")
        self.assertEqual(json.loads((self.folder / "probe.job.json").read_text()), self.job)
        self.assertTrue(self.outcome()["success"])
        self.assertEqual(self.outcome()["cleanup_errors"], [])
        self.assertEqual((self.folder / "probe.jsonl").stat().st_mode & 0o777, 0o600)

    def test_ownership_refusal_prevents_operator_transport_and_management_fallback(self):
        self.target.side_effect = lab.LabError("unowned target")
        with self.assertRaisesRegex(lab.LabError, "unowned target"):
            demo.operator(self.directory, "probe", "print('{}')")
        self.factory.assert_not_called()
        self.capture.assert_not_called()

    def test_uncertain_execute_is_not_replayed_and_disconnect_is_attempted(self):
        self.client.tool.side_effect = [self.active, lab.LabError("execute acknowledgement lost"), self.closed]
        with self.assertRaisesRegex(lab.LabError, "execute acknowledgement lost"):
            demo.operator(self.directory, "probe", "print('{}')")
        self.assertEqual([item.args[0] for item in self.client.tool.call_args_list], ["connect", "run_command", "disconnect"])
        self.client.wait.assert_not_called()
        self.capture.assert_not_called()
        self.assertEqual(self.outcome()["primary_error"], "execute acknowledgement lost")
        self.assertEqual(self.outcome()["cleanup_errors"], [])
        self.assertFalse(self.outcome()["success"])
        self.assertTrue(self.outcome()["idempotency_key"])

    def test_wait_and_disconnect_failures_retain_primary_and_accepted_job(self):
        self.client.wait.side_effect = lab.LabError("status unavailable")
        self.client.tool.side_effect = [self.active, self.accepted, lab.LabError("disconnect unavailable")]
        with self.assertRaisesRegex(lab.LabError, "status unavailable.*disconnect unavailable"):
            demo.operator(self.directory, "probe", "print('{}')")
        self.assertEqual(self.outcome()["primary_error"], "status unavailable")
        self.assertEqual(self.outcome()["cleanup_errors"], ["disconnect unavailable"])
        self.assertEqual(json.loads((self.folder / "probe.job.json").read_text()), self.accepted["job"])
        self.assertFalse(self.outcome()["success"])

    def test_unclosed_disconnect_refuses_operator_success(self):
        self.client.tool.side_effect = [self.active, self.accepted, self.active]
        with self.assertRaisesRegex(lab.LabError, "cleanup failed.*did not close"):
            demo.operator(self.directory, "probe", "print('{}')")
        self.assertFalse(self.outcome()["success"])
        self.assertNotIn("primary_error", self.outcome())
        self.assertEqual(self.outcome()["cleanup_errors"], ["Independent operator MCP connection did not close."])

    def test_interruption_keeps_its_type_and_still_records_disconnect_failure(self):
        self.client.wait.side_effect = lab.LabInterrupted(signal.SIGTERM)
        self.client.tool.side_effect = [self.active, self.accepted, lab.LabError("disconnect unavailable")]
        with self.assertRaises(lab.LabInterrupted) as caught:
            demo.operator(self.directory, "probe", "print('{}')")
        self.assertEqual(caught.exception.signum, signal.SIGTERM)
        self.assertIn("SIGTERM", self.outcome()["primary_error"])
        self.assertEqual(self.outcome()["cleanup_errors"], ["disconnect unavailable"])

    def test_operator_bounds_both_output_streams_and_marks_incomplete_evidence(self):
        bound = 8 * 1024 * 1024
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                self.client.tool.side_effect = [self.active, self.accepted, self.closed]
                stdout, stderr = (b"x" * (bound + 1), b"") if stream == "stdout" else (b"{}", b"x" * (bound + 1))
                self.client.wait.return_value = self.job, stdout, stderr
                with self.assertRaisesRegex(lab.LabError, "finite export bound"):
                    demo.operator(self.directory, "probe", "print('{}')")
                path = self.folder / ("probe.jsonl" if stream == "stdout" else "probe.stderr.log")
                self.assertEqual(path.stat().st_size, bound)
                self.assertTrue(self.outcome()["output_truncated"])
                self.assertEqual(self.outcome()[stream + "_bytes"], bound + 1)
                self.assertFalse(self.outcome()["success"])

    def test_operator_requires_completed_exit_zero_before_accepting_json(self):
        for state, exit_code in (("failed", 0), ("completed", 1)):
            with self.subTest(state=state, exit_code=exit_code):
                self.client.tool.side_effect = [self.active, self.accepted, self.closed]
                self.client.wait.return_value = {**self.job, "state": state, "exit_code": exit_code}, b"{}", b"private failure"
                with self.assertRaisesRegex(lab.LabError, "Independent MCP operator probe failed"):
                    demo.operator(self.directory, "probe", "print('{}')")
                self.assertFalse(self.outcome()["success"])
                self.assertEqual((self.folder / "probe.stderr.log").read_text(), "private failure")


class DemoLifecycleBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.addCleanup(lab.select_profile, "control")

    def test_interruption_saves_evidence_before_diagnostics_and_both_vm_shutdown(self):
        events = []

        def save(path, result):
            events.append("save")
            self.assertIn("SIGTERM", result["primary_error"])

        def stop():
            self.assertIn("save", events)
            events.append("stop")

        with patch.object(lab, "new_results", return_value=("run", self.directory)), \
             patch.object(demo, "test_demo", side_effect=lab.LabInterrupted(signal.SIGTERM)), \
             patch.object(demo, "save_json", side_effect=save), \
             patch.object(demo, "invalidate_ready"), \
             patch.object(demo, "diagnostics", side_effect=lambda directory: events.append("diagnostics")), \
             patch.object(demo, "down", side_effect=stop), patch("builtins.print"):
            with self.assertRaises(lab.LabError) as caught:
                demo.run_operation("test")
        self.assertEqual(caught.exception.exit_code, 143)
        self.assertLess(events.index("save"), events.index("diagnostics"))
        self.assertLess(events.index("diagnostics"), events.index("stop"))

    def test_diagnostic_failure_still_stops_and_preserves_primary_error(self):
        saved = []
        with patch.object(lab, "new_results", return_value=("run", self.directory)), \
             patch.object(demo, "test_demo", side_effect=lab.LabError("actor failed")), \
             patch.object(demo, "save_json", side_effect=lambda path, result: saved.append(json.loads(json.dumps(result)))), \
             patch.object(demo, "invalidate_ready"), \
             patch.object(demo, "diagnostics", side_effect=lab.LabError("diagnostics failed")), \
             patch.object(demo, "down") as stop, patch("builtins.print"):
            with self.assertRaisesRegex(lab.LabError, "actor failed.*diagnostics failed"):
                demo.run_operation("test")
        stop.assert_called_once()
        self.assertEqual(saved[-1]["primary_error"], "actor failed")
        self.assertEqual(saved[-1]["cleanup_errors"], ["diagnostics failed"])

    def test_container_diagnostics_keep_ps_and_every_bounded_log_including_empty_state(self):
        for identities in ([], ["app", "traefik", "lgtm"], [f"container-{index}" for index in range(10)]):
            with self.subTest(containers=len(identities)):
                observed = {}
                logs = {identity: {"stdout": identity + " output", "stderr": identity + " errors", "exit_code": index % 2}
                        for index, identity in enumerate(identities[:8])}
                ps = {"stdout": "container listing", "stderr": "listing errors", "exit_code": 0}
                processes = [CompletedProcess([], ps["exit_code"], ps["stdout"], ps["stderr"])]
                processes.extend(CompletedProcess([], item["exit_code"], item["stdout"], item["stderr"]) for item in logs.values())

                def execute_operator(directory, name, script, *, timeout):
                    self.assertEqual((directory, name, timeout), (self.directory, "failure-containers", 100))
                    output = StringIO()
                    with redirect_stdout(output):
                        exec(compile(script, "<failure-containers>", "exec"), {})
                    observed.update(json.loads(output.getvalue()))
                    return observed

                with patch.object(demo.mcp_lab, "diagnostic"), patch.object(demo, "operator", side_effect=execute_operator), \
                     patch.object(demo, "inspect_deployment", return_value={}) as inspect, \
                     patch("shutil.which", return_value="/usr/bin/docker"), \
                     patch("subprocess.check_output", return_value="\n".join(identities)) as list_ids, \
                     patch("subprocess.run", side_effect=processes) as run:
                    demo.diagnostics(self.directory)

                self.assertEqual(observed, {"ps": ps, "containers": logs})
                list_ids.assert_called_once_with(["sudo", "-n", "docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=sshmcp-demo"], text=True)
                self.assertEqual(run.call_args_list, [call(["sudo", "-n", "docker", "ps", "-a", "--no-trunc"], capture_output=True, text=True, timeout=15)] +
                                 [call(["sudo", "-n", "docker", "logs", "--tail", "100", identity], capture_output=True, text=True, timeout=10) for identity in identities[:8]])
                if identities:
                    inspect.assert_called_once_with(self.directory, "diagnostic-inspect")
                else:
                    inspect.assert_not_called()
                self.assertEqual(json.loads((self.directory / "diagnostic-errors.json").read_text()), [])

    def test_explicit_reset_targets_only_target_and_retains_failed_attempt_until_success(self):
        ready, attempt = self.directory / "ready.json", self.directory / "attempt.json"
        lab.write_private(ready, "{}")
        lab.write_private(attempt, "{}")
        with patch.object(demo, "READY", ready), patch.object(demo, "ATTEMPT", attempt), \
             patch.object(demo, "tunnel_down"), patch.object(demo.mcp_lab, "profile", return_value=({"target": True}, {"state": "Stopped"})) as profile, \
             patch.object(lab, "reset", side_effect=lab.LabError("restore failed")) as restore, patch.object(demo.mcp_lab, "down") as control_stop:
            with self.assertRaisesRegex(lab.LabError, "restore failed"):
                demo.reset_demo(self.directory)
            self.assertFalse(ready.exists())
            self.assertTrue(attempt.exists())
            restore.side_effect = None
            demo.reset_demo(self.directory)
            self.assertFalse(attempt.exists())
            self.assertEqual(profile.call_args.args, ("target",))
            control_stop.assert_not_called()

    def test_up_without_a_ready_demo_does_not_start_vms_or_write_application(self):
        with patch.object(demo, "READY", self.directory / "absent.json"), patch.object(demo.mcp_lab, "up") as up:
            with self.assertRaisesRegex(lab.LabError, "only resumes"):
                demo.resume_demo(self.directory)
            up.assert_not_called()

    def test_forwarding_symlink_is_rejected_before_any_ssh_operation(self):
        socket = self.directory / "socket"
        socket.symlink_to(self.directory / "other")
        with patch.object(demo, "TUNNEL", socket), patch.object(lab, "run") as run:
            with self.assertRaisesRegex(lab.LabError, "symlink"):
                demo.tunnel_down()
            run.assert_not_called()

    def test_public_source_export_omits_private_env_and_redacts_actual_credentials(self):
        secret = "private-password-123456"
        files = [{"path": ".env", "body": base64.b64encode(("PASSWORD=" + secret).encode()).decode(), "sha256": "env-hash"},
                 {"path": "main.go", "body": base64.b64encode(("// " + secret + "\n").encode()).decode(), "sha256": "source-hash"}]
        containers = {"app": {"Config": {"Env": [], "Labels": {"com.docker.compose.project.working_dir": "/home/mcp-test/chosen"}}},
                      "lgtm": {"Config": {"Env": ["GF_SECURITY_ADMIN_PASSWORD=" + secret], "Labels": {"com.docker.compose.project.working_dir": "/home/mcp-test/chosen"}}}}
        with patch.object(demo, "operator", return_value={"files": files}):
            manifest = demo.export_sources(self.directory, containers)
        self.assertFalse((self.directory / "export/source/.env").exists())
        self.assertNotIn(secret, (self.directory / "export/source/main.go").read_text())
        self.assertTrue(manifest[0]["redacted"])


class SharedMCPClientTest(unittest.TestCase):
    def test_wait_drains_both_byte_cursors_after_terminal_state(self):
        client = object.__new__(MCPClient)
        calls = []
        states = [
            {"job": {"state": "completed"}, "output": {"stdout": {"encoding": "base64", "base64": "ww==", "next_cursor": 1, "eof": False, "final": True},
                                                        "stderr": {"encoding": "utf-8", "text": "e", "next_cursor": 1, "eof": True, "final": True}}},
            {"job": {"state": "completed"}, "output": {"stdout": {"encoding": "base64", "base64": "qQ==", "next_cursor": 2, "eof": True, "final": True},
                                                        "stderr": {"encoding": "utf-8", "text": "", "next_cursor": 1, "eof": True, "final": True}}},
        ]
        client.tool = lambda name, **args: calls.append(args) or states.pop(0)
        with patch("mcp_client.time.sleep"):
            _, stdout, stderr = client.wait("job")
        self.assertEqual(stdout, "é".encode())
        self.assertEqual(stderr, b"e")
        self.assertEqual((calls[1]["stdout_cursor"], calls[1]["stderr_cursor"]), (1, 1))
        self.assertTrue(all(0 <= args["wait_ms"] <= 1000 for args in calls))
        self.assertEqual(calls[0]["request_deadline"], calls[1]["request_deadline"])

    def test_expired_outer_deadline_does_not_dispatch_another_status(self):
        client = object.__new__(MCPClient)
        with patch.object(client, "tool", create=True) as tool, self.assertRaisesRegex(lab.LabError, "deadline"):
            client.wait("job", timeout=0)
        tool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
