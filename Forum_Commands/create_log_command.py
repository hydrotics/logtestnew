from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands

from create_log import monitor_job_message

logger = logging.getLogger("discord-log-bot.create_log_command")


def build_create_log_commands(
    *,
    manager: Any,
    owner_id: int,
) -> app_commands.Command:
    def can_manage_forum(user: Any) -> bool:
        """Owner, or a member holding the forum manager role (read live)."""
        if getattr(user, "id", 0) == owner_id:
            return True
        role_id = int(getattr(manager, "forum_manager_role_id", 0) or 0)
        return (
            bool(role_id)
            and isinstance(user, discord.Member)
            and any(role.id == role_id for role in user.roles)
        )

    class ConfirmCreateLogView(discord.ui.View):
        def __init__(
            self,
            pending_members: list[discord.Member],
            command_owner_id: int,
            origin: discord.Interaction,
        ) -> None:
            super().__init__(timeout=60)
            self.pending_members = pending_members
            self.command_owner_id = command_owner_id
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
            if interaction.user.id != self.command_owner_id:
                await interaction.response.send_message(
                    "Only the person who started this command can confirm it.",
                    ephemeral=True,
                )
                return

            if not can_manage_forum(interaction.user):
                await interaction.response.send_message(
                    "You need the forum manager role to confirm this action.",
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
            if manager.job.running:
                await interaction.edit_original_response(
                    content="Another logging job is already running.",
                    embed=None,
                    view=None,
                )
                return

            await manager.start_member_job(pending_members)
            await monitor_job_message(manager, interaction, "Creating member logs")
        except Exception as exc:
            logger.exception("Confirmed create-log job failed to start")
            try:
                await interaction.edit_original_response(
                    content=f"Could not start member logging: `{exc}`",
                    embed=None,
                    view=None,
                )
            except discord.HTTPException:
                logger.debug(
                    "Could not report create-log startup error", exc_info=True
                )

    @app_commands.command(
        name="create_log",
        description="Create a forum post for every member with the configured role.",
    )
    @app_commands.guild_only()
    @app_commands.check(lambda interaction: can_manage_forum(interaction.user))
    async def create_log(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            forum = await manager.get_forum_channel()

            if interaction.guild_id != forum.guild.id or interaction.guild is None:
                await interaction.edit_original_response(
                    content="This command can only be used in the configured server."
                )
                return

            if manager.job.running:
                await interaction.edit_original_response(
                    content="Another logging job is already running."
                )
                return

            members = await manager.fetch_role_members(interaction.guild)
            if not members:
                await interaction.edit_original_response(
                    content=(
                        "No members currently have the configured role. "
                        "No logs were created."
                    )
                )
                return

            pending, already_done = await manager.get_pending_members(members, forum)
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
                command_owner_id=interaction.user.id,
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

    return create_log
