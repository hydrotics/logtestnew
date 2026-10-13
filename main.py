from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import discord
import watermark as wm
from discord import app_commands
from dotenv import load_dotenv
from PIL import Image

from create_log import (
    Database,
    ForumCreatePacer,
    LogManager,
    build_status_embed,
    monitor_job_message,
)
from creation_prompt import setup_creation_prompt
from render import KeepAliveServer
from reveal_history import RevealHistoryError, RevealHistoryStore, make_history_command
from settings import build_settings_command
from status import StatusTracker
from test import build_test_command

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.sqlite3"),
)

try:
    FORUM_CHANNEL_ID = int(os.getenv("DISCORD_FORUM_CHANNEL_ID", "0"))
    DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
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

try:
    REVEAL_ARCHIVE_CHANNEL_ID = int(os.getenv("REVEAL_ARCHIVE_CHANNEL_ID", "0") or "0")
    if REVEAL_ARCHIVE_CHANNEL_ID < 0:
        raise ValueError("REVEAL_ARCHIVE_CHANNEL_ID must be >= 0")
except ValueError as exc:
    raise RuntimeError("REVEAL_ARCHIVE_CHANNEL_ID must be a valid non-negative channel ID.") from exc

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

        guild_id = log_manager.guild_id or DISCORD_GUILD_ID
        if not guild_id and log_manager.forum_channel_id:
            try:
                configured_forum = await log_manager.get_forum_channel()
                guild_id = configured_forum.guild.id
                log_manager.guild_id = guild_id
            except Exception:
                logger.warning(
                    "Could not infer the command guild from the configured forum; "
                    "using global command registration so `/settings` remains available.",
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
            apply_settings=apply_bot_settings,
            apply_creation_settings=apply_creation_settings,
        )
        current_commands = (
            create_log,
            status,
            auto_log,
            creation_prompt_command,
            build_test_command(log_manager),
            settings_command,
            reveal_media,
            trace_reveal,
            reveal_status,
            reveal_history_command,
        )
        expected_names = {
            "auto_log",
            "create_log",
            "creation_prompt",
            "settings",
            "status",
            "test",
            "reveal_media",
            "trace_reveal",
            "reveal_status",
            "reveal_history",
        }

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                if guild_id:
                    guild = discord.Object(id=guild_id)
                    self.tree.clear_commands(guild=guild)
                    self.tree.clear_commands(guild=None)
                    for command in current_commands:
                        self.tree.add_command(command, guild=guild, override=True)
                    synced = await self.tree.sync(guild=guild)

                    stale_global = await self.tree.fetch_commands()
                    if stale_global:
                        await self.tree.sync()
                        logger.info(
                            "Removed %d stale global command registration(s).",
                            len(stale_global),
                        )
                else:
                    self.tree.clear_commands(guild=None)
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

                logger.info(
                    "Commands ready (%s) in guild %s: %s",
                    "guild-scoped" if guild_id else "global first-run fallback",
                    guild_id or "not selected yet",
                    ", ".join(sorted(synced_names)),
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
    saved_target_role_id = int(database.get_setting("target_role_id") or "0")
    saved_guild_id = int(database.get_setting("guild_id") or "0")
    saved_creation_prompt_channel_id = int(
        database.get_setting("creation_prompt_channel_id") or "0"
    )
    saved_creation_approve_channel_id = int(
        database.get_setting("creation_approve_channel_id") or "0"
    )
except ValueError as exc:
    raise RuntimeError("Saved bot settings in SQLite are invalid.") from exc

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
log_manager.creation_prompt_channel_id = saved_creation_prompt_channel_id
log_manager.creation_approve_channel_id = saved_creation_approve_channel_id


class _DynamicStaffRoleId:
    """Keep the history command's role check in step with `/settings` changes."""

    def __init__(self, value: int = 0) -> None:
        self.value = int(value)

    def __int__(self) -> int:
        return self.value


history_staff_role_id = _DynamicStaffRoleId(saved_staff_role_id)
reveal_history_store = RevealHistoryStore(
    bot=bot,
    archive_channel_id=REVEAL_ARCHIVE_CHANNEL_ID,
    staff_role_id=saved_staff_role_id,
)

# StatusTracker instances are retained for the active/recent reveal IDs in this process.
reveal_status_trackers: dict[str, StatusTracker] = {}
latest_reveal_status_id: str | None = None
active_reveal_jobs = 0

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
REVEAL_CACHE_DIR = Path(tempfile.gettempdir()) / "revealbot-originals"


def _watermark_key() -> bytes:
    secret = os.getenv("WATERMARK_SECRET", "")
    if not secret or len(secret) < 16:
        raise RuntimeError("WATERMARK_SECRET is missing or too short (minimum 16 characters).")
    return secret.encode("utf-8")


def _safe_filename(value: str, fallback: str = "media") -> str:
    name = Path(str(value or "")).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or fallback


def _infer_trace_kind(original: Path, leak: Path, requested: str) -> str:
    if requested != "auto":
        return requested
    orig_is_image = original.suffix.lower() in IMAGE_EXTS
    leak_is_image = leak.suffix.lower() in IMAGE_EXTS
    if orig_is_image:
        return "image"
    if leak_is_image:
        return "video_frame"
    if leak.suffix.lower() in VIDEO_EXTS:
        return "video"
    raise ValueError("Leak must be an image or video file.")


def _new_reveal_status(reveal_id: str, user_id: int) -> StatusTracker:
    tracker = StatusTracker()
    tracker.reset(reveal_id)
    tracker.request(user_id)
    reveal_status_trackers[reveal_id] = tracker
    return tracker


def apply_bot_settings(
    forum_channel_id: int,
    staff_role_id: int,
    target_role_id: int,
    auto_create_enabled: bool,
    guild_id: int,
) -> None:
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

    history_staff_role_id.value = int(staff_role_id)
    reveal_history_store.staff_role_id = int(staff_role_id)

    if log_manager.auto_log_enabled and log_manager.role_id:
        log_manager.schedule_auto_reconcile(force=True)


def apply_creation_settings(prompt_channel_id: int, approve_channel_id: int) -> None:
    database.set_setting("creation_prompt_channel_id", str(int(prompt_channel_id)))
    database.set_setting("creation_approve_channel_id", str(int(approve_channel_id)))
    log_manager.creation_prompt_channel_id = int(prompt_channel_id)
    log_manager.creation_approve_channel_id = int(approve_channel_id)


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


@app_commands.command(
    name="create_log",
    description="Create a forum post for every member with the configured role.",
)
@app_commands.guild_only()
async def create_log(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    try:
        forum = await log_manager.get_forum_channel()

        if interaction.guild_id != forum.guild.id or interaction.guild is None:
            await interaction.edit_original_response(
                content="This command can only be used in the configured server."
            )
            return

        if log_manager.job.running:
            await interaction.edit_original_response(
                content="Another logging job is already running."
            )
            return

        members = await log_manager.fetch_role_members(interaction.guild)
        if not members:
            await interaction.edit_original_response(
                content=(
                    "No members currently have the configured role. "
                    "No logs were created."
                )
            )
            return

        pending, already_done = await log_manager.get_pending_members(members, forum)
        if not pending:
            await interaction.edit_original_response(
                content=(
                    f"All `{already_done}` members with the configured role "
                    "already have forum logs. Nothing was created."
                )
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
        log_manager.schedule_auto_reconcile(force=True)


@app_commands.command(
    name="reveal_media",
    description="Watermark an image or video and privately deliver it to a member.",
)
@app_commands.guild_only()
@app_commands.describe(
    media="The clean image or video to watermark.",
    recipient="The server member who should receive the watermarked media.",
)
async def reveal_media(
    interaction: discord.Interaction,
    media: discord.Attachment,
    recipient: discord.Member,
) -> None:
    global latest_reveal_status_id, active_reveal_jobs

    await interaction.response.defer(ephemeral=True, thinking=True)

    if not reveal_history_store.enabled:
        await interaction.edit_original_response(
            content="Reveal delivery is disabled: set `REVEAL_ARCHIVE_CHANNEL_ID` in `.env` first."
        )
        return

    extension = Path(media.filename).suffix.lower()
    if extension not in IMAGE_EXTS | VIDEO_EXTS:
        await interaction.edit_original_response(
            content="Unsupported file type. Upload a JPG, PNG, WEBP, MP4, MOV, WEBM, MKV, AVI, or M4V."
        )
        return
    if recipient.bot:
        await interaction.edit_original_response(content="Choose a human member as the recipient.")
        return

    try:
        key = _watermark_key()
    except RuntimeError as exc:
        await interaction.edit_original_response(content=str(exc))
        return

    reveal_id = uuid.uuid4().hex
    tracker = _new_reveal_status(reveal_id, recipient.id)
    latest_reveal_status_id = reveal_id
    active_reveal_jobs += 1

    archive_complete = False
    delivery_complete = False
    try:
        with tempfile.TemporaryDirectory(prefix="revealbot-embed-") as temporary_dir:
            work_dir = Path(temporary_dir)
            source_path = work_dir / _safe_filename(media.filename, "original")
            await media.save(source_path)

            is_image = extension in IMAGE_EXTS
            if is_image:
                delivery_path = work_dir / "watermarked.jpg"
                with Image.open(source_path) as source_image:
                    width, height = source_image.size
                await asyncio.to_thread(
                    wm.render_image_delivery,
                    source_path,
                    delivery_path,
                    recipient.id,
                    key,
                    reveal_id,
                )
                profile = None
                fps = 0.0
                duration = 0.0
                kind = "image"
            else:
                delivery_path = work_dir / "watermarked.mp4"
                source_info = await asyncio.to_thread(wm.video_info, source_path, 1080)
                video_profile = await asyncio.to_thread(
                    wm.embed_video,
                    source_path,
                    delivery_path,
                    recipient.id,
                    key,
                    reveal_id,
                    max_height=1080,
                    max_seconds=300,
                )
                width = int(video_profile.get("width", source_info.get("width", 0)))
                height = int(video_profile.get("height", source_info.get("height", 0)))
                fps = float(video_profile.get("fps", source_info.get("fps", 0.0)))
                duration = min(300.0, float(source_info.get("duration", 0.0)))
                profile = (height, fps)
                kind = "video"

            archive_record = {
                "reveal_id": reveal_id,
                "kind": kind,
                "path": str(source_path),
                "filename": media.filename,
                "created_at": int(time.time()),
                "width": int(width),
                "height": int(height),
                "fps": float(fps),
                "duration": float(duration),
            }
            await reveal_history_store.archive(archive_record)
            archive_complete = True

            with discord.File(delivery_path, filename=delivery_path.name) as outgoing_file:
                await recipient.send(
                    content=f"Your media delivery is ready. Reveal ID: `{reveal_id}`",
                    file=outgoing_file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            delivery_complete = True

            await reveal_history_store.record_delivery(reveal_id, recipient.id, profile=profile)
            tracker.success(recipient.id)

        await interaction.edit_original_response(
            content=(
                f"Watermarked media sent to {recipient.mention}.\n"
                f"Reveal ID: `{reveal_id}`\n"
                "The clean original was archived for future tracing."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except RevealHistoryError as exc:
        logger.exception("Reveal archive operation failed for %s", reveal_id)
        await interaction.edit_original_response(
            content=f"Reveal operation failed: {exc}\nReveal ID: `{reveal_id}`"
        )
    except discord.Forbidden:
        logger.info("Discord denied a reveal operation for %s / user %s", reveal_id, recipient.id)
        if archive_complete and not delivery_complete:
            message = (
                f"The clean original was archived as reveal `{reveal_id}`, but Discord did not allow the bot "
                f"to DM {recipient.mention}. No successful delivery was recorded."
            )
        elif delivery_complete:
            message = (
                f"The media was sent to {recipient.mention}, but the bot could not update the archive's delivery "
                f"record for reveal `{reveal_id}`. Check the archive-channel permissions and logs."
            )
        else:
            message = (
                "Discord denied access to the reveal archive or recipient. Check the archive channel permissions "
                f"and the recipient's DM settings. Reveal ID: `{reveal_id}`"
            )
        await interaction.edit_original_response(
            content=message,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception as exc:
        logger.exception("Reveal delivery failed for %s", reveal_id)
        if delivery_complete:
            message = (
                f"The media was sent to {recipient.mention}, but updating the reveal record failed "
                f"({type(exc).__name__}). Check the bot logs. Reveal ID: `{reveal_id}`"
            )
        else:
            message = f"Reveal delivery failed ({type(exc).__name__}): {exc}\nReveal ID: `{reveal_id}`"
        await interaction.edit_original_response(
            content=message,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    finally:
        active_reveal_jobs = max(0, active_reveal_jobs - 1)


@app_commands.command(
    name="trace_reveal",
    description="Trace a leaked image, video frame, or video against an archived reveal.",
)
@app_commands.guild_only()
@app_commands.describe(
    reveal_id="The ID shown when the original media was delivered.",
    leak="The leaked image, screenshot/frame, or video.",
    kind="Choose automatic detection or explicitly set the leak type.",
    start_frame="Optional source frame index to start from; zero searches automatically.",
    delivery_height="Stored delivered-video height, if known; zero tries the configured ladder.",
    delivery_fps="Stored delivered-video FPS, if known; zero lets the tracer estimate it.",
    time_budget="Maximum trace runtime in seconds (5–180).",
)
@app_commands.choices(
    kind=[
        app_commands.Choice(name="Automatic", value="auto"),
        app_commands.Choice(name="Image", value="image"),
        app_commands.Choice(name="Video frame / screenshot", value="video_frame"),
        app_commands.Choice(name="Video", value="video"),
    ]
)
async def trace_reveal(
    interaction: discord.Interaction,
    reveal_id: str,
    leak: discord.Attachment,
    kind: app_commands.Choice[str],
    start_frame: app_commands.Range[int, 0, 10_000_000] = 0,
    delivery_height: app_commands.Range[int, 0, 4320] = 0,
    delivery_fps: app_commands.Range[float, 0.0, 120.0] = 0.0,
    time_budget: app_commands.Range[int, 5, 180] = 90,
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)

    if not reveal_history_store.enabled:
        await interaction.edit_original_response(
            content="Tracing is disabled: set `REVEAL_ARCHIVE_CHANNEL_ID` in `.env` first."
        )
        return

    try:
        key = _watermark_key()
    except RuntimeError as exc:
        await interaction.edit_original_response(content=str(exc))
        return

    try:
        record = await reveal_history_store.get(reveal_id.strip(), force_refresh=True)
        if record is None:
            await interaction.edit_original_response(
                content=f"No archived reveal was found for ID `{reveal_id}`."
            )
            return

        leak_extension = Path(leak.filename).suffix.lower()
        if leak_extension not in IMAGE_EXTS | VIDEO_EXTS:
            await interaction.edit_original_response(
                content="Unsupported leak type. Upload an image or video file."
            )
            return

        REVEAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        original_filename = _safe_filename(record.get("archive_filename") or record.get("filename") or "original")
        safe_reveal_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(record["reveal_id"]))
        original_path = REVEAL_CACHE_DIR / f"{safe_reveal_id}_{original_filename}"
        await reveal_history_store.ensure_local_copy(record, original_path)

        with tempfile.TemporaryDirectory(prefix="revealbot-trace-") as temporary_dir:
            leak_path = Path(temporary_dir) / _safe_filename(leak.filename, "leak")
            await leak.save(leak_path)
            selected_kind = _infer_trace_kind(original_path, leak_path, kind.value)
            chosen_height = delivery_height if delivery_height > 0 else None
            chosen_fps = delivery_fps if delivery_fps > 0 else None
            stored_profiles = record.get("profiles", [])
            if stored_profiles:
                # The archive records delivered height/FPS after a successful video DM.
                if chosen_height is None:
                    chosen_height = int(stored_profiles[0][0])
                if chosen_fps is None:
                    chosen_fps = float(stored_profiles[0][1])

            result = await asyncio.to_thread(
                wm.extract,
                original_path,
                leak_path,
                selected_kind,
                key,
                str(record["reveal_id"]),
                max_height=chosen_height or 1080,
                start_frame=int(start_frame),
                delivery_height=chosen_height,
                delivery_fps=chosen_fps,
                time_budget=float(time_budget),
            )

        valid = bool(result.get("valid", result.get("ok", False)))
        if not valid:
            await interaction.edit_original_response(
                content=(
                    f"No CRC-valid watermark was found for reveal `{record['reveal_id']}`. "
                    f"Trace metric: `{float(result.get('metric', 0.0)):.4f}`."
                )
            )
            return

        found_user_id = int(result.get("user_id", 0) or 0)
        served_user_ids = {int(value) for value in record.get("served_user_ids", [])}
        if found_user_id in served_user_ids:
            attribution = (
                f"The CRC-verified payload matches <@{found_user_id}>, and the archive records that account "
                "as a recipient of this reveal. This is a tracing result, not proof by itself of who redistributed it."
            )
        else:
            attribution = (
                f"The CRC-verified payload contains user ID `{found_user_id}`, but that ID is not listed among "
                "the recipients recorded for this reveal. Do not attribute the leak to that account without further verification."
            )

        details = [
            f"Reveal ID: `{record['reveal_id']}`",
            f"Type searched: `{selected_kind}`",
            f"CRC verified: `yes`",
            f"Metric: `{float(result.get('metric', 0.0)):.4f}`",
            attribution,
        ]
        if "time" in result:
            details.append(f"Matched source time: `{float(result['time']):.2f}s` (frame `{int(result.get('frame', 0))}`)")
        await interaction.edit_original_response(
            content="\n".join(details),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except RevealHistoryError as exc:
        logger.exception("Could not restore original reveal %s", reveal_id)
        await interaction.edit_original_response(content=f"Trace failed: {exc}")
    except Exception as exc:
        logger.exception("Trace failed for reveal %s", reveal_id)
        await interaction.edit_original_response(
            content=f"Trace failed ({type(exc).__name__}): {exc}"
        )


@app_commands.command(
    name="reveal_status",
    description="Show delivery progress for the latest or a specific reveal ID.",
)
@app_commands.guild_only()
@app_commands.describe(reveal_id="Optional reveal ID; omit this to view the latest delivery.")
async def reveal_status(
    interaction: discord.Interaction,
    reveal_id: str | None = None,
) -> None:
    chosen_id = (reveal_id or latest_reveal_status_id or "").strip()
    if not chosen_id:
        await interaction.response.send_message(
            "No reveal delivery has been tracked in this process yet.",
            ephemeral=True,
        )
        return

    tracker = reveal_status_trackers.get(chosen_id)
    if tracker is None:
        await interaction.response.send_message(
            f"No in-memory status snapshot is available for reveal `{chosen_id}`. "
            "Status snapshots reset when the bot restarts; delivery history remains in the archive.",
            ephemeral=True,
        )
        return

    snapshot = tracker.snapshot()
    state = snapshot.bot_state(
        waiting=0,
        running=active_reveal_jobs,
        estimated_wait=0.0,
        ready=bot.is_ready(),
    )
    embed = discord.Embed(title="Reveal Delivery Status")
    embed.add_field(name="Reveal ID", value=f"`{chosen_id}`", inline=False)
    embed.add_field(name="Delivery progress", value=f"{snapshot.progress_bar()}\n{snapshot.completed_display}", inline=True)
    embed.add_field(name="Bot state", value=state, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


reveal_history_command = make_history_command(
    reveal_history_store,
    staff_role_id=history_staff_role_id,  # dynamic so `/settings` role changes take effect immediately
)


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


@bot.event
async def on_error(event: str, *args, **kwargs) -> None:
    logger.exception("Unhandled Discord event error in %s", event)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)