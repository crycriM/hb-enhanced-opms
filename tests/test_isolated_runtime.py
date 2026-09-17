import asyncio

import pytest

from opms.isolated_runtime import run_isolated_hummingbot


@pytest.mark.asyncio
async def test_runtime_starts_headless_strategy_without_mqtt_and_shuts_down():
    calls = []

    class _TradingCore:
        async def shutdown(self):
            calls.append("shutdown")

    class _App:
        trading_core = _TradingCore()

    async def bootstrap(config, secrets, **kwargs):
        calls.append(("bootstrap", kwargs))
        return _App()

    async def load(app, **kwargs):
        calls.append(("load", kwargs))
        return True

    async def gateway(app):
        calls.append("gateway")

    async def stop_immediately():
        calls.append("wait")

    app = await run_isolated_hummingbot(
        client_config=object(),
        secrets_manager=object(),
        script_config="opms.yml",
        bootstrap=bootstrap,
        load_and_start=load,
        wait_for_gateway=gateway,
        wait_for_shutdown=stop_immediately,
    )

    assert isinstance(app, _App)
    bootstrap_kwargs = calls[0][1]
    assert bootstrap_kwargs["headless"] is True
    assert bootstrap_kwargs["mqtt_autostart"] is False
    assert calls[1] == (
        "load",
        {"v2_conf": "opms.yml", "headless": True},
    )
    assert calls[-2:] == ["wait", "shutdown"]


@pytest.mark.asyncio
async def test_runtime_shuts_down_when_waiter_is_cancelled():
    calls = []

    class _TradingCore:
        async def shutdown(self):
            calls.append("shutdown")

    class _App:
        trading_core = _TradingCore()

    async def bootstrap(*args, **kwargs):
        return _App()

    async def load(*args, **kwargs):
        return True

    async def gateway(*args, **kwargs):
        pass

    async def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_isolated_hummingbot(
            client_config=object(), secrets_manager=object(), script_config="opms.yml",
            bootstrap=bootstrap, load_and_start=load, wait_for_gateway=gateway,
            wait_for_shutdown=cancelled,
        )
    assert calls == ["shutdown"]
