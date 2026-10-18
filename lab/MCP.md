# Private authenticated MCP (checkpoint 5)

The control VM runs the Go gateway, Keycloak, and Postgres. The target VM runs
only SSH, systemd, and the native runner until a later application exercise.
The gateway uses the official Go MCP SDK v1.8.0, coreos/go-oidc v3.21.0, and
golang.org/x/crypto v0.58.0. It uses stateless Streamable HTTP, including protocol
2026-07-28. Application connection/job IDs belong to the target runner and survive
HTTP client changes and gateway restarts.

```sh
# Once, if the existing CP1/CP4 owned profiles are not enrolled yet:
make lab-up
make target-up

make mcp-lab-up
make mcp-lab-status
make mcp-lab-login
python3 tools/mcp_lab.py codex -- exec 'Use the sshmcp-private tools to inspect the target.'
make mcp-lab-down
```

`up` requires both existing enrolled CP1/CP4 profiles and their recorded ready
baselines, then serially starts/checks them under the existing lifecycle
lock, preserves their original baselines, compares/installs the current target
runner, builds/copies the gateway, and starts the pinned authorization services.
It generates a separate gateway SSH private key **on the control VM**. Only that
key's public half is enrolled on the target, with `restrict` and a forced runner
RPC command. The Mac's management keys remain separate. Every connection pins the
target's enrolled Ed25519 host key; no agent forwarding, PTY, or arbitrary host
input is available.

Two SSH tunnels bind `127.0.0.1:18080` and `127.0.0.1:18081` on the Mac to those
same loopback ports on the control VM. The gateway serves a narrow authorization
resource guard on 18081, forwarding only to fixed Keycloak loopback port 18082.
That upstream port is never forwarded to the Mac. The tunnels explicitly override the management
configuration's `ClearAllForwardings yes`, require `ExitOnForwardFailure`, and use
the private `.lab/mcp/tunnel.sock` master. Shutdown addresses that exact socket;
it never guesses/kills a PID. No listener or public endpoint is exposed on the
VM's NAT interface. There is no public domain, public HTTPS service, or cloud
ChatGPT connection in this checkpoint.

`up` verifies discovery, then uses a real owned-account Authorization Code + PKCE
login to verify the authenticated three-tool catalog. It leaves a successful lab
running for client use. A failed setup collects private diagnostics and attempts
to stop both ownership-verified VMs. `down` stops the two VMs without deleting
their disks or the Postgres volume. `status` inspects existing infrastructure and
discovery; it never starts stopped VMs or installs a new runner/gateway.

## One-time local client authorization

The public pre-registered client is `sshmcp-codex`, with only the exact callback
`http://127.0.0.1:18443/callback`, Authorization Code, and mandatory S256 PKCE.
Password grant, implicit grant, service accounts, and realm self-registration
are disabled for this setup. The callback listener port is explicitly 18443.
Issuer identification is advertised and the callback's `iss` is checked.

`make mcp-lab-login` invokes the installed Codex CLI's normal no-browser login.
Open its authorization URL, sign in to the owned private Keycloak account, and
paste the full callback URL as prompted. Initial lab-account credentials are in
the private `.lab/mcp/accounts.json`; do not paste that file into chat or Git.
The opt-in helper in `tools/lab_oauth.py` can automate this same normal form for
the lab's generated accounts. It does not use a password grant.

The wrapper supplies only this server's configuration as command-line overrides.
It does not rewrite the global model, provider, or other MCP servers. Codex stores
OAuth credentials using its existing configured credential store. Subsequent
wrapper invocations reuse/refresh them; no bearer token is placed in a command
argument or committed configuration. `.lab/mcp/codex-overrides.json` records the
private overrides for inspection.

For this explicitly authorized administrative lab, the wrapper also supplies
`enabled_tools=["connect","run_command","disconnect"]` and invocation-only
`mcp_servers.sshmcp-private.tools.<tool>.approval_mode="approve"` for each of
those three tools. This lets a noninteractive client execute the authorized
remote work. It does not change global approvals or mislabel command execution
as read-only; the allowlist excludes any other tool.

Codex 0.162.1 adds `resource` automatically from protected-resource metadata.
Setting its explicit `oauth_resource` as well sends a duplicated authorization
parameter, which pinned Keycloak rejects. The wrapper therefore uses the exact
discovered resource. The resource remains enforced by both the authorization
server and gateway. Failed/successful automated login transcripts stay private.

Keycloak resource indicators are experimental, so the ARM64 images are pinned
to Keycloak 26.8.1 and Postgres 17.11 by digest. The resource server client's ID
and `resource_url` both equal `http://127.0.0.1:18080/mcp`; the Codex RP's Included Client Audience
targets that resource client. The explicit subject mapper supplies access-token
`sub` (Keycloak no longer adds it automatically when default scopes are omitted).
A narrow bootstrap-admin Authorization Code + PKCE migration verifies/adds that
mapper and the standard optional offline-refresh scope/role for the two owned
accounts in an already-created realm. It checks existing user IDs before and
after and retains the database, users, logins, issuer keys, and client identity.
Initial imports are skipped by Keycloak once the realm exists.

The pinned experimental Keycloak implementation ignores `resource` on refresh
grants. Its normal Included Client Audience is used instead, so the canonical
URI client ID keeps the exact audience stable. The fixed private resource guard
rejects unknown, duplicate, conflicting, or missing authorization-code resource
parameters before forwarding a token request. An omitted refresh resource keeps
the originally authorized audience; an unknown refresh resource is rejected.
Forms, request bodies, concurrency, and upstream HTTP waits are bounded. Account
login, PKCE, grant validation, signatures, and token minting stay in Keycloak.
The guard does not log passwords, forms, codes, or tokens.

Every `/mcp` HTTP request is validated for RS256 signature using discovered JWKS,
exact issuer, exact resource audience, expiration, nonempty subject, and scope
`sshmcp:admin`. The local CLI additionally requests its allowed optional
`offline_access` scope so later clients can refresh stored credentials.
Discovery/JWKS HTTP fetches have an eight-second timeout. Owner
identity is the verified `(issuer, subject)` pair; tool inputs have no owner field.
Protected-resource metadata and issuer discovery remain public for OAuth
bootstrap. Other users cannot inspect/cancel jobs or close another user's shell.

## Exactly three tools

| Tool | Input and result |
| --- | --- |
| `connect` | `host_alias="lab-target"` only. Returns `connection.connection_id` for a new independent persistent Bash shell. |
| `run_command` | `action="execute"`, `"status"`, `"lookup"`, or `"cancel"`, described below. |
| `disconnect` | `connection_id`. Closes the managed shell and cancels its active work. Retained job results remain queryable by its owner. |

Execute requires `connection_id`, a caller-chosen `idempotency_key`, and exactly
one of `command` (Bash source) or `commands` (at most 64 source strings joined by
newlines into one serial Bash job). A batch shares shell state; its last command
determines exit status, using ordinary Bash semantics. Use explicit `set -e` or
`&&` in the source when desired. Total source is at most 1 MiB, without NUL.
`timeout_ms` is optional, from 1 to 3,600,000; its default is one hour.

MCP HTTP, SSH RPC stdin, and both runner/worker Unix RPC decoders share a finite
6,422,528-byte request budget: six times the source limit plus 128 KiB for
metadata and the protocol envelope. This permits JSON's worst-case escaping of
valid source without lowering the 1 MiB source limit. Internal RPC responses
have a separate 2 MiB cap and omit command source; the durable job retains the
full source for execution and exact idempotency comparisons.

Execute returns the durable job acknowledgement promptly. It does not hold SSH
open for the command's lifetime. Save the returned `job.job_id`. Without an
explicit wait, the first response may report `accepted` or `running` even for a
quick command. One active job is allowed per connection and 16 across the
target; use independent connections for parallelism.

Optional `wait_ms` is 0–10,000 milliseconds, default zero, for execute and job
status only. Execute waits for a terminal result or that bound and includes any
retrieved bounded output snapshot from byte zero; a quick command can return its completed
result and output in one tool call. Execute with a wait may also specify `limit`
(default 8,192, maximum 16,384 bytes per stream). Save the returned cursors to
continue with status. A status wait returns as soon as output is available after
the supplied cursors, the job is terminal, or the bound expires. A quiet timeout
returns the latest known state and bounded output; it does not cancel the job.
If the bound expires before the first status read completes, execute can return
only its durable acknowledgement; retrieve output with the returned job ID.
Connection status, lookup, and cancel do not accept a wait.

Waiting happens in the gateway through short read-only RPCs, releasing SSH slots
between reads. Six ordinary SSH channels can therefore serve sixteen concurrent
waited executions. Commands are submitted once; waiting never resends source.
The wait observes caller cancellation and the existing 30-second RPC budget.
After a confirmed execution acknowledgement, a failed optional status read keeps
the known job ID/state and adds `wait_error.code="status_unavailable"`. Inspect
that job with status; do not resubmit it to obtain output.

`status` accepts either `job_id` (state plus incremental stdout/stderr) or
`connection_id` (shell state and active job ID), never both. Job output accepts
nonnegative `stdout_cursor`, `stderr_cursor`, and `limit` (default 8,192, maximum
16,384 **bytes per stream**). For each stream it returns `next_cursor`, `eof`,
`final`, and either readable `encoding="utf-8", text` or lossless
`encoding="base64", base64`. Binary, control characters, and partial UTF-8 at a
byte boundary use base64. Advance cursors by returned byte positions, never text
character counts. `eof` means caught up with the current file; `final` means that
stream is closed. Drain both to EOF/final after a terminal job state.

`lookup` accepts only the original `idempotency_key` and returns the retained
job state/ID. `cancel` accepts only `job_id` and uses independent SSH control
capacity. It acknowledges promptly; poll until terminal. Cancellation first
attempts SIGINT, then kills a noncooperating process group after the runner grace
period. Cancellation conservatively closes the persistent shell and loses its
state in all cases; open a new connection for subsequent work.

Job states are `accepted`, `running`, `completed`, `failed`, `cancelled`, and
`lost`, with exit code/reason where available. Connection states are `active`,
`closed`, and `lost`. Responses omit the original command and owner claims;
large scripts are not echoed on every poll. Tool errors carry a stable `code`
and actionable text: examples include `connection_busy`, `capacity`,
`idempotency_conflict`, `shell_lost`, and `acknowledgement_uncertain`.

`reason="shell_completion_missing"` means Bash exited, including an explicit
`exit` or `set -e` failure, or its completion descriptor closed before a reliable
command result. Such a job is `lost` with no command exit code, including for
`exit 0`. Malformed or oversized completion data and control read failures
report `shell_control_corrupted`. Both include a `job.diagnostic` explaining recovery:
inspect output and external effects, open a new connection, and never replay the
lost job. The runner does not disable native Bash options to preserve its shell.

## Idempotency and uncertain acknowledgements

Save an idempotency key **before** submitting execute. Reusing the same key with
the same connection, exact source, and effective timeout returns the same job and
`duplicate=true`. Changing any of those fields returns `idempotency_conflict`.
Wait duration and output limit are retrieval options, outside execution identity;
changing them can retrieve the same job without executing again. Keys are scoped
to the verified owner across connections, not an HTTP session.

If SSH/HTTP drops during execute, the target may already have persisted it. The
gateway never retries a dispatched RPC. Use `lookup` with the **same key** after
target recovery. If no retained job is found, retry execute only with that exact
key and exact request; never invent a new key to resolve uncertainty. Within the
24-hour retention window this either accepts a never-recorded request or returns
the already-recorded job. A `lost` job is terminal and must not be replayed.
After retention expiry, absence no longer proves nonexecution; inspect external
effects before making an explicit new decision.

Jobs outlive SSH and gateway loss. Runner restart or target reboot invalidates
live shells and records unfinished jobs as `lost`; it never resubmits them.
Default abandonment limits remain 15 minutes idle, one hour per command, and
24 hours maximum connection lifetime/result retention, with an 8 MiB per-stream
capture quota and 256 retained records. See [RUNNER.md](RUNNER.md).

The gateway keeps one six-channel ordinary SSH transport and a separate
two-channel control transport, each below OpenSSH's default ten-session limit.
Channels carry only short runner RPCs, including during optional waits. HTTP
requests, bodies, headers, output, concurrency, SSH handshakes, and RPC durations
are bounded. A caller deadline returns promptly and closes only its channel once
established. A pending channel open keeps its bounded SSH slot until cleanup;
it does not dispatch after cancellation. An independent 30-second watchdog starts
after SSH connection acquisition and retires the transport if channel opening,
RPC execution, or cleanup stalls. That can interrupt sibling calls on the same
transport; an ordinary-transport failure does not close the control transport.
Dispatched RPCs are never automatically replayed, including when their
acknowledgements become uncertain.

## Validation boundaries

```sh
go test -race ./...
go vet ./...
python3 -m unittest discover -s tests -v
make mcp-lab-up
python3 tests/integration_mcp.py -v
```

The opt-in MCP suite uses actual generated-account AuthCode/PKCE tokens, the
HTTP gateway, native SSH, and the target runner. It checks resource rejection,
scope/ownership, tool count, shell state/isolation, serial batches, incremental
text/binary output, idempotency/conflicts, 16 jobs, cancel/deadline, gateway
restart, scoped SSH loss, target reboot, no replay, and short abandonment timers.
It also checks waited quick-command output, bounded quiet status waits, cursor
continuation, sixteen overlapping waited executions, and realistic shell-loss
versus malformed-control diagnostics.
Its final cleanup restores the ordinary runner unit, removes only its marker,
disconnects its shells, collects private diagnostics, and stops both VMs.
It never restores a baseline or installs an application/Docker on the target.

This checkpoint's focused CLI proof is OAuth sign-in and use of the three MCP
tools. It is separate from the later fresh-agent deployment exercise and its
independent evaluator. A passing infrastructure fixture is not MCP or agent
acceptance evidence.

Sources: [official Go SDK protocol guide](https://github.com/modelcontextprotocol/go-sdk/blob/v1.8.0/docs/protocol.md),
[Keycloak MCP/resource indicators](https://www.keycloak.org/securing-apps/mcp-authz-server),
[Keycloak subject-mapper migration](https://www.keycloak.org/docs/26.8.1/upgrading/),
[official Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
and [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
