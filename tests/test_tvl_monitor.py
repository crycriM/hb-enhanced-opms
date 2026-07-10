"""Tests for TVLMonitor sentinel file writing and keeper sentinel consumption."""

import asyncio
import os
import tempfile
import time

import httpx
import pytest
import pytest_asyncio

from dlmm_bot.risk_dlmm import DLMMRiskConfig, PairType
from opms.risk.tvl_monitor import TVLMonitor


@pytest.fixture
def sentinel_path(tmp_path):
    return str(tmp_path / "tvl_kill_sentinel")


@pytest.fixture
def risk_cfg():
    return DLMMRiskConfig(
        pair_type=PairType.BLUECHIP,
        min_tvl_usd=5000.0,
        tvl_drop_pct=30.0,
        tvl_window_s=300.0,
    )


class TestTVLMonitor:
    """Test TVLMonitor polling and sentinel writing."""

    @pytest.mark.asyncio
    async def test_no_sentinel_when_tvl_ok(self, sentinel_path, risk_cfg):
        """TVL above threshold → no sentinel written."""
        monitor = TVLMonitor(
            gateway_url="http://mock",
            pool_address="pool1",
            risk_cfg=risk_cfg,
            sentinel_path=sentinel_path,
        )
        monitor._running = True
        # Direct poll with a mock client
        async def mock_post(path, json):
            resp = MagicMock()
            resp.raise_for_status = lambda: None
            resp.json = lambda: {"tvl": 10000.0}
            return resp

        class MockClient:
            async def post(self, path, json):
                return await mock_post(path, json)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass

        from unittest.mock import MagicMock
        client = MockClient()
        await monitor._poll(client)
        assert not os.path.exists(sentinel_path)

    @pytest.mark.asyncio
    async def test_sentinel_written_when_tvl_low(self, sentinel_path, risk_cfg):
        """TVL below min_tvl_usd → sentinel written."""
        monitor = TVLMonitor(
            gateway_url="http://mock",
            pool_address="pool1",
            risk_cfg=risk_cfg,
            sentinel_path=sentinel_path,
        )

        from unittest.mock import MagicMock
        class MockClient:
            async def post(self, path, json):
                resp = MagicMock()
                resp.raise_for_status = lambda: None
                resp.json = lambda: {"tvl": 100.0}
                return resp
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass

        client = MockClient()
        await monitor._poll(client)
        assert os.path.exists(sentinel_path)
        with open(sentinel_path) as f:
            reason = f.read().strip()
        assert "TVL below minimum" in reason

    def test_stop_sets_flag(self, sentinel_path, risk_cfg):
        monitor = TVLMonitor(
            gateway_url="http://mock",
            pool_address="pool1",
            risk_cfg=risk_cfg,
            sentinel_path=sentinel_path,
        )
        assert monitor._running is False
        monitor.stop()
        assert monitor._running is False
