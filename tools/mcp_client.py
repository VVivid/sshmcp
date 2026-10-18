"""Small real OAuth/MCP client shared by the opt-in lab exercises.

There are deliberately no automatic command retries. An uncertain execute must
be resolved by its saved idempotency key, never by submitting a new command.
"""

import base64
import itertools
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid

import lab
import lab_oauth
import mcp_lab


TERMINAL = {"completed", "failed", "cancelled", "lost"}


def stream_bytes(stream):
    return stream["text"].encode() if stream["encoding"] == "utf-8" else base64.b64decode(stream["base64"])


class MCPClient:
    def __init__(self, account="admin", scope="sshmcp:admin", *, transcript=None, request_timeout=40):
        self.token = lab_oauth.tokens(account, scope)
        self.expires = time.monotonic() + self.token["expires_in"]
        self.ids = itertools.count(1)
        self.refresh_lock = threading.Lock()
        self.record_lock = threading.Lock()
        self.transcript = transcript
        self.request_timeout = request_timeout
        if transcript is not None:
            lab.write_private(transcript, "")

    def record(self, value):
        if self.transcript is not None:
            with self.record_lock:
                descriptor = os.open(self.transcript, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "a") as output:
                    output.write(json.dumps(value) + "\n")
                    output.flush()
                    os.fsync(output.fileno())

    def request(self, method, params=None, token=None, *, deadline=None):
        started = time.monotonic()
        if deadline is not None:
            deadline = min(deadline, started + self.request_timeout)
        identity = next(self.ids)
        # Record before dispatch, including a caller's idempotency key. OAuth
        # headers and refresh credentials never enter this tool transcript.
        self.record({"event": "request", "id": identity, "method": method, "params": params, "time_ns": time.time_ns()})

        def remaining_timeout(maximum):
            remaining = maximum if deadline is None else min(maximum, deadline - time.monotonic())
            if remaining <= 0:
                raise lab.LabError("MCP read deadline expired; execution was not replayed.")
            return remaining

        try:
            # Refresh and dispatch share the caller's budget, including time
            # spent waiting for another request to refresh this client's token.
            acquired = (self.refresh_lock.acquire() if deadline is None else
                        self.refresh_lock.acquire(timeout=remaining_timeout(self.request_timeout)))
            if not acquired:
                raise lab.LabError("MCP read deadline expired waiting for OAuth refresh; execution was not replayed.")
            try:
                if token is None and time.monotonic() > self.expires - 5:
                    result = lab_oauth.token_request({"grant_type": "refresh_token", "refresh_token": self.token["refresh_token"], "resource": mcp_lab.RESOURCE},
                                                    timeout=remaining_timeout(10))
                    if "access_token" not in result:
                        raise AssertionError("OAuth refresh rejected: " + result.get("error", "unknown"))
                    self.token = result
                    self.expires = time.monotonic() + result["expires_in"]
                access_token = self.token["access_token"] if token is None else token
            finally:
                self.refresh_lock.release()
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
            if method == "tools/call":
                headers["Mcp-Name"] = params["name"]
            if token != "":
                headers["Authorization"] = "Bearer " + access_token
            params = dict(params or {})
            params["_meta"] = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}, "io.modelcontextprotocol/clientInfo": {"name": "cp5-integration", "version": "1"}}
            request = urllib.request.Request(mcp_lab.RESOURCE, data=json.dumps({"jsonrpc": "2.0", "id": identity, "method": method, "params": params}).encode(), headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=remaining_timeout(self.request_timeout)) as response:
                    result = json.load(response)
            except urllib.error.HTTPError as error:
                error.close()
                raise
            self.record({"event": "response", "id": identity, "seconds": time.monotonic() - started, "result": result})
            if "error" in result:
                raise AssertionError(result["error"])
            return result["result"]
        except (Exception, KeyboardInterrupt) as error:
            self.record({"event": "error", "id": identity, "seconds": time.monotonic() - started, "error_type": type(error).__name__})
            raise

    def tool(self, name, expected_error=None, *, request_deadline=None, **arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments}, deadline=request_deadline)
        body = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        if expected_error:
            if not result.get("isError") or body.get("code") != expected_error:
                raise AssertionError((expected_error, result))
        elif result.get("isError"):
            raise AssertionError(body)
        return body

    def wait(self, job_id, *, timeout=60):
        deadline = time.monotonic() + timeout
        cursors = {"stdout": 0, "stderr": 0}
        collected = {"stdout": bytearray(), "stderr": bytearray()}
        while time.monotonic() < deadline:
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            result = self.tool("run_command", action="status", job_id=job_id,
                               stdout_cursor=cursors["stdout"], stderr_cursor=cursors["stderr"], limit=16384,
                               wait_ms=min(1000, remaining_ms), request_deadline=deadline)
            for name, output in collected.items():
                stream = result["output"][name]
                output.extend(stream_bytes(stream))
                cursors[name] = stream["next_cursor"]
            if result["job"]["state"] in TERMINAL and all(stream["eof"] and stream["final"] for stream in result["output"].values()):
                return result["job"], bytes(collected["stdout"]), bytes(collected["stderr"])
        raise lab.LabError("MCP job did not finish and drain before the exercise deadline; it was not replayed.")

    def execute(self, connection_id, command, *, timeout=60):
        key = "demo-" + uuid.uuid4().hex
        result = self.tool("run_command", action="execute", connection_id=connection_id,
                           idempotency_key=key, command=command, timeout_ms=int(timeout * 1000))
        return self.wait(result["job"]["job_id"], timeout=timeout + 10)
