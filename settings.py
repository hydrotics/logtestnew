from __future__ import annotations

import logging
from typing import Any, Callable

import discord
from discord import app_commands

logger = logging.getLogger("discord-log-bot.settings")

# Reveal feature configuration. Replace 0 with the numeric Discord snowflake IDs.
# These are non-secret values; WATERMARK_SECRET belongs in .env instead.
REVEAL_ARCHIVE_CHANNEL_ID = 0
BOOSTER_ROLE_ID = 0

ApplySettings = Callable[[int, int, int, bool, int], None]
ApplyCreationSettings = Callable[[int, int], None]
ApplyRevealSettings = Callable[[int, int, int], None]


def build_settings_command(
    *,
    owner_id: int,
    manager: Any,
    apply_settings: ApplySettings,
    apply_creation_settings: ApplyCreationSettings,
    apply_reveal_settings: ApplyRevealSettings,
) -> app_commands.Command:
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
            description=(
                "Members with this role can use bot commands. "
                "Leave blank for owner-only access."
            ),
            component=discord.ui.RoleSelect(
                placeholder="Select a staff role (optional)",
                min_values=0,
                max_values=1,
                required=False,
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
            ),
        )
        auto_create = discord.ui.Label(
            text="Auto create logs",
            description=(
                "Create logs for current matching members and when members "
                "join or gain the role."
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
        creation_channels = discord.ui.Label(
            text="Creation prompt and approval channels",
            description="Choose prompt first, then approval. Select none to clear both.",
            component=discord.ui.ChannelSelect(
                channel_types=[discord.ChannelType.text],
                placeholder="Select prompt first, then approval",
                min_values=0,
                max_values=2,
                required=False,
            ),
        )

        def __init__(self) -> None:
            super().__init__(timeout=300)
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
            self.creation_channels.component.default_values = [
                discord.Object(id=channel_id)
                for channel_id in (
                    manager.creation_prompt_channel_id,
                    manager.creation_approve_channel_id,
                )
                if channel_id
            ]

        async def on_submit(self, interaction: discord.Interaction) -> None:
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

            forum_values = self.forum_channel.component.values
            if not forum_values:
                await interaction.response.send_message(
                    "Choose a forum channel before submitting these settings.",
                    ephemeral=True,
                )
                return

            forum_id = int(forum_values[0].id)
            try:
                forum = guild.get_channel(forum_id)
                if forum is None:
                    forum = await interaction.client.fetch_channel(forum_id)
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

            selected_creation_channels = list(self.creation_channels.component.values)
            if len(selected_creation_channels) == 1:
                await interaction.response.send_message(
                    "Choose both creation channels, in this order: prompt first, approval second. "
                    "Select none to leave both unset.",
                    ephemeral=True,
                )
                return

            prompt_id = 0
            approve_id = 0
            if len(selected_creation_channels) == 2:
                prompt, approve = selected_creation_channels
                prompt = guild.get_channel(int(prompt.id))
                approve = guild.get_channel(int(approve.id))
                if not isinstance(prompt, discord.TextChannel) or not isinstance(
                    approve, discord.TextChannel
                ):
                    await interaction.response.send_message(
                        "Both creation channels must be text channels in this server.",
                        ephemeral=True,
                    )
                    return
                if prompt.id == approve.id:
                    await interaction.response.send_message(
                        "The creation prompt and approval channels must be different.",
                        ephemeral=True,
                    )
                    return
                prompt_id = prompt.id
                approve_id = approve.id

            try:
                apply_settings(
                    forum.id,
                    staff_role_id,
                    target_role_id,
                    auto_create_enabled,
                    guild.id,
                )
                apply_creation_settings(prompt_id, approve_id)
            except Exception:
                logger.exception("Could not save bot settings")
                await interaction.response.send_message(
                    "I could not save all settings. Check the bot logs and try again.",
                    ephemeral=True,
                )
                return

            staff_text = (
                f"<@&{staff_role_id}>" if staff_role_id else "not set (owner-only access)"
            )
            target_text = f"<@&{target_role_id}>" if target_role_id else "not set"
            prompt_text = f"<#{prompt_id}>" if prompt_id else "not set"
            approve_text = f"<#{approve_id}>" if approve_id else "not set"
            await interaction.response.send_message(
                "**Bot settings saved.**\n"
                f"Forum channel: {forum.mention}\n"
                f"Staff role: {staff_text}\n"
                f"Member role to create logs for: {target_text}\n"
                f"Auto create logs: **{auto_create_enabled}**\n"
                f"Creation prompt channel: {prompt_text}\n"
                f"Creation approval channel: {approve_text}\n"
                "Only the configured owner can change these settings.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    class RevealSettingsModal(discord.ui.Modal, title="Reveal Settings"):
        booster_role = discord.ui.Label(
            text="Booster access role",
            description="Members with this role can access personalized reveals. Leave blank to disable booster access.",
            component=discord.ui.RoleSelect(
                placeholder="Select the booster access role (optional)",
                min_values=0,
                max_values=1,
                required=False,
            ),
        )
        archive_channel = discord.ui.Label(
            text="Reveal archive channel",
            description="Choose the text channel used to archive original reveal media. Leave blank to disable archive-backed features.",
            component=discord.ui.ChannelSelect(
                channel_types=[discord.ChannelType.text],
                placeholder="Select the reveal archive channel (optional)",
                min_values=0,
                max_values=1,
                required=False,
            ),
        )

        def __init__(self) -> None:
            super().__init__(timeout=300)
            current_booster_role_id = int(getattr(manager, "booster_role_id", 0) or 0)
            current_archive_channel_id = int(
                getattr(manager, "reveal_archive_channel_id", 0) or 0
            )
            self.booster_role.component.default_values = (
                [discord.Object(id=current_booster_role_id)]
                if current_booster_role_id
                else []
            )
            self.archive_channel.component.default_values = (
                [discord.Object(id=current_archive_channel_id)]
                if current_archive_channel_id
                else []
            )

        async def on_submit(self, interaction: discord.Interaction) -> None:
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

            booster_values = self.booster_role.component.values
            booster_role_id = int(booster_values[0].id) if booster_values else 0
            if booster_role_id == guild.id:
                await interaction.response.send_message(
                    "The @everyone role cannot be used as the booster access role. "
                    "Choose a specific role or leave it blank to disable booster access.",
                    ephemeral=True,
                )
                return
            if booster_role_id and guild.get_role(booster_role_id) is None:
                await interaction.response.send_message(
                    "The selected booster role could not be found in this server.",
                    ephemeral=True,
                )
                return

            archive_values = self.archive_channel.component.values
            archive_channel_id = int(archive_values[0].id) if archive_values else 0
            archive_channel = None
            if archive_channel_id:
                archive_channel = guild.get_channel(archive_channel_id)
                if not isinstance(archive_channel, discord.TextChannel):
                    await interaction.response.send_message(
                        "The selected reveal archive must be a text channel in this server.",
                        ephemeral=True,
                    )
                    return
                if archive_channel.guild.id != guild.id:
                    await interaction.response.send_message(
                        "The selected reveal archive channel must belong to this server.",
                        ephemeral=True,
                    )
                    return

            try:
                apply_reveal_settings(archive_channel_id, booster_role_id, guild.id)
            except Exception:
                logger.exception("Could not save reveal settings")
                await interaction.response.send_message(
                    "I could not save the reveal settings. Check the bot logs and try again.",
                    ephemeral=True,
                )
                return

            archive_text = archive_channel.mention if archive_channel else "not set (archive-backed features disabled)"
            booster_text = (
                f"<@&{booster_role_id}>" if booster_role_id else "not set (booster access disabled)"
            )
            await interaction.response.send_message(
                "**Reveal settings saved.**\n"
                f"Booster access role: {booster_text}\n"
                f"Reveal archive channel: {archive_text}\n"
                "Only the configured owner can change these settings.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    class SettingsMenuView(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=300)

        async def _check_owner(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != owner_id:
                await interaction.response.send_message(
                    "Only the configured bot owner can change settings.",
                    ephemeral=True,
                )
                return False
            if interaction.guild is None:
                await interaction.response.send_message(
                    "Run this command inside the server you want to configure.",
                    ephemeral=True,
                )
                return False
            return True

        @discord.ui.button(label="Logging settings", style=discord.ButtonStyle.secondary)
        async def logging_settings(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            del button
            if not await self._check_owner(interaction):
                return
            await interaction.response.send_modal(BotSettingsModal())

        @discord.ui.button(label="Reveal settings", style=discord.ButtonStyle.secondary)
        async def reveal_settings(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            del button
            if not await self._check_owner(interaction):
                return
            await interaction.response.send_modal(RevealSettingsModal())

    @app_commands.command(
        name="settings",
        description="Configure bot logging, roles, creation channels, and reveal settings (owner only).",
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

        await interaction.response.send_message(
            "Choose which bot settings to configure.",
            view=SettingsMenuView(),
            ephemeral=True,
        )

    return settings