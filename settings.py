from __future__ import annotations

import logging
from typing import Any, Callable

import discord
from discord import app_commands

logger = logging.getLogger("discord-log-bot.settings")

# forum channel, staff role, target/member role, auto-create enabled, guild ID
ApplySettings = Callable[[int, int, int, bool, int], None]


def build_settings_command(
    *,
    owner_id: int,
    manager: Any,
    apply_settings: ApplySettings,
) -> app_commands.Command:
    """Build owner-only /settings and its persistent configuration modal."""

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
                default_values=(
                    [discord.Object(id=manager.forum_channel_id)]
                    if manager.forum_channel_id
                    else []
                ),
            ),
        )
        staff_role = discord.ui.Label(
            text="Staff role",
            description=(
                "Members with this role can use bot commands. "
                "Leave blank for owner-only access."
            ),
            component=discord.ui.RoleSelect(
                placeholder="Select a staff role (optional)",
                min_values=0,
                max_values=1,
                required=False,
                default_values=(
                    [discord.Object(id=manager.staff_role_id)]
                    if manager.staff_role_id
                    else []
                ),
            ),
        )
        target_role = discord.ui.Label(
            text="Member role to create logs for",
            description="Members with this role get logs. Leave blank to disable member logging.",
            component=discord.ui.RoleSelect(
                placeholder="Select the role whose members get logs",
                min_values=0,
                max_values=1,
                required=False,
                default_values=(
                    [discord.Object(id=manager.role_id)] if manager.role_id else []
                ),
            ),
        )
        auto_create = discord.ui.Label(
            text="Auto create logs",
            description=(
                "If True, logs are created for current matching members and "
                "when members join or gain the role."
            ),
            component=discord.ui.Select(
                placeholder="Enable or disable automatic log creation",
                min_values=1,
                max_values=1,
                required=True,
                options=[
                    discord.SelectOption(
                        label="True",
                        value="true",
                        description="Automatically create logs for matching members.",
                    ),
                    discord.SelectOption(
                        label="False",
                        value="false",
                        description="Do not automatically create member logs.",
                    ),
                ],
            ),
        )

        def __init__(self) -> None:
            super().__init__(timeout=300)

            # Keep the defaults in the modal current if settings have changed
            # since startup (for example, via the /auto_log command).
            self.forum_channel.component.default_values = (
                [discord.Object(id=manager.forum_channel_id)]
                if manager.forum_channel_id
                else []
            )
            self.staff_role.component.default_values = (
                [discord.Object(id=manager.staff_role_id)]
                if manager.staff_role_id
                else []
            )
            self.target_role.component.default_values = (
                [discord.Object(id=manager.role_id)] if manager.role_id else []
            )
            self.auto_create.component.options = [
                discord.SelectOption(
                    label="True",
                    value="true",
                    description="Automatically create logs for matching members.",
                    default=manager.auto_log_enabled,
                ),
                discord.SelectOption(
                    label="False",
                    value="false",
                    description="Do not automatically create member logs.",
                    default=not manager.auto_log_enabled,
                ),
            ]

        async def on_submit(self, interaction: discord.Interaction) -> None:
            # Defence in depth: only the configured owner may submit settings.
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
                    "Settings cannot be changed while a logging job is running. "
                    "Try again when it finishes.",
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
                    "I could not access that channel. Make sure it belongs to "
                    "this server and try again.",
                    ephemeral=True,
                )
                return

            if not isinstance(forum, discord.ForumChannel) or forum.guild.id != guild.id:
                await interaction.response.send_message(
                    "The selected channel must be a forum channel in this server.",
                    ephemeral=True,
                )
                return

            staff_values = self.staff_role.component.values
            staff_role_id = int(staff_values[0].id) if staff_values else 0
            if staff_role_id == guild.id:
                await interaction.response.send_message(
                    "The @everyone role cannot be used as the staff role. "
                    "Choose a specific staff role or leave it blank for owner-only access.",
                    ephemeral=True,
                )
                return
            if staff_role_id and guild.get_role(staff_role_id) is None:
                await interaction.response.send_message(
                    "The selected staff role could not be found in this server.",
                    ephemeral=True,
                )
                return

            target_values = self.target_role.component.values
            target_role_id = int(target_values[0].id) if target_values else 0
            if target_role_id == guild.id:
                await interaction.response.send_message(
                    "The @everyone role cannot be used as the member-log role. "
                    "Choose a specific role or leave it blank to disable member logging.",
                    ephemeral=True,
                )
                return
            if target_role_id and guild.get_role(target_role_id) is None:
                await interaction.response.send_message(
                    "The selected member role could not be found in this server.",
                    ephemeral=True,
                )
                return

            auto_values = self.auto_create.component.values
            if not auto_values or auto_values[0] not in {"true", "false"}:
                await interaction.response.send_message(
                    "Choose True or False for automatic log creation.",
                    ephemeral=True,
                )
                return
            auto_create_enabled = auto_values[0] == "true"

            if auto_create_enabled and not target_role_id:
                await interaction.response.send_message(
                    "Select a member role before enabling automatic log creation.",
                    ephemeral=True,
                )
                return

            try:
                apply_settings(
                    forum.id,
                    staff_role_id,
                    target_role_id,
                    auto_create_enabled,
                    guild.id,
                )
            except Exception:
                logger.exception("Could not save bot settings")
                await interaction.response.send_message(
                    "I could not save those settings. Check the bot logs and try again.",
                    ephemeral=True,
                )
                return

            staff_text = (
                f"<@&{staff_role_id}>" if staff_role_id else "not set (owner-only access)"
            )
            target_text = f"<@&{target_role_id}>" if target_role_id else "not set"
            await interaction.response.send_message(
                "**Bot settings saved.**\n"
                f"Forum channel: {forum.mention}\n"
                f"Staff role: {staff_text}\n"
                f"Member role to create logs for: {target_text}\n"
                f"Auto create logs: **{auto_create_enabled}**\n"
                "Only the configured owner can change these settings.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @app_commands.command(
        name="settings",
        description="Configure forum, staff/member roles, and automatic logging (owner only).",
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