"""Keep one Discord embed in sync with a Palworld dedicated server."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands, tasks


LOG = logging.getLogger("palworld_status")
DEFAULT_MARKER = "palworld-status-bot:v1"
REQUEST_TIMEOUT_SECONDS = 15


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required.")
    return value


def positive_int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a whole number.") from error
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}.")
    return value


def api_base_url(raw_url: str) -> str:
    """Accept either the API root or just http(s)://host:27262."""
    url = raw_url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("PALWORLD_API_URL must start with http:// or https://")
    return url if url.endswith("/v1/api") else f"{url}/v1/api"


@dataclass(frozen=True)
class Settings:
    token: str
    channel_id: int
    api_url: str
    api_password: str | None
    update_interval: int
    status_message_id: int | None
    marker: str
    health_port: int

    @classmethod
    def from_environment(cls) -> "Settings":
        message_id_raw = os.getenv("STATUS_MESSAGE_ID", "").strip()
        try:
            message_id = int(message_id_raw) if message_id_raw else None
        except ValueError as error:
            raise RuntimeError("STATUS_MESSAGE_ID must be a Discord message ID.") from error

        return cls(
            token=required_env("DISCORD_TOKEN"),
            channel_id=positive_int_env("DISCORD_CHANNEL_ID", 0),
            api_url=api_base_url(required_env("PALWORLD_API_URL")),
            api_password=os.getenv("PALWORLD_API_PASSWORD", "").strip() or None,
            update_interval=positive_int_env("UPDATE_INTERVAL", 120, minimum=30),
            status_message_id=message_id,
            marker=os.getenv("STATUS_EMBED_MARKER", DEFAULT_MARKER).strip() or DEFAULT_MARKER,
            health_port=positive_int_env("PORT", 10000),
        )


CONFIG = Settings.from_environment()


class PalworldAPIError(RuntimeError):
    """A safe, user-facing description of a Palworld API request failure."""


class StatusBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.http_session: aiohttp.ClientSession | None = None
        self.status_message: discord.Message | None = None
        self.health_runner: web.AppRunner | None = None
        self.status_loop.change_interval(seconds=settings.update_interval)

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        self.http_session = aiohttp.ClientSession(timeout=timeout)
        await self.start_health_server()
        self.status_loop.start()

    async def close(self) -> None:
        if self.status_loop.is_running():
            self.status_loop.cancel()
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        if self.health_runner:
            await self.health_runner.cleanup()
        await super().close()

    async def start_health_server(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self.health)
        self.health_runner = web.AppRunner(app, access_log=None)
        await self.health_runner.setup()
        site = web.TCPSite(self.health_runner, host="0.0.0.0", port=self.settings.health_port)
        await site.start()
        LOG.info("Health endpoint listening on port %s", self.settings.health_port)

    async def health(self, _request: web.Request) -> web.Response:
        # This intentionally does not call Discord or Palworld: it is a liveness check.
        return web.json_response({"status": "ok"})

    async def palworld_get(self, endpoint: str) -> dict[str, Any]:
        if self.http_session is None:
            raise PalworldAPIError("HTTP session is not ready")

        auth = aiohttp.BasicAuth("admin", self.settings.api_password) if self.settings.api_password else None
        url = f"{self.settings.api_url}/{endpoint.lstrip('/')}"
        try:
            async with self.http_session.get(url, auth=auth) as response:
                if response.status == 401:
                    raise PalworldAPIError("Unauthorized (check PALWORLD_API_PASSWORD; username is admin)")
                if response.status >= 400:
                    body = (await response.text())[:200]
                    raise PalworldAPIError(f"Palworld API returned HTTP {response.status}: {body}")
                data = await response.json(content_type=None)
        except asyncio.TimeoutError as error:
            raise PalworldAPIError("Palworld API request timed out") from error
        except aiohttp.ClientError as error:
            raise PalworldAPIError(f"Could not reach Palworld API: {error}") from error

        if not isinstance(data, dict):
            raise PalworldAPIError("Palworld API returned unexpected JSON")
        return data

    async def fetch_status(self) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        info, players, metrics = await asyncio.gather(
            self.palworld_get("info"), self.palworld_get("players"),
            self.palworld_get("metrics"),
        )
        player_list = players.get("players", [])
        if not isinstance(player_list, list):
            raise PalworldAPIError("Palworld players response did not contain a players list")
        return info, [item for item in player_list if isinstance(item, dict)], metrics

    @staticmethod
    def format_uptime(seconds: Any) -> str:
        try:
            total = max(0, int(seconds))
        except (TypeError, ValueError):
            return "Onbekend"
        days, remainder = divmod(total, 86_400)
        hours, remainder = divmod(remainder, 3_600)
        minutes, _ = divmod(remainder, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}u")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    def make_embed(
        self,
        info: dict[str, Any] | None = None,
        players: list[dict[str, Any]] | None = None,
        metrics: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> discord.Embed:
        now = datetime.now(timezone.utc)
        if error:
            embed = discord.Embed(title="Palworld serverstatus", colour=discord.Colour.red(), timestamp=now)
            embed.description = "De serverstatus kon niet worden opgehaald."
            embed.add_field(name="Fout", value=discord.utils.escape_markdown(error)[:1024], inline=False)
        else:
            assert info is not None and players is not None and metrics is not None
            server_name = str(info.get("servername") or "Palworld-server")
            embed = discord.Embed(title=server_name, colour=discord.Colour.green(), timestamp=now)
            description = str(info.get("description") or "Geen beschrijving.")
            embed.description = description[:4096]
            embed.add_field(name="Status", value="🟢 Online", inline=True)
            current_players = metrics.get("currentplayernum", len(players))
            maximum_players = metrics.get("maxplayernum", "?")
            embed.add_field(name="Spelers online", value=f"{current_players} / {maximum_players}", inline=True)
            embed.add_field(name="Versie", value=str(info.get("version") or "Onbekend")[:1024], inline=True)

            if players:
                names = [str(player.get("name") or player.get("accountName") or "Onbekende speler") for player in players]
                shown = ", ".join(names[:20])
                if len(names) > 20:
                    shown += f" (+{len(names) - 20} meer)"
                embed.add_field(name="Online spelers", value=discord.utils.escape_markdown(shown)[:1024], inline=False)

            world_guid = str(info.get("worldguid") or "Onbekend")
            embed.add_field(name="World GUID", value=f"`{world_guid[:100]}`", inline=False)
            embed.add_field(name="Server FPS", value=str(metrics.get("serverfps", "Onbekend")), inline=True)
            frame_time = metrics.get("serverframetime")
            frame_text = f"{frame_time} ms" if frame_time is not None else "Onbekend"
            embed.add_field(name="Frametime", value=frame_text[:1024], inline=True)
            embed.add_field(name="Uptime", value=self.format_uptime(metrics.get("uptime")), inline=True)
            embed.add_field(name="Werelddag", value=str(metrics.get("days", "Onbekend")), inline=True)
            embed.add_field(name="Basiskampen", value=str(metrics.get("basecampnum", "Onbekend")), inline=True)

        embed.set_footer(text=f"{self.settings.marker} • Laatst bijgewerkt")
        return embed

    async def get_channel(self) -> discord.TextChannel | discord.Thread:
        channel = self.get_channel_cached()
        if channel is None:
            channel = await self.fetch_channel(self.settings.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise RuntimeError("DISCORD_CHANNEL_ID must refer to a text channel or thread.")
        return channel

    def get_channel_cached(self) -> discord.abc.GuildChannel | discord.Thread | None:
        return super().get_channel(self.settings.channel_id)

    async def find_or_create_status_message(self) -> discord.Message:
        if self.status_message is not None:
            return self.status_message
        channel = await self.get_channel()

        if self.settings.status_message_id:
            try:
                self.status_message = await channel.fetch_message(self.settings.status_message_id)
                return self.status_message
            except discord.NotFound:
                LOG.warning("STATUS_MESSAGE_ID %s was not found; searching by marker.", self.settings.status_message_id)
            except discord.Forbidden as error:
                raise RuntimeError("Bot cannot fetch STATUS_MESSAGE_ID; check channel permissions.") from error

        try:
            async for message in channel.history(limit=None, oldest_first=False):
                if message.author.id != self.user.id:
                    continue
                if any(embed.footer and embed.footer.text and self.settings.marker in embed.footer.text for embed in message.embeds):
                    self.status_message = message
                    LOG.info("Recovered status message %s using the footer marker.", message.id)
                    return message
        except discord.Forbidden as error:
            raise RuntimeError("Bot needs Read Message History to recover the status embed after restarts.") from error

        self.status_message = await channel.send(embed=self.make_embed(error="Wachten op eerste Palworld-update…"))
        LOG.info("Created status message %s. Set STATUS_MESSAGE_ID=%s to pin it explicitly.", self.status_message.id, self.status_message.id)
        return self.status_message

    @tasks.loop(seconds=60, reconnect=True)
    async def status_loop(self) -> None:
        try:
            message = await self.find_or_create_status_message()
            info, players, metrics = await self.fetch_status()
            await message.edit(embed=self.make_embed(info=info, players=players, metrics=metrics))
            LOG.info("Updated message %s: %s player(s) online.", message.id, len(players))
        except (PalworldAPIError, discord.HTTPException, RuntimeError) as error:
            LOG.warning("Status refresh failed: %s", error)
            try:
                message = await self.find_or_create_status_message()
                await message.edit(embed=self.make_embed(error=str(error)))
            except (discord.HTTPException, RuntimeError) as edit_error:
                LOG.error("Could not show failed status in Discord: %s", edit_error)

    @status_loop.before_loop
    async def before_status_loop(self) -> None:
        await self.wait_until_ready()

    @status_loop.error
    async def status_loop_error(self, error: Exception) -> None:
        LOG.exception("Unexpected status-loop failure", exc_info=error)

    async def on_ready(self) -> None:
        LOG.info("Connected to Discord as %s (%s)", self.user, self.user.id if self.user else "unknown")


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    StatusBot(CONFIG).run(CONFIG.token, log_handler=None, reconnect=True)


if __name__ == "__main__":
    main()
