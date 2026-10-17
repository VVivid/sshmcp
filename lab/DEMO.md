# Private agent exercise

This lifecycle runs two real, fresh installed-Codex sessions against the three
private MCP tools. The first deploys an observable Go/Traefik/LGTM demo from a
clean target. An independent evaluator then proves its behavior through backend
APIs. A separate operator breaks the application's generic OTLP endpoint while
keeping HTTP healthy. A new troubleshooting session receives exactly:

> Telemetry has stopped arriving; diagnose and repair it.

The evaluator requires new telemetry after repair. A successful exercise leaves
both private VMs and the demo running. Checkpoint 6 implements this machinery;
the actual model-driven deployment and repair acceptance run is checkpoint 7.
No prepared Go app, Docker installation recipe, Compose configuration, or seed
application files are copied to the target or actor workspace by this harness.

Checkpoint 7's sixth fresh deployment attempt passed the complete deployment,
120-second fault, and fresh repair exercise on 2026-10-17. The five earlier
attempts remain failed. The [actual-source snapshot and report](../examples/agent-demo/README.md)
preserve their history, the independent backend results, and the agent-created
files captured after acceptance. Detailed private evidence references remain in
[VALIDATION.md](VALIDATION.md).

## Commands and state

Both original profiles must already have their enrolled ownership, pinned SSH
keys, and ready baselines. See [MCP.md](MCP.md) for one-time stored Codex OAuth
login. The exercise preserves the installed account/model defaults and supplies
only invocation-local isolation and private MCP settings.

```sh
make mcp-lab-up
make agent-demo-test
make agent-demo-down
make agent-demo-up
make agent-demo-reset
```

- `test` starts/checks the existing owned VMs, proves the target has no Docker or
  application artifacts, runs the real MCP benchmark, launches both actors with
  independent evaluation/fault phases, and exports evidence. It never resets a
  target automatically. An actor-attempt intent is saved before the first model
  session; another attempt requires explicit reset. The initial deployment goal
  and tool documentation are versioned in [agent-goal.v1.md](agent-goal.v1.md)
  and [agent-tools.v1.md](agent-tools.v1.md).
- `up` requires a previously evaluated demo-ready record. It starts the same
  disks, checks/refreshes the existing MCP infrastructure, resumes forwarding,
  and independently checks fresh application telemetry. It creates no
  application source, configuration, or deployment. Missing/broken demo files
  produce an error rather than an automatic rebuild.
- `down` closes the exact owned demo and MCP SSH control sockets, then stops
  both ownership-verified VMs without deleting disks or volumes. Repeating it on
  stopped VMs is harmless. No PID search or port-based process killing is used.
- `reset` invalidates the current demo-ready pointer, closes the demo forwarding
  socket, restores only the target's original recorded baseline through the
  existing ownership/pin/mount safeguards, checks the current runner, and stops
  the target. It removes the actor-attempt gate only after successful restore.
  It retains all prior evidence and the control disk, gateway private key,
  accounts, issuer database/keys, and stored Codex OAuth login. The next MCP up
  reenrolls the existing gateway public key on the restored target.

All lifecycle functions run under one workspace lock; they call existing MCP/lab
functions directly rather than launching another lock-owning wrapper. A test/up
failure or handled interruption stops the actor's own process group first, saves
private evidence, collects bounded diagnostics, and attempts to stop both VMs.
Original and cleanup errors are recorded separately. There is no automatic
baseline restore, destructive recovery, or coached continuation of a failed
actor. Forced process/host loss cannot guarantee cleanup.

## Private access

| Mac loopback | Destination |
| --- | --- |
| `18090` | target Traefik `127.0.0.1:8080` |
| `13000` | target Grafana `127.0.0.1:3000` |
| `18080` | control MCP gateway |
| `18081` | control OAuth issuer/resource guard |
| `18443` | exact local OAuth callback |

Keycloak upstream `18082` remains control-only. Demo forwarding uses its own
private `.lab/demo/target-tunnel.sock`; the MCP socket remains
`.lab/mcp/tunnel.sock`. No bridge, public listener, Mac home/repository mount, or
port forward for an application/backend container is added. Scoped read-only
Docker mounts of agent-created Ubuntu configuration are allowed; telemetry/log
data use Docker named volumes.

## What is evaluated

Each successful deployment/repair phase requires an independently generated
request through Traefik returning `Hello, world!`, a fresh W3C trace with actual
Go-server-to-Traefik parent ancestry, and Go OTLP logs filtered by that same trace
ID in Loki. Trace identifiers accept Tempo's hex or base64 OTLP JSON encoding.
Traefik's collected JSON access log must contain the unique request probe header.
Genuine Go and Traefik request counters must increase after a known burst;
histogram `_count` guesses and health-only responses cannot satisfy this check.
The brief requires Prometheus's `type-and-unit-labels` feature. The evaluator
checks its active flags and the backend-derived `__type__="counter"` label on
service-attributed OTLP series, including that type in the sampled selector.
It does not depend on the legacy scrape-only metadata endpoint. The pinned
[Prometheus type-label contract](https://github.com/prometheus/prometheus/blob/v3.15.0/docs/feature_flags.md#type-and-unit-labels)
and [LGTM extra-argument support](https://github.com/grafana/docker-otel-lgtm/blob/v0.37.0/docker/run-prometheus.sh)
define this component constraint; no application or deployment recipe is added.

Grafana datasources are discovered by type rather than assuming their UIDs.
Dashboard checks require request/latency queries, populated application and
proxy log streams, and trace navigation.
Backend dashboard probes render an explicit five-minute time context, including
the common numeric range/interval forms. They also resolve declared textbox
defaults when `current.value` is explicitly the empty string, or an explicit
`${name:raw}` format with a scalar string current value of at most 4 KiB.
Raw values preserve Grafana's verbatim semantics, including regex syntax.
Unknown variables, other formats, nonempty unformatted defaults, and multivalue
defaults fail clearly before backend use.
Backend responses and current deployment/container state are retained. Export
waits are bounded to 120 seconds. The controlled fault is proved over a complete
120-second window with HTTP/proxy telemetry still working, fresh Go traces/logs
absent, and the Go request counter unchanged. Repair uses different request and
trace identifiers and must pass the full positive checks again.

The benchmark warms 16 independent MCP shells, then dispatches 16 real `sleep 2`
jobs concurrently. Every job must finish with its own exact output, all 16 guest
execution intervals must overlap, and host dispatch through final output drain
must finish within eight seconds. Warm-command and raw per-call latency are
recorded. Transport failures are not automatically retried; idempotency keys are
written before dispatch and uncertain acknowledgements remain explicit.

Actors use fresh private temporary directories outside any checkout, fresh
ephemeral sessions, the existing account/model defaults, and only the private
three-tool MCP. The launcher discovers all installed MCP names/transports and
the actual skill catalog, resolves skill-root aliases to exact `SKILL.md` files,
disables host capabilities for the invocation, and renders the final prompt to
prove the catalog/memory/project instructions are absent. A changed/unsupported
isolation format fails before a model run. Completed JSONL tool results are
audited; local command/file/browser actions or unrelated MCPs fail acceptance.
Exit zero or an actor's success statement is insufficient.
The installed CLI's required MCP transport host remains enabled; its
model-visible Code Mode feature stays disabled. CLI diagnostic items are counted
as runtime errors. A successful disconnect must return the requested idle
connection in `closed` or already-cleaned `lost` state; both states are counted,
while an active, mismatched, or busy result cannot satisfy cleanup.
Each actor/probe stdout and stderr stream is capped separately at 8 MiB while
the child runs. Overflow rejects the operation and stops its exact process
group; the private process record marks retained byte prefixes as incomplete.
The transcript audit reads bounded lines incrementally and enforces the total
byte quota again, so a complete-looking prefix of an oversized file cannot pass.

## Evidence and developer checks

Every operation has a unique private `.lab/results/*-demo-*/` directory. Actor
prompts, prompt-isolation probes, streaming JSONL/stdout/stderr, process timing,
tool-call/error/duplicate counts, final feedback, operator inputs/results,
container logs/inspection, raw telemetry API responses, and source originals
remain private. Infrastructure/operator actions never enter actor transcripts.
Human interventions are recorded as zero for the automated flow; a coached or
modified attempt must be retained as failed and restarted after explicit reset.

Target freshness/application inspection, controlled fault injection, container
logs, and source export use separate operator MCP sessions with `connect`,
`run_command`, and `disconnect`. Each operator saves its Python input, a unique
idempotency key before dispatch, raw stdout/stderr, latest accepted or final job state,
and MCP transcript under private `operator/` evidence. An uncertain execution
is never replayed. Output is capped at 8 MiB per stream; exceeding the bound
retains a marked prefix and fails the operation. Operator results do not count
as actor or benchmark evidence. Ownership/provisioning checks, SSH forwarding,
and fixed VM/service failure diagnostics remain management infrastructure.
Those diagnostics can run when MCP is broken; target application operations
have no management-SSH fallback. Container diagnostics retain `docker ps` even
with no demo containers and collect at most eight containers' last 100 log lines.

After success, `export/report.json` is a sanitized summary and `export/source/`
contains actual agent-created source/config with a hash manifest. Private env,
secret, credential, and key files are excluded from this shareable copy, known
runtime credentials are redacted, and original files/hashes stay in private
operator evidence. Docker volumes, logs/data directories, dependency caches,
and compiled binaries are excluded. Export refuses symlinks, unknown paths,
non-UTF-8 source/config, files over 256 KiB, over 128 files, or over 4 MiB total;
it does not silently truncate an archive.

The checkpoint-7 snapshot has 20 primary files from that unchanged
successful export plus the actual `verify.py` collected afterward through a
separate bounded read-only MCP operator. Its combined manifest distinguishes
the two collections. No source was rewritten or supplied to either actor.
The gitignored `.lab/demo/access.json` holds private Grafana access notes with
mode `0600`; it is separate from the shareable source and report.

These focused developer commands do not run deployment/troubleshooting models:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile tools/agent_demo.py tools/demo_actor.py tools/demo_evaluator.py tools/mcp_client.py
python3 tools/agent_demo.py isolation-check
python3 tools/agent_demo.py benchmark
```

The isolation check only renders installed-Codex prompts/inventories. Its local
runtime may require normal host access beyond a restricted filesystem sandbox.
The benchmark starts the existing private VMs, checks a clean target, records
real MCP results/diagnostics, and stops both VMs. It neither restores a baseline
nor installs Docker/app files nor runs an actor. The normal unit suite mocks
external boundaries and never operates VMs, network services, or models.

Read [VALIDATION.md](VALIDATION.md) for checks actually performed and their limits.
Official references: [Codex noninteractive output](https://learn.chatgpt.com/docs/non-interactive-mode),
[Codex settings](https://learn.chatgpt.com/docs/config-file/config-reference),
[LGTM](https://grafana.com/docs/opentelemetry/docker-lgtm/),
[Grafana datasources](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/data_source/),
[Tempo HTTP API](https://grafana.com/docs/tempo/latest/api_docs/),
and [Loki HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/).
