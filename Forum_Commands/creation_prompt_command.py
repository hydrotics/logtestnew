from __future__ import annotations

import asyncio
import logging
import os
import tempfile
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


def configured_embed_colour(manager: Any) -> discord.Colour:
    colour = getattr(manager, "embed_colour", None)
    return colour if isinstance(colour, discord.Colour) else EMBED_COLOUR


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

    async def download_to_path(
        session: aiohttp.ClientSession,
        url: str,
        path: str,
    ) -> None:
        """Stream a Discord CDN attachment to disk without loading it into RAM."""
        timeout = aiohttp.ClientTimeout(total=120)
        async with session.get(url, timeout=timeout) as response:
            response.raise_for_status()
            with open(path, "wb") as output_file:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    output_file.write(chunk)

    def safe_filename(filename: str) -> str:
        # Prevent path components in a user-supplied attachment filename from
        # being interpreted as part of the temporary file path.
        filename = (filename or "upload").replace("\\", "/").rsplit("/", 1)[-1]
        return filename.strip() or "upload"

    def unique_filename(filename: str, used_filenames: set[str]) -> str:
        original_filename = safe_filename(filename)
        stem, dot, extension = original_filename.rpartition(".")
        if not dot:
            stem, extension = original_filename, ""

        candidate = original_filename
        duplicate_index = 2
        while candidate in used_filenames:
            suffix = f".{extension}" if extension else ""
            candidate = f"{stem}_{duplicate_index}{suffix}"
            duplicate_index += 1

        used_filenames.add(candidate)
        return candidate

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
        reason: str | None = None,
    ) -> discord.Embed:
        verdict = "Accepted" if accepted else "Denied"
        timestamp = int(discord.utils.utcnow().timestamp())
        status_line = f"**Status:** {verdict} by <@{staff_id}> · <t:{timestamp}:R>"
        if not accepted and reason:
            safe_reason = reason.replace("`", "'")
            status_line += f" · `{safe_reason}`"
        info_value = f"**Submitted by:** <@{submitter_id}>\n{status_line}"
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

    class DenyReasonModal(discord.ui.Modal, title="Deny creation"):
        reason = discord.ui.Label(
            text="Denial reason",
            component=discord.ui.TextInput(
                style=discord.TextStyle.paragraph,
                max_length=500,
                required=True,
            ),
        )

        def __init__(self, review_item: Any, review_message: discord.Message) -> None:
            super().__init__()
            self.review_item = review_item
            self.review_message = review_message

        async def on_submit(self, interaction: discord.Interaction) -> None:
            reason = (self.reason.component.value or "").strip()
            await self.review_item._run_review(
                interaction, self.review_message, False, reason
            )

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

            accepted = bool(self.item.values and self.item.values[0] == "accept")
            if accepted:
                await self._run_review(interaction, message, True)
                return

            # Denials ask for a reason first. The lock is only taken once the
            # modal is submitted, so closing the modal can't leave it stuck.
            await interaction.response.send_modal(DenyReasonModal(self, message))
            try:
                # Reset the select so "Deny" doesn't stay highlighted if cancelled.
                await message.edit(view=review_view(self.user_id))
            except (discord.HTTPException, AttributeError):
                pass

        async def _run_review(
            self,
            interaction: discord.Interaction,
            message: discord.Message,
            accepted: bool,
            reason: str | None = None,
        ) -> None:
            if message.id in _reviewing:
                await reply(interaction, "This submission is already being reviewed.")
                return

            _reviewing.add(message.id)
            try:
                await self._review(interaction, message, accepted, reason)
            finally:
                _reviewing.discard(message.id)

        async def _review(
            self,
            interaction: discord.Interaction,
            message: discord.Message,
            accepted: bool,
            reason: str | None = None,
        ) -> None:
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
            review_media_files: list[discord.File] = []
            dm_failed = False
            temporary_directory = tempfile.TemporaryDirectory(
                prefix="creation-review-"
            )

            try:
                # Only accepted submissions need their original attachments
                # downloaded for a second upload. Denied submissions keep their
                # existing review-channel attachments and are never downloaded.
                if accepted and attachments:
                    used_filenames: set[str] = set()
                    async with aiohttp.ClientSession() as session:
                        for index, attachment in enumerate(attachments):
                            filename = unique_filename(
                                attachment.filename or "upload",
                                used_filenames,
                            )
                            temporary_path = os.path.join(
                                temporary_directory.name,
                                f"attachment_{index}",
                            )
                            await download_to_path(
                                session,
                                str(attachment.url),
                                temporary_path,
                            )
                            media_files.append(
                                discord.File(temporary_path, filename=filename)
                            )
                elif not attachments:
                    # Recovery fallback for older submissions where only an
                    # embed image URL remains. Stream it to disk rather than
                    # loading the complete image into memory. Keep a separate
                    # upload for the review message so the media remains visible
                    # there after acceptance as well as denial.
                    image_url = source_embed.image.url if source_embed.image else None
                    if image_url and image_url.startswith(("https://", "http://")):
                        filename = (
                            image_url.split("?", 1)[0].rsplit("/", 1)[-1]
                            or "creation-image.png"
                        )
                        if "." not in filename:
                            filename += ".png"
                        filename = unique_filename(filename, set())
                        temporary_path = os.path.join(
                            temporary_directory.name,
                            "creation_image",
                        )
                        async with aiohttp.ClientSession() as session:
                            await download_to_path(
                                session,
                                image_url,
                                temporary_path,
                            )
                        review_media_files.append(
                            discord.File(temporary_path, filename=filename)
                        )
                        if accepted:
                            media_files.append(
                                discord.File(temporary_path, filename=filename)
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

                    # Publish the accepted creation with Components V2. The
                    # layout is: username + mention beside avatar, divider,
                    # every uploaded file, then the original submission text.
                    description = source_embed.description or " "
                    submitter = (
                        interaction.guild.get_member(self.user_id)
                        if interaction.guild is not None
                        else None
                    )
                    username = (
                        submitter.name
                        if submitter is not None
                        else (source_embed.author.name or f"User {self.user_id}")
                    )
                    avatar_url = (
                        str(submitter.display_avatar.url)
                        if submitter is not None
                        else source_embed.author.icon_url
                    )

                    # Components V2 messages cannot contain a legacy discord.Embed.
                    # A Container gives the layout an embed-like card background and
                    # uses the configured global embed colour as its accent.
                    published_view = discord.ui.LayoutView(timeout=None)
                    published_container = discord.ui.Container(
                        accent_colour=configured_embed_colour(manager)
                    )
                    header_text = f"## {username}\n<@{self.user_id}>"
                    if avatar_url:
                        published_container.add_item(
                            discord.ui.Section(
                                discord.ui.TextDisplay(header_text),
                                accessory=discord.ui.Thumbnail(
                                    avatar_url,
                                    description=f"{username}'s avatar",
                                ),
                            )
                        )
                    else:
                        # Older submissions may not have stored an avatar URL.
                        published_container.add_item(
                            discord.ui.TextDisplay(header_text)
                        )

                    published_container.add_item(
                        discord.ui.Separator(visible=True)
                    )
                    # These uploads are validated as images/videos at submission
                    # time, so a MediaGallery renders the actual media inline rather
                    # than only showing a downloadable file card.
                    media_gallery = discord.ui.MediaGallery()
                    for uploaded_file in media_files:
                        media_gallery.add_item(media=uploaded_file)
                    published_container.add_item(media_gallery)
                    published_container.add_item(
                        discord.ui.TextDisplay(description)
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

                # Keep the original review-channel attachments in place. If an
                # older submission only has an embed image URL, replace it with
                # a real attachment so it remains visible in the review channel.
                if attachments or review_media_files:
                    source_embed.set_image(url=None)
                    source_embed.set_thumbnail(url=None)

                status_embed = update_submission_status(
                    source_embed,
                    submitter_id=self.user_id,
                    staff_id=interaction.user.id,
                    accepted=accepted,
                    reason=reason,
                )
                edit_attachments: list[Any] = list(source_message.attachments)
                edit_attachments.extend(review_media_files)

                await source_message.edit(
                    embed=status_embed,
                    attachments=edit_attachments,
                    view=review_view(self.user_id, disabled=True),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                review_media_files = []  # discord.py has consumed/closed them

                if not accepted:
                    def quote_block(text: str, limit: int) -> str:
                        text = (text or "").strip() or "—"
                        if len(text) > limit:
                            text = text[: limit - 3].rstrip() + "..."
                        return "\n".join(
                            f"> {line}" if line.strip() else ">"
                            for line in text.splitlines()
                        )

                    submission_quote = quote_block(source_embed.description or "", 1000)
                    reason_quote = quote_block(reason or "No reason provided.", 1000)
                    try:
                        target = (
                            interaction.guild.get_member(self.user_id)
                            if interaction.guild
                            else None
                        )
                        if target is None:
                            target = await interaction.client.fetch_user(self.user_id)
                        dm_embed = discord.Embed(
                            title="Creation Submission Denied",
                            description=(
                                "Unfortunately, your creation submission has been "
                                "**denied** by staff."
                            ),
                            colour=DENIED_COLOUR,
                            timestamp=discord.utils.utcnow(),
                        )
                        dm_embed.add_field(
                            name="Your Submission",
                            value=submission_quote,
                            inline=False,
                        )
                        dm_embed.add_field(
                            name="Reason",
                            value=reason_quote,
                            inline=False,
                        )
                        dm_guild = interaction.guild
                        if dm_guild is not None:
                            dm_embed.set_footer(
                                text=dm_guild.name,
                                icon_url=dm_guild.icon.url if dm_guild.icon else None,
                            )
                        await target.send(embed=dm_embed)
                    except (discord.Forbidden, discord.HTTPException):
                        dm_failed = True
                        logger.warning(
                            "Could not DM denial reason to user %s",
                            self.user_id,
                            exc_info=True,
                        )
            except (discord.HTTPException, OSError, aiohttp.ClientError, ValueError):
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
                for media_file in review_media_files:
                    media_file.close()
                temporary_directory.cleanup()

            verdict = "accepted" if accepted else "denied"
            note = " (I couldn't DM the user.)" if (not accepted and dm_failed) else ""
            await interaction.followup.send(
                f"Submission {verdict}.{note}", ephemeral=True
            )

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
            uploaded_files: list[discord.File] = []
            with tempfile.TemporaryDirectory(prefix="creation-submission-") as temp_dir:
                try:
                    used_filenames: set[str] = set()
                    async with aiohttp.ClientSession() as session:
                        for index, attachment in enumerate(attachments):
                            filename = unique_filename(
                                attachment.filename or "upload",
                                used_filenames,
                            )
                            temporary_path = os.path.join(
                                temp_dir,
                                f"attachment_{index}",
                            )
                            await download_to_path(
                                session,
                                str(attachment.url),
                                temporary_path,
                            )
                            uploaded_files.append(
                                discord.File(temporary_path, filename=filename)
                            )

                    embed = discord.Embed(
                        description=self.details.component.value,
                        colour=configured_embed_colour(manager),
                    )
                    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
                    embed.add_field(
                        name="Information",
                        value=f"**Submitted by:** {user.mention}\n**Status:** Pending review",
                        inline=False,
                    )
                    filenames = "\n".join(
                        f"• `{uploaded_file.filename}`" for uploaded_file in uploaded_files
                    )
                    embed.add_field(
                        name=f"Attachments ({len(uploaded_files)})",
                        value=filenames[:1024],
                        inline=False,
                    )
                    # Keep all media as normal message attachments, not embed images.

                    await channel.send(
                        embed=embed,
                        files=uploaded_files,
                        view=review_view(user.id),
                    )
                except (discord.HTTPException, OSError, aiohttp.ClientError, ValueError):
                    logger.exception(
                        "Could not forward creation submission from %s", user.id
                    )
                    await reply(
                        interaction,
                        "Your submission could not be sent. Please try again later.",
                    )
                    return
                finally:
                    for uploaded_file in uploaded_files:
                        uploaded_file.close()

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
            colour=configured_embed_colour(manager),
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
                if client.user is None or old_message.author.id != client.user.id:
                    continue
                if not any(
                    embed.title == "Upload your creation" for embed in old_message.embeds
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
        channel = text_channel(message.guild, manager.creation_prompt_channel_id)
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
                embed.title == "Upload your creation" for embed in message.embeds
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
                await reply(interaction, "Submissions are currently unavailable.")
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

        await reply(interaction, f"Creation prompt sent to {channel.mention}.")

    client_key = id(client)
    if client_key not in _registered_clients:
        client.add_view(PromptView())
        client.add_dynamic_items(ReviewSelect)

        original_on_message = getattr(client, "on_message", None)

        async def combined_on_message(
            self_client: discord.Client,
            message: discord.Message,
        ) -> None:
            if original_on_message is not None:
                result = original_on_message(message)
                if hasattr(result, "__await__"):
                    await result
            await on_prompt_channel_message(message)

        client.on_message = MethodType(combined_on_message, client)
        _registered_clients.add(client_key)

    return creation_prompt
