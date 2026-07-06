#!/usr/bin/env bash
# Created on 2026-07-05T20:02:30-04:00
# Author: nate

set -euo pipefail

DIR="$(dirname "$(readlink -f "$0")")"

pushd "$DIR" >/dev/null
trap 'popd >/dev/null' EXIT


PROJECT_ROOT="$(mktemp -d)"

set -o allexport
source "$DIR/.env"
set +o allexport

echo "###############################################################"
echo "$NATE_NTM_CONTROL_HOST:$NATE_NTM_CONTROL_PORT"
echo "###############################################################"

uv run nate-ntm runtime start \
  --project "$PROJECT_ROOT" \
  --mode create \
  --agents 1 \
  --adapter-mode real \
  --with-control-api
