# Native target runner

The CP4 runner executes trusted administrator commands on `sshmcp-target`. It is
an execution foundation for a future gateway; it does not implement MCP, OAuth,
or an authorization boundary against the target account or root. Every request
requires an opaque, nonempty owner `{issuer, subject}`. The runner persists that
pair and checks exact equality on all connection/job access. A future gateway
must obtain it from verified authentication, rather than trusting client fields.

## Reading order

1. `cmd/sshmcp-runner/main.go`: daemon, short RPC CLI, shell worker entry points.
2. `internal/runner/types.go`: requests, durable records, limits and outcomes.
3. `internal/runner/engine.go` and `store.go`: admission, durable acknowledgement,
   ownership, output cursors, retention, startup recovery.
4. `internal/runner/worker.go` and `supervisor.go`: native Bash and systemd units.
5. `internal/runner/engine_test.go` and `tests/integration_runner.py`: focused
   native worker checks and real target checks.

## RPC

The runner daemon has a private Unix socket at
`/run/user/$(id -u)/sshmcp-runner.sock`, mode `0600`. There is no listening TCP
port. `sshmcp-runner rpc` reads one JSON request from stdin, forwards it locally,
prints one JSON response, and exits. This CLI is designed to be invoked over a
pinned SSH connection by the future gateway. It does not require a long-lived
SSH channel to keep a job alive.

Source is limited to 1 MiB. CLI stdin and both daemon/worker Unix request
decoders allow up to 6,422,528 encoded bytes, covering six-byte JSON escaping
plus 128 KiB of metadata. RPC response decoders retain a separate 2 MiB cap.
Responses project job metadata without the source; durable records retain the
complete command for execution and idempotency, including after restart.
The MCP-only `wait_ms` option is handled by the gateway using short read-only
RPCs. It is not a native runner request field: strict native decoders reject it,
so a wait cannot silently become an unbounded target operation. No SSH slot or
engine lock remains held between the gateway's status reads.

Lab readiness uses an internal `health` action through this same private RPC.
It requires the same nonempty owner fields, returns an empty successful response
only after startup recovery and RPC binding, and rejects a fatal or closing
engine. It does not allocate a connection/job, persist anything, or refresh idle
or retention timers. It is an internal service probe, outside the future MCP
tool surface. The lab uses one bounded attempt for status and waits up to 45
seconds for lifecycle recovery; an active systemd service alone is insufficient.

After `make target-up`, create a connection with:

```sh
ssh -F .lab/target/ssh_config sshmcp-target \
  'XDG_RUNTIME_DIR=/run/user/$(id -u) sshmcp-runner rpc' <<'JSON'
{"action":"connect","owner":{"issuer":"local-lab","subject":"administrator"}}
JSON
```

Copy `connection.id` into subsequent requests sent through the same SSH command.
The action contract is:

| Action | Additional request fields | Result |
| --- | --- | --- |
| `connect` | none | A new isolated Bash connection |
| `connection` | `connection_id` | Current connection state |
| `run` | `connection_id`, `idempotency_key`, `command`; optional `timeout_ms` | Durable accepted job; duplicate returns the original job |
| `job` | `job_id` | Current job record |
| `lookup` | `idempotency_key` | Original job after an uncertain acknowledgement |
| `output` | `job_id`; optional `stdout_cursor`, `stderr_cursor`, `limit` | Bounded incremental output |
| `cancel` | `job_id` | Independent cancellation acknowledgement; poll `job` for its final outcome |
| `disconnect` | `connection_id` | Stop that shell's service and mark the connection closed |

For example, the first command can set native shell state:

```json
{"action":"run","owner":{"issuer":"local-lab","subject":"administrator"},"connection_id":"COPY_CONNECTION_ID","idempotency_key":"example-1","command":"cd /tmp; greeting=hello; greet() { printf '%s\\n' \"$greeting\"; }; greet"}
```

A later command `pwd; greet` on the same connection sees `/tmp`, `greeting`, and
`greet`. A different connection starts a different Bash process in the account's
home directory. Commands use native noninteractive Bash semantics and receive
`/dev/null` stdin; there is no PTY or interactive-input protocol.

Idempotency keys are mandatory and scoped to the owner pair across connections.
The normalized request identity is connection, exact command, and effective
command timeout. Repeating that identity returns the original job ID, including
while running or after restart. Reusing a key with different content fails with
`idempotency_conflict`. A lost acknowledgement must be resolved with `lookup` or
a retry of the same key and request. Retrying with a new key can execute twice.

The daemon persists `accepted` before dispatch. A job then becomes `running`,
`completed` (exit zero), `failed` (nonzero exit or enforced limit), `cancelled`,
or `lost`. No startup recovery path resubmits an accepted or running command.
Exit codes are present when Bash reports one reliably. Reasons distinguish
command timeout, output quota, shell failure, daemon recovery, and disconnection.
`shell_completion_missing` means Bash exited or the completion pipe closed
before a valid command result. It covers explicit `exit` (even `exit 0`), an
errexit failure, or closing FD 3 while the shell is alive. EOF and process exit
can arrive in either order, so the reason does not claim which caused the loss.
The exit code remains absent and the connection is lost. Malformed or oversized
records and completion read failures are separately `shell_control_corrupted`;
an empty record is malformed, whereas EOF is not a record. Neither outcome
permits replay. Inspect captured output and external effects before further
action, then open a new connection. The MCP response includes this recovery
guidance.

## Output and supervision

Each connection has a dedicated transient systemd user service. Inside it, one
native Bash process preserves `cd`, ordinary variables, functions and shell
options. One job at a time may use a connection; a busy connection rejects a new
key. Up to 16 accepted/running jobs may execute across different connections.

The completion/control pipe is independent of stdout and stderr. User output
never contains protocol markers. Per-job FIFOs capture separate stdout/stderr
files, so background writers from one command do not contaminate the next job.
Output responses encode bytes as base64, preserving NUL and non-UTF-8 bytes.
Cursors are byte offsets. `limit` is 1–65536 bytes per stream, default 65536;
invalid or out-of-range cursors fail explicitly. An EOF flag means caught up to
that read's captured size; `stdout_final`/`stderr_final` means that stream's
writers have closed and the captured file has been synced. A completed command
can still have background writers, so EOF alone never means permanently final.

Cancellation uses a separate worker control request. It sends SIGINT to the
Bash process group, waits up to 10 seconds, then kills that group; systemd also
cleans remaining processes in that connection's service cgroup. Cancellation
always reports shell state lost: partially interrupted `eval`, traps, and shell
options cannot safely be resumed. Open a new connection afterward. A normal
nonzero command result keeps the shell unless Bash itself exits. Explicit
`exit`, `exec`, errexit failures, descriptor corruption, or fatal traps can lose
it. Commands may alter shell state because this is a trusted administrator
interface, not a root sandbox.

Separate shell units keep jobs alive when a short SSH RPC exits or a future
gateway disconnects. A runner daemon crash/restart or target reboot invalidates
all live shells. Startup stops their recorded units and marks nonterminal jobs
lost without replay. Pending unit stops are durable, so an interrupted cleanup
is retried on startup. A Docker container created in detached mode will belong
to Docker's daemon, outside the shell's service cgroup; disconnecting its shell
does not remove that container. Docker is absent from the target baseline, and
CP4 does not install or validate application containers there.

## Limits and retention

Defaults can be shortened on the daemon command line for focused checks:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--idle-timeout` | `15m` | Close an idle shell; job submission/completion counts as activity |
| `--command-timeout` | `1h` | Maximum command duration; a request may specify a shorter timeout |
| `--max-lifetime` | `24h` | Maximum connection age, including active work |
| `--result-ttl` | `24h` | Retain terminal jobs, output, and idempotency keys after completion |
| `--cancel-grace` | `10s` | SIGINT grace before forced process-group termination |
| `--output-limit` | `8388608` | Captured bytes per stdout/stderr stream |
| `--max-records` | `256` | Retained job and connection quotas; new records fail when full |

The default captured-output budget is at most 256 × 2 × 8 MiB (4 GiB), plus
bounded request metadata and 64 KiB diagnostics per connection. Exceeding a
foreground stream quota fails the job with `output_limit` and loses its shell.
Late background output is capped too. Commands themselves can create files as
an administrator; these quotas cover runner-owned captures, not arbitrary guest
filesystem writes. Retention expiry removes records and output and permits key
reuse. Before expiry, quotas fail admission rather than evicting promised
idempotency records. Configuration should keep result retention at least as long
as connection lifetime when long-lived background writers are used.

State is an atomically replaced, fsynced private JSON database plus private job
files under `~/.sshmcp/`. A file lock prevents two daemons using the same store.
A malformed store or persistence error fails closed. The target is a single
administrator-owned local filesystem; host/root tampering and power-loss
filesystem guarantees are outside the application trust boundary.

## Validation

`go test -race ./...` runs focused behavior tests using the real worker and
native Bash with a local process launcher. They cover persistence/isolation,
separate binary output and cursors, failure/state loss, required ownership,
idempotency, 16-job admission, independent cancellation, deadlines, output quota,
background streams, retained recovery without replay, idle/lifetime limits,
retention, and short Unix RPC clients. `go vet ./...` and a Linux ARM64 build
complete local validation. A separate readiness check covers an absent RPC,
required ownership, fatal engine refusal, and no durable record allocation.
Local socket tests require the host to permit Unix
socket binding.

`python3 tests/integration_runner.py -v` separately exercises the current build
on Ubuntu/systemd over real pinned SSH. It covers persistent state and separate
output, 16 concurrent jobs, busy/capacity/dedup/conflict outcomes, independent
cancellation and command deadlines, SSH-client exit, abrupt daemon crash and
automatic restart, and an actual target reboot. It checks retained lookup and
no replay after both failures, keeps target ownership/pin/baseline, preserves
control VM records, removes its own markers, and stops the target afterward.
MCP, OAuth, gateway behavior, detached Docker survival, and agent acceptance
remain later-checkpoint validation.
