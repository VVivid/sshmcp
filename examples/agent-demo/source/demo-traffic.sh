#!/usr/bin/env bash
set -euo pipefail
count="${1:-60}"
for ((i=0;i<count;i++)); do
  curl --fail --silent --show-error -H "X-SSHMCP-Probe: operator-demo-$i" http://127.0.0.1:8080/
  sleep 1
done
