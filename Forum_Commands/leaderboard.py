from __future__ import annotations

import asyncio
import heapq
import logging
import math
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import aiohttp
import discord
from discord import app_commands

import group_scan

logger = logging.getLogger("discord-log-bot.leaderboard")

TOP_N = 5

DERANK_CHANNEL_ID = 1272268411145289831

# ---------------------------------------------------------------------------
# Scan tuning
# ---------------------------------------------------------------------------

# The month is cut into windows of this many hours. Fewer, larger windows waste
# fewer requests (every window costs at least one page).
SCAN_WINDOW_HOURS = 24

# Max windows being scanned at once per channel. This is only an upper bound:
# the rate gate below decides when a request may actually be sent.
MAX_PARALLEL_WINDOWS = 6

# A review message is posted before it is reviewed, so requests posted shortly
# before the month began can still be reviewed inside it. These extra days are
# scanned, but only reviews whose own <t:...> timestamp is inside the month are
# counted. Set this to the longest time a request normally sits pending.
REVIEW_LOOKBACK_DAYS = 3

PAGE_SIZE = 100  # Discord's hard maximum per history request.

# ---------------------------------------------------------------------------
# Rate-limit safety
# ---------------------------------------------------------------------------

API_BASE = "https://discord.com/api/v10"

# Requests are only sent when Discord's own rate-limit headers say there is
# room (see _Gate). On top of that, a global ceiling across all channels.
# Discord's global limit is 50/s; this stays well under it.
GLOBAL_MAX_REQUESTS_PER_SECOND = 20

# Extra wait after a bucket's reset time before using it again.
RESET_PADDING = 0.1

# If Discord still answers 429 (e.g. another feature of the bot is using the
# same channel at the same moment), the Retry-After is obeyed. After this many
# hits the scan is aborted rather than continuing to push.
MAX_RATE_LIMIT_HITS = 3

MAX_SERVER_ERROR_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 30

# ---------------------------------------------------------------------------
# Progress display
# ---------------------------------------------------------------------------

# How often the loading message is edited. Kept slow on purpose so the
# progress updates themselves can never hit a rate limit.
PROGRESS_EDIT_INTERVAL = 3.0

# Interaction tokens expire after 15 minutes, so the scan is cut off before that.
SCAN_TIMEOUT_SECONDS = 13 * 60

BAR_WIDTH = 20

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_REVIEW_RE = re.compile(
    r"\*\*Status:\*\*\s*(Accepted|Denied) by <@!?(\d+)>\s*[·\u00b7]\s*<t:(\d+)"
)

_INFORMATION_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?(?:request\s+)?information(?:\*\*)?\s*:"
)

_DERANK_RE = re.compile(r"\bDeranked by\s*<@!?(\d+)>")

_MENTION_RE = re.compile(r"<@!?(\d+)>")

_MARKDOWN_RE = re.compile(r"[*_`~]")

Counts = dict[int, int]

# A parser looks at one message's raw embed dicts and returns
# (staff_id, increment), or None when the message should not be counted.
Parser = Callable[[list[dict[str, Any]]], "tuple[int, int] | None"]


def _month_bounds() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)

    return start, end


def _embed_text(embeds: list[dict[str, Any]]) -> str:
    parts: list[str] = []

    for embed in embeds:
        parts.append(embed.get("title") or "")
        parts.append(embed.get("description") or "")

        author = embed.get("author") or {}
        parts.append(author.get("name") or "")

        footer = embed.get("footer") or {}
        parts.append(footer.get("text") or "")

        for field in embed.get("fields") or []:
            parts.append(field.get("name") or "")
            parts.append(field.get("value") or "")

    return "\n".join(parts)


def _has_creation_information(embeds: list[dict[str, Any]]) -> bool:
    for embed in embeds:
        for field in embed.get("fields") or []:
            name = _MARKDOWN_RE.sub("", field.get("name") or "").strip().casefold()
            value = (field.get("value") or "").strip()

            if name in ("information", "request information") and value:
                return True

            if value and _INFORMATION_RE.search(value):
                return True

        description = embed.get("description") or ""

        if description and _INFORMATION_RE.search(description):
            return True

    return False


def _make_review_parser(
    *,
    month_start_ts: int,
    month_end_ts: int,
    accepted_only: bool,
    creation_only: bool,
) -> Parser:
    def parse(embeds: list[dict[str, Any]]) -> tuple[int, int] | None:
        if creation_only != _has_creation_information(embeds):
            return None

        text = _embed_text(embeds)
        match = _REVIEW_RE.search(text) if text else None

        if match is None:
            return None

        if accepted_only and match.group(1) != "Accepted":
            return None

        # Month accuracy: judged by when the review happened, not when the
        # request message was posted.
        reviewed_at = int(match.group(3))

        if not (month_start_ts <= reviewed_at < month_end_ts):
            return None

        return int(match.group(2)), 1

    return parse


def _parse_derank(embeds: list[dict[str, Any]]) -> tuple[int, int] | None:
    content = _embed_text(embeds)

    if not content:
        return None

    match = _DERANK_RE.search(content)

    if match is None:
        return None

    deranked_members = set(_MENTION_RE.findall(content[: match.start()]))

    if not deranked_members:
        return None

    return int(match.group(1)), len(deranked_members)


# ---------------------------------------------------------------------------
# Rate-limit gate
#
# Instead of sending requests and reacting to 429s, every request must first
# pass two checks:
#
#   1. _GlobalPacer: no more than GLOBAL_MAX_REQUESTS_PER_SECOND in any second.
#   2. _Gate (one per channel): driven by Discord's own X-RateLimit-* response
#      headers. A request starts only if
#          remaining - (requests already in flight) >= 1
#      otherwise it waits for the bucket's reset. Until the first response has
#      told us the bucket size, only ONE request is allowed in flight (a probe),
#      so the opening burst that normally triggers 429s cannot happen.
# ---------------------------------------------------------------------------


class ScanError(Exception):
    """The scan stopped; the message is safe to show to staff."""


class AccessError(ScanError):
    pass


class _GlobalPacer:
    def __init__(self) -> None:
        self._times: deque[float] = deque()
        self._lock = asyncio.Lock()
        self.paused_until = 0.0

    async def wait(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()

                if now < self.paused_until:
                    await asyncio.sleep(self.paused_until - now)
                    continue

                while self._times and now - self._times[0] >= 1.0:
                    self._times.popleft()

                if len(self._times) < GLOBAL_MAX_REQUESTS_PER_SECOND:
                    self._times.append(now)
                    return

                await asyncio.sleep(1.0 - (now - self._times[0]) + 0.01)


class _Gate:
    def __init__(self) -> None:
        self.limit: int | None = None
        self.remaining: int | None = None
        self.reset_at = math.inf      # monotonic time the current window ends
        self.epoch = 0.0              # Discord's X-RateLimit-Reset of that window
        self.blocked_until = 0.0
        self.inflight = 0
        self._cond = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._cond:
            while True:
                now = time.monotonic()

                if now < self.blocked_until:
                    timeout = self.blocked_until - now
                else:
                    # The window has rolled over: the bucket is full again.
                    if (
                        self.remaining is not None
                        and now >= self.reset_at + RESET_PADDING
                    ):
                        self.remaining = self.limit
                        self.reset_at = math.inf

                    if self.remaining is None:
                        allowed = self.inflight == 0   # probe request
                    else:
                        allowed = self.remaining - self.inflight >= 1

                    if allowed:
                        self.inflight += 1
                        return

                    timeout = (
                        self.reset_at + RESET_PADDING - now
                        if math.isfinite(self.reset_at)
                        else 1.0
                    )

                try:
                    await asyncio.wait_for(self._cond.wait(), max(0.02, timeout))
                except asyncio.TimeoutError:
                    pass

    async def release(self, headers: Any = None) -> None:
        async with self._cond:
            self.inflight = max(0, self.inflight - 1)

            if headers is not None:
                self._update(headers)

            self._cond.notify_all()

    def _update(self, headers: Any) -> None:
        now = time.monotonic()

        try:
            limit = int(headers["X-RateLimit-Limit"])
            remaining = int(headers["X-RateLimit-Remaining"])
            epoch = float(headers["X-RateLimit-Reset"])
            reset_after = float(headers["X-RateLimit-Reset-After"])
        except (KeyError, TypeError, ValueError):
            # No usable headers: fall back to one request per second.
            self.limit = 1
            self.remaining = 0
            self.reset_at = now + 1.0
            self.epoch = time.time()
            return

        self.limit = limit

        if epoch > self.epoch:
            # First response of a newer window.
            self.epoch = epoch
            self.remaining = remaining
            self.reset_at = now + reset_after
        elif epoch == self.epoch:
            # Same window: trust the lowest remaining count seen.
            current = self.remaining if self.remaining is not None else remaining
            self.remaining = min(current, remaining)
        # Older window (a late response): ignore.

    async def penalize(self, retry_after: float) -> None:
        async with self._cond:
            until = time.monotonic() + retry_after + RESET_PADDING
            self.blocked_until = max(self.blocked_until, until)
            self.remaining = 0
            self.reset_at = until
            self._cond.notify_all()


class _Fetcher:
    """Paced, header-aware reader for GET /channels/{id}/messages."""

    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self._session = session
        self._headers = {
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (leaderboard-scan, 1.0)",
        }
        self._gates: dict[int, _Gate] = {}
        self._pacer = _GlobalPacer()
        self.rate_limit_hits = 0

    def _gate(self, channel_id: int) -> _Gate:
        gate = self._gates.get(channel_id)

        if gate is None:
            gate = self._gates[channel_id] = _Gate()

        return gate

    async def page(self, channel_id: int, after_id: int) -> list[dict[str, Any]]:
        url = f"{API_BASE}/channels/{channel_id}/messages"
        params = {"limit": str(PAGE_SIZE), "after": str(after_id)}
        gate = self._gate(channel_id)
        server_errors = 0

        while True:
            await gate.acquire()

            try:
                await self._pacer.wait()
            except BaseException:
                await gate.release()
                raise

            try:
                async with self._session.get(
                    url,
                    params=params,
                    headers=self._headers,
                ) as response:
                    status = response.status
                    headers = response.headers

                    if status == 200:
                        data = await response.json(content_type=None)
                    else:
                        data = None
                        body = await response.text()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await gate.release()
                server_errors += 1

                if server_errors > MAX_SERVER_ERROR_RETRIES:
                    raise ScanError("Could not reach Discord. Try again shortly.")

                await asyncio.sleep(1.0 + server_errors)
                continue
            except BaseException:
                await gate.release()
                raise

            if status == 200:
                await gate.release(headers)
                data.sort(key=lambda message: int(message["id"]))
                return data

            if status == 429:
                retry_after = _retry_after(headers, body)
                await gate.release()
                await gate.penalize(retry_after)

                if str(headers.get("X-RateLimit-Global", "")).lower() == "true":
                    self._pacer.paused_until = max(
                        self._pacer.paused_until,
                        time.monotonic() + retry_after + RESET_PADDING,
                    )

                self.rate_limit_hits += 1
                logger.warning(
                    "Leaderboard hit a 429 on channel %s (retry in %.2fs, hit %d/%d)",
                    channel_id,
                    retry_after,
                    self.rate_limit_hits,
                    MAX_RATE_LIMIT_HITS,
                )

                if self.rate_limit_hits >= MAX_RATE_LIMIT_HITS:
                    raise ScanError(
                        "Stopped because Discord kept rate limiting the scan. "
                        "Try again in a few minutes."
                    )

                continue

            await gate.release(headers)

            if status in (401, 403, 404):
                raise AccessError(
                    "I need View Channel and Read Message History in that channel."
                )

            if status >= 500:
                server_errors += 1

                if server_errors > MAX_SERVER_ERROR_RETRIES:
                    raise ScanError("Discord had a problem. Try again shortly.")

                await asyncio.sleep(1.0 + server_errors)
                continue

            logger.error("Unexpected %s reading channel %s: %s", status, channel_id, body[:200])
            raise ScanError("Discord didn't let me read this channel. Try again shortly.")


def _retry_after(headers: Any, body: str) -> float:
    try:
        return max(0.0, float(headers.get("Retry-After")))
    except (TypeError, ValueError):
        pass

    match = re.search(r'"retry_after"\s*:\s*([0-9.]+)', body or "")

    if match:
        return float(match.group(1))

    return 5.0


# ---------------------------------------------------------------------------
# Scanning
#
#   * Raw REST pages are parsed as dicts (no Message objects), and non-bot
#     messages are dropped after one lookup.
#   * Each window stops at its own end. (channel.history with both `after` and
#     `before` plus oldest_first pages onward past `before`, so every window
#     would re-read the rest of the channel.)
#   * Windows are disjoint snowflake ranges, so nothing is double counted.
#   * Nothing after "now" is scanned.
#
# Intents: this uses REST only, so no gateway intents are needed. Because it only
# reads messages authored by the bot itself, embed contents are returned even
# without the Message Content intent.
# ---------------------------------------------------------------------------


class _Progress:
    """Time-weighted progress of one channel scan (0.0 to 1.0)."""

    def __init__(self) -> None:
        self._fractions: list[float] = []
        self._weights: list[int] = []

    def setup(self, ranges: list[tuple[int, int]]) -> None:
        self._fractions = [0.0] * len(ranges)
        self._weights = [end - start for start, end in ranges]

    def set(self, index: int, fraction: float) -> None:
        self._fractions[index] = max(0.0, min(1.0, fraction))

    def fraction(self) -> float:
        total = sum(self._weights)

        if not total:
            return 0.0

        return sum(f * w for f, w in zip(self._fractions, self._weights)) / total


def _windows(start: datetime, end: datetime) -> list[tuple[int, int]]:
    """Disjoint [start_id, end_id) snowflake ranges covering [start, end)."""
    ranges: list[tuple[int, int]] = []
    step = timedelta(hours=SCAN_WINDOW_HOURS)
    current = start

    while current < end:
        window_end = min(current + step, end)
        ranges.append(
            (
                discord.utils.time_snowflake(current),
                discord.utils.time_snowflake(window_end),
            )
        )
        current = window_end

    return ranges


async def _scan_window(
    fetcher: _Fetcher,
    channel_id: int,
    bot_id: int,
    *,
    start_id: int,
    end_id: int,
    parse: Parser,
    on_progress: Callable[[float], None],
) -> tuple[Counts, int]:
    counts: Counts = {}
    total = 0
    cursor = start_id - 1  # `after` is exclusive; the range is [start, end).
    span = max(1, end_id - start_id)

    while True:
        page = await fetcher.page(channel_id, cursor)

        if not page:
            break

        finished = False

        for message in page:
            message_id = int(message["id"])

            if message_id >= end_id:
                finished = True
                break

            if int((message.get("author") or {}).get("id") or 0) != bot_id:
                continue

            embeds = message.get("embeds")

            if not embeds:
                continue

            result = parse(embeds)

            if result is None:
                continue

            staff_id, amount = result
            counts[staff_id] = counts.get(staff_id, 0) + amount
            total += amount

        if finished or len(page) < PAGE_SIZE:
            break

        cursor = int(page[-1]["id"])
        on_progress((cursor - start_id) / span)

    on_progress(1.0)
    return counts, total


async def _scan_channel(
    fetcher: _Fetcher,
    channel_id: int,
    bot_id: int,
    *,
    start: datetime,
    end: datetime,
    parse: Parser,
    progress: _Progress,
    label: str,
) -> tuple[Counts, int]:
    started = time.perf_counter()

    # Never scan the future part of the month.
    end = min(end, datetime.now(timezone.utc))
    ranges = _windows(start, end) if start < end else []

    progress.setup(ranges)

    semaphore = asyncio.Semaphore(MAX_PARALLEL_WINDOWS)

    async def run(index: int, start_id: int, end_id: int) -> tuple[Counts, int]:
        async with semaphore:
            return await _scan_window(
                fetcher,
                channel_id,
                bot_id,
                start_id=start_id,
                end_id=end_id,
                parse=parse,
                on_progress=lambda fraction: progress.set(index, fraction),
            )

    tasks = [
        asyncio.create_task(run(index, s, e))
        for index, (s, e) in enumerate(ranges)
    ]

    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        # One failed window makes the whole board unusable: stop the rest
        # instead of letting them keep requesting in the background.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    combined: Counts = {}
    total = 0

    for counts, window_total in results:
        for staff_id, count in counts.items():
            combined[staff_id] = combined.get(staff_id, 0) + count
        total += window_total

    logger.info(
        "Leaderboard scan %s: %d windows, %d counted, %.1fs",
        label,
        len(ranges),
        total,
        time.perf_counter() - started,
    )

    return combined, total


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _top(counts: Counts) -> list[tuple[int, int]]:
    return heapq.nlargest(
        TOP_N,
        counts.items(),
        key=lambda item: (item[1], -item[0]),
    )


def _board_text(
    title: str,
    rows: list[tuple[int, int]],
    total: int,
) -> str:
    header = f"## {title}"

    if not rows:
        return f"{header}\n\n*Nothing yet this month.*"

    lines = [
        (
            f"{index}. <@{staff_id}> · "
            f"{round(count * 100 / total) if total else 0}% · "
            f"{count}/{total} done"
        )
        for index, (staff_id, count) in enumerate(rows, 1)
    ]

    return header + "\n\n" + "\n".join(lines)


def _percent(fraction: float, *, done: bool) -> int:
    value = int(fraction * 100)
    return 100 if done else min(99, max(0, value))


def _bar(percent: int) -> str:
    filled = round(BAR_WIDTH * percent / 100)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


class LeaderboardView(discord.ui.View):
    """Paged result view.

    results[i] is a str for an error/notice, or (rows, total) once done.
    """

    def __init__(
        self,
        titles: tuple[str, ...],
        month_start: datetime,
        next_month: datetime,
    ) -> None:
        super().__init__(timeout=900)

        self.titles = titles
        self.results: list[Any] = [None] * len(titles)
        self.month_start = month_start
        self.next_month = next_month
        self.page = 0

    def render_content(self) -> str:
        title = self.titles[self.page]
        state = self.results[self.page]

        if state is None:
            board = f"## {title}\n\n*No data.*"
        elif isinstance(state, str):
            board = f"## {title}\n\n*{state}*"
        else:
            rows, total = state
            board = _board_text(title, rows, total)

        month_info = (
            f"*{self.month_start.strftime('%B %Y')} · "
            f"Resets <t:{int(self.next_month.timestamp())}:R>*"
        )

        return (
            "# Staff Leaderboard\n"
            f"{month_info}\n\n"
            f"{board}\n\n"
            f"*Category {self.page + 1}/{len(self.titles)}*"
        )

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary)
    async def next_category(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.page = (self.page + 1) % len(self.titles)

        await interaction.response.edit_message(
            content=self.render_content(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def build_leaderboard_command(
    *,
    manager: Any,
    owner_id: int,
) -> app_commands.Command:

    lock = asyncio.Lock()

    async def reply(
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    def text_channel(
        guild: discord.Guild,
        channel_id: int,
    ) -> discord.TextChannel | None:
        channel = guild.get_channel(channel_id) if channel_id else None

        return channel if isinstance(channel, discord.TextChannel) else None

    def log_channel(
        guild: discord.Guild,
        channel_id: int,
    ) -> discord.TextChannel | discord.Thread | None:
        channel = guild.get_channel_or_thread(channel_id) if channel_id else None

        return (
            channel
            if isinstance(channel, (discord.TextChannel, discord.Thread))
            else None
        )

    @app_commands.command(
        name="leaderboard",
        description="Top staff this month for creation reviews, booster ranks and deranks.",
    )
    @app_commands.guild_only()
    async def leaderboard(interaction: discord.Interaction) -> None:
        guild = interaction.guild

        if guild is None:
            await reply(interaction, "Run this command inside the server.")
            return

        creation_channel = text_channel(
            guild,
            int(getattr(manager, "creation_approve_channel_id", 0) or 0),
        )
        booster_channel = text_channel(
            guild,
            int(group_scan.BOOSTER_RANK or 0),
        )
        roleshift_channel = log_channel(guild, DERANK_CHANNEL_ID)

        if (
            creation_channel is None
            and booster_channel is None
            and roleshift_channel is None
        ):
            await reply(
                interaction,
                "None of the creation approval, booster rank or roleshift channels are "
                "configured. Check `/settings`.",
            )
            return

        token = getattr(interaction.client.http, "token", None)

        if not token:
            await reply(interaction, "Could not read the bot token for the scan.")
            return

        # Check and acquire with no await in between, so two simultaneous
        # invocations can never both pass.
        if lock.locked():
            await reply(
                interaction,
                "A leaderboard scan is already running. Try again in a moment.",
            )
            return

        await lock.acquire()

        ticker: asyncio.Task[None] | None = None

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)

            bot_id = interaction.client.user.id if interaction.client.user else 0
            month_start, next_month = _month_bounds()
            month_start_ts = int(month_start.timestamp())
            month_end_ts = int(next_month.timestamp())
            review_start = month_start - timedelta(days=REVIEW_LOOKBACK_DAYS)

            no_mentions = discord.AllowedMentions.none()

            titles = (
                "Creation Reviews",
                "Booster Rank Requests",
                "Booster Deranks",
            )

            channels = (creation_channel, booster_channel, roleshift_channel)

            parsers: tuple[Parser, ...] = (
                _make_review_parser(
                    month_start_ts=month_start_ts,
                    month_end_ts=month_end_ts,
                    accepted_only=False,
                    creation_only=True,
                ),
                _make_review_parser(
                    month_start_ts=month_start_ts,
                    month_end_ts=month_end_ts,
                    accepted_only=True,
                    creation_only=False,
                ),
                # A derank message is created at derank time, so it needs no
                # lookback: the message timestamp is the event timestamp.
                _parse_derank,
            )

            starts = (review_start, review_start, month_start)

            view = LeaderboardView(titles, month_start, next_month)
            progresses = [_Progress() for _ in titles]

            def render_loading() -> str:
                fractions: list[float] = []
                lines: list[str] = []

                for index, title in enumerate(titles):
                    if channels[index] is None:
                        continue

                    state = view.results[index]
                    done = state is not None

                    fraction = 1.0 if done else progresses[index].fraction()
                    fractions.append(fraction)

                    if isinstance(state, str):
                        status = "failed"
                    elif done:
                        status = "done"
                    else:
                        status = f"{_percent(fraction, done=False)}%"

                    lines.append(f"{title}: {status}")

                overall_fraction = sum(fractions) / len(fractions) if fractions else 0.0
                overall_done = all(view.results[i] is not None for i, c in enumerate(channels) if c is not None)
                percent = _percent(overall_fraction, done=overall_done)

                return (
                    "# Staff Leaderboard\n"
                    f"*Loading… {percent}%*\n\n"
                    f"`{_bar(percent)}` {percent}%\n\n"
                    + "\n".join(lines)
                )

            async def safe_edit(**kwargs: Any) -> None:
                try:
                    await interaction.edit_original_response(
                        allowed_mentions=no_mentions,
                        **kwargs,
                    )
                except discord.HTTPException:
                    logger.exception("Could not update leaderboard message")

            await safe_edit(content=render_loading())

            async def run_ticker() -> None:
                last = ""

                while True:
                    await asyncio.sleep(PROGRESS_EDIT_INTERVAL)
                    text = render_loading()

                    if text != last:
                        last = text
                        await safe_edit(content=text)

            ticker = asyncio.create_task(run_ticker())

            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                fetcher = _Fetcher(session, token)

                async def run_board(index: int) -> None:
                    channel = channels[index]

                    if channel is None:
                        view.results[index] = "Channel not configured. Check `/settings`."
                        return

                    try:
                        counts, total = await _scan_channel(
                            fetcher,
                            channel.id,
                            bot_id,
                            start=starts[index],
                            end=next_month,
                            parse=parsers[index],
                            progress=progresses[index],
                            label=titles[index],
                        )
                        view.results[index] = (_top(counts), total)
                    except ScanError as exc:
                        view.results[index] = str(exc)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Unexpected leaderboard error: %s", titles[index])
                        view.results[index] = "Something went wrong scanning this channel."

                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(run_board(i) for i in range(len(titles)))),
                        timeout=SCAN_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    for index, state in enumerate(view.results):
                        if state is None and channels[index] is not None:
                            view.results[index] = (
                                "Scan took too long and was stopped. "
                                "Try again when the channel is quieter."
                            )

            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
            ticker = None

            await safe_edit(content=view.render_content(), view=view)

        finally:
            if ticker is not None:
                ticker.cancel()
                await asyncio.gather(ticker, return_exceptions=True)

            lock.release()

    return leaderboard