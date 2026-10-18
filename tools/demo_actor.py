"""Launch and audit fresh installed-Codex actors with only the private MCP.

Account, model, and credential stores stay in place. Isolation is invocation
local and is verified with the installed CLI's prompt renderer before a model
run. The actor's empty working directory contains no recipes or source files.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time

import lab
import mcp_lab


REPAIR_PROMPT = "Telemetry has stopped arriving; diagnose and repair it."
TOOLS = {"connect", "run_command", "disconnect"}
STREAM_LIMIT = 8 * 1024 * 1024
PROGRESS_INTERVAL = 10
DISABLED_FEATURES = (
    "shell_tool", "apps", "remote_plugin", "computer_use", "browser_use",
    "memories", "multi_agent", "hooks", "view_image", "code_mode",
    "tool_suggest", "workspace_dependencies",
)


def save_json(path, value):
    lab.write_private(path, json.dumps(value, indent=2) + "\n")


def terminate_group(process):
    """Only the child group created by this invocation is addressed."""
    # The CLI may exit while one of its MCP helper descendants still runs.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    # Reap helpers that outlived the CLI, including helpers ignoring SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait(timeout=5)


def capture(command, directory, name, *, cwd, prompt=None, timeout=60, cancel_event=None):
    """Stream private evidence while boundedly owning the entire child group."""
    stdout, stderr = directory / (name + ".jsonl"), directory / (name + ".stderr.log")
    lab.write_private(stdout, "")
    lab.write_private(stderr, "")
    stream_limit = STREAM_LIMIT
    started = time.monotonic()
    process = None
    outcome = {"timeout_seconds": timeout, "started_ns": time.time_ns(), "stream_limit_bytes": stream_limit,
               "output_complete": False, "output_truncated": False,
               "phase": name, "running": False, "event_types": {}, "tool_calls_started": 0, "tool_calls_completed": 0,
               "streams": {name: {"retained_bytes": 0, "observed_bytes": 0, "truncated": False} for name in ("stdout", "stderr")}}
    pending_events = b""
    last_saved, last_announced = started, started - PROGRESS_INTERVAL

    def progress(*, announce=False):
        nonlocal last_saved, last_announced
        now = time.monotonic()
        outcome.update(duration_seconds=now - started, updated_ns=time.time_ns())
        save_json(directory / (name + ".process.json"), outcome)
        last_saved = now
        if announce and now - last_announced >= PROGRESS_INTERVAL:
            print(f"Actor phase={name} elapsed={now - started:.1f}s tools={outcome['tool_calls_completed']}/{outcome['tool_calls_started']} "
                  f"stdout={outcome['streams']['stdout']['retained_bytes']}B stderr={outcome['streams']['stderr']['retained_bytes']}B "
                  f"events={','.join(sorted(outcome['event_types'])) or 'none'}", flush=True)
            last_announced = now

    def observe_events(body):
        nonlocal pending_events
        pending_events += body
        lines = pending_events.split(b"\n")
        pending_events = lines.pop()
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            # Only fixed event labels/counts are printed; never tool source,
            # arguments, output, model text, thread IDs, or credentials.
            kind = event.get("type")
            if kind in {"thread.started", "turn.started", "turn.completed", "turn.failed", "item.started", "item.updated", "item.completed", "error"}:
                outcome["event_types"][kind] = outcome["event_types"].get(kind, 0) + 1
            if (event.get("item") or {}).get("type") == "mcp_tool_call":
                if kind == "item.started":
                    outcome["tool_calls_started"] += 1
                elif kind == "item.completed":
                    outcome["tool_calls_completed"] += 1
    try:
        with stdout.open("wb") as output, stderr.open("wb") as errors, selectors.DefaultSelector() as ready:
            process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            outcome.update(pid=process.pid, running=True)
            progress(announce=True)
            for stream_name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(pipe.fileno(), False)
                ready.register(pipe, selectors.EVENT_READ, stream_name)
            pending_input = memoryview(prompt.encode()) if prompt is not None else memoryview(b"")
            if process.stdin is not None:
                if pending_input:
                    os.set_blocking(process.stdin.fileno(), False)
                    ready.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
            while ready.get_map() or process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    raise lab.LabError("Actor stopped because its independent fault observer failed; private evidence is retained.")
                remaining = started + timeout - time.monotonic()
                if remaining <= 0:
                    raise lab.LabError(f"{name} exceeded its {timeout}s deadline; the attempt is retained.")
                for key, _ in ready.select(min(remaining, .1)):
                    if key.data == "stdin":
                        try:
                            pending_input = pending_input[os.write(key.fd, pending_input[:65536]):]
                        except BrokenPipeError:
                            pending_input = memoryview(b"")
                        if not pending_input:
                            ready.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    stream = outcome["streams"][key.data]
                    capacity = stream_limit - stream["retained_bytes"]
                    body = os.read(key.fd, min(65536, capacity + 1))
                    if not body:
                        ready.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    retained = body[:capacity]
                    destination = output if key.data == "stdout" else errors
                    destination.write(retained)
                    destination.flush()
                    os.fsync(destination.fileno())
                    stream["retained_bytes"] += len(retained)
                    stream["observed_bytes"] += len(body)
                    if key.data == "stdout":
                        observe_events(retained)
                    progress(announce=True)
                    if len(body) > capacity:
                        stream["truncated"] = outcome["output_truncated"] = True
                        raise lab.LabError(f"{name} {key.data} exceeded its {stream_limit}-byte evidence quota; a marked bounded prefix was retained.")
                if time.monotonic() - last_saved >= 1:
                    progress(announce=True)
            outcome["output_complete"] = True
            outcome["exit_code"] = process.returncode
    except (Exception, KeyboardInterrupt) as error:
        outcome["failure"] = lab.error_text(error)
        raise
    finally:
        with lab.interrupt_handling(cleanup=True):
            if process is not None:
                terminate_group(process)
                outcome["exit_code"] = process.returncode
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
            outcome["running"] = False
            progress()
    return outcome


def prompt_text(value):
    """The debug command returns the exact model-visible message list."""
    if not isinstance(value, list):
        raise lab.LabError("Unexpected installed-Codex prompt-renderer format; isolation was not proven.")
    return "\n".join(content.get("text", "") for message in value for content in message.get("content", []) if isinstance(content, dict))


def assert_isolated(value):
    text = prompt_text(value)
    for marker in ("## Skills", "Available skills", "<memory", "MEMORY_SUMMARY", "## Memory", "AGENTS.md instructions for"):
        if marker in text:
            raise lab.LabError("Installed Codex still exposes host skills, memory, or project instructions; actor not launched.")


def catalog_paths(text):
    roots = dict(re.findall(r"^- `([^`]+)` = `([^`]+)`", text, re.M))
    paths = set()
    for path in re.findall(r"\(file: ([^\n)]+/SKILL\.md)\)", text):
        prefix, separator, remainder = path.partition("/")
        if prefix in roots:
            path = roots[prefix] + separator + remainder
        if not Path(path).is_absolute():
            raise lab.LabError("Installed skill catalog contains an unresolved path; actor not launched.")
        paths.add(path)
    return sorted(paths)


def isolated_arguments(directory, workspace):
    # The installed CLI routes direct MCP calls through this transport host even
    # when the model-visible Code Mode feature is disabled.
    options = ["--enable", "code_mode_host", "-c", "mcp_servers." + mcp_lab.SERVER_NAME + ".required=true"]
    for feature in DISABLED_FEATURES:
        options.extend(["--disable", feature])
    for key, value in {
        "agents.enabled": False, "memories.use_memories": False,
        "memories.generate_memories": False, "project_doc_max_bytes": 0,
        "web_search": "disabled", "notify": [],
    }.items():
        options.extend(["-c", key + "=" + json.dumps(value)])
    options.extend(["-C", str(workspace)])
    base = mcp_lab.codex_arguments(*options)
    # Discover configured and plugin-provided servers, including future ones.
    outcome = capture(base + ["mcp", "list", "--json"], directory, "mcp-inventory", cwd=workspace)
    if outcome["exit_code"]:
        raise lab.LabError("Installed-Codex server inventory failed; actor not launched.")
    servers = json.loads((directory / "mcp-inventory.jsonl").read_text())
    disabled = []
    for server in servers:
        name = server["name"]
        if name != mcp_lab.SERVER_NAME:
            # The installed CLI's dotted override parser treats quote marks as
            # literal key characters. Refuse names it cannot safely address.
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise lab.LabError("An installed MCP name cannot be safely disabled by this CLI; actor not launched.")
            # Plugin-only names are not necessarily tables in config.toml. The
            # CLI validates even disabled entries, so retain the discovered
            # transport discriminator rather than creating an invalid table.
            transport = server.get("transport", {})
            field = "command" if transport.get("command") else "url" if transport.get("url") else None
            if field is None:
                raise lab.LabError("An installed MCP transport cannot be safely disabled; actor not launched.")
            options.extend(["-c", "mcp_servers." + name + "." + field + "=" + json.dumps(transport[field])])
            options.extend(["-c", "mcp_servers." + name + ".enabled=false"])
            disabled.append(name)
    base = mcp_lab.codex_arguments(*options)
    outcome = capture(base + ["mcp", "list", "--json"], directory, "isolated-mcp-inventory", cwd=workspace)
    if outcome["exit_code"]:
        raise lab.LabError("Installed-Codex isolated server inventory failed; actor not launched.")
    isolated = json.loads((directory / "isolated-mcp-inventory.jsonl").read_text())
    if {server["name"] for server in isolated if server["enabled"]} != {mcp_lab.SERVER_NAME}:
        raise lab.LabError("Installed Codex did not isolate the private MCP server map; actor not launched.")
    # Read the actual catalog rather than baking in today's installed skills.
    outcome = capture(base + ["debug", "prompt-input", "isolation probe"], directory, "catalog-before", cwd=workspace)
    if outcome["exit_code"]:
        raise lab.LabError("Installed-Codex catalog probe failed; actor not launched.")
    text = prompt_text(json.loads((directory / "catalog-before.jsonl").read_text()))
    paths = catalog_paths(text)
    if "Available skills" in text and not paths:
        raise lab.LabError("Installed skill catalog format changed; actor not launched.")
    # Codex 0.162.1 matches exact SKILL.md paths despite the folder wording in
    # the online configuration reference. The second probe verifies the result.
    entries = ",".join("{path=" + json.dumps(path) + ",enabled=false}" for path in paths)
    options.extend(["-c", "skills.config=[" + entries + "]"])
    save_json(directory / "isolation.json", {"allowed_servers": [mcp_lab.SERVER_NAME], "disabled_servers": disabled,
                                               "disabled_skills": paths, "disabled_features": DISABLED_FEATURES,
                                               "required_mcp": True, "code_mode_host": True, "model_code_mode": False})
    return mcp_lab.codex_arguments(*options)


def preflight(directory, workspace):
    """Read-only local/authenticated checks; no model call or account changes."""
    from mcp_client import MCPClient
    record = {"success": False, "quota": "unknown", "quota_guarantee": False, "required_mcp": False}
    phase = "launcher"
    try:
        executable = shutil.which("codex")
        if executable is None:
            raise lab.LabError("Installed Codex CLI was not found; actor not launched.")
        version = capture([executable, "--version"], directory, "cli-version", cwd=workspace)
        text = (directory / "cli-version.jsonl").read_text().strip()
        match = re.fullmatch(r"codex-cli (\d+)\.(\d+)\.(\d+)(?:[-+][\w.-]+)?", text)
        if version["exit_code"] or match is None or tuple(map(int, match.groups())) < (0, 162, 1):
            raise lab.LabError("Installed Codex version is unsupported by the verified actor isolation/required-MCP contract.")
        record.update(cli_version=text, required_mcp=True)
        phase = "authentication"
        status = capture([executable, "login", "status"], directory, "cli-auth", cwd=workspace)
        if status["exit_code"]:
            raise lab.LabError("Installed Codex authentication is unavailable; actor not launched.")
        phase = "mcp_catalog"
        client = MCPClient(transcript=directory / "preflight.mcp.jsonl", request_timeout=15)
        catalog = client.request("tools/list")
        save_json(directory / "authenticated-catalog.json", catalog)
        names = [tool["name"] for tool in catalog.get("tools", [])]
        if len(names) != 3 or set(names) != TOOLS:
            raise lab.LabError("Authenticated MCP catalog must contain exactly connect, run_command, and disconnect.")
        record.update(success=True, cli_authenticated=True, authenticated_tools=sorted(names))
    except (Exception, KeyboardInterrupt) as error:
        record.update(failure_category=phase, error_type=type(error).__name__)
        raise
    finally:
        save_json(directory / "preflight.json", record)
    return record


def failure_category(directory, fallback):
    """Classify retained private errors without disclosing their text."""
    text = ""
    stderr = directory / "actor.stderr.log"
    if stderr.exists():
        with stderr.open("rb") as source:
            text = source.read(STREAM_LIMIT).decode(errors="replace").lower()
    transcript = directory / "actor.jsonl"
    if transcript.exists():
        try:
            for event in transcript_events(transcript):
                if event.get("type") in {"error", "turn.failed"}:
                    text += json.dumps(event).lower()
                item = event.get("item") or {}
                if item.get("type") == "error":
                    text += json.dumps(item).lower()
                elif item.get("type") == "mcp_tool_call" and item.get("error"):
                    text += json.dumps(item["error"]).lower()
        except (ValueError, lab.LabError):
            # An incomplete/oversized failure transcript stays failed, even
            # when it cannot provide a more specific diagnostic category.
            pass
    if re.search(r"usage.limit|quota.exceeded|insufficient.quota|rate.limit|usage_limit_reached", text):
        return "quota"
    if re.search(r"unauthorized|not logged in|authentication.failed|invalid.auth|token.expired|status.?401", text):
        return "authentication"
    if re.search(r"transport.error|mcp.*(?:failed|unavailable)|tool.call.failed", text):
        return "tool_transport"
    return fallback


def tool_body(result):
    if not result:
        return {}
    body = result.get("structured_content") or result.get("structuredContent")
    if body is not None:
        return body
    texts = [entry["text"] for entry in result.get("content", []) if entry.get("type") == "text"]
    return json.loads(texts[0]) if texts else {}


def audit_transcript(path):
    """Require actual completed tool results and reject every host action."""
    calls, active, completed_jobs, submissions = [], set(), set(), {}
    counts = {"tool_errors": 0, "runtime_errors": 0, "failed_jobs": 0, "duplicate_results": 0, "repeated_execute_keys": 0,
              "repeated_source_submissions": 0, "closed_disconnects": 0, "lost_disconnects": 0}
    feedback, turn_completed, thread_ids = [], False, set()
    completed_ids, started_ids, failed_jobs = set(), set(), set()
    for event in transcript_events(path):
        if event.get("type") == "thread.started":
            thread_ids.add(event["thread_id"])
        if event.get("type") == "turn.failed":
            raise lab.LabError("Actor reported a failed turn; inspect its private transcript.")
        if event.get("type") == "error":
            counts["runtime_errors"] += 1
        if event.get("type") == "turn.completed":
            turn_completed = True
        item = event.get("item")
        if not item:
            continue
        kind = item.get("type")
        if kind == "error":
            # CLI startup/configuration diagnostics are not model tool actions.
            # Actual completed private MCP work is still required below.
            counts["runtime_errors"] += int(event["type"] == "item.completed")
            continue
        if kind not in {"agent_message", "reasoning", "mcp_tool_call"}:
            raise lab.LabError(f"Actor attempted a disallowed host action ({kind}); attempt rejected.")
        if kind != "mcp_tool_call":
            if kind == "agent_message" and event["type"] == "item.completed":
                feedback.append(item.get("text", ""))
            continue
        if item.get("server") != mcp_lab.SERVER_NAME or item.get("tool") not in TOOLS:
            raise lab.LabError("Actor used a tool outside the private three-tool allowlist.")
        if event["type"] == "item.started":
            started_ids.add(item["id"])
        if event["type"] != "item.completed":
            continue
        if item["id"] in completed_ids:
            raise lab.LabError("Actor transcript repeated a completed item; audit is ambiguous.")
        completed_ids.add(item["id"])
        calls.append(item)
        tool, arguments = item["tool"], item.get("arguments", {})
        # Count dispatch attempts even when the acknowledgement was lost. A
        # same-key retry can be safe deduplication, but it is still a retry.
        if tool == "run_command" and arguments.get("action") == "execute":
            key = arguments.get("idempotency_key")
            counts["repeated_execute_keys"] += int(key in submissions)
            source = arguments.get("command", arguments.get("commands"))
            digest = hashlib.sha256(json.dumps(source).encode()).hexdigest()
            counts["repeated_source_submissions"] += int(digest in submissions.values())
            submissions[key] = digest
        body = tool_body(item.get("result"))
        result = item.get("result") or {}
        if item.get("error") or item.get("status") != "completed" or body.get("code") or result.get("isError") or result.get("is_error"):
            counts["tool_errors"] += 1
            continue
        if tool == "connect":
            if body.get("connection", {}).get("state") != "active":
                raise lab.LabError("Actor connect has no active result.")
            active.add(body["connection"]["connection_id"])
        elif tool == "disconnect":
            connection = body.get("connection", {})
            state = connection.get("state")
            # A successful disconnect preserves an already-cleaned lost shell's
            # terminal state. It must still identify this call's idle shell.
            if connection.get("connection_id") != arguments.get("connection_id") or state not in {"closed", "lost"} or connection.get("busy_job_id"):
                raise lab.LabError("Actor disconnect has no matching idle terminal result.")
            counts[state + "_disconnects"] += 1
            active.discard(connection["connection_id"])
        else:
            job = body.get("job", {})
            if arguments.get("action") == "execute":
                counts["duplicate_results"] += int(body.get("duplicate", False))
            if job.get("state") == "completed" and job.get("exit_code") == 0:
                completed_jobs.add(job["job_id"])
            elif job.get("state") in {"failed", "cancelled", "lost"}:
                failed_jobs.add(job["job_id"])
    counts["failed_jobs"] = len(failed_jobs)
    if not turn_completed or len(thread_ids) != 1 or not calls or not submissions or not completed_jobs or active or started_ids - completed_ids:
        raise lab.LabError(f"Actor lacks a fresh completed session, completed remote work, or closed connections; {counts['runtime_errors']} runtime errors recorded; exit status alone is insufficient.")
    return {"thread_id": next(iter(thread_ids)), "tool_calls": len(calls), "completed_jobs": len(completed_jobs),
            "submissions": len(submissions), "human_interventions": 0, "feedback": feedback, **counts}


def transcript_events(path):
    """Audit incrementally, including files supplied outside capture()."""
    consumed = 0
    with path.open("rb") as source:
        while line := source.readline(STREAM_LIMIT - consumed + 1):
            consumed += len(line)
            if consumed > STREAM_LIMIT:
                raise lab.LabError("Actor transcript exceeded its byte quota; a prefix cannot pass acceptance.")
            yield json.loads(line)


def run_actor(directory, name, prompt, *, timeout=1800, cancel_event=None):
    actor = directory / name
    actor.mkdir(mode=0o700)
    workspace = new_workspace(actor)
    lab.write_private(actor / "prompt.txt", prompt)
    outcome = {"success": False, "quota": "unknown", "human_interventions": 0}
    phase = "preflight"
    try:
        preflight(actor, workspace)
        phase = "isolation"
        arguments = isolated_arguments(actor, workspace)
        probe = capture(arguments + ["debug", "prompt-input", prompt], actor, "prompt-input", cwd=workspace)
        if probe["exit_code"]:
            raise lab.LabError("Actor prompt rendering failed; no model run was started.")
        assert_isolated(json.loads((actor / "prompt-input.jsonl").read_text()))
        if any(workspace.iterdir()):
            raise lab.LabError("Actor working directory is no longer empty.")
        phase = "launch"
        result = capture(arguments + ["exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never", "-"],
                         actor, "actor", cwd=workspace, prompt=prompt, timeout=timeout, cancel_event=cancel_event)
        phase = "tool_or_actor_contract"
        summary = audit_transcript(actor / "actor.jsonl")
        summary.update(result)
        summary["quota"] = "unknown"
        save_json(actor / "audit.json", summary)
        if result["exit_code"]:
            raise lab.LabError("Actor process failed; inspected tool evidence is retained with its nonzero exit status.")
        outcome["success"] = True
        return summary
    except (Exception, KeyboardInterrupt) as error:
        category = failure_category(actor, phase)
        record = actor / "preflight.json"
        if phase == "preflight" and record.exists():
            category = json.loads(record.read_text()).get("failure_category", category)
        outcome.update(failure_category=category, error_type=type(error).__name__)
        raise
    finally:
        save_json(actor / "outcome.json", outcome)


def new_workspace(directory):
    # A subdirectory of .lab/results would still belong to the repository.
    # Keep the actor outside every checkout, with a fresh private empty root.
    temporary_root = Path("/private/tmp") if Path("/private/tmp").is_dir() else Path("/tmp")
    workspace = Path(tempfile.mkdtemp(prefix="sshmcp-actor-", dir=temporary_root)).resolve()
    workspace.chmod(0o700)
    if workspace.is_relative_to(lab.ROOT.resolve()) or any((parent / ".git").exists() for parent in workspace.parents):
        workspace.rmdir()
        raise lab.LabError("Fresh actor workspace belongs to a repository; actor not launched.")
    save_json(directory / "workspace.json", {"path": str(workspace), "initially_empty": not any(workspace.iterdir()), "outside_repository": True})
    return workspace
