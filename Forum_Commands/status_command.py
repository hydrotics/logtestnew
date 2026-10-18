from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import discord
from discord import app_commands

from create_log import build_status_embed


@dataclass(frozen=True)
class StatusSnapshot:
    reveal_id: str | None
    requested_users: int
    successful_users: int

    @property
    def percent(self) -> float:
        if self.requested_users <= 0:
            return 0.0
        return min(100.0, self.successful_users * 100.0 / self.requested_users)

    @property
    def completed_display(self) -> str:
        if self.requested_users <= 0:
            return "0"
        return f"{self.successful_users} / {self.requested_users}"

    def progress_bar(self, width: int = 20) -> str:
        width = max(1, int(width))
        filled = min(width, int(self.percent * width / 100.0))
        return "[" + "█" * filled + "░" * (width - filled) + "]"

    def bot_state(
        self,
        waiting: int,
        running: int,
        estimated_wait: float,
        ready: bool,
    ) -> str:
        if not ready:
            return "OFFLINE"
        if waiting >= 10 or estimated_wait >= 20:
            return "OVERLOADED"
        if waiting > 0 or running > 0:
            return "BUSY"
        return "NORMAL"


class StatusTracker:
    def __init__(self) -> None:
        self.reveal_id: str | None = None
        self._requested: set[int] = set()
        self._successful: set[int] = set()

    def reset(self, reveal_id: str | None) -> None:
        self.reveal_id = str(reveal_id) if reveal_id else None
        self._requested.clear()
        self._successful.clear()

    def request(self, user_id: int) -> None:
        self._requested.add(int(user_id))

    def success(self, user_id: int) -> None:
        uid = int(user_id)
        self._requested.add(uid)
        self._successful.add(uid)

    def snapshot(self) -> StatusSnapshot:
        return StatusSnapshot(
            reveal_id=self.reveal_id,
            requested_users=len(self._requested),
            successful_users=len(self._successful),
        )


def build_status_command(*, manager: Any, owner_id: int = 0) -> app_commands.Command:
    def can_manage_forum(user: Any) -> bool:
        """Owner, or a member holding the forum manager role (read live)."""
        if owner_id and getattr(user, "id", 0) == owner_id:
            return True
        role_id = int(getattr(manager, "forum_manager_role_id", 0) or 0)
        return (
            bool(role_id)
            and isinstance(user, discord.Member)
            and any(role.id == role_id for role in user.roles)
        )

    @app_commands.command(
        name="status",
        description="Show the current logging progress.",
    )
    @app_commands.guild_only()
    @app_commands.check(lambda interaction: can_manage_forum(interaction.user))
    async def status(interaction: discord.Interaction) -> None:
        initial_embed = build_status_embed(manager.job)
        configured_colour = getattr(manager, "embed_colour", None)
        if configured_colour is not None:
            initial_embed.colour = configured_colour
        await interaction.response.send_message(
            embed=initial_embed,
            ephemeral=True,
        )

        if not manager.job.running:
            return

        deadline = asyncio.get_running_loop().time() + 14 * 60
        while manager.job.running and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2.5)
            try:
                progress_embed = build_status_embed(manager.job)
                configured_colour = getattr(manager, "embed_colour", None)
                if configured_colour is not None:
                    progress_embed.colour = configured_colour
                await interaction.edit_original_response(embed=progress_embed)
            except discord.HTTPException:
                return

        try:
            final_embed = build_status_embed(manager.job)
            configured_colour = getattr(manager, "embed_colour", None)
            if configured_colour is not None:
                final_embed.colour = configured_colour
            await interaction.edit_original_response(embed=final_embed)
        except discord.HTTPException:
            pass

    return status
