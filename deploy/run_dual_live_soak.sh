#!/usr/bin/env bash
# Bounded two-account ETH/SOL mainnet soak. Defaults to one hour.
set -euo pipefail

DURATION="${1:-3600}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HB_SOURCE="${HB_SOURCE:-/home/christian/sources/hummingbot}"
PY="${PY:-/home/christian/miniforge3/envs/hummingbot/bin/python}"
ENV_FILE="$REPO/.env"
MIN_COLLATERAL="${MIN_COLLATERAL:-300}"
MAX_DRAWDOWN_PCT="${MAX_DRAWDOWN_PCT:-1.0}"
STAMP="$(date +%Y%m%dT%H%M%S)"
ARTIFACT_DIR="${ARTIFACT_DIR:-$REPO/hb-enhanced-opms/logs/live_dual_soak_${STAMP}}"
COMMON_PYTHONPATH="$REPO/hb-enhanced-opms/src:$REPO/perp-bot/src:$REPO/mm-core/src"
PORTFOLIO_MEMBERS="e2_mm1:ETH,e2_mm1:SOL,e3_sub1:ETH,e3_sub1:SOL"
ACCOUNTS=(e2_mm1 e3_sub1)
COINS=(eth sol)

[[ "$DURATION" =~ ^[0-9]+$ && "$DURATION" -gt 0 && "$DURATION" -le 3600 ]] || {
  echo "duration must be 1..3600 seconds" >&2; exit 2;
}
[[ "${OPMS_HB_MAINNET:-}" == confirm && "${OPMS_HB_PLACE_ORDERS:-}" == confirm ]] || {
  echo "refusing live mainnet orders: set OPMS_HB_MAINNET=confirm and OPMS_HB_PLACE_ORDERS=confirm" >&2
  exit 2
}
[[ -x "$PY" && -d "$HB_SOURCE/hummingbot" && -x "$HB_SOURCE/bin/hummingbot_quickstart.py" ]] || {
  echo "Hummingbot checkout or Python environment not found" >&2; exit 2
}
[[ -f "$ENV_FILE" ]] || { echo "environment file not found: $ENV_FILE" >&2; exit 2; }
[[ ! -e "$ARTIFACT_DIR" ]] || { echo "refusing to reuse artifact directory: $ARTIFACT_DIR" >&2; exit 2; }

# Hyperliquid uses the agent signer nonce even when vaultAddress differs.
"$PY" - "$ENV_FILE" "${ACCOUNTS[@]}" <<'PY'
import os
import sys
from dotenv import dotenv_values
from eth_account import Account

env = {**dotenv_values(sys.argv[1]), **os.environ}
accounts = sys.argv[2:4]
keys = [env.get(f"HYPERLIQUID_{a.upper()}_PRIVATE_KEY") for a in accounts]
addresses = [env.get(f"HYPERLIQUID_{a.upper()}_ACCOUNT_ADDRESS") for a in accounts]
if not all(keys) or not all(addresses):
    raise SystemExit("both account credentials are required")
if addresses[0].lower() == addresses[1].lower():
    raise SystemExit("refusing dual soak: account addresses are identical")
if Account.from_key(keys[0]).address.lower() == Account.from_key(keys[1]).address.lower():
    raise SystemExit(f"refusing dual soak: {accounts[0]} and {accounts[1]} share an agent signer")
print("dual signer and account isolation passed")
PY

mkdir -p "$ARTIFACT_DIR"
for account in "${ACCOUNTS[@]}"; do
  if ! "$PY" "$REPO/hb-enhanced-opms/scripts/check_hl_account_state.py" \
    --account-id "$account" --coins ETH SOL --require-clean \
    --min-equity "$MIN_COLLATERAL" --output "$ARTIFACT_DIR/${account}_preflight.json" \
    >"$ARTIFACT_DIR/${account}_preflight.log"; then
    echo "$account preflight failed: $ARTIFACT_DIR/${account}_preflight.json" >&2
    exit 1
  fi
done

RUNTIME_BASE="$(mktemp -d /tmp/hb-dual-live-soak.XXXXXX)"
STARTED=0
COMPLETED=0
declare -A HB_PID MON_PID

finish() {
  local status=$? account coin log hb_status mon_status cleanup_status post_status
  trap - EXIT INT TERM
  set +e
  for account in "${ACCOUNTS[@]}"; do
    if [[ -n "${HB_PID[$account]:-}" ]]; then
      kill -INT "${HB_PID[$account]}" 2>/dev/null || true
    fi
  done
  for account in "${ACCOUNTS[@]}"; do
    if [[ -n "${HB_PID[$account]:-}" ]]; then
      wait "${HB_PID[$account]}"
      hb_status=$?
      if [[ "$hb_status" -ne 0 && "$hb_status" -ne 124 && "$hb_status" -ne 130 && "$hb_status" -ne 143 ]]; then
        echo "$account Hummingbot exit status: $hb_status" >&2
        status=1
      fi
    fi
    if [[ -n "${MON_PID[$account]:-}" ]]; then
      wait "${MON_PID[$account]}"
      mon_status=$?
      if [[ "$mon_status" -ne 0 ]]; then
        echo "$account monitor exit status: $mon_status" >&2
        status=1
      fi
    fi
  done
  if [[ "$STARTED" -eq 1 ]]; then
    for account in "${ACCOUNTS[@]}"; do
      "$PY" "$REPO/hb-enhanced-opms/scripts/cleanup_hl_soak.py" \
        --account-id "$account" --coins ETH SOL --allow-cleanup \
        --output "$ARTIFACT_DIR/${account}_cleanup.json" \
        >"$ARTIFACT_DIR/${account}_cleanup.log" 2>&1
      cleanup_status=$?
      "$PY" "$REPO/hb-enhanced-opms/scripts/check_hl_account_state.py" \
        --account-id "$account" --coins ETH SOL --require-clean \
        --output "$ARTIFACT_DIR/${account}_postflight.json" \
        >"$ARTIFACT_DIR/${account}_postflight.log" 2>&1
      post_status=$?
      if [[ "$cleanup_status" -ne 0 || "$post_status" -ne 0 ]]; then
        echo "$account cleanup/postflight failed: $cleanup_status/$post_status" >&2
        status=1
      fi
      if ! rg -q "Strategy opms_perp_mm started successfully" "$ARTIFACT_DIR/${account}_launcher.log"; then
        echo "$account strategy did not report successful startup" >&2
        status=1
      fi
      for coin in "${COINS[@]}"; do
        log="$REPO/hb-enhanced-opms/logs/hb_soak/perp_mm_${account}_${coin}_soak.decisions.jsonl"
        if [[ -s "$log" ]]; then
          cp "$log" "$ARTIFACT_DIR/"
        else
          echo "$account/$coin decision log missing" >&2
          status=1
        fi
      done
    done
  fi
  rm -rf "$RUNTIME_BASE"
  if [[ "$status" -eq 0 && "$COMPLETED" -eq 1 ]]; then
    echo "dual live soak passed: ${DURATION}s, ETH/SOL on both accounts, clean teardown"
  elif [[ "$status" -ne 0 ]]; then
    echo "dual live soak failed; inspect artifacts and both accounts" >&2
  fi
  echo "artifacts: $ARTIFACT_DIR"
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for account in "${ACCOUNTS[@]}"; do
  runtime="$RUNTIME_BASE/$account"
  mkdir -p "$runtime"
  cp -al "$HB_SOURCE/bin" "$runtime/bin"
  cp -al "$HB_SOURCE/hummingbot" "$runtime/hummingbot"
  cp -al "$HB_SOURCE/scripts" "$runtime/scripts"
  cp -al "$HB_SOURCE/controllers" "$runtime/controllers"
  cp -a "$HB_SOURCE/conf" "$runtime/conf"
  mkdir -p "$runtime/data" "$runtime/logs"
  cp --remove-destination "$REPO/hb-enhanced-opms/deploy/hummingbot/conf/scripts/opms_perp_mm_${account}_soak.yml" \
    "$runtime/conf/scripts/"
  for coin in "${COINS[@]}"; do
    cp --remove-destination "$REPO/hb-enhanced-opms/deploy/hummingbot/conf/controllers/perp_mm_${account}_${coin}_soak.yml" \
      "$runtime/conf/controllers/"
  done
  cp --remove-destination "$REPO/hb-enhanced-opms/deploy/hummingbot/scripts/validate_hb_soak_config.py" \
    "$runtime/scripts/"
done

PASSWORD="${HB_PASSWORD:-$($PY -c 'import secrets; print(secrets.token_urlsafe(32))')}"
for account in "${ACCOUNTS[@]}"; do
  runtime="$RUNTIME_BASE/$account"
  (
    cd "$runtime"
    OPMS_ENV_FILE="$ENV_FILE" HB_PASSWORD="$PASSWORD" PYTHONPATH="$runtime:$COMMON_PYTHONPATH" \
      "$PY" "$REPO/hb-enhanced-opms/scripts/import_hl_mainnet_credentials.py" \
        --account-id "$account"
    OPMS_ENV_FILE="$ENV_FILE" PYTHONPATH="$runtime:$COMMON_PYTHONPATH" \
      "$PY" "$runtime/scripts/validate_hb_soak_config.py" --account-id "$account"
  )
done

if [[ "${OPMS_HB_DRY_RUN:-}" == 1 ]]; then
  echo "dual live soak dry-run passed: preflight and config validation only"
  exit 0
fi

mkdir -p "$REPO/hb-enhanced-opms/logs/hb_soak"
for account in "${ACCOUNTS[@]}"; do
  for coin in "${COINS[@]}"; do
    log="$REPO/hb-enhanced-opms/logs/hb_soak/perp_mm_${account}_${coin}_soak.decisions.jsonl"
    [[ -f "$log" ]] && mv "$log" "${log%.jsonl}.${STAMP}.jsonl"
  done
done

run_instance() {
  local account="$1" runtime="$RUNTIME_BASE/$1"
  (
    cd "$runtime"
    exec env OPMS_ENV_FILE="$ENV_FILE" CONFIG_PASSWORD="$PASSWORD" \
      OPMS_PORTFOLIO_STOP_DB="$RUNTIME_BASE/portfolio_stop.db" \
      OPMS_PORTFOLIO_MEMBERS="$PORTFOLIO_MEMBERS" \
      SCRIPT_CONFIG="opms_perp_mm_${account}_soak.yml" \
      PYTHONPATH="$runtime/bin:$runtime:$COMMON_PYTHONPATH" \
      timeout --signal=INT --kill-after=60 "$((DURATION + 120))" \
        "$PY" "$REPO/hb-enhanced-opms/deploy/hummingbot/scripts/run_hummingbot_isolated.py"
  ) >"$ARTIFACT_DIR/${account}_launcher.log" 2>&1 &
  HB_PID[$account]=$!
}

echo "starting dual live soak: duration=${DURATION}s artifact_dir=$ARTIFACT_DIR"
STARTED=1
for account in "${ACCOUNTS[@]}"; do
  run_instance "$account"
  "$PY" "$REPO/hb-enhanced-opms/scripts/monitor_hl_soak.py" \
    --account-id "$account" --pid "${HB_PID[$account]}" --duration "$DURATION" \
    --max-drawdown-pct "$MAX_DRAWDOWN_PCT" \
    --output "$ARTIFACT_DIR/${account}_monitor.jsonl" \
    >"$ARTIFACT_DIR/${account}_monitor.log" 2>&1 &
  MON_PID[$account]=$!
done

start_ts=$(date +%s)
deadline=$((start_ts + DURATION))
startup_checked=0
while (( $(date +%s) < deadline )); do
  for account in "${ACCOUNTS[@]}"; do
    if ! kill -0 "${HB_PID[$account]}" 2>/dev/null || \
       ! kill -0 "${MON_PID[$account]}" 2>/dev/null; then
      echo "$account launcher or monitor stopped before the soak deadline" >&2
      exit 1
    fi
  done
  if (( startup_checked == 0 && $(date +%s) >= start_ts + 90 )); then
    for account in "${ACCOUNTS[@]}"; do
      if ! rg -q "Strategy opms_perp_mm started successfully" "$ARTIFACT_DIR/${account}_launcher.log"; then
        echo "$account strategy did not start within 90 seconds" >&2
        exit 1
      fi
      for coin in "${COINS[@]}"; do
        log="$REPO/hb-enhanced-opms/logs/hb_soak/perp_mm_${account}_${coin}_soak.decisions.jsonl"
        [[ -s "$log" ]] || { echo "$account/$coin produced no decisions in 90 seconds" >&2; exit 1; }
      done
    done
    startup_checked=1
  fi
  sleep 1
done
COMPLETED=1
