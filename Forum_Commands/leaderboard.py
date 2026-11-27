
from __future__ import annotations

import asyncio
import heapq
import logging
import re
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands

import group_scan

logger = logging.getLogger("discord-log-bot.leaderboard")

TOP_N = 5

# Fixed channel containing the booster derank logs.
DERANK_CHANNEL_ID = 1272268411145289831

# Matches the status line the bot writes when a request is reviewed.
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

_COUNTS_EMPTY: tuple[dict[int, int], int] = ({}, 0)

Counts = dict[int, int]


def _month_bounds() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)

    return start, end


def _history_after(timestamp: datetime) -> discord.Object:
    """Create a snowflake marking the beginning of the requested period."""
    return discord.Object(
        id=discord.utils.time_snowflake(timestamp)
    )


def _embed_text(embeds: list[discord.Embed]) -> str:
    """Collect searchable text from embeds only."""
    parts: list[str] = []

    for embed in embeds:
        parts.extend([
            embed.title or "",
            embed.description or "",
            embed.author.name if embed.author else "",
            embed.footer.text if embed.footer else "",
        ])

        for field in embed.fields:
            parts.extend([
                field.name or "",
                field.value or "",
            ])

    return "\n".join(parts)


def _has_creation_information(message: discord.Message) -> bool:
    """Identify creation requests using their information fields."""
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


def _get_derank_source(embeds: list[dict[str, Any]]) -> str | None:
    """Get the first embed containing the derank marker."""
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


async def _tally_reviews(
    channel: discord.TextChannel,
    bot_id: int,
    *,
    month_start: datetime,
    accepted_only: bool,
    creation_only: bool,
) -> tuple[Counts, int]:
    """Count this month's creation reviews or booster approvals."""
    counts: Counts = {}
    total = 0
    start_ts = int(month_start.timestamp())

    # Only retrieve messages from the beginning of this month onwards.
    after_id = _history_after(month_start)

    async for message in channel.history(
        limit=None,
        after=after_id,
        oldest_first=True,
    ):
        # Immediately discard messages that aren't authored by this bot.
        if message.author.id != bot_id:
            continue

        # Ignore bot messages without embeds.
        if not message.embeds:
            continue

        # Classify creation requests separately from booster requests.
        has_information = _has_creation_information(message)

        if creation_only and not has_information:
            continue

        if not creation_only and has_information:
            continue

        text = _embed_text(message.embeds)

        if not text:
            continue

        match = _REVIEW_RE.search(text)

        if match is None:
            continue

        # Booster requests only count when Accepted.
        if accepted_only and match.group(1) != "Accepted":
            continue

        # Use the review timestamp written in the embed.
        if int(match.group(3)) < start_ts:
            continue

        staff_id = int(match.group(2))
        counts[staff_id] = counts.get(staff_id, 0) + 1
        total += 1

    return counts, total


async def _tally_deranks(
    channel: discord.TextChannel | discord.Thread,
    bot_id: int,
    *,
    month_start: datetime,
) -> tuple[Counts, int]:
    """Count this month's deranks from the bot's own embeds."""
    counts: Counts = {}
    total = 0

    if getattr(channel, "id", None) != DERANK_CHANNEL_ID:
        logger.error(
            "Derank scan refused: expected channel %s, received channel %s",
            DERANK_CHANNEL_ID,
            getattr(channel, "id", None),
        )
        return counts, total

    if getattr(channel, "guild", None) is None:
        logger.error("Derank scan refused: channel has no guild.")
        return counts, total

    # Only retrieve messages from the beginning of this month onwards.
    after_id = _history_after(month_start)

    async for message in channel.history(
        limit=None,
        after=after_id,
        oldest_first=True,
    ):
        # Immediately discard messages that aren't authored by this bot.
        if message.author.id != bot_id:
            continue

        if not message.embeds:
            continue

        content = _embed_text(message.embeds)

        if not content:
            continue

        match = _DERANK_RE.search(content)

        if match is None:
            continue

        # Exact staff mention immediately following "Deranked by".
        staff_id = int(match.group(1))

        # Only count target mentions before the staff marker.
        target_text = content[:match.start()]

        # Deduplicate targets within this individual derank log.
        deranked_members = set(_MENTION_RE.findall(target_text))
        entries = len(deranked_members)

        if entries == 0:
            continue

        counts[staff_id] = counts.get(staff_id, 0) + entries
        total += entries

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
        """Render the currently selected category."""
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
        channel = (
            guild.get_channel_or_thread(channel_id)
            if channel_id
            else None
        )

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
                # Sequential scans keep peak memory and concurrent network activity low.
                if creation_channel is not None:
                    creation_counts, creation_total = await _tally_reviews(
                        creation_channel,
                        bot_id,
                        month_start=month_start,
                        accepted_only=False,
                        creation_only=True,
                    )
                else:
                    creation_counts, creation_total = _COUNTS_EMPTY

                if booster_channel is not None:
                    booster_counts, booster_total = await _tally_reviews(
                        booster_channel,
                        bot_id,
                        month_start=month_start,
                        accepted_only=True,
                        creation_only=False,
                    )
                else:
                    booster_counts, booster_total = _COUNTS_EMPTY

                if roleshift_channel is not None:
                    derank_counts, derank_total = await _tally_deranks(
                        roleshift_channel,
                        bot_id,
                        month_start=month_start,
                    )
                else:
                    derank_counts, derank_total = _COUNTS_EMPTY

            except discord.Forbidden:
                await reply(
                    interaction,
                    "I need View Channel and Read Message History in the creation, "
                    "booster rank and roleshift channels.",
                )
                return

            except discord.HTTPException:
                logger.exception(
                    "Could not read channel history for the leaderboard"
                )

                await reply(
                    interaction,
                    "Discord didn't let me read the channel history. "
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
