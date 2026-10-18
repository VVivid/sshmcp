# Private SSHMCP tool documentation, version 1

Only `sshmcp-private.connect`, `sshmcp-private.run_command`, and
`sshmcp-private.disconnect` are available for server work.

`connect(host_alias="lab-target")` opens an independent persistent Bash shell.
Save `connection.connection_id`. A connection retains its working directory,
variables, and functions between jobs. Open independent connections for parallel
work; one job may be active per connection, with at most 16 active on the target.

`run_command(action="execute", connection_id=..., idempotency_key=..., command=...)`
submits Bash source and promptly returns `job.job_id`. Alternatively provide
`commands=[...]`: those strings form one serial job sharing shell state. Standard
Bash exit behavior applies; request explicit failure handling when appropriate.
The optional `timeout_ms` is 1 through 3,600,000. Source is bounded to 1 MiB.
Add `wait_ms=1000` (0 through 10,000, default zero) to wait briefly for completion
and retrieve bounded stdout/stderr in the same call. If still running, save the
acknowledged job ID and continue with status. A waited execute may specify
`limit`; its output starts at byte zero. Changing the wait or output limit with
the same execution identity does not execute the command again.

Poll `run_command(action="status", job_id=..., stdout_cursor=..., stderr_cursor=...)`.
The first result can still be `accepted` or `running`. Save each output stream's
`next_cursor` and supply it on the next poll. Cursor values are byte positions.
Output is either UTF-8 `text` or lossless `base64`; decode according to `encoding`.
The per-stream `limit` defaults to 8,192 bytes and is at most 16,384 bytes. Drain
both streams to `eof=true` and `final=true` after a terminal job state. Terminal
states are `completed`, `failed`, `cancelled`, and `lost`; inspect the exit code
and reason. Polling a connection ID instead returns shell/busy state.
Job status also accepts `wait_ms`: it returns on new output after your cursors,
a terminal job state, or the bound, without cancelling work. Waiting releases
SSH capacity between status reads, so sixteen parallel executions remain
supported. `wait_error` means the optional read failed after acknowledgement;
retain that job ID and inspect it with status. Do not resubmit for output.

`reason="shell_completion_missing"` means Bash exited (including `exit 0` or
`set -e`) or its completion descriptor closed before a reliable command result.
The job is lost with an unknown exit code. `shell_control_corrupted` instead
means malformed or oversized completion data, or a control read failure. Read
`job.diagnostic`, inspect captured output and external effects, open a new
connection, and never replay the lost job. Native Bash options remain in effect.

Choose an idempotency key before submitting each new job. If the acknowledgement
is uncertain, use `run_command(action="lookup", idempotency_key=...)`. Retry only
the same key, connection, exact source, and timeout when needed. A repeated
accepted submission returns the same job with `duplicate=true`; changing source
or connection produces `idempotency_conflict`. Do not replay a `lost` job.

`run_command(action="cancel", job_id=...)` uses independent control capacity.
Poll for the terminal result. Cancellation loses that shell's state, so reconnect
for subsequent work. Jobs can outlive SSH or gateway interruption; runner restart
or target reboot marks unfinished work lost without replaying it.

`disconnect(connection_id=...)` closes the shell and cancels its active work.
Close every connection you opened. The deployed containers should continue
independently. Results remain queryable by their authenticated owner.
