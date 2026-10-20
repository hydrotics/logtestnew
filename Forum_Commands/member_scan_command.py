from __future__ import annotations

import logging
import math
import re
from typing import Any, Callable

import discord
from discord import app_commands

logger = logging.getLogger("discord-log-bot.member_scan")

ID_PATTERN = re.compile(rb"(?<!\d)\d{17,19}(?!\d)")
MAX_FILE_BYTES = 2 * 1024 * 1024
PAGE_SIZE = 8
BULK_BAN_CHUNK = 200
VIEW_TIMEOUT = 600
MAX_REASON_LENGTH = 512

GetScanSettings = Callable[[], tuple[int, str]]


class ExcludeButton(discord.ui.Button):
    def __init__(self, scan: "ScanView", user_id: int, excluded: bool) -> None:
        super().__init__(
            label="Include" if excluded else "Exclude",
            style=discord.ButtonStyle.secondary
            if excluded
            else discord.ButtonStyle.danger,
        )
        self.scan = scan
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        scan = self.scan
        if self.user_id in scan.excluded:
            scan.excluded.discard(self.user_id)
        else:
            scan.excluded.add(self.user_id)
        scan.confirming = False
        scan.render()
        await interaction.response.edit_message(view=scan)


class ScanView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        guild: discord.Guild,
        user_ids: list[int],
        requester_id: int,
        reason: str,
        source_id: int,
        colour: discord.Colour,
        scanned: int,
        skipped: int,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.guild = guild
        self.user_ids = user_ids
        self.requester_id = requester_id
        self.reason = reason[:MAX_REASON_LENGTH]
        self.source_id = source_id
        self.colour = colour
        self.scanned = scanned
        self.skipped = skipped
        self.page = 0
        self.pages = max(1, math.ceil(len(user_ids) / PAGE_SIZE))
        self.excluded: set[int] = set()
        self.confirming = False
        self.finished = False
        self.origin: discord.Interaction | None = None
        self.render()

    def _single(self, text: str) -> None:
        self.clear_items()
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text), accent_colour=self.colour
            )
        )

    def render(self) -> None:
        self.clear_items()
        total = len(self.user_ids)
        active = total - len(self.excluded)
        start = self.page * PAGE_SIZE

        header = (
            "## Member scan\n"
            + (f"Source server: `{self.source_id}`\n" if self.source_id else "")
            + f"IDs scanned: **{self.scanned}** · Matches: **{total}** · "
            f"Excluded: **{len(self.excluded)}** · To ban: **{active}**\n"
            f"Page {self.page + 1}/{self.pages}"
        )
        if self.skipped:
            header += (
                f"\n-# {self.skipped} match(es) hidden: protected or above the bot's role."
            )

        container = discord.ui.Container(accent_colour=self.colour)
        container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.Separator())

        for user_id in self.user_ids[start : start + PAGE_SIZE]:
            member = self.guild.get_member(user_id)
            is_excluded = user_id in self.excluded
            if member is not None:
                text = (
                    f"{member.mention} · `{user_id}`\n"
                    f"-# {discord.utils.escape_markdown(member.name)} · "
                    f"joined <t:{int(member.joined_at.timestamp())}:R>"
                    if member.joined_at
                    else f"{member.mention} · `{user_id}`\n"
                    f"-# {discord.utils.escape_markdown(member.name)}"
                )
            else:
                text = f"`{user_id}`\n-# no longer in this server"
            if is_excluded:
                text = f"~~{text}~~"
            container.add_item(
                discord.ui.Section(
                    text, accessory=ExcludeButton(self, user_id, is_excluded)
                )
            )

        container.add_item(discord.ui.Separator())

        prev_button = discord.ui.Button(
            label="Prev",
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0,
        )
        prev_button.callback = self.on_prev
        next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.pages - 1,
        )
        next_button.callback = self.on_next
        ban_button = discord.ui.Button(
            label=(
                f"Confirm ban ({active})" if self.confirming else f"Ban all ({active})"
            ),
            style=discord.ButtonStyle.danger,
            disabled=active == 0,
        )
        ban_button.callback = self.on_ban
        cancel_button = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.secondary
        )
        cancel_button.callback = self.on_cancel

        container.add_item(
            discord.ui.ActionRow(prev_button, next_button, ban_button, cancel_button)
        )
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Only the person who ran this scan can use it.", ephemeral=True
        )
        return False

    async def on_prev(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self.confirming = False
        self.render()
        await interaction.response.edit_message(view=self)

    async def on_next(self, interaction: discord.Interaction) -> None:
        self.page = min(self.pages - 1, self.page + 1)
        self.confirming = False
        self.render()
        await interaction.response.edit_message(view=self)

    async def on_cancel(self, interaction: discord.Interaction) -> None:
        self.finished = True
        self.stop()
        self._single("Scan cancelled. No members were banned.")
        await interaction.response.edit_message(view=self)

    async def on_ban(self, interaction: discord.Interaction) -> None:
        if not self.confirming:
            self.confirming = True
            self.render()
            await interaction.response.edit_message(view=self)
            return

        targets = [uid for uid in self.user_ids if uid not in self.excluded]
        self.finished = True
        self.stop()
        self._single(f"Banning **{len(targets)}** member(s)...")
        await interaction.response.edit_message(view=self)

        banned, failed = await self._ban(targets)

        text = f"## Scan complete\nBanned: **{banned}**"
        if failed:
            shown = ", ".join(f"`{uid}`" for uid in failed[:20])
            more = f" and {len(failed) - 20} more" if len(failed) > 20 else ""
            text += f"\nFailed: **{len(failed)}** ({shown}{more})"
        if self.excluded:
            text += f"\nExcluded: **{len(self.excluded)}**"
        self._single(text)
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            logger.debug("Could not update scan result message", exc_info=True)

    async def _ban(self, targets: list[int]) -> tuple[int, list[int]]:
        banned = 0
        failed: list[int] = []
        for index in range(0, len(targets), BULK_BAN_CHUNK):
            objects = [
                discord.Object(id=uid)
                for uid in targets[index : index + BULK_BAN_CHUNK]
            ]
            try:
                result = await self.guild.bulk_ban(
                    objects, reason=self.reason, delete_message_seconds=0
                )
                banned += len(result.banned)
                failed.extend(user.id for user in result.failed)
                continue
            except (AttributeError, discord.HTTPException):
                logger.info("Bulk ban unavailable for this chunk; banning individually.")
            for obj in objects:
                try:
                    await self.guild.ban(
                        obj, reason=self.reason, delete_message_seconds=0
                    )
                    banned += 1
                except discord.HTTPException:
                    failed.append(obj.id)
        return banned, failed

    async def on_timeout(self) -> None:
        if self.finished or self.origin is None:
            return
        self._single("Scan expired. Run `/member_scan` again.")
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass


def build_member_scan_command(
    *,
    manager: Any,
    owner_id: int,
    get_scan_settings: GetScanSettings,
) -> app_commands.Command:
    @app_commands.command(
        name="member_scan",
        description="Find members of this server whose IDs appear in a blacklist file.",
    )
    @app_commands.describe(
        file="Text or CSV file containing user IDs from the blacklisted server."
    )
    @app_commands.guild_only()
    async def member_scan(
        interaction: discord.Interaction, file: discord.Attachment
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Run this command inside the server.", ephemeral=True
            )
            return
        if not guild.me.guild_permissions.ban_members:
            await interaction.response.send_message(
                "I need the Ban Members permission to run a scan.", ephemeral=True
            )
            return
        if file.size > MAX_FILE_BYTES:
            await interaction.response.send_message(
                "That file is too large. The limit is 2 MB.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            data = await file.read()
        except discord.HTTPException:
            await interaction.followup.send(
                "I could not download that file.", ephemeral=True
            )
            return

        ids = {int(match) for match in ID_PATTERN.findall(data)}
        del data
        if not ids:
            await interaction.followup.send(
                "No valid user IDs were found in that file.", ephemeral=True
            )
            return

        if not guild.chunked:
            await guild.chunk()

        me = guild.me
        protected = {owner_id, interaction.user.id, me.id, guild.owner_id}
        matches: list[int] = []
        skipped = 0
        for user_id in ids:
            member = guild.get_member(user_id)
            if member is None:
                continue
            if user_id in protected or member.top_role >= me.top_role:
                skipped += 1
                continue
            matches.append(user_id)
        matches.sort()

        if not matches:
            note = f" {skipped} match(es) were protected." if skipped else ""
            await interaction.followup.send(
                f"Scanned **{len(ids)}** IDs. No bannable members found.{note}",
                ephemeral=True,
            )
            return

        source_id, reason = get_scan_settings()
        view = ScanView(
            guild=guild,
            user_ids=matches,
            requester_id=interaction.user.id,
            reason=reason,
            source_id=source_id,
            colour=getattr(manager, "embed_colour", discord.Colour(0x242429)),
            scanned=len(ids),
            skipped=skipped,
        )
        view.origin = interaction
        await interaction.followup.send(view=view, ephemeral=True)

    return member_scan
