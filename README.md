# OPMS — Order and Position Management System

Hummingbot-based execution body: controllers, executors, and a Gateway bridge wired to the `mm_core` brain.

OPMS is the execution layer of the [amm-solution](https://github.com/amm-solution) spot and perpetual market-making system. It sits between the `mm_core` decision engine (the "brain") and Hummingbot (the "body"), translating `ExecIntent` from Keeper into venue-agnostic order specs, running execution algorithms, and collecting fill-level analytics.

## Architecture

```
┌──────────────────┐     ┌─────────────────────┐     ┌──────────────────────┐
│   mm_core        │───▶│  OPMS controllers    │───▶│  Hummingbot executors│
│   Keeper (brain) │  ↕  │  + InProcessClient  │  ↕  │  (body)              │
└──────────────────┘     └─────────────────────┘     └──────────────────────┘
                              │                          ▲
                              ▼                          │
                      ┌─────────────────────┐     ┌──────────────────────┐
                      │  OPMS executors     │────▶│  PnL + Markout       │
                      │  (AC / PA)          │     │  FillObserver        │
                      └─────────────────────┘     └──────────────────────┘
                              │
                              ▼
                      ┌─────────────────────┐
                      │  HistoricalProfile  │
                      │  Forecaster         │
                      │  (volume-aware AC)  │
                      └─────────────────────┘
```

### Core flow

1. **Controller** (`PerpMMController`) drives Keeper each HB cycle via `InProcessClient`. Keeper emits `ExecIntent`.
2. **Bridge** (`InProcessClient`, `intent_to_order_specs`) maps the intent to `OrderSpec`.
3. **Executors** (`ACScheduleExecutor`, `PassiveAggressiveExecutor`) consume specs and submit orders to the HB connector.
4. **Analytics** (`FillObserver`) watches fill events, computing PnL, markout, and slippage.
5. **Forecasting** (`HistoricalProfileForecaster`) provides volume-aware scheduling for AC executor.

## Modules

### `opms.controllers`

| Module | Purpose |
|--------|---------|
| `perp_mm_controller` | Hummingbot `ControllerBase` integration. The **only** module with `import hummingbot`. Drives Keeper via `InProcessClient` and translates `ExecIntent` → HB `ExecutorAction`. |
| `perp_mm_bridge` | HB-free bridge. `InProcessClient` duck-types `OpmsClient` for in-process Keeper communication. `intent_to_order_specs()` maps keeper decisions to venue-agnostic order specs. |

The perp controller uses `perp_bot`'s shared fail-closed margin-health
invariant. A failed, missing, malformed, or non-finite
`tokenToAvailableAfterMaintenance` reading is logged at critical severity and
fed to the keeper as zero available margin, forcing an emergency-exit decision
instead of silently disabling the stop.

### `opms.executors`

| Module | Purpose |
|--------|---------|
| `ac_schedule_executor` | **Almgren-Chriss** execution: optimal slice schedule with volume-aware time-shifting. Submits market orders paced by the AC schedule. |
| `passive_aggressive_executor` | **Passive-Aggressive V2** execution: child limit orders at L1, refreshed on timer, falling back to aggressive (market) orders on cycle expiry. |
| `_ac_math` | Pure Almgren-Chriss schedule math. No I/O, no asyncio, no service deps. HB-free and independently testable. |

### `opms.gateway`

| Module | Purpose |
|--------|---------|
| `exec_bridge` | `GatewayExecBridge` — HTTP client to the Hummingbot Gateway's DEX (Meteora/Jupiter) endpoints. HB-free (`httpx`), tested against a mock server. Only code that talks to the Gateway; needed for DEX venues, not for perp. |

### `opms.analytics`

| Module | Purpose |
|--------|---------|
| `fill_observer` | Subscribe to HB `OrderFilled` events. Feeds `mm_core.pnl.PnLLedger` (realized PnL, WAC position, spread capture), `mm_core.markout.MarkoutTracker` (toxicity at configurable horizons), and per-fill slippage tracking. |

### `opms.forecasting`

| Module | Purpose |
|--------|---------|
| `historical_profile` | Intraday volume-profile forecaster. Builds a per-hour-of-day rate from historical candles, forecasts volume for each time bucket. Falls back to uniform profile when data is absent. |

## Installation

OPMS depends on `mm_core`, `perp_bot`, and `dlmm_bot` (all local packages in
the amm-solution monorepo). **Hummingbot itself is NOT pip-installable from
PyPI** — its isolated sdist build fails on Cython `.pyx` globbing — so it
lives in its own conda env and the Python packages are installed *into* that
env.

### Hummingbot dependency deployment

Hummingbot is the "body" that actually places orders. Create its conda env
(named `hummingbot`; older docs call it `hummingbot-venv`), compile Hummingbot
itself, then install the monorepo deps + OPMS into that same env:

```bash
# 1. Create the conda env from the Hummingbot checkout's environment.yml
conda env create -f <hummingbot-checkout>/setup/environment.yml
conda activate hummingbot

# 2. Compile + install Hummingbot ITSELF. This is the step that makes
#    `import hummingbot` work: the checkout ships ~60 Cython .pyx sources and
#    no .so files, so without it `import hummingbot.connector.connector_base`
#    fails. `./install` does the same thing plus the env create/update.
cd <hummingbot-checkout>
pip install -e . --no-deps --no-build-isolation

# 3. Install the monorepo dependencies + OPMS into that same env
pip install -e ../mm-core
pip install -e ../perp-bot
pip install -e .          # this package (opms)
```

Verify the runtime is real before trusting it:

```bash
python -c "import hummingbot.connector.connector_base as cb; print(cb.__file__)"
python -m pytest tests_real/ -q     # real-HB tests, no stubs
```

⚠ **Local patch to the pinned checkout.** The HL WS funding parser
(`hyperliquid_perpetual_api_order_book_data_source.py::_parse_funding_info_message`)
used `ctx.get("openInterest", ctx.get("funding", "0"))` as the rate. HL's
`activeAssetCtx` payload carries both fields, so the connector reported open
interest (~1e6) as funding — which the keeper would accrue as funding PnL on
every tick. It is patched locally to `ctx.get("funding", "0")`. **Re-apply
after any Hummingbot update.** `scripts/run_hb_mainnet_smoke.py` fails if the
rate is implausible (≥ 0.01), so the regression is caught.

Notes:

- One venv per project (see the monorepo `AGENTS.md`). This is the deliberate
  exception — Hummingbot is conda-only.
- `<hummingbot-checkout>` is a local Hummingbot clone pinned to the version
  that carries the multi-subaccount `vaultAddress` fixes (connector issues
  [#6805](https://github.com/hummingbot/hummingbot/issues/6805) /
  [#7324](https://github.com/hummingbot/hummingbot/issues/7324)) — see
  `perp-bot/docs/account-naming.md` before trusting subaccount routing with
  real capital.
- Tests do **not** require a live Hummingbot runtime or a running connector:
  `conftest.py` stubs all HB types via `sys.modules` injection.

### Hyperliquid credentials & subaccounts

Credentials live in the repo-root `.env` (values are secrets — never commit
this file; add `.env` to `.gitignore`). Each subaccount has its own key
triple, in the `{EXCHANGE}_{ACCOUNT_ID}_{CREDENTIAL_TYPE}` shape:

| Subaccount | Keeper `account_id` | `.env` keys | Collateral |
|---|---|---|---|
| `HYPERLIQUID_E2_MM1` | `basket_a` (Sub A) | `HYPERLIQUID_E2_MM1_PRIVATE_KEY`, `HYPERLIQUID_E2_MM1_ACCOUNT_ADDRESS`, `HYPERLIQUID_E2_MM1_IS_TESTNET` | 300 USDC |
| `HYPERLIQUID_E2_MM2` | `basket_b` (Sub B) | `HYPERLIQUID_E2_MM2_PRIVATE_KEY`, `HYPERLIQUID_E2_MM2_ACCOUNT_ADDRESS`, `HYPERLIQUID_E2_MM2_IS_TESTNET` | 300 USDC |

`HYPERLIQUID_E2_MAIN_*` holds the master-agent key set (used at
`createSubAccount` time). See `perp-bot/docs/account-naming.md` for the full
subaccount-auth mechanics and the rule to use an **agent-wallet** key for
`_PRIVATE_KEY` (no withdraw rights), never the master's own key.

**How the credentials are used** — two paths exist and are not interchangeable:

- **Live path (`hb-enhanced-opms` + Hummingbot connector):** the wallet
  credentials are imported once into Hummingbot's encrypted
  `conf/connectors/` store — via `scripts/import_hl_testnet_credentials.py`
  (testnet) / `scripts/import_hl_mainnet_credentials.py` (mainnet) or HB's
  interactive `connect` command — and are **not** read from `.env` at
  runtime. The running controller's `PerpMMControllerConfig.connector_name`
  selects the wallet; `venue`/`account_id` are routing labels only.

**Mainnet subaccount routing.** `scripts/import_hl_mainnet_credentials.py`
imports either the master (`use_vault=False`) or a subaccount
(`use_vault=True`, `address=<subaccount>`, secret = the *same* master-approved
agent key), auto-detecting which from `HYPERLIQUID_MASTER_ACCOUNT_ADDRESS`.
HB's connector then signs every request with `vaultAddress=<subaccount>`
(`HyperliquidPerpetualAuth._vault_address`). ⚠ Hummingbot keys credentials by
**connector name**, and there is only one `hyperliquid_perpetual` slot — so a
subaccount import overwrites a master import, and running `e2_mm1` and
`e2_mm2` concurrently needs two separate HB instances (or a future
connector-name split). Verify with `--dry-run` before importing.
- **`dex_executor`'s `AccountRegistry`:** reads `{EXCHANGE}_{ACCOUNT_ID}_...`
  vars from the environment live. This path is currently bypassed for live HL
  trading.

The `.env` keys above are the canonical shape both paths (and the import
script's one-off env vars) follow.

**⚠ `account_id` must stay consistent across the stack.** `basket_config.py`
labels the two keepers `account_id="basket_a"` / `"basket_b"`; those labels
thread into `validate_account_topology`, decision logs, and the OPMS fills
websocket (`/ws/fills/hyperliquid?account_id=...`). Map `basket_a` →
`HYPERLIQUID_E2_MM1` and `basket_b` → `HYPERLIQUID_E2_MM2` and keep that slug
identical between `PerpPairConfig` and the corresponding `PerpMMControllerConfig`
(so the connector's `vaultAddress` points at the matching subaccount). Getting
it wrong silently misroutes fills/logs even though the trade still hits the
right wallet.

### Hummingbot Gateway (for DEX connectors — optional for perp)

The **Hummingbot Gateway** is a separate HTTP service (default
`http://localhost:15888`) that Hummingbot's **DEX** connectors attach to. It is
**not** needed for the current Hyperliquid-perp path — that drives HB's
`hyperliquid_perpetual` connector in-process via `InProcessClient`. The Gateway
only comes into play for DEX venues (Meteora / DLMM), which is exactly the
"future DEX support" case.

- The Gateway **ships with Hummingbot** — there is nothing separate to
  `pip install`. In the Docker deployment it runs alongside the HB container;
  the `GatewayExecBridge` (the *only* code that talks to it) runs as an ordinary
  host process and calls the Gateway over HTTP.
- `opms/gateway/exec_bridge.py` is **HB-free** (plain `httpx`, no `hummingbot`
  import) and fully testable against a mock HTTP server — `GatewayConfig`
  defaults to `gateway_url="http://localhost:15888"`. Point it at your running
  Gateway and configure a Meteora connector in the Gateway to go live.
- For perp-only work, skip this — stand up the conda env and connectors above
  and you're done.

## Testing

```bash
# All tests (HB mocked via conftest.py sys.modules injection)
pytest

# Individual test files
pytest tests/test_ac_math.py
pytest tests/test_pa_executor.py
pytest tests/test_ac_executor.py
pytest tests/test_fill_observer.py
pytest tests/test_perp_mm_bridge.py
pytest tests/test_historical_profile.py
```

Tests do not require a live Hummingbot runtime. The `conftest.py` injects stub modules covering all HB types (enums, events, data types) and the `ExecutorBase` interface.

**Run `tests_real/` in its own invocation** — it deliberately does *not* use the
stubs and imports the real Hummingbot, so it must not share a process with
`tests/` (whose conftest injects stubs into `sys.modules`):

```bash
pytest tests_real/ -q                                  # real-HB controller + connector structure
OPMS_HB_MAINNET=confirm pytest tests_real/ -q          # also reads the real HL mainnet connector (no orders)
```

The full network smoke — start the connector, call the real `on_start()` /
`update_processed_data()`, and check mid / funding / equity / analytics — is a
standalone script rather than a pytest test, because HB connectors spawn
background tasks that outlive pytest-asyncio's per-test event loop:

```bash
OPMS_HB_MAINNET=confirm python scripts/run_hb_mainnet_smoke.py --account-id e2_mm1
```

The **write** gate places real orders: the controller's own quote becomes two
tiny `LIMIT_MAKER` (HL `Alo`) orders through real `OrderExecutor`s, which must
rest only on the target subaccount, refresh, and cancel to 0 orders / 0
position. Prices are pushed `--passive-bps` through the touch so nothing fills,
notional is capped by `--max-notional`, and leftovers are force-cancelled (or
market-closed) through the raw HL SDK. Run `--dry-run` first; artifacts
(`gate.json`, `decisions.jsonl`, `gate.log`) land in `logs/quote_gate_<ts>/`:

```bash
OPMS_HB_MAINNET=confirm OPMS_HB_PLACE_ORDERS=confirm \
  python scripts/run_hb_mainnet_quote_gate.py --account-id e2_mm1 --max-position 0.06 --dry-run
```

HL rejects orders under $10 notional; quote size is 10% of `--max-position`,
so size it against the current mid.

The **de-risk** gate opens a real micro position (default 0.012 ETH, ~$30),
lets the keeper's own risk policy flatten it through a reduce-only
`PassiveAggressiveExecutor`, re-opens, injects a drawdown, and requires the
emergency exit to go flat within `--emergency-deadline` seconds. It drives the
controller at real control-cycle cadence (`--cycle-s`) and never executes quote
creates. Takes ~3–5 minutes:

```bash
OPMS_HB_MAINNET=confirm OPMS_HB_PLACE_ORDERS=confirm \
  python scripts/run_hb_mainnet_derisk_gate.py --account-id e2_mm1 --dry-run
```

### Real Hummingbot launch (`deploy/`)

`deploy/hummingbot/` mirrors the HB root and is symlinked into the checkout by
`deploy/install_into_hummingbot.sh` (re-run after an HB update):

- `scripts/opms_perp_mm.py` — `V2WithControllers` plus two start-time refusals
  HB lacks (its `add_controller()` only logs constructor errors): topology
  violations, and credentials that route to a different account than the
  controller's `account_id` (HB has one `hyperliquid_perpetual` slot).
- `controllers/generic/perp_mm.py` — re-exports `PerpMMController` where HB's
  loader looks (`controllers.<controller_type>.<controller_name>`).
- `conf/scripts/opms_perp_mm_e2_mm1_shadow.yml` +
  `conf/controllers/perp_mm_e2_mm1_eth_shadow.yml` — one untilted ETH
  controller on `e2_mm1`, `shadow_mode: true`, micro caps, 5 s cycle.

One-time: choose an HB password, put `HB_PASSWORD=...` in the monorepo `.env`,
then `python scripts/import_hl_mainnet_credentials.py --account-id e2_mm1`.
A shadow session (no orders) followed by Phase-1 parity — the HB log replayed
through a standalone `Keeper` (`perp-bot/scripts/replay_decision_log.py`):

```bash
deploy/run_shadow_session.sh 1800   # seconds
```

`tests_real/` skips automatically when Hummingbot is not importable. It caught
three real bugs the stub suite masked: `PerpMMController` imported a
`TwapExecutorConfig` that does not exist in Hummingbot (the real class is
`TWAPExecutorConfig`, with `total_amount_quote`/`total_duration`/
`order_interval`), the custom `PassiveAggressiveExecutorConfig` type was never
registered in HB's `ExecutorOrchestrator._executor_mapping` (so every
de-risk/emergency executor would have raised "Unsupported executor config
type"), and `_current_equity()` looked up `"USDC"` while HB's Hyperliquid
connector reports the balance under `"USD"` (silent zero equity). All three are
fixed; `PerpMMController` now registers the executor at import time and
resolves equity against the connector's actual balances.

## Dependency layout

```
hb-enhanced-opms/
├── pyproject.toml
├── src/opms/
│   ├── analytics/
│   │   └── fill_observer.py
│   ├── connectors/          # (reserved)
│   ├── controllers/
│   │   ├── perp_mm_bridge.py
│   │   └── perp_mm_controller.py
│   ├── executors/
│   │   ├── _ac_math.py
│   │   ├── ac_schedule_executor.py
│   │   └── passive_aggressive_executor.py
│   ├── forecasting/
│   │   └── historical_profile.py
   │   ├── gateway/             # GatewayExecBridge — HTTP client to the Hummingbot Gateway (DEX only)
│   ├── research/            # (reserved)
│   └── risk/                # (reserved)
└── tests/
    ├── conftest.py          # HB stubs + sys.modules injection
    ├── test_ac_math.py
    ├── test_ac_executor.py
    ├── test_historical_profile.py
    ├── test_pa_executor.py
    ├── test_perp_mm_bridge.py
    └── test_fill_observer.py
```

## Key interfaces

### `FillObserver`

```python
observer = FillObserver(venue="hyperliquid_perpetual", symbol="SOL-PERP")
observer.register(connector)
observer.update_mid(mid=current_mid)
# ... trading ...
report = observer.explain(mid=current_mid)
stats = observer.markout_stats()
slippage = observer.slippage_stats()
observer.unregister(connector)
```

### `HistoricalProfileForecaster`

```python
forecaster = HistoricalProfileForecaster(provider=HBCandlesProvider(connector, "SOL-PERP"))
volumes = await forecaster.forecast(symbol="SOL-PERP", num_buckets=12, bucket_seconds=300.0)
```

### `InProcessClient`

```python
client = InProcessClient()
client.on_snapshot(keeper._on_snapshot)
client.on_fill(keeper._on_fill)
client.on_error(keeper._on_error)
client.set_positions({"BTC": Position(coin="BTC", position=0.0, equity=1000.0)})
intent_to_order_specs(client.last_intent)
```
