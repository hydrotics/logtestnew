from __future__ import annotations

import asyncio
import logging
import random
import re
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
    Mapping,
    Optional,
    TypeVar,
)

import aiohttp
import discord

logger = logging.getLogger("discord-log-bot.create_log")

# ----------------------------------------------------------------------------
# Pacing strategy (goal: maximum speed, zero 429s)
#
# Discord tells us the exact limit of the forum-post route in every response:
#   X-RateLimit-Limit / -Remaining / -Reset-After / -Reset
# ForumRateGate reads those headers (via an aiohttp trace hook on discord.py's
# HTTP session) and only lets a request start when a slot is guaranteed:
#   remaining - reserve - in_flight > 0
# so the bot bursts through the whole window allowance, then sleeps exactly
# until the window resets. No guessing, no fixed delay, no wasted time.
# ----------------------------------------------------------------------------
MAX_CONCURRENCY = 10
FALLBACK_INTERVAL = 1.0      # spacing used ONLY until Discord reveals the limits
SAFETY_MARGIN = 0.05         # added to every reset time (seconds)
JITTER_SECONDS = 0.05        # only used when a minimum interval is configured
MAX_429_RETRIES = 12
FALLBACK_429_DELAY = 5.0
MAX_CONSECUTIVE_FAILURES = 10
THREAD_NAME_LIMIT = 100

PROGRESS_BAR_LENGTH = 24
STATUS_ETA_THRESHOLD = 15.0
STATUS_UPDATE_SECONDS = 2.5
ETA_GUESS_PER_ITEM = 1.0
INTERACTION_TOKEN_SECONDS = 14 * 60   # interaction tokens die after 15 minutes

_THREAD_CREATE_RE = re.compile(r"/channels/\d+/threads/?$")
_SENTINEL = object()

T = TypeVar("T")


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
    rate_limit_wait_seconds: float = 0.0

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
        details.append(f"**Rate limits** {job.rate_limit_hits}")

    embed.add_field(name="\u200b", value="  •  ".join(details), inline=False)

    if job.last_error:
        embed.add_field(
            name="Last error",
            value=job.last_error[:1000],
            inline=False,
        )

    return embed


# ----------------------------------------------------------------------------
# Rate-limit gate
# ----------------------------------------------------------------------------
def _header_float(headers: Mapping[str, str], name: str) -> Optional[float]:
    try:
        return float(headers[name])
    except (KeyError, TypeError, ValueError):
        return None


class ForumRateGate:
    """Header-driven limiter for ``POST /channels/{id}/threads``.

    * Starts with ONE probe request so Discord can report the real limits.
    * Afterwards a request may start only if
      ``remaining - reserve - in_flight > 0`` (so concurrent workers can never
      overshoot the window), otherwise it sleeps until the window resets.
    * If Discord ever still answers 429 (undocumented / shared limits), the
      gate waits out the Retry-After and permanently keeps one more slot per
      window in reserve, so it converges on a pace that never trips again.
    """

    def __init__(self, min_interval: float = 0.0) -> None:
        self.min_interval = max(0.0, float(min_interval))

        self.limit: Optional[int] = None
        self.remaining: Optional[int] = None
        self.reset_at = 0.0                  # local monotonic time
        self.reserve = 0
        self.inflight = 0
        self.rate_limit_hits = 0
        self.on_rate_limit: Optional[Callable[[float], None]] = None

        self._window_epoch: Optional[float] = None   # server-side reset time
        self._blocked_until = 0.0
        self._next_start = 0.0
        self._cond = asyncio.Condition()
        self._bg: set[asyncio.Task] = set()

        # Hand this to discord.Client(http_trace=...)
        self.trace_config = aiohttp.TraceConfig()
        self.trace_config.on_request_end.append(self._on_request_end)

    # -- header ingestion ----------------------------------------------------
    async def _on_request_end(self, session, ctx, params) -> None:
        try:
            if params.method.upper() != "POST":
                return
            if not _THREAD_CREATE_RE.search(params.url.path):
                return
            async with self._cond:
                self._ingest(params.response.status, params.response.headers)
                self._cond.notify_all()
        except Exception:
            logger.debug("Could not read rate-limit headers", exc_info=True)

    def _ingest(self, status: int, headers: Mapping[str, str]) -> None:
        now = time.monotonic()
        limit = _header_float(headers, "X-RateLimit-Limit")
        remaining = _header_float(headers, "X-RateLimit-Remaining")
        reset_after = _header_float(headers, "X-RateLimit-Reset-After")
        reset_epoch = _header_float(headers, "X-RateLimit-Reset")

        if status == 429:
            retry_after = _header_float(headers, "Retry-After")
            if retry_after is None:
                retry_after = reset_after
            if retry_after is None:
                retry_after = FALLBACK_429_DELAY

            self.rate_limit_hits += 1
            if limit is not None:
                self.limit = int(limit)
            self.remaining = 0
            self.reset_at = max(self.reset_at, now + retry_after + SAFETY_MARGIN)
            self._blocked_until = self.reset_at
            self._window_epoch = None
            if self.limit is not None:
                self.reserve = min(self.reserve + 1, max(0, self.limit - 1))

            logger.warning(
                "Discord returned 429 on forum creation (retry in %.2fs). "
                "Keeping %d slot(s) in reserve from now on.",
                retry_after,
                self.reserve,
            )
            if self.on_rate_limit is not None:
                try:
                    self.on_rate_limit(retry_after)
                except Exception:
                    pass
            return

        # Ignore late responses that were sent before a 429 happened.
        if now < self._blocked_until:
            return

        if limit is None or remaining is None or reset_after is None:
            return

        remaining_i = int(remaining)
        new_reset = now + reset_after + SAFETY_MARGIN   # never earlier than reality
        self.limit = int(limit)
        self.reserve = min(self.reserve, max(0, self.limit - 1))

        same_window = False
        if now < self.reset_at and self.remaining is not None:
            if reset_epoch is not None and self._window_epoch is not None:
                if reset_epoch < self._window_epoch - 0.1:
                    return  # response from an older window, ignore
                same_window = abs(reset_epoch - self._window_epoch) <= 0.1
            elif reset_epoch is None:
                same_window = abs(new_reset - self.reset_at) < 0.5

        if same_window:
            # Responses can arrive out of order; the lowest value is the truth.
            self.remaining = min(self.remaining, remaining_i)  # type: ignore[arg-type]
            self.reset_at = min(self.reset_at, new_reset)
        else:
            self.remaining = remaining_i
            self.reset_at = new_reset
            self._window_epoch = reset_epoch

    # -- scheduling ----------------------------------------------------------
    def _delay(self, now: float) -> Optional[float]:
        """0 = go now, >0 = sleep that long, None = wait for a state change."""
        if self._next_start > now:
            return self._next_start - now

        if now < self.reset_at and self.remaining is not None:
            if self.remaining - self.reserve - self.inflight > 0:
                return 0.0
            return self.reset_at - now

        # Window is over, or we have never seen headers.
        if self.limit is None:
            return 0.0 if self.inflight == 0 else None  # one probe at a time

        if self.limit - self.reserve - self.inflight > 0:
            return 0.0
        return None

    async def acquire(self) -> None:
        async with self._cond:
            while True:
                now = time.monotonic()
                delay = self._delay(now)
                if delay is not None and delay <= 0:
                    break
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

            self.inflight += 1
            interval = (
                self.min_interval
                if self.limit is not None
                else max(self.min_interval, FALLBACK_INTERVAL)
            )
            jitter = random.uniform(0.0, JITTER_SECONDS) if interval > 0 else 0.0
            self._next_start = now + interval + jitter

    def release(self) -> None:
        self.inflight = max(0, self.inflight - 1)
        task = asyncio.get_running_loop().create_task(self._notify())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    async def run(self, call: Callable[[], Awaitable[T]]) -> T:
        """Run one forum-create call under the gate.

        Only explicit 429s are retried. Network/5xx errors are NOT retried
        because Discord may have created the post before the response was lost.
        """
        for attempt in range(MAX_429_RETRIES + 1):
            await self.acquire()
            try:
                return await call()
            except discord.HTTPException as exc:
                if exc.status != 429 or attempt >= MAX_429_RETRIES:
                    raise
                # discord.py gave up on its own retries; make sure we wait.
                now = time.monotonic()
                if self.reset_at <= now:
                    self.remaining = 0
                    self.reset_at = now + FALLBACK_429_DELAY
            finally:
                self.release()

        raise RuntimeError("Unreachable retry state")


# ----------------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------------
class LogManager:
    def __init__(
        self,
        bot: discord.Client,
        database: Database,
        role_id: int,
        forum_channel_id: int,
        gate: ForumRateGate,
        concurrency: int = 4,
    ) -> None:
        self.bot = bot
        self.database = database
        self.role_id = role_id
        self.forum_channel_id = forum_channel_id
        self.gate = gate
        self.concurrency = max(1, min(MAX_CONCURRENCY, int(concurrency)))

        self.known_logs: dict[int, int] = database.load_member_logs()
        self.auto_log_enabled = database.get_auto_log()

        self.job = LogJob()
        self.lock = asyncio.Lock()
        self._job_task: Optional[asyncio.Task] = None
        self._auto_task: Optional[asyncio.Task] = None
        self._auto_queue: dict[int, discord.Member] = {}
        self._abort = False
        self._consecutive_failures = 0
        self.status_mention = "`/status`"

        self.gate.on_rate_limit = self._on_rate_limit

        logger.info(
            "LogManager ready: concurrency=%d, min_interval=%.2fs",
            self.concurrency,
            self.gate.min_interval,
        )

    def _on_rate_limit(self, retry_after: float) -> None:
        if self.job.running:
            self.job.rate_limit_hits += 1
            self.job.rate_limit_wait_seconds += retry_after

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
        role = guild.get_role(self.role_id)
        if role is None:
            raise RuntimeError(
                f"DISCORD_ROLE_ID ({self.role_id}) was not found in {guild.name}."
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

        result = await self.gate.run(create)

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

        await self.gate.run(
            lambda: forum_channel.create_thread(
                name=title,
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                reason="Forum load test created by /test",
            )
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
        """Process items with `self.concurrency` workers sharing one iterator."""
        async with self.lock:
            try:
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
                self._finish_job()

    def _finish_job(self) -> None:
        self.job.running = False
        self.job.finished_at = time.monotonic()
        self._job_task = None

        logger.info(
            "Job finished: %d/%d created, %d failed, %d rate limits%s.",
            self.job.created,
            self.job.total,
            self.job.failed,
            self.job.rate_limit_hits,
            " (aborted early)" if self.job.aborted else "",
        )

    # ------------------------------------------------------------------
    # Automatic logging
    # ------------------------------------------------------------------

    async def ensure_member_has_log(self, member: discord.Member) -> bool:
        """Queue an automatic log for `member`. Returns True if queued.

        BUG FIX: the old version only drained its queue when a *bulk* job
        finished, so two members joining at the same time left the second one
        queued forever. A single worker now owns the queue.
        """
        if member.bot:
            return False

        forum_channel = await self.get_forum_channel()
        if member.guild.id != forum_channel.guild.id:
            return False

        role = member.guild.get_role(self.role_id)
        if role is None or role not in member.roles:
            return False

        if member.id in self.known_logs:
            return False

        self._auto_queue[member.id] = member
        if self._auto_task is None or self._auto_task.done():
            self._auto_task = asyncio.create_task(
                self._auto_worker(),
                name="auto-log-worker",
            )
        return True

    async def _auto_worker(self) -> None:
        # Waiting on the lock also means waiting for any bulk job to finish.
        async with self.lock:
            forum_channel: Optional[discord.ForumChannel] = None

            while self._auto_queue:
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
    """Switch the invoking message to a live status when ETA exceeds 15s."""
    live = False
    last_edit = 0.0
    started = time.monotonic()

    while manager.job.running:
        await asyncio.sleep(0.5)

        eta = manager.job.eta_seconds
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
                    embed=build_status_embed(manager.job),
                    view=None,
                )
            except discord.HTTPException:
                logger.debug("Could not update live progress message", exc_info=True)

    job = manager.job

    result = f"Created `{job.created}/{job.total}` successfully."
    if job.failed:
        result += f" `{job.failed}` failed."
    if job.aborted:
        result += (
            " Stopped early after repeated failures"
            f" (last error: {job.last_error})."
        )

    try:
        await interaction.edit_original_response(content=result, embed=None, view=None)
    except discord.HTTPException:
        # BUG FIX: long jobs outlive the interaction token, so the final
        # result silently vanished. Fall back to a normal channel message.
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
