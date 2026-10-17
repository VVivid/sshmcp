#!/usr/bin/env python3
"""Private CP5 gateway/auth lifecycle; uses the existing owned lab profiles."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

import lab


PRIVATE = lab.ROOT / ".lab" / "mcp"
RESOURCE = "http://127.0.0.1:18080/mcp"
ISSUER = "http://127.0.0.1:18081/realms/sshmcp"
CALLBACK = "http://127.0.0.1:18443/callback"
CLIENT = "sshmcp-codex"
SERVER_NAME = "sshmcp-private"
REMOTE = "/home/mcp-test/.sshmcp-control"
TUNNEL = PRIVATE / "tunnel.sock"
GATEWAY_UNIT = f"""[Unit]
Description=Private SSHMCP gateway
After=network-online.target
[Service]
ExecStart=/usr/local/bin/sshmcp-gateway --config {REMOTE}/gateway.json
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=yes
[Install]
WantedBy=default.target
"""


def profile(name):
    """Profile selection is process-global; all calls are serial under one lock."""
    lab.select_profile(name)
    return lab.load_state(), lab.inventory()


def private_directory():
    lab.ensure_private_path(PRIVATE, directory=True)
    PRIVATE.mkdir(mode=0o700, exist_ok=True)


def require_enrolled_profiles():
    """CP5 adds services only after the existing CP1/CP4 trust baselines exist."""
    try:
        for name in ("control", "target"):
            lab.select_profile(name)
            if not lab.MANIFEST.exists():
                raise lab.LabError("Enroll both profiles first with make lab-up and make target-up before mcp-lab-up.")
            state = lab.load_state()
            if not state.get("created_here") or not state.get("host_key") or state.get("baseline", {}).get("phase") != "ready":
                raise lab.LabError("Both profiles need their existing enrolled trust and ready baselines before mcp-lab-up.")
    finally:
        lab.select_profile("control")


def accounts():
    path = PRIVATE / "accounts.json"
    lab.ensure_private_path(path)
    if not path.exists():
        lab.write_private(path, json.dumps({name: secrets.token_urlsafe(30) for name in ("admin", "other")}) + "\n")
    value = json.loads(path.read_text())
    if set(value) != {"admin", "other"} or any(not isinstance(v, str) or len(v) < 30 for v in value.values()):
        raise lab.LabError("Invalid private test-account record; refusing to replace it.")
    return value


def realm_record(passwords):
    # Resource Indicators remains experimental in the pinned Keycloak release.
    # The resource server client supplies the resource_url mapping; the RP's
    # Included Client Audience authorizes that exact resource for this client.
    return {
        "realm": "sshmcp",
        "enabled": True,
        "sslRequired": "none",
        "registrationAllowed": False,
        "resetPasswordAllowed": False,
        "rememberMe": False,
        "loginWithEmailAllowed": False,
        "accessTokenLifespan": 60,
        "ssoSessionIdleTimeout": 86400,
        "ssoSessionMaxLifespan": 604800,
        "revokeRefreshToken": True,
        "roles": {"realm": [{"name": "offline_access"}]},
        "scopeMappings": [{"clientScope": "offline_access", "roles": ["offline_access"]}],
        "clientScopes": [
            {
                "name": "sshmcp:admin",
                "protocol": "openid-connect",
                "attributes": {
                    "include.in.token.scope": "true",
                    "display.on.consent.screen": "false",
                },
            },
            {
                "name": "offline_access",
                "protocol": "openid-connect",
                "attributes": {
                    "include.in.token.scope": "true",
                    "display.on.consent.screen": "true",
                },
            },
        ],
        "clients": [
            {
                "clientId": RESOURCE,
                "enabled": True,
                "protocol": "openid-connect",
                "bearerOnly": True,
                "attributes": {"resource_url": RESOURCE},
            },
            {
                "clientId": CLIENT,
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": True,
                "standardFlowEnabled": True,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "redirectUris": [CALLBACK],
                "webOrigins": [],
                "attributes": {
                    "pkce.code.challenge.method": "S256",
                    "post.logout.redirect.uris": "",
                    "oauth2.device.authorization.grant.enabled": "false",
                },
                "defaultClientScopes": [],
                "optionalClientScopes": ["sshmcp:admin", "offline_access"],
                "protocolMappers": [
                    {
                        "name": "private-mcp-resource",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-audience-mapper",
                        "config": {
                            "included.client.audience": RESOURCE,
                            "access.token.claim": "true",
                            "id.token.claim": "false",
                            "introspection.token.claim": "true",
                        },
                    },
                    {
                        "name": "verified-subject",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-sub-mapper",
                        "config": {
                            "access.token.claim": "true",
                            "introspection.token.claim": "true",
                        },
                    },
                ],
            },
        ],
        "users": [
            {
                "username": name,
                "firstName": name,
                "lastName": "Lab",
                "email": name + "@sshmcp.invalid",
                "enabled": True,
                "emailVerified": True,
                "realmRoles": ["offline_access"],
                "credentials": [{"type": "password", "value": password, "temporary": False}],
            }
            for name, password in passwords.items()
        ],
    }


def remote_files(files):
    """Copy explicit files over pinned SSH; no workspace or home mounts."""
    encoded = {name: base64.b64encode(data).decode() for name, data in files.items()}
    script = """import base64,os,pathlib
os.umask(0o077)
root=pathlib.Path.home()/'.sshmcp-control'
if root.is_symlink(): raise RuntimeError('private root is symlink')
root.mkdir(mode=0o700,exist_ok=True)
for name,data in %r.items():
 p=root/name
 if p.is_symlink(): raise RuntimeError('private file is symlink')
 temporary=root/(name+'.new')
 if temporary.is_symlink(): raise RuntimeError('private temporary file is symlink')
 temporary.write_bytes(base64.b64decode(data));temporary.chmod(0o600)
 temporary.replace(p)
""" % encoded
    lab.strict_ssh("python3 -", label="Copy private control files", timeout=90, input_text=script)


def configure_gateway(target_state, directory):
    state, existing = profile("control")
    lab.verify_identity(state)
    # This private key is generated on the control VM and is never copied back.
    public = lab.strict_ssh(f"set -eu; umask 077; test ! -L {REMOTE}; mkdir -p -m 700 {REMOTE}; "
        f"if ! test -e {REMOTE}/target_ed25519; then "
        f"test ! -e {REMOTE}/target_ed25519.pub; ssh-keygen -q -t ed25519 -N '' -C sshmcp-gateway -f {REMOTE}/target_ed25519; fi; "
        f"test ! -L {REMOTE}/target_ed25519; test ! -L {REMOTE}/target_ed25519.pub; "
        f"chmod 600 {REMOTE}/target_ed25519 {REMOTE}/target_ed25519.pub; "
        f"ssh-keygen -y -P '' -f {REMOTE}/target_ed25519", label="Generate and read only gateway SSH public key", timeout=20)
    public = lab.public_key(public)
    profile("target")
    lab.verify_identity(target_state)
    uid = lab.strict_ssh("id -u", label="Target account identity", timeout=15).strip()
    if not re.fullmatch(r"[1-9][0-9]*", uid):
        raise lab.LabError("Unexpected target account ID.")
    authorized = f'restrict,command="env XDG_RUNTIME_DIR=/run/user/{uid} /usr/local/bin/sshmcp-runner rpc" {public} sshmcp-gateway'
    install = r"""import os,pathlib
os.umask(0o077)
p=pathlib.Path.home()/'.ssh'/'authorized_keys'
if p.is_symlink(): raise RuntimeError('authorized_keys is symlink')
lines=p.read_text().splitlines()
entry=%r
key=%r
matching=[line for line in lines if key in line]
if matching and matching != [entry]: raise RuntimeError('gateway key has unexpected authorization options')
if not matching: p.write_text('\n'.join(lines+[entry])+'\n')
p.chmod(0o600)
""" % (authorized, public.split()[1])
    lab.strict_ssh("python3 -", label="Enroll restricted gateway public key on target", timeout=20, input_text=install)
    profile("control")
    lab.verify_identity(state)
    config = {"listen": "127.0.0.1:18080", "issuer": ISSUER, "resource": RESOURCE,
              "ssh": {"address": target_state["last_ip"] + ":22", "user": lab.USER,
                      "private_key": REMOTE + "/target_ed25519", "host_key": target_state["host_key"]}}
    binary = PRIVATE / "sshmcp-gateway"
    lab.ensure_private_path(binary)
    lab.run(["env", "GOOS=linux", "GOARCH=arm64", "CGO_ENABLED=0", "go", "build", "-o", binary, "./cmd/sshmcp-gateway"], label="Build native gateway", timeout=180, log_path=directory / "build.log")
    binary.chmod(0o600)
    remote_files({"gateway.install": binary.read_bytes(), "gateway.json": json.dumps(config).encode(),
                  "compose.yaml": (lab.ROOT / "lab" / "auth-compose.v1.yaml").read_bytes(),
                  "realm.json": json.dumps(realm_record(accounts())).encode(), "gateway.service": GATEWAY_UNIT.encode()})
    script = f"""set -eu
umask 077
cd {REMOTE}
if ! test -f .env; then
  python3 - <<'PY'
import os,secrets
fd=os.open('.env',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:
 f.write('POSTGRES_PASSWORD='+secrets.token_urlsafe(40)+'\\nKEYCLOAK_ADMIN_PASSWORD='+secrets.token_urlsafe(40)+'\\n')
PY
fi
test ! -L .env
chmod 600 .env
# Import file contains private initial credentials; Keycloak runs as uid 1000.
sudo -n chown 1000:1000 realm.json
sudo -n docker compose -p sshmcp-auth --env-file .env up -d
sudo -n install -o root -g root -m 0755 gateway.install /usr/local/bin/sshmcp-gateway
rm -- gateway.install
mkdir -p -m 700 "$HOME/.config/systemd/user"
install -m 600 gateway.service "$HOME/.config/systemd/user/sshmcp-gateway.service"
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
systemctl --user daemon-reload
systemctl --user enable sshmcp-gateway.service
systemctl --user restart sshmcp-gateway.service
"""
    lab.strict_ssh("sh -s", label="Start private authorization and gateway services", timeout=600, input_text=script, log_path=directory / "services.log")
    lab.strict_ssh("python3 -", label="Verify private authorization configuration without resetting identities", timeout=210,
                   input_text=(lab.ROOT / "lab" / "configure-auth.v1.py").read_text(), log_path=directory / "auth-configuration.log")
    return state


def tunnel_command(*arguments):
    return ["ssh", "-F", str(lab.PROFILES["control"].directory / "ssh_config"), "-o", "ClearAllForwardings=no", "-o", "ExitOnForwardFailure=yes", "-o", "ControlMaster=yes", "-o", "ControlPersist=no", "-S", str(TUNNEL), *arguments, lab.PROFILES["control"].vm]


def tunnel_up(directory):
    check_tunnel_path()
    if TUNNEL.exists():
        # A control socket is not a regular private file. Authenticate the
        # specific master through OpenSSH rather than guessing a process ID.
        lab.run(tunnel_command("-O", "check"), label="Check owned tunnel master", timeout=15)
        return
    lab.run(tunnel_command("-f", "-N", "-L", "127.0.0.1:18080:127.0.0.1:18080", "-L", "127.0.0.1:18081:127.0.0.1:18081"), label="Open authenticated loopback SSH tunnels", timeout=20, log_path=directory / "tunnel.log")


def tunnel_down():
    check_tunnel_path()
    if TUNNEL.exists():
        lab.run(tunnel_command("-O", "exit"), label="Close owned tunnel master", timeout=15)


def check_tunnel_path():
    if TUNNEL.is_symlink():
        raise lab.LabError("Private tunnel socket is a symlink.")
    if TUNNEL.exists():
        info = TUNNEL.stat()
        if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise lab.LabError("Private tunnel socket has an unexpected owner, type, or mode.")


def http_json(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def readiness():
    deadline = time.monotonic() + 150
    while True:
        try:
            discovery = http_json(ISSUER + "/.well-known/openid-configuration")
            resource = http_json("http://127.0.0.1:18080/.well-known/oauth-protected-resource/mcp")
            if discovery.get("issuer") != ISSUER or "S256" not in discovery.get("code_challenge_methods_supported", []) or not discovery.get("authorization_response_iss_parameter_supported") or resource.get("resource") != RESOURCE:
                raise lab.LabError("OAuth discovery does not match the exact private issuer/resource/PKCE contract.")
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if time.monotonic() >= deadline:
                raise lab.LabError("Private OAuth/gateway readiness timed out; inspect private diagnostics.") from None
            time.sleep(1)


def diagnostic(directory):
    commands = {
        "control": "systemctl --user status sshmcp-gateway.service --no-pager --lines=0 || failed=1; "
            "journalctl --user -u sshmcp-gateway.service -n 80 --no-pager || failed=1; "
            f"cd {REMOTE} && sudo -n docker compose -p sshmcp-auth logs --tail 80 || failed=1",
        "target": "systemctl --user status sshmcp-runner.service --no-pager --lines=0 || failed=1; "
            "journalctl --user -u sshmcp-runner.service -n 80 --no-pager || failed=1; "
            "systemctl --user cat sshmcp-runner.service --no-pager || failed=1",
    }
    errors = []
    try:
        for name, command in commands.items():
            try:
                state, existing = profile(name)
                if not state or not existing or existing.get("state") != "Running":
                    raise lab.LabError("VM is unavailable or stopped; diagnostics not collected.")
                lab.verify_identity(state)
                log_name = "diagnostics.log" if name == "control" else "target-diagnostics.log"
                lab.strict_ssh("export XDG_RUNTIME_DIR=/run/user/$(id -u); export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus; "
                    "failed=0; " + command + '; exit "$failed"', label=f"Private MCP {name} diagnostics", timeout=30, log_path=directory / log_name)
            except (Exception, KeyboardInterrupt) as error:
                errors.append(f"{name}: {lab.error_text(error)}")
    finally:
        lab.select_profile("control")
    if errors:
        raise lab.LabError("Private MCP diagnostics failed: " + "; ".join(errors))


def write_codex_config():
    # Kept as a separate private fragment, used with command-line overrides.
    # It never rewrites the user's global model, provider, or MCP definitions.
    overrides = {
        f"mcp_servers.{SERVER_NAME}.url": RESOURCE,
        # Codex 0.162.1 already derives resource from protected metadata. An
        # explicit oauth_resource duplicates that parameter and Keycloak rejects
        # the request, so use the exact discovered resource instead.
        f"mcp_servers.{SERVER_NAME}.oauth.client_id": CLIENT,
        f"mcp_servers.{SERVER_NAME}.oauth.callback_url": CALLBACK,
        f"mcp_servers.{SERVER_NAME}.oauth.callback_port": 18443,
        f"mcp_servers.{SERVER_NAME}.startup_timeout_sec": 30,
        f"mcp_servers.{SERVER_NAME}.enabled_tools": ["connect", "run_command", "disconnect"],
        # The user explicitly authorizes this private administrative lab. Trust
        # only its three tools for this invocation, retaining all global policy.
        f"mcp_servers.{SERVER_NAME}.tools.connect.approval_mode": "approve",
        f"mcp_servers.{SERVER_NAME}.tools.run_command.approval_mode": "approve",
        f"mcp_servers.{SERVER_NAME}.tools.disconnect.approval_mode": "approve",
    }
    lab.write_private(PRIVATE / "codex-overrides.json", json.dumps(overrides, indent=2) + "\n")
    return overrides


def codex_arguments(*arguments):
    codex = shutil.which("codex")
    if not codex:
        raise lab.LabError("Codex CLI is required for private MCP login.")
    overrides = write_codex_config()
    command = [codex]
    for key, value in overrides.items():
        command.extend(["-c", key + "=" + json.dumps(value)])
    return command + list(arguments)


def up(directory):
    target_state, existing = profile("target")
    target_state = lab.up(target_state, existing)
    control_state, existing = profile("control")
    lab.up(control_state, existing)
    configure_gateway(target_state, directory)
    tunnel_up(directory)
    readiness()
    authenticated_catalog()
    write_codex_config()
    print("Private MCP ready: http://127.0.0.1:18080/mcp; OAuth issuer on 127.0.0.1:18081.")
    print("Both owned VMs are running. Run make mcp-lab-login once, then tools/mcp_lab.py codex -- <Codex arguments>.")


def authenticated_catalog():
    # A successful public discovery response alone cannot establish that a
    # usable authenticated MCP endpoint exists (for example, a missing sub).
    import lab_oauth
    token = lab_oauth.tokens()["access_token"]
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}
    request = urllib.request.Request(RESOURCE, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"})
    with urllib.request.urlopen(request, timeout=40) as response:
        result = json.load(response)
    if {tool["name"] for tool in result.get("result", {}).get("tools", [])} != {"connect", "run_command", "disconnect"}:
        raise lab.LabError("Authenticated private MCP tool catalog differs from the three-tool contract.")


def down():
    errors = []
    try:
        tunnel_down()
    except Exception as error:
        errors.append(str(error))
    for name in ("control", "target"):
        try:
            state, existing = profile(name)
            lab.down(state, existing)
        except Exception as error:
            errors.append(str(error))
    if errors:
        raise lab.LabError("; ".join(errors))


def status():
    for name in ("control", "target"):
        state, existing = profile(name)
        lab.status(state, existing)
    if TUNNEL.exists():
        lab.run(tunnel_command("-O", "check"), label="Check private tunnel", timeout=15)
        readiness()
        print("Private MCP metadata and OAuth discovery are ready.")
    else:
        print("Private SSH tunnels are absent; MCP is not ready on this Mac.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("up", "down", "status", "login", "codex"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    os.umask(0o077)
    directory = None
    try:
        lab.select_profile("control")
        with lab.interrupt_handling(), lab.workspace_lock():
            if args.command == "up":
                require_enrolled_profiles()
            private_directory()
            if args.command in ("codex", "login"):
                arguments = args.arguments
                if arguments[:1] == ["--"]:
                    arguments = arguments[1:]
                if args.command == "login":
                    arguments = ["mcp", "login", SERVER_NAME, "--no-browser", "--scopes", "sshmcp:admin,offline_access"]
                # Interactive login deliberately runs attached to the terminal.
                return subprocess.call(codex_arguments(*arguments), cwd=lab.ROOT)
            _, directory = lab.new_results("mcp-" + args.command)
            if args.command == "up":
                up(directory)
            elif args.command == "down":
                down()
            else:
                status()
            lab.write_private(directory / "result.json", json.dumps({"operation": args.command, "ok": True}) + "\n")
            print(f"Private result: {directory}")
        return 0
    except (Exception, KeyboardInterrupt) as error:
        cleanup = []
        if directory is not None:
            with lab.interrupt_handling(cleanup=True):
                try:
                    diagnostic(directory)
                except Exception as detail:
                    cleanup.append(str(detail))
                if args.command == "up":
                    try:
                        down()
                    except Exception as detail:
                        cleanup.append(str(detail))
                lab.write_private(directory / "result.json", json.dumps({"operation": args.command, "ok": False, "error": str(error), "cleanup_errors": cleanup}) + "\n")
            print(f"Private result: {directory}", file=sys.stderr)
        print(f"Private MCP lab error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
