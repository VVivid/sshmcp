# Lab validation

This validates the reusable SSH/Docker infrastructure. There is no MCP application
or current application build deployed in this VM. Both checkpoint 3 security cases
passed, followed by a successful repeat `make lab-test`. The VM was left stopped.

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
