# Lab validation

The opening sections record the original reusable SSH/Docker infrastructure
validation: both checkpoint 3 security cases passed, followed by a successful
repeat `make lab-test`, and the control VM was left stopped. Later sections
record the native runner, private MCP, and actual checkpoint-7 application
exercise. The accepted demo is currently running on its separate target VM.

## Reading order

1. [README.md](../README.md): prerequisites, commands, ownership, and reset effects.
2. [tools/lab.py](../tools/lab.py): lifecycle entry points, identity checks,
   readiness, baseline handling, and bounded cleanup.
3. [cloud-init.v1.yaml](cloud-init.v1.yaml) and [smoke.v1.sh](smoke.v1.sh): installed
   services and the public-image systemd/Compose/HTTP fixture.
4. [test_lab.py](../tests/test_lab.py) and
   [integration_lab.py](../tests/integration_lab.py): focused in-memory guards and
   the separately invoked real-VM cases.

## Reproduce

Run from the repository root, with no other lab operation in progress. Ordinary
test discovery and syntax checks never start a VM:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile tools/lab.py tests/test_lab.py tests/integration_lab.py
bash -n lab/smoke.v1.sh
```

`make lab-up` provisions or starts the owned VM and checks readiness. The new
security cases require that enrolled VM and its ready baseline; they refuse to
create a replacement. Run just those two cases, then the normal smoke workflow:

```sh
python3 tests/integration_lab.py -v \
  RealLabLifecycleTest.test_unapproved_ssh_key_cannot_execute \
  RealLabLifecycleTest.test_down_refuses_owner_and_host_pin_mismatches
make lab-test
make lab-status
```

The expected final VM state is `Stopped` and not ready. The complete five-case
suite is optional when repeating the earlier lifecycle checks is useful:

```sh
python3 tests/integration_lab.py -v
```

That complete suite **restores the recorded baseline and discards later guest
disk changes**, including pulled images. The two selected security cases do not
restore it. Neither invocation deletes a VM, replaces a baseline, or mounts host
files. Temporary keys are generated only inside private ignored `.lab/`
directories and removed after use. Each new case finishes with an
ownership-verified stop using the original trusted state.

## Coverage and evidence

The observed host setup used Canonical's signed and notarized stable Multipass
`1.16.4+mac` with the `qemu` driver. The guest is Ubuntu 24.04 ARM64 with 2 CPUs,
4GB RAM, a 40GB disk, and no host mounts. The fixture runs actual Docker Compose
with the nginx registry digest pinned in `smoke.v1.sh`; HTTP binds only to guest
loopback. These are observations of this validation environment, not a guarantee
about future Ubuntu images or package versions.

| Check | Execution and observed result | Boundary |
| --- | --- | --- |
| Initial provisioning and management | CP1 fresh provisioning, readiness, status, repeated up/reuse and down, restart, and cloud-init schema validation passed. Evidence was terminal output; CP1 has no structured result artifact. | One ARM64 macOS host/guest setup. |
| Normal smoke lifecycle | CP2 `make lab-test` passed on the corrected code in about 52 seconds. CP3 repeated it successfully in 52.804 seconds, with no primary or cleanup errors. See the result references below. | Real SSH, systemd user job, Compose health/HTTP/logs, per-run cleanup, and owned stop. |
| Failure, handled signal, and reset | The three earlier real-VM cases passed together in 391.774 seconds before the narrow unavailable-VM cleanup correction. The normal smoke was then retested successfully on the corrected code. | Those three cases were not repeated after that correction. |
| Reset recovery and identity | Reset removed a guest marker, repaired deliberately stopped Docker, and preserved owner, local SSH key, pin, and baseline identity. | The original baseline was created during Builder through an equivalent preliminary management sequence. Final-code baseline verification/reuse/reset were live tested; final `ensure_baseline()` first-use creation was not live tested from fresh state. |
| Focused safety guards | Four unit tests cover eight stopped-VM mount subcases; stopped/absent cleanup with or without attempted workload; identity refusal; and diagnostic/fixture errors still reaching owned stop. | In-memory patched boundaries; these do not exercise a real mount, missing VM, or failed daemon. |
| Unapproved SSH key | CP3 passed: a disposable Ed25519 key with no SSH agent/config identities received explicit public-key denial. An authorized connection confirmed that its unique marker command did not execute. The connection used the actual saved host pin and a bounded noninteractive client. | SSH infrastructure authentication only. A network error or host-key error is not accepted as an authentication test pass. |
| Owner and host-pin refusal | CP3 passed: real `down` calls with separate in-memory mismatches raised the expected errors. The VM remained running and local manifest/key/pin/config bytes remained identical after each refusal; the trusted state then stopped it. | Local lab ownership/trust guards, not MCP principal authentication or ownership. |

Private CP2 result references are relative to the repository root; they are
gitignored local evidence, not committed fixtures:

- Corrected normal smoke:
  `.lab/results/20261017T025045Z-test-755db42fdf8f4d2d868838d1eb43bc59/result.json`
- Earlier normal smoke:
  `.lab/results/20261017T023404Z-test-51c0db12ad6b4d1886606d63744f206b/result.json`
- Expected workload failure, with successful cleanup:
  `.lab/results/20261017T023518Z-test-49f861b90a35459189b550e8680ea211/result.json`
- Baseline reset:
  `.lab/results/20261017T023745Z-reset-4818f9330858427c8b417e536f1d7bf0/result.json`
- Expected `SIGTERM`, with successful cleanup:
  `.lab/results/20261017T024006Z-test-d6d8392401be4d4a85205f68f501878f/result.json`

The expected-failure and signal results record `success: false` for the injected
primary error; passing harness assertions require that error and an empty cleanup
error list. A failed result is not itself a passing application test. Normal
smoke/reset result directories also contain private diagnostic and cleanup logs.

Checkpoint 3 selected security tests exited 0: two cases passed in 95.406 seconds
(95.506 seconds including the outer process). Its normal smoke exited 0 in
52.804 seconds. Private evidence:

- Selected security harness output:
  `.lab/results/20261017T030337Z-cp3-security.log`
- Normal workflow output:
  `.lab/results/20261017T030337Z-cp3-normal.log`
- Final audit:
  `.lab/results/20261017T030337Z-cp3-audit.json`
- Final normal smoke result:
  `.lab/results/20261017T030533Z-test-26f9020e0d5e411ca49cae037033d6ed/result.json`

Final inspection confirmed `Stopped`, empty mount metadata, a valid baseline, and
unchanged owner/key/pin/baseline hashes. Disposable key directories were gone;
the harness proved the unauthorized marker absent. New evidence files were
`0600` and result directories `0700`. The unchanged three deeper lifecycle cases
were not repeated in checkpoint 3.

## Limits and safe next changes

The real signal case delivered `SIGTERM` to the installed handler after the
fixture completed and its Compose service was restarted. It did not interrupt an
in-flight SSH command. `SIGINT`, `SIGKILL`, host shutdown, and daemon loss were not
separately live tested; bounded cleanup cannot guarantee recovery after forced
process/host loss.

Pending application work includes deployment of a current MCP build, MCP protocol
calls, OAuth/authentication, gateway/runner behavior, persistent shells, and the
planned 16-session scenarios. Passing these infrastructure checks supplies no
evidence for those behaviors. Add application tests when those entry points exist;
keep them distinct from SSH transport and local VM identity safeguards.

Change installation in the versioned cloud-init recipe, the smoke payload in the
versioned fixture, and lifecycle/ownership rules in `tools/lab.py`. Updating a
recipe does not repair an already provisioned VM. Preserve `.lab/` state and the
recorded baseline, inspect `make lab-status` after interrupted operations, and
review private result files before manual recovery.

## Checkpoint 4: native execution foundation (2026-10-17)

Implemented a separate explicit `target` profile and a native Go target runner.
The default control profile and its original `.lab/` records remain compatible;
the target uses `.lab/target/`. No control VM reset or payload change was made.
The target's original clean snapshot is retained and never replaced. Target
up/test/reset compare the actual guest binary and service hashes with the current
ARM64 build and upgrade only when they differ, including after baseline restore.
Status reports installed-service readiness without rebuilding.

Checks actually run:

- `python3 -m unittest discover -s tests -v`: 9 focused lab regression checks
  passed, including stopped mount refusal for both profiles, cross-profile
  manifest refusal, guest hash based upgrades, and pre-build symlink refusal.
- Python syntax checks for both lab harnesses and the runner harness passed;
  both shell smoke fixtures passed Bash syntax checks.
- `go test -race ./... -count=1 -timeout=90s`: six focused native worker behavior
  tests passed on the final local module (`sshmcp`), 17.047s. The helper test
  executes real Bash workers in child processes; only the systemd launcher is
  replaced locally. macOS required permission to bind local Unix sockets.
- `go vet ./...` and `GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build` passed.
- `python3 tests/integration_runner.py -v`: all 3 real Ubuntu/systemd/pinned-SSH
  cases passed in 83.306s. They exercised persistent cwd/variables/functions,
  isolation, separate output/cursors, 16 concurrently active jobs, busy/capacity
  rejection, duplicate/conflict behavior, independent SIGINT cancellation,
  command deadline, SSH client exit, an abrupt runner-daemon kill with automatic
  restart, and an actual reboot of only `sshmcp-target`. Restart/reboot left
  live jobs lost and retained keys without replaying their side effects.
- After the final pipe-write/sync hardening and local module naming change,
  `python3 tools/lab.py test --profile target` rebuilt/deployed by guest hash and
  passed its current-build smoke. Private result:
  `.lab/target/results/20261017T123613Z-test-1d3ea2207a1c4ed590c0936e1f0da5c6/`.
  Workload, diagnostics, cleanup, and the final owned stop all succeeded.
- Final read-only target checks showed 4 CPUs, 8,109,092 KiB guest memory,
  a 42,949,672,960-byte root disk (`sda`), Docker absent, and a mode-0600 runner socket. The installed and recorded
  current binary SHA256 matched:
  `08df77c7a0c3895e2128adb90b308ecf8d377f8109b1307b2b504aea00e05caf`.
- Every live runner case verified the original target baseline identity and
  unchanged control manifest/key/pin/config contents, removed its own marker,
  and stopped the target. Both named VMs were stopped after validation.

The full three-case live harness preceded the final defensive pipe-write/sync
hardening; the final code received the full focused race checks and a current
build real target smoke afterward. MCP, OAuth, gateway behavior, detached Docker
survival, and agent acceptance are later-checkpoint validation, not claimed by
these results. Translation and independent Review are still required before the
checkpoint is accepted.

The CP4 readiness review corrections received the following additional checks:

- Status no longer builds or installs a runner when its local installation flag
  is missing or false. Only explicit up/test/reset/install paths request an
  installation. The control profile retains its existing behavior.
- Target readiness now requires a private, owner-bearing `health` RPC after the
  installed-binary and active-service checks. This probe allocates no shell/job,
  changes no durable records or timers, and rejects a fatal or closing engine.
  Status makes one attempt with a 10-second host deadline; lifecycle readiness
  polls for at most 45 seconds so startup recovery can finish before returning.
- All 12 focused Python checks passed, including incomplete installation records,
  an active service without available RPC, and bounded lifecycle polling. Python
  syntax, both shell fixtures' syntax, and whitespace checks passed.
- `go test -race ./... -count=1 -timeout=90s` passed in 17.025s with permitted
  local Unix socket binding. The added transport check covers unavailable RPC,
  required ownership, fatal engine refusal, and unchanged durable records.
  `go vet ./...` and the Linux ARM64 build also passed.
- One current-build real target smoke passed with the new readiness probe:
  `.lab/target/results/20261017T130120Z-test-f7dd8c7bf45041a1983917b596ccf66f/`.
  Workload, diagnostics, cleanup, and owned stop succeeded. Before/after checks
  confirmed the original target baseline, target key/pin bytes, and control
  manifest/key/pin/config bytes were preserved, and both VMs were stopped.

The full crash/reboot harness was not repeated for these readiness corrections.
# Checkpoint 5 builder evidence (2026-10-17)

The private MCP implementation is described in [MCP.md](MCP.md). These are
developer results before independent Translation, final validation, Tester,
and Review; they are not checkpoint acceptance or the later agent-deployment
exercise.

- Gateway/runner Go race tests and vet passed. Existing host-only Python lab
  safety tests passed (12 cases). Gateway checks exercise actual signed JWKS
  verification, issuer/audience/expiry/subject/scope rejection, verified-owner
  mapping, bounded lossless output, three-tool HTTP mapping, native SSH pins and
  independent channel limits, no automatic replay, and the OAuth resource guard.
- Real authenticated MCP shell tests passed for persistent Bash state,
  independent connections, serial batches, byte cursors/readable/binary output,
  idempotency/conflicts, 16 active jobs, capacity/busy rejection, prompt cancel,
  and deadlines. Evidence: private
  `.lab/results/20261017T134233Z-mcp-integration-e36d14c295984ef0b39ad99b8c8d7046/`.
  That run's auth fixture failed because an empty OAuth scope was rejected by
  Keycloak; the fixture was corrected to request openid-only and check gateway
  scope denial. It is not recorded as an entirely passing suite.
- The corrected real auth case subsequently passed in isolation, including
  unknown/mismatched authorization-code resources, intended/omitted refresh
  audience, rejected unknown/duplicate refresh resource, required admin scope,
  and cross-principal isolation. Evidence: private
  `.lab/mcp/auth-developer-check.json` and
  `.lab/mcp/keycloak-refresh-protected.json`.
- Repeated private authorization configuration retained the two existing user
  IDs, resource client UUID, and pre-registered Codex RP identity. Private logs:
  `.lab/mcp/auth-final-configuration-{0,1}.log`. The final setup's authenticated
  catalog passed in
  `.lab/results/20261017T135546Z-mcp-up-d8963072c46f48e0b5d019a6b460e8f4/`.
- Installed Codex 0.162.1 performed normal Authorization Code + S256 PKCE login.
  A fresh process then completed real `connect`, execute, output/status, and
  `disconnect` calls, returning `CP5_CODEX_REUSED`. It began 151.485 seconds after
  the successful login, beyond the configured 60-second access-token lifetime,
  and reused/refreshed stored credentials without a new login. Evidence:
  `.lab/mcp/codex-use3.jsonl`, `codex-use3.txt`, and `codex-use3-summary.json`.
  The user's account/model settings were retained; only private-server and
  invocation-local tool/isolation overrides were supplied.

The tests also exposed concrete interoperability boundaries: explicit Codex
`oauth_resource` duplicated its authorization parameter; missing Keycloak subject
mapping was rejected; Codex refresh needed its allowed optional offline scope;
and pinned Keycloak refresh ignored resource indicators. The implementation
preserves strict JWT checks and adds the fixed private resource guard plus a
canonical URI resource-client ID. Failed login/tool traces and the original
refresh matrix remain private under `.lab/mcp/`; unsuccessful CLI runs are not
counted by their exit code alone.

The full CP5 gateway/SSH/reboot/abandonment fault suite is implemented in
`tests/integration_mcp.py` but did not complete a successful full builder
run. The subsequent independent attempt is recorded below. CP4's earlier runner
recovery evidence remains separate. No application deployment, baseline restore,
target Docker installation, public endpoint, or CP6/CP7 acceptance was performed
for these CP5 developer checks.

## Checkpoint 5 independent test failure and fixture correction (2026-10-17)

The dedicated Tester ran six live methods in 53.962s: the first three passed;
the fault method errored and both expiry assertions failed. This was a failed
suite, not CP5 acceptance. Private evidence:
`.lab/mcp/tester-live-output.log` and
`.lab/results/20261017T141416Z-mcp-integration-bb1344c291934dc3b3e0948505d723b0/`.

- The passing methods covered authentication/resource/refresh/ownership checks,
  persistent and isolated shell state, output/deduplication, 16 active jobs,
  capacity/busy rejection, cancellation, and deadlines.
- The fault method passed gateway restart, SSH transport loss, recovery after
  an actual changed target boot ID, `lost`/`shell_state_lost`, and duplicate
  recognition. Its final side-effect check failed because Ubuntu removed the
  marker under `/tmp` during reboot. That attempt did not establish the final
  no-replay side-effect claim.
- Idle expiry returned the defined `lost/idle_timeout`; the fixture incorrectly
  expected `closed`. The Tester's extra active-lifetime method likewise observed
  `lost/max_lifetime` for its connection and failed an incorrect `closed`
  expectation. These state mismatches did not demonstrate a runner defect.
- The Tester reported byte-equal restoration of the normal runner unit. The
  evidence recorded no cleanup errors; both VMs were stopped and their original
  baseline/owner/pin/private-file comparisons were unchanged.

The corrected harness uses a unique private marker in `/home/mcp-test/`, removes
it during cleanup, and saves expected/observed contents plus the before/after
boot IDs in private `replay-marker.json`. The idle assertion now requires
`lost/idle_timeout`. Within the same temporary two-second idle/five-second
lifetime service configuration, it also starts a real sleeping job and requires
`failed/max_lifetime`, connection `lost/max_lifetime`, and no trailing output.
The normal runner service is still restored in `finally`; restoration failures
are retained separately from the primary test failure.

Diagnostics now independently verify each running VM's ownership before bounded
inspection. The existing private control log is retained; the private target
log adds the runner journal, status, and current unit. Neither diagnostic path
starts a stopped VM or changes guest state. A failure on one profile does not
skip the other. Diagnostics precede shutdown, and evidence is written after all
shutdown attempts so it includes shutdown errors as well as earlier failures.

For this correction, `python3 -m unittest discover -s tests -v` passed all 18
host-only checks, including independent diagnostic collection, ownership refusal,
stopped/absent profile refusal, and primary/cleanup error retention. Python syntax
and whitespace checks passed for the corrected files. These checks did not start
or operate either VM, install a service, or repeat the live MCP suite. The final
translated code still requires final validation, a fresh dedicated live Tester,
and independent Review before CP5 acceptance. CP6/CP7 remain outside this work.

## Checkpoint 5 final independent validation (2026-10-17)

After correction Translation and final local validation, a fresh dedicated
Tester installed the current build and ran all five permanent live methods:
**PASS, 53.615s** (53.691s outer duration), exit 0. Private evidence is in
`.lab/results/20261017T143402Z-mcp-integration-071867762b054d56a71064d4591936aa/`
and the consolidated `.lab/mcp/final-tester-runtime.json`.

The run established authentication/resource/refresh/ownership enforcement,
persistent isolated shell state, incremental output, idempotency, 16 active jobs,
cancellation/deadline handling, gateway restart, scoped SSH loss, actual target
reboot without replay, idle expiry, and active maximum-lifetime expiry. The
reboot-persistent marker contained exactly `onceonce`; before/after boot IDs and
marker contents are retained in `replay-marker.json`. Both VM diagnostics were
saved before shutdown. The target unit was byte-equal to the normal runner unit
without shortened timer flags. Cleanup errors were empty; both VMs were stopped.
Owners, baselines, management keys, host pins, accounts, and authenticated subjects
were preserved. The permanent test source was unchanged during this run.

The Tester independently inspected actual completed Codex tool results in
`.lab/mcp/codex-usetesterFinal{.jsonl,.txt,-summary.json}`: connect, execute,
completed output `CP5_CODEX_REUSED` with exit 0, and closed disconnect. That
15.25s run began 949.446s after the stored login against a 60-second access-token
lifetime, without a new login. The final Python-only fixture correction did not
change gateway/client configuration. The Tester also passed all 18 host safety
tests and all eight local JWT HTTP subcases. Coordinator final checks passed
Go formatting/race/vet, Linux ARM64 builds, Python/shell syntax, and whitespace.
Independent Review remains the final checkpoint gate. CP6/CP7 agent deployment
acceptance is not established by these results.

## Checkpoint 5 request/response bounds correction (2026-10-17)

Independent Review subsequently denied the implementation for two uncovered
size boundaries. A valid 1,043,517-byte tab-heavy source fit the former 2 MiB
request and acknowledgement limits, but echoing that source alongside the
default 8 KiB stdout chunk exceeded the 2 MiB response limit. A valid
1,048,014-byte source with a long `>` comment expanded to more than 6 MiB under
Go's default JSON HTML escaping, exceeding the target CLI and Unix request
decoders. The earlier passing five-case live run remains valid for its recorded
cases; it did not exercise either of these sizes.

The correction projects command-free job responses by copying the job before
serialization. Durable records, internal execution, and exact-source
idempotency retain the complete command. All source admission/load checks use
the same 1 MiB limit. HTTP's outer reader, the MCP SDK's separate body reader,
target CLI stdin, and both daemon/worker Unix request decoders now share a
6,422,528-byte budget: six times the source bound plus 128 KiB for escaped
owner/key metadata and the RPC/MCP envelope. Response decoders and SSH capture
keep a separate finite 2 MiB cap. Default JSON escaping remains supported.

Focused local tests send both exact source sizes through the real Unix runner
and Bash worker, read the 8 KiB boundary output, verify source-free responses,
restart the engine, and check full-source persistence, same-key deduplication,
and changed-source conflict. A separate HTTP assertion sends a maximum 1 MiB
source with default Go HTML escaping above the SDK's former 4 MiB body limit.
The existing live shell/output method now executes both size classes through
MCP/SSH/CLI/runner/worker, requires 8,192 stdout bytes, and checks duplicate,
lookup, and bounded status responses.

Builder's full local Go race suite, focused boundary rerun, local vet/Linux ARM64
build, all 18 host-only Python safety checks, Python syntax, and whitespace
checks passed. No VM was
started or operated for this correction. Final translated-code checks, the
extended five-case live suite by a fresh dedicated Tester, and fresh independent
Review are still required. The earlier 53.615s live result does not validate
these new boundary cases. CP6/CP7 remain outside this work.

## Checkpoint 5 extended final live validation (2026-10-17)

After fresh Translation, coordinator final checks passed formatting, full race
tests (gateway 4.347s, runner 23.839s), vet, both Linux ARM64 builds, all 18 host
safety tests, syntax, and whitespace. A fresh dedicated Tester installed both
current binaries and independently matched their guest/host hashes. The target
still had no Docker or application deployment.

The unchanged permanent five-case live suite **passed in 55.524s** (55.591s
outer), exit 0. Both large-source subcases completed through actual HTTP, native
SSH, target CLI, Unix runner, and Bash worker, each returning 8,192 stdout bytes.
Duplicate/lookup retained the original job ID and status stayed source-free and
bounded. All other authentication, execution, recovery, reboot/no-replay, and
expiry cases passed. Private evidence:
`.lab/results/20261017T145714Z-mcp-integration-636de0f4dff94ce69e873abc6d3d56f6/`
and `.lab/mcp/wire-tester-runtime.json`.

The Tester independently verified an actual changed target boot UUID in its
journal and exact marker `onceonce`. The raw Multipass boot-output artifact
retains a progress prefix; it is not treated as a normalized UUID. Both VM logs
were saved before shutdown; the normal runner unit was restored byte-for-byte,
cleanup errors were empty, and both VMs were stopped. Owners, baselines, keys,
pins, accounts, and permanent test source were unchanged. Current pre-suite
subjects matched prior identities; post-reboot subjects were not decoded again,
although authenticated calls after reboot passed.

A fresh isolated installed-Codex stored-login proof passed in 12.105s, starting
3,561.443s after the login log against a 60-second access-token lifetime.
The Tester verified actual active connect, accepted execute, completed exit-0
output `CP5_CODEX_REUSED`, and closed disconnect. Its known post-completion
shutdown warning is retained; exit status alone is not the success criterion.
Private proof: `.lab/mcp/codex-usewireFinal-summary.json` and associated JSONL.
No new login, code edits, baseline reset, or deployment occurred. Fresh
independent Review remains the final checkpoint gate.

Fresh independent Review subsequently returned **Approved**, with no actionable
findings. It independently passed the full race suite, vet, 18 host tests,
formatting/syntax/whitespace checks, and both ARM64 builds; rebuilt hashes matched
the live-test binaries. It inspected the raw live/Codex, reboot, no-replay,
restoration, and preservation evidence without repeating VM or model operations.
This approval covers checkpoint 5 only; CP6/CP7 and public HTTPS remain excluded.

## Checkpoint 6 builder checks (2026-10-17)

The exercise/lifecycle implementation is described in [DEMO.md](DEMO.md).
Builder checks before fresh Translation, final checks, and independent Review:

- All 32 host-only Python checks passed in 0.014s. The 14 added focused cases
  cover real-result/host-action audit boundaries, exact skill-path resolution,
  child-group timeout cleanup, fresh trace ancestry, Loki structured metadata,
  histogram/unrelated-counter refusal, post-phase counter sample filtering,
  NaN dashboard refusal, interruption/diagnostic failure ordering, explicit
  target-only reset, absent-demo up refusal, socket symlink refusal, private
  source redaction, and byte-cursor drainage. No unit check operates a VM,
  network service, or model. Python syntax and whitespace checks passed.
- Installed Codex prompt-only isolation passed with the final explicit private
  temporary workspace root outside Git ancestry and bounded process-group
  cleanup. Private evidence:
  `.lab/results/20261017T153949Z-demo-isolation-check-ca75a8a498034c1abb0d75b34a3c4c56/`.
  The actual final prompt contains no skill catalog, memory, or project
  instructions; the isolated enabled MCP inventory contains only
  `sshmcp-private`. Account/model/global settings were retained. Earlier failed
  probes remain under separate result directories: they exposed plugin-only
  transport validation, skill-root aliases, and restricted host-runtime access.
- The real OAuth/MCP/native-SSH benchmark passed all 16 warmed `sleep 2` jobs
  with exact individual outputs, overlapping guest execution intervals, and
  2.754798s from host dispatch through final drainage. Concurrent warm-command
  median was 0.829370s and maximum 0.890509s. Private raw tool/results/diagnostics:
  `.lab/results/20261017T153210Z-demo-benchmark-8099aabcff4441159c3ebfde9340622f/`.
  Its fresh-target inspection found no Docker binary/packages/data or Go/app
  deployment artifacts. Connection cleanup and diagnostics succeeded; cleanup
  errors were empty and both VMs were stopped. The benchmark preceded the final
  shorter per-request benchmark timeout, parallel disconnect cleanup, and
  summary counters; the shared wire/auth/cursor protocol was unchanged.
- Before/after comparisons matched both owners, original baselines, pins,
  management-key/config bytes, private account file, and global Codex config.
  The private comparison records live in `.lab/demo/developer-preservation-*.json`.

These checks did not reset a baseline, install target Docker or application
files, run a deployment/troubleshooting model session, inject a telemetry fault,
or query a live application LGTM backend. Dashboard/API acceptance, controlled
fault effectiveness, fresh repair, private credential export, demo resume, and
the full handled-interruption path during a real actor remain checkpoint-7
validation. No demo-ready or actor-attempt pointer was created. There is no
dedicated Tester for checkpoint 6; fresh Translation, final checks, and independent
Review are still required before its commit.

## Checkpoint 6 diagnostic and operator transport corrections (2026-10-17)

Fresh Translation identified a diagnostic assignment outside its container
loop. The new host-only regression reproduced an empty-container `NameError`
and loss of all but the last container's log evidence before the correction.
Diagnostics now retain an explicit container map, preserve `docker ps` evidence
with zero demo containers, and skip deployment inspection/export for that empty
state. The regression executes the actual embedded Python with stubbed Docker
process results for zero, three, and ten containers. It verifies distinct
stdout/stderr/exit results, the eight-container/100-line log limit, and the
existing per-command/operator deadlines. Existing primary/diagnostic-error and
evidence-before-shutdown checks continue to pass.

The operator helper now performs target application inspection, container logs,
fault injection, and source export through separate recorded MCP connections
using only `connect`, `run_command`, and `disconnect`. The operator's Python
input and idempotency key are saved before dispatch; private MCP transcripts,
raw output streams, latest job state, timing, and separate primary/disconnect
errors remain under `operator/`. Uncertain executes are not replayed. Both
output streams retain the finite 8 MiB bound; overflow saves an explicitly
marked prefix and fails. Ownership/provisioning checks, SSH forwarding, and
fixed VM/service diagnostics remain management infrastructure, with no
management-SSH application fallback. Operator results do not count as actor or
benchmark evidence.

All 41 host-only checks passed in 0.039s, including eight new mocked operator
checks for quoting/private evidence, ownership refusal, uncertain execution,
primary plus disconnect failures, unclosed disconnect, signal preservation,
output bounds, and terminal job acceptance. Python syntax checks for all tool
and test modules and whitespace checks passed. No VM, Docker process, network
service, or actor/model was operated for this correction. The earlier live
benchmark/fresh-target evidence predates this operator transport change; it
does not validate the new helper path. Live deployment/fault/repair acceptance
remains checkpoint 7. Fresh Translation, final validation, and independent
Review are still required for checkpoint 6; there is no dedicated Tester.

## Checkpoint 6 final translated validation (2026-10-17)

Fresh Translation reformatted only the five embedded operator scripts. Their
syntax trees and the outer module's syntax tree were unchanged. Coordinator
final checks passed all 41 host tests, Python/shell syntax, Go formatting,
race/vet checks, both ARM64 builds, and whitespace checks.

The explicit `agent-demo-reset` command restored only the target baseline,
verified its identity/readiness, and stopped it. Owners, original baseline
records, keys, host pins, accounts, and global Codex configuration matched the
saved fingerprints; the control profile was preserved. Private evidence:
`.lab/results/20261017T155603Z-demo-reset-8f1773547955477e84cda5fdd82f9b5e/`
and `.lab/target/results/20261017T155603Z-reset-ccbc56be6e5c4a7dbb37059ff6b82f82/`.

Final installed-client prompt isolation passed without a model run in
`.lab/results/20261017T155734Z-demo-isolation-check-c5eb1107a4a7449f8ad152e587fe5486/`.
The final real benchmark passed after current-build installation: all sixteen
warmed two-second jobs overlapped and drained in **2.128980s**. Concurrent warm
command median was **0.157188s**, maximum **0.182186s**; 652 MCP tool calls were
recorded with zero transport errors. Fresh-target inspection and container
diagnostics completed through the new MCP operator helper with recorded
connect/execute/status/disconnect evidence and no cleanup errors. No Docker,
application files, or Docker data existed on the target.

Private final evidence:
`.lab/results/20261017T155754Z-demo-benchmark-2264d5e2b1c14dd6b6bb9b3cf7822a7f/`.
Both VM/service logs were collected before shutdown. Independent final inventory
showed both VMs stopped with empty mounts. Owners, baselines, keys, host pins,
management configurations, accounts, and global settings matched the saved
before/after fingerprints in `.lab/demo/cp6-final-{before,after}.json`.

No deployment/troubleshooting model session, Docker/app installation, telemetry
fault, live LGTM query, or actual application-source export occurred. Those
acceptance paths remain checkpoint 7. Independent Review is the final CP6 gate;
no dedicated Tester was used for this checkpoint.

## Checkpoint 6 counter, actor quota, and dashboard corrections (2026-10-17)

The independent review found that the pinned Prometheus 3.15 metadata endpoint
only enumerates scrape targets, so valid LGTM OTLP counters could never satisfy
the earlier type check. The deployment brief now declares one additional
component constraint: enable Prometheus's `type-and-unit-labels` feature. The
evaluator checks the live backend's feature flags and requires its reserved
`__type__="counter"` label, retaining that type and the service attribution in
each sampled selector. Exact received metric names, including `_total`, remain
unchanged. The legacy metadata API is no longer needed. Histogram counts,
gauges, missing type labels, and unrelated services are rejected; fresh sample
timestamps and the full independent twelve-request counter increase still
remain necessary.

This choice follows the pinned [metadata API implementation](https://github.com/prometheus/prometheus/blob/v3.15.0/web/api/v1/api.go),
[type-label contract](https://github.com/prometheus/prometheus/blob/v3.15.0/docs/feature_flags.md#type-and-unit-labels),
[OTLP type mapping](https://github.com/prometheus/prometheus/blob/v3.15.0/storage/remote/otlptranslator/prometheusremotewrite/otlp_to_openmetrics_metadata.go),
and [reserved-label assignment](https://github.com/prometheus/prometheus/blob/v3.15.0/storage/remote/otlptranslator/prometheusremotewrite/helper.go).
The mapping classifies cumulative monotonic sums as counters, nonmonotonic sums
and gauges as gauges, and histograms as histograms. Native delta sums receive
unknown type and cannot satisfy cumulative-counter acceptance. The pinned
[LGTM launcher](https://github.com/grafana/docker-otel-lgtm/blob/v0.37.0/docker/run-prometheus.sh)
supports extra Prometheus arguments. No application source, Compose recipe, or
collector configuration was supplied to an actor by this correction.

Actor and prompt-probe capture now drains stdout/stderr through separate bounded
pipes while delivering stdin without blocking the output readers. Each stream
retains at most 8 MiB. The first excess byte fails the operation, terminates its
own child process group, and marks the byte counts/prefix as incomplete in the
private process record. Transcript audit reads incrementally and independently
enforces the total byte quota, including for oversized files with otherwise
complete successful-session prefixes. Dashboard acceptance records distinct
application/proxy log checks from fresh, populated backend-returned streams.
Application logs alone, empty or stale proxy streams, and other services fail.

All 49 host-only Python tests passed, including the added OTLP-without-scrape-
metadata positive case, missing-feature refusal before polling, counter-type
negatives, full burst-delta positive/negative cases, per-stream byte overflow,
exact-limit multibyte output, large stdin with simultaneous output drainage,
bounded transcript audit, and proxy-dashboard coverage. Capture tests run only
local Python subprocesses; a real descendant ignoring SIGTERM stopped after
the exact owned process group received SIGKILL. `make runner-check` passed the
Go race suite (cached) and vet. Python syntax, both lab shell syntax checks, and
whitespace checks passed.

No VM, Docker process, model/actor session, baseline reset, deployment, or live
application/backend query was operated for this correction. Existing private
lab evidence was preserved. The previous live benchmark predates these edits;
it does not prove the new capture or backend paths. Actual typed OTLP backend
responses, dashboard queries, controlled fault, fresh repair, and resume remain
checkpoint-7 acceptance work. Fresh Translation, final validation, and a fresh
independent Review are still required before checkpoint 6 can complete.

Fresh Translation inspected the corrected files without edits and independently
passed all 49 host tests. Coordinator final validation also passed all 49 tests,
Python/shell syntax, whitespace checks, and cached Go race tests plus vet.
The installed-client isolation check passed using the final bounded capture,
without a model call or VM operation. Its private evidence is
`.lab/results/20261017T162008Z-demo-isolation-check-ad6495a857ba44ea8934bdb82fb36a5b/`.
This confirms real inventory/prompt capture after the quota change; live OTLP
ingestion and the two actual actor exercises remain checkpoint 7.

Fresh independent Review found no actionable issues and returned **Approved**.
It independently passed all 49 host tests, Python/shell syntax, whitespace,
and Go race/vet checks (race results cached), inspected the saved benchmark,
isolation and preservation evidence, and checked the pinned Prometheus type-label
contract. No dedicated Tester or deployment/model operation was used for CP6.

## Checkpoint 7 builder launch and audit corrections (2026-10-17)

The first explicit target-only reset passed before the first real deployment
attempt. That attempt failed at actor launch: the installed CLI reported
`code-mode host is disabled`, exposed no usable private tools, and completed
zero MCP calls in 13.791s. The earlier audit incorrectly described its CLI
diagnostic item as a disallowed host action. The narrow correction keeps the
required transport host enabled while model-visible Code Mode and host tools
remain disabled, removes the locally ignored image-setting override, and counts
runtime diagnostics while still requiring completed private MCP work. A separate
fresh read-only CLI preflight then completed direct connect, execute, status, and
disconnect calls in 15.021s, with no tool/runtime errors. The initialized CLI
catalog independently exposed exactly the private three tools; disabled server
entries had empty tool maps. One initial catalog-probe assertion incorrectly
assumed disabled servers disappear; its failed result and corrected verification
are both retained. No target/model action occurred in that catalog probe.

After another explicit target-only reset, the second fresh deployment actor
finished in 550.269s with 92 completed MCP calls. Its 32 jobs included 27 completed,
four failed, and one lost. The lost command used Bash `set -e` around a Dockerized
Go dependency operation that failed because Docker-bridge DNS refused
`proxy.golang.org`. Loss of the persistent shell/control channel is consistent
with the documented Bash option semantics. The actor recovered independently;
its later self-initiated reboot also left an idle connection `lost/runner_stopped`.
The exact failed command, output, and lost-job metadata are retained privately.

The second attempt was still rejected before independent evaluation because
the audit demanded `closed` for successful disconnects of those two already-lost
connections. The runner deliberately preserves an already-cleaned terminal
state. The corrected audit requires a successful response for the same
connection ID, `closed` or `lost`, and no busy job; it counts both terminal
outcomes. Active, unknown, mismatched, busy, and errored cleanup results remain
insufficient. No runner behavior was changed. This correction does not change
the failed status of either prior attempt. Both attempts saved diagnostics and
stopped both VMs with no cleanup errors; neither reached independent deployment,
controlled-fault, or repair evaluation. Source/config from attempt two was
exported privately during failure diagnostics. Actor claims and its own
verification files are not independent acceptance evidence.

Private evidence:

- First reset: `.lab/results/20261017T162519Z-demo-reset-8147faf914f24493aa32ae817aa5b9c4/`.
- First failed attempt: `.lab/results/20261017T162617Z-demo-test-6f4610ccae2442b5b75ddfe7bf833202/`.
- Read-only CLI preflight: `.lab/results/20261017T163159Z-cp7-cli-preflight-326887f240c04a208cab5f7c2bb26e24/`.
- Actual initialized catalog: `.lab/results/20261017T163250Z-cp7-catalog-probe-0ebd9653dbbb492fa24a3a32d07e1d8c/`.
- Second reset: `.lab/results/20261017T163333Z-demo-reset-e881e4f9c08848dd8262ed39fc72dcd3/`.
- Second failed attempt: `.lab/results/20261017T163425Z-demo-test-30e09ee532054406be39f7a2a01372c5/`.

The first and second warmed sixteen-job benchmarks passed in 2.151320s and
2.158705s respectively, with zero transport errors or automatic retries. Hash-only
records in `.lab/demo/cp7-*.json` preserve comparisons against the protected CP6
records. Successful in-run human guidance remains separate from these explicit
across-attempt developer corrections and the standalone preflight. Fresh reset
and a new unchanged-goal attempt are required for acceptance; no failed actor is
continued or coached.

## Checkpoint 7 live counter and dashboard checker corrections (2026-10-17)

The third explicit target-only reset preceded another fresh, unchanged-goal
deployment. Its warmed sixteen-job benchmark passed in 2.133768s with zero
transport errors. The deployment actor completed in 580.483s with 89 completed
tool calls, 23 completed jobs, four failed jobs, one lost job, and two tool errors.
It returned four closed and one already-lost idle disconnect. Reused execute
keys, exact repeated source submissions, and duplicate results were all zero;
the recorded failed/lost/tool errors still required independent actor recovery.
The corrected tool audit passed. Independent counter-baseline evaluation then
failed, retaining actual backend observations and stopping both VMs cleanly:
`.lab/results/20261017T165105Z-demo-test-b34a2d8020154c3083105d62a65178b1/`.
No controlled fault or troubleshooting actor ran in that attempt.

Typed discovery returned genuine application and proxy counters, but the
freshness join returned no rows. Pinned [Prometheus v3.15 timestamp code](https://github.com/prometheus/prometheus/blob/v3.15.0/promql/functions.go)
drops reserved metadata labels, leaving the counter's `__type__` unmatched on
the timestamp side. A separate read-only diagnostic against the preserved
deployment confirmed the actual mismatch: the original join returned zero
rows, while `and ignoring(__type__,__unit__)` returned the real counter sum `3`.
Future-cutoff, historical-stale, and unrelated-service controls all returned zero
rows; the unfiltered historical selector itself returned two rows. Both VMs
stopped cleanly. Raw query evidence:
`.lab/results/20261017T171017Z-cp7-counter-diagnostic-d7fcb12f339c430197c6572a81e81bf7/`.

The evaluator now ignores only those reserved metadata labels at the freshness
join. Typed/service-scoped selectors, underlying sample cutoff, and the full
independent request-burst delta remain required. Request-byte volume counters
are also excluded by metric name or byte unit; they cannot stand in for request
counts. Existing counter regressions were expanded rather than adding another
test suite.

A full positive checker diagnostic against the unchanged old deployment then
proved fresh HTTP, trace ancestry, twelve correlated Go logs, matching proxy
logs, and both genuine request counters increasing exactly `3 -> 15`. It failed
later because a dashboard's valid `$__range` macro was sent literally to
Prometheus (HTTP 400). Its saved dashboard also had two Loki log panels using
`${probe}`, an explicitly declared textbox with empty current value. Evidence:
`.lab/results/20261017T171315Z-cp7-positive-checker-diagnostic-23bc6d57405a419f95bee849e2b46a4d/`.
That diagnostic remains failed.

Dashboard probes now render whole standard time-macro names in an explicit
five-minute context and resolve only declared empty textbox defaults. Numeric
forms are handled without prefix substitution; undeclared, nonempty, multivalue,
and non-textbox defaults remain unsupported and fail clearly. This follows
[Grafana's global-variable contract](https://grafana.com/docs/grafana/latest/visualizations/dashboards/variables/global-variables/).
The existing dashboard regression covers these positive and negative cases.
All fourteen saved dashboard targets resolved locally before a new live check.
All 52 host tests, Python syntax, and whitespace checks passed.

The subsequent full positive checker diagnostic passed on the unchanged old
application: both genuine request counters again increased `3 -> 15`, the fresh
trace had actual proxy-to-Go ancestry, twelve Go logs correlated to that trace,
the proxy JSON log contained the probe, and requests, latency, both log streams,
and trace navigation all passed dashboard checks. Both VMs stopped with empty
cleanup errors. Evidence:
`.lab/results/20261017T172130Z-cp7-positive-checker-diagnostic-0ff1dacf870845c191dcea38c11865bd/`.
These developer diagnostics ran no model, application modification, controlled
fault, or repair actor. They do not make any prior attempt accepted. The next
acceptance attempt still requires explicit reset and fresh unchanged-goal actors.

The fourth fresh attempt's deployment audit passed, with 104 completed tool
calls, 30 completed jobs, five failed jobs, two lost jobs, and two tool errors.
Two disconnects returned closed and three returned already-lost idle terminal
states. The independent positive probes proved actual trace ancestry, twelve
correlated Go logs, matching proxy JSON, and genuine application/proxy request
counters each increasing `30 -> 42`. The dashboard checker then rejected valid
`${probe:raw}` interpolation. This failed attempt remains preserved and both VMs
stopped with empty cleanup errors:
`.lab/results/20261017T172414Z-demo-test-4f5d7baa64404044a5ca59dbb4e5d970/`.

Inspection of all eleven actual dashboard targets found seven standard
`$__rate_interval` forms and two `${probe:raw}` forms; datasource UIDs were literal
`prometheus` and `loki`, with no datasource variables. The only declared variable
was a textbox named `probe` with scalar current value `.*`. Its two Loki targets
used that value as a regex. [Grafana's raw-format contract](https://grafana.com/docs/grafana/latest/visualizations/dashboards/variables/variable-syntax/#raw)
preserves the value without datasource formatting. The checker now implements
that explicit scalar textbox raw case with a 4 KiB value bound, preserving empty
defaults and whole-name time macros. Unknown, missing, non-string, multivalue,
oversized, and unsupported-format values still fail clearly. Replacement uses
the matched variable and a callback so raw backslashes and variable-like literal
text remain verbatim. The existing dashboard regression covers these boundaries;
no application code or saved dashboard was modified.

All eleven saved targets then resolved locally. All 52 host tests, syntax, and
whitespace checks passed. A separate full positive checker diagnostic on the
unchanged fourth-attempt deployment verified every original exported source
hash, both genuine request counters `3 -> 15`, actual proxy-to-Go ancestry,
twelve correlated Go logs, matching proxy JSON, and all five dashboard checks.
It ran no model, application modification, or controlled fault, and stopped
both VMs with empty cleanup errors after 35.022s. Evidence:
`.lab/results/20261017T174417Z-cp7-positive-checker-diagnostic-17fed894669c4f339dc8db9deaae3323/`.
The fourth acceptance attempt remains failed; the next attempt requires a fresh
explicit reset and new unchanged-goal actors.

## Checkpoint 7 usage-limit interruption and continuation (2026-10-17)

The fifth fresh unchanged-goal deployment was interrupted by the installed CLI's
account usage limit after 518.863s, with exit 1 and a failed turn. Its partial
transcript contains 82 completed MCP calls, 24 completed jobs, four failed jobs,
one lost job, and one last observed accepted job; there were no tool errors.
These are partial-transcript observations, not a successful actor audit. Its
warmed sixteen-job benchmark passed in 2.156783s. Independent deployment
evaluation, controlled fault, and fresh repair did not run. The failed result,
private partial transcript/source export, and diagnostics remain preserved:
`.lab/results/20261017T174618Z-demo-test-2964cb78279f467aab22eb352839b6bb/`.
Both VMs stopped with empty cleanup errors, and the resumed Builder found no
remaining actor or forwarding process.

At the user's continuation boundary, the stored Codex account file's raw hash
differed from the earlier attempt. No credentials or global account/model
settings were changed by the resumed Builder; raw-file hashes cannot distinguish
token rotation from user reauthentication. The new private preservation record
establishes the current account-identity hash as a continuation baseline. All
management keys, host pins, account fixture/global configuration bytes, ownership,
and recorded baselines still match the original CP7 preservation record.
The subsequent explicit target-only reset passed and retained all failed-run
evidence:
`.lab/results/20261017T202427Z-demo-reset-d41781b2759242cd8b688337cbf95121/`.

## Checkpoint 7 actual deployment, fault, and repair (2026-10-17)

The sixth fresh unchanged-goal exercise passed in 900.061s, with no primary or
cleanup errors. Both owned VMs and the private demo were left running. Evidence:
`.lab/results/20261017T202530Z-demo-test-c94c230b79564965bf98acd9600e7427/`.
The successful ready pointer identifies that run. The five earlier attempts
remain failed; no diagnostic retroactively changes their status.

The deployment actor completed in 562.666s using 90 MCP calls and 30 submitted
jobs: 23 completed, five failed, and two lost. It recovered independently after
those failures and shell losses. The separate repair actor completed in
116.817s using 25 MCP calls and nine completed jobs, with no failed/lost jobs.
Both actors had zero tool/runtime errors, duplicate results, repeated execute
keys, and exact repeated source submissions. Their session IDs differ, and both
prompt-isolation audits passed. The repair prompt was exactly the single
sentence documented in `DEMO.md`. In-run human guidance was zero; the engineering
corrections between earlier attempts are separate and remain documented above.

Independent deployment checks passed fresh proxy-to-Go ancestry, twelve
trace-correlated Go logs, the matching Traefik JSON log, and all five dashboard
checks. The genuine Go and proxy counters each increased `35 -> 47` after twelve
requests. The operator changed only the application's generic OTLP endpoint and
recreated only that container. Over the complete 120-second fault window, fresh
Go spans/logs stayed zero and its counter stayed `47 -> 47`, while HTTP, matching
proxy logs, and 24 fresh proxy spans remained available. After fresh-agent repair,
new ancestry/log/dashboard checks passed again; the Go counter increased
`35 -> 47` and proxy counter `95 -> 107` after twelve new requests.

The warmed sixteen-job `sleep 2` benchmark passed in 2.136538s, with all guest
execution intervals overlapping. Its 628 real MCP calls had median/p95/max
latencies 0.004219/0.065954/0.128671s. Warm command median/max were
0.145786/0.155272s. Transport errors and automatic transport retries were zero;
this does not erase the actor's independent failure recovery. Actor JSONL does
not provide comparable per-call timing.

One separate bounded, read-only observation inspected the fixed gateway/runner
services while buffered actor output appeared delayed. Both were active; it
inspected no application files, changed no service, and provided no agent
guidance. Private evidence:
`.lab/results/20261017T203534Z-cp7-active-infrastructure-observation-670de560a0c34105b2da0005560873d6/`.

The [actual-source snapshot and report](../examples/agent-demo/README.md) contain
twenty unchanged primary export files plus the actor's actual `verify.py`.
That script was collected afterward through a separate bounded read-only MCP
operator, with no application change:
`.lab/results/20261017T204409Z-cp7-supplemental-source-aca5f4e1508e4f33b6462e7a221f3330/`.
The combined manifest distinguishes primary and supplemental collection and
records original/export hashes. All 21 published contents match the original
agent-created bytes; known credentials are absent. Original private exports and
reports are unchanged. Private Grafana access notes are only in the gitignored
`.lab/demo/access.json`, mode `0600`. The source and verification reports remain
actor artifacts; independent acceptance uses separate operator/backend evidence.

Post-run preservation checks match all protected management/configuration bytes,
ownership, host pins, and baselines against the original CP7 record. The account
identity matches the new continuation baseline; its raw auth file differs from
the pre-continuation record as disclosed above. Evidence:
`.lab/demo/cp7-attempt6-after.json`.
No deployment, Compose configuration, or Go source was hand-rewritten for export.
Modern Go guidance was read for both the repository's Go 1.26 and the actual
export's declared Go 1.25 before copying the source snapshot.

All 52 host tests passed before the exercise. Artifact hashes, JSON/Python
syntax, shell syntax, and whitespace were checked after packaging. Independent
demo down/up, fresh telemetry after resume, and live runtime privacy are reserved
for the later dedicated Tester. Fresh Translation, final validation, Tester,
and Review remain required before the checkpoint's workflow can complete.

## Checkpoint 7 final validation and independent Tester (2026-10-17)

Fresh Translation returned a factual report correction: the catalog had one
enabled server containing three tools, while one report field counted only one
tool. A fresh Builder corrected that field against the raw catalog; original
catalog, source, and provenance bytes remained unchanged. Fresh Translation
then completed without edits or unresolved readability issues.

Coordinator final checks passed all 52 host tests (0.334s), Python AST/JSON/shell
syntax, Go formatting, cached Go race tests, vet, both Linux ARM64 builds, and
whitespace checks. These checks made no runtime or application changes.

The dedicated independent Tester passed actual down (9.465s), observed both VMs
stopped with empty mounts, and passed up (75.021s). Its separate fresh post-resume
probe proved span ancestry, twelve correlated Go logs, proxy JSON logs, Go/proxy
counters 18 → 30, and all five dashboard checks. Pre-stop trace/log/counter
history, the same container IDs and named volumes, 23 private captured hashes,
and 21 public source hashes were preserved. Live privacy, ARM64 images,
instrumentation, protected keys/configuration/identities, and private credential
permissions passed. The current raw account hash matched the continuation
baseline; its earlier difference remains disclosed above.

With the application stack running, all sixteen warmed two-second jobs
overlapped and drained in 2.169478s. The benchmark recorded 634 MCP calls,
zero transport errors/retries, and sixteen closed connections. Historical actor,
exact-prompt, isolation, and fault records were independently audited separately
from these newly executed checks. Tester-only capture corrections are retained;
no application change, reset, redeployment, fault reinjection, or model actor
was introduced by the Tester.

Private evidence:
`.lab/results/20261017T210215Z-cp7-independent-tester-206c830dfb5b40c7b14dc2ae5f125d73/`.
Both VMs and the demo remain running. Independent Review and the dedicated CP7
commit are the remaining workflow gates.
