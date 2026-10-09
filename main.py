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

from create_log import (
    Database,
    ForumCreatePacer,
    LogManager,
    build_status_embed,
    monitor_job_message,
)
from render import KeepAliveServer
from test import build_test_command
from settings import build_settings_command

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
# Prefer a database beside the app when DATABASE_PATH is not configured.
# On Render, /var/data is only writable when a persistent disk is mounted there.
DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.sqlite3"),
)

try:
    # Optional initial forum. /settings can set or change it and the selection
    # is persisted in SQLite.
    FORUM_CHANNEL_ID = int(os.getenv("DISCORD_FORUM_CHANNEL_ID", "0"))
    DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
    # Kept for compatibility with existing deployments. The forum-create route
    # is deliberately single-flight in LogManager to prevent request bursts.
    CREATE_CONCURRENCY = int(os.getenv("CREATE_CONCURRENCY", "1"))
    owner_id_value = os.getenv("BOT_OWNER_ID", "").strip()
    if not owner_id_value:
        raise RuntimeError("BOT_OWNER_ID is missing from .env")
    BOT_OWNER_ID = int(owner_id_value)
    if BOT_OWNER_ID <= 0:
        raise ValueError("BOT_OWNER_ID must be a positive Discord user ID")
except ValueError as exc:
    raise RuntimeError(
        "BOT_OWNER_ID, DISCORD_FORUM_CHANNEL_ID, DISCORD_GUILD_ID and "
        "CREATE_CONCURRENCY must be valid values."
    ) from exc

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("discord-log-bot")

intents = discord.Intents.default()
intents.members = True


class LogCommandTree(app_commands.CommandTree):
    """Apply the owner/staff role gate to every slash command in one place."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        manager = globals().get("log_manager")
        command_name = getattr(interaction.command, "name", "") or str(
            (interaction.data or {}).get("name", "")
        )

        # The owner may always open /settings, including during first-run setup.
        if command_name == "settings" and interaction.user.id == BOT_OWNER_ID:
            return True

        # All operational commands are bound to the configured server once it
        # is known. This also keeps globally synced first-run commands inert in
        # other servers.
        configured_guild_id = getattr(manager, "guild_id", 0) if manager else 0
        if command_name != "settings" and configured_guild_id:
            if interaction.guild_id != configured_guild_id:
                await self._deny(
                    interaction,
                    "This bot is configured for a different server.",
                )
                return False

        if interaction.user.id == BOT_OWNER_ID:
            return True

        staff_role_id = getattr(manager, "staff_role_id", 0) if manager else 0
        if not staff_role_id:
            await self._deny(
                interaction,
                "The bot has no staff role configured yet. Only the bot owner can use commands until one is set with `/settings`.",
            )
            return False

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None or not any(role.id == staff_role_id for role in member.roles):
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
    """One client and one CommandTree. Commands are synced only once per run."""

    def __init__(self) -> None:
        super().__init__(
            intents=intents,
            # Connect ForumCreatePacer's aiohttp response listener to
            # discord.py's actual HTTP session. Without this option, the
            # listener defined in create_log.py never sees rate-limit headers,
            # so adaptive pacing never activates and large batches can hit a
            # bucket limit partway through (for example around post 40).
            http_trace=forum_pacer.trace_config,
            # Keep waiting for Discord's advertised reset rather than
            # surfacing RateLimited and abandoning a healthy batch.
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
        """Start keep-alive and register commands in the configured guild."""
        # Bind the port FIRST so Render sees an open port immediately.
        try:
            await self.keepalive.start()
        except Exception:
            logger.exception("Keep-alive server failed to start")

        # A guild ID can be inferred from the configured forum, supplied via
        # DISCORD_GUILD_ID, or loaded from the settings saved by /settings.
        guild_id = log_manager.guild_id or DISCORD_GUILD_ID
        if not guild_id and log_manager.forum_channel_id:
            try:
                configured_forum = await log_manager.get_forum_channel()
                guild_id = configured_forum.guild.id
                log_manager.guild_id = guild_id
            except Exception:
                logger.warning(
                    "Could not infer the command guild from the configured forum; "
                    "falling back to global command registration so /settings remains available.",
                    exc_info=True,
                )

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                current_commands = (
                    create_log,
                    status,
                    auto_log,
                    build_test_command(log_manager),
                    build_settings_command(
                        owner_id=BOT_OWNER_ID,
                        manager=log_manager,
                        apply_settings=apply_bot_settings,
                    ),
                )

                if guild_id:
                    guild = discord.Object(id=guild_id)
                    for command in current_commands:
                        self.tree.add_command(command, guild=guild, override=True)
                    synced = await self.tree.sync(guild=guild)
                else:
                    # First-run fallback: global syncing allows the owner to
                    # open /settings even when no forum/guild ID is configured.
                    for command in current_commands:
                        self.tree.add_command(command, override=True)
                    synced = await self.tree.sync()

                names = sorted(command.name for command in synced)
                expected = ["auto_log", "create_log", "settings", "status", "test"]
                if names != expected:
                    raise RuntimeError(
                        "Command sync verification failed. "
                        f"Expected {expected}, got {names}."
                    )

                status_command = next(c for c in synced if c.name == "status")
                log_manager.status_mention = status_command.mention

                # Remove stale global copies only when we synced guild-scoped
                # commands. In first-run global mode, clearing globals would
                # remove /settings itself.
                if guild_id:
                    self.tree.clear_commands(guild=None)
                    try:
                        await self.tree.sync()
                    except Exception:
                        logger.warning(
                            "Could not remove old global commands; guild commands are still ready.",
                            exc_info=True,
                        )

                logger.info(
                    "Commands ready (%s) in guild %s: %s",
                    "guild-scoped" if guild_id else "global first-run fallback",
                    guild_id or "not selected yet",
                    ", ".join(names),
                )
                self.commands_ready.set()
                return

            except Exception as exc:
                last_error = exc
                logger.warning("Command setup attempt %d/3 failed: %s", attempt, exc)
                if attempt < 3:
                    await asyncio.sleep(float(2**attempt))

        self.command_setup_error = last_error or RuntimeError(
            "Unknown command setup failure."
        )
        logger.error(
            "Command setup failed after 3 attempts: %s", self.command_setup_error
        )

    async def close(self) -> None:
        await self.keepalive.stop()
        await super().close()
        database.close()


forum_pacer = ForumCreatePacer(FORUM_CHANNEL_ID)
bot = LogBot()
tree = bot.tree

def _open_database(configured_path: str) -> tuple[Database, str]:
    """Open SQLite at the configured path, with a writable fallback.

    A Render service without a disk mounted at /var/data cannot create files
    there. Fall back to the OS temporary directory so the bot can still start.
    The fallback is ephemeral; configure DATABASE_PATH to a mounted disk for
    persistence across restarts/deploys.
    """
    fallback_path = os.path.join(tempfile.gettempdir(), "bot_data.sqlite3")

    def should_fallback(exc: BaseException) -> bool:
        message = str(exc).lower()
        return isinstance(exc, (PermissionError, OSError)) or any(
            marker in message
            for marker in ("unable to open database file", "readonly database", "read-only database", "permission denied")
        )

    try:
        return Database(configured_path), configured_path
    except (PermissionError, OSError, sqlite3.OperationalError) as exc:
        if not should_fallback(exc):
            raise
        if os.path.abspath(configured_path) == os.path.abspath(fallback_path):
            raise
        logger.warning(
            "Cannot use DATABASE_PATH=%s (%s). Falling back to temporary SQLite path %s. "
            "Set DATABASE_PATH to a writable mounted disk to keep data across restarts.",
            configured_path,
            exc,
            fallback_path,
        )
        return Database(fallback_path), fallback_path


database, DATABASE_PATH = _open_database(DATABASE_PATH)
logger.info("Using SQLite database at %s", os.path.abspath(DATABASE_PATH))
# Load settings selected in /settings. Environment forum/guild IDs are only
# initial fallbacks; SQLite values survive restarts and take precedence.
try:
    saved_forum_id = int(database.get_setting("forum_channel_id") or "0")
    saved_staff_role_id = int(database.get_setting("staff_role_id") or "0")
    saved_target_role_id = int(database.get_setting("target_role_id") or "0")
    saved_guild_id = int(database.get_setting("guild_id") or "0")
except ValueError as exc:
    raise RuntimeError("Saved forum/staff/member-role/guild settings in SQLite are invalid.") from exc

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
log_manager.guild_id = saved_guild_id or DISCORD_GUILD_ID


def apply_bot_settings(
    forum_channel_id: int,
    staff_role_id: int,
    target_role_id: int,
    auto_create_enabled: bool,
    guild_id: int,
) -> None:
    """Persist owner-selected settings and apply them without a restart."""
    database.set_setting("forum_channel_id", str(int(forum_channel_id)))
    database.set_setting("staff_role_id", str(int(staff_role_id)))
    database.set_setting("target_role_id", str(int(target_role_id)))
    database.set_setting("guild_id", str(int(guild_id)))

    log_manager.forum_channel_id = int(forum_channel_id)
    log_manager.staff_role_id = int(staff_role_id)
    log_manager.role_id = int(target_role_id)
    log_manager.guild_id = int(guild_id)
    forum_pacer.forum_channel_id = int(forum_channel_id)
    log_manager.set_auto_log(bool(auto_create_enabled))

    if log_manager.auto_log_enabled and log_manager.role_id:
        log_manager.schedule_auto_reconcile(force=True)


@tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
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


class ConfirmCreateLogView(discord.ui.View):
    def __init__(
        self,
        pending_members: list[discord.Member],
        owner_id: int,
        origin: discord.Interaction,
    ) -> None:
        super().__init__(timeout=60)
        self.pending_members = pending_members
        self.owner_id = owner_id
        self.origin = origin
        self.confirmed = False

    async def on_timeout(self) -> None:
        # BUG FIX: the Confirm button used to stay on screen and fail with
        # "interaction failed" after the 60s timeout.
        if self.confirmed:
            return
        try:
            await self.origin.edit_original_response(
                content="Confirmation timed out. Run `/create_log` again.",
                view=None,
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.secondary)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who started this command can confirm it.",
                ephemeral=True,
            )
            return

        # Re-check current access at button time as well as command time. A
        # staff member whose role was removed after starting the command must
        # not be able to confirm a pending job.
        if interaction.user.id != BOT_OWNER_ID:
            current_staff_role_id = getattr(log_manager, "staff_role_id", 0)
            current_member = (
                interaction.user
                if isinstance(interaction.user, discord.Member)
                else None
            )
            if not current_staff_role_id or current_member is None or not any(
                role.id == current_staff_role_id for role in current_member.roles
            ):
                await interaction.response.send_message(
                    "You need the configured staff role to confirm this action.",
                    ephemeral=True,
                )
                return

        if self.confirmed:
            return

        self.confirmed = True

        await interaction.response.edit_message(
            content="Starting member logging...",
            view=None,
            embed=None,
        )
        self.stop()

        asyncio.create_task(
            run_confirmed_create_log(interaction, self.pending_members),
            name="confirmed-create-log",
        )


async def run_confirmed_create_log(
    interaction: discord.Interaction,
    pending_members: list[discord.Member],
) -> None:
    try:
        if log_manager.job.running:
            await interaction.edit_original_response(
                content="Another logging job is already running.",
                embed=None,
                view=None,
            )
            return

        await log_manager.start_member_job(pending_members)

        await monitor_job_message(log_manager, interaction, "Creating member logs")

    except Exception as exc:
        logger.exception("Confirmed create-log job failed to start")
        try:
            await interaction.edit_original_response(
                content=f"Could not start member logging: `{exc}`",
                embed=None,
                view=None,
            )
        except discord.HTTPException:
            logger.debug("Could not report create-log startup error", exc_info=True)


# Access to operational commands is enforced by LogCommandTree.interaction_check
# using the configured owner ID and staff role; Discord administrator status is
# not required when a member has the staff role.
@app_commands.command(
    name="create_log",
    description="Create a forum post for every member with the configured role.",
)
@app_commands.guild_only()
async def create_log(interaction: discord.Interaction) -> None:
    # Acknowledge immediately so a large guild/forum scan cannot cause an
    # "application did not respond" interaction timeout.
    await interaction.response.defer(ephemeral=True)

    try:
        forum = await log_manager.get_forum_channel()

        if interaction.guild_id != forum.guild.id or interaction.guild is None:
            await interaction.edit_original_response(
                content="This command can only be used in the configured server.",
            )
            return

        if log_manager.job.running:
            await interaction.edit_original_response(
                content="Another logging job is already running.",
            )
            return

        members = await log_manager.fetch_role_members(interaction.guild)

        if not members:
            await interaction.edit_original_response(
                content=(
                    "No members currently have the configured role. "
                    "No logs were created."
                ),
            )
            return

        pending, already_done = await log_manager.get_pending_members(members, forum)

        if not pending:
            await interaction.edit_original_response(
                content=(
                    f"All `{already_done}` members with the configured role "
                    "already have forum logs. Nothing was created."
                ),
            )
            return

        view = ConfirmCreateLogView(
            pending_members=pending,
            owner_id=interaction.user.id,
            origin=interaction,
        )

        await interaction.edit_original_response(
            content=(
                "Are you sure you would like to create logs for "
                f"`{len(pending)}` members?"
            ),
            view=view,
        )

    except Exception as exc:
        logger.exception("create_log command failed")
        try:
            await interaction.edit_original_response(
                content=f"Could not prepare member logging: `{exc}`",
                embed=None,
                view=None,
            )
        except discord.HTTPException:
            logger.debug("Could not report create-log error", exc_info=True)


@app_commands.command(
    name="status",
    description="Show the current logging progress.",
)
@app_commands.guild_only()
async def status(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        embed=build_status_embed(log_manager.job),
        ephemeral=True,
    )

    if not log_manager.job.running:
        return

    # Interaction tokens only live 15 minutes, so stop editing before then.
    deadline = asyncio.get_running_loop().time() + 14 * 60

    while log_manager.job.running and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(2.5)
        try:
            await interaction.edit_original_response(
                embed=build_status_embed(log_manager.job),
            )
        except discord.HTTPException:
            return

    try:
        await interaction.edit_original_response(
            embed=build_status_embed(log_manager.job),
        )
    except discord.HTTPException:
        pass


@app_commands.command(
    name="auto_log",
    description="Enable or disable automatic member logging.",
)
@app_commands.guild_only()
@app_commands.describe(
    enabled="True to enable automatic logging, false to disable it.",
)
async def auto_log(
    interaction: discord.Interaction,
    enabled: bool,
) -> None:
    # Save first, then confirm (the old order could report success even if
    # saving the setting failed).
    if enabled and not log_manager.role_id:
        await interaction.response.send_message(
            "Select a member role in `/settings` before enabling automatic logging.",
            ephemeral=True,
        )
        return

    log_manager.set_auto_log(enabled)
    await interaction.response.send_message(
        content=f"Automatic logging is now **{'enabled' if enabled else 'disabled'}**.",
        ephemeral=True,
    )
    if enabled and log_manager.forum_channel_id:
        # Enabling Auto Log also repairs/queues existing members; it is not
        # limited to members who join after the setting is turned on. If no
        # forum has been selected yet, saving /settings will trigger this.
        log_manager.schedule_auto_reconcile(force=True)


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if not log_manager.forum_channel_id or not log_manager.auto_log_enabled or member.bot:
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
    if not log_manager.forum_channel_id or not log_manager.auto_log_enabled or after.bot:
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
    # Raw event: also fires for threads that were not in the cache.
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
        # Gateway reconnects do not guarantee that every member event that
        # occurred while offline was replayed. Reconcile persisted/Discord
        # state whenever Auto Log is enabled.
        if log_manager.forum_channel_id:
            log_manager.schedule_auto_reconcile()


@bot.event
async def on_error(event: str, *args, **kwargs) -> None:
    logger.exception("Unhandled Discord event error in %s", event)


if __name__ == "__main__":
    # log_handler=None: we already configured logging above. Without this,
    # discord.py installs a second root handler and every line prints twice.
    bot.run(DISCORD_TOKEN, log_handler=None)