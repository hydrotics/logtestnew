
from __future__ import annotations

import asyncio
import heapq
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.http import Route

import group_scan

logger = logging.getLogger("discord-log-bot.leaderboard")

TOP_N = 5

# Fixed channel containing the booster derank logs.
DERANK_CHANNEL_ID = 1272268411145289831

# Discord message search returns a maximum of 25 results per request.
DERANK_SEARCH_PAGE_SIZE = 25

# A message is created BEFORE it is reviewed, so we look slightly further back than the
# start of the month to catch submissions that were reviewed early this month.
# Creation submissions are re-sent every 24h while pending, so a short buffer is enough.
# Booster requests are never re-sent, so they get a longer buffer.
CREATION_LOOKBACK = timedelta(days=3)
BOOSTER_LOOKBACK = timedelta(days=14)

# Matches the status line the bot writes when a request is reviewed, e.g.
#   **Status:** Accepted by <@123> · <t:1700000000:R>
#   **Status:** Denied by <@123> · <t:1700000000:R> · `reason`
_REVIEW_RE = re.compile(
    r"\*\*Status:\*\*\s*(Accepted|Denied) by <@!?(\d+)>\s*[·\u00b7]\s*<t:(\d+)"
)

# Matches the information label in creation request embeds.
_INFORMATION_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?(?:request\s+)?information(?:\*\*)?\s*:"
)

# Matches the exact staff mention immediately following "Deranked by".
_DERANK_RE = re.compile(
    r"\bDeranked by\s*<@!?(\d+)>"
)

# Matches normal Discord user mentions, excluding roles and channels.
_MENTION_RE = re.compile(r"<@!?(\d+)>")

Counts = dict[int, int]


def _month_bounds() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)

    return start, end


def _message_text(message: discord.Message) -> str:
    """Collect searchable text from the message and all its embeds."""
    parts = [message.content or ""]

    for embed in message.embeds:
        parts.extend([
            embed.title or "",
            embed.description or "",
            getattr(embed, "author", None).name
            if getattr(embed, "author", None) else "",
            getattr(embed, "footer", None).text
            if getattr(embed, "footer", None) else "",
        ])

        for field in embed.fields:
            parts.extend([
                field.name or "",
                field.value or "",
            ])

    return "\n".join(parts)


def _has_creation_information(message: discord.Message) -> bool:
    """Identify creation requests by their information text in the embed."""
    for embed in message.embeds:
        for field in embed.fields:
            field_name = re.sub(
                r"[*_`~]",
                "",
                field.name or "",
            ).strip().casefold()

            field_value = (field.value or "").strip()

            if (
                field_name in ("information", "request information")
                and field_value
            ):
                return True

            if field_value and _INFORMATION_RE.search(field_value):
                return True

        description = embed.description or ""

        if description and _INFORMATION_RE.search(description):
            return True

    return False


async def _tally_reviews(
    channel: discord.TextChannel,
    bot_id: int,
    *,
    month_start: datetime,
    lookback: timedelta,
    accepted_only: bool,
    creation_only: bool,
) -> tuple[Counts, int]:
    """Count creation reviews or booster approvals from the bot's review embeds.

    Creation reviews are identified by their information text.
    Booster requests are identified by the absence of that information text.
    """
    counts: Counts = {}
    total = 0
    start_ts = int(month_start.timestamp())

    async for message in channel.history(
        limit=None,
        after=month_start - lookback,
        oldest_first=False,
    ):
        if message.author.id != bot_id or not message.embeds:
            continue

        # Identify whether this is a creation or booster request.
        has_information = _has_creation_information(message)

        if creation_only and not has_information:
            continue

        if not creation_only and has_information:
            continue

        text = _message_text(message)

        if "by <@" not in text:
            continue

        match = _REVIEW_RE.search(text)

        if match is None:
            continue

        # Booster requests only count when Accepted.
        # Denied and pending booster requests are not counted.
        if accepted_only and match.group(1) != "Accepted":
            continue

        # Creation reviews count both Accepted and Denied.
        # Use the review time written in the embed, not message creation time.
        if int(match.group(3)) < start_ts:
            continue

        staff_id = int(match.group(2))
        counts[staff_id] = counts.get(staff_id, 0) + 1
        total += 1

    return counts, total


def _get_derank_source(embeds: list[dict[str, Any]]) -> str | None:
    """Get the first embed containing the derank marker.

    This works with raw embed data returned by Discord's message-search API.
    It deliberately does not inspect message content.
    """
    for embed in embeds:
        parts = [
            embed.get("title") or "",
            embed.get("description") or "",
        ]

        for field in embed.get("fields", []):
            parts.extend([
                field.get("name") or "",
                field.get("value") or "",
            ])

        author = embed.get("author") or {}
        if author:
            parts.append(author.get("name") or "")

        footer = embed.get("footer") or {}
        if footer:
            parts.append(footer.get("text") or "")

        embed_text = "\n".join(parts)

        if _DERANK_RE.search(embed_text):
            return embed_text

    return None


async def _tally_deranks(
    channel: discord.abc.Messageable,
    bot_id: int,
    *,
    month_start: datetime,
    http: Any,
) -> tuple[Counts, int]:
    """Count deranks using Discord's filtered message-search endpoint.

    Discord filters the results BEFORE returning them:
    - Exact derank channel
    - Exact bot author
    - Messages containing embeds
    - Messages from the current month

    This avoids downloading unrelated channel messages.

    Only the exact staff mention immediately following 'Deranked by'
    receives credit. Unique target mentions before that marker are counted.
    """
    counts: Counts = {}
    total = 0

    if getattr(channel, "id", None) != DERANK_CHANNEL_ID:
        logger.error(
            "Derank search refused: expected channel %s, received channel %s",
            DERANK_CHANNEL_ID,
            getattr(channel, "id", None),
        )
        return counts, total

    guild = getattr(channel, "guild", None)

    if guild is None:
        logger.error("Derank search refused: channel has no guild.")
        return counts, total

    route = Route(
        "GET",
        "/guilds/{guild_id}/messages/search",
        guild_id=guild.id,
    )

    # Discord snowflake corresponding to the beginning of this month.
    min_message_id = discord.utils.time_snowflake(month_start)

    # Search filters are applied by Discord, not after downloading messages.
    base_params = [
        ("limit", str(DERANK_SEARCH_PAGE_SIZE)),
        ("min_id", str(min_message_id)),
        ("channel_id", str(DERANK_CHANNEL_ID)),
        ("author_id", str(bot_id)),
        ("has", "embed"),
        ("sort_by", "timestamp"),
        ("sort_order", "desc"),
    ]

    before_id: str | None = None

    while True:
        params = list(base_params)

        # Cursor pagination: request messages older than the previous page.
        if before_id is not None:
            params.append(("max_id", before_id))

        # Discord may return code 110000 while its search index is preparing.
        data = None

        for attempt in range(3):
            data = await http.request(
                route,
                params=params,
            )

            if not isinstance(data, dict) or data.get("code") != 110000:
                break

            retry_after = data.get("retry_after", 1)

            try:
                delay = min(max(float(retry_after), 0.25), 2.0)
            except (TypeError, ValueError):
                delay = 1.0

            if attempt < 2:
                await asyncio.sleep(delay)

        if not isinstance(data, dict):
            raise RuntimeError(
                "Discord returned an unexpected response from message search."
            )

        if data.get("code") == 110000:
            raise RuntimeError(
                "Discord's message-search index is not ready yet. "
                "Please try the leaderboard again shortly."
            )

        # Discord returns messages as a nested array.
        messages: list[dict[str, Any]] = []

        for group in data.get("messages", []):
            if isinstance(group, list):
                messages.extend(
                    item for item in group
                    if isinstance(item, dict)
                )
            elif isinstance(group, dict):
                messages.append(group)

        if not messages:
            break

        oldest_message_id: int | None = None

        for message_data in messages:
            # Extra safety checks, even though Discord already applies these filters.
            if str(message_data.get("channel_id")) != str(DERANK_CHANNEL_ID):
                continue

            author = message_data.get("author") or {}

            if str(author.get("id")) != str(bot_id):
                continue

            embeds = message_data.get("embeds") or []

            if not embeds:
                continue

            message_id = int(message_data["id"])

            if (
                oldest_message_id is None
                or message_id < oldest_message_id
            ):
                oldest_message_id = message_id

            # Search only the embeds, never the message content.
            content = _get_derank_source(embeds)

            if content is None:
                continue

            # Capture the exact staff mention after "Deranked by".
            match = _DERANK_RE.search(content)

            if match is None:
                continue

            staff_id = int(match.group(1))

            # Only count target mentions before the staff marker.
            target_text = content[:match.start()]

            # Deduplicate targets within this individual derank log.
            deranked_members = set(
                _MENTION_RE.findall(target_text)
            )

            entries = len(deranked_members)

            if entries == 0:
                continue

            counts[staff_id] = counts.get(staff_id, 0) + entries
            total += entries

        # Stop if Discord gave us no usable message IDs.
        if oldest_message_id is None:
            break

        # Move the cursor backwards for the next page.
        before_id = str(oldest_message_id)

        # If fewer than a full page came back, there are no more results.
        if len(messages) < DERANK_SEARCH_PAGE_SIZE:
            break

    return counts, total


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
    """Build a normal message leaderboard for one category."""
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
    """Interactive three-category leaderboard with a next-category button."""

    def __init__(
        self,
        boards: tuple[
            tuple[str, list[tuple[int, int]], int],
            ...,
        ],
        month_start: datetime,
        next_month: datetime,
    ) -> None:
        super().__init__(timeout=900)

        self.boards = boards
        self.month_start = month_start
        self.next_month = next_month
        self.page = 0

    def render_content(self) -> str:
        """Render the currently selected category as a normal Discord message."""
        title, rows, total = self.boards[self.page]

        month_info = (
            f"*{self.month_start.strftime('%B %Y')} · "
            f"Resets <t:{int(self.next_month.timestamp())}:R>*"
        )

        leaderboard = _board_text(title, rows, total)

        return (
            "# Staff Leaderboard\n"
            f"{month_info}\n\n"
            f"{leaderboard}\n\n"
            f"*Category {self.page + 1}/3*"
        )

    @discord.ui.button(
        label=">",
        style=discord.ButtonStyle.secondary,
    )
    async def next_category(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        """Move to the next category and update the existing message."""
        self.page = (self.page + 1) % len(self.boards)

        await interaction.response.edit_message(
            content=self.render_content(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


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

        return (
            channel
            if isinstance(channel, discord.TextChannel)
            else None
        )

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
            await reply(
                interaction,
                "Run this command inside the server.",
            )
            return

        creation_channel = text_channel(
            guild,
            int(getattr(manager, "creation_approve_channel_id", 0) or 0),
        )

        booster_channel = text_channel(
            guild,
            int(group_scan.BOOSTER_RANK or 0),
        )

        # Deranks ALWAYS use the specified channel ID.
        roleshift_channel = log_channel(
            guild,
            DERANK_CHANNEL_ID,
        )

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

        if lock.locked():
            await reply(
                interaction,
                "A leaderboard scan is already running. Try again in a moment.",
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        bot_id = interaction.client.user.id if interaction.client.user else 0

        month_start, next_month = _month_bounds()

        async with lock:
            try:
                empty: tuple[Counts, int] = ({}, 0)

                async def creation_job() -> tuple[Counts, int]:
                    if creation_channel is None:
                        return empty

                    return await _tally_reviews(
                        creation_channel,
                        bot_id,
                        month_start=month_start,
                        lookback=CREATION_LOOKBACK,
                        accepted_only=False,
                        creation_only=True,
                    )

                async def booster_job() -> tuple[Counts, int]:
                    if booster_channel is None:
                        return empty

                    return await _tally_reviews(
                        booster_channel,
                        bot_id,
                        month_start=month_start,
                        lookback=BOOSTER_LOOKBACK,
                        accepted_only=True,
                        creation_only=False,
                    )

                async def derank_job() -> tuple[Counts, int]:
                    if roleshift_channel is None:
                        return empty

                    return await _tally_deranks(
                        roleshift_channel,
                        bot_id,
                        month_start=month_start,
                        http=interaction.client.http,
                    )

                (
                    (creation_counts, creation_total),
                    (booster_counts, booster_total),
                    (derank_counts, derank_total),
                ) = await asyncio.gather(
                    creation_job(),
                    booster_job(),
                    derank_job(),
                )

            except discord.Forbidden:
                await reply(
                    interaction,
                    "I need View Channel and Read Message History in the creation, "
                    "booster rank and roleshift channels.",
                )
                return

            except RuntimeError as error:
                logger.error(
                    "Leaderboard message search failed: %s",
                    error,
                )

                await reply(
                    interaction,
                    "Discord's derank message search isn't ready yet. "
                    "Please try again shortly.",
                )
                return

            except discord.HTTPException:
                logger.exception(
                    "Could not read channel history or search messages for the leaderboard"
                )

                await reply(
                    interaction,
                    "Discord didn't let me read or search the channels. "
                    "Try again shortly.",
                )
                return

        creation_rows = _top(creation_counts)
        booster_rows = _top(booster_counts)
        derank_rows = _top(derank_counts)

        boards = (
            ("Creation Reviews", creation_rows, creation_total),
            ("Booster Rank Requests", booster_rows, booster_total),
            ("Booster Deranks", derank_rows, derank_total),
        )

        view = LeaderboardView(
            boards=boards,
            month_start=month_start,
            next_month=next_month,
        )

        await interaction.followup.send(
            content=view.render_content(),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    return leaderboard
