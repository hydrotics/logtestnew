from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
import discord
import watermark as wm
from dotenv import load_dotenv
from discord import app_commands
from PIL import Image

from create_log import (
    Database,
    ForumCreatePacer,
    LogManager,
    build_status_embed,
    monitor_job_message,
)
from creation_prompt import setup_creation_prompt
from reveal_history import ArchiveServedUsersView, RevealHistoryError, RevealHistoryStore, make_history_command
import settings as bot_settings
from settings import build_settings_command
from status import StatusTracker
from test import build_test_command

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _setting(name: str, default):
    """Read runtime configuration from settings.py rather than .env."""
    return getattr(bot_settings, name, default)


def _setting_any(names: tuple[str, ...], default):
    for name in names:
        if hasattr(bot_settings, name):
            return getattr(bot_settings, name)
    return default


def _bool_setting(name: str, default: bool) -> bool:
    value = _setting(name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


# Secrets and the owner identity stay in .env. All other bot settings remain in settings.py
# or in the SQLite settings store that is managed by /settings.
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
BOT_OWNER_ID_RAW = os.getenv("BOT_OWNER_ID", "").strip()

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")

if not BOT_OWNER_ID_RAW:
    raise RuntimeError("BOT_OWNER_ID is missing from .env")

try:
    BOT_OWNER_ID = int(BOT_OWNER_ID_RAW)
except (TypeError, ValueError) as exc:
    raise RuntimeError("BOT_OWNER_ID in .env must be a valid Discord user ID") from exc

if BOT_OWNER_ID <= 0:
    raise RuntimeError("BOT_OWNER_ID in .env must be a positive Discord user ID")

DATABASE_PATH = str(_setting("DATABASE_PATH", BASE_DIR / "bot_data.sqlite3"))

try:
    FORUM_CHANNEL_ID = int(_setting_any(("DISCORD_FORUM_CHANNEL_ID", "FORUM_CHANNEL_ID"), 0) or 0)
    DISCORD_GUILD_ID = int(_setting_any(("DISCORD_GUILD_ID", "GUILD_ID"), 0) or 0)
    CREATE_CONCURRENCY = int(_setting_any(("CREATE_CONCURRENCY", "MAX_CONCURRENT_JOBS"), 1) or 1)
except (TypeError, ValueError) as exc:
    raise RuntimeError(
        "DISCORD_FORUM_CHANNEL_ID, DISCORD_GUILD_ID and CREATE_CONCURRENCY "
        "must be configured with valid values in settings.py."
    ) from exc

# Forum/guild IDs are allowed to be absent on a first run because /settings can
# configure them and the active values are persisted in SQLite. If these optional
# constants are explicitly provided in settings.py, keep validating their values.
if FORUM_CHANNEL_ID <= 0 and any(
    hasattr(bot_settings, name)
    for name in ("DISCORD_FORUM_CHANNEL_ID", "FORUM_CHANNEL_ID")
):
    raise RuntimeError("DISCORD_FORUM_CHANNEL_ID in settings.py must be a positive Discord channel ID")
if DISCORD_GUILD_ID <= 0 and any(
    hasattr(bot_settings, name)
    for name in ("DISCORD_GUILD_ID", "GUILD_ID")
):
    raise RuntimeError("DISCORD_GUILD_ID in settings.py must be a positive Discord server ID")
if CREATE_CONCURRENCY <= 0:
    raise RuntimeError("CREATE_CONCURRENCY in settings.py must be a positive integer")

# Reveal-specific defaults come from settings.py. Values saved through /settings
# are loaded from SQLite below and take precedence over these defaults.
REVEAL_ARCHIVE_CHANNEL_ID = int(_setting("REVEAL_ARCHIVE_CHANNEL_ID", 0) or 0)
WATERMARK_SECRET = os.getenv("WATERMARK_SECRET", "").strip()
BOOSTER_ROLE_ID = int(_setting("BOOSTER_ROLE_ID", 0) or 0)
MAX_UPLOAD_BYTES = int(
    _setting("MAX_UPLOAD_BYTES", float(_setting("MAX_UPLOAD_MB", 120) or 120) * 1048576)
    or (120 * 1048576)
)
MAX_URL_BYTES = min(
    MAX_UPLOAD_BYTES,
    int(float(_setting("MAX_MEDIA_URL_MB", 120) or 120) * 1048576),
)
MAX_VIDEO_SECONDS = int(_setting("MAX_VIDEO_SECONDS", 180) or 180)
MAX_VIDEO_HEIGHT = int(_setting("MAX_VIDEO_HEIGHT", 720) or 720)
MAX_IMAGE_PIXELS = int(
    min(
        float(_setting("MAX_IMAGE_MEGAPIXELS", 40) or 40),
        float(_setting("TRACE_IMAGE_MAX_MEGAPIXELS", 18) or 18),
        float(_setting("EMBED_IMAGE_MAX_MEGAPIXELS", 40) or 40),
    ) * 1_000_000
)
TRACE_LEAK_MAX_PIXELS = int(
    _setting(
        "TRACE_LEAK_MAX_PIXELS",
        float(_setting("TRACE_IMAGE_MAX_MEGAPIXELS", 18) or 18) * 1_000_000,
    ) or 18_000_000
)
VIDEO_PRESET = str(_setting("VIDEO_PRESET", "superfast") or "superfast").strip().lower()
IMAGE_AMP = float(_setting("WM_IMAGE_AMP", 3.0) or 3.0)
VIDEO_AMP = float(_setting("WM_VIDEO_AMP", 3.0) or 3.0)
VIDEO_AUDIO_KBPS = int(_setting("VIDEO_AUDIO_KBPS", 48) or 48)
VIDEO_TARGET_MAX_MB = float(_setting("VIDEO_TARGET_MAX_MB", 48) or 48)
TRACE_MAX_REVEALS = max(1, int(_setting("TRACE_MAX_REVEALS", 12) or 12))
TRACE_TIME_BUDGET = max(10.0, float(_setting("TRACE_TIME_BUDGET", 90.0) or 90.0))
TRACE_IMAGE_TIME_BUDGET = max(3.0, float(_setting("TRACE_IMAGE_TIME_BUDGET", 8.0) or 8.0))
TRACE_VIDEO_TIME_BUDGET = max(5.0, float(_setting("TRACE_VIDEO_TIME_BUDGET", 20.0) or 20.0))
CACHE_MAX_BYTES = int(float(_setting("CACHE_MAX_MB", 768) or 768) * 1048576)
CACHE_TTL_SECONDS = max(300, int(_setting("CACHE_TTL_SECONDS", 3600) or 3600))
AUTO_DELETE_OLD_ORIGINALS = _bool_setting("AUTO_DELETE_OLD_ORIGINALS", True)
IMAGE_CACHE_VERSION = str(_setting("IMAGE_CACHE_VERSION", "fullres-jpeg-444-v2"))
VIDEO_CACHE_VERSION = str(_setting("VIDEO_CACHE_VERSION", "quality-first-crf-v1"))
_DEFAULT_DATA_DIR = Path("/var/data") if Path("/var/data").is_dir() else BASE_DIR / "data"
DATA_DIR = Path(_setting("DATA_DIR", _DEFAULT_DATA_DIR))
REVEALS_DIR = DATA_DIR / "reveals"
CACHE_DIR = DATA_DIR / "cache"
TMP_DIR = DATA_DIR / "tmp"
for _directory in (REVEALS_DIR, CACHE_DIR, TMP_DIR):
    _directory.mkdir(parents=True, exist_ok=True)

DEFAULT_UPLOAD_LIMIT = 20 * 1048576
URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
HTTP_USER_AGENT = "RevealBot/1.1 (+https://discord.com/)"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
REVEAL_ID_RE = re.compile(r"^\d+_[0-9a-f]{8}$")

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

        if command_name == "view_reveal":
            # This command performs its own booster-role check after server scoping.
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

    async def setup_hook(self) -> None:
        await asyncio.to_thread(startup_cleanup)
        self.add_view(RevealButtonView())
        self.archive_served_users_view = ArchiveServedUsersView(
            reveal_history_store, saved_staff_role_id
        )
        self.add_view(self.archive_served_users_view)
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
            apply_reveal_settings=apply_reveal_settings,
        )
        current_commands = (
            create_log,
            status,
            auto_log,
            creation_prompt_command,
            build_test_command(log_manager),
            settings_command,
            reveal_media,
            upload,
            view_reveal,
            add_reveal_button,
            trace,
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
            "upload",
            "view_reveal",
            "Add Reveal Button",
            "trace",
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
    saved_reveal_archive_channel_id = int(
        database.get_setting("reveal_archive_channel_id")
        or str(REVEAL_ARCHIVE_CHANNEL_ID)
        or "0"
    )
    saved_booster_role_id = int(
        database.get_setting("booster_role_id") or str(BOOSTER_ROLE_ID) or "0"
    )
except ValueError as exc:
    raise RuntimeError("Saved bot settings in SQLite are invalid.") from exc

FORUM_CHANNEL_ID = saved_forum_id or FORUM_CHANNEL_ID
REVEAL_ARCHIVE_CHANNEL_ID = saved_reveal_archive_channel_id
BOOSTER_ROLE_ID = saved_booster_role_id
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
log_manager.booster_role_id = BOOSTER_ROLE_ID
log_manager.reveal_archive_channel_id = REVEAL_ARCHIVE_CHANNEL_ID


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
CURRENT_REVEAL: dict | None = None
PERSISTED_REVEALS: dict[str, dict] = {}
_history_hydrated = False
_active_reveal_ids: set[str] = set()
_reveal_restore_locks: dict[str, asyncio.Lock] = {}
_reveal_queue: list[dict] = []
_reveal_queue_lock: asyncio.Lock | None = None
_recent_build_seconds: dict[str, list[float]] = {"image": [], "video": []}
REVEAL_CACHE_DIR = CACHE_DIR / "trace-originals"


def _watermark_key() -> bytes:
    secret = WATERMARK_SECRET
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


def _history_to_metadata(record: dict) -> dict:
    filename = record.get("archive_filename") or record.get("filename") or (
        "original.mp4" if record.get("kind") == "video" else "original.jpg"
    )
    suffix = Path(filename).suffix.lower()
    if suffix not in IMAGE_EXTS | VIDEO_EXTS:
        suffix = ".mp4" if record.get("kind") == "video" else ".jpg"
    local_path = REVEALS_DIR / str(record["reveal_id"]) / f"original{suffix}"
    return {**record, "path": str(local_path), "filename": record.get("filename") or filename}


async def hydrate_reveal_history() -> None:
    global CURRENT_REVEAL, PERSISTED_REVEALS, _history_hydrated
    if not reveal_history_store.enabled:
        logger.warning("REVEAL_ARCHIVE_CHANNEL_ID is not configured in settings.py; archive-backed reveal features are disabled.")
        _history_hydrated = True
        return
    rows = await reveal_history_store.records(force_refresh=True)
    PERSISTED_REVEALS = {row["reveal_id"]: _history_to_metadata(row) for row in rows}
    CURRENT_REVEAL = PERSISTED_REVEALS[rows[0]["reveal_id"]] if rows else None
    if CURRENT_REVEAL:
        tracker = reveal_status_trackers.get(CURRENT_REVEAL["reveal_id"])
        if tracker is None:
            tracker = StatusTracker()
            tracker.reset(CURRENT_REVEAL["reveal_id"])
            reveal_status_trackers[CURRENT_REVEAL["reveal_id"]] = tracker
    _history_hydrated = True


def _get_reveal_restore_lock(reveal_id: str) -> asyncio.Lock:
    lock = _reveal_restore_locks.get(str(reveal_id))
    if lock is None:
        lock = asyncio.Lock()
        _reveal_restore_locks[str(reveal_id)] = lock
    return lock


async def ensure_reveal_source(reveal: dict) -> dict:
    source = Path(str(reveal.get("path", "")))
    if source.is_file() and source.stat().st_size > 0:
        return reveal
    if not reveal_history_store.enabled:
        raise RevealHistoryError("The original is not available locally and archive persistence is disabled.")
    filename = reveal.get("archive_filename") or reveal.get("filename") or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in IMAGE_EXTS | VIDEO_EXTS:
        suffix = ".mp4" if reveal.get("kind") == "video" else ".jpg"
    destination = REVEALS_DIR / str(reveal["reveal_id"]) / f"original{suffix}"
    async with _get_reveal_restore_lock(str(reveal["reveal_id"])):
        if destination.is_file() and destination.stat().st_size > 0:
            restored = {**reveal, "path": str(destination)}
            PERSISTED_REVEALS[str(reveal["reveal_id"])] = restored
            return restored
        await reveal_history_store.ensure_local_copy(reveal, destination, max_bytes=MAX_UPLOAD_BYTES)
    restored = {**reveal, "path": str(destination), "filename": reveal.get("filename") or destination.name}
    PERSISTED_REVEALS[str(reveal["reveal_id"])] = restored
    return restored


async def cleanup_deleted_reveal(reveal_id: str) -> None:
    global CURRENT_REVEAL
    PERSISTED_REVEALS.pop(reveal_id, None)
    reveal_status_trackers.pop(reveal_id, None)
    shutil.rmtree(REVEALS_DIR / reveal_id, ignore_errors=True)
    shutil.rmtree(CACHE_DIR / reveal_id, ignore_errors=True)
    try:
        safe_prefix = _safe_filename(reveal_id, "reveal") + "_"
        for cached_original in REVEAL_CACHE_DIR.glob(safe_prefix + "*"):
            cached_original.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove trace cache for reveal %s", reveal_id, exc_info=True)
    if CURRENT_REVEAL and CURRENT_REVEAL.get("reveal_id") == reveal_id:
        latest = await reveal_history_store.latest()
        CURRENT_REVEAL = _history_to_metadata(latest) if latest else None
        if CURRENT_REVEAL:
            PERSISTED_REVEALS[CURRENT_REVEAL["reveal_id"]] = CURRENT_REVEAL


async def before_delete_reveal(reveal_id: str) -> None:
    if reveal_id in _active_reveal_ids:
        raise RevealHistoryError("That reveal is in use right now. Try again after the operation finishes.")


async def resolve_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(int(user_id))
    if member is not None:
        return member
    try:
        return await guild.fetch_member(int(user_id))
    except discord.HTTPException:
        return None


def member_has_role(member: discord.Member, role_id: int) -> bool:
    return bool(role_id) and any(role.id == int(role_id) for role in member.roles)


def member_is_booster(member: discord.Member) -> bool:
    return member_has_role(member, BOOSTER_ROLE_ID)


def compute_video_target_bytes(upload_limit: int | None) -> int:
    configured = int(max(1.0, VIDEO_TARGET_MAX_MB) * 1048576)
    if not upload_limit or upload_limit <= 0:
        return configured
    margin = min(512 * 1024, max(128 * 1024, int(upload_limit * 0.025)))
    return min(configured, max(256 * 1024, int(upload_limit) - margin))


async def _stream_to_file(response: aiohttp.ClientResponse, destination: Path, limit: int) -> int:
    written = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        async for chunk in response.content.iter_chunked(1 << 20):
            written += len(chunk)
            if written > limit:
                raise ValueError(f"The file exceeds the {limit / 1048576:.0f} MB limit.")
            handle.write(chunk)
    return written


class PublicOnlyResolver(aiohttp.abc.AbstractResolver):
    def __init__(self) -> None:
        self._inner = aiohttp.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        infos = await self._inner.resolve(host, port, family)
        if not infos or any(
            not ipaddress.ip_address(item["host"].split("%", 1)[0]).is_global
            for item in infos
        ):
            raise OSError(f"blocked non-public address for {host}")
        return infos

    async def close(self) -> None:
        await self._inner.close()


def _host_allowed(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]").split("%", 1)[0]).is_global
    except ValueError:
        return True


async def download_media_url(url: str, limit: int = MAX_UPLOAD_BYTES) -> tuple[Path, str, str, str]:
    current = url.strip().strip("<>")
    if not URL_RE.fullmatch(current):
        raise ValueError("Send one direct http(s) media URL.")
    timeout = aiohttp.ClientTimeout(total=180, connect=15, sock_read=45)
    connector = aiohttp.TCPConnector(
        limit=2, limit_per_host=1, ttl_dns_cache=30, resolver=PublicOnlyResolver()
    )
    destination: Path | None = None
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={"User-Agent": HTTP_USER_AGENT},
        ) as session:
            for _ in range(5):
                parsed = urlparse(current)
                if (
                    parsed.scheme.lower() not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.port not in (None, 80, 443)
                    or not _host_allowed(parsed.hostname)
                ):
                    raise ValueError("That media URL is not allowed.")
                async with session.get(current, allow_redirects=False) as response:
                    if 300 <= response.status < 400 and response.headers.get("Location"):
                        current = urljoin(current, response.headers["Location"])
                        continue
                    if response.status != 200:
                        raise ValueError(f"The media URL returned HTTP {response.status}.")
                    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                    try:
                        advertised_size = int(response.headers.get("Content-Length", "0") or 0)
                    except ValueError:
                        advertised_size = 0
                    if advertised_size > limit:
                        raise ValueError(f"That URL points to {advertised_size / 1048576:.1f} MB; the limit is {limit / 1048576:.1f} MB.")
                    suffix = Path(parsed.path).suffix.lower()
                    if suffix not in IMAGE_EXTS | VIDEO_EXTS:
                        suffix = mimetypes.guess_extension(content_type) or ""
                    proposed = f"download{suffix or '.bin'}"
                    allowed, kind, normalized_ext = is_allowed_media(proposed, content_type)
                    if not allowed:
                        raise ValueError("The URL does not point to a supported image or video.")
                    destination = TMP_DIR / f"url_{secrets.token_hex(8)}{normalized_ext}"
                    await _stream_to_file(response, destination, limit)
                    return destination, kind, normalized_ext, Path(parsed.path).name or proposed
        raise ValueError("Too many redirects.")
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        if destination:
            destination.unlink(missing_ok=True)
        if "blocked non-public address" in str(exc):
            raise ValueError("That media URL is not allowed.") from exc
        raise ValueError(f"I couldn't download that media URL ({type(exc).__name__}).") from exc
    except BaseException:
        if destination:
            destination.unlink(missing_ok=True)
        raise


def is_allowed_media(filename: str, content_type: str | None) -> tuple[bool, str | None, str | None]:
    extension = Path(filename).suffix.lower()
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if extension in IMAGE_EXTS or (media_type.startswith("image/") and media_type != "image/gif"):
        return True, "image", extension if extension in IMAGE_EXTS else ".png"
    if extension in VIDEO_EXTS or media_type.startswith("video/"):
        return True, "video", extension if extension in VIDEO_EXTS else ".mp4"
    return False, None, None


async def _store_new_reveal(source: Path, filename: str, kind: str, extension: str) -> dict:
    global CURRENT_REVEAL, latest_reveal_status_id
    if not reveal_history_store.enabled:
        raise RevealHistoryError("Configure REVEAL_ARCHIVE_CHANNEL_ID in settings.py before uploading reveals.")
    reveal_id = f"{int(time.time())}_{secrets.token_hex(4)}"
    directory = REVEALS_DIR / reveal_id
    suffix = extension if extension in IMAGE_EXTS | VIDEO_EXTS else (".mp4" if kind == "video" else ".jpg")
    destination = directory / f"original{suffix}"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(source), str(destination))
        info: dict = {}
        if kind == "image":
            with Image.open(destination) as image:
                width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise ValueError(f"That image is {width * height / 1e6:.0f} MP; the limit is {MAX_IMAGE_PIXELS / 1e6:.0f} MP.")
            with Image.open(destination) as probe:
                probe.verify()
            info = {"width": width, "height": height, "fps": 0.0, "duration": 0.0}
        else:
            info = await asyncio.to_thread(wm.video_info, destination, MAX_VIDEO_HEIGHT)
            if float(info.get("duration", 0.0)) > MAX_VIDEO_SECONDS:
                info["truncated_to"] = MAX_VIDEO_SECONDS
        record = {
            "reveal_id": reveal_id,
            "kind": kind,
            "path": str(destination),
            "filename": filename,
            "created_at": int(time.time()),
            **info,
        }
        archived = await reveal_history_store.archive(record)
        record = {**record, **archived, "path": str(destination), "filename": filename}
        safe_json_write(directory / "metadata.json", record)
        PERSISTED_REVEALS[reveal_id] = record
        CURRENT_REVEAL = record
        latest_reveal_status_id = reveal_id
        tracker = StatusTracker()
        tracker.reset(reveal_id)
        reveal_status_trackers[reveal_id] = tracker
        await asyncio.to_thread(prune_old_data, reveal_id)
        return record
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def safe_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with temporary.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prune_old_data(keep_reveal_id: str) -> None:
    try:
        cache_children = list(CACHE_DIR.iterdir())
    except OSError:
        cache_children = []
    for child in cache_children:
        if not child.is_dir() or child.name in {keep_reveal_id, "trace-originals"}:
            continue
        if child.name in _active_reveal_ids:
            continue
        shutil.rmtree(child, ignore_errors=True)

    if AUTO_DELETE_OLD_ORIGINALS:
        try:
            reveal_children = list(REVEALS_DIR.iterdir())
        except OSError:
            reveal_children = []
        for directory in reveal_children:
            if not directory.is_dir() or directory.name == keep_reveal_id:
                continue
            if directory.name not in _active_reveal_ids:
                shutil.rmtree(directory, ignore_errors=True)

    cutoff = time.time() - 3600
    try:
        leftovers = list(TMP_DIR.iterdir())
    except OSError:
        leftovers = []
    for leftover in leftovers:
        try:
            if leftover.is_file() and leftover.stat().st_mtime < cutoff:
                leftover.unlink(missing_ok=True)
        except OSError:
            pass


def enforce_cache_limit(protect: Path | None = None) -> None:
    now = time.time()
    files: list[tuple[float, int, Path]] = []
    total = 0
    try:
        for path in CACHE_DIR.glob("*/*"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if not path.is_file():
                continue
            if path.name.startswith(".") and now - stat.st_mtime > 3600:
                path.unlink(missing_ok=True)
                continue
            if now - stat.st_mtime > CACHE_TTL_SECONDS and path != protect:
                path.unlink(missing_ok=True)
                continue
            total += stat.st_size
            files.append((stat.st_mtime, stat.st_size, path))
    except OSError:
        return
    if CACHE_MAX_BYTES <= 0 or total <= CACHE_MAX_BYTES:
        return
    files.sort(key=lambda item: item[0])
    for _, size, path in files:
        if total <= CACHE_MAX_BYTES:
            break
        if protect is not None and path == protect:
            continue
        try:
            path.unlink(missing_ok=True)
            total -= size
        except OSError:
            pass


def startup_cleanup() -> None:
    try:
        for leftover in TMP_DIR.iterdir():
            if leftover.is_file():
                leftover.unlink(missing_ok=True)
    except OSError:
        pass
    enforce_cache_limit()


async def _save_uploaded_attachment(attachment: discord.Attachment, upload_limit: int) -> dict:
    allowed, kind, extension = is_allowed_media(attachment.filename, attachment.content_type)
    if not allowed or kind is None or extension is None:
        raise ValueError("Please send a supported JPG/PNG/WEBP image or MP4/MOV/WEBM/MKV/AVI/M4V video.")
    if attachment.size > upload_limit:
        raise ValueError(f"That file is {attachment.size / 1048576:.1f} MB; the limit is {upload_limit / 1048576:.1f} MB.")
    temporary = TMP_DIR / f"upload_{secrets.token_hex(8)}{extension}"
    try:
        timeout = aiohttp.ClientTimeout(total=900, connect=15, sock_read=60)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": HTTP_USER_AGENT}) as session:
            async with session.get(attachment.url) as response:
                if response.status != 200:
                    raise ValueError(f"Discord returned HTTP {response.status} for that attachment.")
                await _stream_to_file(response, temporary, upload_limit)
        return await _store_new_reveal(temporary, attachment.filename, kind, extension)
    finally:
        temporary.unlink(missing_ok=True)


async def _save_reveal_from_url(url: str, upload_limit: int) -> dict:
    path: Path | None = None
    try:
        path, kind, extension, filename = await download_media_url(
            url, min(MAX_URL_BYTES, upload_limit)
        )
        return await _store_new_reveal(path, filename, kind, extension)
    finally:
        if path:
            path.unlink(missing_ok=True)


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
    archive_view = getattr(bot, "archive_served_users_view", None)
    if archive_view is not None:
        archive_view.staff_role_id = int(staff_role_id)

    if log_manager.auto_log_enabled and log_manager.role_id:
        log_manager.schedule_auto_reconcile(force=True)


def apply_creation_settings(prompt_channel_id: int, approve_channel_id: int) -> None:
    database.set_setting("creation_prompt_channel_id", str(int(prompt_channel_id)))
    database.set_setting("creation_approve_channel_id", str(int(approve_channel_id)))
    log_manager.creation_prompt_channel_id = int(prompt_channel_id)
    log_manager.creation_approve_channel_id = int(approve_channel_id)


def apply_reveal_settings(
    archive_channel_id: int,
    booster_role_id: int,
    guild_id: int,
) -> None:
    """Persist and apply reveal settings changed from the owner-only settings panel."""
    global REVEAL_ARCHIVE_CHANNEL_ID, BOOSTER_ROLE_ID, _history_hydrated

    database.set_setting("reveal_archive_channel_id", str(int(archive_channel_id)))
    database.set_setting("booster_role_id", str(int(booster_role_id)))
    # Record the same server as the rest of the bot configuration.
    database.set_setting("guild_id", str(int(guild_id)))

    REVEAL_ARCHIVE_CHANNEL_ID = int(archive_channel_id)
    BOOSTER_ROLE_ID = int(booster_role_id)
    log_manager.reveal_archive_channel_id = int(archive_channel_id)
    log_manager.booster_role_id = int(booster_role_id)
    log_manager.guild_id = int(guild_id)
    reveal_history_store.archive_channel_id = int(archive_channel_id)

    # Reload archive state after changing its channel. This also enables features
    # immediately when an archive is configured after the bot has already started.
    _history_hydrated = False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(hydrate_reveal_history(), name="hydrate-reveal-history-after-settings")


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
            content="Reveal delivery is disabled: configure `REVEAL_ARCHIVE_CHANNEL_ID` in `settings.py` first."
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
    _active_reveal_ids.add(reveal_id)
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
        _active_reveal_ids.discard(reveal_id)
        active_reveal_jobs = max(0, active_reveal_jobs - 1)


@app_commands.command(
    name="upload",
    description="Admin: upload an image/video reveal.",
)
@app_commands.guild_only()
@app_commands.describe(
    file="The clean reveal image/video",
    url="A direct http(s) media URL (use this instead of file)",
)
async def upload(
    interaction: discord.Interaction,
    file: discord.Attachment | None = None,
    url: str | None = None,
) -> None:
    if (file is None) == (url is None):
        await interaction.response.send_message(
            "Provide exactly one of `file` or `url`.", ephemeral=True
        )
        return
    upload_limit = min(
        MAX_UPLOAD_BYTES,
        int(getattr(interaction, "filesize_limit", 0) or MAX_UPLOAD_BYTES),
    )
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if file is not None:
            metadata = await _save_uploaded_attachment(file, upload_limit)
        else:
            metadata = await _save_reveal_from_url(str(url or ""), upload_limit)
        note = ""
        if metadata.get("truncated_to"):
            note = (
                f" The source is {float(metadata.get('duration', 0.0)):.0f}s; "
                f"viewers receive the first {int(metadata['truncated_to'])}s."
            )
        await interaction.followup.send(
            f"Reveal `{metadata['reveal_id']}` is live ({metadata['kind']}). "
            f"Eligible boosters can use `/view_reveal`." + note,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (ValueError, RevealHistoryError) as exc:
        await interaction.followup.send(f"Reveal upload failed: {exc}", ephemeral=True)
    except Exception as exc:
        logger.exception("Reveal upload failed")
        await interaction.followup.send(
            f"I couldn't ingest/archive that media ({type(exc).__name__}). Check the bot logs.",
            ephemeral=True,
        )


def _components_v2_available() -> bool:
    return (
        hasattr(discord.ui, "LayoutView")
        and hasattr(discord.ui, "MediaGallery")
        and hasattr(discord.ui, "TextDisplay")
        and hasattr(discord, "MediaGalleryItem")
    )


def _build_media_gallery_view(filename: str, content: str):
    view = discord.ui.LayoutView(timeout=15 * 60)
    view.add_item(discord.ui.TextDisplay(content))
    gallery = discord.ui.MediaGallery()
    gallery.add_item(
        media=f"attachment://{filename}",
        description="Your private personalized reveal",
        spoiler=False,
    )
    view.add_item(gallery)
    return view


def _get_reveal_queue_lock() -> asyncio.Lock:
    global _reveal_queue_lock
    if _reveal_queue_lock is None:
        _reveal_queue_lock = asyncio.Lock()
    return _reveal_queue_lock


def _estimated_build_seconds(kind: str) -> float:
    values = _recent_build_seconds.get(kind, [])
    if values:
        return max(0.5, min(60.0, sum(values) / len(values)))
    return 1.0 if kind == "image" else 8.0


def _queue_position(ticket: dict) -> int:
    ahead = 0
    for item in _reveal_queue:
        if item is ticket:
            break
        if item.get("state") in {"queued", "running"}:
            ahead += 1
    return ahead + 1


def _queue_percent(ticket: dict, position: int) -> int:
    if ticket.get("state") == "running" or position <= 1:
        return 90
    initial = max(1, int(ticket.get("initial_ahead", 0)))
    completed_ahead = max(0, initial - max(0, position - 1))
    return max(1, min(89, 5 + round(84 * completed_ahead / initial)))


async def _send_queue_progress(
    callback,
    percent: int,
    position: int,
    estimated_wait: float,
) -> None:
    if callback is None:
        return
    try:
        await callback(int(percent), int(position), float(estimated_wait))
    except TypeError:
        await callback(int(percent))


async def _build_personalized_reveal(
    reveal: dict,
    user_id: int,
    target_bytes: int | None,
    progress_callback=None,
) -> tuple[Path, dict | None]:
    reveal = await ensure_reveal_source(reveal)
    source = Path(reveal["path"])
    if not source.is_file():
        raise RuntimeError("There is currently no valid reveal source.")
    key = _watermark_key()
    suffix = ".mp4" if reveal["kind"] == "video" else ".jpg"
    version = VIDEO_CACHE_VERSION if reveal["kind"] == "video" else IMAGE_CACHE_VERSION
    cache_key = f"user_{int(user_id)}_{version}_{int(target_bytes or 0)}b"
    destination = CACHE_DIR / str(reveal["reveal_id"]) / f"{cache_key}{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size > 0:
        try:
            destination.touch()
        except OSError:
            pass
        return destination, None

    temporary = destination.with_name(
        f".{destination.stem}.tmp{secrets.token_hex(4)}{destination.suffix}"
    )
    ticket = {
        "reveal_id": str(reveal["reveal_id"]),
        "user_id": int(user_id),
        "kind": str(reveal["kind"]),
        "queued_at": time.perf_counter(),
        "state": "queued",
        "initial_ahead": 0,
    }
    queue_lock = _get_reveal_queue_lock()
    async with queue_lock:
        ticket["initial_ahead"] = sum(
            1 for item in _reveal_queue if item.get("state") in {"queued", "running"}
        )
        _reveal_queue.append(ticket)
    _active_reveal_ids.add(str(reveal["reveal_id"]))
    semaphore = _get_reveal_build_semaphore()
    acquire_task = asyncio.create_task(semaphore.acquire())
    acquired = False
    last_percent = -1
    last_update = 0.0
    started = 0.0
    try:
        while not acquire_task.done():
            async with queue_lock:
                position = _queue_position(ticket)
                percent = _queue_percent(ticket, position)
            now = time.perf_counter()
            if progress_callback is not None and now - last_update >= 0.8 and percent != last_percent:
                estimated = 0.0 if ticket["state"] == "running" else position * _estimated_build_seconds(ticket["kind"])
                await _send_queue_progress(progress_callback, percent, position, estimated)
                last_percent = percent
                last_update = now
            await asyncio.sleep(0.3)
        await acquire_task
        acquired = True
        async with queue_lock:
            ticket["state"] = "running"
        if progress_callback is not None:
            await _send_queue_progress(progress_callback, 90, 1, 0.0)
        started = time.perf_counter()

        if reveal["kind"] == "image":
            info = await asyncio.to_thread(
                wm.render_image_delivery,
                source,
                temporary,
                int(user_id),
                key,
                str(reveal["reveal_id"]),
                amp=IMAGE_AMP,
                target_bytes=target_bytes,
            )
        elif reveal["kind"] == "video":
            info = await asyncio.to_thread(
                wm.embed_video,
                source,
                temporary,
                int(user_id),
                key,
                str(reveal["reveal_id"]),
                amp=VIDEO_AMP,
                preset=VIDEO_PRESET,
                max_seconds=MAX_VIDEO_SECONDS,
                max_height=MAX_VIDEO_HEIGHT,
                target_bytes=target_bytes,
                audio_kbps=VIDEO_AUDIO_KBPS,
            )
        else:
            raise RuntimeError(f"Unsupported reveal kind: {reveal['kind']}")
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise RuntimeError("The watermark encoder produced an empty file.")
        temporary.replace(destination)
        if started:
            values = _recent_build_seconds.setdefault(str(reveal["kind"]), [])
            values.append(time.perf_counter() - started)
            del values[:-20]
        await asyncio.to_thread(enforce_cache_limit, destination)
        return destination, info
    finally:
        temporary.unlink(missing_ok=True)
        if not acquire_task.done():
            acquire_task.cancel()
            try:
                await acquire_task
            except asyncio.CancelledError:
                pass
        if acquired:
            semaphore.release()
        async with queue_lock:
            try:
                _reveal_queue.remove(ticket)
            except ValueError:
                pass
        _active_reveal_ids.discard(str(reveal["reveal_id"]))


_reveal_build_semaphore: asyncio.Semaphore | None = None


def _get_reveal_build_semaphore() -> asyncio.Semaphore:
    global _reveal_build_semaphore
    if _reveal_build_semaphore is None:
        _reveal_build_semaphore = asyncio.Semaphore(
            max(1, int(_setting("MAX_CONCURRENT_JOBS", 1) or 1))
        )
    return _reveal_build_semaphore


async def send_personalized_reveal(interaction: discord.Interaction) -> None:
    global latest_reveal_status_id, active_reveal_jobs
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if member is None:
        await interaction.response.send_message(
            "I couldn't verify your server membership.", ephemeral=True
        )
        return
    if not BOOSTER_ROLE_ID:
        await interaction.response.send_message(
            "Booster reveal access is not configured in `settings.py` yet.", ephemeral=True
        )
        return
    if not member_is_booster(member):
        await interaction.response.send_message(
            "This reveal is available to current server boosters only.", ephemeral=True
        )
        return
    reveal = CURRENT_REVEAL
    if not reveal:
        await interaction.response.send_message(
            "There isn't a reveal uploaded right now.", ephemeral=True
        )
        return

    upload_limit = (
        getattr(interaction, "filesize_limit", None)
        or getattr(interaction, "attachment_size_limit", None)
        or (getattr(interaction.guild, "filesize_limit", None) if interaction.guild else None)
        or DEFAULT_UPLOAD_LIMIT
    )
    target_bytes = compute_video_target_bytes(upload_limit) if reveal["kind"] == "video" else None
    tracker = reveal_status_trackers.get(str(reveal["reveal_id"]))
    if tracker is None:
        tracker = StatusTracker()
        tracker.reset(str(reveal["reveal_id"]))
        reveal_status_trackers[str(reveal["reveal_id"])] = tracker
    tracker.request(member.id)
    latest_reveal_status_id = str(reveal["reveal_id"])
    active_reveal_jobs += 1
    await interaction.response.defer(ephemeral=True, thinking=True)
    started = time.perf_counter()
    last_progress_edit = 0.0

    async def reveal_progress(
        percent: int, position: int = 1, estimated_wait: float = 0.0
    ) -> None:
        nonlocal last_progress_edit
        now = time.perf_counter()
        if percent < 90 and now - last_progress_edit < 0.9:
            return
        last_progress_edit = now
        if estimated_wait > 0.5 and int(position) > 1:
            wait_text = f"Queue: {int(position)} • Estimated wait: {max(1, round(estimated_wait))}s"
        elif percent >= 90:
            wait_text = "Generating your personalized reveal now"
        else:
            wait_text = "Joining the reveal queue"
        await interaction.edit_original_response(
            content=f"Reveal loading: {int(percent)}% • {wait_text}",
            attachments=[],
            view=None,
        )

    try:
        path, info = await _build_personalized_reveal(
            reveal, member.id, target_bytes, progress_callback=reveal_progress
        )
        size = path.stat().st_size
        if size <= 0:
            raise RuntimeError("The generated reveal file is empty.")
        if size > upload_limit:
            raise RuntimeError(
                f"The personalized file is {size / 1048576:.1f} MB, above this server's "
                f"{upload_limit / 1048576:.1f} MB attachment limit."
            )

        filename = "reveal.mp4" if reveal["kind"] == "video" else "reveal.jpg"
        content = (
            "Do not share this video with anyone else; doing so could result in moderation."
            if reveal["kind"] == "video"
            else "Do not share this image with anyone else; doing so could result in moderation."
        )
        delivered = False
        if _components_v2_available():
            out_file = discord.File(path, filename=filename, spoiler=False)
            try:
                await interaction.edit_original_response(
                    content=None,
                    attachments=[out_file],
                    view=_build_media_gallery_view(filename, content),
                )
                delivered = True
            except (discord.HTTPException, TypeError, AttributeError, OSError):
                logger.debug("Components V2 reveal delivery failed; trying attachment fallback", exc_info=True)
            finally:
                out_file.close()
        if not delivered:
            out_file = discord.File(path, filename=filename, spoiler=False)
            try:
                await interaction.edit_original_response(
                    content=content,
                    attachments=[out_file],
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                delivered = True
            finally:
                out_file.close()
        if not delivered:
            raise RuntimeError("Discord did not accept the reveal attachment.")

        profile = None
        if reveal["kind"] == "video":
            if info and info.get("height") and info.get("fps"):
                profile = (int(info["height"]), round(float(info["fps"]), 3))
            else:
                try:
                    profile_info = await asyncio.to_thread(wm.video_info, path, MAX_VIDEO_HEIGHT)
                    profile = (int(profile_info["height"]), round(float(profile_info["fps"]), 3))
                except Exception:
                    logger.debug("Could not probe delivered video profile", exc_info=True)
        await reveal_history_store.record_delivery(str(reveal["reveal_id"]), member.id, profile)
        tracker.success(member.id)
        refreshed = await reveal_history_store.get(str(reveal["reveal_id"]))
        if refreshed:
            PERSISTED_REVEALS[str(reveal["reveal_id"])] = _history_to_metadata(refreshed)
        logger.info(
            "Delivered personalized reveal=%s user=%s kind=%s size=%d cached=%s elapsed=%.1fs",
            reveal["reveal_id"], member.id, reveal["kind"], size, info is None,
            time.perf_counter() - started,
        )
    except discord.Forbidden:
        await _fail_response(interaction, "I couldn't deliver that reveal. Check Discord permissions and the member's DM/interaction settings.")
    except Exception as exc:
        logger.exception("Failed to build/deliver personalized reveal for user=%s", member.id)
        await _fail_response(interaction, f"I couldn't generate your personalized reveal ({type(exc).__name__}). Please try again.")
    finally:
        active_reveal_jobs = max(0, active_reveal_jobs - 1)


async def _fail_response(interaction: discord.Interaction, text: str) -> None:
    try:
        await interaction.edit_original_response(content=text, attachments=[], view=None)
    except discord.HTTPException:
        logger.debug("Could not report reveal failure to user=%s", interaction.user.id, exc_info=True)


class RevealButtonView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="View Reveal",
        style=discord.ButtonStyle.primary,
        emoji="👁️",
        custom_id="revealbot:view-reveal:v1",
    )
    async def view_reveal_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await send_personalized_reveal(interaction)


@app_commands.command(name="view_reveal", description="Get your booster reveal.")
@app_commands.guild_only()
async def view_reveal(interaction: discord.Interaction) -> None:
    await send_personalized_reveal(interaction)


@app_commands.context_menu(name="Add Reveal Button")
async def add_reveal_button(
    interaction: discord.Interaction, message: discord.Message
) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This app can only be used in a server.", ephemeral=True
        )
        return
    try:
        await message.reply(
            content="Booster reveal — use the button below to open the current reveal.",
            view=RevealButtonView(),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        logger.exception("Could not post reveal button reply to message %s", message.id)
        await interaction.response.send_message(
            "I couldn't post the reveal button. Check channel permissions.", ephemeral=True
        )
        return
    await interaction.response.send_message("Posted a View Reveal button.", ephemeral=True)


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
            content="Tracing is disabled: configure `REVEAL_ARCHIVE_CHANNEL_ID` in `settings.py` first."
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
    name="trace",
    description="Admin: trace a leaked reveal against archived originals.",
)
@app_commands.guild_only()
@app_commands.describe(
    file="Leaked image/video, or a frame from a video reveal",
    reveal_id="Optional reveal ID; omit to search the newest archived reveals",
    start_frame="Optional source frame hint for video tracing",
)
async def trace(
    interaction: discord.Interaction,
    file: discord.Attachment,
    reveal_id: str | None = None,
    start_frame: app_commands.Range[int, 0, 200000] = 0,
) -> None:
    if file.size > MAX_UPLOAD_BYTES:
        await interaction.response.send_message(
            f"That leak is {file.size / 1048576:.1f} MB; the limit is {MAX_UPLOAD_BYTES / 1048576:.1f} MB.",
            ephemeral=True,
        )
        return
    allowed, leak_kind, extension = is_allowed_media(file.filename, file.content_type)
    if not allowed or leak_kind is None or extension is None:
        await interaction.response.send_message(
            "The leak must be a supported image or video.", ephemeral=True
        )
        return
    if not reveal_history_store.enabled:
        await interaction.response.send_message(
            "Tracing is unavailable because reveal archive storage is not configured in settings.py.",
            ephemeral=True,
        )
        return
    try:
        key = _watermark_key()
    except RuntimeError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    leak_path = TMP_DIR / f"leak_{secrets.token_hex(8)}{extension}"
    checked = 0
    deadline = time.monotonic() + TRACE_TIME_BUDGET
    try:
        await file.save(leak_path)
        if leak_kind == "image":
            try:
                with Image.open(leak_path) as image:
                    leak_width, leak_height = image.size
                    image.verify()
            except Exception as exc:
                await interaction.edit_original_response(
                    content=f"I couldn't read the leak as an image ({type(exc).__name__})."
                )
                return
            if leak_width * leak_height > TRACE_LEAK_MAX_PIXELS:
                await interaction.edit_original_response(
                    content="That leak is too large for tracing here. Downscale it first and try again."
                )
                return

        if reveal_id:
            record = await reveal_history_store.get(reveal_id.strip(), force_refresh=True)
            candidates = [record] if record else []
        else:
            candidates = await reveal_history_store.records(force_refresh=True)
            candidates = candidates[:TRACE_MAX_REVEALS]
        candidates = [row for row in candidates if row]
        if not candidates:
            await interaction.edit_original_response(
                content="I couldn't find an archived reveal matching that ID." if reveal_id else "There are no archived reveals to trace."
            )
            return

        for row in candidates:
            if time.monotonic() >= deadline:
                break
            rid = str(row["reveal_id"])
            _active_reveal_ids.add(rid)
            try:
                try:
                    record = await ensure_reveal_source(_history_to_metadata(row))
                except Exception:
                    logger.exception("Could not restore reveal %s for tracing", rid)
                    continue
                if record.get("kind") == "image" and leak_kind != "image":
                    continue
                if record.get("kind") == "video" and leak_kind not in {"image", "video"}:
                    continue
                selected_kind = _infer_trace_kind(Path(record["path"]), leak_path, "auto")
                stored_profiles = [
                    (int(profile[0]), round(float(profile[1]), 3))
                    for profile in row.get("profiles", [])
                    if len(profile) >= 2 and float(profile[1]) > 0
                ]
                attempts: list[tuple[int | None, float | None]] = []
                if record.get("kind") == "video":
                    attempts.extend((height, fps) for height, fps in stored_profiles[:4])
                attempts.append((None, None))
                seen_attempts: set[tuple[int | None, float | None]] = set()
                for delivery_height, delivery_fps in attempts:
                    if time.monotonic() >= deadline:
                        break
                    profile = (delivery_height, delivery_fps)
                    if profile in seen_attempts:
                        continue
                    seen_attempts.add(profile)
                    if record.get("kind") == "image":
                        budget_cap = TRACE_IMAGE_TIME_BUDGET
                    else:
                        budget_cap = TRACE_VIDEO_TIME_BUDGET
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    budget = min(budget_cap, remaining)
                    kwargs = {
                        "max_height": delivery_height or MAX_VIDEO_HEIGHT,
                        "start_frame": int(start_frame),
                        "delivery_height": delivery_height,
                        "delivery_fps": delivery_fps,
                        "time_budget": float(budget),
                    }
                    checked += 1
                    try:
                        result = await asyncio.to_thread(
                            wm.extract,
                            Path(record["path"]),
                            leak_path,
                            selected_kind,
                            key,
                            rid,
                            **kwargs,
                        )
                    except Exception as exc:
                        logger.info(
                            "Trace candidate failed for reveal=%s profile=%s: %s", rid, profile, exc
                        )
                        continue
                    if not bool(result.get("valid", result.get("ok", False))):
                        continue

                    uid = int(result.get("user_id", 0) or 0)
                    served_ids = {int(value) for value in row.get("served_user_ids", [])}
                    served = uid in served_ids
                    member = await resolve_member(interaction.guild, uid) if interaction.guild else None
                    status_line = (
                        "The archive confirms this account received the reveal."
                        if served
                        else "The decoded ID is not listed among the recorded recipients; treat attribution as unverified."
                    )
                    frame_text = ""
                    if "time" in result:
                        frame_text = f"\nMatched source time: `{float(result['time']):.2f}s` (frame `{int(result.get('frame', 0))}`)."
                    await interaction.edit_original_response(
                        content=(
                            f"Watermark decoded (CRC verified): <@{uid}> (`{uid}`)"
                            f"{' — currently in this server' if member else ' — not currently in this server'}\n"
                            f"Reveal `{rid}` • media type `{selected_kind}`\n{status_line}\n"
                            f"Trace metric: `{float(result.get('metric', 0.0)):.4f}`{frame_text}"
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    logger.warning(
                        "TRACE by %s: reveal=%s decoded_user=%s served=%s checked=%d profile=%s",
                        interaction.user.id, rid, uid, served, checked, profile,
                    )
                    return
            finally:
                _active_reveal_ids.discard(rid)

        timed_out = time.monotonic() >= deadline
        timeout_note = " The trace time budget was reached before all candidates were checked." if timed_out else ""
        await interaction.edit_original_response(
            content=(
                f"No CRC-valid watermark was found across {checked} trace candidate(s). "
                "The leak may be cropped, edited, compressed, or from a reveal whose watermark cannot be recovered."
                + timeout_note
            )
        )
    except Exception as exc:
        logger.exception("Trace failed")
        try:
            await interaction.edit_original_response(
                content=f"Trace failed ({type(exc).__name__}). Check the bot logs."
            )
        except discord.HTTPException:
            pass
    finally:
        leak_path.unlink(missing_ok=True)


def _reveal_status_embed(reveal_id: str | None = None) -> discord.Embed:
    chosen_id = reveal_id or latest_reveal_status_id
    tracker = reveal_status_trackers.get(str(chosen_id)) if chosen_id else None
    snapshot = tracker.snapshot() if tracker else StatusTracker().snapshot()
    waiting = sum(1 for ticket in _reveal_queue if ticket.get("state") == "queued")
    running = sum(1 for ticket in _reveal_queue if ticket.get("state") == "running")
    current = PERSISTED_REVEALS.get(str(chosen_id)) if chosen_id else CURRENT_REVEAL
    kind = str((current or CURRENT_REVEAL or {}).get("kind", "video"))
    queue_wait = sum(
        _estimated_build_seconds(str(ticket.get("kind", kind)))
        for ticket in _reveal_queue
        if ticket.get("state") == "queued"
    )
    state = snapshot.bot_state(
        waiting=waiting,
        running=running,
        estimated_wait=queue_wait,
        ready=bot.is_ready(),
    )
    total_active = snapshot.successful_users + waiting + running
    if total_active <= 0:
        progress = "0\n[" + "░" * 20 + "] 0%"
    else:
        ratio = min(1.0, snapshot.successful_users / total_active)
        filled = min(20, int(ratio * 20))
        progress = (
            f"{snapshot.successful_users} / {total_active} completed\n"
            f"[{'█' * filled}{'░' * (20 - filled)}] {ratio * 100:.0f}%"
        )
    embed = discord.Embed(title="Reveal Status")
    embed.add_field(name="Progress", value=progress, inline=False)
    if chosen_id:
        embed.add_field(name="Reveal ID", value=f"`{chosen_id}`", inline=False)
    embed.add_field(name="Generating", value=str(running), inline=True)
    embed.add_field(name="Waiting", value=str(waiting), inline=True)
    embed.add_field(
        name="Estimated wait",
        value=f"~{queue_wait:.0f}s" if waiting else "0s",
        inline=True,
    )
    embed.add_field(name="Status", value=state, inline=True)
    embed.add_field(name="Requested", value=str(snapshot.requested_users), inline=True)
    embed.set_footer(text="Live reveal queue dashboard")
    return embed


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
    chosen_id = (reveal_id or latest_reveal_status_id or "").strip() or None
    if chosen_id and chosen_id not in reveal_status_trackers:
        await interaction.response.send_message(
            f"No in-memory status snapshot is available for reveal `{chosen_id}`. "
            "Status snapshots reset when the bot restarts; archived delivery history remains available.",
            ephemeral=True,
        )
        return
    if not chosen_id and not CURRENT_REVEAL and not reveal_status_trackers:
        await interaction.response.send_message(
            "No reveal delivery has been tracked yet.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        embed=_reveal_status_embed(chosen_id), ephemeral=True
    )
    if not _reveal_queue and active_reveal_jobs <= 0:
        return

    deadline = asyncio.get_running_loop().time() + 14 * 60
    while asyncio.get_running_loop().time() < deadline:
        if not _reveal_queue and active_reveal_jobs <= 0:
            break
        await asyncio.sleep(2.5)
        try:
            await interaction.edit_original_response(embed=_reveal_status_embed(chosen_id))
        except discord.HTTPException:
            return
    try:
        await interaction.edit_original_response(embed=_reveal_status_embed(chosen_id))
    except discord.HTTPException:
        pass


reveal_history_command = make_history_command(
    reveal_history_store,
    cleanup_callback=cleanup_deleted_reveal,
    before_delete_callback=before_delete_reveal,
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
    global _history_hydrated
    if not _history_hydrated:
        try:
            await hydrate_reveal_history()
            logger.info("Hydrated %d persisted reveal(s).", len(PERSISTED_REVEALS))
        except Exception:
            logger.exception("Could not hydrate reveal history")
            _history_hydrated = True

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