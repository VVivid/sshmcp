#!/usr/bin/env python3
"""Manage this workspace's private Multipass lab; never delete VM disks."""

import argparse
import base64
from contextlib import contextmanager
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".lab"
MANIFEST = LOCAL / "state.json"
KEY = LOCAL / "id_ed25519"
KNOWN_HOSTS = LOCAL / "known_hosts"
SSH_CONFIG = LOCAL / "ssh_config"
VM = "sshmcp-test"
USER = "mcp-test"
OWNER_FILE = "/etc/sshmcp-lab/owner"
TEMPLATE = ROOT / "lab" / "cloud-init.v1.yaml"
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)

# This runs through both Multipass and real SSH. No image pull or test workload
# is needed to establish that the installed services and account are usable.
HEALTH_SCRIPT = r"""set -eu
check() {
    label=$1
    shift
    if ! "$@" >/dev/null 2>&1; then
        printf 'Readiness check failed: %s\n' "$label" >&2
        exit 1
    fi
}
. /etc/os-release
check 'Ubuntu 24.04' test "$ID:$VERSION_ID" = ubuntu:24.04
check 'ARM64' test "$(dpkg --print-architecture)" = arm64
check 'systemd PID 1' test "$(cat /proc/1/comm)" = systemd
check 'provisioning v1 completed' test "$(sudo -n cat /etc/sshmcp-lab/provisioned)" = 1
check 'SSH daemon' systemctl is-active ssh.service
check 'Docker daemon' systemctl is-active docker.service
check 'mcp-test noninteractive sudo' sudo -n -u mcp-test sudo -n true
check 'Docker Engine API' sudo -n -u mcp-test sudo -n docker info
check 'Compose plugin' sudo -n -u mcp-test sudo -n docker compose version
test_uid=$(id -u mcp-test)
check 'user lingering' test "$(loginctl show-user mcp-test -p Linger --value)" = yes
check 'systemd user manager' sudo -n -u mcp-test env \
    "XDG_RUNTIME_DIR=/run/user/$test_uid" \
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$test_uid/bus" \
    systemctl --user is-active default.target
"""


class LabError(Exception):
    """An expected failure that can be safely shown without command output."""


def run(arguments, *, label, timeout=30, input_text=None):
    """Keep all subprocesses bounded and never expose their possibly private output."""
    try:
        result = subprocess.run(
            [str(argument) for argument in arguments],
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            cwd=ROOT,
        )
    except subprocess.TimeoutExpired:
        raise LabError(f"{label} timed out after {timeout}s; inspect the VM before retrying.") from None
    except OSError as error:
        raise LabError(f"{label} could not run ({error.strerror}).") from None
    if result.returncode:
        # Our readiness script emits fixed check names, never keys or markers.
        failed_check = next(
            (line for line in result.stderr.splitlines() if line.startswith("Readiness check failed: ")),
            None,
        )
        detail = f" {failed_check}." if failed_check else ""
        raise LabError(f"{label} failed (exit {result.returncode}).{detail}")
    return result.stdout


def multipass(*arguments, label, timeout=30):
    return run(["multipass", *arguments], label=label, timeout=timeout)


def guest(*arguments, label, timeout=30, input_text=None):
    return run(
        ["multipass", "exec", "--no-map-working-directory", VM, "--", *arguments],
        label=label,
        timeout=timeout,
        input_text=input_text,
    )


def ensure_private_path(path, *, directory=False):
    """Refuse symlinks and foreign-owned state before reading or writing it."""
    if path.is_symlink():
        raise LabError(f"Refusing symlink in local lab state: {path.name}.")
    if not path.exists():
        return
    info = path.stat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode) or info.st_uid != os.getuid():
        raise LabError(f"Local lab state has an unexpected owner or type: {path.name}.")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise LabError(f"Local lab state must be private: chmod {'700' if directory else '600'} {path}.")


@contextmanager
def workspace_lock():
    if LOCAL.is_symlink():
        raise LabError("Refusing a symlink at .lab.")
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    ensure_private_path(LOCAL, directory=True)
    lock_path = LOCAL / "lock"
    ensure_private_path(lock_path)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LabError("Another lab command holds this workspace's lifecycle lock.") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def write_private(path, content):
    ensure_private_path(path)
    temporary = path.with_name(path.name + ".tmp")
    ensure_private_path(temporary)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def save_state(state):
    write_private(MANIFEST, json.dumps(state, indent=2) + "\n")


def public_key(value):
    fields = value.strip().split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise LabError("Expected an Ed25519 SSH public key; refusing ambiguous key data.")
    try:
        decoded = base64.b64decode(fields[1], validate=True)
    except ValueError:
        raise LabError("Invalid SSH public key encoding.") from None
    if len(decoded) != 51 or decoded[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
        raise LabError("Invalid Ed25519 SSH public key data.")
    return " ".join(fields[:2])


def load_state():
    ensure_private_path(MANIFEST)
    if not MANIFEST.exists():
        # Do not overwrite a key or enrollment left behind by a damaged manifest.
        if any(path.name != "lock" for path in LOCAL.iterdir()):
            raise LabError("Local lab files exist without state.json; refusing ambiguous state.")
        return None
    try:
        state = json.loads(MANIFEST.read_text())
        valid = (
            state["schema"] == 1
            and state["workspace"] == str(ROOT)
            and state["vm_name"] == VM
            and re.fullmatch(r"[0-9a-f]{32}", state["owner"])
            and isinstance(state["created_here"], bool)
            and state["phase"] in {"prepared", "launching", "enrolled", "ready", "stopped"}
        )
        if not valid:
            raise ValueError
        if state["host_key"] is not None:
            public_key(state["host_key"])
        for path in (KEY, KEY.with_suffix(".pub"), KNOWN_HOSTS, SSH_CONFIG):
            ensure_private_path(path)
    except (ValueError, KeyError, TypeError):
        raise LabError("Invalid or foreign local lab manifest; refusing to manage the named VM.") from None
    return state


def inventory():
    try:
        data = json.loads(multipass("list", "--format", "json", label="Multipass inventory"))
        if not isinstance(data, dict):
            raise ValueError
        entries = data["list"]
        if not isinstance(entries, list) or data.get("errors"):
            raise ValueError
        matches = [item for item in entries if item["name"] == VM]
        if len(matches) > 1:
            raise ValueError
        return matches[0] if matches else None
    except (ValueError, KeyError, TypeError):
        raise LabError("Multipass returned ambiguous inventory; no lifecycle action was taken.") from None


def instance_info():
    try:
        data = json.loads(multipass("info", VM, "--format", "json", label="Multipass VM information"))
        if not isinstance(data, dict) or data.get("errors"):
            raise ValueError
        info = data["info"][VM]
        if not isinstance(info, dict) or not isinstance(info["state"], str):
            raise ValueError
        return info
    except (ValueError, KeyError, TypeError):
        raise LabError("Multipass returned ambiguous VM information.") from None


def verify_identity(state, *, enroll=False):
    owner = guest("sudo", "-n", "cat", OWNER_FILE, label="VM ownership verification").strip()
    if owner != state["owner"]:
        raise LabError("The VM owner marker does not match this workspace; no further VM action was taken.")
    host_key = public_key(
        guest("sudo", "-n", "cat", "/etc/ssh/ssh_host_ed25519_key.pub", label="Managed SSH host-key verification")
    )
    if state["host_key"] is None:
        if not enroll or state["phase"] != "launching":
            raise LabError("The VM has no prior SSH host-key enrollment; run lab-up to inspect its creation state.")
        state["host_key"] = host_key
        state["created_here"] = True
        state["phase"] = "enrolled"
        save_state(state)
    elif host_key != state["host_key"] or not state["created_here"]:
        raise LabError("The VM SSH host key differs from its saved pin; no further VM action was taken.")


def check_no_mounts(info):
    mounts = info.get("mounts")
    if not isinstance(mounts, dict) or mounts:
        raise LabError("The lab VM has host mounts or unknown mount information; refusing lab readiness.")


def configure_ssh(state, info):
    # Route lookup does not send a packet. It selects the NAT interface rather
    # than Docker's additional addresses, which can also appear in `info`.
    try:
        routes = json.loads(guest("ip", "-j", "route", "get", "1.1.1.1", label="VM private address discovery"))
        address = ipaddress.ip_address(routes[0]["prefsrc"])
        if address.version != 4 or not any(address in network for network in PRIVATE_NETWORKS):
            raise ValueError
        if str(address) not in info["ipv4"]:
            raise ValueError
    except (ValueError, KeyError, TypeError, IndexError):
        raise LabError("Cannot establish the VM's private NAT address; SSH configuration was not changed.") from None
    write_private(KNOWN_HOSTS, f"{VM} {state['host_key']}\n")

    def quote_path(path):
        return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'

    write_private(
        SSH_CONFIG,
        f"""Host {VM}
    HostName {address}
    User {USER}
    Port 22
    IdentityFile {quote_path(KEY)}
    IdentitiesOnly yes
    BatchMode yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    StrictHostKeyChecking yes
    HostKeyAlias {VM}
    HostKeyAlgorithms ssh-ed25519
    UserKnownHostsFile {quote_path(KNOWN_HOSTS)}
    GlobalKnownHostsFile /dev/null
    UpdateHostKeys no
    ConnectTimeout 8
    ConnectionAttempts 1
    ServerAliveInterval 5
    ServerAliveCountMax 2
    ForwardAgent no
    ClearAllForwardings yes
    RequestTTY no
""",
    )
    state["last_ip"] = str(address)
    save_state(state)


def check_ready(state, *, wait):
    info = instance_info()
    if info["state"] != "Running":
        raise LabError(f"{VM} is {info['state']}; readiness is unavailable.")
    check_no_mounts(info)
    if wait:
        guest(
            "sudo", "-n", "timeout", "600", "cloud-init", "status", "--wait",
            label="cloud-init completion", timeout=620,
        )
    try:
        cloud = json.loads(guest("cloud-init", "status", "--format", "json", label="cloud-init health"))
        if cloud["status"] != "done":
            raise LabError(f"cloud-init is {cloud['status']}; the lab is not ready.")
    except (ValueError, KeyError, TypeError):
        raise LabError("Cannot establish cloud-init completion; the lab is not ready.") from None
    guest("sh", "-s", label="Managed VM readiness", timeout=60, input_text=HEALTH_SCRIPT)
    configure_ssh(state, info)
    run(
        ["ssh", "-F", SSH_CONFIG, VM, "sh -s"],
        label="Strict SSH readiness", timeout=60, input_text=HEALTH_SCRIPT,
    )
    state["phase"] = "ready"
    state["last_error"] = None
    save_state(state)
    print(f"{VM}: ready at {state['last_ip']} (Ubuntu 24.04 ARM64, systemd, SSH, Docker/Compose, user manager).")


def prepare_state():
    if platform.machine().lower() not in {"arm64", "aarch64"}:
        raise LabError("This lab requires an ARM64 host: native Multipass must launch Ubuntu 24.04 ARM64.")
    state = {
        "schema": 1,
        "workspace": str(ROOT),
        "vm_name": VM,
        "owner": uuid.uuid4().hex,
        "phase": "prepared",
        "created_here": False,
        "host_key": None,
        "last_ip": None,
        "last_error": None,
    }
    save_state(state)
    return state


def launch(state):
    if state["phase"] != "prepared":
        raise LabError("A prior creation record exists but the VM is absent; inspect it manually before recreating anything.")
    primary = multipass("get", "client.primary-name", label="Multipass primary-instance check").strip()
    if not primary or primary == VM:
        raise LabError("sshmcp-test must not be Multipass's primary instance (it can acquire an automatic home mount).")
    for path in (KEY, KEY.with_suffix(".pub")):
        ensure_private_path(path)
    if not KEY.exists() and not KEY.with_suffix(".pub").exists():
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", VM, "-f", KEY], label="Test SSH key generation")
        KEY.chmod(0o600)
        KEY.with_suffix(".pub").chmod(0o600)
    if not KEY.exists() or not KEY.with_suffix(".pub").exists():
        raise LabError("The generated SSH key pair is incomplete; refusing to replace it.")
    supplied_key = public_key(KEY.with_suffix(".pub").read_text())
    derived_key = public_key(run(["ssh-keygen", "-y", "-P", "", "-f", KEY], label="Test SSH key-pair check"))
    if supplied_key != derived_key:
        raise LabError("The test SSH public and private keys do not match.")
    rendered = TEMPLATE.read_text().replace("@@PUBLIC_KEY@@", json.dumps(supplied_key)).replace("@@OWNER@@", state["owner"])
    cloud_init = LOCAL / "cloud-init.yaml"
    write_private(cloud_init, rendered)
    # Persist intent first: a timed-out launch can continue inside multipassd.
    # A retry must inspect the named VM rather than issue a second launch.
    state["phase"] = "launching"
    save_state(state)
    print(f"Creating {VM}: Ubuntu 24.04 ARM64, 2 CPUs, 4GB RAM, 40GB disk; first provisioning may take several minutes.", flush=True)
    multipass(
        "launch", "24.04", "--name", VM, "--cpus", "2", "--memory", "4G", "--disk", "40G",
        "--cloud-init", str(cloud_init), "--timeout", "900",
        label="VM launch/provisioning", timeout=930,
    )


def up(state, existing):
    if existing is None:
        if state is None:
            state = prepare_state()
        launch(state)
    elif state is None:
        raise LabError("sshmcp-test already exists without this workspace's creation record; refusing to adopt or start it.")
    else:
        vm_state = existing.get("state")
        if vm_state == "Stopped":
            if not state["created_here"] or not state["host_key"]:
                raise LabError("Stopped VM lacks prior ownership and host-key enrollment; refusing to start ambiguous provisioning state.")
        elif vm_state != "Running":
            raise LabError(f"VM is {vm_state}; no lifecycle action is safe until its state is resolved.")
        # Multipass exposes configured mounts while stopped; reject them before
        # a start could activate host filesystem access.
        check_no_mounts(instance_info())
        if vm_state == "Stopped":
            print(f"Starting {VM} for immediate ownership and pinned-key verification.", flush=True)
            multipass("start", VM, "--timeout", "180", label="VM verification start", timeout=200)
    verify_identity(state, enroll=True)
    check_ready(state, wait=True)


def down(state, existing):
    if existing is None:
        print(f"{VM}: absent; nothing to stop.")
        return
    if state is None:
        raise LabError("sshmcp-test is not recorded as owned by this workspace; refusing to stop it.")
    vm_state = existing.get("state")
    if vm_state == "Stopped":
        print(f"{VM}: already stopped; disk retained. Guest identity and readiness cannot be checked while stopped.")
        return
    if vm_state != "Running":
        raise LabError(f"VM is {vm_state}; refusing to stop an ambiguous instance state.")
    verify_identity(state, enroll=state["phase"] == "launching")
    multipass("stop", VM, label="Owned VM stop", timeout=110)
    if instance_info()["state"] != "Stopped":
        raise LabError("Stop returned but the VM is not stopped; inspect Multipass state.")
    state["phase"] = "stopped"
    save_state(state)
    print(f"{VM}: stopped; disk retained.")


def status(state, existing):
    if existing is None:
        print(f"{VM}: absent; not ready.")
        return
    if state is None:
        raise LabError("sshmcp-test exists but is unowned by this workspace; readiness is unverified.")
    vm_state = existing.get("state")
    if vm_state != "Running":
        print(f"{VM}: {vm_state}; not ready. Guest identity cannot be verified without starting it.")
        return
    verify_identity(state)
    check_ready(state, wait=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("up", "down", "status"))
    arguments = parser.parse_args()
    os.umask(0o077)
    state = None
    try:
        for command in ("multipass", "ssh", "ssh-keygen"):
            if shutil.which(command) is None:
                raise LabError(f"Missing prerequisite: {command}. Install Multipass separately from https://canonical.com/multipass/install and ensure the OpenSSH client and Python 3 are on PATH.")
        with workspace_lock():
            state = load_state()
            existing = inventory()
            {"up": up, "down": down, "status": status}[arguments.command](state, existing)
    except (LabError, OSError) as error:
        print(f"Lab error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Lab command interrupted. A Multipass operation may continue in the background; inspect lab-status before retrying.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
