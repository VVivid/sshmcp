#!/usr/bin/env bash
set -euo pipefail
umask 077
action=${1:?missing action}
run_id=${2:?missing run identifier}
[[ $run_id =~ ^[0-9a-f]{32}$ ]] || exit 2
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
base="$HOME/.sshmcp-lab-runs/$run_id"
case "$action" in
  run)
    mkdir -p -m 700 "$base"
    python3 - "$run_id" "$base" <<'PY'
import base64, json, pathlib, subprocess, sys, time
run_id, base = sys.argv[1:]
owner = {"issuer": "sshmcp-target-smoke", "subject": run_id}
def rpc(action, **fields):
    result = subprocess.run(["sshmcp-runner", "rpc"], input=json.dumps(dict(action=action, owner=owner, **fields)), capture_output=True, text=True, check=True, timeout=35)
    response = json.loads(result.stdout)
    assert "error" not in response, response
    return response
connection = rpc("connect")["connection"]["id"]
pathlib.Path(base, "connection").write_text(connection)
job = rpc("run", connection_id=connection, idempotency_key=run_id, command="cd /tmp; retained=target-smoke; printf '%s\\n' \"$retained\"; printf 'stderr-smoke\\n' >&2")["job"]
for _ in range(100):
    result = rpc("job", job_id=job["id"])["job"]
    if result["state"] not in ("accepted", "running"): break
    time.sleep(.05)
assert result["state"] == "completed", result
output = rpc("output", job_id=job["id"])["output"]
assert base64.b64decode(output["stdout"]) == b"target-smoke\n", output
assert base64.b64decode(output["stderr"]) == b"stderr-smoke\n", output
assert rpc("lookup", idempotency_key=run_id)["job"]["id"] == job["id"]
assert rpc("run", connection_id=connection, idempotency_key=run_id, command=job["command"])["duplicate"]
rpc("disconnect", connection_id=connection)
print("Target runner: native Bash, durable job, separate output, idempotent lookup, disconnect passed.")
PY
    ;;
  diagnostics)
    sudo -n cloud-init status --long
    sudo -n journalctl --no-pager --lines=50 -u ssh.service
    journalctl --user --no-pager --lines=80 -u sshmcp-runner.service
    ;;
  cleanup)
    if test -f "$base/connection"; then
      python3 - "$base/connection" "$run_id" <<'PY'
import json, pathlib, subprocess, sys
request = dict(action="disconnect", owner=dict(issuer="sshmcp-target-smoke", subject=sys.argv[2]), connection_id=pathlib.Path(sys.argv[1]).read_text())
subprocess.run(["sshmcp-runner", "rpc"], input=json.dumps(request), text=True, check=True, timeout=35)
PY
    fi
    rm -rf -- "$base"
    ;;
  *) exit 2 ;;
esac
