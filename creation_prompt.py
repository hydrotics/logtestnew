
from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from types import MethodType
from typing import Any

import aiohttp

import discord
from discord import app_commands

logger = logging.getLogger("discord-log-bot.creation")

EMBED_COLOUR = discord.Colour.from_rgb(36, 36, 41)
ACCEPTED_COLOUR = discord.Colour.from_rgb(46, 204, 113)
DENIED_COLOUR = discord.Colour.from_rgb(231, 76, 60)
PROMPT_SELECT_ID = "creation:prompt"
REVIEW_SELECT_ID = "creation:review"
MAX_TEXT_LENGTH = 1800
MEDIA_TYPES = ("image/", "video/")

_reviewing: set[int] = set()
_registered_clients: set[int] = set()
_prompt_messages: dict[int, int] = {}
_prompt_locks: dict[int, asyncio.Lock] = {}


def setup_creation_prompt(
    client: discord.Client,
    *,
    manager: Any,
    owner_id: int,
) -> app_commands.Command:
    def text_channel(
        guild: discord.Guild | None, channel_id: int
    ) -> discord.TextChannel | None:
        channel = guild.get_channel(channel_id) if guild and channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    def is_staff(user: discord.User | discord.Member) -> bool:
        if user.id == owner_id:
            return True
        role_id = manager.staff_role_id
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

    def review_view(user_id: int, *, disabled: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(ReviewSelect(user_id, disabled=disabled))
        return view

    async def fetch_latest_message(message: discord.Message) -> discord.Message:
        fetch_message = getattr(message.channel, "fetch_message", None)
        if fetch_message is None:
            return message
        try:
            return await fetch_message(message.id)
        except discord.HTTPException:
            logger.warning(
                "Could not refresh submission message %s; using interaction message data.",
                message.id,
                exc_info=True,
            )
            return message

    def update_submission_status(
        embed: discord.Embed,
        *,
        submitter_id: int,
        staff_id: int,
        accepted: bool,
    ) -> discord.Embed:
        verdict = "Accepted" if accepted else "Denied"
        timestamp = int(discord.utils.utcnow().timestamp())
        info_value = (
            f"**Submitted by:** <@{submitter_id}>\n"
            f"**Status:** {verdict} by <@{staff_id}> · <t:{timestamp}:R>"
        )
        info_index = next(
            (index for index, field in enumerate(embed.fields) if field.name == "Information"),
            None,
        )
        if info_index is None:
            embed.add_field(name="Information", value=info_value, inline=False)
        else:
            embed.set_field_at(
                info_index,
                name="Information",
                value=info_value,
                inline=False,
            )
        embed.colour = ACCEPTED_COLOUR if accepted else DENIED_COLOUR
        return embed

    class ReviewSelect(
        discord.ui.DynamicItem[discord.ui.Select],
        template=rf"{REVIEW_SELECT_ID}:(?P<user_id>[0-9]+)",
    ):
        def __init__(self, user_id: int, *, disabled: bool = False) -> None:
            super().__init__(
                discord.ui.Select(
                    custom_id=f"{REVIEW_SELECT_ID}:{user_id}",
                    placeholder="Actions",
                    disabled=disabled,
                    options=[
                        discord.SelectOption(label="Accept", value="accept"),
                        discord.SelectOption(label="Deny", value="deny"),
                    ],
                )
            )
            self.user_id = user_id

        @classmethod
        async def from_custom_id(
            cls,
            interaction: discord.Interaction,
            item: discord.ui.Select,
            match,
            /,
        ) -> ReviewSelect:
            return cls(int(match["user_id"]))

        async def callback(self, interaction: discord.Interaction) -> None:
            message = interaction.message
            if message is None:
                await reply(interaction, "I could not find this submission message.")
                return
            if not is_staff(interaction.user):
                await reply(
                    interaction,
                    "You need the configured staff role to review submissions.",
                )
                return
            if message.id in _reviewing:
                await reply(interaction, "This submission is already being reviewed.")
                return

            _reviewing.add(message.id)
            try:
                await self._review(interaction, message)
            finally:
                _reviewing.discard(message.id)

        async def _review(
            self,
            interaction: discord.Interaction,
            message: discord.Message,
        ) -> None:
            accepted = bool(self.item.values and self.item.values[0] == "accept")
            await interaction.response.defer()

            source_message = await fetch_latest_message(message)
            if not source_message.embeds:
                await self._fail(
                    interaction,
                    "This submission has no embed to review. Please ask the user to resubmit it.",
                )
                return

            source_embed = discord.Embed.from_dict(source_message.embeds[0].to_dict())
            attachments = list(source_message.attachments)
            media_files: list[discord.File] = []

            try:
                # Convert every uploaded attachment to a real file. URLs are never
                # included in message text; media is sent as regular attachments.
                if attachments:
                    for attachment in attachments:
                        media_files.append(await attachment.to_file())
                else:
                    # Recovery fallback for older submissions where only an embed
                    # image URL remains. This is downloaded and re-uploaded privately.
                    image_url = source_embed.image.url if source_embed.image else None
                    if image_url and image_url.startswith(("https://", "http://")):
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                image_url,
                                timeout=aiohttp.ClientTimeout(total=20),
                            ) as response:
                                response.raise_for_status()
                                data = await response.read()
                                if data:
                                    filename = (
                                        image_url.split("?", 1)[0].rsplit("/", 1)[-1]
                                        or "creation-image.png"
                                    )
                                    if "." not in filename:
                                        filename += ".png"
                                    media_files.append(
                                        discord.File(BytesIO(data), filename=filename)
                                    )

                if accepted:
                    channel = text_channel(
                        interaction.guild, manager.creation_prompt_channel_id
                    )
                    if channel is None:
                        await self._fail(
                            interaction,
                            "The creation prompt channel is unavailable. Check `/settings`.",
                        )
                        return
                    if not media_files:
                        await self._fail(
                            interaction,
                            "I can't retrieve the uploaded files from this submission. Please ask the user to resubmit them.",
                        )
                        return

                    # Publish the approved creation using Discord Components V2.
                    # Keep every uploaded file attached to the message.
                    submitter = (
                        interaction.guild.get_member(self.user_id)
                        if interaction.guild is not None
                        else None
                    )

                    username = (
                        submitter.name
                        if submitter is not None
                        else source_embed.author.name
                        if source_embed.author.name
                        else f"user-{self.user_id}"
                    )

                    avatar_url = (
                        submitter.display_avatar.url
                        if submitter is not None
                        else source_embed.author.icon_url
                        if source_embed.author.icon_url
                        else client.user.display_avatar.url
                    )

                    # Components V2 layout.
                    published_view = discord.ui.LayoutView(timeout=None)

                    published_container = discord.ui.Container(
                        accent_colour=discord.Colour.from_rgb(36, 36, 39)
                    )

                    # Username, mention, and square avatar thumbnail.
                    published_container.add_item(
                        discord.ui.Section(
                            discord.ui.TextDisplay(
                                f"## {username}\n<@{self.user_id}>"
                            ),
                            accessory=discord.ui.Thumbnail(
                                media=avatar_url,
                                description=f"{username}'s avatar",
                            ),
                        )
                    )

                    # Divider underneath the member information.
                    published_container.add_item(
                        discord.ui.Separator()
                    )

                    # Find the first uploaded image or video.
                    first_media_filename: str | None = None
                    media_extensions = (
                        ".png",
                        ".jpg",
                        ".jpeg",
                        ".gif",
                        ".webp",
                        ".bmp",
                        ".avif",
                        ".mp4",
                        ".webm",
                        ".mov",
                        ".m4v",
                    )

                    for attachment in attachments:
                        content_type = attachment.content_type or ""
                        if content_type.startswith(MEDIA_TYPES):
                            first_media_filename = attachment.filename
                            break

                    if first_media_filename is None:
                        first_media_filename = next(
                            (
                                file.filename
                                for file in media_files
                                if file.filename.lower().endswith(media_extensions)
                            ),
                            None,
                        )

                    # Display the first uploaded image or video where supported.
                    if first_media_filename:
                        published_container.add_item(
                            discord.ui.MediaGallery(
                                discord.MediaGalleryItem(
                                    media=f"attachment://{first_media_filename}"
                                )
                            )
                        )

                    # Display the text entered by the submitter.
                    submission_description = source_embed.description
                    if submission_description:
                        published_container.add_item(
                            discord.ui.TextDisplay(
                                submission_description
                            )
                        )

                    published_view.add_item(published_container)

                    await channel.send(
                        view=published_view,
                        files=media_files,
                        allowed_mentions=discord.AllowedMentions(
                            users=[discord.Object(id=self.user_id)]
                        ),
                    )
                    media_files = []  # discord.py has consumed/closed them

                # Update status but keep the existing attachment on the review
                # message. This prevents the image from becoming a separate file.
                # The uploaded media should remain a normal attachment below the
                # review/status embed, not be rendered inside the embed itself.
                source_embed.set_image(url=None)
                source_embed.set_thumbnail(url=None)
                status_embed = update_submission_status(
                    source_embed,
                    submitter_id=self.user_id,
                    staff_id=interaction.user.id,
                    accepted=accepted,
                )
                edit_attachments: list[Any] = list(source_message.attachments)
                if not accepted and not edit_attachments and media_files:
                    # Restore media if the original attachment was already missing.
                    edit_attachments = media_files
                    media_files = []

                await source_message.edit(
                    embed=status_embed,
                    attachments=edit_attachments,
                    view=review_view(self.user_id, disabled=True),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (
                discord.HTTPException,
                OSError,
                aiohttp.ClientError,
                ValueError,
            ):
                logger.exception(
                    "Could not process creation review for %s (message %s)",
                    self.user_id,
                    source_message.id,
                )
                await self._fail(
                    interaction,
                    "I couldn't retrieve or preserve the uploaded file. Please ask the user to resubmit it.",
                )
                return
            finally:
                for media_file in media_files:
                    media_file.close()

            verdict = "accepted" if accepted else "denied"
            await interaction.followup.send(f"Submission {verdict}.", ephemeral=True)

        async def _fail(
            self,
            interaction: discord.Interaction,
            message: str,
        ) -> None:
            await interaction.followup.send(message, ephemeral=True)

    class SubmissionModal(discord.ui.Modal, title="Upload your creation"):
        details = discord.ui.Label(
            text="Creation description / text",
            component=discord.ui.TextInput(
                style=discord.TextStyle.paragraph,
                max_length=MAX_TEXT_LENGTH,
                required=True,
            ),
        )
        media = discord.ui.Label(
            text="Creation image / video",
            component=discord.ui.FileUpload(min_values=1, max_values=10, required=True),
        )

        async def on_submit(self, interaction: discord.Interaction) -> None:
            attachments = list(self.media.component.values)
            invalid_attachments = [
                attachment.filename
                for attachment in attachments
                if not (attachment.content_type or "").startswith(MEDIA_TYPES)
            ]
            if invalid_attachments:
                names = ", ".join(invalid_attachments[:5])
                await reply(
                    interaction,
                    f"Please upload only images or videos. Unsupported file(s): {names}",
                )
                return

            channel = text_channel(
                interaction.guild, manager.creation_approve_channel_id
            )
            if channel is None:
                await reply(interaction, "Submissions are currently unavailable.")
                return

            await interaction.response.defer(ephemeral=True)

            user = interaction.user
            uploaded_files = [
                await attachment.to_file() for attachment in attachments
            ]
            embed = discord.Embed(
                description=self.details.component.value,
                colour=EMBED_COLOUR,
            )
            embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
            embed.add_field(
                name="Information",
                value=f"**Submitted by:** {user.mention}\n**Status:** Pending review",
                inline=False,
            )
            filenames = "\n".join(
                f"• `{uploaded_file.filename}`"
                for uploaded_file in uploaded_files
            )
            embed.add_field(
                name=f"Attachments ({len(uploaded_files)})",
                value=filenames[:1024],
                inline=False,
            )
            # Keep all media as normal message attachments, not embed images.

            try:
                await channel.send(
                    embed=embed,
                    files=uploaded_files,
                    view=review_view(user.id),
                )
            except discord.HTTPException:
                for uploaded_file in uploaded_files:
                    uploaded_file.close()
                logger.exception(
                    "Could not forward creation submission from %s", user.id
                )
                await reply(
                    interaction,
                    "Your submission could not be sent. Please try again later.",
                )
                return

            await reply(
                interaction,
                "Your creation has been submitted for staff approval.",
            )

    def prompt_embed() -> discord.Embed:
        return discord.Embed(
            title="Upload your creation",
            description=(
                "Share your creations here. "
                "Each submission will be approved by staff before being sent."
            ),
            colour=EMBED_COLOUR,
        )

    async def send_prompt(channel: discord.TextChannel) -> discord.Message:
        message = await channel.send(embed=prompt_embed(), view=PromptView())
        _prompt_messages[channel.id] = message.id
        return message

    async def find_prompt_message(
        channel: discord.TextChannel,
    ) -> discord.Message | None:
        prompt_id = _prompt_messages.get(channel.id)
        if prompt_id is not None:
            try:
                return await channel.fetch_message(prompt_id)
            except discord.NotFound:
                _prompt_messages.pop(channel.id, None)
            except discord.HTTPException:
                logger.exception(
                    "Could not fetch tracked creation prompt in channel %s",
                    channel.id,
                )
                return None

        # Recover the latest prompt after a bot restart, when the in-memory ID is lost.
        try:
            async for old_message in channel.history(limit=50):
                if old_message.author.id != client.user.id:
                    continue
                if not any(
                    embed.title == "Upload your creation"
                    for embed in old_message.embeds
                ):
                    continue
                if any(
                    getattr(component, "custom_id", None) == PROMPT_SELECT_ID
                    for row in old_message.components
                    for component in getattr(row, "children", [])
                ):
                    _prompt_messages[channel.id] = old_message.id
                    return old_message
        except discord.HTTPException:
            logger.exception(
                "Could not search for the existing creation prompt in channel %s",
                channel.id,
            )
        return None

    async def on_prompt_channel_message(message: discord.Message) -> None:
        # Only react inside the configured prompt channel. Human messages and
        # this bot's approved-creation posts should bump the prompt; other bots'
        # messages are ignored.
        if message.guild is None:
            return
        channel = text_channel(
            message.guild, manager.creation_prompt_channel_id
        )
        if channel is None or message.channel.id != channel.id:
            return

        if message.author.bot and (
            client.user is None or message.author.id != client.user.id
        ):
            return

        # Ignore only the prompt itself, not every message sent by this bot.
        # Check both the tracked ID and the prompt's title/component so the
        # initial prompt won't trigger a refresh before its ID is tracked.
        is_prompt_message = message.id == _prompt_messages.get(channel.id)
        if not is_prompt_message:
            is_prompt_message = any(
                embed.title == "Upload your creation"
                for embed in message.embeds
            ) and any(
                getattr(component, "custom_id", None) == PROMPT_SELECT_ID
                for row in message.components
                for component in getattr(row, "children", [])
            )
        if is_prompt_message:
            return

        lock = _prompt_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            old_prompt = await find_prompt_message(channel)
            if old_prompt is not None:
                try:
                    await old_prompt.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    logger.exception(
                        "Could not delete old creation prompt %s", old_prompt.id
                    )
                    # Still send a fresh prompt so the upload control remains available.
            try:
                await send_prompt(channel)
            except discord.HTTPException:
                logger.exception(
                    "Could not resend creation prompt in channel %s", channel.id
                )

    class PromptView(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=None)

        @discord.ui.select(
            custom_id=PROMPT_SELECT_ID,
            placeholder="Upload your creation",
            options=[
                discord.SelectOption(
                    label="Upload creation",
                    value="upload",
                    description="Submit your creation for staff approval.",
                )
            ],
        )
        async def prompt(
            self,
            interaction: discord.Interaction,
            select: discord.ui.Select,
        ) -> None:
            if text_channel(
                interaction.guild, manager.creation_approve_channel_id
            ) is None:
                await reply(
                    interaction, "Submissions are currently unavailable."
                )
            else:
                await interaction.response.send_modal(SubmissionModal())

            try:
                await interaction.message.edit(view=self)
            except (discord.HTTPException, AttributeError):
                pass

    @app_commands.command(
        name="creation_prompt",
        description="Send the creation upload prompt to the configured channel.",
    )
    @app_commands.guild_only()
    async def creation_prompt(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        channel = text_channel(
            interaction.guild, manager.creation_prompt_channel_id
        )
        if channel is None or text_channel(
            interaction.guild, manager.creation_approve_channel_id
        ) is None:
            await reply(
                interaction,
                "Set the creation prompt and approval channels with `/settings` first.",
            )
            return

        try:
            old_prompt = await find_prompt_message(channel)
            if old_prompt is not None:
                try:
                    await old_prompt.delete()
                except discord.NotFound:
                    pass
            await send_prompt(channel)
        except discord.HTTPException:
            logger.exception("Could not send creation prompt")
            await reply(
                interaction,
                f"I could not send the prompt to {channel.mention}. Check my permissions there.",
            )
            return

        await reply(
            interaction, f"Creation prompt sent to {channel.mention}."
        )

    client_key = id(client)
    if client_key not in _registered_clients:
        client.add_view(PromptView())
        client.add_dynamic_items(ReviewSelect)

        # LogBot may inherit directly from discord.Client, which has no
        # the commands.Bot listener API. Wrap on_message while
        # preserving any handler the application already defines.
        original_on_message = getattr(client, "on_message", None)

        async def combined_on_message(
            self_client, message: discord.Message
        ) -> None:
            if original_on_message is not None:
                result = original_on_message(message)
                if hasattr(result, "__await__"):
                    await result
            await on_prompt_channel_message(message)

        client.on_message = MethodType(combined_on_message, client)
        _registered_clients.add(client_key)

    return creation_prompt
