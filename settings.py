from __future__ import annotations

import logging
from typing import Any, Callable

import discord
from discord import app_commands

logger = logging.getLogger("discord-log-bot.settings")

ApplySettings = Callable[[int, int, int], None]


def build_settings_command(
    *,
    owner_id: int,
    manager: Any,
    apply_settings: ApplySettings,
) -> app_commands.Command:
    """Build the owner-only /settings command and its configuration modal.

    The modal saves a forum channel, an optional staff role (blank means
    owner-only access), and the guild these settings belong to.
    """

    class BotSettingsModal(discord.ui.Modal, title="Bot Settings"):
        forum_channel = discord.ui.Label(
            text="Forum channel",
            description="Choose the forum where member logs should be created.",
            component=discord.ui.ChannelSelect(
                channel_types=[discord.ChannelType.forum],
                placeholder="Select a forum channel",
                min_values=1,
                max_values=1,
                required=True,
            ),
        )
        staff_role = discord.ui.Label(
            text="Staff role",
            description="Members with this role can use bot commands. Leave blank for owner-only access.",
            component=discord.ui.RoleSelect(
                placeholder="Select a staff role (optional)",
                min_values=0,
                max_values=1,
                required=False,
            ),
        )

        def __init__(self) -> None:
            super().__init__(timeout=300)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            # Defence in depth: only the owner may submit or replay this modal.
            if interaction.user.id != owner_id:
                await interaction.response.send_message(
                    "Only the configured bot owner can change settings.",
                    ephemeral=True,
                )
                return

            guild = interaction.guild
            if guild is None:
                await interaction.response.send_message(
                    "Run this command inside the server you want to configure.",
                    ephemeral=True,
                )
                return

            if manager.job.running or manager.lock.locked():
                await interaction.response.send_message(
                    "Settings cannot be changed while a logging job is running. Try again when it finishes.",
                    ephemeral=True,
                )
                return

            channel_values = self.forum_channel.component.values
            if not channel_values:
                await interaction.response.send_message(
                    "Choose a forum channel before submitting these settings.",
                    ephemeral=True,
                )
                return

            selected_channel_id = int(channel_values[0].id)
            try:
                forum = guild.get_channel(selected_channel_id)
                if forum is None:
                    forum = await interaction.client.fetch_channel(selected_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await interaction.response.send_message(
                    "I could not access that channel. Make sure it belongs to this server and try again.",
                    ephemeral=True,
                )
                return

            if not isinstance(forum, discord.ForumChannel) or forum.guild.id != guild.id:
                await interaction.response.send_message(
                    "The selected channel must be a forum channel in this server.",
                    ephemeral=True,
                )
                return

            role_values = self.staff_role.component.values
            staff_role_id = int(role_values[0].id) if role_values else 0
            if staff_role_id and guild.get_role(staff_role_id) is None:
                await interaction.response.send_message(
                    "The selected staff role could not be found in this server.",
                    ephemeral=True,
                )
                return

            try:
                apply_settings(forum.id, staff_role_id, guild.id)
            except Exception:
                logger.exception("Could not save bot settings")
                await interaction.response.send_message(
                    "I could not save those settings. Check the bot logs and try again.",
                    ephemeral=True,
                )
                return

            role_text = f"<@&{staff_role_id}>" if staff_role_id else "not set (owner-only access)"
            await interaction.response.send_message(
                "**Bot settings saved.**\n"
                f"Forum channel: {forum.mention}\n"
                f"Staff role: {role_text}\n"
                "Only the configured owner can change these settings.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @app_commands.command(
        name="settings",
        description="Configure the bot's forum channel and staff role (owner only).",
    )
    @app_commands.guild_only()
    async def settings(interaction: discord.Interaction) -> None:
        if interaction.user.id != owner_id:
            await interaction.response.send_message(
                "Only the configured bot owner can use `/settings`.",
                ephemeral=True,
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "Run this command inside the server you want to configure.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(BotSettingsModal())

    return settings