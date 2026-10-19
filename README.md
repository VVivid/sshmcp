# SSHMCP

Version **1.0.0** provides an authenticated MCP gateway and native execution
runner for configured SSH targets, plus an opt-in testing lab.
See [CHANGELOG.md](CHANGELOG.md) for the release scope and validation limits.

This repository provides two reusable private Linux VM profiles and a native Go
execution runner. The original control lab provisions Ubuntu 24.04 ARM64 with SSH, systemd,
Docker Engine, and the Docker Compose plugin. It creates a dedicated `mcp-test`
account with its own generated SSH key, noninteractive sudo, and a lingering
systemd user manager.

The target profile provides persistent native Bash connections, durable jobs,
bounded output, idempotency, and independent cancellation. The private MCP gateway
and OAuth run on the control VM. A fresh-agent deployment/repair lifecycle and
independent evaluator are available as an explicit exercise. The control lab keeps its original
Docker/Compose infrastructure fixture.

## Requirements

- An ARM64 macOS or Linux host supported by Multipass.
- [Multipass](https://canonical.com/multipass/install), installed separately with
  its daemon running and the `multipass` command on `PATH`.
- Go 1.26 or newer for building the native target runner.
- Python 3.10 or newer, Make, and OpenSSH (`ssh` and `ssh-keygen`). Python uses only
  its standard library; no virtual environment or Python packages are required.
- At least 2 CPUs, 4GB RAM, and 40GB disk available for this VM, plus Internet
  access for Ubuntu and Docker packages during first provisioning. The first
  smoke run also pulls the public ARM64 nginx Alpine image from Docker Hub,
  pinned by registry digest in the versioned fixture.

## Use

Run these commands from the repository:

```sh
make lab-up
make lab-status
make lab-down
make lab-test
make lab-reset
```

`lab-up` creates the named VM `sshmcp-test` with 2 CPUs, 4GB RAM, and a 40GB disk,
or starts and checks the previously enrolled VM. First provisioning can take
several minutes. It waits for cloud-init and verifies Ubuntu, ARM64, systemd, SSH,
noninteractive sudo, the Docker daemon/API, Compose, and the systemd user manager.
The checks run through Multipass, then through a real SSH connection using the
generated key and strict host-key verification. On the first successful
provisioning/readiness, it immediately records the clean baseline before returning
control for interactive use: stop, snapshot, start, and recheck readiness. Later
`lab-up` calls verify the existing baseline without replacing it.

`lab-status` inspects the current VM and checks readiness when it is running. It
reports an absent or stopped VM as **not ready**, and does not start it. Failed
identity, provisioning, or readiness checks return an error. An absent or stopped
VM is a normal status result, not a claim that services are ready.

`lab-down` verifies a running VM's identity before stopping it. Repeated stops
are harmless. The disk and its contents remain available for the next `lab-up`.
These commands never delete or purge VMs, and never target all instances.

`lab-test` starts or provisions the owned VM, checks readiness, and establishes a
clean baseline before running any test payload. For the first baseline it stops
the VM, takes one snapshot, then starts and verifies it again. It streams the
versioned [smoke fixture](lab/smoke.v1.sh) through strict SSH and checks:

- The dedicated account can execute commands through its pinned SSH connection.
- A short systemd user job completes successfully.
- Docker Compose pulls/starts an HTTP container, passes its healthcheck, returns
  the expected file through a port bound only to `127.0.0.1` **inside the VM**,
  exposes its logs, and shuts down explicitly.

The guest directory, systemd units, and Compose project have a fresh run ID.
Cleanup collects diagnostics first, stops that run's workload unit, removes only
that run's fixture resources, and stops the ownership-verified VM. Repeated tests
reuse the disk and image cache. They do not automatically restore the entire VM.
The versioned smoke fixture pins the successfully validated public image digest;
the baseline covers the provisioned VM before that image was pulled.

`lab-reset` is explicitly destructive to **all guest disk changes made after the
recorded clean baseline**, including manually created guest files and pulled
container images. It never discards host files or replaces the VM. It checks the
recorded snapshot's name, owner-bound comment, creation metadata, and mounts;
starts the VM if needed to verify its current owner and SSH pin; stops it; and
restores that exact snapshot. It then starts the restored VM, verifies identity
and full readiness, and stops it again. The owner marker, dedicated SSH key, and
pinned guest host key remain the same. A test after reset may need to pull the
public image again.

Reset intentionally checks identity without requiring Docker health before
restore. It can repair broken Docker when the VM still boots and its ownership
can be proven. It refuses absent or unrecorded baselines, extra/unknown snapshots,
changed metadata, mounts, or owner/key mismatches. If the VM cannot boot far enough
to prove its owner and pin, reset cannot safely repair it; inspect it manually.
It never deletes/recreates the VM or rewrites the pin to recover.

Each test/reset reports a private directory under `.lab/results/`. Test runs save
`workload.log`, fixed `diagnostics.log`, `cleanup.log`, and `result.json`; reset
runs save restore/diagnostic output and a result. Diagnostics include cloud-init
status, SSH/Docker journals, the run's systemd journal, and fixture status/logs.
They do not dump environment variables or SSH key files. Directories are `0700`
and files are `0600`. A failure before guest identity is proven can only save a
local result; it cannot safely inspect, clean, or stop an unverified VM. Review
the result file for the original error and any separate diagnostics/cleanup errors.

After successful `lab-up`, an interactive connection is available with:

```sh
ssh -F .lab/ssh_config sshmcp-test
```

## Native execution target (checkpoint 4)

The default `lab-*` commands continue to manage `sshmcp-test`. Explicit target
commands manage `sshmcp-target` with Ubuntu 24.04 ARM64, 4 CPUs, 8GB RAM, and a
40GB disk. Its clean baseline contains SSH, systemd, the lingering test account,
and the native runner. It contains no Docker or application payload. Provisioning
builds the current Go runner for Linux ARM64, installs it over the enrolled SSH
connection, and records the baseline after runner readiness.

```sh
make target-up
make target-status
make target-test
make target-runner-install
make target-down
```

Target keys, host pin, manifest, baseline identity, and results live separately
under `.lab/target/`. The explicit CLI equivalent is
`python3 tools/lab.py up --profile target`; `--profile control` is the default.
Both profiles share one lifecycle lock. Ownership checks, private NAT, no host
mounts, strict pinned SSH, and exact snapshot/reset rules apply to both.

`target-up` and `target-test` build the current runner and compare the actual
guest binary/service hashes, upgrading only when they differ. `target-test`
runs a small real SSH/runner smoke and stops the target afterward.
`target-status` only inspects the running target, including a bounded private
RPC health request. It never builds, installs, or restarts the runner, even if
an interrupted provision left its local installation record incomplete.
Lifecycle commands wait up to 45 seconds for RPC recovery; status makes one
attempt with a 10-second host deadline. Health requests create no shell or job.
`target-runner-install` explicitly performs the same build/hash check. Changed
runner installations restart its service; live target shells become lost. The
original baseline is preserved.
`target-reset` destructively restores that target's original baseline, checks
the current runner build, and stops it. The baseline snapshot itself is retained.
The original control VM is never reset by these commands.

For the full current-build CP4 check, including 16 concurrent jobs, independent
cancellation, daemon crash, and a target reboot:

```sh
make target-up
make target-runner-install
python3 tests/integration_runner.py -v
```

This opt-in harness uses only the target, never restores a baseline, removes its
own marker files, and stops the target after each case. The crash and reboot
cases intentionally invalidate live target connections. Run them when other
interactive target shells can be discarded. See [lab/RUNNER.md](lab/RUNNER.md)
for the local RPC contract, execution model, limits, and failure behavior.

## Ownership and SSH trust

Generated files live in gitignored `.lab/`, with directory permissions `0700` and
file permissions `0600`. This includes a local creation manifest, a dedicated
test private key, rendered cloud-init, SSH configuration, and pinned known-host
data. Cloud-init receives the **public** key only. Keep `.lab/` with this
workspace; removing or moving it breaks the ownership record. The tool refuses
to adopt an existing VM merely because its name is `sshmcp-test`.

The optional baseline record extends the existing schema-1 manifest. Its unique
versioned snapshot name and comment are persisted **before** issuing the snapshot
command. Later runs verify and reuse it; they never overwrite it. If snapshot
creation is interrupted, a later command accepts only the exact recorded identity
when its complete metadata is visible. Missing or ambiguous creation state fails
closed instead of issuing another snapshot. This uses Multipass 1.16's
`info --snapshots` and `info INSTANCE.SNAPSHOT` interfaces.

Creation records a random owner marker locally and in `/etc/sshmcp-lab/owner` in
the guest. The tool obtains the SSH host public key through Multipass's management
connection and pins it locally. Future checks compare both the owner marker and
the existing host-key pin. They never obtain trust from an unauthenticated network
scan. IP changes are discovered through Multipass; the SSH configuration is
refreshed only after the owner and pinned key still match. The host-key alias
retains the same pin across private IP changes.

Multipass does not expose the guest marker while a VM is stopped. A stopped VM
is started for immediate identity verification **only** when this workspace has
a prior successful creation record and an enrolled host key. If either guest
identity check differs after that start, the tool reports an error and makes no
further VM changes; it cannot undo the verification start. A stopped partial
launch without enrollment is deliberately refused. This is a local development
ownership safeguard, not protection against an administrator deliberately replacing
both local and guest records.

## Isolation and failure behavior

The VM uses Multipass's default private NAT network. Provisioning does not add a
bridge, public interface, port forward, or host filesystem mount. The tool refuses
to create the VM if `sshmcp-test` is configured as Multipass's primary instance,
which can acquire an automatic home-directory mount. Readiness also checks that
no host mounts are present. Before starting a stopped VM, `lab-up` checks its
configured mounts through Multipass and refuses host mounts or unknown mount
information. Docker is installed inside the VM; Docker Desktop on the host is
not needed.

Lifecycle commands hold a workspace lock to prevent overlapping operations. Every
subprocess has a deadline. A launch timeout or interruption may leave provisioning
running inside Multipass, so inspect `make lab-status` before retrying. Failed
provisioning is an error, never a ready result. No failure handler deletes the VM,
rewrites a mismatched identity, rotates the pinned host key, or substitutes a new
private key. If the local record is damaged, the name is occupied by an unrelated
VM, or an interrupted launch cannot be proven to belong to this workspace, inspect
the state manually before taking further action. Provisioning is a first-boot
recipe; changing it does not repair a VM that already failed cloud-init. For a
fresh disposable lab, preserve diagnostic evidence and verify ownership before
manually stopping and recreating that one VM and its local state. The lifecycle
commands deliberately do not automate destructive recovery.

Tests and reset preserve a nonzero exit status on workload, diagnostics, cleanup,
or stop failure. They attempt diagnostics before fixture removal and attempt the
ownership-checked stop even when diagnostic collection or fixture cleanup fails.
A handled `SIGINT`/`SIGTERM` during the operation enters bounded cleanup and exits
with status 130/143. `SIGINT`/`SIGTERM` received during cleanup are ignored so it can
finish; `SIGKILL`, host shutdown, or daemon failure cannot guarantee cleanup.
The remote workload also has a six-minute systemd runtime limit. An interrupted
Multipass client can leave an operation running in its daemon; if ownership was
not established, the tool reports the error without trying to stop that VM.
No repository/home directory is mounted from the host. The HTTP container mounts
only its run-specific fixture file already copied into the guest.

The versioned recipe is [lab/cloud-init.v1.yaml](lab/cloud-init.v1.yaml), and the
entry point is [tools/lab.py](tools/lab.py). Docker uses its
[official Ubuntu APT repository and keyring](https://docs.docker.com/engine/install/ubuntu/#install-using-the-apt-repository).
The recipe is versioned; Ubuntu 24.04 image builds and package versions can evolve
between fresh provisions.

## Checkpoints

1. **Provision and manage:** versioned cloud-init, owned VM creation, private SSH
   trust, readiness, up/down/status, and this usage guide.
2. **Test lifecycle:** strict SSH integration checks, private diagnostic logs, and a
   baseline snapshot/reset workflow.
3. **Validate the infrastructure:** a modest set of failure and repeatability checks.
4. **Native execution foundation:** separate target profile, persistent Bash,
   durable jobs/output/idempotency, cancellation, deadlines, and recovery.
5. **Private authenticated MCP:** official Go SDK gateway, three tools, native
   SSH, private Keycloak/Postgres, local Codex OAuth, and an opt-in integration
   suite. Run `make mcp-lab-up`; see [lab/MCP.md](lab/MCP.md) for login and contracts.
6. **Simulation and evaluator:** `make agent-demo-test`, `agent-demo-up`,
   `agent-demo-down`, and explicit target-only `agent-demo-reset`; fresh isolated
   actors, separate fault operator, real telemetry evaluation, and private evidence
   exports. See [lab/DEMO.md](lab/DEMO.md).
7. **Actual agent exercise:** a sixth fresh deployment attempt passed the full
   independent telemetry checks, controlled 120-second fault, and a separate
   fresh repair. The [actual source and report](examples/agent-demo/README.md)
   preserve all five earlier failed attempts and the agent-created snapshot.

The separate opt-in `make agent-demo-operations-test` reuses that accepted demo
for three fresh upgrade, exact rollback, and interrupted activation actors.
Its [operations contract](lab/DEMO.md#commands-and-state) requires independent
version/image/source, telemetry, persistence, and original-job recovery evidence;
it does not reset or seed the target. Implemented machinery and live acceptance
remain separate checkpoints.

Checkpoints 1–3 implement the original control infrastructure. Checkpoint 4 adds
the target runner and its focused checks. Checkpoint 5 adds private authenticated
MCP access. Checkpoint 6 supplies the exercise machinery; checkpoint 7 records
the actual deployment and repair acceptance run. A passing default `lab-test`
continues to cover only the control infrastructure fixture.

## Development checks

The normal focused regression test and syntax checks do not start a VM:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile tools/lab.py tests/test_lab.py tests/integration_lab.py
bash -n lab/smoke.v1.sh
```

The following opt-in harness uses the real VM and **restores its baseline**. Run
it when guest changes are disposable and no other lab operation is running:

```sh
python3 tests/integration_lab.py -v
```

Its five cases cover:

- An expected workload failure and real handled `SIGTERM`. These two cases patch
  only the Python workload boundary after the real fixture completes, restart
  that run's Compose service, then inject failure or the signal. They assert
  private failure results, removed active run resources, and a stopped VM.
- Reset removing a guest marker while repairing deliberately stopped Docker,
  with ownership, SSH trust, and baseline identity preserved.
- A real SSH attempt with a disposable, unapproved Ed25519 key. It uses only that
  key, requires the existing host pin, asserts public-key authentication denial,
  and checks through authorized SSH that the attempted guest command did not run.
- Refusal to stop the real running VM when an in-memory copy of its owner or host
  pin is wrong. Both refusals must leave the VM running and the local manifest,
  key, and pinned SSH configuration unchanged before an authorized final stop.

The two security cases require an existing enrolled VM and a recorded ready
baseline; they do not create or restore a baseline. To validate checkpoint 3
without repeating the three deeper lifecycle cases:

```sh
python3 tests/integration_lab.py -v \
  RealLabLifecycleTest.test_unapproved_ssh_key_cannot_execute \
  RealLabLifecycleTest.test_down_refuses_owner_and_host_pin_mismatches
make lab-test
```

Both new cases remove their disposable local keys and stop the ownership-verified
VM in a final cleanup step. SSH key rejection tests infrastructure authentication;
the owner/pin cases test local lab management safeguards. They do not test MCP
principal ownership, OAuth, application authentication, persistent shells, or
16 concurrent sessions. The harness is separate from ordinary test discovery and
adds no production fault-injection switches.

See [lab/VALIDATION.md](lab/VALIDATION.md) for reading order, reproducible commands,
recorded evidence, and the limits of what has been exercised.
