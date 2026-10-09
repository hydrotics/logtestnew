from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Optional,
)

import aiohttp


import discord

logger = logging.getLogger("discord-log-bot.create_log")

# Forum-thread creation is a single hot route. Discord documents that some
# per-guild/shared buckets can still return 429s even when the route headers
# appear to have quota, so this bot deliberately serializes forum-create calls
# and learns its pacing from Discord's actual response headers.
MAX_CONCURRENCY = 1
MAX_CONSECUTIVE_FAILURES = 10
THREAD_NAME_LIMIT = 100

# The first request has no response headers yet. Use a conservative first-to-
# second gap, then adapt from Discord's X-RateLimit-* headers. This is not a
# claimed Discord limit; it is only a cold-start safety interval.
FORUM_INITIAL_INTERVAL = 1.25
FORUM_MIN_INTERVAL = 1.0
# Use a small safety buffer around Discord's advertised bucket window.
# Do not cap the calculated interval: a hard maximum can send requests faster
# than the bucket allows and cause discord.py to sleep through Retry-After.
FORUM_SAFETY_FACTOR = 1.02
FORUM_SAFETY_MARGIN = 0.02
FORUM_RETRY_MARGIN = 0.50
MAX_FORUM_429_RETRIES = 5

PROGRESS_BAR_LENGTH = 24
STATUS_ETA_THRESHOLD = 15.0
STATUS_UPDATE_SECONDS = 2.5
ETA_GUESS_PER_ITEM = 1.0
INTERACTION_TOKEN_SECONDS = 14 * 60   # interaction tokens die after 15 minutes

_SENTINEL = object()



# ----------------------------------------------------------------------------
# Forum-create pacing
# ----------------------------------------------------------------------------
class ForumCreatePacer:
    """Single-flight, header-driven scheduler for forum thread creation.

    Discord explicitly recommends consuming the returned rate-limit headers
    rather than hard-coding a route limit. A single request is allowed in
    flight for this route; after each response we derive a conservative launch
    interval from X-RateLimit-Limit and X-RateLimit-Reset-After.

    The scheduler is intentionally separate from discord.py's internal
    ratelimit bucket. discord.py remains responsible for route/global waits and
    retries; this class spaces starts using current bucket headers so we avoid
    spending all advertised quota before its reset.
    """

    def __init__(self, forum_channel_id: int) -> None:
        self.forum_channel_id = int(forum_channel_id)
        # Create the lock lazily inside the running event loop. This keeps the
        # bot compatible with Python versions where asyncio primitives could
        # otherwise bind to the loop that happened to be current at import.
        self._lock: Optional[asyncio.Lock] = None
        self._next_start = time.monotonic() + FORUM_INITIAL_INTERVAL
        self._interval = FORUM_INITIAL_INTERVAL
        self.limit: Optional[int] = None
        self.remaining: Optional[int] = None
        self.reset_after: Optional[float] = None
        self.bucket: Optional[str] = None
        self._last_target_start: Optional[float] = None

        self.trace_config = aiohttp.TraceConfig()
        self.trace_config.on_request_start.append(self._on_request_start)
        self.trace_config.on_request_end.append(self._on_request_end)

    def _is_target_request(self, params: Any) -> bool:
        try:
            if params.method.upper() != "POST":
                return False
            return params.url.path.endswith(
                f"/channels/{self.forum_channel_id}/threads"
            )
        except Exception:
            return False

    async def _on_request_start(self, session: Any, ctx: Any, params: Any) -> None:
        if self._is_target_request(params):
            # Track each actual HTTP attempt, including discord.py's internal
            # retries. The wrapper call's start time is not the real start of a
            # successful attempt if the HTTP client waited through a 429.
            self._last_target_start = time.monotonic()

    async def _on_request_end(self, session: Any, ctx: Any, params: Any) -> None:
        if not self._is_target_request(params):
            return

        try:
            headers = params.response.headers
            self.bucket = headers.get("X-Ratelimit-Bucket", self.bucket)

            limit = self._float_header(headers, "X-Ratelimit-Limit")
            remaining = self._float_header(headers, "X-Ratelimit-Remaining")
            reset_after = self._float_header(headers, "X-Ratelimit-Reset-After")

            if limit is not None and limit > 0:
                self.limit = max(1, int(limit))
            if remaining is not None:
                self.remaining = max(0, int(remaining))
            if reset_after is not None and reset_after >= 0:
                self.reset_after = reset_after

            status_code = getattr(params.response, "status", 0)
            retry_after = self._float_header(headers, "Retry-After")
            scope = headers.get("X-RateLimit-Scope", "unknown")

            # Pace using the *remaining* quota and the time left in this
            # window. Both values fall together as requests are made, so their
            # ratio stays approximately constant. Using reset_after / limit
            # instead makes the interval shrink throughout the window, which
            # can consume the bucket early and trigger Discord's long cooldown.
            #
            # When one token remains, keep the already-learned steady interval
            # so that available token is not delayed for the entire reset
            # window. If a successful response says remaining=0, schedule the
            # next request after reset. On 429 responses, do not add a second
            # custom cooldown; discord.py owns the actual retry/reset wait.
            if reset_after is not None and reset_after > 0 and status_code != 429:
                if remaining is not None and remaining > 1:
                    derived = (
                        (reset_after / remaining) * FORUM_SAFETY_FACTOR
                        + FORUM_SAFETY_MARGIN
                    )
                    self._interval = max(FORUM_MIN_INTERVAL, derived)
                if remaining == 0:
                    reset_at = time.monotonic() + reset_after + FORUM_SAFETY_MARGIN
                    self._next_start = max(self._next_start, reset_at)
            if status_code == 429:
                # A 429 may expose a shared/resource-specific limit that cannot
                # be inferred from successful responses. Log the server's
                # instruction, but do NOT turn Retry-After into a permanent
                # per-post interval: Retry-After is a cooldown, not a pacing
                # rate. discord.py handles the actual retry/cooldown internally.
                logger.warning(
                    "Forum-create HTTP 429: retry_after=%s scope=%s global=%s "
                    "limit=%s remaining=%s reset_after=%s bucket=%s "
                    "steady_interval=%.2fs",
                    retry_after,
                    scope,
                    headers.get("X-RateLimit-Global", "false"),
                    limit if limit is not None else "n/a",
                    remaining if remaining is not None else "n/a",
                    reset_after if reset_after is not None else "n/a",
                    self.bucket or "unknown",
                    self._interval,
                )
            if status_code != 429 and (remaining is not None and (remaining <= 5 or remaining == (limit - 1 if limit else -1))):
                logger.info(
                    "Forum-create rate-limit telemetry: status=%s limit=%s "
                    "remaining=%s reset_after=%.3fs bucket=%s interval=%.2fs",
                    status_code,
                    limit if limit is not None else "n/a",
                    remaining,
                    reset_after if reset_after is not None else -1.0,
                    self.bucket or "unknown",
                    self._interval,
                )
        except Exception:
            logger.debug("Could not ingest forum rate-limit headers", exc_info=True)

    @staticmethod
    def _float_header(headers: Any, name: str) -> Optional[float]:
        try:
            return float(headers[name])
        except (KeyError, TypeError, ValueError):
            return None

    async def _wait_for_slot(self) -> None:
        while True:
            delay = max(0.0, self._next_start - time.monotonic())
            if delay <= 0:
                return
            await asyncio.sleep(delay)

    @staticmethod
    def _retry_after(exc: discord.HTTPException) -> float:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                return max(0.0, float(headers.get("Retry-After", 0.0)))
            except (TypeError, ValueError):
                pass

        text = getattr(exc, "text", "")
        if isinstance(text, str):
            try:
                payload = json.loads(text)
                return max(0.0, float(payload.get("retry_after", 0.0)))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        return FORUM_INITIAL_INTERVAL

    async def run(self, call: Callable[[], Awaitable[Any]]) -> Any:
        """Run exactly one forum-create request at a time.

        discord.py normally consumes 429 responses itself. This retry exists as
        a final safety net for documented 429 responses that escape the HTTP
        client's normal handling.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()

        async with self._lock:
            for attempt in range(MAX_FORUM_429_RETRIES + 1):
                await self._wait_for_slot()
                started = time.monotonic()
                try:
                    result = await call()
                    # Pace *start-to-start*. Do not add the interval again
                    # after the response completes: that made each cycle
                    # request latency + interval (e.g. a 3.1s interval plus
                    # 0.7s API latency became 3.8s per post).
                    actual_start = self._last_target_start or started
                    self._next_start = max(
                        self._next_start,
                        actual_start + self._interval,
                    )
                    return result
                except discord.HTTPException as exc:
                    if exc.status != 429 or attempt >= MAX_FORUM_429_RETRIES:
                        raise

                    retry_after = self._retry_after(exc)
                    # Normally discord.py consumes 429s internally. If one
                    # escapes, schedule this retry from Retry-After rather than
                    # looping immediately. This is deliberately not a separate
                    # long-lived bucket state; the HTTP client's bucket state is
                    # authoritative for the next request.
                    retry_at = time.monotonic() + retry_after + FORUM_RETRY_MARGIN
                    self._next_start = max(self._next_start, retry_at)
                    logger.warning(
                        "Forum-create request received HTTP 429; backing off %.2fs "
                        "before retry %d/%d.",
                        retry_after,
                        attempt + 1,
                        MAX_FORUM_429_RETRIES,
                    )

            raise RuntimeError("Forum-create retry loop exhausted")


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
class Database:
    """Persistent storage for the one-log-per-user rule.

    Uses a single long-lived connection (opening a new one for every query was
    slow and blocked the event loop longer than necessary).
    """

    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._initialize()

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back on error."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def _initialize(self) -> None:
        with self._session() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS member_logs (
                    user_id INTEGER PRIMARY KEY,
                    thread_id INTEGER NOT NULL UNIQUE,
                    thread_url TEXT NOT NULL,
                    username TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                INSERT OR IGNORE INTO settings(key, value)
                VALUES ('auto_log', 'false')
                """
            )

    def load_member_logs(self) -> dict[int, int]:
        with self._session() as db:
            rows = db.execute("SELECT user_id, thread_id FROM member_logs").fetchall()
        return {int(row["user_id"]): int(row["thread_id"]) for row in rows}

    def add_member_log(
        self,
        user_id: int,
        thread_id: int,
        thread_url: str,
        username: str,
    ) -> bool:
        """Insert a row; returns False if the user already has one (race guard)."""
        with self._session() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO member_logs
                    (user_id, thread_id, thread_url, username, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    thread_id,
                    thread_url,
                    username,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def upsert_member_log(
        self,
        user_id: int,
        thread_id: int,
        thread_url: str,
        username: str,
    ) -> None:
        """Insert or REPLACE the stored thread (used when repairing from Discord).

        BUG FIX: the old code used INSERT OR IGNORE here, so a user whose post
        was re-created kept pointing at the old, dead thread id in the DB.
        """
        with self._session() as db:
            db.execute(
                """
                INSERT INTO member_logs
                    (user_id, thread_id, thread_url, username, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    thread_url = excluded.thread_url,
                    username = excluded.username
                """,
                (
                    user_id,
                    thread_id,
                    thread_url,
                    username,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def remove_member_log(self, user_id: int) -> None:
        with self._session() as db:
            db.execute("DELETE FROM member_logs WHERE user_id = ?", (user_id,))

    def get_setting(self, key: str) -> Optional[str]:
        with self._session() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._session() as db:
            db.execute(
                """
                INSERT INTO settings(key, value)
                VALUES (?, ?)
                ON CONFLICT(key)
                DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def set_auto_log(self, enabled: bool) -> None:
        self.set_setting("auto_log", "true" if enabled else "false")

    def get_auto_log(self) -> bool:
        return (self.get_setting("auto_log") or "false").lower() == "true"


# ----------------------------------------------------------------------------
# Job bookkeeping + status display
# ----------------------------------------------------------------------------
@dataclass
class LogJob:
    operation: str = ""
    running: bool = False
    aborted: bool = False
    total: int = 0
    completed: int = 0
    created: int = 0
    skipped: int = 0
    failed: int = 0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    last_error: Optional[str] = None
    rate_limit_hits: int = 0

    @property
    def progress_percent(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(100.0, self.completed * 100.0 / self.total)

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at or time.monotonic()
        return max(0.0, end - self.started_at)

    @property
    def eta_seconds(self) -> Optional[float]:
        if not self.running:
            return 0.0

        remaining = max(0, self.total - self.completed)
        if remaining == 0:
            return 0.0

        if self.completed == 0:
            return remaining * ETA_GUESS_PER_ITEM

        elapsed = max(self.elapsed_seconds, 0.001)
        rate = self.completed / elapsed
        if rate <= 0:
            return None

        return remaining / rate


def make_progress_bar(current: int, total: int) -> str:
    percent = 0.0 if total <= 0 else min(100.0, current * 100.0 / total)
    filled = round((percent / 100.0) * PROGRESS_BAR_LENGTH)
    filled = max(0, min(PROGRESS_BAR_LENGTH, filled))
    return "█" * filled + "░" * (PROGRESS_BAR_LENGTH - filled)


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "calculating..."

    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def build_status_embed(job: LogJob) -> discord.Embed:
    embed = discord.Embed(colour=discord.Colour.from_rgb(36, 36, 41))

    if not job.running:
        embed.description = "No logs being created right now."
        return embed

    embed.description = (
        f"`{make_progress_bar(job.completed, job.total)}` "
        f"**{job.completed}/{job.total}** • **{job.progress_percent:.1f}%**"
    )

    details = [f"**ETA** {format_duration(job.eta_seconds)}"]

    if job.failed:
        details.append(f"**Failed** {job.failed}")

    if job.rate_limit_hits:
        details.append(f"**HTTP 429s** {job.rate_limit_hits}")

    embed.add_field(name="\u200b", value="  •  ".join(details), inline=False)

    if job.last_error:
        embed.add_field(
            name="Last error",
            value=job.last_error[:1000],
            inline=False,
        )

    return embed


# ----------------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
class LogManager:
    def __init__(
        self,
        bot: discord.Client,
        database: Database,
        role_id: int,
        forum_channel_id: int,
        forum_pacer: ForumCreatePacer,
        concurrency: int = 1,
    ) -> None:
        self.bot = bot
        self.database = database
        self.role_id = role_id
        self.forum_channel_id = forum_channel_id
        # Keep the knob for backwards-compatible environment files, but clamp
        # it to one for the forum-create route. Concurrent creates are the
        # exact burst pattern this bot must avoid.
        self.concurrency = 1
        self.forum_pacer = forum_pacer

        self.known_logs: dict[int, int] = database.load_member_logs()
        self.auto_log_enabled = database.get_auto_log()

        self.job = LogJob()
        self.lock = asyncio.Lock()
        self._job_task: Optional[asyncio.Task] = None
        self._auto_task: Optional[asyncio.Task] = None
        self._auto_queue: dict[int, discord.Member] = {}
        self._auto_reconcile_task: Optional[asyncio.Task] = None
        self._last_auto_reconcile = 0.0
        self._abort = False
        self._consecutive_failures = 0
        self.status_mention = "`/status`"

        logger.info(
            "LogManager ready: forum-create single-flight queue enabled; "
            "cold-start interval=%.2fs; adaptive header pacing active.",
            FORUM_INITIAL_INTERVAL,
        )

    # ------------------------------------------------------------------
    # Discord helpers
    # ------------------------------------------------------------------

    async def get_forum_channel(self) -> discord.ForumChannel:
        channel = self.bot.get_channel(self.forum_channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(self.forum_channel_id)

        if not isinstance(channel, discord.ForumChannel):
            raise RuntimeError(
                f"DISCORD_FORUM_CHANNEL_ID ({self.forum_channel_id}) "
                "does not point to a forum channel."
            )

        return channel

    async def fetch_role_members(self, guild: discord.Guild) -> list[discord.Member]:
        if not self.role_id:
            raise RuntimeError(
                "No member role is configured. Use /settings to choose the role "
                "whose members should receive logs."
            )

        role = guild.get_role(self.role_id)
        if role is None:
            raise RuntimeError(
                f"The configured member role ({self.role_id}) was not found in "
                f"{guild.name}. Use /settings to select a valid role."
            )

        if not guild.chunked:
            try:
                await guild.chunk(cache=True)
            except discord.Forbidden as exc:
                raise RuntimeError(
                    "Discord refused member chunking. Enable Server Members "
                    "Intent in the Developer Portal and keep intents.members=True."
                ) from exc

        return [member for member in role.members if not member.bot]

    @staticmethod
    def parse_user_id_from_thread(thread: discord.Thread) -> Optional[int]:
        name = thread.name.rstrip()
        if not name.endswith("]"):
            return None

        start = name.rfind("[")
        if start == -1:
            return None

        value = name[start + 1 : -1].strip()
        if not value.isdigit():
            return None

        try:
            return int(value)
        except ValueError:
            return None

    async def get_all_existing_forum_threads(
        self,
        forum_channel: discord.ForumChannel,
    ) -> tuple[dict[int, discord.Thread], set[int], bool]:
        """Index active + archived posts.

        Returns (threads by user id, every thread id seen, scan_complete).
        """
        by_user: dict[int, discord.Thread] = {}
        all_ids: set[int] = set()
        complete = True

        def index(thread: discord.Thread) -> None:
            all_ids.add(thread.id)
            user_id = self.parse_user_id_from_thread(thread)
            if user_id is not None:
                by_user[user_id] = thread

        # ForumChannel.threads is a cache, not an authoritative inventory.
        # Fetch the guild's active threads so logs that were created while the
        # bot was offline are still discovered after a restart/reconnect.
        try:
            active_threads = await forum_channel.guild.active_threads()
            for thread in active_threads:
                if thread.parent_id == forum_channel.id:
                    index(thread)
        except discord.HTTPException as exc:
            complete = False
            logger.warning("Could not fully scan active forum posts (%s).", exc)

        # Keep cached forum threads as an additional source in case the active
        # endpoint is temporarily incomplete.
        for thread in forum_channel.threads:
            index(thread)

        try:
            async for thread in forum_channel.archived_threads(limit=None):
                index(thread)
        except discord.HTTPException as exc:
            complete = False
            logger.warning("Could not fully scan archived forum posts (%s).", exc)

        return by_user, all_ids, complete

    async def get_pending_members(
        self,
        members: list[discord.Member],
        forum_channel: discord.ForumChannel,
    ) -> tuple[list[discord.Member], int]:
        by_user, all_ids, complete = await self.get_all_existing_forum_threads(
            forum_channel
        )

        if not complete:
            # BUG FIX: previously an incomplete scan still "repaired" the DB,
            # wiping every archived post's record and creating duplicates.
            raise RuntimeError(
                "Could not read the archived forum posts, so existing logs "
                "can't be verified. Nothing was changed; please try again."
            )

        # Repair DB state from actual Discord state. A record is kept as long
        # as its thread still exists, even if the title was edited.
        for user_id, thread_id in list(self.known_logs.items()):
            if thread_id not in all_ids:
                self.known_logs.pop(user_id, None)
                self.database.remove_member_log(user_id)

        pending: list[discord.Member] = []
        already_done = 0

        for member in members:
            thread = by_user.get(member.id)
            if thread is not None:
                if self.known_logs.get(member.id) != thread.id:
                    self.known_logs[member.id] = thread.id
                    self.database.upsert_member_log(
                        member.id,
                        thread.id,
                        thread.jump_url,
                        member.name,
                    )
                already_done += 1
            elif member.id in self.known_logs:
                already_done += 1  # thread exists but its title was edited
            else:
                pending.append(member)

        return pending, already_done

    @staticmethod
    def _format_member_content(member: discord.Member) -> str:
        account_created_timestamp = int(member.created_at.timestamp())

        joined_days: int | str = "unknown"
        if member.joined_at is not None:
            joined_at = member.joined_at.astimezone(timezone.utc)
            now = datetime.now(timezone.utc)
            joined_days = max(0, (now - joined_at).days)

        return (
            "This member log was created automatically.\n\n"
            f"User Mention: <@{member.id}>\n\n"
            "Account created: "
            f"<t:{account_created_timestamp}:F> "
            f"(Joined the server `{joined_days}` days ago)"
        )

    # ------------------------------------------------------------------
    # Thread creation
    # ------------------------------------------------------------------

    async def create_member_forum_post(
        self,
        member: discord.Member,
        forum_channel: discord.ForumChannel,
    ) -> Optional[discord.Thread]:
        if member.bot or member.id in self.known_logs:
            return None

        # BUG FIX: thread names are capped at 100 chars. Trim the username,
        # never the "[id]" suffix the whole system relies on.
        suffix = f" [{member.id}]"
        title = member.name[: THREAD_NAME_LIMIT - len(suffix)] + suffix
        content = self._format_member_content(member)

        async def create() -> Any:
            return await forum_channel.create_thread(
                name=title,
                content=content,
                # The mention still renders as a clickable name, but nobody
                # gets pinged / pulled into thousands of threads.
                allowed_mentions=discord.AllowedMentions.none(),
                reason=f"Member log for Discord user {member.id}",
            )

        # One request at a time + adaptive pacing prevents the application
        # from creating bursts before Discord's shared/guild bucket is known.
        result = await self.forum_pacer.run(create)

        thread = getattr(result, "thread", result)
        if not isinstance(thread, discord.Thread):
            raise RuntimeError("Discord did not return the created forum thread.")

        stored = self.database.add_member_log(
            member.id,
            thread.id,
            thread.jump_url,
            member.name,
        )

        if not stored:
            try:
                await thread.delete(reason="Duplicate member log prevented")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Could not delete duplicate thread %s", thread.id)
            return None

        self.known_logs[member.id] = thread.id
        return thread

    async def _create_test_post(
        self,
        index: int,
        batch_id: int,
        forum_channel: discord.ForumChannel,
    ) -> None:
        title = f"[TEST] Member {index} [{batch_id}-{index}]"
        content = (
            "Synthetic forum post generated by `/test`.\n\n"
            f"Test member: `{index}`\n"
            f"Test batch: `{batch_id}`\n\n"
            "This post is for load testing and is not a real member log."
        )

        if index == 1 or index % 10 == 0 or index == self.job.total:
            logger.info(
                "Test batch %s: submitting forum post %d/%d.",
                batch_id,
                index,
                self.job.total,
            )

        await self.forum_pacer.run(
            lambda: forum_channel.create_thread(
                name=title,
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                reason="Forum load test created by /test",
            )
        )

        if index == 1 or index % 10 == 0 or index == self.job.total:
            logger.info(
                "Test batch %s: forum post %d/%d created successfully.",
                batch_id,
                index,
                self.job.total,
            )

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def _start_job(self, operation: str, total: int) -> None:
        if self.job.running or self.lock.locked():
            raise RuntimeError("Another logging job is already running.")
        if total <= 0:
            raise RuntimeError("There is nothing to process.")

        self._abort = False
        self._consecutive_failures = 0
        self.job = LogJob(
            operation=operation,
            running=True,
            total=total,
            started_at=time.monotonic(),
        )

    async def start_member_job(self, members: list[discord.Member]) -> None:
        # Resolve the channel first (cache OR REST) so a failure can't leave a
        # half-started job behind.
        forum_channel = await self.get_forum_channel()
        self._start_job("Creating member logs", len(members))

        async def handler(member: discord.Member) -> bool:
            thread = await self.create_member_forum_post(member, forum_channel)
            return thread is not None

        self._job_task = asyncio.create_task(
            self._run_job(members, handler, "Member log"),
            name="member-log-job",
        )

    async def start_test_job(self, count: int) -> None:
        forum_channel = await self.get_forum_channel()
        self._start_job("Creating test forum posts", count)

        batch_id = int(time.time() * 1000)

        async def handler(index: int) -> bool:
            await self._create_test_post(index, batch_id, forum_channel)
            return True

        self._job_task = asyncio.create_task(
            self._run_job(range(1, count + 1), handler, "Test"),
            name="test-forum-job",
        )

    async def _process_item(
        self,
        handler: Callable[[Any], Awaitable[bool]],
        item: Any,
        label: str,
    ) -> None:
        """Run one item and update job counters. Never raises."""
        created = False
        error: Optional[str] = None

        try:
            created = await handler(item)
        except discord.Forbidden as exc:
            error = "Missing Discord permissions."
            logger.error("%s permission error: %s", label, exc)
        except discord.NotFound as exc:
            error = "Forum channel was not found."
            logger.error("%s not found: %s", label, exc)
        except discord.HTTPException as exc:
            if exc.status == 429:
                # A 429 here means both discord.py and the forum safety queue
                # exhausted their documented recovery paths. Keep this visible
                # instead of pretending the request was never rate limited.
                self.job.rate_limit_hits += 1
            error = f"Discord HTTP {exc.status}: {str(exc.text)[:200]}"
            logger.error("%s HTTP failure (%s): %s", label, exc.status, exc)
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            logger.exception("%s item failed", label)

        self.job.completed += 1
        if error is not None:
            self.job.failed += 1
            self.job.last_error = error
            self._consecutive_failures += 1
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # Don't hammer Discord with thousands of doomed requests.
                self._abort = True
                self.job.aborted = True
        else:
            self._consecutive_failures = 0
            if created:
                self.job.created += 1
            else:
                self.job.skipped += 1

    async def _run_job(
        self,
        items: Iterable[Any],
        handler: Callable[[Any], Awaitable[bool]],
        label: str,
    ) -> None:
        """Process items with one or more workers sharing one iterator.

        The job remains marked running until the shared lock has actually been
        released. This prevents the UI from saying a job is finished while a
        second job is still temporarily blocked behind the lock.
        """
        try:
            async with self.lock:
                iterator = iter(items)

                async def worker() -> None:
                    # next() on a shared iterator is synchronous, so each item
                    # goes to exactly one worker.
                    while not self._abort:
                        item = next(iterator, _SENTINEL)
                        if item is _SENTINEL:
                            break
                        await self._process_item(handler, item, label)

                await asyncio.gather(*(worker() for _ in range(self.concurrency)))
        finally:
            # This runs after `async with self.lock` exits, so a new job can
            # start immediately after we mark the old one finished.
            self._finish_job()

    def _finish_job(self) -> None:
        self.job.running = False
        self.job.finished_at = time.monotonic()
        self._job_task = None

        logger.info(
            "Job finished: %d/%d created, %d failed, %d HTTP 429s%s.",
            self.job.created,
            self.job.total,
            self.job.failed,
            self.job.rate_limit_hits,
            " (aborted early)" if self.job.aborted else "",
        )

    # ------------------------------------------------------------------
    # Automatic logging
    # ------------------------------------------------------------------

    def _queue_auto_members(self, members: Iterable[discord.Member]) -> int:
        """Add missing members to the persistent auto-log work queue."""
        queued = 0
        for member in members:
            if member.bot or member.id in self.known_logs:
                continue
            if member.id not in self._auto_queue:
                self._auto_queue[member.id] = member
                queued += 1

        if queued and (self._auto_task is None or self._auto_task.done()):
            self._auto_task = asyncio.create_task(
                self._auto_worker(),
                name="auto-log-worker",
            )
        return queued

    async def ensure_member_has_log(self, member: discord.Member) -> bool:
        """Queue an automatic log for `member`. Returns True if queued."""
        if not self.auto_log_enabled or not self.role_id or member.bot:
            return False

        forum_channel = await self.get_forum_channel()
        if member.guild.id != forum_channel.guild.id:
            return False

        role = member.guild.get_role(self.role_id)
        if role is None or role not in member.roles:
            return False

        if member.id in self.known_logs:
            return False

        return bool(self._queue_auto_members([member]))

    def schedule_auto_reconcile(self, force: bool = False) -> None:
        """Schedule a startup/reconnect reconciliation when Auto Log is enabled.

        Reconciliation is intentionally cooldown-limited so transient Discord
        gateway reconnects do not cause repeated full scans of archived posts.
        """
        if not self.auto_log_enabled or not self.role_id or not self.forum_channel_id:
            return
        if self._auto_reconcile_task is not None and not self._auto_reconcile_task.done():
            return

        now = time.monotonic()
        if not force and now - self._last_auto_reconcile < 60.0:
            return

        self._last_auto_reconcile = now
        self._auto_reconcile_task = asyncio.create_task(
            self.reconcile_auto_logs(),
            name="auto-log-reconcile",
        )

    async def reconcile_auto_logs(self) -> None:
        """Repair persisted log state and queue every current member missing a log."""
        if not self.auto_log_enabled or not self.role_id or not self.forum_channel_id:
            return

        try:
            forum_channel = await self.get_forum_channel()
            guild = self.bot.get_guild(forum_channel.guild.id) or forum_channel.guild
            members = await self.fetch_role_members(guild)

            pending, already_done = await self.get_pending_members(
                members, forum_channel
            )
            queued = self._queue_auto_members(pending)

            logger.info(
                "Auto-log reconciliation complete: %d members already have logs, "
                "%d missing logs queued.",
                already_done,
                queued,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Auto-log startup/reconnect reconciliation failed")

    async def _auto_worker(self) -> None:
        # Waiting on the lock also means waiting for any bulk job to finish.
        async with self.lock:
            forum_channel: Optional[discord.ForumChannel] = None

            while self._auto_queue:
                if not self.auto_log_enabled:
                    self._auto_queue.clear()
                    return

                member_id = next(iter(self._auto_queue))
                member = self._auto_queue.pop(member_id)

                try:
                    if member_id in self.known_logs:
                        continue
                    if member.guild.get_member(member_id) is None:
                        continue  # left the server while waiting
                    if forum_channel is None:
                        forum_channel = await self.get_forum_channel()
                    await self.create_member_forum_post(member, forum_channel)
                except Exception:
                    logger.exception("Queued auto-log failed for %s", member_id)

    def set_auto_log(self, enabled: bool) -> None:
        self.database.set_auto_log(enabled)
        self.auto_log_enabled = enabled
        if not enabled:
            self._auto_queue.clear()

    def handle_thread_delete(self, thread_id: int) -> None:
        for user_id, stored_thread_id in list(self.known_logs.items()):
            if stored_thread_id == thread_id:
                self.known_logs.pop(user_id, None)
                self.database.remove_member_log(user_id)
                logger.info(
                    "Deleted forum thread %s removed from member-log database.",
                    thread_id,
                )
                break


# ----------------------------------------------------------------------------
# Live progress message
# ----------------------------------------------------------------------------
async def monitor_job_message(
    manager: LogManager,
    interaction: discord.Interaction,
    operation_text: str,
) -> None:
    """Show live progress and a stable final result for this specific job."""
    # Capture the job object. A new job can replace manager.job immediately after
    # the old one finishes, and this monitor must never report the new job's
    # counters as the previous command's result.
    job = manager.job

    live = False
    last_edit = 0.0
    started = time.monotonic()

    while job.running:
        await asyncio.sleep(0.5)

        eta = job.eta_seconds
        if not live and eta is not None and eta > STATUS_ETA_THRESHOLD:
            live = True

        # Interaction tokens expire after 15 minutes; stop editing before that.
        can_edit = time.monotonic() - started < INTERACTION_TOKEN_SECONDS

        if live and can_edit and time.monotonic() - last_edit >= STATUS_UPDATE_SECONDS:
            last_edit = time.monotonic()
            try:
                await interaction.edit_original_response(
                    content=(
                        f"{operation_text}. View the "
                        f"{manager.status_mention} below:"
                    ),
                    embed=build_status_embed(job),
                    view=None,
                )
            except discord.HTTPException:
                logger.debug("Could not update live progress message", exc_info=True)

    elapsed = format_duration(job.elapsed_seconds)

    if job.failed == 0 and not job.aborted:
        result = f"Successfully created `{job.created}` logs in • `{elapsed}`."
    else:
        result = f"Created `{job.created}` logs in • `{elapsed}`."
        if job.failed:
            result += f" `{job.failed}` failed."
        if job.aborted:
            result += " Stopped early after repeated failures."

    try:
        await interaction.edit_original_response(content=result, embed=None, view=None)
    except discord.HTTPException:
        # Long jobs can outlive the interaction token. Fall back to a normal
        # channel message using the already-captured completed job.
        logger.debug("Could not write final job result", exc_info=True)
        channel = interaction.channel
        if channel is not None and hasattr(channel, "send"):
            try:
                await channel.send(
                    f"{interaction.user.mention} {result}",
                    allowed_mentions=discord.AllowedMentions(
                        users=[interaction.user]
                    ),
                )
            except discord.HTTPException:
                logger.debug("Could not send final result to channel", exc_info=True)