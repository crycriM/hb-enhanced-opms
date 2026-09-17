#!/usr/bin/env bash
# One shadow session: real Hummingbot (headless) running the OPMS launcher for
# DURATION seconds, then Phase-1 decision-log parity against a standalone Keeper.
# Shadow mode places no orders. The HB password comes from HB_PASSWORD in the
# monorepo .env and reaches HB only through its CONFIG_PASSWORD env var.
set -euo pipefail
DURATION="${1:-1800}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HB_ROOT="${HB_ROOT:-/home/christian/sources/hummingbot}"
PY="${PY:-/home/christian/miniforge3/envs/hummingbot/bin/python}"
SCRIPT_CONF=opms_perp_mm_e2_mm1_shadow.yml
CTRL_CONF_ETH="$REPO/hb-enhanced-opms/deploy/hummingbot/conf/controllers/perp_mm_e2_mm1_eth_shadow.yml"
CTRL_CONF_SOL="$REPO/hb-enhanced-opms/deploy/hummingbot/conf/controllers/perp_mm_e2_mm1_sol_shadow.yml"

LOG_ETH="$("$PY" -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["decision_log_path"])' "$CTRL_CONF_ETH")"
LOG_SOL="$("$PY" -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["decision_log_path"])' "$CTRL_CONF_SOL")"
for log in "$LOG_ETH" "$LOG_SOL"; do
  [ -f "$log" ] && mv "$log" "${log%.jsonl}.$(date +%Y%m%dT%H%M%S).jsonl"
done   # one session per log

PASSWORD="$("$PY" -c 'import sys; from dotenv import dotenv_values; print(dotenv_values(sys.argv[1]).get("HB_PASSWORD") or "")' "$REPO/.env")"
[ -n "$PASSWORD" ] || { echo "set HB_PASSWORD in $REPO/.env" >&2; exit 2; }

cd "$HB_ROOT"
CONFIG_PASSWORD="$PASSWORD" SCRIPT_CONFIG="$SCRIPT_CONF" \
  PYTHONPATH="$HB_ROOT:$REPO/hb-enhanced-opms/src:$REPO/perp-bot/src:$REPO/mm-core/src" \
  timeout --signal=INT --kill-after=60 "$DURATION" \
    "$PY" "$REPO/hb-enhanced-opms/deploy/hummingbot/scripts/run_hummingbot_isolated.py" || true

for pair in ETH SOL; do
  if [ "$pair" = ETH ]; then
    ctrl="$CTRL_CONF_ETH"
    log="$LOG_ETH"
  else
    ctrl="$CTRL_CONF_SOL"
    log="$LOG_SOL"
  fi
  [ -s "$log" ] || { echo "no decisions logged at $log — check $HB_ROOT/logs/" >&2; exit 1; }
  "$PY" "$REPO/perp-bot/scripts/replay_decision_log.py" "$ctrl" "$log"
done
