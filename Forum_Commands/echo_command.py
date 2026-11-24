from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

logger = logging.getLogger("discord-log-bot.echo")

MAX_ECHO_LENGTH = 2000


def build_echo_commands(
    *,
    manager: Any,
    owner_id: int,
) -> tuple[app_commands.ContextMenu, app_commands.ContextMenu]:
    def is_staff(user: discord.User | discord.Member) -> bool:
        if user.id == owner_id:
            return True
        role_id = int(getattr(manager, "staff_role_id", 0) or 0)
        return (
            bool(role_id)
            and isinstance(user, discord.Member)
            and user.get_role(role_id) is not None
        )

    async def reply(interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    # Users can be mentioned, but @everyone/@here and role pings are suppressed.
    echo_mentions = discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=True,
        replied_user=False,
    )

    class EchoModal(discord.ui.Modal):
        echo_text = discord.ui.Label(
            text="Enter your message",
            component=discord.ui.TextInput(
                style=discord.TextStyle.paragraph,
                max_length=MAX_ECHO_LENGTH,
                required=True,
            ),
        )

        def __init__(self, target: discord.Message, *, as_reply: bool) -> None:
            super().__init__(
                title="Echo Reply" if as_reply else "Echo",
                timeout=300,
            )
            self.target = target
            self.as_reply = as_reply

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if not is_staff(interaction.user):
                await reply(
                    interaction,
                    "You need the configured staff role to use this app.",
                )
                return

            text = (self.echo_text.component.value or "").strip()
            if not text:
                await reply(interaction, "Enter a message to send.")
                return

            await interaction.response.defer(ephemeral=True)
            try:
                if self.as_reply:
                    await self.target.reply(
                        text,
                        mention_author=False,
                        allowed_mentions=echo_mentions,
                    )
                else:
                    channel = interaction.channel or self.target.channel
                    await channel.send(text, allowed_mentions=echo_mentions)
            except (discord.HTTPException, AttributeError):
                logger.warning("Could not send echo message", exc_info=True)
                await interaction.followup.send(
                    "I couldn't send that message. Check my permissions in this channel.",
                    ephemeral=True,
                )
                return

            await interaction.followup.send("Message sent.", ephemeral=True)

    @app_commands.guild_only()
    async def echo_callback(
        interaction: discord.Interaction, message: discord.Message
    ) -> None:
        if not is_staff(interaction.user):
            await reply(interaction, "You need the configured staff role to use this app.")
            return
        await interaction.response.send_modal(EchoModal(message, as_reply=False))

    @app_commands.guild_only()
    async def echo_reply_callback(
        interaction: discord.Interaction, message: discord.Message
    ) -> None:
        if not is_staff(interaction.user):
            await reply(interaction, "You need the configured staff role to use this app.")
            return
        await interaction.response.send_modal(EchoModal(message, as_reply=True))

    echo = app_commands.ContextMenu(name="Echo", callback=echo_callback)
    echo_reply = app_commands.ContextMenu(name="Echo Reply", callback=echo_reply_callback)
    return echo, echo_reply
