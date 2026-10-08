from __future__ import annotations

import discord
from discord import app_commands

from create_log import LogManager, monitor_job_message


def build_test_command(manager: LogManager) -> app_commands.Command:
    @app_commands.command(
        name="test",
        description="Create synthetic forum posts to test the logger.",
    )
    @app_commands.describe(
        member_count="Number of synthetic forum posts to create (1-10000).",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def test(
        interaction: discord.Interaction,
        member_count: app_commands.Range[int, 1, 10000],
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            forum = await manager.get_forum_channel()

            if interaction.guild_id != forum.guild.id:
                await interaction.edit_original_response(
                    content="This command can only be used in the configured server.",
                )
                return

            if manager.job.running or manager.lock.locked():
                await interaction.edit_original_response(
                    content="Another logging job is already running.",
                )
                return

            await manager.start_test_job(int(member_count))

            await monitor_job_message(
                manager,
                interaction,
                "Creating test forum posts",
            )

        except Exception as exc:
            try:
                await interaction.edit_original_response(
                    content=f"Could not start the test: `{exc}`",
                    embed=None,
                    view=None,
                )
            except discord.HTTPException:
                pass

    return test
