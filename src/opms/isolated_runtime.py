"""Run an HB strategy headlessly without coupling trading to MQTT health."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


async def run_isolated_hummingbot(
    *,
    client_config: Any,
    secrets_manager: Any,
    script_config: str,
    bootstrap: Callable[..., Awaitable[Any]],
    load_and_start: Callable[..., Awaitable[bool]],
    wait_for_gateway: Callable[[Any], Awaitable[None]],
    wait_for_shutdown: Callable[[], Awaitable[None]],
) -> Any:
    """Start the strategy directly and keep MQTT out of the runtime path.

    Hummingbot's stock quickstart forces MQTT autostart whenever ``headless``
    is true, then retries a missing broker forever.  A bounded deployment does
    not need remote command transport: it already owns process lifetime and
    cleanup.  This helper retains headless connector/strategy behavior while
    explicitly leaving MQTT disabled.
    """
    mqtt_bridge = getattr(client_config, "mqtt_bridge", None)
    if mqtt_bridge is not None:
        mqtt_bridge.mqtt_autostart = False
    app = await bootstrap(
        client_config,
        secrets_manager,
        headless=True,
        mqtt_autostart=False,
    )
    if app is None:
        raise RuntimeError("Hummingbot login/bootstrap failed")

    try:
        started = await load_and_start(
            app,
            v2_conf=script_config,
            headless=True,
        )
        if not started:
            raise RuntimeError(f"failed to start Hummingbot strategy {script_config}")
        await wait_for_gateway(app)
        await wait_for_shutdown()
        return app
    finally:
        await app.trading_core.shutdown()


__all__ = ["run_isolated_hummingbot"]
