#!/usr/bin/env python
"""Start a configured Hummingbot strategy headlessly with MQTT disabled.

The stock quickstart couples headless mode to an always-retrying MQTT bridge.
Our bounded runners own process lifecycle themselves, so this launcher starts
the strategy directly and waits only for SIGINT/SIGTERM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

import path_util  # noqa: F401  (initialises Hummingbot's runtime paths)

from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger
from hummingbot.client.config.config_helpers import load_client_config_map_from_file
from hummingbot.client.runner import (
    bootstrap_application,
    load_and_start_strategy,
    wait_for_gateway_ready,
)

from opms.isolated_runtime import run_isolated_hummingbot


async def _wait_for_shutdown_signal() -> None:
    event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, event.set)
            installed.append(sig)
        except NotImplementedError:  # pragma: no cover - Unix deployment path
            pass
    try:
        await event.wait()
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


async def _main() -> None:
    script_config = os.environ.get("SCRIPT_CONFIG")
    password = os.environ.get("CONFIG_PASSWORD")
    if not script_config:
        raise RuntimeError("SCRIPT_CONFIG is required")
    if not password:
        raise RuntimeError("CONFIG_PASSWORD is required")

    client_config = load_client_config_map_from_file()
    client_config.mqtt_bridge.mqtt_autostart = False
    await run_isolated_hummingbot(
        client_config=client_config,
        secrets_manager=ETHKeyFileSecretManger(password),
        script_config=script_config,
        bootstrap=bootstrap_application,
        load_and_start=load_and_start_strategy,
        wait_for_gateway=wait_for_gateway_ready,
        wait_for_shutdown=_wait_for_shutdown_signal,
    )


if __name__ == "__main__":
    logging.getLogger(__name__).info("Starting isolated Hummingbot runtime (MQTT disabled)")
    asyncio.run(_main())
