#!/usr/bin/env bash
# Run one bounded real-Hummingbot ETH/SOL MM session on e2_mm1.
#
# This is an explicitly gated mainnet write.  It uses a disposable HB runtime,
# imports only e2_mm1 into that runtime, monitors account margin read-only, then
# cancels and flattens the scoped ETH/SOL state before returning.
# Diagnostic only: a single-account run cannot exercise the two-account
# zero-net-USDC STOP_QUOTING target. It can still exercise emergency exits.
set -euo pipefail

DURATION="${1:-1800}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HB_SOURCE="${HB_SOURCE:-/home/christian/sources/hummingbot}"
PY="${PY:-/home/christian/miniforge3/envs/hummingbot/bin/python}"
ENV_FILE="${OPMS_ENV_FILE:-$REPO/.env}"
ACCOUNT_ID=e2_mm1
MIN_COLLATERAL="${MIN_COLLATERAL:-600}"
MAX_DRAWDOWN_PCT="${MAX_DRAWDOWN_PCT:-1.0}"
STAMP="$(date +%Y%m%dT%H%M%S)"
ARTIFACT_DIR="${ARTIFACT_DIR:-$REPO/hb-enhanced-opms/logs/live_soak_${STAMP}}"
COMMON_PYTHONPATH="$REPO/hb-enhanced-opms/src:$REPO/perp-bot/src:$REPO/mm-core/src"

[[ "$DURATION" =~ ^[0-9]+$ && "$DURATION" -gt 0 ]] || {
  echo "duration must be a positive integer" >&2; exit 2;
}
[[ "${OPMS_HB_MAINNET:-}" == "confirm" ]] || {
  echo "refusing mainnet connectors: set OPMS_HB_MAINNET=confirm" >&2; exit 2;
}
[[ "${OPMS_HB_PLACE_ORDERS:-}" == "confirm" ]] || {
  echo "refusing live orders: set OPMS_HB_PLACE_ORDERS=confirm" >&2; exit 2;
}
[[ -x "$PY" ]] || { echo "Hummingbot Python not found: $PY" >&2; exit 2; }
[[ -d "$HB_SOURCE/hummingbot" && -x "$HB_SOURCE/bin/hummingbot_quickstart.py" ]] || {
  echo "Hummingbot checkout not found at $HB_SOURCE" >&2; exit 2;
}
[[ -f "$ENV_FILE" ]] || { echo "environment file not found: $ENV_FILE" >&2; exit 2; }

mkdir -p "$ARTIFACT_DIR"
PREflight="$ARTIFACT_DIR/preflight.json"
"$PY" "$REPO/hb-enhanced-opms/scripts/check_hl_account_state.py" \
  --account-id "$ACCOUNT_ID" --coins ETH SOL --require-clean \
  --min-equity "$MIN_COLLATERAL" --output "$PREflight"

if [[ -f "$REPO/hb-enhanced-opms/logs/hb_soak/perp_mm_e2_mm1_eth_soak.decisions.jsonl" ]]; then
  mv "$REPO/hb-enhanced-opms/logs/hb_soak/perp_mm_e2_mm1_eth_soak.decisions.jsonl" \
     "$REPO/hb-enhanced-opms/logs/hb_soak/perp_mm_e2_mm1_eth_soak.decisions.$STAMP.jsonl"
fi
if [[ -f "$REPO/hb-enhanced-opms/logs/hb_soak/perp_mm_e2_mm1_sol_soak.decisions.jsonl" ]]; then
  mv "$REPO/hb-enhanced-opms/logs/hb_soak/perp_mm_e2_mm1_sol_soak.decisions.jsonl" \
     "$REPO/hb-enhanced-opms/logs/hb_soak/perp_mm_e2_mm1_sol_soak.decisions.$STAMP.jsonl"
fi

REMOVE_BASE=0
if [[ -z "${RUNTIME_BASE:-}" ]]; then
  RUNTIME_BASE="$(mktemp -d /tmp/hb-live-soak.XXXXXX)"
  REMOVE_BASE=1
else
  mkdir -p "$RUNTIME_BASE"
fi
RUNTIME="$RUNTIME_BASE/runtime"
[[ ! -e "$RUNTIME" ]] || { echo "refusing to reuse runtime: $RUNTIME" >&2; exit 2; }
mkdir -p "$RUNTIME"

cleanup_runtime() {
  rm -rf "$RUNTIME"
  if [[ "$REMOVE_BASE" -eq 1 ]]; then
    rmdir "$RUNTIME_BASE" 2>/dev/null || true
  fi
}
trap cleanup_runtime EXIT

# Code can be hard-linked; conf is copied because HB credential-store writes
# must never mutate the source checkout or another runtime's store.
cp -al "$HB_SOURCE/bin" "$RUNTIME/bin"
cp -al "$HB_SOURCE/hummingbot" "$RUNTIME/hummingbot"
cp -al "$HB_SOURCE/scripts" "$RUNTIME/scripts"
cp -al "$HB_SOURCE/controllers" "$RUNTIME/controllers"
cp -a "$HB_SOURCE/conf" "$RUNTIME/conf"
mkdir -p "$RUNTIME/data" "$RUNTIME/logs"
cp "$REPO/hb-enhanced-opms/deploy/hummingbot/conf/scripts/opms_perp_mm_e2_mm1_soak.yml" \
   "$RUNTIME/conf/scripts/"
cp "$REPO/hb-enhanced-opms/deploy/hummingbot/conf/controllers/perp_mm_e2_mm1_eth_soak.yml" \
   "$REPO/hb-enhanced-opms/deploy/hummingbot/conf/controllers/perp_mm_e2_mm1_sol_soak.yml" \
   "$RUNTIME/conf/controllers/"
cp "$REPO/hb-enhanced-opms/deploy/hummingbot/scripts/validate_hb_soak_config.py" \
   "$RUNTIME/scripts/"

PASSWORD="${HB_PASSWORD:-}"
if [[ -z "$PASSWORD" ]]; then
  PASSWORD="$($PY -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi

(
  cd "$RUNTIME"
  OPMS_ENV_FILE="$ENV_FILE" HB_PASSWORD="$PASSWORD" \
    PYTHONPATH="$RUNTIME:$COMMON_PYTHONPATH" \
    "$PY" "$REPO/hb-enhanced-opms/scripts/import_hl_mainnet_credentials.py" \
      --account-id "$ACCOUNT_ID"
)
(
  cd "$RUNTIME"
  OPMS_ENV_FILE="$ENV_FILE" PYTHONPATH="$RUNTIME:$COMMON_PYTHONPATH" \
    "$PY" "$RUNTIME/scripts/validate_hb_soak_config.py"
)

if [[ "${OPMS_HB_DRY_RUN:-}" == "1" ]]; then
  echo "warning: single-account diagnostic; cross-account USDC-flat stop is unavailable" >&2
  echo "live soak dry-run passed: runtime prepared and config validated; no Hummingbot process started"
  exit 0
fi

LOG_ETH="$REPO/hb-enhanced-opms/logs/hb_soak/perp_mm_e2_mm1_eth_soak.decisions.jsonl"
LOG_SOL="$REPO/hb-enhanced-opms/logs/hb_soak/perp_mm_e2_mm1_sol_soak.decisions.jsonl"
mkdir -p "$(dirname "$LOG_ETH")"

echo "warning: single-account diagnostic; cross-account USDC-flat stop is unavailable" >&2
echo "starting e2_mm1 live soak: duration=${DURATION}s artifact_dir=$ARTIFACT_DIR"
(
  cd "$RUNTIME"
  exec env OPMS_ENV_FILE="$ENV_FILE" CONFIG_PASSWORD="$PASSWORD" \
    SCRIPT_CONFIG=opms_perp_mm_e2_mm1_soak.yml \
    PYTHONPATH="$RUNTIME/bin:$RUNTIME:$COMMON_PYTHONPATH" \
    timeout --signal=INT --kill-after=60 "$DURATION" \
      "$PY" "$REPO/hb-enhanced-opms/deploy/hummingbot/scripts/run_hummingbot_isolated.py"
) >"$ARTIFACT_DIR/launcher.log" 2>&1 &
HB_PID=$!

set +e
"$PY" "$REPO/hb-enhanced-opms/scripts/monitor_hl_soak.py" \
  --account-id "$ACCOUNT_ID" --pid "$HB_PID" --duration "$DURATION" \
  --max-drawdown-pct "$MAX_DRAWDOWN_PCT" \
  --output "$ARTIFACT_DIR/monitor.jsonl" >"$ARTIFACT_DIR/monitor.log" 2>&1 &
MON_PID=$!
wait "$HB_PID"
HB_STATUS=$?
wait "$MON_PID"
MON_STATUS=$?
set -e

if ! rg -q "Strategy opms_perp_mm started successfully" "$ARTIFACT_DIR/launcher.log"; then
  echo "Hummingbot strategy did not report successful startup" >&2
  HB_STATUS=1
fi

set +e
"$PY" "$REPO/hb-enhanced-opms/scripts/cleanup_hl_soak.py" \
  --account-id "$ACCOUNT_ID" --coins ETH SOL --allow-cleanup \
  --output "$ARTIFACT_DIR/cleanup.json"
CLEAN_STATUS=$?
"$PY" "$REPO/hb-enhanced-opms/scripts/check_hl_account_state.py" \
  --account-id "$ACCOUNT_ID" --coins ETH SOL --require-clean \
  --output "$ARTIFACT_DIR/postflight.json"
POST_STATUS=$?
set -e

if [[ ! -s "$LOG_ETH" || ! -s "$LOG_SOL" ]]; then
  echo "live soak did not produce both decision logs" >&2
  HB_STATUS=1
fi

is_expected_stop() {
  [[ "$1" -eq 0 || "$1" -eq 124 || "$1" -eq 130 || "$1" -eq 143 ]]
}
if ! is_expected_stop "$HB_STATUS" || [[ "$MON_STATUS" -ne 0 || "$CLEAN_STATUS" -ne 0 || "$POST_STATUS" -ne 0 ]]; then
  echo "live soak failed: hb=$HB_STATUS monitor=$MON_STATUS cleanup=$CLEAN_STATUS postflight=$POST_STATUS" >&2
  echo "artifacts: $ARTIFACT_DIR" >&2
  exit 1
fi

echo "e2_mm1 live soak passed: ${DURATION}s, ETH/SOL, 6x, clean teardown"
echo "artifacts: $ARTIFACT_DIR"
