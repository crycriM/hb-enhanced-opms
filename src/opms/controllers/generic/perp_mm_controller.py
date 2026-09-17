"""The one module that speaks Hummingbot for the perp keeper.

PerpMMController drives an unmodified perp_bot.keeper.Keeper each control
cycle via the InProcessClient bridge (perp_mm_bridge.py) and translates its
ExecIntent into HB executor actions. All quoting/risk logic lives in
mm_core + Keeper — nothing is re-implemented here.
"""

import asyncio
import logging
import os
from decimal import ROUND_CEILING, Decimal
from typing import List, Union

from pydantic import Field

from hummingbot.core.data_type.common import PositionAction, PriceType, TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.executor_orchestrator import ExecutorOrchestrator
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo

from mm_core.contracts import ExecIntent
from mm_core.inventory import Caps
from mm_core.risk_policy import RiskConfig

from perp_bot.config import PerpPairConfig
from perp_bot.keeper import Keeper
from perp_bot.margin_health import fail_closed_margin_available
from perp_bot.opms_client import Position

from opms.analytics.fill_observer import FillObserver
from opms.executors.passive_aggressive_executor import PassiveAggressiveExecutor, PassiveAggressiveExecutorConfig
from opms.resilience import QuoteLivenessWatchdog, VenueCircuitBreaker

from .perp_mm_bridge import (
    ExecutionRequest,
    InProcessClient,
    intent_is_quoting,
    intent_to_execution_request,
    intent_to_order_specs,
)

logger = logging.getLogger(__name__)

# HB's ExecutorOrchestrator only knows its built-in executors. Register the
# passive-aggressive executor so a CreateExecutorAction carrying our config
# type maps to a real executor instead of raising "Unsupported executor config
# type" when the controller routes a de-risk/emergency intent.
ExecutorOrchestrator._executor_mapping.setdefault(
    "passive_aggressive_executor", PassiveAggressiveExecutor
)
# Creating it is only half: ExecutorInfo.config is a pydantic discriminated
# union over HB's built-in configs, so `executor.executor_info` raises for a
# running PA — which breaks controller executor reports, get_active_executors,
# and MarketsRecorder.store_or_update_executor. Found live on mainnet.
_info_config = ExecutorInfo.model_fields["config"]
if PassiveAggressiveExecutorConfig not in getattr(_info_config.annotation, "__args__", ()):
    _info_config.annotation = Union[_info_config.annotation, PassiveAggressiveExecutorConfig]
    ExecutorInfo.model_rebuild(force=True)


def _config_notional(config) -> Decimal | None:
    """The size a running executor is working, across the executor config types
    the de-risk path can create (PA base, TWAP quote, plain order amount)."""
    for attr in ("total_amount_base", "total_amount_quote", "amount"):
        value = getattr(config, attr, None)
        if value is not None:
            return Decimal(str(value))
    return None


def _execution_signature(config) -> tuple:
    # Amount is part of the key (review #4): a *growing* de-risk need on an
    # already-running executor must be topped up (replace), not silently
    # dropped. A steady request keeps the same signature, so the child clock is
    # still preserved for the in-flight case.
    return (config.type, config.side,
            getattr(config, "child_order_time_limit", None), _config_notional(config))


class PerpMMControllerConfig(ControllerConfigBase):
    controller_type: str = "generic"
    connector_name: str = Field(json_schema_extra={"prompt": "Perpetual connector name: ", "prompt_on_new": True})
    trading_pair: str = Field(json_schema_extra={"prompt": "Trading pair (e.g. BTC-USD): ", "prompt_on_new": True})
    # perp_bot/OPMS venue key (perp_bot.venue_capabilities), a different
    # namespace from HB's connector_name — e.g. connector_name may be
    # "hyperliquid_perpetual_testnet" while venue is "hyperliquid".
    venue: str = Field(json_schema_extra={"prompt": "perp_bot venue key (e.g. hyperliquid): ", "prompt_on_new": True})
    account_id: str = "default"
    gamma: float = 0.5
    kappa: float = 0.3
    widen_factor: float = 2.0
    max_position: float = 10.0
    critical_position: float = 20.0
    # Persistent inventory tilt for a cross-hedged basket leg.  Risk limits
    # are evaluated relative to this target by perp_bot/mm_core.
    target_inventory: float = 0.0
    leverage: int = 1
    # The quote/collateral asset label the connector reports balances under.
    # Hummingbot's Hyperliquid connector uses "USD" (CONSTANTS.CURRENCY), not
    # "USDC" — _current_equity also falls back across common labels.
    collateral_asset: str = "USD"
    decision_log_path: str | None = None
    # Decide and log every cycle but emit no executor actions (keeper
    # shadow_mode: intents are recorded with intent_sent=False).
    shadow_mode: bool = False
    # Control-cycle cadence. HB's add_controller() never passes one, so without
    # this every controller runs at ControllerBase's 1 s default — and quotes
    # are cancel/replaced every cycle.
    update_interval: float = 5.0
    # Margin-health stop thresholds (see mm_core.risk_policy.RiskConfig):
    # ratio of available-after-maintenance to equity. Only used when the
    # connector can supply the margin figure.
    margin_health_soft: float = 0.20
    margin_health_hard: float = 0.10
    # The spot-clearinghouse margin read is a separate REST call from the
    # connector's own balance polling; cache a successful reading for this many
    # seconds so a fast control cadence does not double the account's /info load
    # and invite the rate limits the stop is meant to survive (review #2).
    margin_cache_ttl: float = 10.0
    # A quote stop first rests one reduce-only child, then crosses for whatever
    # remains. This bounds the time a maker fill can remain unquoted.
    quote_stop_time_limit: float = 15.0
    # Expected quote sides must acquire actual venue order ids within this
    # window. A miss opens a cooldown circuit and flattens residual inventory.
    quote_liveness_timeout: float = 15.0
    quote_recovery_cooldown: float = 30.0
    # Keep a confirmed live maker pair for this long before a two-phase
    # cancel-then-create refresh. Never overlap old and new target-sized quotes.
    quote_refresh_interval: float = 20.0
    # Explicit deadlines and rate-limit-aware backoff for controller-owned HL
    # requests (margin state and post-fill position reconciliation).
    venue_request_timeout: float = 5.0
    venue_failure_threshold: int = 3
    venue_backoff_initial: float = 2.0
    venue_backoff_max: float = 60.0
    control_cycle_timeout: float = 15.0
    # On a hedge-mode venue topology allows sibling controllers to share a
    # (coin, account); set this to "long"/"short" so _current_base_position
    # reports only this controller's leg and not a sibling's opposite exposure.
    # Leave unset for net-mode venues (single controller per market/account).
    position_side: str | None = None

    @property
    def coin(self) -> str:
        return self.trading_pair.split("-")[0]

    def update_markets(self, markets):
        return markets.add_or_update(self.connector_name, self.trading_pair)


class PerpMMController(ControllerBase):
    def __init__(self, config: PerpMMControllerConfig, *args, **kwargs):
        if len(args) < 3:  # (market_data_provider, actions_queue, update_interval)
            kwargs.setdefault("update_interval", config.update_interval)
        super().__init__(config, *args, **kwargs)
        self.config = config
        pair_config = PerpPairConfig(
            coin=config.coin,
            gamma=config.gamma,
            kappa=config.kappa,
            widen_factor=config.widen_factor,
            exchange=config.venue,
            account_id=config.account_id,
            target_inventory=config.target_inventory,
            leverage=config.leverage,
            caps=Caps(max_position=config.max_position, critical_position=config.critical_position),
            risk=RiskConfig(
                margin_health_soft=config.margin_health_soft,
                margin_health_hard=config.margin_health_hard,
            ),
        )
        self._client = InProcessClient()
        if config.decision_log_path:
            os.makedirs(os.path.dirname(os.path.abspath(config.decision_log_path)), exist_ok=True)
        self.keeper = Keeper(self._client, pair_config, decision_log_path=config.decision_log_path,
                             shadow_mode=config.shadow_mode)
        self._client.on_snapshot(self.keeper._on_snapshot)
        self._client.on_fill(self.keeper._on_fill)
        self._client.on_error(self.keeper._on_error)
        self._fill_observer = FillObserver(
            venue=config.connector_name,
            symbol=config.trading_pair,
        )
        self._settled_executor_ids: set[str] = set()
        self._margin_cache: tuple[float, float] | None = None
        self._venue_circuit = VenueCircuitBreaker(
            failure_threshold=config.venue_failure_threshold,
            base_backoff_s=config.venue_backoff_initial,
            max_backoff_s=config.venue_backoff_max,
        )
        self._quote_liveness = QuoteLivenessWatchdog(
            timeout_s=config.quote_liveness_timeout,
            recovery_cooldown_s=config.quote_recovery_cooldown,
        )
        self._quote_refresh_pending = False

    def _ensure_resilience_state(self) -> None:
        """Initialise safety state for normal and object.__new__ test paths."""
        if not hasattr(self, "_venue_circuit"):
            self._venue_circuit = VenueCircuitBreaker(
                failure_threshold=getattr(self.config, "venue_failure_threshold", 3),
                base_backoff_s=getattr(self.config, "venue_backoff_initial", 2.0),
                max_backoff_s=getattr(self.config, "venue_backoff_max", 60.0),
            )
        if not hasattr(self, "_quote_liveness"):
            self._quote_liveness = QuoteLivenessWatchdog(
                timeout_s=getattr(self.config, "quote_liveness_timeout", 15.0),
                recovery_cooldown_s=getattr(self.config, "quote_recovery_cooldown", 30.0),
            )
        if not hasattr(self, "_quote_refresh_pending"):
            self._quote_refresh_pending = False

    async def on_start(self):
        connector = self.market_data_provider.get_connector(self.config.connector_name)
        self._fill_observer.register(connector)

    def on_stop(self):
        connector = self.market_data_provider.get_connector(self.config.connector_name)
        self._fill_observer.unregister(connector)

    async def control_task(self):
        """Bound the whole decision cycle and always reach fail-safe actions.

        ControllerBase skips ``determine_executor_actions`` when an update
        raises. That would leave the previous quotes resting indefinitely on a
        hung venue read. We instead turn a deadline/transport failure into an
        emergency flatten intent, then enqueue its cancel/close actions.
        """
        if not (self.market_data_provider.ready and self.executors_update_event.is_set()):
            return
        try:
            await asyncio.wait_for(
                self.update_processed_data(),
                timeout=getattr(self.config, "control_cycle_timeout", 15.0),
            )
        except Exception as exc:
            self._ensure_resilience_state()
            now = self.market_data_provider.time()
            if self._venue_circuit.allow_request(now):
                self._venue_circuit.record_failure(now, exc)
            logger.error(
                "%s control cycle failed; forcing quote cancel/flatten: %s: %s",
                self.config.trading_pair, type(exc).__name__, exc,
                exc_info=True,
            )
            self._force_flatten_intent()

        actions = self.determine_executor_actions()
        if actions:
            await self.send_actions(actions)

    def _force_flatten_intent(self) -> None:
        try:
            current = float(self._current_base_position())
        except Exception:
            current = float(getattr(self.keeper._inventory, "position", 0.0))
        self._client.last_intent = ExecIntent(
            venue=self.config.venue,
            coin=self.config.coin,
            account_id=self.config.account_id,
            target_inventory=0.0,
            current_inventory=current,
            quote=None,
            urgency="emergency",
            strategy_hint="passive_aggressive",
        )

    async def _refresh_positions_after_fills(self) -> None:
        """Re-read venue positions once an executor that traded has finished.

        The connector's position cache polls every 5–12 s (HL). In the cycle
        right after a de-risk completes it still shows the pre-fill size, so
        the keeper re-issues a de-risk for inventory that is already gone —
        seen live on mainnet as a second reduce-only PA rejected 9× with
        "Reduce only order would increase position".
        """
        current = self.executors_info
        # Drop ids that have aged out of the live executor list so the set stays
        # bounded to the process's active window (review #5).
        self._settled_executor_ids &= {e.id for e in current}
        traded = {e.id for e in current if e.is_done and e.filled_amount_quote > 0}
        pending = traded - self._settled_executor_ids
        if pending:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            try:
                await self._venue_request(
                    connector._update_positions,
                    operation="post-fill position refresh",
                )
            except Exception:
                # Do not mark the ids settled: a half-open probe retries after
                # backoff. Margin then fails closed through the same circuit.
                return
            self._settled_executor_ids |= pending

    async def _venue_request(self, request_factory, *, operation: str):
        """Run one controller-owned venue request under deadline/backoff."""
        self._ensure_resilience_state()
        now = self.market_data_provider.time()
        if not self._venue_circuit.allow_request(now):
            state = self._venue_circuit.snapshot(now)
            raise RuntimeError(
                f"{operation} blocked by venue circuit for "
                f"{state['retry_in_s']:.1f}s after {state['last_error']}"
            )
        try:
            result = await asyncio.wait_for(
                request_factory(),
                timeout=getattr(self.config, "venue_request_timeout", 5.0),
            )
        except Exception as exc:
            delay = self._venue_circuit.record_failure(now, exc)
            state = self._venue_circuit.snapshot(now)
            logger.warning(
                "%s failed (%s); backing off %.1fs, circuit=%s",
                operation, state["last_error"], delay, state["state"],
            )
            raise
        self._venue_circuit.record_success()
        return result

    async def update_processed_data(self):
        await self._refresh_positions_after_fills()
        mid = self.get_current_price(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        funding_info = self.market_data_provider.get_funding_info(self.config.connector_name, self.config.trading_pair)
        funding_rate = float(funding_info.rate) if funding_info is not None else None

        self._fill_observer.update_mid(float(mid))

        self._client.set_positions({
            self.config.coin: Position(
                coin=self.config.coin,
                position=float(self._current_base_position()),
                equity=float(self._current_equity()),
                margin_available=await self._current_margin_available(),
            )
        })

        await self.keeper._on_snapshot({
            "ts": self.market_data_provider.time(),
            "mid": float(mid),
            "funding_rate": funding_rate,
        })
        await self.keeper._tick()

    def _current_base_position(self) -> Decimal:
        # Venue truth from the connector, not HB's `positions_held`: that list
        # is executor bookkeeping and only counts executors closed with
        # POSITION_HOLD, so passive-aggressive de-risk fills never reach it and
        # the keeper would keep de-risking an already-flat book. Topology only
        # guarantees one controller per (coin, account) on a *net* venue; a
        # hedge-mode venue may host siblings on opposite legs, so when
        # `position_side` is set we sum only this controller's leg (review #3).
        connector = self.market_data_provider.get_connector(self.config.connector_name)
        own_side = (self.config.position_side or "").upper()
        total = Decimal("0")
        for p in connector.account_positions.values():
            if p.trading_pair != self.config.trading_pair:
                continue
            if own_side and p.position_side.name != own_side:
                continue
            total += Decimal(str(p.amount))
        return total

    async def _current_margin_available(self) -> float:
        """Venue-computed liquidation distance for the margin-health stop.

        On HL unified accounts this is spotClearinghouseState
        .tokenToAvailableAfterMaintenance (token index 0 = USDC) = spot
        total − cross maintenance margin used, read through the connector's
        own rate-limited REST machinery. Invalid data and read failures use
        the keeper's shared fail-closed invariant: log critically and return
        zero, which the risk policy treats as an emergency-exit breach."""
        connector = self.market_data_provider.get_connector(self.config.connector_name)
        now = self.market_data_provider.time()
        self._ensure_resilience_state()
        if not self._venue_circuit.allow_request(now):
            state = self._venue_circuit.snapshot(now)
            return fail_closed_margin_available(
                None,
                source=(
                    f"{self.config.connector_name} venue circuit open for "
                    f"{state['retry_in_s']:.1f}s after {state['last_error']}"
                ),
            )
        cached = getattr(self, "_margin_cache", None)
        if cached is not None and now - cached[1] < self.config.margin_cache_ttl:
            return cached[0]
        try:
            from hummingbot.connector.derivative.hyperliquid_perpetual import (
                hyperliquid_perpetual_constants as hl_constants,
            )
            spot = await self._venue_request(
                lambda: connector._api_post(
                    path_url=hl_constants.ACCOUNT_INFO_URL,
                    data={
                        "type": hl_constants.SPOT_USER_STATE_TYPE,
                        "user": connector.hyperliquid_perpetual_address,
                    },
                ),
                operation="spot clearinghouse margin read",
            )
            avail = {
                int(token): value
                for token, value in spot.get("tokenToAvailableAfterMaintenance", [])
            }
            value = fail_closed_margin_available(
                avail.get(0),
                source=f"{self.config.connector_name} spot clearinghouse state",
            )
        except Exception as exc:
            # Fail closed for this cycle but never cache the failure: the next
            # cycle retries so a transient error cannot mask a real breach.
            return fail_closed_margin_available(
                None,
                source=(
                    f"{self.config.connector_name} spot clearinghouse read failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
        self._margin_cache = (value, now)
        return value

    def _current_equity(self) -> Decimal:
        # Cross-margined account value (collateral + unrealized PnL), resolved
        # against the connector's actual balances. HB's Hyperliquid connector
        # labels the balance "USD" (CONSTANTS.CURRENCY), not "USDC" — trusting
        # the label alone yields a silent 0 equity.
        balances = self.market_data_provider.get_connector(
            self.config.connector_name
        ).get_all_balances()
        if balances.get(self.config.collateral_asset):
            return Decimal(str(balances[self.config.collateral_asset]))
        for asset in ("USD", "USDC", "USDT"):
            if balances.get(asset):
                return Decimal(str(balances[asset]))
        if len(balances) == 1:
            return Decimal(str(next(iter(balances.values()))))
        return Decimal("0")

    def _min_child_quantity(self) -> Decimal:
        """Smallest child the venue accepts: min notional at the current mid,
        with 10% headroom for price drift, rounded up to the size step."""
        rules = self.market_data_provider.get_trading_rules(
            self.config.connector_name, self.config.trading_pair
        )
        mid = Decimal(str(self.get_current_price(
            self.config.connector_name, self.config.trading_pair, PriceType.MidPrice
        )))
        step = rules.min_base_amount_increment
        # HB uses Decimal('NaN') as the no-price sentinel and NaN is truthy, so
        # `not mid` alone lets it through and blows up min()/max() downstream on
        # a thin/disconnected book — precisely the DE_RISK path (review #1).
        if not mid or not mid.is_finite() or not rules.min_notional_size or not step:
            return Decimal("0")
        raw = rules.min_notional_size * Decimal("1.1") / mid
        return (raw / step).to_integral_value(rounding=ROUND_CEILING) * step

    def _execution_actions(self, req: ExecutionRequest) -> list[ExecutorAction]:
        ts = self.market_data_provider.time()
        side = TradeType.BUY if req.side == "buy" else TradeType.SELL
        amount = Decimal(str(req.amount))
        position_action = PositionAction.CLOSE if req.reduce_only else PositionAction.OPEN

        if req.urgency in ("passive", "normal"):
            child = min(amount, max(amount / 5, self._min_child_quantity()))
            config = PassiveAggressiveExecutorConfig(
                timestamp=ts,
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                side=side,
                total_amount_base=amount,
                child_order_quantity=child,
                child_order_time_limit=60.0,
                child_order_refresh_time=20.0,
                leverage=self.config.leverage,
                position_action=position_action,
            )
            return [CreateExecutorAction(controller_id=self.config.id, executor_config=config)]

        if req.urgency == "immediate":
            # STOP_QUOTING is the keeper's immediate path. Work the entire
            # residual as one reduce-only passive child, then cross after a
            # short fixed deadline. HB's native TWAP always sends OPEN and is
            # therefore unsafe for a flatten (it can flip through zero).
            time_limit = getattr(self.config, "quote_stop_time_limit", 15.0)
            config = PassiveAggressiveExecutorConfig(
                timestamp=ts,
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                side=side,
                total_amount_base=amount,
                child_order_quantity=amount,
                child_order_time_limit=time_limit,
                child_order_refresh_time=time_limit,
                leverage=self.config.leverage,
                position_action=position_action,
            )
            return [CreateExecutorAction(controller_id=self.config.id, executor_config=config)]

        # "emergency"
        min_size = self.market_data_provider.get_trading_rules(
            self.config.connector_name, self.config.trading_pair
        ).min_order_size
        if Decimal(str(req.amount)) < min_size:
            config = OrderExecutorConfig(
                timestamp=ts,
                trading_pair=self.config.trading_pair,
                connector_name=self.config.connector_name,
                side=side,
                amount=Decimal(str(req.amount)),
                price=None,
                execution_strategy=ExecutionStrategy.MARKET,
                position_action=PositionAction.CLOSE if req.reduce_only else PositionAction.OPEN,
                leverage=self.config.leverage,
            )
            return [CreateExecutorAction(controller_id=self.config.id, executor_config=config)]

        config = PassiveAggressiveExecutorConfig(
            timestamp=ts,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=side,
            total_amount_base=Decimal(str(req.amount)),
            child_order_quantity=Decimal(str(req.amount)),
            # 5 s passive attempt, then market: the Phase 2 gate requires the
            # emergency market order within 10 s of the decision, and a 10 s
            # limit measured 12.2 s live (executor tick + cancel ack on top).
            # With refresh == limit the refresh branch never fires — one
            # passive attempt, by design.
            child_order_time_limit=5.0,
            child_order_refresh_time=5.0,
            leverage=self.config.leverage,
            position_action=position_action,
        )
        return [CreateExecutorAction(controller_id=self.config.id, executor_config=config)]

    def determine_executor_actions(self) -> List[ExecutorAction]:
        if self.config.shadow_mode:
            return []
        self._ensure_resilience_state()
        active = self.get_active_executors(
            connector_names=[self.config.connector_name],
            trading_pairs=[self.config.trading_pair],
        )
        keep: set[str] = set()

        if intent_is_quoting(self._client.last_intent):
            specs = intent_to_order_specs(self._client.last_intent)
            now = self.market_data_provider.time()
            execution_active = [e for e in active if not self._is_quote_executor(e)]
            if execution_active:
                # A quote-stop flatten remains authoritative if the regime
                # reopens before its PA executor finishes.
                self._quote_liveness.suspend()
                self._quote_refresh_pending = False
                keep = {e.id for e in execution_active}
                new_actions = []
            else:
                expected = {s.side for s in specs if s.side is not None}
                live = self._live_quote_sides(active)
                if self._quote_refresh_pending:
                    # Old quote executors are shutting down. Their temporary
                    # lack of venue ids is planned, not a liveness incident.
                    self._quote_liveness.suspend()
                    if active:
                        new_actions = []
                    else:
                        self._quote_refresh_pending = False
                        new_actions = self._quote_create_actions(specs, now)
                else:
                    tripped = (
                        not self._quote_liveness.allow_quotes(now)
                        or self._quote_liveness.observe(now, expected, live)
                    )
                    if tripped:
                        logger.error(
                            "%s quote liveness circuit open: expected=%s live=%s",
                            self.config.trading_pair, sorted(expected), sorted(live),
                        )
                        req = self._quote_failsafe_request(self._client.last_intent)
                        # Cancel confirmation precedes the close. A same-batch
                        # close can be mis-sized by a quote that fills while
                        # its executor is still shutting down.
                        new_actions = (
                            self._execution_actions(req)
                            if req and not active else []
                        )
                    elif active:
                        # Refresh in two phases. Stop old quote executors now and
                        # create replacements only after HB reports them inactive.
                        # This prevents cancel/create overlap from allowing two
                        # individually-capped fills to cross the structural target.
                        active_quotes = [e for e in active if self._is_quote_executor(e)]
                        configured_sides = {
                            self._quote_side(e.config.side)
                            for e in active_quotes
                        }
                        oldest_age = max(
                            now - float(getattr(e.config, "timestamp", now))
                            for e in active_quotes
                        )
                        exact_executor_set = (
                            configured_sides == expected
                            and len(active_quotes) == len(expected)
                        )
                        waiting_for_order_ids = not expected.issubset(live)
                        still_fresh = oldest_age < getattr(
                            self.config, "quote_refresh_interval", 20.0
                        )
                        if exact_executor_set and (waiting_for_order_ids or still_fresh):
                            keep = {e.id for e in active}
                        else:
                            self._quote_refresh_pending = True
                            self._quote_liveness.suspend()
                        new_actions = []
                    else:
                        new_actions = self._quote_create_actions(specs, now)
        else:
            self._quote_liveness.suspend()
            self._quote_refresh_pending = False
            req = intent_to_execution_request(self._client.last_intent)
            quote_active = [e for e in active if self._is_quote_executor(e)]
            new_actions = (
                self._execution_actions(req)
                if req and not quote_active else []
            )
            # HB calls this every control cycle. An execution executor already
            # working the same request keeps running: stop/recreate would reset
            # its child clock each cycle, so the passive-aggressive fallback —
            # and with it an emergency exit — could never fire. A different
            # side or urgency (cycle length) still replaces it.
            if new_actions:
                wanted = _execution_signature(new_actions[0].executor_config)
                keep = {e.id for e in active if _execution_signature(e.config) == wanted}
                if keep:
                    new_actions = []
            elif quote_active:
                # Preserve any already-running close while quote executors are
                # being drained; only the quote executors receive stop actions.
                keep = {e.id for e in active if not self._is_quote_executor(e)}

        stops = [
            StopExecutorAction(controller_id=self.config.id, executor_id=e.id)
            for e in active if e.id not in keep
        ]
        return stops + new_actions

    @staticmethod
    def _is_quote_executor(executor) -> bool:
        config = executor.config
        strategy = getattr(config, "execution_strategy", None)
        value = getattr(strategy, "value", strategy)
        return config.type == "order_executor" and value == "LIMIT_MAKER"

    def _live_quote_sides(self, active) -> set[str]:
        live: set[str] = set()
        for executor in active:
            if not self._is_quote_executor(executor):
                continue
            info = getattr(executor, "custom_info", {}) or {}
            if not info.get("order_id"):
                continue
            live.add(self._quote_side(executor.config.side))
        return live

    @staticmethod
    def _quote_side(side) -> str:
        """Normalize HB's numeric TradeType enum to the bridge side labels."""
        if side == TradeType.BUY or getattr(side, "name", "").upper() == "BUY":
            return "buy"
        if side == TradeType.SELL or getattr(side, "name", "").upper() == "SELL":
            return "sell"
        return str(getattr(side, "value", side)).lower()

    def _quote_create_actions(self, specs, timestamp: float) -> list[ExecutorAction]:
        return [
            CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=OrderExecutorConfig(
                    timestamp=timestamp,
                    trading_pair=self.config.trading_pair,
                    connector_name=self.config.connector_name,
                    side=TradeType.BUY if spec.side == "buy" else TradeType.SELL,
                    amount=Decimal(str(spec.amount)),
                    price=Decimal(str(spec.price)) if spec.price is not None else None,
                    execution_strategy=(
                        ExecutionStrategy.LIMIT_MAKER
                        if spec.price is not None else ExecutionStrategy.MARKET
                    ),
                    position_action=(
                        PositionAction.CLOSE if spec.reduce_only else PositionAction.OPEN
                    ),
                    leverage=self.config.leverage,
                ),
            )
            for spec in specs if spec.side is not None and spec.amount > 0
        ]

    @staticmethod
    def _quote_failsafe_request(intent: ExecIntent) -> ExecutionRequest | None:
        current = intent.current_inventory or 0.0
        if abs(current) < 1e-12:
            return None
        return ExecutionRequest(
            side="sell" if current > 0 else "buy",
            amount=abs(current),
            urgency="immediate",
            reduce_only=True,
        )

    def get_custom_info(self) -> dict:
        self._ensure_resilience_state()
        now = self.market_data_provider.time()
        mid = float(
            self.get_current_price(
                self.config.connector_name, self.config.trading_pair, PriceType.MidPrice
            )
        )
        return {
            "fill_pnl": self._fill_observer.explain(mid=mid),
            "markout": self._fill_observer.markout_stats(),
            "slippage": self._fill_observer.slippage_stats(),
            "quote_liveness": self._quote_liveness.snapshot(now),
            "venue_circuit": self._venue_circuit.snapshot(now),
        }
