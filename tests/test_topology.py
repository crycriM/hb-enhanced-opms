"""Topology validation tests."""

import pytest
from types import SimpleNamespace

from opms.connectors.topology import (
    VenueCapabilities,
    get_venue_capabilities,
    restrict_hyperliquid_market_scope,
    validate_controller_topology,
)

# Minimal config stub
class MockConfig:
    def __init__(self, connector_name, trading_pair, account_id="default"):
        self.connector_name = connector_name
        self.trading_pair = trading_pair
        self.account_id = account_id


class TestVenueCapabilities:

    def test_hyperliquid_net(self):
        caps = get_venue_capabilities("hyperliquid_perpetual")
        assert caps.position_mode == "net"
        assert caps.supports_same_account_hedge is False

    def test_aster_hedge(self):
        caps = get_venue_capabilities("aster_perpetual")
        assert caps.position_mode == "hedge"
        assert caps.supports_same_account_hedge is True

    def test_lighter_net_matches_perp_bot(self):
        # regression: this module's own table still said hedge after perp_bot was fixed
        assert get_venue_capabilities("lighter_perpetual").position_mode == "net"

    def test_testnet_connector_suffix(self):
        assert get_venue_capabilities("hyperliquid_perpetual_testnet").position_mode == "net"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown connector"):
            get_venue_capabilities("nonexistent")

    def test_case_insensitive(self):
        caps = get_venue_capabilities("Hyperliquid_Perpetual")
        assert caps.position_mode == "net"


class TestValidateControllerTopology:

    def test_one_per_coin(self):
        configs = [MockConfig("hyperliquid_perpetual", "BTC-USD")]
        result = validate_controller_topology(configs)
        assert result is not None

    def test_two_same_coin_raises(self):
        configs = [
            MockConfig("hyperliquid_perpetual", "BTC-USD"),
            MockConfig("hyperliquid_perpetual", "BTC-USD"),
        ]
        with pytest.raises(ValueError, match="Netted venue"):
            validate_controller_topology(configs)

    def test_two_same_coin_diff_accounts(self):
        configs = [
            MockConfig("hyperliquid_perpetual", "BTC-USD", account_id="acc1"),
            MockConfig("hyperliquid_perpetual", "BTC-USD", account_id="acc2"),
        ]
        result = validate_controller_topology(configs)
        assert len(result) == 2

    def test_two_aster_same_coin_passes(self):
        configs = [
            MockConfig("aster_perpetual", "BTC-USD"),
            MockConfig("aster_perpetual", "BTC-USD"),
        ]
        result = validate_controller_topology(configs)
        assert len(result) == 2

    def test_two_different_coins(self):
        configs = [
            MockConfig("hyperliquid_perpetual", "BTC-USD"),
            MockConfig("hyperliquid_perpetual", "SOL-USD"),
        ]
        result = validate_controller_topology(configs)
        assert len(result) == 2


def test_native_hyperliquid_configs_disable_unneeded_hip3_polling():
    connector = SimpleNamespace(_enable_hip3_markets=True)
    configs = [
        MockConfig("hyperliquid_perpetual", "ETH-USD"),
        MockConfig("hyperliquid_perpetual", "SOL-USD"),
    ]

    restrict_hyperliquid_market_scope(configs, {"hyperliquid_perpetual": connector})

    assert connector._enable_hip3_markets is False


def test_hip3_config_keeps_hip3_polling_enabled():
    connector = SimpleNamespace(_enable_hip3_markets=True)
    configs = [MockConfig("hyperliquid_perpetual", "xyz:XYZ100-USD")]

    restrict_hyperliquid_market_scope(configs, {"hyperliquid_perpetual": connector})

    assert connector._enable_hip3_markets is True
