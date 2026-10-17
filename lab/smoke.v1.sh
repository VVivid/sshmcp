#!/usr/bin/env bash
# Streamed over strict SSH; no host directories enter the guest.
set -euo pipefail
umask 077

action=${1:?missing action}
run_id=${2:?missing run identifier}
[[ $run_id =~ ^[0-9a-f]{32}$ ]] || exit 2
test "$(id -un)" = mcp-test
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
base="$HOME/.sshmcp-lab-runs"
directory="$base/$run_id"
project="sshmcp-smoke-$run_id"
job="sshmcp-smoke-$run_id.service"
workload="sshmcp-workload-$run_id.service"
test ! -L "$base"
test ! -L "$directory"

compose() {
    sudo -n docker compose --project-name "$project" --file "$directory/compose.yaml" "$@"
}

stop_unit() {
    local state
    state=$(systemctl --user show "$1" --property=LoadState --value) || return 1
    if test "$state" != not-found; then
        systemctl --user stop "$1" || return 1
        systemctl --user reset-failed "$1" 2>/dev/null || true
    fi
}

case "$action" in
    run)
        mkdir -p -m 700 "$base"
        mkdir -m 700 "$directory"
        printf 'sshmcp HTTP smoke %s\n' "$run_id" > "$directory/index.html"
        # nginx's worker uses a different guest/container UID. This fixture is
        # public test text; private local logs and SSH material remain 0600.
        chmod 644 "$directory/index.html"
        cat > "$directory/compose.yaml" <<'COMPOSE'
services:
  http:
    image: nginx@sha256:892bb2152464f0c5d8972593a5f5afaf8054552580b699d95f8ff4af08c2d33d
    ports:
      - "127.0.0.1::80"
    volumes:
      - ./index.html:/usr/share/nginx/html/index.html:ro
    healthcheck:
      test: ["CMD-SHELL", "wget -q -O /dev/null http://127.0.0.1/ || exit 1"]
      interval: 2s
      timeout: 3s
      retries: 20
COMPOSE
        printf 'Strict SSH smoke: user=%s architecture=%s\n' "$(id -un)" "$(uname -m)"
        systemd-run --user --unit "$job" --wait --pipe --collect \
            /bin/sh -c 'printf "systemd user job smoke ok\n"'
        compose up --detach --wait --wait-timeout 90
        address=$(compose port http 80)
        [[ $address =~ ^127\.0\.0\.1:[0-9]+$ ]]
        curl --fail --silent --show-error --max-time 15 "http://$address/" > "$directory/response.txt"
        cmp "$directory/index.html" "$directory/response.txt"
        compose ps
        compose logs --no-color --timestamps --tail 100
        compose down --remove-orphans --timeout 10
        printf 'HTTP healthcheck, response, logs, and explicit Compose shutdown passed.\n'
        ;;
    diagnostics)
        # Fixed diagnostics intentionally avoid environment or key-file dumps.
        result=0
        sudo -n cloud-init status --long || result=1
        sudo -n journalctl --no-pager --lines=80 -u ssh.service -u docker.service || result=1
        journalctl --user --no-pager --lines=80 -u "$job" -u "$workload" || result=1
        if test -f "$directory/compose.yaml"; then
            compose ps --all || result=1
            compose logs --no-color --timestamps --tail 100 || result=1
        fi
        exit "$result"
        ;;
    cleanup)
        result=0
        # Stop the bounded workload unit first: an SSH disconnect must not leave
        # it creating containers after cleanup has removed the same run.
        stop_unit "$workload" || result=1
        stop_unit "$job" || result=1
        if test -f "$directory/compose.yaml"; then
            compose down --remove-orphans --timeout 10 || result=1
        fi
        # Keep the fixture on cleanup failure so diagnostics/recovery retain its
        # exact Compose definition. Never remove another run's directory.
        if test "$result" = 0; then
            rm -rf -- "$directory"
        fi
        exit "$result"
        ;;
    *) exit 2 ;;
esac
