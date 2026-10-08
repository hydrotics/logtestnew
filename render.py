"""Tiny keep-alive web server for Render + UptimeRobot.

Render web services must listen on the port in the ``PORT`` environment
variable and need inbound HTTP traffic (free instances sleep after ~15 minutes
without any). This module runs an aiohttp server *inside the bot's own event
loop* (no extra thread, no Flask) and answers UptimeRobot's pings.

Endpoints (GET and HEAD both work, UptimeRobot's free plan uses HEAD):
    /          -> "Bot is alive."
    /health    -> JSON status, HTTP 503 only if the bot has been closed
    /healthz   -> alias of /health
    /ping      -> alias of /health
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Iterator, Optional

import aiohttp
from aiohttp import web

logger = logging.getLogger("discord-log-bot.render")

DEFAULT_PORT = 10000          # Render's default PORT
PORT_SCAN_RANGE = 20          # how many ports above the preferred one to try
SELF_PING_SECONDS = 10 * 60   # optional backup ping (only on Render)


def _candidate_ports() -> Iterator[int]:
    """PORT from the environment first, then nearby ports, then any free one."""
    try:
        preferred = int(os.getenv("PORT", DEFAULT_PORT))
    except ValueError:
        preferred = DEFAULT_PORT

    yield preferred
    for offset in range(1, PORT_SCAN_RANGE + 1):
        port = preferred + offset
        if port <= 65535:
            yield port
    yield 0  # let the OS pick


class KeepAliveServer:
    def __init__(
        self,
        health: Optional[Callable[[], dict[str, Any]]] = None,
    ) -> None:
        self._health = health
        self._runner: Optional[web.AppRunner] = None
        self._ping_task: Optional[asyncio.Task] = None
        self.port: Optional[int] = None

    async def start(self) -> int:
        if self._runner is not None and self.port is not None:
            return self.port

        app = web.Application()
        app.router.add_get("/", self._index)
        for path in ("/health", "/healthz", "/ping"):
            app.router.add_get(path, self._health_handler)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()

        last_error: Optional[Exception] = None
        for port in _candidate_ports():
            site = web.TCPSite(runner, host="0.0.0.0", port=port)
            try:
                await site.start()
            except OSError as exc:
                last_error = exc
                logger.warning("Port %s is unavailable (%s). Trying the next one.", port, exc)
                try:
                    await site.stop()
                except Exception:
                    pass
                continue

            self._runner = runner
            self.port = runner.addresses[0][1]
            break
        else:
            await runner.cleanup()
            raise RuntimeError("Could not bind any port for the keep-alive server.") from last_error

        logger.info("Keep-alive server listening on 0.0.0.0:%d", self.port)

        external_url = os.getenv("RENDER_EXTERNAL_URL")
        if external_url:
            self._ping_task = asyncio.create_task(
                self._self_ping(external_url.rstrip("/")),
                name="keepalive-self-ping",
            )

        return self.port

    async def stop(self) -> None:
        if self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                logger.debug("Keep-alive cleanup failed", exc_info=True)
            self._runner = None

    # ------------------------------------------------------------------

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(text="Bot is alive.\n")

    async def _health_handler(self, request: web.Request) -> web.Response:
        data: dict[str, Any] = {}
        try:
            if self._health is not None:
                data = self._health()
        except Exception:
            logger.debug("Health snapshot failed", exc_info=True)

        closed = bool(data.get("closed"))
        return web.json_response(
            {"status": "closed" if closed else "ok", **data},
            status=503 if closed else 200,
        )

    async def _self_ping(self, base_url: str) -> None:
        """Backup traffic in case UptimeRobot is ever paused."""
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                await asyncio.sleep(SELF_PING_SECONDS)
                try:
                    async with session.get(f"{base_url}/health") as resp:
                        logger.debug("Self-ping -> %s", resp.status)
                except Exception as exc:
                    logger.debug("Self-ping failed: %s", exc)


if __name__ == "__main__":
    # Quick local check:  python render.py   then open http://localhost:10000/
    async def _demo() -> None:
        server = KeepAliveServer(lambda: {"closed": False, "demo": True})
        port = await server.start()
        print(f"Listening on port {port}")
        await asyncio.Event().wait()

    asyncio.run(_demo())
