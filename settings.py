from __future__ import annotations

import logging
import re
from typing import Any, Callable

import discord
from discord import app_commands

logger = logging.getLogger("discord-log-bot.settings")

ApplyMainSettings = Callable[[int, int], None]
ApplyForumSettings = Callable[[int, int, bool, int], None]
ApplyCreationSettings = Callable[[int, int], None]
DEFAULT_EMBED_COLOUR = 0x242429


def build_settings_command(
    *,
    owner_id: int,
    manager: Any,
    apply_main_settings: ApplyMainSettings,
    apply_forum_settings: ApplyForumSettings,
    apply_creation_settings: ApplyCreationSettings,
) -> app_commands.Command:
    async def reply(interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def validate_submit_context(interaction: discord.Interaction) -> bool:
        if interaction.user.id != owner_id:
            await reply(interaction, "Only the configured bot owner can change settings.")
            return False
        if interaction.guild is None:
            await reply(
                interaction,
                "Run this command inside the server you want to configure.",
            )
            return False
        if manager.job.running or manager.lock.locked():
            await reply(
                interaction,
                "Settings cannot be changed while a logging job is running. "
                "Try again when it finishes.",
            )
            return False
        return True

    class MainConfigModal(discord.ui.Modal, title="Main Config"):
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
        embed_colour_field = discord.ui.Label(
            text="Global embed colour",
            description="Enter a hex colour, for example #242429.",
            component=discord.ui.TextInput(
                style=discord.TextStyle.short,
                min_length=6,
                max_length=7,
                required=True,
                placeholder="#242429",
            ),
        )

        def __init__(self) -> None:
            super().__init__(timeout=300)
            self.staff_role.component.default_values = (
                [discord.Object(id=manager.staff_role_id)]
                if manager.staff_role_id
                else []
            )
            current_colour = getattr(
                manager,
                "embed_colour",
                discord.Colour(DEFAULT_EMBED_COLOUR),
            )
            self.embed_colour_field.component.default = (
                f"#{int(current_colour.value):06x}"
            )

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if not await validate_submit_context(interaction):
                return
            guild = interaction.guild
            assert guild is not None

            role_values = self.staff_role.component.values
            staff_role_id = int(role_values[0].id) if role_values else 0
            if staff_role_id == guild.id:
                await reply(
                    interaction,
                    "The @everyone role cannot be used as the staff role. "
                    "Choose a specific staff role or leave it blank for owner-only access.",
                )
                return
            if staff_role_id and guild.get_role(staff_role_id) is None:
                await reply(
                    interaction,
                    "The selected staff role could not be found in this server.",
                )
                return

            raw_colour = self.embed_colour_field.component.value.strip()
            if raw_colour.startswith("#"):
                raw_colour = raw_colour[1:]
            if not re.fullmatch(r"[0-9a-fA-F]{6}", raw_colour):
                await reply(
                    interaction,
                    "Enter a valid six-digit hex colour, such as `#242429`.",
                )
                return
            colour_value = int(raw_colour, 16)

            try:
                apply_main_settings(staff_role_id, colour_value)
            except Exception:
                logger.exception("Could not save main bot settings")
                await reply(
                    interaction,
                    "I could not save the main settings. Check the bot logs and try again.",
                )
                return

            staff_text = (
                f"<@&{staff_role_id}>" if staff_role_id else "not set (owner-only access)"
            )
            await reply(
                interaction,
                "**Main settings saved.**\n"
                f"Staff role: {staff_text}\n"
                f"Global embed colour: `#{colour_value:06x}`",
            )

    class ForumConfigModal(discord.ui.Modal, title="Forum Config"):
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
        target_role = discord.ui.Label(
            text="Member role to create logs for",
            description=(
                "Members with this role get logs. Leave blank to disable member logging."
            ),
            component=discord.ui.RoleSelect(
                placeholder="Select the member role (optional)",
                min_values=0,
                max_values=1,
                required=False,
            ),
        )
        auto_create = discord.ui.Label(
            text="Auto create logs",
            description=(
                "Create logs for current matching members and when members join or gain the role."
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
            self.forum_channel.component.default_values = (
                [discord.Object(id=manager.forum_channel_id)]
                if manager.forum_channel_id
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
            if not await validate_submit_context(interaction):
                return
            guild = interaction.guild
            assert guild is not None

            forum_values = self.forum_channel.component.values
            if not forum_values:
                await reply(interaction, "Choose a forum channel before saving these settings.")
                return
            forum_id = int(forum_values[0].id)
            try:
                forum = guild.get_channel(forum_id)
                if forum is None:
                    forum = await interaction.client.fetch_channel(forum_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await reply(
                    interaction,
                    "I could not access that channel. Make sure it belongs to this server and try again.",
                )
                return

            if not isinstance(forum, discord.ForumChannel) or forum.guild.id != guild.id:
                await reply(
                    interaction,
                    "The selected channel must be a forum channel in this server.",
                )
                return

            role_values = self.target_role.component.values
            target_role_id = int(role_values[0].id) if role_values else 0
            if target_role_id == guild.id:
                await reply(
                    interaction,
                    "The @everyone role cannot be used as the member-log role. "
                    "Choose a specific role or leave it blank to disable member logging.",
                )
                return
            if target_role_id and guild.get_role(target_role_id) is None:
                await reply(
                    interaction,
                    "The selected member role could not be found in this server.",
                )
                return

            auto_values = self.auto_create.component.values
            if not auto_values or auto_values[0] not in {"true", "false"}:
                await reply(interaction, "Choose True or False for automatic log creation.")
                return
            auto_create_enabled = auto_values[0] == "true"
            if auto_create_enabled and not target_role_id:
                await reply(
                    interaction,
                    "Select a member role before enabling automatic log creation.",
                )
                return

            try:
                apply_forum_settings(
                    forum.id,
                    target_role_id,
                    auto_create_enabled,
                    guild.id,
                )
            except Exception:
                logger.exception("Could not save forum settings")
                await reply(
                    interaction,
                    "I could not save the forum settings. Check the bot logs and try again.",
                )
                return

            target_text = f"<@&{target_role_id}>" if target_role_id else "not set"
            await reply(
                interaction,
                "**Forum settings saved.**\n"
                f"Forum channel: {forum.mention}\n"
                f"Member role to create logs for: {target_text}\n"
                f"Auto create logs: **{auto_create_enabled}**",
            )

    class CreationApprovalModal(discord.ui.Modal, title="Creation Approval"):
        prompt_channel = discord.ui.Label(
            text="Creation prompt channel",
            description="Text channel where users upload their creations.",
            component=discord.ui.ChannelSelect(
                channel_types=[discord.ChannelType.text],
                placeholder="Select the prompt channel (optional)",
                min_values=0,
                max_values=1,
                required=False,
            ),
        )
        approve_channel = discord.ui.Label(
            text="Creation approval channel",
            description="Text channel where staff review submissions.",
            component=discord.ui.ChannelSelect(
                channel_types=[discord.ChannelType.text],
                placeholder="Select the approval channel (optional)",
                min_values=0,
                max_values=1,
                required=False,
            ),
        )

        def __init__(self) -> None:
            super().__init__(timeout=300)
            self.prompt_channel.component.default_values = (
                [discord.Object(id=manager.creation_prompt_channel_id)]
                if manager.creation_prompt_channel_id
                else []
            )
            self.approve_channel.component.default_values = (
                [discord.Object(id=manager.creation_approve_channel_id)]
                if manager.creation_approve_channel_id
                else []
            )

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if not await validate_submit_context(interaction):
                return
            guild = interaction.guild
            assert guild is not None

            prompt_values = self.prompt_channel.component.values
            approve_values = self.approve_channel.component.values
            if bool(prompt_values) != bool(approve_values):
                await reply(
                    interaction,
                    "Choose both creation channels, or clear both to disable creation submissions.",
                )
                return

            prompt_id = 0
            approve_id = 0
            if prompt_values and approve_values:
                prompt_id = int(prompt_values[0].id)
                approve_id = int(approve_values[0].id)
                prompt = guild.get_channel(prompt_id)
                approve = guild.get_channel(approve_id)
                if not isinstance(prompt, discord.TextChannel) or not isinstance(
                    approve, discord.TextChannel
                ):
                    await reply(
                        interaction,
                        "Both creation channels must be text channels in this server.",
                    )
                    return
                if prompt.id == approve.id:
                    await reply(
                        interaction,
                        "The creation prompt and approval channels must be different.",
                    )
                    return

            try:
                apply_creation_settings(prompt_id, approve_id)
            except Exception:
                logger.exception("Could not save creation approval settings")
                await reply(
                    interaction,
                    "I could not save the creation approval settings. Check the bot logs and try again.",
                )
                return

            prompt_text = f"<#{prompt_id}>" if prompt_id else "not set"
            approve_text = f"<#{approve_id}>" if approve_id else "not set"
            await reply(
                interaction,
                "**Creation approval settings saved.**\n"
                f"Creation prompt channel: {prompt_text}\n"
                f"Creation approval channel: {approve_text}",
            )

    class BoosterConfigModal(discord.ui.Modal, title="Booster Config"):
        placeholder_field = discord.ui.Label(
            text="Booster configuration",
            description="This section is a placeholder for future settings.",
            component=discord.ui.TextInput(
                style=discord.TextStyle.short,
                required=False,
                max_length=100,
                placeholder="No booster settings yet",
            ),
        )

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if interaction.user.id != owner_id:
                await reply(interaction, "Only the configured bot owner can change settings.")
                return
            await reply(
                interaction,
                "Booster config is a placeholder for now. No settings were changed.",
            )

    class SettingsMenuView(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=300)

        async def _owner_only(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != owner_id:
                await reply(interaction, "Only the configured bot owner can use these settings.")
                return False
            if interaction.guild is None:
                await reply(interaction, "Use these settings inside the configured server.")
                return False
            return True

        @discord.ui.button(
            label="Main config",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        async def main_config(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ) -> None:
            if await self._owner_only(interaction):
                await interaction.response.send_modal(MainConfigModal())

        @discord.ui.button(
            label="Forum config",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        async def forum_config(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ) -> None:
            if await self._owner_only(interaction):
                await interaction.response.send_modal(ForumConfigModal())

        @discord.ui.button(
            label="Booster config",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        async def booster_config(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ) -> None:
            if await self._owner_only(interaction):
                await interaction.response.send_modal(BoosterConfigModal())

        @discord.ui.button(
            label="Creation approval",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        async def creation_approval(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ) -> None:
            if await self._owner_only(interaction):
                await interaction.response.send_modal(CreationApprovalModal())

    @app_commands.command(
        name="settings",
        description="Configure bot logging, roles, embed colour, and creation channels (owner only).",
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
            "Choose a configuration section:",
            view=SettingsMenuView(),
            ephemeral=True,
        )

    return settings
