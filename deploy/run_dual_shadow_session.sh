#!/usr/bin/env bash
# Run e2_mm1 and e2_mm2 as two independent real-Hummingbot shadow instances.
# Each instance gets its own Hummingbot checkout copy and encrypted connector
# store because the HL connector has one credential slot per process.
set -euo pipefail

DURATION="${1:-60}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HB_SOURCE="${HB_SOURCE:-/home/christian/sources/hummingbot}"
PY="${PY:-/home/christian/miniforge3/envs/hummingbot/bin/python}"
RUNTIME_BASE="${RUNTIME_BASE:-$(mktemp -d /tmp/dual-hb-shadow.XXXXXX)}"
ENV_FILE="${OPMS_ENV_FILE:-$REPO/.env}"
COMMON_PYTHONPATH="$REPO/hb-enhanced-opms/src:$REPO/perp-bot/src:$REPO/mm-core/src"

[[ "$DURATION" =~ ^[0-9]+$ ]] || { echo "duration must be an integer" >&2; exit 2; }
[[ "${OPMS_HB_MAINNET:-}" == "confirm" ]] || {
  echo "refusing mainnet connectors: set OPMS_HB_MAINNET=confirm" >&2
  exit 2
}
[[ -d "$HB_SOURCE/hummingbot" && -x "$HB_SOURCE/bin/hummingbot_quickstart.py" ]] || {
  echo "Hummingbot checkout not found at $HB_SOURCE" >&2
  exit 2
}
[[ -f "$HB_SOURCE/scripts/validate_hb_deploy_configs.py" ]] || {
  echo "run deploy/install_into_hummingbot.sh before the dual shadow" >&2
  exit 2
}
[[ -f "$ENV_FILE" ]] || { echo "environment file not found: $ENV_FILE" >&2; exit 2; }

(cd "$HB_SOURCE" &&
  OPMS_ENV_FILE="$ENV_FILE" PYTHONPATH="$HB_SOURCE:$COMMON_PYTHONPATH" \
  "$PY" scripts/validate_hb_deploy_configs.py)

RUNTIME_A="$RUNTIME_BASE/e2_mm1"
RUNTIME_B="$RUNTIME_BASE/e2_mm2"
mkdir -p "$RUNTIME_BASE"

prepare_runtime() {
  local runtime="$1"
  if [[ -e "$runtime" ]]; then
    echo "refusing to reuse existing runtime: $runtime" >&2
    echo "set RUNTIME_BASE to a new directory or remove the old test runtime" >&2
    exit 2
  fi
  mkdir -p "$runtime"
  # Hardlinks keep the two test checkouts cheap while giving Hummingbot a
  # different __file__ root, hence different conf/data/logs paths.
  cp -al "$HB_SOURCE/bin" "$runtime/bin"
  cp -al "$HB_SOURCE/hummingbot" "$runtime/hummingbot"
  cp -al "$HB_SOURCE/scripts" "$runtime/scripts"
  cp -al "$HB_SOURCE/controllers" "$runtime/controllers"
  cp -al "$HB_SOURCE/conf" "$runtime/conf"
  mkdir -p "$runtime/data" "$runtime/logs"
}

prepare_runtime "$RUNTIME_A"
prepare_runtime "$RUNTIME_B"

PASSWORD="${HB_PASSWORD:-}"
if [[ -z "$PASSWORD" ]]; then
  if [[ -f "$HB_SOURCE/conf/.password_verification" ]]; then
    echo "HB_SOURCE already has an encrypted store; set HB_PASSWORD to unlock it" >&2
    exit 2
  fi
  PASSWORD="$($PY -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi

import_account() {
  local runtime="$1" account="$2"
  (cd "$runtime" &&
    OPMS_ENV_FILE="$ENV_FILE" HB_PASSWORD="$PASSWORD" \
    PYTHONPATH="$runtime:$COMMON_PYTHONPATH" \
    "$PY" "$REPO/hb-enhanced-opms/scripts/import_hl_mainnet_credentials.py" \
      --account-id "$account")
}

import_account "$RUNTIME_A" e2_mm1
import_account "$RUNTIME_B" e2_mm2

LOG_A="$REPO/hb-enhanced-opms/logs/hb_shadow/perp_mm_e2_mm1_eth.decisions.jsonl"
LOG_B="$REPO/hb-enhanced-opms/logs/hb_shadow/perp_mm_e2_mm1_sol.decisions.jsonl"
LOG_C="$REPO/hb-enhanced-opms/logs/hb_shadow/perp_mm_e2_mm2_eth.decisions.jsonl"
LOG_D="$REPO/hb-enhanced-opms/logs/hb_shadow/perp_mm_e2_mm2_sol.decisions.jsonl"
for log in "$LOG_A" "$LOG_B" "$LOG_C" "$LOG_D"; do
  [[ -f "$log" ]] && mv "$log" "${log%.jsonl}.$(date +%Y%m%dT%H%M%S).jsonl"
done

run_instance() {
  local runtime="$1" config="$2" output="$3"
  (cd "$runtime" &&
    OPMS_ENV_FILE="$ENV_FILE" CONFIG_PASSWORD="$PASSWORD" \
    SCRIPT_CONFIG="$config" \
    PYTHONPATH="$runtime:$COMMON_PYTHONPATH" \
    timeout --signal=INT --kill-after=30 "$DURATION" \
      "$PY" "$REPO/hb-enhanced-opms/deploy/hummingbot/scripts/run_hummingbot_isolated.py" \
      >"$output" 2>&1) &
}

OUT_A="$RUNTIME_A/launcher.log"
OUT_B="$RUNTIME_B/launcher.log"
run_instance "$RUNTIME_A" opms_perp_mm_e2_mm1_shadow.yml "$OUT_A"
PID_A=$!
run_instance "$RUNTIME_B" opms_perp_mm_e2_mm2_shadow.yml "$OUT_B"
PID_B=$!

set +e
wait "$PID_A"; STATUS_A=$?
wait "$PID_B"; STATUS_B=$?
set -e

if [[ ! -s "$LOG_A" || ! -s "$LOG_B" || ! -s "$LOG_C" || ! -s "$LOG_D" ]]; then
  echo "dual shadow did not produce all four decision logs" >&2
  echo "e2_mm1 launcher: $OUT_A" >&2
  echo "e2_mm2 launcher: $OUT_B" >&2
  exit 1
fi
for output in "$OUT_A" "$OUT_B"; do
  if ! rg -q "Strategy opms_perp_mm started successfully" "$output"; then
    echo "Hummingbot strategy did not report successful startup: $output" >&2
    exit 1
  fi
  if rg -q -i "OrderExecutor|order (created|placed|filled|cancelled)" "$output"; then
    echo "shadow launcher emitted an order-related event: $output" >&2
    exit 1
  fi
done
is_expected_stop() {
  # timeout(1) returns 124 when it owns the exit, while Python commonly
  # returns 130 after handling the INT as KeyboardInterrupt. Both are normal
  # for this bounded shadow run; other statuses indicate a startup/runtime
  # failure.
  [[ "$1" -eq 0 || "$1" -eq 124 || "$1" -eq 130 || "$1" -eq 143 ]]
}
if ! is_expected_stop "$STATUS_A" || ! is_expected_stop "$STATUS_B"; then
  echo "dual shadow launcher failed: e2_mm1=$STATUS_A e2_mm2=$STATUS_B" >&2
  echo "e2_mm1 launcher: $OUT_A" >&2
  echo "e2_mm2 launcher: $OUT_B" >&2
  exit 1
fi

echo "dual shadow passed: e2_mm1 and e2_mm2 ran concurrently for ${DURATION}s"
echo "launcher logs: $OUT_A $OUT_B"
