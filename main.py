from __future__ import annotations

import asyncio
import logging
import math
import os
import sqlite3
import tempfile
from typing import Any

import discord
from discord import app_commands
from dotenv import load_dotenv

import create_log as create_log_module
from create_log import Database, ForumCreatePacer, LogManager
from render import KeepAliveServer

_original_build_status_embed = create_log_module.build_status_embed


def _build_status_embed_with_global_colour(job: Any) -> discord.Embed:
    embed = _original_build_status_embed(job)
    manager = globals().get("log_manager")
    configured_colour = getattr(manager, "embed_colour", None) if manager else None
    if configured_colour is not None:
        embed.colour = configured_colour
    return embed


create_log_module.build_status_embed = _build_status_embed_with_global_colour

from Forum_Commands.create_log_command import build_create_log_commands
from Forum_Commands.creation_prompt_command import setup_creation_prompt
from Forum_Commands.member_scan_command import build_member_scan_command
from Forum_Commands.status_command import build_status_command
from settings import DEFAULT_SCAN_REASON, build_settings_command

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

_default_data_dir = (
    "/var/data"
    if os.path.isdir("/var/data")
    else os.path.dirname(os.path.abspath(__file__))
)
DATABASE_PATH = os.path.join(_default_data_dir, "bot_data.sqlite3")

FORUM_CHANNEL_ID = 0
DISCORD_GUILD_ID = 0
CREATE_CONCURRENCY = 1

try:
    owner_id_value = os.getenv("BOT_OWNER_ID", "").strip()
    if not owner_id_value:
        raise RuntimeError("BOT_OWNER_ID is missing from .env")
    BOT_OWNER_ID = int(owner_id_value)
    if BOT_OWNER_ID <= 0:
        raise ValueError("BOT_OWNER_ID must be a positive Discord user ID")
except ValueError as exc:
    raise RuntimeError("BOT_OWNER_ID must be a valid Discord user ID.") from exc

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("discord-log-bot")

intents = discord.Intents.default()
intents.members = True


FORUM_MANAGER_COMMANDS = {"create_log", "status"}


class LogCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        manager = globals().get("log_manager")
        command_name = getattr(interaction.command, "name", "") or str(
            (interaction.data or {}).get("name", "")
        )

        if command_name == "settings" and interaction.user.id == BOT_OWNER_ID:
            return True

        configured_guild_id = getattr(manager, "guild_id", 0) if manager else 0
        if command_name != "settings" and configured_guild_id:
            if interaction.guild_id != configured_guild_id:
                await self._deny(
                    interaction,
                    "This bot is configured for a different server.",
                )
                return False

        member_roles = (
            {role.id for role in interaction.user.roles}
            if isinstance(interaction.user, discord.Member)
            else set()
        )
        forum_manager_id = int(getattr(manager, "forum_manager_role_id", 0) or 0)

        if command_name in FORUM_MANAGER_COMMANDS:
            if interaction.user.id == BOT_OWNER_ID:
                return True
            if forum_manager_id and forum_manager_id in member_roles:
                return True
            await self._deny(
                interaction,
                "You need the forum manager role to use this command.",
            )
            return False

        if command_name == "settings":
            booster_staff_id = int(getattr(booster, "STAFF_TEAM_ROLE_ID", 0) or 0)
            if (booster_staff_id and booster_staff_id in member_roles) or (
                forum_manager_id and forum_manager_id in member_roles
            ):
                return True

        if booster.is_booster_interaction(interaction):
            return True

        if interaction.user.id == BOT_OWNER_ID:
            return True

        staff_role_id = getattr(manager, "staff_role_id", 0) if manager else 0
        if not staff_role_id:
            await self._deny(
                interaction,
                "The bot has no staff role configured yet. Only the bot owner can use commands until one is set with `/settings`.",
            )
            return False

        member = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else None
        )
        if member is None or not any(
            role.id == staff_role_id for role in member.roles
        ):
            await self._deny(
                interaction,
                "You need the configured staff role to use this bot.",
            )
            return False

        return True

    @staticmethod
    async def _deny(interaction: discord.Interaction, message: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            logger.debug("Could not send command permission denial", exc_info=True)


class LogBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(
            intents=intents,
            http_trace=forum_pacer.trace_config,
            max_ratelimit_timeout=None,
        )
        self.tree = LogCommandTree(self)
        self.commands_ready = asyncio.Event()
        self.command_setup_error: Exception | None = None
        self.keepalive = KeepAliveServer(self._health_snapshot)

    def _health_snapshot(self) -> dict[str, Any]:
        latency = self.latency
        return {
            "closed": self.is_closed(),
            "ready": self.is_ready(),
            "commands_ready": self.commands_ready.is_set(),
            "latency_ms": None if math.isnan(latency) else round(latency * 1000),
            "job_running": log_manager.job.running,
        }

    async def setup_hook(self) -> None:
        try:
            await self.keepalive.start()
        except Exception:
            logger.exception("Keep-alive server failed to start")

        try:
            await booster.setup_hook(self)
        except Exception:
            logger.exception("Booster reveal setup failed")

        booster_commands = booster.collect_commands(self.tree)

        guild_id = log_manager.guild_id or DISCORD_GUILD_ID
        if not guild_id and log_manager.forum_channel_id:
            try:
                configured_forum = await log_manager.get_forum_channel()
                guild_id = configured_forum.guild.id
                log_manager.guild_id = guild_id
            except Exception:
                logger.warning(
                    "Could not infer the command guild from the configured forum; "
                    "using global command registration so /settings remains available.",
                    exc_info=True,
                )

        creation_prompt_command = setup_creation_prompt(
            self,
            manager=log_manager,
            owner_id=BOT_OWNER_ID,
        )
        settings_command = build_settings_command(
            owner_id=BOT_OWNER_ID,
            manager=log_manager,
            apply_main_settings=apply_main_settings,
            apply_forum_settings=apply_forum_settings,
            apply_creation_settings=apply_creation_settings,
            apply_booster_settings=apply_booster_settings,
            get_booster_settings=get_booster_settings,
            apply_scan_settings=apply_scan_settings,
            get_scan_settings=get_scan_settings,
        )
        forum_commands = (
            build_create_log_commands(manager=log_manager, owner_id=BOT_OWNER_ID),
            build_status_command(manager=log_manager, owner_id=BOT_OWNER_ID),
            build_member_scan_command(
                manager=log_manager,
                owner_id=BOT_OWNER_ID,
                get_scan_settings=get_scan_settings,
            ),
            creation_prompt_command,
            settings_command,
        )

        forum_names = {command.name for command in forum_commands}
        accepted_booster_commands = []
        for command in booster_commands:
            if command.name in forum_names:
                logger.error(
                    "Booster command %r conflicts with an existing command name and was not registered.",
                    command.name,
                )
                continue
            accepted_booster_commands.append(command)

        current_commands = (*forum_commands, *accepted_booster_commands)
        expected_names = {command.name for command in current_commands}

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                previous_guild_id = int(
                    database.get_setting("command_sync_guild_id") or "0"
                )

                guild_scope_ids: set[int] = set()
                try:
                    async for installed_guild in self.fetch_guilds(limit=None):
                        guild_scope_ids.add(int(installed_guild.id))
                except (discord.HTTPException, discord.Forbidden):
                    logger.warning(
                        "Could not enumerate all installed guilds; will still clean "
                        "the configured and previously recorded guild scopes.",
                        exc_info=True,
                    )
                if previous_guild_id:
                    guild_scope_ids.add(previous_guild_id)
                if guild_id:
                    guild_scope_ids.add(int(guild_id))

                self.tree.clear_commands(guild=None)
                await self.tree.sync()
                remote_global = await self.tree.fetch_commands()
                if remote_global:
                    raise RuntimeError(
                        "Global command cleanup verification failed; remote global "
                        f"commands remain: {sorted(command.name for command in remote_global)}"
                    )

                for stale_guild_id in sorted(guild_scope_ids - ({int(guild_id)} if guild_id else set())):
                    stale_guild = discord.Object(id=stale_guild_id)
                    try:
                        self.tree.clear_commands(guild=stale_guild)
                        await self.tree.sync(guild=stale_guild)
                        remaining = await self.tree.fetch_commands(guild=stale_guild)
                    except (discord.NotFound, discord.Forbidden):
                        logger.warning(
                            "Could not access old command scope for guild %s; "
                            "Discord may already have removed it with the app installation.",
                            stale_guild_id,
                            exc_info=True,
                        )
                        continue
                    if remaining:
                        raise RuntimeError(
                            f"Guild command cleanup verification failed for {stale_guild_id}: "
                            f"{sorted(command.name for command in remaining)}"
                        )
                    logger.info(
                        "Removed stale guild command registrations from guild %s.",
                        stale_guild_id,
                    )

                if guild_id:
                    guild = discord.Object(id=int(guild_id))
                    self.tree.clear_commands(guild=guild)
                    for command in current_commands:
                        self.tree.add_command(command, guild=guild, override=True)
                    synced = await self.tree.sync(guild=guild)
                else:
                    for command in current_commands:
                        self.tree.add_command(command, override=True)
                    synced = await self.tree.sync()

                synced_names = {command.name for command in synced}
                if synced_names != expected_names:
                    raise RuntimeError(
                        "Command sync verification failed. "
                        f"Expected {sorted(expected_names)}, got {sorted(synced_names)}."
                    )

                status_command = next(
                    command for command in synced if command.name == "status"
                )
                log_manager.status_mention = status_command.mention
                database.set_setting(
                    "command_sync_guild_id", str(int(guild_id or 0))
                )

                logger.info(
                    "Commands ready (%s) in guild %s: %s; global registrations cleared=%s",
                    "guild-scoped" if guild_id else "global first-run fallback",
                    guild_id or "not selected yet",
                    ", ".join(sorted(synced_names)),
                    bool(guild_id),
                )
                self.commands_ready.set()
                return

            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Command setup attempt %d/3 failed: %s", attempt, exc
                )
                if attempt < 3:
                    await asyncio.sleep(float(2**attempt))

        self.command_setup_error = last_error or RuntimeError(
            "Unknown command setup failure."
        )
        logger.error(
            "Command setup failed after 3 attempts: %s", self.command_setup_error
        )

    async def close(self) -> None:
        booster.shutdown()
        await self.keepalive.stop()
        await super().close()
        database.close()


def _open_database(configured_path: str) -> tuple[Database, str]:
    fallback_path = os.path.join(tempfile.gettempdir(), "bot_data.sqlite3")

    def should_fallback(exc: BaseException) -> bool:
        message = str(exc).lower()
        return isinstance(exc, (PermissionError, OSError)) or any(
            marker in message
            for marker in (
                "unable to open database file",
                "readonly database",
                "read-only database",
                "permission denied",
            )
        )

    try:
        return Database(configured_path), configured_path
    except (PermissionError, OSError, sqlite3.OperationalError) as exc:
        if not should_fallback(exc):
            raise
        if os.path.abspath(configured_path) == os.path.abspath(fallback_path):
            raise
        logger.warning(
            "Cannot use DATABASE_PATH=%s (%s). Falling back to temporary SQLite "
            "path %s. Configure DATABASE_PATH to a writable persistent disk to "
            "keep data across restarts.",
            configured_path,
            exc,
            fallback_path,
        )
        return Database(fallback_path), fallback_path


forum_pacer = ForumCreatePacer(FORUM_CHANNEL_ID)
bot = LogBot()
tree = bot.tree

database, DATABASE_PATH = _open_database(DATABASE_PATH)
logger.info("Using SQLite database at %s", os.path.abspath(DATABASE_PATH))

try:
    saved_forum_id = int(database.get_setting("forum_channel_id") or "0")
    saved_staff_role_id = int(database.get_setting("staff_role_id") or "0")
    saved_forum_manager_role_id = int(
        database.get_setting("forum_manager_role_id") or "0"
    )
    saved_target_role_id = int(database.get_setting("target_role_id") or "0")
    saved_guild_id = int(database.get_setting("guild_id") or "0")
    saved_creation_prompt_channel_id = int(
        database.get_setting("creation_prompt_channel_id") or "0"
    )
    saved_creation_approve_channel_id = int(
        database.get_setting("creation_approve_channel_id") or "0"
    )
    saved_booster_role_id = int(database.get_setting("booster_role_id") or "0")
    saved_staff_team_role_id = int(database.get_setting("staff_team_role_id") or "0")
    saved_reveal_archive_channel_id = int(
        database.get_setting("reveal_archive_channel_id") or "0"
    )
    saved_embed_colour_raw = database.get_setting("embed_colour") or "#242429"
    if saved_embed_colour_raw.startswith("#"):
        saved_embed_colour_raw = saved_embed_colour_raw[1:]
    if len(saved_embed_colour_raw) != 6 or any(
        character not in "0123456789abcdefABCDEF"
        for character in saved_embed_colour_raw
    ):
        raise ValueError("embed_colour must be a six-digit hexadecimal colour")
    saved_embed_colour_value = int(saved_embed_colour_raw, 16)
except ValueError as exc:
    raise RuntimeError("Saved bot settings in SQLite are invalid.") from exc

os.environ["BOOSTER_ROLE_ID"] = str(saved_booster_role_id)
os.environ["STAFF_TEAM_ROLE_ID"] = str(saved_staff_team_role_id)
os.environ["REVEAL_ARCHIVE_CHANNEL_ID"] = str(saved_reveal_archive_channel_id)

from booster_commands import booster_reveal as booster

FORUM_CHANNEL_ID = saved_forum_id or FORUM_CHANNEL_ID
forum_pacer.forum_channel_id = FORUM_CHANNEL_ID
log_manager = LogManager(
    bot=bot,
    database=database,
    role_id=saved_target_role_id,
    forum_channel_id=FORUM_CHANNEL_ID,
    forum_pacer=forum_pacer,
    concurrency=CREATE_CONCURRENCY,
)
log_manager.staff_role_id = saved_staff_role_id
log_manager.forum_manager_role_id = saved_forum_manager_role_id
log_manager.embed_colour = discord.Colour(saved_embed_colour_value)
log_manager.guild_id = saved_guild_id or DISCORD_GUILD_ID
log_manager.creation_prompt_channel_id = saved_creation_prompt_channel_id
log_manager.creation_approve_channel_id = saved_creation_approve_channel_id

booster.install(
    bot,
    booster_role_id=saved_booster_role_id,
    staff_team_role_id=saved_staff_team_role_id,
    reveal_archive_channel_id=saved_reveal_archive_channel_id,
    owner_id=BOT_OWNER_ID,
)


def apply_main_settings(staff_role_id: int, embed_colour_value: int) -> None:
    staff_role_id = int(staff_role_id)
    embed_colour_value = int(embed_colour_value)
    if not 0 <= embed_colour_value <= 0xFFFFFF:
        raise ValueError("Embed colour must be between 0x000000 and 0xFFFFFF")

    database.set_setting("staff_role_id", str(staff_role_id))
    database.set_setting("embed_colour", f"#{embed_colour_value:06x}")
    log_manager.staff_role_id = staff_role_id
    log_manager.embed_colour = discord.Colour(embed_colour_value)


def apply_forum_settings(
    forum_channel_id: int,
    target_role_id: int,
    auto_create_enabled: bool,
    guild_id: int,
    forum_manager_role_id: int = 0,
) -> None:
    database.set_setting("forum_manager_role_id", str(int(forum_manager_role_id)))
    log_manager.forum_manager_role_id = int(forum_manager_role_id)
    database.set_setting("forum_channel_id", str(int(forum_channel_id)))
    database.set_setting("target_role_id", str(int(target_role_id)))
    database.set_setting("guild_id", str(int(guild_id)))

    log_manager.forum_channel_id = int(forum_channel_id)
    log_manager.role_id = int(target_role_id)
    log_manager.guild_id = int(guild_id)
    forum_pacer.forum_channel_id = int(forum_channel_id)
    log_manager.set_auto_log(bool(auto_create_enabled))

    if log_manager.auto_log_enabled and log_manager.role_id:
        log_manager.schedule_auto_reconcile(force=True)


def apply_creation_settings(prompt_channel_id: int, approve_channel_id: int) -> None:
    database.set_setting("creation_prompt_channel_id", str(int(prompt_channel_id)))
    database.set_setting("creation_approve_channel_id", str(int(approve_channel_id)))
    log_manager.creation_prompt_channel_id = int(prompt_channel_id)
    log_manager.creation_approve_channel_id = int(approve_channel_id)


def get_booster_settings() -> tuple[int, int, int]:
    return (
        int(booster.BOOSTER_ROLE_ID),
        int(booster.STAFF_TEAM_ROLE_ID),
        int(booster.REVEAL_ARCHIVE_CHANNEL_ID),
    )


def apply_booster_settings(
    booster_role_id: int,
    staff_team_role_id: int,
    reveal_archive_channel_id: int,
) -> None:
    database.set_setting("booster_role_id", str(int(booster_role_id)))
    database.set_setting("staff_team_role_id", str(int(staff_team_role_id)))
    database.set_setting(
        "reveal_archive_channel_id", str(int(reveal_archive_channel_id))
    )
    booster.update_config(
        booster_role_id=int(booster_role_id),
        staff_team_role_id=int(staff_team_role_id),
        reveal_archive_channel_id=int(reveal_archive_channel_id),
    )


def get_scan_settings() -> tuple[int, str]:
    try:
        source_id = int(database.get_setting("scan_guild_id") or "0")
    except ValueError:
        source_id = 0
    reason = database.get_setting("scan_ban_reason") or DEFAULT_SCAN_REASON
    return source_id, reason


def apply_scan_settings(source_guild_id: int, ban_reason: str) -> None:
    database.set_setting("scan_guild_id", str(int(source_guild_id)))
    database.set_setting("scan_ban_reason", ban_reason or DEFAULT_SCAN_REASON)


@tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if booster.is_booster_interaction(interaction):
        message = booster.command_error_message(error)
    elif isinstance(error, app_commands.CheckFailure):
        message = "You do not have permission to use this command."
    else:
        logger.error("Unhandled command error", exc_info=error)
        message = "Something went wrong while running that command."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logger.debug("Could not report command error", exc_info=True)


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if (
        not log_manager.forum_channel_id
        or not log_manager.auto_log_enabled
        or member.bot
    ):
        return

    try:
        await log_manager.ensure_member_has_log(member)
    except Exception:
        logger.exception("Auto-log failed for joined member %s", member.id)


@bot.event
async def on_member_update(
    before: discord.Member,
    after: discord.Member,
) -> None:
    if (
        not log_manager.forum_channel_id
        or not log_manager.auto_log_enabled
        or after.bot
    ):
        return

    role_id = log_manager.role_id
    if not role_id:
        return
    role = after.guild.get_role(role_id)
    if role is None:
        return

    if role not in before.roles and role in after.roles:
        try:
            await log_manager.ensure_member_has_log(after)
        except Exception:
            logger.exception("Auto-log failed for role assignment to %s", after.id)


@bot.event
async def on_raw_thread_delete(payload: discord.RawThreadDeleteEvent) -> None:
    if payload.parent_id == log_manager.forum_channel_id:
        log_manager.handle_thread_delete(payload.thread_id)


@bot.event
async def on_ready() -> None:
    logger.info(
        "Logged in as %s (%s)",
        bot.user,
        bot.user.id if bot.user else "unknown",
    )

    if bot.command_setup_error is not None:
        logger.error("Commands are not ready: %s", bot.command_setup_error)
    elif bot.commands_ready.is_set():
        logger.info("Command registration is stable; no command resync on reconnect.")
        if log_manager.forum_channel_id:
            log_manager.schedule_auto_reconcile()

    await booster.on_ready_hook()


@bot.event
async def on_disconnect() -> None:
    booster.on_disconnect_hook()


@bot.event
async def on_resumed() -> None:
    booster.on_resumed_hook()


@bot.event
async def on_error(event: str, *args, **kwargs) -> None:
    logger.exception("Unhandled Discord event error in %s", event)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
