from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from typing import Any, NamedTuple

import aiohttp
import discord
from discord import app_commands

from settings import is_main_staff_or_owner

logger = logging.getLogger("discord-log-bot.group_scan")

GROUP_ID = 33955777
GROUP_ROLE: str | int | list[str | int] = 107818007
BOOSTER_ID = 1560413737578659840
BOOSTER_BUTTON_THREAD = 1571124508184547328
BOOSTER_RANK = 1561794561930428536
ROLESHIFT_CHANNEL = 1561789572894036040
GROUP_NAME = "Mythic Reliques"
ROLESHIFT_FROM = "Booster"
ROLESHIFT_TO = "Player"


def get_group_rank_settings() -> tuple[int, int, int, int, str, str, str]:
    return (
        int(BOOSTER_ID),
        int(BOOSTER_BUTTON_THREAD),
        int(BOOSTER_RANK),
        int(ROLESHIFT_CHANNEL),
        str(GROUP_NAME),
        str(ROLESHIFT_FROM),
        str(ROLESHIFT_TO),
    )


def apply_group_rank_settings(
    booster_role_id: int,
    username_thread_id: int,
    booster_rank_channel_id: int,
    roleshift_channel_id: int,
    group_name: str,
    roleshift_from: str,
    roleshift_to: str,
) -> None:
    global BOOSTER_ID, BOOSTER_BUTTON_THREAD, BOOSTER_RANK, ROLESHIFT_CHANNEL
    global GROUP_NAME, ROLESHIFT_FROM, ROLESHIFT_TO

    ids = (
        booster_role_id,
        username_thread_id,
        booster_rank_channel_id,
        roleshift_channel_id,
    )
    if any(int(value) < 0 for value in ids):
        raise ValueError("Booster group-rank role and channel IDs cannot be negative.")

    labels = (group_name.strip(), roleshift_from.strip(), roleshift_to.strip())
    if any(not value for value in labels):
        raise ValueError("Group name and roleshift labels cannot be blank.")

    BOOSTER_ID = int(booster_role_id)
    BOOSTER_BUTTON_THREAD = int(username_thread_id)
    BOOSTER_RANK = int(booster_rank_channel_id)
    ROLESHIFT_CHANNEL = int(roleshift_channel_id)
    GROUP_NAME, ROLESHIFT_FROM, ROLESHIFT_TO = labels


MIN_USERNAME_LENGTH = 5
MAX_USERNAME_LENGTH = 20
MAX_FILE_BYTES = 2 * 1024 * 1024
ROBLOX_PAGE_SIZE = 100
ROBLOX_MAX_PAGES = 200
ROBLOX_MAX_RETRIES = 5
BOOSTER_CACHE_TTL = 120.0
BOOSTER_CACHE_MIN_REFRESH = 10.0
WINDOW_TTL = 7200.0  # seconds a /fetch_rank list can still be used for logging
WINDOW_MAX = 20  # lists kept in memory at once
WATCH_INTERVAL = 60.0  # seconds between checks for newly deranked users
WATCH_DURATION = 14 * 60.0  # the ephemeral message can only be edited for ~15 minutes
SUBMIT_COOLDOWN = 60.0  # seconds between rank requests from the same user
RANKED_HISTORY_LIMIT = 2000
LIST_FIELD_LIMIT = 1000
MAX_LIST_FIELDS = 3

ROBLOX_GROUPS_API = "https://groups.roblox.com"
ROBLOX_USERS_API = "https://users.roblox.com"
ROBLOX_THUMBNAILS_API = "https://thumbnails.roblox.com"

DEFAULT_EMBED_COLOUR = discord.Colour.from_rgb(36, 36, 41)
ACCEPTED_COLOUR = discord.Colour.from_rgb(46, 204, 113)
DENIED_COLOUR = discord.Colour.from_rgb(231, 76, 60)
DERANKED_COLOUR = discord.Colour.from_rgb(149, 165, 166)

PROMPT_TITLE = "Booster Rank Request"
REQUEST_BUTTON_ID = "booster:request"
RANK_BUTTON_ID = "booster:rank"

# The approval message in BOOSTER_RANK is the only record of a request. Its status line moves
# through: Not ranked -> Accepted -> Deranked (once its roleshift has been logged).
# "Pending derank" is read as Accepted (older versions of this bot wrote it).
STATUS_ACCEPTED = "**Status:** Accepted"
STATUS_PENDING = "**Status:** Pending derank"
STATUS_DERANKED = "**Status:** Deranked"

_ID_RE = re.compile(r"<@!?(\d{17,20})>|(?<![\d&#:])(\d{17,20})(?!\d)")
_TOKEN_RE = re.compile(r"<(?:@&\d+|#\d+|a?:\w+:\d+|t:\d+(?::[a-zA-Z])?)>")
_URL_RE = re.compile(r"https?://\S+")
_DISPLAY_LABEL_RE = re.compile(r"^\W*(?:display\s*name|display|nick\s*name|nick)\b", re.I)
_USER_LABEL_RE = re.compile(
    r"^\W*(?:my\s+)?"
    r"(?:(?:roblox\s*|rbx\s*)?(?:user\s*name|user|name|ign|account|profile)|roblox|rbx)"
    r"\s*(?:[:=\-\u2013\u2014]|\bis\b)\s*(?P<value>\S.*)$",
    re.I,
)
_TRAILING_BRACKET_RE = re.compile(r"\s*[(\[{].*$")
_TRIM_CHARS = " \t\r\n*`~|\"'\u201c\u201d\u2018\u2019<>.,!?:;_-@/\\"
_USERNAME_RE = re.compile(r"[A-Za-z0-9]+(?:_+[A-Za-z0-9]+)*")
_SUBMIT_RE = re.compile(r"[A-Za-z0-9_]+")
_TIME_ONLY_RE = re.compile(r"^\[?\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?\]?$")


def clean_candidate(value: str) -> str | None:
    value = _TRAILING_BRACKET_RE.sub("", value).strip(_TRIM_CHARS)
    if not MIN_USERNAME_LENGTH <= len(value) <= MAX_USERNAME_LENGTH:
        return None
    return value if _USERNAME_RE.fullmatch(value) else None


def extract_from_line(line: str) -> str | None:
    line = _URL_RE.sub(" ", _TOKEN_RE.sub(" ", line)).strip()
    if not line or _DISPLAY_LABEL_RE.match(line):
        return None
    labelled = _USER_LABEL_RE.match(line)
    if labelled:
        return clean_candidate(labelled.group("value"))
    return clean_candidate(line)


def scan_text(text: str, wanted: set[str]) -> dict[str, set[int | None]]:
    found: dict[str, set[int | None]] = {}
    current_id: int | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _ID_RE.search(line)
        if match:
            current_id = int(match.group(1) or match.group(2))
            line = _ID_RE.sub(" ", line).strip()
            if not line:
                continue
        if _TIME_ONLY_RE.match(line):
            continue
        candidate = extract_from_line(line)
        if candidate is None:
            continue
        key = candidate.lower()
        if key in wanted:
            found.setdefault(key, set()).add(current_id)
    return found


class RobloxError(Exception):
    pass


async def _roblox_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any = None,
) -> dict[str, Any]:
    for attempt in range(ROBLOX_MAX_RETRIES + 1):
        delay = 0.0
        async with session.request(method, url, params=params, json=json) as response:
            status = response.status
            if status == 200:
                return await response.json()
            if status == 404:
                raise RobloxError("Roblox group, role or user not found.")
            if status == 429 or status >= 500:
                if attempt == ROBLOX_MAX_RETRIES:
                    raise RobloxError(
                        f"Roblox kept rate limiting or failing (HTTP {status}). Try again later."
                    )
                try:
                    delay = float(response.headers.get("Retry-After", ""))
                except ValueError:
                    delay = float(2**attempt)
                delay = min(max(delay, 1.0), 30.0)
            else:
                raise RobloxError(f"Roblox API returned HTTP {status}.")
        await asyncio.sleep(delay)
    raise RobloxError("Roblox request failed.")


def _new_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))


def wanted_roles() -> set[str]:
    raw = GROUP_ROLE if isinstance(GROUP_ROLE, (list, tuple, set)) else [GROUP_ROLE]
    return {str(item).strip().lower() for item in raw if str(item).strip()}


def role_matches(role: dict[str, Any]) -> bool:
    wanted = wanted_roles()
    return (
        str(role.get("name", "")).strip().lower() in wanted or str(role.get("id")) in wanted
    )


async def fetch_role_users(
    session: aiohttp.ClientSession,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = await _roblox_request(session, "GET", f"{ROBLOX_GROUPS_API}/v1/groups/{GROUP_ID}/roles")
    all_roles = data.get("roles", [])
    roles = [r for r in all_roles if role_matches(r)]
    if not roles:
        available = ", ".join(str(r.get("name")) for r in all_roles) or "none"
        raise RobloxError(
            f"None of the configured roles were found in that group. Available roles: {available}"
        )

    users: dict[int, dict[str, Any]] = {}
    for role in roles:
        cursor: str | None = None
        for _ in range(ROBLOX_MAX_PAGES):
            params: dict[str, Any] = {"limit": ROBLOX_PAGE_SIZE, "sortOrder": "Asc"}
            if cursor:
                params["cursor"] = cursor
            page = await _roblox_request(
                session,
                "GET",
                f"{ROBLOX_GROUPS_API}/v1/groups/{GROUP_ID}/roles/{role['id']}/users",
                params=params,
            )
            for entry in page.get("data", []):
                users[int(entry.get("userId", 0))] = entry
            cursor = page.get("nextPageCursor")
            if not cursor:
                break
            await asyncio.sleep(0.25)
    return roles, list(users.values())


async def lookup_user(session: aiohttp.ClientSession, username: str) -> dict[str, Any] | None:
    data = await _roblox_request(
        session,
        "POST",
        f"{ROBLOX_USERS_API}/v1/usernames/users",
        json={"usernames": [username], "excludeBannedUsers": True},
    )
    users = data.get("data", [])
    return users[0] if users else None


async def fetch_headshot(session: aiohttp.ClientSession, user_id: int) -> str | None:
    try:
        data = await _roblox_request(
            session,
            "GET",
            f"{ROBLOX_THUMBNAILS_API}/v1/users/avatar-headshot",
            params={"userIds": user_id, "size": "150x150", "format": "Png", "isCircular": "false"},
        )
    except (RobloxError, aiohttp.ClientError, asyncio.TimeoutError):
        return None
    entries = data.get("data", [])
    if entries and entries[0].get("state") == "Completed":
        return entries[0].get("imageUrl")
    return None


_booster_cache: dict[str, Any] = {"ids": frozenset(), "at": float("-inf")}
_booster_lock = asyncio.Lock()


def store_booster_cache(users: list[dict[str, Any]]) -> None:
    _booster_cache["ids"] = frozenset(int(u.get("userId", 0)) for u in users)
    _booster_cache["at"] = time.monotonic()


async def group_status(
    session: aiohttp.ClientSession, user_id: int
) -> tuple[str, list[str]]:
    if (
        user_id in _booster_cache["ids"]
        and time.monotonic() - _booster_cache["at"] < BOOSTER_CACHE_TTL
    ):
        return "ranked", []

    data = await _roblox_request(
        session,
        "GET",
        f"{ROBLOX_GROUPS_API}/v2/users/{user_id}/groups/roles",
        params={"includeLocked": "true"},
    )
    in_group = False
    found_names: list[str] = []
    for entry in data.get("data", []):
        group = entry.get("group") or {}
        if int(group.get("id", 0)) != GROUP_ID:
            continue
        in_group = True
        held = entry.get("roles")
        if not isinstance(held, list):
            held = []
        if isinstance(entry.get("role"), dict):
            held.append(entry["role"])
        for role in held:
            if isinstance(role, dict):
                found_names.append(f"{role.get('name')} ({role.get('id')})")
                if role_matches(role):
                    return "ranked", found_names
    if not in_group:
        return "none", found_names

    async with _booster_lock:
        if time.monotonic() - _booster_cache["at"] >= BOOSTER_CACHE_MIN_REFRESH:
            _, users = await fetch_role_users(session)
            store_booster_cache(users)
        is_booster = user_id in _booster_cache["ids"]
    if is_booster:
        return "ranked", found_names
    logger.info("User %s is in group %s, primary roles: %s", user_id, GROUP_ID, found_names)
    return "member", found_names


def profile_url(user_id: int) -> str:
    return f"https://www.roblox.com/users/{user_id}/profile"


def configured_colour(manager: Any) -> discord.Colour:
    colour = getattr(manager, "embed_colour", None)
    return colour if isinstance(colour, discord.Colour) else DEFAULT_EMBED_COLOUR


ROLESHIFT_BUTTON_ID = "booster:roleshift"
ROLESHIFT_CONFIRM_ID = "booster:roleshift:confirm"
ROLESHIFT_CANCEL_ID = "booster:roleshift:cancel"
_CONFIRM_COUNT_RE = re.compile(r"\((\d+)\) deranked members")
_RANK_ID_RE = re.compile(rf"{RANK_BUTTON_ID}:(\d+):([A-Za-z0-9_]+)")
_registered_fetch_clients: set[int] = set()


class Tracked(NamedTuple):
    message_id: int
    state: str  # "accepted" | "pending" | "deranked"
    name: str


class Candidate(NamedTuple):
    discord_id: int
    roblox_id: int
    name: str
    message_id: int | None  # approval message to mark Deranked (None for file checks)


class DerankScan(NamedTuple):
    tracked: int
    still_boosting: int
    cleared: int
    to_derank: list[Candidate]
    truncated: bool


class Window:
    """The people listed by one /fetch_rank message. Kept in memory only, never saved."""

    __slots__ = ("at", "guild_id", "items", "embed")

    def __init__(self, guild_id: int, items: list[Candidate], embed: discord.Embed) -> None:
        self.at = time.monotonic()
        self.guild_id = guild_id
        self.items = items
        self.embed = embed


def pack_lines(lines: list[str], limit: int, max_fields: int) -> tuple[list[str], int]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    shown = 0
    for line in lines:
        add = len(line) + 1
        if current and size + add > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
            if len(chunks) >= max_fields:
                break
        current.append(line)
        size += add
        shown += 1
    if current and len(chunks) < max_fields:
        chunks.append("\n".join(current))
    return chunks, shown


def pack_roleshift_messages(
    entries: list[tuple[int, str]], staff_id: int, limit: int = 2000
) -> list[str]:
    if not entries:
        return []
    attribution = f"- {ROLESHIFT_FROM} -> {ROLESHIFT_TO} · Deranked by <@{staff_id}>"
    if len(entries) == 1:
        discord_id, roblox_name = entries[0]
        return [
            f"- {GROUP_NAME} Roblox group\n"
            f"- <@{discord_id}>\n"
            f"- {roblox_name}\n"
            f"{attribution}"
        ]

    header = f"- {GROUP_NAME} Roblox group\n\n"
    suffix = f"\n\n{attribution}"
    chunks: list[str] = []
    current: list[str] = []
    for index, (discord_id, roblox_name) in enumerate(entries):
        line = f"<@{discord_id}> · {roblox_name}"
        if index < len(entries) - 1:
            line += ","
        candidate = header + "\n".join([*current, line]) + suffix
        if current and len(candidate) > limit:
            chunks.append(header + "\n".join(current) + suffix)
            current = []
        current.append(line)
    if current:
        chunks.append(header + "\n".join(current) + suffix)
    return chunks


async def resolve_usernames(
    session: aiohttp.ClientSession, names: list[str]
) -> dict[str, tuple[int, str]]:
    found: dict[str, tuple[int, str]] = {}
    for start in range(0, len(names), 100):
        data = await _roblox_request(
            session,
            "POST",
            f"{ROBLOX_USERS_API}/v1/usernames/users",
            json={"usernames": names[start : start + 100], "excludeBannedUsers": False},
        )
        for user in data.get("data", []):
            found[str(user.get("requestedUsername", "")).lower()] = (
                int(user["id"]),
                str(user.get("name")),
            )
    return found


def _request_state(text: str) -> str | None:
    if STATUS_ACCEPTED in text or STATUS_PENDING in text:
        return "accepted"
    if STATUS_DERANKED in text:
        return "deranked"
    return None


async def ensure_members_loaded(guild: discord.Guild) -> None:
    """The derank check relies on the member cache; an unloaded cache would make boosters
    look like they left."""
    if not guild.chunked:
        try:
            await guild.chunk()
        except discord.HTTPException:
            logger.warning("Could not load the member list for guild %s", guild.id, exc_info=True)


def _is_boosting(guild: discord.Guild, discord_id: int) -> bool:
    member = guild.get_member(discord_id)
    return member is not None and member.get_role(BOOSTER_ID) is not None


async def read_ranked_requests(
    channel: discord.TextChannel, bot_id: int
) -> tuple[dict[tuple[int, int], Tracked], dict[tuple[int, str], Tracked], bool]:
    """Stream the review channel (newest first) and keep the newest request per user/Roblox
    account from the bot's own messages. Returns (by Roblox ID, older name-based requests,
    whether the history limit was reached)."""
    ranked: dict[tuple[int, int], Tracked] = {}
    legacy: dict[tuple[int, str], Tracked] = {}
    scanned = 0
    async for message in channel.history(limit=RANKED_HISTORY_LIMIT):
        scanned += 1
        if message.author.id != bot_id or not message.embeds:
            continue
        embed = message.embeds[0]
        text = (embed.description or "") + "".join(f.value or "" for f in embed.fields)
        state = _request_state(text)
        if state is None:
            continue
        for row in message.components:
            for component in getattr(row, "children", ()):
                match = _RANK_ID_RE.fullmatch(getattr(component, "custom_id", "") or "")
                if not match:
                    continue
                discord_id, ref = int(match.group(1)), match.group(2)
                tracked = Tracked(message.id, state, embed_roblox_name(embed) or ref)
                if ref.isdigit():
                    ranked.setdefault((discord_id, int(ref)), tracked)
                else:
                    legacy.setdefault((discord_id, ref), tracked)
    return ranked, legacy, scanned >= RANKED_HISTORY_LIMIT


async def set_request_state(
    channel: discord.TextChannel, message_id: int, status: str, colour: discord.Colour
) -> bool:
    try:
        message = await channel.fetch_message(message_id)
        if not message.embeds:
            return False
        embed = message.embeds[0].copy()
        set_status(embed, status)
        embed.colour = colour
        await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.HTTPException:
        logger.warning("Could not update booster request message %s", message_id, exc_info=True)
        return False


class ReviewChannelUnavailable(Exception):
    pass


async def booster_ids(max_age: float) -> frozenset[int]:
    """Roblox user IDs currently in the booster role(s); reuses the cache if it is fresh."""
    async with _booster_lock:
        if time.monotonic() - _booster_cache["at"] >= max_age:
            async with _new_session() as session:
                _, users = await fetch_role_users(session)
            store_booster_cache(users)
        return _booster_cache["ids"]


async def deranked_now(
    guild: discord.Guild, items: list[Candidate], max_age: float
) -> list[Candidate]:
    """The listed people who have since been removed from the Roblox booster role (and
    haven't regained the Discord booster role)."""
    ids = await booster_ids(max_age)
    await ensure_members_loaded(guild)
    return [c for c in items if c.roblox_id not in ids and not _is_boosting(guild, c.discord_id)]


_windows: dict[int, Window] = {}
_watchers: dict[int, asyncio.Task] = {}


def store_window(message_id: int, window: Window) -> None:
    now = time.monotonic()
    for key in [k for k, w in _windows.items() if now - w.at > WINDOW_TTL]:
        del _windows[key]
    while len(_windows) >= WINDOW_MAX:
        del _windows[next(iter(_windows))]
    _windows[message_id] = window


def setup_fetch_rank(
    client: discord.Client, *, manager: Any, owner_id: int
) -> app_commands.Command:
    lock = asyncio.Lock()
    roleshift_lock = asyncio.Lock()

    def is_staff(user: discord.User | discord.Member) -> bool:
        return is_main_staff_or_owner(user, manager=manager, owner_id=owner_id)

    async def reply(interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    def bot_id() -> int:
        return client.user.id if client.user else 0

    async def collect_derank_data(guild: discord.Guild) -> DerankScan:
        await ensure_members_loaded(guild)
        review_channel = guild.get_channel(BOOSTER_RANK) if BOOSTER_RANK else None
        if not isinstance(review_channel, discord.TextChannel):
            raise ReviewChannelUnavailable

        ranked, legacy, truncated = await read_ranked_requests(review_channel, bot_id())

        async with _new_session() as session:
            _, users = await fetch_role_users(session)
            store_booster_cache(users)
            if legacy:
                resolved = await resolve_usernames(
                    session, sorted({ref for _, ref in legacy})
                )
                for (discord_id, ref), tracked in legacy.items():
                    hit = resolved.get(ref.lower())
                    if hit:
                        ranked.setdefault(
                            (discord_id, hit[0]),
                            Tracked(tracked.message_id, tracked.state, hit[1] or tracked.name),
                        )

        booster_names = {
            int(user.get("userId", 0)): str(user.get("name") or user.get("username") or "")
            for user in users
        }
        del users

        tracked_count = 0
        still_boosting = 0
        cleared = 0
        to_derank: list[Candidate] = []
        for (discord_id, roblox_id), tracked in ranked.items():
            if tracked.state == "deranked":
                continue
            tracked_count += 1
            if _is_boosting(guild, discord_id):
                still_boosting += 1
            elif roblox_id in booster_names:
                to_derank.append(
                    Candidate(
                        discord_id,
                        roblox_id,
                        booster_names[roblox_id] or tracked.name,
                        tracked.message_id,
                    )
                )
            else:
                cleared += 1
        to_derank.sort(key=lambda c: c.name.lower())
        return DerankScan(tracked_count, still_boosting, cleared, to_derank, truncated)

    async def mark_deranked(guild: discord.Guild, items: list[Candidate], staff_id: int) -> int:
        """Mark the approval messages Deranked. Returns how many couldn't be updated."""
        channel = guild.get_channel(BOOSTER_RANK) if BOOSTER_RANK else None
        marked = [c for c in items if c.message_id is not None]
        if not marked:
            return 0
        if not isinstance(channel, discord.TextChannel):
            return len(marked)
        timestamp = int(discord.utils.utcnow().timestamp())
        status = f"{STATUS_DERANKED} by <@{staff_id}> · <t:{timestamp}:R>"
        failed = 0
        for item in marked:
            if not await set_request_state(channel, item.message_id, status, DERANKED_COLOUR):
                failed += 1
        return failed

    async def get_roleshift_channel(guild: discord.Guild) -> discord.abc.Messageable | None:
        if not ROLESHIFT_CHANNEL:
            return None
        channel = guild.get_channel(ROLESHIFT_CHANNEL)
        if channel is None:
            try:
                channel = await client.fetch_channel(ROLESHIFT_CHANNEL)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None
        if channel.guild.id != guild.id:
            return None
        return channel

    def window_embed(window: Window, ready: int) -> discord.Embed:
        embed = window.embed.copy()
        if ready:
            embed.description = (
                f"{embed.description or ''}\n"
                f"**{ready}** of **{len(window.items)}** deranked and ready to log."
            )
        return embed

    class ConfirmRoleshiftView(discord.ui.View):
        def __init__(self, window_id: int) -> None:
            super().__init__(timeout=900)
            self.window_id = window_id

        @discord.ui.button(
            label="Confirm",
            style=discord.ButtonStyle.success,
            custom_id=ROLESHIFT_CONFIRM_ID,
        )
        async def confirm(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            if not is_staff(interaction.user):
                await interaction.response.send_message(
                    "You need the configured staff role to do this.", ephemeral=True
                )
                return
            guild = interaction.guild
            window = _windows.get(self.window_id)
            if guild is None or window is None:
                await interaction.response.edit_message(
                    content="This list has expired. Run `/fetch_rank` again.", view=None
                )
                return
            message_content = interaction.message.content if interaction.message else ""
            counts = list(_CONFIRM_COUNT_RE.finditer(message_content or ""))
            confirmed_count = int(counts[-1].group(1)) if counts else None
            if roleshift_lock.locked():
                await interaction.response.edit_message(
                    content="Roleshifts are already being processed. Please try again shortly.",
                    view=None,
                )
                return

            await interaction.response.defer()
            async with roleshift_lock:
                channel = await get_roleshift_channel(guild)
                if channel is None:
                    await interaction.edit_original_response(
                        content="The configured roleshift channel wasn't found as a text channel in this server.",
                        view=None,
                    )
                    return
                try:
                    items = await deranked_now(guild, window.items, max_age=15.0)
                except (RobloxError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    logger.warning("Roblox check failed before logging roleshifts", exc_info=True)
                    await interaction.edit_original_response(
                        content=f"Couldn't check Roblox right now ({exc}). Please try again.",
                        view=None,
                    )
                    return
                if not items:
                    await interaction.edit_original_response(
                        content="There are no longer any deranked members to log.", view=None
                    )
                    return
                if confirmed_count is not None and len(items) != confirmed_count:
                    await interaction.edit_original_response(
                        content=(
                            "The deranked count changed. Are you sure you would like to log "
                            f"({len(items)}) deranked members?"
                        ),
                        view=ConfirmRoleshiftView(self.window_id),
                    )
                    return

                messages = pack_roleshift_messages(
                    [(c.discord_id, c.name) for c in items], interaction.user.id
                )
                sent = 0
                try:
                    for content in messages:
                        await channel.send(
                            content,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        sent += 1
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Could not send roleshift logs to the roleshift channel")
                    await interaction.edit_original_response(
                        content=(
                            f"I could only send {sent} of {len(messages)} roleshift log message(s). "
                            "Some entries may not have been logged; check the roleshift channel "
                            "before trying again."
                        ),
                        view=None,
                    )
                    return

                failed = await mark_deranked(guild, items, interaction.user.id)
                logged = {(c.discord_id, c.roblox_id) for c in items}
                window.items = [
                    c for c in window.items if (c.discord_id, c.roblox_id) not in logged
                ]
                note = (
                    f"\n{failed} request message(s) couldn't be marked as deranked."
                    if failed
                    else ""
                )
                await interaction.edit_original_response(
                    content=(
                        f"Successfully logged {len(items)} deranked member(s) in "
                        f"<#{ROLESHIFT_CHANNEL}>.{note}"
                    ),
                    view=None,
                )

        @discord.ui.button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            custom_id=ROLESHIFT_CANCEL_ID,
        )
        async def cancel(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            if not is_staff(interaction.user):
                await interaction.response.send_message(
                    "You need the configured staff role to do this.", ephemeral=True
                )
                return
            await interaction.response.edit_message(
                content="Roleshift logging cancelled.", view=None
            )

    class RoleshiftView(discord.ui.View):
        def __init__(self, *, enabled: bool = True, count: int | None = None) -> None:
            super().__init__(timeout=None)
            self.log_roleshifts.disabled = not enabled
            if count:
                self.log_roleshifts.label = f"Log roleshifts ({count})"

        @discord.ui.button(
            label="Log roleshifts",
            style=discord.ButtonStyle.primary,
            custom_id=ROLESHIFT_BUTTON_ID,
        )
        async def log_roleshifts(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            if not is_staff(interaction.user):
                await reply(interaction, "You need the configured staff role to do this.")
                return
            guild = interaction.guild
            if guild is None:
                await reply(interaction, "Run this button inside the server.")
                return
            message = interaction.message
            window = _windows.get(message.id) if message else None
            if window is None or message is None:
                await reply(interaction, "This list has expired. Run `/fetch_rank` again.")
                return
            if not window.items:
                await reply(interaction, "Everyone on this list has already been logged.")
                return
            if not ROLESHIFT_CHANNEL:
                await reply(
                    interaction,
                    "Set the roleshift channel in `DEFAULT_SETTINGS` (`settings.py`) first.",
                )
                return
            if roleshift_lock.locked():
                await reply(interaction, "Roleshifts are already being processed. Please try again shortly.")
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            async with roleshift_lock:
                if await get_roleshift_channel(guild) is None:
                    await interaction.edit_original_response(
                        content="The configured roleshift channel wasn't found as a text channel in this server.",
                    )
                    return
                try:
                    items = await deranked_now(guild, window.items, max_age=30.0)
                except (RobloxError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    logger.warning("Roblox check failed while preparing roleshifts", exc_info=True)
                    await interaction.edit_original_response(
                        content=f"Couldn't check Roblox right now ({exc}). Please try again."
                    )
                    return
                if not items:
                    await interaction.edit_original_response(
                        content="No one on this list has been unranked yet."
                    )
                    return
                await interaction.edit_original_response(
                    content=(
                        "Are you sure you would like to log "
                        f"({len(items)}) deranked members?"
                    ),
                    view=ConfirmRoleshiftView(message.id),
                )

    async def watch_window(interaction: discord.Interaction, message_id: int) -> None:
        """Re-check Roblox every minute while the message can still be edited, and unlock
        the button as soon as someone on the list has been deranked."""
        deadline = time.monotonic() + WATCH_DURATION
        shown = 0
        try:
            while time.monotonic() + WATCH_INTERVAL < deadline:
                await asyncio.sleep(WATCH_INTERVAL)
                window = _windows.get(message_id)
                guild = interaction.guild
                if window is None or guild is None or not window.items:
                    return
                try:
                    ready = len(
                        await deranked_now(guild, window.items, max_age=WATCH_INTERVAL * 0.75)
                    )
                except (RobloxError, aiohttp.ClientError, asyncio.TimeoutError):
                    continue
                if ready != shown:
                    shown = ready
                    try:
                        await interaction.edit_original_response(
                            embed=window_embed(window, shown),
                            view=RoleshiftView(enabled=shown > 0, count=shown),
                        )
                    except discord.HTTPException:
                        return
                if shown and shown >= len(window.items):
                    return
            # The message is about to become uneditable. Leave the button usable so a click
            # (which re-checks Roblox live) still works afterwards.
            window = _windows.get(message_id)
            if window is not None and window.items and shown == 0:
                try:
                    await interaction.edit_original_response(view=RoleshiftView(enabled=True))
                except discord.HTTPException:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Derank watcher failed")

    async def publish_window(
        interaction: discord.Interaction,
        guild: discord.Guild,
        embed: discord.Embed,
        items: list[Candidate],
        **extra: Any,
    ) -> None:
        kwargs: dict[str, Any] = {"embed": embed, "ephemeral": True, "wait": True, **extra}
        if items:
            kwargs["view"] = RoleshiftView(enabled=False)
        message = await interaction.followup.send(**kwargs)
        if not items:
            return
        store_window(message.id, Window(guild.id, items, embed.copy()))
        previous = _watchers.pop(guild.id, None)
        if previous is not None:
            previous.cancel()
        task = asyncio.create_task(watch_window(interaction, message.id))
        _watchers[guild.id] = task
        task.add_done_callback(
            lambda t, gid=guild.id: _watchers.get(gid) is t and _watchers.pop(gid, None)
        )

    async def run_derank_check(interaction: discord.Interaction, guild: discord.Guild) -> None:
        try:
            scan = await collect_derank_data(guild)
        except ReviewChannelUnavailable:
            await reply(interaction, "The `BOOSTER_RANK` channel wasn't found as a text channel.")
            return
        except discord.HTTPException:
            logger.exception("Could not read the booster review channel")
            await reply(
                interaction,
                "I couldn't read the review channel. I need View Channel and Read Message History there.",
            )
            return
        except RobloxError as exc:
            await reply(interaction, f"Roblox lookup failed: {exc}")
            return
        except (aiohttp.ClientError, asyncio.TimeoutError):
            logger.exception("Roblox request failed")
            await reply(interaction, "Couldn't reach Roblox. Please try again later.")
            return

        need = len(scan.to_derank)
        description = (
            f"**{need}** user{'s' if need != 1 else ''} still need{'s' if need == 1 else ''} "
            "to be deranked.\nThe **Log roleshifts** button unlocks once you've deranked someone."
            if need
            else "Nobody still needs to be deranked."
        )
        embed = discord.Embed(
            title="Booster Derank Check",
            description=description,
            colour=DENIED_COLOUR if need else ACCEPTED_COLOUR,
        )
        embed.add_field(name="Tracked", value=f"`{scan.tracked}`", inline=True)
        embed.add_field(name="Still boosting", value=f"`{scan.still_boosting}`", inline=True)
        embed.add_field(name="Already cleared", value=f"`{scan.cleared}`", inline=True)

        if scan.to_derank:
            rows = [
                f"`{index}.` **<@{c.discord_id}>** · `{c.name}`"
                + ("" if guild.get_member(c.discord_id) else " · *left server*")
                for index, c in enumerate(scan.to_derank, 1)
            ]
            chunks, shown = pack_lines(rows, LIST_FIELD_LIMIT, MAX_LIST_FIELDS)
            for position, chunk in enumerate(chunks):
                embed.add_field(
                    name=f"Needs deranking ({need})" if position == 0 else "\u200b",
                    value=chunk,
                    inline=False,
                )
            if shown < len(rows):
                embed.add_field(
                    name="\u200b",
                    value=f"*+{len(rows) - shown} more, all included when logging.*",
                    inline=False,
                )
        if scan.truncated:
            embed.set_footer(
                text=(
                    f"Only the newest {RANKED_HISTORY_LIMIT} review-channel messages were read; "
                    "older requests aren't tracked."
                )
            )
        await publish_window(interaction, guild, embed, scan.to_derank)

    async def run_file_check(
        interaction: discord.Interaction, guild: discord.Guild, thread_file: discord.Attachment
    ) -> None:
        filename = (thread_file.filename or "").lower()
        if not (
            filename.endswith(".txt") or (thread_file.content_type or "").startswith("text/plain")
        ):
            await reply(interaction, "Please attach a `.txt` file.")
            return
        if thread_file.size > MAX_FILE_BYTES:
            await reply(
                interaction,
                f"That file is too large. The limit is {MAX_FILE_BYTES // (1024 * 1024)} MB.",
            )
            return
        try:
            raw = await thread_file.read()
        except discord.HTTPException:
            await reply(interaction, "I couldn't download that file. Please try again.")
            return
        text = raw.decode("utf-8-sig", errors="replace")
        del raw

        await ensure_members_loaded(guild)
        try:
            async with _new_session() as session:
                roles, role_users = await fetch_role_users(session)
                store_booster_cache(role_users)
        except RobloxError as exc:
            await reply(interaction, f"Roblox lookup failed: {exc}")
            return
        except (aiohttp.ClientError, asyncio.TimeoutError):
            logger.exception("Roblox request failed")
            await reply(interaction, "Couldn't reach Roblox. Please try again later.")
            return

        role_lookup: dict[str, tuple[int, str]] = {}
        for entry in role_users:
            username = str(entry.get("name") or entry.get("username") or "")
            if username:
                role_lookup[username.lower()] = (int(entry.get("userId", 0)), username)
        del role_users

        found = scan_text(text, set(role_lookup))
        missing: list[tuple[str, int, int, bool]] = []
        manual: list[tuple[str, int]] = []
        verified = 0
        for key in sorted(found):
            roblox_id, roblox_name = role_lookup[key]
            has_booster = False
            candidate: tuple[int, bool] | None = None
            for discord_id in found[key]:
                if discord_id is None:
                    continue
                member = guild.get_member(discord_id)
                if member is not None and member.get_role(BOOSTER_ID) is not None:
                    has_booster = True
                elif candidate is None:
                    candidate = (discord_id, member is not None)
            if has_booster:
                verified += 1
            elif candidate is not None:
                missing.append((roblox_name, roblox_id, candidate[0], candidate[1]))
            else:
                manual.append((roblox_name, roblox_id))
        missing.sort(key=lambda row: row[0].lower())
        manual.sort(key=lambda row: row[0].lower())

        description = (
            f"**{len(missing)}** ranked user{'s' if len(missing) != 1 else ''} in "
            f"`{thread_file.filename}` lack the booster role.\n"
            "The **Log roleshifts** button unlocks once you've deranked someone."
            if missing
            else "Every ranked user found in the file has the booster role."
        )
        embed = discord.Embed(
            title="Booster Rank Check",
            description=description,
            colour=DENIED_COLOUR if missing else ACCEPTED_COLOUR,
        )
        embed.add_field(name="Ranked in group", value=f"`{len(role_lookup)}`", inline=True)
        embed.add_field(name="Found in file", value=f"`{len(found)}`", inline=True)
        embed.add_field(name="Have booster role", value=f"`{verified}`", inline=True)

        truncated = False
        rows = [
            f"`{i}.` **<@{d}>** · `{n}`" + ("" if ins else " · *not in server*")
            for i, (n, r, d, ins) in enumerate(missing, 1)
        ]
        if rows:
            chunks, shown = pack_lines(rows, LIST_FIELD_LIMIT, MAX_LIST_FIELDS)
            truncated = shown < len(rows)
            for position, chunk in enumerate(chunks):
                embed.add_field(
                    name=f"Missing the booster role ({len(missing)})" if position == 0 else "\u200b",
                    value=chunk,
                    inline=False,
                )
            if truncated:
                embed.add_field(
                    name="\u200b",
                    value=f"*+{len(rows) - shown} more in the attached file.*",
                    inline=False,
                )
        if manual:
            manual_rows = [f"`{n}`" for n, r in manual]
            chunks, shown = pack_lines(manual_rows, LIST_FIELD_LIMIT, 1)
            truncated = truncated or shown < len(manual_rows)
            embed.add_field(
                name=f"No Discord ID above the name ({len(manual)})",
                value=chunks[0],
                inline=False,
            )

        extra: dict[str, Any] = {}
        if truncated:
            report = [
                f"MISSING BOOSTER ROLE ({len(missing)}):",
                *[
                    f"<@{d}> · {n}" + ("" if ins else " | not in server")
                    for n, r, d, ins in missing
                ],
                "",
                f"NO DISCORD ID ({len(manual)}):",
                *[n for n, r in manual],
            ]
            extra["file"] = discord.File(
                io.BytesIO("\n".join(report).encode("utf-8")), filename="fetch_rank_results.txt"
            )
        await publish_window(
            interaction,
            guild,
            embed,
            [Candidate(d, r, n, None) for n, r, d, _ in missing],
            **extra,
        )

    @app_commands.command(
        name="fetch_rank",
        description="Find ranked boosters who need deranking (attach a .txt to check a file instead).",
    )
    @app_commands.describe(
        thread_file="Optional .txt with a Discord user ID above each Roblox username. Leave empty to check button requests."
    )
    @app_commands.guild_only()
    async def fetch_rank(
        interaction: discord.Interaction, thread_file: discord.Attachment | None = None
    ) -> None:
        if not is_staff(interaction.user):
            await reply(
                interaction,
                "You need the configured main staff role or bot owner access to use this command.",
            )
            return
        guild = interaction.guild
        if guild is None:
            await reply(interaction, "Run this command inside the server.")
            return
        if not (GROUP_ID and wanted_roles() and BOOSTER_ID):
            await reply(
                interaction,
                "Set `GROUP_ID` and `GROUP_ROLE` in `group_scan.py`, and the booster role in "
                "`DEFAULT_SETTINGS` (`settings.py`), first.",
            )
            return
        if guild.get_role(BOOSTER_ID) is None:
            await reply(interaction, "The configured booster role wasn't found in this server.")
            return
        if lock.locked():
            await reply(interaction, "A scan is already running. Try again in a moment.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with lock:
            if thread_file is None:
                await run_derank_check(interaction, guild)
            else:
                await run_file_check(interaction, guild, thread_file)

    client_key = id(client)
    if client_key not in _registered_fetch_clients:
        # Lets a click on an older list reply "expired" instead of "interaction failed".
        client.add_view(RoleshiftView())
        _registered_fetch_clients.add(client_key)

    return fetch_rank


_registered_clients: set[int] = set()
_prompt_messages: dict[int, int] = {}
_busy: set[int] = set()
_submit_times: dict[int, float] = {}

_STATUS_RE = re.compile(r"\*\*Status:\*\*.*$", re.M)
_NAME_IN_CODE_RE = re.compile(r"`([A-Za-z0-9_]{3,20})`")


def set_status(embed: discord.Embed, status: str) -> None:
    if embed.description and _STATUS_RE.search(embed.description):
        embed.description = _STATUS_RE.sub(lambda _: status, embed.description)
        return
    index = next((i for i, f in enumerate(embed.fields) if f.name == "Information"), None)
    if index is None:
        embed.description = f"{embed.description or ''}\n{status}".strip()
        return
    value = embed.fields[index].value or ""
    value = _STATUS_RE.sub(lambda _: status, value) if _STATUS_RE.search(value) else f"{value}\n{status}"
    embed.set_field_at(index, name="Information", value=value, inline=False)


def embed_roblox_name(embed: discord.Embed) -> str | None:
    for text in [embed.description or "", *[f.value or "" for f in embed.fields]]:
        match = _NAME_IN_CODE_RE.search(text)
        if match:
            return match.group(1)
    return None


def setup_booster_user_prompt(
    client: discord.Client,
    *,
    manager: Any,
    owner_id: int,
) -> app_commands.Command:
    def text_channel(guild: discord.Guild | None, channel_id: int) -> discord.TextChannel | None:
        channel = guild.get_channel(channel_id) if guild and channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    def is_staff(user: discord.User | discord.Member) -> bool:
        return is_main_staff_or_owner(user, manager=manager, owner_id=owner_id)

    async def reply(interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    def lacks_booster_role(user: discord.User | discord.Member) -> bool:
        return (
            bool(BOOSTER_ID)
            and isinstance(user, discord.Member)
            and user.get_role(BOOSTER_ID) is None
        )

    def rank_view(user_id: int, ref: str, *, disabled: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(MarkRankedButton(user_id, ref, disabled=disabled))
        return view

    async def notify_ranked(
        interaction: discord.Interaction, user_id: int, roblox_name: str | None
    ) -> bool:
        guild = interaction.guild
        target: discord.abc.User | None = (
            guild.get_member(user_id) if guild is not None else None
        ) or client.get_user(user_id)
        try:
            if target is None:
                target = await client.fetch_user(user_id)
            embed = discord.Embed(
                title="Ranked as booster in the group",
                description="> You have been successfully ranked!",
                colour=ACCEPTED_COLOUR,
                timestamp=discord.utils.utcnow(),
            )
            if roblox_name:
                embed.add_field(name="Roblox username", value=f"`{roblox_name}`", inline=False)
            if guild is not None:
                embed.set_footer(
                    text=guild.name, icon_url=guild.icon.url if guild.icon else None
                )
            await target.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.info("Could not DM booster rank notice to %s; using thread fallback", user_id)

        if guild is None or not BOOSTER_BUTTON_THREAD:
            return False
        try:
            channel = guild.get_thread(BOOSTER_BUTTON_THREAD) or await client.fetch_channel(
                BOOSTER_BUTTON_THREAD
            )
            ping = await channel.send(
                f"<@{user_id}>, you have been ranked! ✅",
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user_id)]),
            )
            await ping.delete()
            return True
        except (discord.HTTPException, AttributeError):
            logger.warning("Could not send booster rank fallback ping to %s", user_id, exc_info=True)
            return False

    class MarkRankedButton(
        discord.ui.DynamicItem[discord.ui.Button],
        template=rf"{RANK_BUTTON_ID}:(?P<user_id>[0-9]+):(?P<ref>[A-Za-z0-9_]+)",
    ):
        def __init__(self, user_id: int, ref: str, *, disabled: bool = False) -> None:
            super().__init__(
                discord.ui.Button(
                    label="Ranked" if disabled else "Mark as ranked",
                    style=discord.ButtonStyle.success,
                    custom_id=f"{RANK_BUTTON_ID}:{user_id}:{ref}",
                    disabled=disabled,
                )
            )
            self.user_id = user_id
            self.ref = ref

        @classmethod
        async def from_custom_id(
            cls,
            interaction: discord.Interaction,
            item: discord.ui.Button,
            match,
            /,
        ) -> MarkRankedButton:
            return cls(int(match["user_id"]), match["ref"])

        async def callback(self, interaction: discord.Interaction) -> None:
            message = interaction.message
            if message is None:
                await reply(interaction, "I could not find this request message.")
                return
            if not is_staff(interaction.user):
                await reply(interaction, "You need the configured staff role to review requests.")
                return
            if message.id in _busy:
                await reply(interaction, "This request is already being reviewed.")
                return

            _busy.add(message.id)
            try:
                await self._review(interaction, message)
            finally:
                _busy.discard(message.id)

        async def _review(
            self, interaction: discord.Interaction, message: discord.Message
        ) -> None:
            await interaction.response.defer()

            if not message.embeds:
                await interaction.followup.send("This request has no embed to update.", ephemeral=True)
                return
            embed = message.embeds[0].copy()
            roblox_name = embed_roblox_name(embed) or (None if self.ref.isdigit() else self.ref)
            shown_name = f"`{roblox_name}`" if roblox_name else "That user"

            try:
                async with _new_session() as session:
                    if self.ref.isdigit():
                        roblox_id: int | None = int(self.ref)
                    else:
                        user = await lookup_user(session, self.ref)
                        roblox_id = int(user["id"]) if user else None
                    if roblox_id is None:
                        await interaction.followup.send(
                            f"I couldn't find the Roblox user {shown_name}.", ephemeral=True
                        )
                        return
                    status, _ = await group_status(session, roblox_id)
            except RobloxError as exc:
                await interaction.followup.send(f"Roblox lookup failed: {exc}", ephemeral=True)
                return
            except (aiohttp.ClientError, asyncio.TimeoutError):
                logger.exception("Roblox request failed during rank check")
                await interaction.followup.send(
                    "Couldn't reach Roblox. Please try again later.", ephemeral=True
                )
                return

            if status != "ranked":
                reason = "is not in the group" if status == "none" else "is not ranked in the group"
                await interaction.followup.send(f"{shown_name} {reason}.", ephemeral=True)
                return

            timestamp = int(discord.utils.utcnow().timestamp())
            set_status(
                embed,
                f"{STATUS_ACCEPTED} by {interaction.user.mention} · <t:{timestamp}:R>",
            )
            embed.colour = ACCEPTED_COLOUR

            try:
                await message.edit(
                    embed=embed,
                    view=rank_view(self.user_id, self.ref, disabled=True),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.exception("Could not update booster request message %s", message.id)
                await interaction.followup.send(
                    "The user is ranked, but I couldn't update the message.", ephemeral=True
                )
                return

            notified = await notify_ranked(interaction, self.user_id, roblox_name)
            note = "" if notified else " I couldn't notify the user."
            await interaction.followup.send(f"Marked as ranked.{note}", ephemeral=True)

    class RankRequestModal(discord.ui.Modal, title="Request your booster rank"):
        username = discord.ui.Label(
            text="Roblox username",
            description="Letters, numbers and underscores only. No spaces.",
            component=discord.ui.TextInput(
                style=discord.TextStyle.short,
                min_length=MIN_USERNAME_LENGTH,
                max_length=MAX_USERNAME_LENGTH,
                required=True,
                placeholder="Your Roblox username",
            ),
        )

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if lacks_booster_role(interaction.user):
                await reply(
                    interaction, "You need the server booster role to request a booster rank."
                )
                return

            name = (self.username.component.value or "").strip()
            if (
                not MIN_USERNAME_LENGTH <= len(name) <= MAX_USERNAME_LENGTH
                or not _SUBMIT_RE.fullmatch(name)
            ):
                await reply(
                    interaction,
                    f"That isn't a valid username. Use {MIN_USERNAME_LENGTH}-{MAX_USERNAME_LENGTH} "
                    "characters: letters, numbers and `_` only, with no spaces.",
                )
                return

            now = time.monotonic()
            wait = SUBMIT_COOLDOWN - (now - _submit_times.get(interaction.user.id, float("-inf")))
            if wait > 0:
                await reply(
                    interaction,
                    f"Please wait {int(wait) + 1} seconds before submitting another request.",
                )
                return
            if len(_submit_times) > 500:
                for user_id in [
                    uid for uid, at in _submit_times.items() if now - at >= SUBMIT_COOLDOWN
                ]:
                    del _submit_times[user_id]
            _submit_times[interaction.user.id] = now

            channel = text_channel(interaction.guild, BOOSTER_RANK)
            if channel is None:
                await reply(interaction, "Rank requests are currently unavailable.")
                return

            await interaction.response.defer(ephemeral=True)

            roblox_name = name
            roblox_id: int | None = None
            avatar_url: str | None = None
            try:
                async with _new_session() as session:
                    user = await lookup_user(session, name)
                    if user is None:
                        await reply(
                            interaction,
                            f"I couldn't find a Roblox user named `{name}`. Check the spelling and try again.",
                        )
                        return
                    roblox_name = str(user.get("name") or name)
                    roblox_id = int(user["id"])
                    status, _found_roles = await group_status(session, roblox_id)
                    if status == "none":
                        await reply(
                            interaction,
                            f"`{roblox_name}` isn't in the group. Join the group first, "
                            "then submit your request again to be ranked.",
                        )
                        return
                    if status == "ranked":
                        await reply(
                            interaction,
                            f"`{roblox_name}` is already ranked as booster in the group, "
                            "so this request was denied.",
                        )
                        return
                    avatar_url = await fetch_headshot(session, roblox_id)
            except (RobloxError, aiohttp.ClientError, asyncio.TimeoutError):
                logger.warning("Roblox lookup failed for booster request %s", name, exc_info=True)

            user_obj = interaction.user
            timestamp = int(discord.utils.utcnow().timestamp())
            profile_part = f" · [Profile]({profile_url(roblox_id)})" if roblox_id else ""
            embed = discord.Embed(
                description=(
                    f"## Roblox username: `{roblox_name}`{profile_part}\n"
                    f"**Submitted by:** {user_obj.mention} · <t:{timestamp}:R>\n"
                    "**Status:** Not ranked"
                ),
                colour=configured_colour(manager),
            )
            embed.set_author(name=user_obj.display_name, icon_url=user_obj.display_avatar.url)
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)

            try:
                await channel.send(
                    embed=embed,
                    view=rank_view(user_obj.id, str(roblox_id) if roblox_id else roblox_name),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.exception("Could not forward booster rank request from %s", user_obj.id)
                await reply(
                    interaction, "Your request could not be sent. Please try again later."
                )
                return

            await reply(interaction, "Your rank request has been sent to staff.")

    def prompt_embed() -> discord.Embed:
        return discord.Embed(
            title=PROMPT_TITLE,
            description=(
                "Boosting the server? Click the button below and enter your Roblox "
                "username to request your booster rank in the group. Staff will review it."
            ),
            colour=configured_colour(manager),
        )

    class PromptView(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=None)

        @discord.ui.button(
            label="Request rank",
            style=discord.ButtonStyle.primary,
            custom_id=REQUEST_BUTTON_ID,
        )
        async def request_rank(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            if text_channel(interaction.guild, BOOSTER_RANK) is None:
                await reply(interaction, "Rank requests are currently unavailable.")
                return
            if lacks_booster_role(interaction.user):
                await reply(
                    interaction, "You need the server booster role to request a booster rank."
                )
                return
            await interaction.response.send_modal(RankRequestModal())

    async def find_prompt_message(thread: discord.Thread) -> discord.Message | None:
        prompt_id = _prompt_messages.get(thread.id)
        if prompt_id is not None:
            try:
                return await thread.fetch_message(prompt_id)
            except discord.NotFound:
                _prompt_messages.pop(thread.id, None)
            except discord.HTTPException:
                logger.exception("Could not fetch tracked booster prompt in thread %s", thread.id)
                return None
        try:
            async for old in thread.history(limit=50):
                if client.user is None or old.author.id != client.user.id:
                    continue
                if any(embed.title == PROMPT_TITLE for embed in old.embeds):
                    _prompt_messages[thread.id] = old.id
                    return old
        except discord.HTTPException:
            logger.exception("Could not search for the booster prompt in thread %s", thread.id)
        return None

    @app_commands.command(
        name="booster-user-prompt",
        description="Send the booster rank request button to the configured thread.",
    )
    @app_commands.guild_only()
    async def booster_user_prompt(interaction: discord.Interaction) -> None:
        if not is_staff(interaction.user):
            await reply(
                interaction,
                "You need the configured main staff role or bot owner access to use this command.",
            )
            return
        guild = interaction.guild
        if guild is None:
            await reply(interaction, "Run this command inside the server.")
            return
        if not (BOOSTER_BUTTON_THREAD and BOOSTER_RANK):
            await reply(
                interaction,
                "Set the username thread and booster rank channel in `DEFAULT_SETTINGS` "
                "(`settings.py`) first.",
            )
            return
        if text_channel(guild, BOOSTER_RANK) is None:
            await reply(interaction, "The booster rank channel wasn't found as a text channel.")
            return

        await interaction.response.defer(ephemeral=True)
        try:
            thread = guild.get_thread(BOOSTER_BUTTON_THREAD) or await client.fetch_channel(
                BOOSTER_BUTTON_THREAD
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await reply(interaction, "I couldn't access the configured thread.")
            return
        if not isinstance(thread, discord.Thread):
            await reply(interaction, "The username thread ID must be a thread.")
            return

        try:
            old_prompt = await find_prompt_message(thread)
            if old_prompt is not None:
                try:
                    await old_prompt.delete()
                except discord.NotFound:
                    pass
            message = await thread.send(embed=prompt_embed(), view=PromptView())
            _prompt_messages[thread.id] = message.id
        except discord.HTTPException:
            logger.exception("Could not send booster prompt")
            await reply(
                interaction,
                f"I could not send the prompt to {thread.mention}. Check my permissions there.",
            )
            return

        await reply(interaction, f"Booster rank prompt sent to {thread.mention}.")

    client_key = id(client)
    if client_key not in _registered_clients:
        client.add_view(PromptView())
        client.add_dynamic_items(MarkRankedButton)
        _registered_clients.add(client_key)

    return booster_user_prompt
