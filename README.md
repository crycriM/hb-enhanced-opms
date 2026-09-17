# OPMS — Order and Position Management System

Hummingbot-based execution body: controllers, executors, and a Gateway bridge wired to the `mm_core` brain.

OPMS is the execution layer of the [amm-solution](https://github.com/amm-solution) spot and perpetual market-making system. It sits between the `mm_core` decision engine (the "brain") and Hummingbot (the "body"), translating `ExecIntent` from Keeper into venue-agnostic order specs, running execution algorithms, and collecting fill-level analytics.

> Current run status, known issues, and required local patches live in [`status.md`](status.md), not here.

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

The bridge also applies the shared schema-v2 `PortfolioExecIntent` generation
gate. Grouped generations are admitted/deduplicated in shadow-only mode; live
multi-account placement remains disabled until a central portfolio coordinator
owns cancellation acknowledgements and account routing.

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
is patched locally to read only `ctx.get("funding", "0")`. **Re-apply after
any Hummingbot update** — `scripts/run_hb_mainnet_smoke.py` catches the
regression if it's missing. See [`status.md`](status.md) for why.

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
triple, in the `{EXCHANGE}_{ACCOUNT_ID}_{CREDENTIAL_TYPE}` shape — e.g.
`PRIVATE_KEY`, `ACCOUNT_ADDRESS`, `IS_TESTNET`. The keeper's `account_id`
(set per `PerpPairConfig` / `PerpMMControllerConfig`) selects which
subaccount's credentials to use; there is one such triple per subaccount you
run.

The basket controllers run at **6x leverage**, targeting `+0.4 ETH / -4 SOL`
gross per account. The calibrated `max_position` values are inventory bounds,
not a request to fill the entire cap — size against current collateral (see
[`status.md`](status.md) for last-recorded balances).

`{EXCHANGE}_MAIN_*` holds the master-agent key set (used at
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
subaccount import overwrites a master import, and running multiple
subaccounts concurrently needs one HB instance per subaccount (or a future
connector-name split). Verify with `--dry-run` before importing.
- **`dex_executor`'s `AccountRegistry`:** reads `{EXCHANGE}_{ACCOUNT_ID}_...`
  vars from the environment live. This path is currently bypassed for live HL
  trading.

The `.env` keys above are the canonical shape both paths (and the import
script's one-off env vars) follow.

**⚠ `account_id` must stay consistent across the stack.** Each keeper config
assigns a distinct `account_id`, and that label threads into
`validate_account_topology`, decision logs, and the OPMS fills websocket
(`/ws/fills/hyperliquid?account_id=...`). Keep that slug identical between
`PerpPairConfig` and the corresponding `PerpMMControllerConfig` for a given
subaccount (so the connector's `vaultAddress` points at the matching
subaccount). Getting it wrong silently misroutes fills/logs even though the
trade still hits the right wallet.

### Hummingbot Gateway (for DEX connectors — optional for perp)

The **Hummingbot Gateway** is a separate HTTP service (default
`http://localhost:15888`) that Hummingbot's **DEX** connectors attach to. It is
**not** needed for the current Hyperliquid-perp path — that drives HB's
`hyperliquid_perpetual` connector in-process via `InProcessClient`. The Gateway
only comes into play for DEX venues (Meteora / DLMM), which is exactly the
"future DEX support" case.

> **Execution routing note:** hb-opms is the **only route for orders on perp
> DEX**. However, liquidity provision on Meteora with a precise bin profile is
> **not yet available via the HB Gateway** — those operations go through the
> sibling project [`solana-clmm-executor`](../solana-clmm-executor/)
> (JSON-lines signing boundary for Meteora DLMM deposits/withdrawals/refresh
> workflows). Until the Gateway can express exact bin ladders, Meteora LP
> execution depends on `solana-clmm-executor`; perp order routing does not.

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
`tests/` (whose conftest injects stubs into `sys.modules`). It skips
automatically when Hummingbot is not importable — see
[`status.md`](status.md) for bugs it has previously caught that the stub
suite missed.

```bash
pytest tests_real/ -q                                  # real-HB controller + connector structure
OPMS_HB_MAINNET=confirm pytest tests_real/ -q          # also reads the real HL mainnet connector (no orders)
```

The full network smoke — start the connector, call the real `on_start()` /
`update_processed_data()`, and check mid / funding / equity / analytics — is a
standalone script rather than a pytest test, because HB connectors spawn
background tasks that outlive pytest-asyncio's per-test event loop:

```bash
OPMS_HB_MAINNET=confirm python scripts/run_hb_mainnet_smoke.py --account-id <account-id>
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
  python scripts/run_hb_mainnet_quote_gate.py --account-id <account-id> --max-position 0.06 --dry-run
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
  python scripts/run_hb_mainnet_derisk_gate.py --account-id <account-id> --dry-run
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
- `conf/scripts/opms_perp_mm_<account>_shadow.yml` +
  `conf/controllers/perp_mm_<account>_{eth,sol}_shadow.yml` — the two tilted
  ETH/SOL basket legs for a given subaccount, `shadow_mode: true`, calibrated
  caps, 6x leverage, and a 5 s cycle. This set repeats per subaccount you run.

Validate both configs with Hummingbot's own loader and the deploy-time routing
checks (no network, no orders):

```bash
python scripts/validate_hb_deploy_configs.py
```

For a bounded live read-only check, `deploy/run_dual_shadow_session.sh` creates
two disposable Hummingbot runtimes, imports one subaccount into each encrypted
store, and starts both instances concurrently. It requires
`OPMS_HB_MAINNET=confirm`; set `HB_PASSWORD` only when reusing an existing
Hummingbot store. The runner keeps all four controllers in `shadow_mode`:

```bash
OPMS_HB_MAINNET=confirm deploy/run_dual_shadow_session.sh 1800
```

For an explicitly authorized single-account mainnet soak, use the disposable
single-account runner. It starts both calibrated ETH/SOL controllers at 6x for the
requested duration, monitors margin read-only, then cancels and market-closes
scoped state before verifying a flat account. The runner requires both
`OPMS_HB_MAINNET=confirm` and `OPMS_HB_PLACE_ORDERS=confirm`. All bounded
deploy runners use `run_hummingbot_isolated.py`, which starts the real
trading core headlessly with MQTT disabled and still performs normal
strategy/order shutdown on `SIGINT`/`SIGTERM`. See
[`status.md`](status.md) for the latest soak result before relying on the
behavioral safety gate.

```bash
OPMS_HB_MAINNET=confirm OPMS_HB_PLACE_ORDERS=confirm \
  deploy/run_single_live_soak.sh 1800
```

One-time: choose an HB password, put `HB_PASSWORD=...` in the monorepo `.env`,
then `python scripts/import_hl_mainnet_credentials.py --account-id <account-id>`.
A shadow session (no orders) followed by Phase-1 parity — the HB log replayed
through a standalone `Keeper` (`perp-bot/scripts/replay_decision_log.py`):

```bash
deploy/run_shadow_session.sh 1800   # seconds
```

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
