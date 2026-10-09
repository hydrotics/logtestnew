from __future__ import annotations

import asyncio
import logging
import math
import os
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

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = os.getenv("DATABASE_PATH", "/var/data/bot_data.sqlite3")

try:
    ROLE_ID = int(os.getenv("DISCORD_ROLE_ID", "0"))
    FORUM_CHANNEL_ID = int(os.getenv("DISCORD_FORUM_CHANNEL_ID", "0"))
    # Kept for compatibility with existing deployments. The forum-create route
    # is deliberately single-flight in LogManager to prevent request bursts.
    CREATE_CONCURRENCY = int(os.getenv("CREATE_CONCURRENCY", "1"))
except ValueError as exc:
    raise RuntimeError(
        "DISCORD_ROLE_ID, DISCORD_FORUM_CHANNEL_ID and CREATE_CONCURRENCY "
        "must be valid values."
    ) from exc

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")
if not ROLE_ID:
    raise RuntimeError("DISCORD_ROLE_ID is missing from .env")
if not FORUM_CHANNEL_ID:
    raise RuntimeError("DISCORD_FORUM_CHANNEL_ID is missing from .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("discord-log-bot")

intents = discord.Intents.default()
intents.members = True


class LogBot(discord.Client):
    """One client and one CommandTree. Commands are synced only once per run."""

    def __init__(self) -> None:
        super().__init__(
            intents=intents,
            # Keep waiting indefinitely when discord.py encounters a normal
            # route/global rate limit rather than surfacing RateLimited.
            max_ratelimit_timeout=None,
        )
        self.tree = app_commands.CommandTree(self)
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
        """Start the keep-alive server, then register the guild commands once."""
        # Bind the port FIRST so Render sees an open port immediately.
        try:
            await self.keepalive.start()
        except Exception:
            logger.exception("Keep-alive server failed to start")

        last_error: Exception | None = None

        for attempt in range(1, 4):
            try:
                forum = await log_manager.get_forum_channel()
                guild = discord.Object(id=forum.guild.id)

                current_commands = (
                    create_log,
                    status,
                    auto_log,
                    build_test_command(log_manager),
                )

                for command in current_commands:
                    self.tree.add_command(command, guild=guild, override=True)

                synced = await self.tree.sync(guild=guild)

                names = sorted(command.name for command in synced)
                expected = ["auto_log", "create_log", "status", "test"]

                if names != expected:
                    raise RuntimeError(
                        "Guild command sync verification failed. "
                        f"Expected {expected}, got {names}."
                    )

                status_command = next(c for c in synced if c.name == "status")
                log_manager.status_mention = status_command.mention

                # Only after guild commands are confirmed do we remove stale
                # global commands.
                self.tree.clear_commands(guild=None)
                try:
                    await self.tree.sync()
                except Exception:
                    logger.warning(
                        "Could not remove old global commands; guild commands are still ready.",
                        exc_info=True,
                    )

                # BUG FIX: forum.guild is only a bare discord.Object here (the
                # guild cache is empty during setup_hook), so `.name` raised
                # AttributeError, setup "failed" three times, and
                # commands_ready was never set.
                logger.info(
                    "Commands ready in guild %s: %s",
                    forum.guild.id,
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

logger.info("Using SQLite database at %s", os.path.abspath(DATABASE_PATH))
database = Database(DATABASE_PATH)
log_manager = LogManager(
    bot=bot,
    database=database,
    role_id=ROLE_ID,
    forum_channel_id=FORUM_CHANNEL_ID,
    forum_pacer=forum_pacer,
    concurrency=CREATE_CONCURRENCY,
)


@tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the **Administrator** permission to use this command."
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


# Admin-only by default (commands can still be re-scoped in Server Settings ->
# Integrations). Previously ANY member could run /test 10000 or /create_log.
@app_commands.command(
    name="create_log",
    description="Create a forum post for every member with the configured role.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
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
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    enabled="True to enable automatic logging, false to disable it.",
)
async def auto_log(
    interaction: discord.Interaction,
    enabled: bool,
) -> None:
    # Save first, then confirm (the old order could report success even if
    # saving the setting failed).
    log_manager.set_auto_log(enabled)
    await interaction.response.send_message(
        content=f"Automatic logging is now **{'enabled' if enabled else 'disabled'}**.",
        ephemeral=True,
    )
    if enabled:
        # Enabling Auto Log also repairs/queues existing members; it is not
        # limited to members who join after the setting is turned on.
        log_manager.schedule_auto_reconcile(force=True)


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if not log_manager.auto_log_enabled or member.bot:
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
    if not log_manager.auto_log_enabled or after.bot:
        return

    role = after.guild.get_role(ROLE_ID)
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
    if payload.parent_id == FORUM_CHANNEL_ID:
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
        log_manager.schedule_auto_reconcile()


@bot.event
async def on_error(event: str, *args, **kwargs) -> None:
    logger.exception("Unhandled Discord event error in %s", event)


if __name__ == "__main__":
    # log_handler=None: we already configured logging above. Without this,
    # discord.py installs a second root handler and every line prints twice.
    bot.run(DISCORD_TOKEN, log_handler=None)