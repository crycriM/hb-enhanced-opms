import asyncio
from unittest.mock import MagicMock

from opms.controllers.generic.perp_mm_controller import (
    PerpMMController,
    PerpMMControllerConfig,
)


def test_controller_passes_calibrated_venue_grid_to_keeper():
    config = PerpMMControllerConfig(
        id="ena",
        controller_name="perp_mm",
        controller_type="generic",
        connector_name="hyperliquid_perpetual",
        trading_pair="ENA-USD",
        venue="hyperliquid",
        quote_size=97,
        price_tick=0.00001,
    )

    controller = PerpMMController(config, MagicMock(), asyncio.Queue(), 5.0)

    assert controller.keeper.config.quote_size == 97
    assert controller.keeper.config.price_tick == 0.00001
