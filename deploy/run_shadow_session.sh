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
CTRL_CONF="$REPO/hb-enhanced-opms/deploy/hummingbot/conf/controllers/perp_mm_e2_mm1_eth_shadow.yml"

LOG="$("$PY" -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["decision_log_path"])' "$CTRL_CONF")"
[ -f "$LOG" ] && mv "$LOG" "${LOG%.jsonl}.$(date +%Y%m%dT%H%M%S).jsonl"   # one session per log

PASSWORD="$("$PY" -c 'import sys; from dotenv import dotenv_values; print(dotenv_values(sys.argv[1]).get("HB_PASSWORD") or "")' "$REPO/.env")"
[ -n "$PASSWORD" ] || { echo "set HB_PASSWORD in $REPO/.env" >&2; exit 2; }

cd "$HB_ROOT"
CONFIG_PASSWORD="$PASSWORD" SCRIPT_CONFIG="$SCRIPT_CONF" HEADLESS_MODE=true \
  timeout --signal=INT --kill-after=60 "$DURATION" "$PY" bin/hummingbot_quickstart.py || true

[ -s "$LOG" ] || { echo "no decisions logged at $LOG — check $HB_ROOT/logs/" >&2; exit 1; }
"$PY" "$REPO/perp-bot/scripts/replay_decision_log.py" "$CTRL_CONF" "$LOG"
