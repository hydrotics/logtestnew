
from __future__ import annotations

import asyncio
import heapq
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands

import group_scan

logger = logging.getLogger("discord-log-bot.leaderboard")

TOP_N = 5

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

# Roleshift log messages end with "... · Deranked by <@staff>" and list one or more
# deranked members as mentions before it.
_DERANK_RE = re.compile(r"Deranked by <@!?(\d+)>")
_MENTION_RE = re.compile(r"<@!?\d+>")

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
            getattr(embed.author, "name", "") or "",
            getattr(embed.footer, "text", "") or "",
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


async def _tally_deranks(
    channel: discord.abc.Messageable,
    bot_id: int,
    *,
    month_start: datetime,
) -> tuple[Counts, int]:
    """Count deranks from the bot's own roleshift log messages posted this month.

    Deranked by is checked in both message content and embed content.
    """
    counts: Counts = {}
    total = 0

    async for message in channel.history(
        limit=None,
        after=month_start,
        oldest_first=False,
    ):
        if message.author.id != bot_id:
            continue

        content = _message_text(message)

        if not content or "Deranked by" not in content:
            continue

        match = _DERANK_RE.search(content)
        if match is None:
            continue

        # One message can log several members; every mention except the staff one counts.
        entries = max(1, len(_MENTION_RE.findall(content)) - 1)

        staff_id = int(match.group(1))
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
        f"{index}. <@{staff_id}> · {round(count * 100 / total)}%"
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
        return channel if isinstance(channel, discord.TextChannel) else None

    def log_channel(
        guild: discord.Guild,
        channel_id: int,
    ) -> discord.TextChannel | discord.Thread | None:
        channel = guild.get_channel_or_thread(channel_id) if channel_id else None
        return channel if isinstance(channel, (discord.TextChannel, discord.Thread)) else None

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

        roleshift_channel = log_channel(
            guild,
            int(group_scan.ROLESHIFT_CHANNEL or 0),
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

        await interaction.response.defer(ephemeral=True, thinking=True)

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

            except discord.HTTPException:
                logger.exception(
                    "Could not read channel history for the leaderboard"
                )
                await reply(
                    interaction,
                    "Discord didn't let me read the channels. Try again shortly.",
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
