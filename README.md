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

### `opms.executors`

| Module | Purpose |
|--------|---------|
| `ac_schedule_executor` | **Almgren-Chriss** execution: optimal slice schedule with volume-aware time-shifting. Submits market orders paced by the AC schedule. |
| `passive_aggressive_executor` | **Passive-Aggressive V2** execution: child limit orders at L1, refreshed on timer, falling back to aggressive (market) orders on cycle expiry. |
| `_ac_math` | Pure Almgren-Chriss schedule math. No I/O, no asyncio, no service deps. HB-free and independently testable. |

### `opms.analytics`

| Module | Purpose |
|--------|---------|
| `fill_observer` | Subscribe to HB `OrderFilled` events. Feeds `mm_core.pnl.PnLLedger` (realized PnL, WAC position, spread capture), `mm_core.markout.MarkoutTracker` (toxicity at configurable horizons), and per-fill slippage tracking. |

### `opms.forecasting`

| Module | Purpose |
|--------|---------|
| `historical_profile` | Intraday volume-profile forecaster. Builds a per-hour-of-day rate from historical candles, forecasts volume for each time bucket. Falls back to uniform profile when data is absent. |

## Installation

OPMS depends on `mm_core` and `perp_bot` (both local packages in the amm-solution monorepo).

**hummingbot is NOT pip-installable from PyPI.** Install via conda first:

```bash
conda env create -f <hummingbot-checkout>/setup/environment.yml
conda activate hummingbot-venv
pip install -e .
```

Or install all three (mm_core, perp_bot, opms) inside the Hummingbot conda env.

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

## Dependency layout

```
opms/
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
│   ├── gateway/             # (reserved)
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
