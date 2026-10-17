# SSHMCP testing lab

This repository currently provides a reusable, private Linux VM for later SSHMCP
development. The first milestone provisions Ubuntu 24.04 ARM64 with SSH, systemd,
Docker Engine, and the Docker Compose plugin. It creates a dedicated `mcp-test`
account with its own generated SSH key, noninteractive sudo, and a lingering
systemd user manager.

The MCP server, application deployment, persistent shell sessions, and MCP tests
are future work. None are implemented in this checkpoint.

## Requirements

- An ARM64 macOS or Linux host supported by Multipass.
- [Multipass](https://canonical.com/multipass/install), installed separately with
  its daemon running and the `multipass` command on `PATH`.
- Python 3.10 or newer, Make, and OpenSSH (`ssh` and `ssh-keygen`). Python uses only
  its standard library; no virtual environment or Python packages are required.
- At least 2 CPUs, 4GB RAM, and 40GB disk available for this VM, plus Internet
  access for Ubuntu and Docker packages during first provisioning.

## Use

Run these commands from the repository:

```sh
make lab-up
make lab-status
make lab-down
```

`lab-up` creates the named VM `sshmcp-test` with 2 CPUs, 4GB RAM, and a 40GB disk,
or starts and checks the previously enrolled VM. First provisioning can take
several minutes. It waits for cloud-init and verifies Ubuntu, ARM64, systemd, SSH,
noninteractive sudo, the Docker daemon/API, Compose, and the systemd user manager.
The checks run through Multipass, then through a real SSH connection using the
generated key and strict host-key verification.

`lab-status` inspects the current VM and checks readiness when it is running. It
reports an absent or stopped VM as **not ready**, and does not start it. Failed
identity, provisioning, or readiness checks return an error. An absent or stopped
VM is a normal status result, not a claim that services are ready.

`lab-down` verifies a running VM's identity before stopping it. Repeated stops
are harmless. The disk and its contents remain available for the next `lab-up`.
These commands never delete or purge VMs, and never target all instances.

After successful `lab-up`, an interactive connection is available with:

```sh
ssh -F .lab/ssh_config sshmcp-test
```

## Ownership and SSH trust

Generated files live in gitignored `.lab/`, with directory permissions `0700` and
file permissions `0600`. This includes a local creation manifest, a dedicated
test private key, rendered cloud-init, SSH configuration, and pinned known-host
data. Cloud-init receives the **public** key only. Keep `.lab/` with this
workspace; removing or moving it breaks the ownership record. The tool refuses
to adopt an existing VM merely because its name is `sshmcp-test`.

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

The versioned recipe is [lab/cloud-init.v1.yaml](lab/cloud-init.v1.yaml), and the
entry point is [tools/lab.py](tools/lab.py). Docker uses its
[official Ubuntu APT repository and keyring](https://docs.docker.com/engine/install/ubuntu/#install-using-the-apt-repository).
The recipe is versioned; Ubuntu 24.04 image builds and package versions can evolve
between fresh provisions.

## Checkpoints

1. **Provision and manage:** versioned cloud-init, owned VM creation, private SSH
   trust, readiness, up/down/status, and this usage guide.
2. **Test lifecycle:** direct Multipass integration checks, diagnostic logs, and a
   baseline snapshot/reset workflow.
3. **Validate and hand off:** a modest set of failure and repeatability checks,
   verified usage instructions, and final handoff.

Checkpoint 1 implements only the first item. `lab-test`, snapshots, and reset
commands arrive in checkpoint 2.
