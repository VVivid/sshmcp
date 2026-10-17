#!/usr/bin/env python3
"""Manage this workspace's private Multipass lab; never delete VM disks."""

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
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
SMOKE = ROOT / "lab" / "smoke.v1.sh"
BASELINE_VERSION = 1
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


@dataclass(frozen=True)
class Profile:
    name: str
    vm: str
    directory: Path
    template: Path
    smoke: Path
    cpus: str
    memory: str
    summary: str


PROFILES = {
    "control": Profile("control", "sshmcp-test", ROOT / ".lab", TEMPLATE, SMOKE, "2", "4G", "systemd, SSH, Docker/Compose, user manager"),
    "target": Profile("target", "sshmcp-target", ROOT / ".lab" / "target", ROOT / "lab" / "target-cloud-init.v1.yaml", ROOT / "lab" / "target-smoke.v1.sh", "4", "8G", "systemd, SSH, native runner, user manager"),
}
PROFILE = PROFILES["control"]
CONTROL_HEALTH = HEALTH_SCRIPT
TARGET_HEALTH = "\n".join(line for line in CONTROL_HEALTH.splitlines() if not any(value in line for value in ("Docker daemon", "Docker Engine API", "Compose plugin"))) + r"""
check 'native runner installed' test -x /usr/local/bin/sshmcp-runner
check 'native runner user service' sudo -n -u mcp-test env \
    "XDG_RUNTIME_DIR=/run/user/$test_uid" \
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$test_uid/bus" \
    systemctl --user is-active sshmcp-runner.service
"""


def select_profile(name):
    """Select one explicit profile for this process; control remains compatible."""
    global PROFILE, LOCAL, MANIFEST, KEY, KNOWN_HOSTS, SSH_CONFIG, VM, TEMPLATE, SMOKE, HEALTH_SCRIPT
    PROFILE = PROFILES[name]
    LOCAL = PROFILE.directory
    MANIFEST, KEY = LOCAL / "state.json", LOCAL / "id_ed25519"
    KNOWN_HOSTS, SSH_CONFIG = LOCAL / "known_hosts", LOCAL / "ssh_config"
    VM, TEMPLATE, SMOKE = PROFILE.vm, PROFILE.template, PROFILE.smoke
    HEALTH_SCRIPT = TARGET_HEALTH if name == "target" else CONTROL_HEALTH


class LabError(Exception):
    """An expected failure that can be safely shown without command output."""

    def __init__(self, message, *, exit_code=1):
        super().__init__(message)
        self.exit_code = exit_code


class LabInterrupted(Exception):
    def __init__(self, signum):
        self.signum = signum
        super().__init__(f"Interrupted by {signal.Signals(signum).name}.")


@contextmanager
def interrupt_handling(*, cleanup=False):
    """Bounded cleanup completes after the first handled interrupt."""
    def interrupted(signum, frame):
        raise LabInterrupted(signum)

    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, signal.SIG_IGN if cleanup else interrupted)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run(arguments, *, label, timeout=30, input_text=None, log_path=None):
    """Keep all subprocesses bounded and never expose their possibly private output."""
    log = None
    try:
        if log_path is not None:
            write_private(log_path, f"{label}\n")
            descriptor = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
            log = os.fdopen(descriptor, "w")
        result = subprocess.run(
            [str(argument) for argument in arguments],
            input=input_text,
            text=True,
            stdout=log if log else subprocess.PIPE,
            stderr=subprocess.STDOUT if log else subprocess.PIPE,
            timeout=timeout,
            check=False,
            cwd=ROOT,
        )
    except subprocess.TimeoutExpired:
        raise LabError(f"{label} timed out after {timeout}s; inspect the VM before retrying.") from None
    except OSError as error:
        raise LabError(f"{label} could not run ({error.strerror}).") from None
    finally:
        if log is not None:
            log.close()
    if result.returncode:
        # Our readiness script emits fixed check names, never keys or markers.
        failed_check = next(
            (line for line in (result.stderr or "").splitlines() if line.startswith("Readiness check failed: ")),
            None,
        )
        detail = f" {failed_check}." if failed_check else ""
        raise LabError(f"{label} failed (exit {result.returncode}).{detail}")
    return result.stdout or ""


def multipass(*arguments, label, timeout=30, log_path=None):
    return run(["multipass", *arguments], label=label, timeout=timeout, log_path=log_path)


def guest(*arguments, label, timeout=30, input_text=None, log_path=None):
    return run(
        ["multipass", "exec", "--no-map-working-directory", VM, "--", *arguments],
        label=label,
        timeout=timeout,
        input_text=input_text,
        log_path=log_path,
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
    root_state = ROOT / ".lab"
    if root_state.is_symlink():
        raise LabError("Refusing a symlink at .lab.")
    root_state.mkdir(mode=0o700, exist_ok=True)
    ensure_private_path(root_state, directory=True)
    ensure_private_path(LOCAL, directory=True)
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    lock_path = root_state / "lock"
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
        reserved = {"lock", "target"} if PROFILE.name == "control" else set()
        if any(path.name not in reserved for path in LOCAL.iterdir()):
            raise LabError("Local lab files exist without state.json; refusing ambiguous state.")
        return None
    try:
        state = json.loads(MANIFEST.read_text())
        valid = (
            state["schema"] == 1
            and state["workspace"] == str(ROOT)
            and state["vm_name"] == VM
            and state.get("profile", "control") == PROFILE.name
            and re.fullmatch(r"[0-9a-f]{32}", state["owner"])
            and isinstance(state["created_here"], bool)
            and state["phase"] in {"prepared", "launching", "enrolled", "ready", "stopped"}
        )
        if not valid:
            raise ValueError
        if state["host_key"] is not None:
            public_key(state["host_key"])
        if "baseline" in state:
            baseline = state["baseline"]
            if (
                not isinstance(baseline, dict)
                or baseline["version"] != BASELINE_VERSION
                or not re.fullmatch(r"[0-9a-f]{32}", baseline["id"])
                or baseline["name"] != f"clean-v{BASELINE_VERSION}-{baseline['id'][:16]}"
                or baseline["comment"] != f"sshmcp-clean-v{BASELINE_VERSION}:{state['owner']}:{baseline['id']}"
                or baseline["phase"] not in {"creating", "ready"}
                or (baseline["phase"] == "ready" and not isinstance(baseline["created"], str))
            ):
                raise ValueError
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


def check_ready(state, *, wait, install=False):
    """Inspect readiness; explicit lifecycle callers may install the target runner."""
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
    configure_ssh(state, info)
    if PROFILE.name == "target" and install:
        install_runner(state)
    guest("sh", "-s", label="Managed VM readiness", timeout=60, input_text=HEALTH_SCRIPT)
    run(
        ["ssh", "-F", SSH_CONFIG, VM, "sh -s"],
        label="Strict SSH readiness", timeout=60, input_text=HEALTH_SCRIPT,
    )
    if PROFILE.name == "target":
        check_runner_ready(wait=wait)
    state["phase"] = "ready"
    state["last_error"] = None
    save_state(state)
    print(f"{VM}: ready at {state['last_ip']} (Ubuntu 24.04 ARM64, {PROFILE.summary}).")


def check_runner_ready(*, wait):
    """Probe the private RPC without allocating a shell or durable job."""
    request = json.dumps({
        "action": "health",
        "owner": {"issuer": "sshmcp-lab", "subject": "readiness"},
    }) + "\n"
    deadline = time.monotonic() + (45 if wait else 10)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            strict_ssh(
                "XDG_RUNTIME_DIR=/run/user/$(id -u) "
                "timeout --kill-after=1s 2s /usr/local/bin/sshmcp-runner rpc",
                label="Target runner RPC readiness", timeout=min(10, remaining),
                input_text=request,
            )
            return
        except LabError:
            if not wait:
                break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(.2, remaining))
    raise LabError(
        "Native runner RPC is not ready; inspect the target's sshmcp-runner.service "
        "user journal and durable storage, then retry target-up to finish installation or recovery."
    )


def prepare_state():
    if platform.machine().lower() not in {"arm64", "aarch64"}:
        raise LabError("This lab requires an ARM64 host: native Multipass must launch Ubuntu 24.04 ARM64.")
    state = {
        "schema": 1,
        "workspace": str(ROOT),
        "vm_name": VM,
        "profile": PROFILE.name,
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
        raise LabError(f"{VM} must not be Multipass's primary instance (it can acquire an automatic home mount).")
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
    print(f"Creating {VM}: Ubuntu 24.04 ARM64, {PROFILE.cpus} CPUs, {PROFILE.memory} RAM, 40GB disk; first provisioning may take several minutes.", flush=True)
    multipass(
        "launch", "24.04", "--name", VM, "--cpus", PROFILE.cpus, "--memory", PROFILE.memory, "--disk", "40G",
        "--cloud-init", str(cloud_init), "--timeout", "900",
        label="VM launch/provisioning", timeout=930,
    )


def start_owned(state, existing):
    """Create or start the recorded VM, then prove its owner and SSH pin."""
    if existing is None:
        if state is None:
            state = prepare_state()
        launch(state)
    elif state is None:
        raise LabError(f"{VM} already exists without this workspace's creation record; refusing to adopt or start it.")
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
    return state


def up(state, existing):
    state = start_owned(state, existing)
    check_ready(state, wait=True, install=True)
    if ensure_baseline(state):
        state = start_owned(state, inventory())
        check_ready(state, wait=True, install=True)
    return state


def down(state, existing):
    if existing is None:
        print(f"{VM}: absent; nothing to stop.")
        return
    if state is None:
        raise LabError(f"{VM} is not recorded as owned by this workspace; refusing to stop it.")
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
        raise LabError(f"{VM} exists but is unowned by this workspace; readiness is unverified.")
    vm_state = existing.get("state")
    if vm_state != "Running":
        print(f"{VM}: {vm_state}; not ready. Guest identity cannot be verified without starting it.")
        return
    verify_identity(state)
    check_ready(state, wait=False)


def snapshot_inventory(*, name=None):
    """Multipass 1.16 exposes snapshots through info, not a snapshots command."""
    arguments = [f"{VM}.{name}"] if name else [VM, "--snapshots"]
    try:
        data = json.loads(multipass("info", *arguments, "--format", "json", label="Baseline snapshot information"))
        if not isinstance(data, dict) or data.get("errors") != []:
            raise ValueError
        info = data["info"]
        if info == {}:
            return {}
        if not isinstance(info, dict) or set(info) != {VM}:
            raise ValueError
        snapshots = info[VM]["snapshots"]
        if not isinstance(snapshots, dict) or any(not isinstance(item, dict) for item in snapshots.values()):
            raise ValueError
        return snapshots
    except (ValueError, KeyError, TypeError):
        raise LabError("Multipass returned ambiguous snapshot metadata; the baseline was not changed.") from None


def verify_baseline(state):
    baseline = state.get("baseline")
    if baseline is None:
        raise LabError("No recorded clean baseline exists; lab-reset will not guess or create one.")
    snapshots = snapshot_inventory()
    if set(snapshots) != {baseline["name"]}:
        raise LabError("Baseline snapshot is absent or unknown snapshots exist; inspect Multipass without replacing the baseline.")
    detailed = snapshot_inventory(name=baseline["name"])
    if detailed != snapshots:
        raise LabError("Snapshot listing and detail disagree; refusing an ambiguous baseline.")
    metadata = snapshots[baseline["name"]]
    if (
        metadata.get("comment") != baseline["comment"]
        or not isinstance(metadata.get("created"), str)
        or not metadata["created"]
        or metadata.get("parent") != ""
        or metadata.get("children") != []
    ):
        raise LabError("Snapshot identity does not match the recorded clean baseline.")
    check_no_mounts(metadata)
    if baseline["phase"] == "creating":
        # An interrupted client may have completed its snapshot in multipassd.
        # Adopt only the exact pre-recorded name and owner-bound comment.
        baseline["created"] = metadata["created"]
        baseline["phase"] = "ready"
        save_state(state)
    elif baseline["created"] != metadata["created"]:
        raise LabError("The baseline creation metadata changed; refusing a replacement snapshot.")


def ensure_baseline(state):
    """Verify or create the baseline after readiness, before any test payload.

    Return True only when creation stopped the VM; the caller must restart it
    and recheck readiness before returning control or running a workload.
    """
    if state.get("baseline") is not None:
        verify_baseline(state)
        return False
    if snapshot_inventory():
        raise LabError("Unrecorded snapshots exist; refusing to choose or replace a clean baseline.")
    down(state, inventory())
    identity = uuid.uuid4().hex
    state["baseline"] = {
        "version": BASELINE_VERSION,
        "id": identity,
        "name": f"clean-v{BASELINE_VERSION}-{identity[:16]}",
        "comment": f"sshmcp-clean-v{BASELINE_VERSION}:{state['owner']}:{identity}",
        "phase": "creating",
    }
    # A timeout or interruption can outlive the snapshot client. Never issue a
    # second creation command from this intent, even if no snapshot is visible.
    save_state(state)
    multipass(
        "snapshot", VM, "--name", state["baseline"]["name"], "--comment", state["baseline"]["comment"],
        label="Clean baseline creation", timeout=240,
    )
    verify_baseline(state)
    print(f"{VM}: clean baseline v{BASELINE_VERSION} recorded; it will not be overwritten.")
    return True


def new_results(operation):
    parent = LOCAL / "results"
    ensure_private_path(parent, directory=True)
    parent.mkdir(mode=0o700, exist_ok=True)
    run_id = uuid.uuid4().hex
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = parent / f"{stamp}-{operation}-{run_id}"
    directory.mkdir(mode=0o700)
    return run_id, directory


def strict_ssh(command, *, label, timeout, input_text=None, log_path=None):
    return run(
        ["ssh", "-F", SSH_CONFIG, VM, command], label=label, timeout=timeout,
        input_text=input_text, log_path=log_path,
    )


RUNNER_UNIT = """[Unit]
Description=SSHMCP native execution runner
[Service]
ExecStart=/usr/local/bin/sshmcp-runner daemon --socket %t/sshmcp-runner.sock
Restart=on-failure
RestartSec=2
KillMode=control-group
TimeoutStopSec=45
UMask=0077
[Install]
WantedBy=default.target
"""


def install_runner(state):
    """Build and compare actual guest files, including after a baseline restore."""
    if PROFILE.name != "target":
        raise LabError("Runner installation requires the explicit target profile.")
    binary = LOCAL / "sshmcp-runner"
    ensure_private_path(binary)
    run(["env", "GOOS=linux", "GOARCH=arm64", "CGO_ENABLED=0", "go", "build", "-o", binary, "./cmd/sshmcp-runner"], label="Native ARM64 runner build", timeout=120)
    binary.chmod(0o600)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    unit_digest = hashlib.sha256(RUNNER_UNIT.encode()).hexdigest()
    installed = strict_ssh(
        'if test -f /usr/local/bin/sshmcp-runner && test -f "$HOME/.config/systemd/user/sshmcp-runner.service"; then '
        'sha256sum /usr/local/bin/sshmcp-runner "$HOME/.config/systemd/user/sshmcp-runner.service"; fi',
        label="Compare installed runner and service with current build", timeout=20,
    ).splitlines()
    if [line.split()[0] for line in installed] != [digest, unit_digest]:
        script = """set -eu
umask 077
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
base64 -d > "$HOME/.sshmcp-runner.install" <<'RUNNER_BINARY'
""" + base64.b64encode(binary.read_bytes()).decode() + """
RUNNER_BINARY
sudo -n install -o root -g root -m 0755 "$HOME/.sshmcp-runner.install" /usr/local/bin/sshmcp-runner
rm -- "$HOME/.sshmcp-runner.install"
mkdir -p -m 700 "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/sshmcp-runner.service" <<'UNIT'
""" + RUNNER_UNIT + """UNIT
systemctl --user daemon-reload
systemctl --user enable sshmcp-runner.service
systemctl --user restart sshmcp-runner.service
systemctl --user is-active sshmcp-runner.service
"""
        strict_ssh("sh -s", label="Install changed target runner and private user service", timeout=60, input_text=script)
    state["runner_installed"] = True
    state["runner_sha256"] = digest
    save_state(state)


def reinstall_runner(state, existing):
    if PROFILE.name != "target":
        raise LabError("runner-install requires --profile target; the control VM is unchanged.")
    if state is None or existing is None or state.get("baseline", {}).get("phase") != "ready":
        raise LabError("runner-install requires the existing enrolled target and its ready baseline; run target up first.")
    state = start_owned(state, existing)
    configure_ssh(state, instance_info())
    install_runner(state)
    check_ready(state, wait=True)


def run_workload(run_id, directory):
    """Run the versioned fixture through strict SSH in a bounded user unit."""
    # The unit owns the remote process lifetime even if the SSH client exits.
    # Its separate short child job is part of the smoke fixture being tested.
    command = (
        f"XDG_RUNTIME_DIR=/run/user/$(id -u) "
        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus "
        f"systemd-run --user --unit=sshmcp-workload-{run_id}.service "
        "--property=RuntimeMaxSec=360 --property=TimeoutStopSec=10 --wait --pipe --collect "
        f"bash -s -- run {run_id}"
    )
    strict_ssh(command, label="Strict SSH integration workload", timeout=390,
               input_text=SMOKE.read_text(), log_path=directory / "workload.log")


def collect_diagnostics(run_id, directory):
    # Management access is inspection only; the test payload always uses SSH.
    guest(
        "sudo", "-n", "-u", USER, "env", f"HOME=/home/{USER}",
        "bash", "-s", "--", "diagnostics", run_id,
        label="Fixed VM diagnostics", timeout=60, input_text=SMOKE.read_text(),
        log_path=directory / "diagnostics.log",
    )


def cleanup_workload(run_id, directory):
    strict_ssh(
        f"timeout --kill-after=10s 70s bash -s -- cleanup {run_id}",
        label="Run-specific fixture cleanup", timeout=90, input_text=SMOKE.read_text(),
        log_path=directory / "cleanup.log",
    )


def error_text(error):
    if isinstance(error, KeyboardInterrupt):
        return "Interrupted by SIGINT."
    if isinstance(error, (LabError, LabInterrupted, OSError)):
        return str(error)
    return f"Unexpected {type(error).__name__}; inspect the lab implementation."


def cleanup_step(action, errors):
    """Record one cleanup failure without preventing the remaining steps."""
    try:
        action()
    except (Exception, KeyboardInterrupt) as error:
        errors.append(error_text(error))


def cleanup_verified_vm(state, run_id, directory, *, workload_attempted, errors):
    """Re-prove current ownership before any diagnostics or fixture commands."""
    try:
        current = inventory()
        if current is None or current.get("state") != "Running":
            # exec may start a stopped VM on some Multipass versions. Do not
            # restart it to finish cleanup, but report the incomplete steps.
            vm_state = "absent" if current is None else current.get("state", "unknown")
            required = "diagnostics and run-specific fixture cleanup" if workload_attempted else "diagnostics"
            errors.append(f"VM is {vm_state}; unable to complete {required}.")
            return
        verify_identity(state)
    except (Exception, KeyboardInterrupt) as error:
        errors.append(error_text(error))
        return
    cleanup_step(lambda: collect_diagnostics(run_id, directory), errors)
    if workload_attempted:
        cleanup_step(lambda: cleanup_workload(run_id, directory), errors)
    # Diagnostics/fixture failure must never skip ownership-checked stop.
    cleanup_step(lambda: down(state, inventory()), errors)


def finish_result(operation, run_id, directory, primary, cleanup_errors):
    message = error_text(primary) if primary is not None else None
    write_private(directory / "result.json", json.dumps({
        "operation": operation,
        "run_id": run_id,
        "success": primary is None and not cleanup_errors,
        "primary_error": message,
        "cleanup_errors": cleanup_errors,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
    }, indent=2) + "\n")
    print(f"Private {operation} results: {directory}", flush=True)
    if primary is not None or cleanup_errors:
        details = message or "The workload completed, but cleanup or diagnostics failed."
        if cleanup_errors:
            details += " Cleanup/diagnostics errors: " + "; ".join(cleanup_errors)
        if isinstance(primary, (KeyboardInterrupt, LabInterrupted)):
            details += " A Multipass operation may continue in the background; inspect lab-status before retrying."
        if isinstance(primary, LabInterrupted):
            code = 128 + primary.signum
        elif isinstance(primary, KeyboardInterrupt):
            code = 130
        else:
            code = 1
        raise LabError(details, exit_code=code)


def test_lab(state, existing):
    if state is None and existing is None:
        state = prepare_state()
    run_id, directory = new_results("test")
    identity_verified = False
    workload_attempted = False
    primary = None
    cleanup_errors = []
    try:
        state = start_owned(state, existing)
        identity_verified = True
        check_ready(state, wait=True, install=True)
        if ensure_baseline(state):
            identity_verified = False
            state = start_owned(state, inventory())
            identity_verified = True
            check_ready(state, wait=True, install=True)
        workload_attempted = True
        run_workload(run_id, directory)
    except (Exception, KeyboardInterrupt) as error:
        primary = error
    finally:
        with interrupt_handling(cleanup=True):
            if identity_verified:
                cleanup_verified_vm(state, run_id, directory, workload_attempted=workload_attempted, errors=cleanup_errors)
    finish_result("test", run_id, directory, primary, cleanup_errors)
    print(f"{VM}: infrastructure smoke passed; run resources removed and VM stopped.")


def reset(state, existing):
    if state is None or existing is None:
        raise LabError("The owned VM and its recorded baseline must exist before lab-reset.")
    run_id, directory = new_results("reset")
    identity_verified = False
    primary = None
    cleanup_errors = []
    try:
        verify_baseline(state)
        # Identity-only start deliberately permits repairing broken Docker or
        # cloud-init health. Never restore when owner, pin, or mounts are unsafe.
        state = start_owned(state, existing)
        identity_verified = True
        check_no_mounts(instance_info())
        down(state, inventory())
        verify_baseline(state)
        check_no_mounts(instance_info())
        identity_verified = False
        multipass(
            "restore", "--destructive", f"{VM}.{state['baseline']['name']}",
            label="Recorded clean baseline restore", timeout=240, log_path=directory / "restore.log",
        )
        state = start_owned(state, inventory())
        identity_verified = True
        check_ready(state, wait=True, install=True)
    except (Exception, KeyboardInterrupt) as error:
        primary = error
    finally:
        with interrupt_handling(cleanup=True):
            if identity_verified:
                cleanup_verified_vm(state, run_id, directory, workload_attempted=False, errors=cleanup_errors)
    finish_result("reset", run_id, directory, primary, cleanup_errors)
    print(f"{VM}: clean baseline v{BASELINE_VERSION} restored, identity/readiness verified, VM stopped.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("up", "down", "status", "test", "reset", "runner-install"))
    parser.add_argument("--profile", choices=tuple(PROFILES), default="control")
    arguments = parser.parse_args()
    select_profile(arguments.profile)
    os.umask(0o077)
    state = None
    try:
        for command in ("multipass", "ssh", "ssh-keygen"):
            if shutil.which(command) is None:
                raise LabError(f"Missing prerequisite: {command}. Install Multipass separately from https://canonical.com/multipass/install and ensure the OpenSSH client and Python 3 are on PATH.")
        with interrupt_handling(), workspace_lock():
            state = load_state()
            existing = inventory()
            {"up": up, "down": down, "status": status, "test": test_lab, "reset": reset, "runner-install": reinstall_runner}[arguments.command](state, existing)
    except (LabError, OSError) as error:
        print(f"Lab error: {error}", file=sys.stderr)
        return error.exit_code if isinstance(error, LabError) else 1
    except (KeyboardInterrupt, LabInterrupted) as error:
        print("Lab command interrupted. A Multipass operation may continue in the background; inspect lab-status before retrying.", file=sys.stderr)
        return 128 + error.signum if isinstance(error, LabInterrupted) else 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
