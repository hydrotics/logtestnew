from __future__ import annotations

import asyncio
import heapq
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import discord
from discord import app_commands

import group_scan

logger = logging.getLogger("discord-log-bot.leaderboard")

TOP_N = 5

DERANK_CHANNEL_ID = 1272268411145289831

# ---------------------------------------------------------------------------
# Scan tuning
# ---------------------------------------------------------------------------

# The month is cut into windows of this many hours, and windows are scanned
# concurrently. Smaller windows balance load better on busy channels, but
# every window costs at least one request.
SCAN_WINDOW_HOURS = 12

# Max in-flight windows per channel. Lower this if you see 429 warnings.
MAX_PARALLEL_WINDOWS = 8

# A review message is posted before it is reviewed, so requests posted shortly
# before the month began can still be reviewed inside it. These extra days are
# scanned, but only reviews whose own <t:...> timestamp is inside the month are
# counted. Set this to the longest time a request normally sits pending.
REVIEW_LOOKBACK_DAYS = 3

PAGE_SIZE = 100          # Discord's hard maximum per history request.
MAX_RETRIES = 3          # Retries for transient 5xx errors only.

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
# Scanning
#
# Why this is fast without storage, caching or message intents:
#   * Raw REST pages are read straight from the HTTP client. No Message objects
#     are built, and non-bot messages are discarded after one dict lookup.
#   * Each window stops at its own end. (channel.history with both `after` and
#     `before` plus oldest_first pages onward past `before` and filters
#     client-side, so every window would re-read the rest of the channel.)
#   * Windows are disjoint snowflake ranges scanned concurrently, and the three
#     boards (different channels, different rate-limit buckets) run at once.
#   * Nothing after "now" is scanned.
#
# Intents: reading history over REST needs no gateway intents, and because this
# only reads messages authored by the bot itself, embed contents are returned
# even without the Message Content intent.
# ---------------------------------------------------------------------------


def _windows(
    start: datetime,
    end: datetime,
) -> list[tuple[int, int]]:
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


async def _fetch_page(
    http: Any,
    channel_id: int,
    after_id: int,
) -> list[dict[str, Any]]:
    for attempt in range(MAX_RETRIES):
        try:
            page = await http.logs_from(channel_id, PAGE_SIZE, after=after_id)
            break
        except discord.HTTPException as exc:
            if exc.status < 500 or attempt == MAX_RETRIES - 1:
                raise
            await asyncio.sleep(1 + attempt)

    # Discord returns newest first even when paging with `after`.
    page.sort(key=lambda message: int(message["id"]))
    return page


async def _scan_window(
    http: Any,
    channel_id: int,
    bot_id: int,
    *,
    start_id: int,
    end_id: int,
    parse: Parser,
) -> tuple[Counts, int]:
    counts: Counts = {}
    total = 0
    cursor = start_id - 1  # `after` is exclusive; the range is [start, end).

    while True:
        page = await _fetch_page(http, channel_id, cursor)

        if not page:
            break

        for message in page:
            message_id = int(message["id"])

            if message_id >= end_id:
                return counts, total

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

        if len(page) < PAGE_SIZE:
            break

        cursor = int(page[-1]["id"])

    return counts, total


async def _scan_channel(
    http: Any,
    channel_id: int,
    bot_id: int,
    *,
    start: datetime,
    end: datetime,
    parse: Parser,
    label: str,
) -> tuple[Counts, int]:
    started = time.perf_counter()

    # Never scan the future part of the month.
    end = min(end, datetime.now(timezone.utc))
    ranges = _windows(start, end) if start < end else []

    semaphore = asyncio.Semaphore(MAX_PARALLEL_WINDOWS)

    async def run(start_id: int, end_id: int) -> tuple[Counts, int]:
        async with semaphore:
            return await _scan_window(
                http,
                channel_id,
                bot_id,
                start_id=start_id,
                end_id=end_id,
                parse=parse,
            )

    tasks = [asyncio.create_task(run(s, e)) for s, e in ranges]

    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        # One failed window makes the whole board unusable: stop the rest
        # instead of letting them keep hammering the API in the background.
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


class LeaderboardView(discord.ui.View):
    """Paged view. Each page fills in as its scan finishes.

    results[i] is None while pending, a str for an error/notice, or
    (rows, total) once done.
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
            board = f"## {title}\n\n*Scanning… this page updates when it's ready.*"
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

        # Check and acquire with no await in between, so two simultaneous
        # invocations can never both pass.
        if lock.locked():
            await reply(
                interaction,
                "A leaderboard scan is already running. Try again in a moment.",
            )
            return

        await lock.acquire()

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)

            http = interaction.client.http
            bot_id = interaction.client.user.id if interaction.client.user else 0
            month_start, next_month = _month_bounds()

            view = LeaderboardView(
                titles=(
                    "Creation Reviews",
                    "Booster Rank Requests",
                    "Booster Deranks",
                ),
                month_start=month_start,
                next_month=next_month,
            )

            no_mentions = discord.AllowedMentions.none()

            await interaction.followup.send(
                content=view.render_content(),
                view=view,
                ephemeral=True,
                allowed_mentions=no_mentions,
            )

            async def refresh() -> None:
                try:
                    await interaction.edit_original_response(
                        content=view.render_content(),
                        view=view,
                        allowed_mentions=no_mentions,
                    )
                except discord.HTTPException:
                    logger.exception("Could not update leaderboard message")

            async def run_board(
                index: int,
                channel: discord.TextChannel | discord.Thread | None,
                *,
                start: datetime,
                parse: Parser,
                label: str,
            ) -> None:
                if channel is None:
                    view.results[index] = "Channel not configured. Check `/settings`."
                else:
                    try:
                        counts, total = await _scan_channel(
                            http,
                            channel.id,
                            bot_id,
                            start=start,
                            end=next_month,
                            parse=parse,
                            label=label,
                        )
                        view.results[index] = (_top(counts), total)
                    except discord.Forbidden:
                        view.results[index] = (
                            "I need View Channel and Read Message History in that channel."
                        )
                    except discord.HTTPException:
                        logger.exception("Leaderboard scan failed: %s", label)
                        view.results[index] = (
                            "Discord didn't let me read this channel. Try again shortly."
                        )
                    except Exception:
                        logger.exception("Unexpected leaderboard error: %s", label)
                        view.results[index] = "Something went wrong scanning this channel."

                await refresh()

            month_start_ts = int(month_start.timestamp())
            month_end_ts = int(next_month.timestamp())
            review_start = month_start - timedelta(days=REVIEW_LOOKBACK_DAYS)

            await asyncio.gather(
                run_board(
                    0,
                    creation_channel,
                    start=review_start,
                    parse=_make_review_parser(
                        month_start_ts=month_start_ts,
                        month_end_ts=month_end_ts,
                        accepted_only=False,
                        creation_only=True,
                    ),
                    label="creation",
                ),
                run_board(
                    1,
                    booster_channel,
                    start=review_start,
                    parse=_make_review_parser(
                        month_start_ts=month_start_ts,
                        month_end_ts=month_end_ts,
                        accepted_only=True,
                        creation_only=False,
                    ),
                    label="booster",
                ),
                # A derank message is created at derank time, so it needs no
                # lookback: the message timestamp is the event timestamp.
                run_board(
                    2,
                    roleshift_channel,
                    start=month_start,
                    parse=_parse_derank,
                    label="derank",
                ),
            )
        finally:
            lock.release()

    return leaderboard