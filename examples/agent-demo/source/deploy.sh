#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
# Use the VM resolver for dependency downloads; Compose v5 Bake otherwise
# requires an extra host-network entitlement not exposed by compose build.
sudo -n docker build --network=host -t sshmcp-demo-app:local ./app
sudo -n docker compose up -d --force-recreate --wait --wait-timeout 180
