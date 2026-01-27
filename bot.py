import os
import re
import sys
import asyncio
import time
import random
import json
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass
from typing import Optional, AsyncGenerator, Deque, Dict, Any, Tuple
from collections import deque

import discord
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
from aiohttp import web

# ============================================================
# FULL FEATURE BOT + AUTO CONFIG MODE (MULTI-GUILD SAFE)
#
# Changes requested:
# - /play: when queueing a single song, reply with the actual track name + its position in queue
# - /playnext: new command that inserts the next song at the top of the queue
# ============================================================
CONTROL_CONFIG_PATH = os.getenv("CONTROL_CONFIG_PATH", "control_config.json")

def load_control_config():
    try:
        with open(CONTROL_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {
                "host": data.get("host", "127.0.0.1"),
                "port": int(data.get("port", 8765)),
                "key": data.get("api_key", ""),
            }
    except FileNotFoundError:
        logger.warning("Control config not found (%s), using defaults", CONTROL_CONFIG_PATH)
    except Exception as e:
        logger.error("Failed to load control config: %s", e)

    return {
        "host": "127.0.0.1",
        "port": 8765,
        "key": "",
    }

# -------------------- ENV --------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in .env")

# Optional: set to speed up command sync during testing (guild-only sync)
GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")

_control = load_control_config()

CONTROL_HOST = _control["host"]
CONTROL_PORT = _control["port"]
CONTROL_KEY = _control["key"]

LOG_PATH = os.getenv("LOG_PATH", "bot.log")
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "instance-1")
STARTED_AT = time.time()

CONFIG_PATH = os.getenv("CONFIG_PATH", "guild_config.json")
MUSIC_ROLE_NAME = os.getenv("MUSIC_ROLE_NAME", "MusicBot")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

DEFAULT_VOLUME = float(os.getenv("DEFAULT_VOLUME", "1.0") or "1.0")  # 0.0-2.0
IDLE_DISCONNECT_SECONDS = int(os.getenv("IDLE_DISCONNECT_SECONDS", "300") or "300")
EMPTY_CHANNEL_DISCONNECT_SECONDS = int(os.getenv("EMPTY_CHANNEL_DISCONNECT_SECONDS", "30") or "30")
VOLUME_STEP = float(os.getenv("VOLUME_STEP", "0.1") or "0.1")

# -------------------- LOGGING --------------------
logger = logging.getLogger("musicbot")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

fh = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
fh.setFormatter(_fmt)
logger.addHandler(fh)

ch = logging.StreamHandler()
ch.setFormatter(_fmt)
logger.addHandler(ch)

# -------------------- CONFIG (per-guild) --------------------
def _load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("Failed to read %s: %s", CONFIG_PATH, e)
        return {}

def _save_config(cfg: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.warning("Failed to write %s: %s", CONFIG_PATH, e)

CONFIG: Dict[str, Any] = _load_config()

def get_guild_channel_id(guild_id: int) -> int:
    g = CONFIG.get(str(guild_id), {})
    return int(g.get("channel_id", 0) or 0)

def set_guild_channel_id(guild_id: int, channel_id: int) -> None:
    CONFIG.setdefault(str(guild_id), {})
    CONFIG[str(guild_id)]["channel_id"] = int(channel_id)
    _save_config(CONFIG)

# -------------------- YTDLP / FFMPEG --------------------
YTDLP_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "noplaylist": True,
}
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}
ytdlp = yt_dlp.YoutubeDL(YTDLP_OPTS)

# -------------------- SPOTIFY (OPTIONAL) --------------------
SPOTIFY_ENABLED = False
sp = None
try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        sp = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
            )
        )
        SPOTIFY_ENABLED = True
except Exception:
    SPOTIFY_ENABLED = False

SPOTIFY_TRACK_RE = re.compile(r"open\.spotify\.com/track/([A-Za-z0-9]+)")
SPOTIFY_PLAYLIST_RE = re.compile(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)")
SPOTIFY_ALBUM_RE = re.compile(r"open\.spotify\.com/album/([A-Za-z0-9]+)")

# -------------------- HELPERS --------------------
@dataclass
class Track:
    query: str
    requested_by: str
    title: Optional[str] = None
    artist: Optional[str] = None
    webpage_url: Optional[str] = None
    duration: Optional[int] = None  # seconds
    thumbnail: Optional[str] = None  # image URL (when available)

def fmt_time(seconds: Optional[int]) -> str:
    if seconds is None:
        return "?:??"
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def progress_bar(elapsed: int, total: int, width: int = 24) -> str:
    if not total or total <= 0:
        return "▱" * width
    ratio = min(1.0, max(0.0, elapsed / total))
    filled = int(ratio * width)
    return "▰" * filled + "▱" * (width - filled)

async def ytdlp_resolve(query_or_url: str) -> dict:
    loop = asyncio.get_running_loop()

    def extract():
        info = ytdlp.extract_info(query_or_url, download=False)
        if "entries" in info:
            info = info["entries"][0]
        return info

    return await loop.run_in_executor(None, extract)

def _pick_artist_from_info(info: dict) -> Optional[str]:
    for key in ("artist", "creator", "uploader", "channel"):
        val = info.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None

def make_audio_source(stream_url: str, volume: float) -> discord.PCMVolumeTransformer:
    src = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTS)
    return discord.PCMVolumeTransformer(src, volume=max(0.0, min(volume, 2.0)))

async def resolve_title_for_queue_display(query_or_url: str) -> Tuple[str, Optional[str], Optional[str], Optional[int]]:
    """
    Resolve now so /play can show the REAL track title instantly.
    Returns (title, artist, webpage_url, duration).
    """
    info = await ytdlp_resolve(query_or_url)
    title = info.get("title") or "Unknown title"
    artist = _pick_artist_from_info(info)
    url = info.get("webpage_url") or query_or_url
    duration = info.get("duration")
    return title, artist, url, duration

async def spotify_track_objects(url: str, requested_by: str) -> AsyncGenerator[Track, None]:
    """
    Yields Track objects with title/artist filled from Spotify metadata,
    while query is a YouTube-search string that yt-dlp can resolve/play.
    """
    if not SPOTIFY_ENABLED or sp is None:
        raise app_commands.AppCommandError(
            "Spotify support isn't enabled. Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to .env"
        )

    m = SPOTIFY_TRACK_RE.search(url)
    if m:
        tid = m.group(1)
        t = sp.track(tid)
        artists = ", ".join(a["name"] for a in t["artists"])
        name = t["name"]
        yield Track(query=f"{artists} - {name} audio", requested_by=requested_by, title=name, artist=artists)
        return

    m = SPOTIFY_ALBUM_RE.search(url)
    if m:
        aid = m.group(1)
        limit = 50
        offset = 0
        while True:
            page = sp.album_tracks(aid, limit=limit, offset=offset)
            items = page.get("items", [])
            if not items:
                break
            for item in items:
                name = item.get("name") or "Unknown"
                artists = ", ".join(a["name"] for a in item.get("artists", [])) or "Unknown artist"
                yield Track(query=f"{artists} - {name} audio", requested_by=requested_by, title=name, artist=artists)
            if page.get("next") is None:
                break
            offset += limit
        return

    m = SPOTIFY_PLAYLIST_RE.search(url)
    if m:
        pid = m.group(1)
        limit = 50
        offset = 0
        while True:
            page = sp.playlist_items(pid, limit=limit, offset=offset, additional_types=("track",))
            items = page.get("items", [])
            if not items:
                break
            for it in items:
                tr = it.get("track")
                if not tr:
                    continue
                artists = ", ".join(a["name"] for a in tr.get("artists", [])) or "Unknown artist"
                name = tr.get("name") or "Unknown"
                yield Track(query=f"{artists} - {name} audio", requested_by=requested_by, title=name, artist=artists)
            if page.get("next") is None:
                break
            offset += limit
        return

    raise app_commands.AppCommandError("That doesn't look like a Spotify track/album/playlist link.")

# -------------------- AUTO-CONFIG HELPERS --------------------
def _music_role_permissions() -> discord.Permissions:
    p = discord.Permissions.none()
    p.view_channel = True
    p.send_messages = True
    p.embed_links = True
    p.read_message_history = True
    p.add_reactions = True
    p.connect = True
    p.speak = True
    p.use_voice_activation = True
    p.send_messages_in_threads = True
    p.use_external_emojis = True
    return p

async def ensure_music_role_and_permissions(guild: discord.Guild, text_channel: discord.TextChannel, me: discord.Member) -> str:
    notes = []
    can_manage_roles = me.guild_permissions.manage_roles
    can_manage_channels = me.guild_permissions.manage_channels

    role = discord.utils.get(guild.roles, name=MUSIC_ROLE_NAME)

    if not can_manage_roles:
        notes.append("❌ Missing **Manage Roles** — can't create/assign role automatically.")
    else:
        if role is None:
            try:
                role = await guild.create_role(
                    name=MUSIC_ROLE_NAME,
                    permissions=_music_role_permissions(),
                    reason="Auto-config for music bot",
                )
                notes.append(f"✅ Created role **{MUSIC_ROLE_NAME}**.")
            except Exception as e:
                notes.append(f"❌ Failed to create role: `{e}`")
                role = None
        else:
            try:
                desired = _music_role_permissions()
                merged = discord.Permissions(role.permissions.value | desired.value)
                if merged.value != role.permissions.value:
                    await role.edit(permissions=merged, reason="Auto-config (merge perms)")
                notes.append(f"✅ Found role **{MUSIC_ROLE_NAME}**.")
            except Exception as e:
                notes.append(f"⚠️ Couldn't update role perms: `{e}`")

        if role is not None:
            try:
                if role not in me.roles:
                    await me.add_roles(role, reason="Auto-config for music bot")
                    notes.append("✅ Assigned role to the bot.")
            except Exception as e:
                notes.append(f"❌ Failed to assign role: `{e}`")

    if not can_manage_channels:
        notes.append("❌ Missing **Manage Channels** — can't set channel overwrites automatically.")
    else:
        try:
            if role is not None:
                ow = text_channel.overwrites_for(role)
                ow.view_channel = True
                ow.send_messages = True
                ow.embed_links = True
                ow.read_message_history = True
                ow.add_reactions = True
                ow.send_messages_in_threads = True
                await text_channel.set_permissions(role, overwrite=ow, reason="Auto-config for music bot")
                notes.append(f"✅ Set permissions for **{MUSIC_ROLE_NAME}** in {text_channel.mention}.")
            else:
                ow = text_channel.overwrites_for(me)
                ow.view_channel = True
                ow.send_messages = True
                ow.embed_links = True
                ow.read_message_history = True
                ow.add_reactions = True
                ow.send_messages_in_threads = True
                await text_channel.set_permissions(me, overwrite=ow, reason="Auto-config (member overwrite)")
                notes.append(f"✅ Set permissions for the bot in {text_channel.mention}.")
        except Exception as e:
            notes.append(f"❌ Failed to set channel overwrites: `{e}`")

    return "\n".join(notes) if notes else "✅ Auto-config complete."

# -------------------- MUSIC PLAYER --------------------
class MusicPlayer:
    def __init__(self, client: discord.Client, guild_id: int):
        self.client = client
        self.guild_id = int(guild_id)

        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.current: Optional[Track] = None
        self.voice: Optional[discord.VoiceClient] = None
        self.text_channel: Optional[discord.abc.Messageable] = None

        self._player_task: Optional[asyncio.Task] = None
        self._track_done: Optional[asyncio.Event] = None
        self._bg_tasks: set[asyncio.Task] = set()

        self._nowplaying_message: Optional[discord.Message] = None
        self._np_update_task: Optional[asyncio.Task] = None

        # Persistent “Music Panel” message we keep editing (avoids spamming Now Playing messages)
        self._panel_message: Optional[discord.Message] = None

        self._started_monotonic: Optional[float] = None
        self._paused_at: Optional[float] = None
        self._paused_total: float = 0.0

        self._last_np_edit: float = 0.0
        self._np_interval: float = 1.0

        self.volume: float = max(0.0, min(DEFAULT_VOLUME, 2.0))
        self.history: Deque[Track] = deque(maxlen=25)

        # Auto-disconnect tracking
        self._last_activity: float = time.monotonic()
        self._disconnect_watch_task: Optional[asyncio.Task] = None
        self._empty_since: Optional[float] = None

    def set_channel(self, channel: discord.abc.Messageable):
        self.text_channel = channel

    async def ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient:
        if not isinstance(interaction.user, discord.Member):
            raise app_commands.AppCommandError("This command must be used in a server.")
        if not interaction.user.voice or not interaction.user.voice.channel:
            raise app_commands.AppCommandError("You must be in a voice channel first.")
        channel = interaction.user.voice.channel

        if self.voice and self.voice.is_connected():
            if self.voice.channel != channel:
                await self.voice.move_to(channel)
        else:
            self.voice = await channel.connect()

        self.mark_activity()
        self._start_disconnect_watcher()
        return self.voice

    def start_if_needed(self):
        if self._player_task is None or self._player_task.done():
            self._player_task = asyncio.create_task(self._player_loop())

    async def add_track(self, track: Track):
        self.mark_activity()
        await self.queue.put(track)
        self.start_if_needed()

    def add_track_front(self, track: Track):
        self.queue._queue.appendleft(track)

    async def add_track_next(self, track: Track):
        self.mark_activity()
        # Put at top of pending queue so it plays next.
        self.add_track_front(track)
        self.start_if_needed()

    def spawn_bg(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(lambda t: self._bg_tasks.discard(t))

    # ---------------- Queue ops ----------------
    def queue_snapshot(self):
        return list(self.queue._queue)


    def mark_activity(self):
        """Mark that a user interacted with the bot (prevents idle disconnect)."""
        self._last_activity = time.monotonic()

    def _start_disconnect_watcher(self):
        """Start (or restart) the background watcher that auto-disconnects when unused/empty."""
        if self._disconnect_watch_task and not self._disconnect_watch_task.done():
            return

        async def loop():
            try:
                while True:
                    await asyncio.sleep(5)
                    if not self.voice or not self.voice.is_connected():
                        return

                    ch = getattr(self.voice, "channel", None)
                    if ch is None:
                        continue

                    # Condition A: No non-bot users in the voice channel
                    nonbot_members = [m for m in getattr(ch, "members", []) if not getattr(m, "bot", False)]
                    now = time.monotonic()
                    if len(nonbot_members) == 0:
                        if self._empty_since is None:
                            self._empty_since = now
                        elif (now - self._empty_since) >= EMPTY_CHANNEL_DISCONNECT_SECONDS:
                            if self.text_channel:
                                try:
                                    await self.text_channel.send("👋 Disconnecting: nobody is in the voice channel.")
                                except Exception:
                                    pass
                            await self.stop()
                            return
                    else:
                        self._empty_since = None

                    # Condition B: Bot is idle (not playing/paused, no queue) for a while
                    is_busy = False
                    try:
                        is_busy = bool(self.voice.is_playing() or self.voice.is_paused())
                    except Exception:
                        is_busy = False

                    if (not is_busy) and self.queue.empty() and (now - self._last_activity) >= IDLE_DISCONNECT_SECONDS:
                        if self.text_channel:
                            try:
                                await self.text_channel.send("👋 Disconnecting: idle (no playback/queue/activity).")
                            except Exception:
                                pass
                        await self.stop()
                        return
            except asyncio.CancelledError:
                return

        self._disconnect_watch_task = asyncio.create_task(loop())


    def pending_queue_len(self) -> int:
        return len(self.queue._queue)

    def clear_queue(self) -> int:
        cleared = 0
        try:
            while True:
                self.queue.get_nowait()
                cleared += 1
        except asyncio.QueueEmpty:
            pass
        return cleared

    def shuffle_queue(self) -> int:
        q = self.queue._queue
        items = list(q)
        random.shuffle(items)
        q.clear()
        q.extend(items)
        return len(items)

    def remove_from_queue(self, index_1_based: int) -> Track:
        if index_1_based <= 0:
            raise ValueError("Index must be 1 or greater.")
        q = self.queue._queue
        items = list(q)
        idx = index_1_based - 1
        if idx < 0 or idx >= len(items):
            raise IndexError("Index out of range.")
        removed = items.pop(idx)
        q.clear()
        q.extend(items)
        return removed

    def skip_to_queue_index(self, index_1_based: int) -> int:
        if index_1_based <= 0:
            raise ValueError("Index must be 1 or greater.")
        q = self.queue._queue
        items = list(q)
        idx = index_1_based - 1
        if idx < 0 or idx >= len(items):
            raise IndexError("Index out of range.")
        dropped = items[:idx]
        remaining = items[idx:]
        q.clear()
        q.extend(remaining)
        self.skip()
        return len(dropped)

    # ---------------- Playback controls ----------------
    def skip(self):
        self.mark_activity()
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()

    def previous(self) -> bool:
        if not self.history:
            return False
        prev = self.history.pop()
        if self.current:
            self.add_track_front(self.current)
        self.add_track_front(prev)
        self.skip()
        return True

    def pause(self):
        self.mark_activity()
        if self.voice and self.voice.is_playing():
            self.voice.pause()
            if self._paused_at is None:
                self._paused_at = time.monotonic()

    def resume(self):
        self.mark_activity()
        if self.voice and self.voice.is_paused():
            self.voice.resume()
            if self._paused_at is not None:
                self._paused_total += time.monotonic() - self._paused_at
                self._paused_at = None

    def set_volume(self, new_volume: float) -> float:
        self.volume = max(0.0, min(float(new_volume), 2.0))
        if self.voice and self.voice.source and isinstance(self.voice.source, discord.PCMVolumeTransformer):
            self.voice.source.volume = self.volume
        return self.volume

    def volume_up(self, step: float = VOLUME_STEP) -> float:
        return self.set_volume(self.volume + step)

    def volume_down(self, step: float = VOLUME_STEP) -> float:
        return self.set_volume(self.volume - step)

    async def stop(self):
        # Cancel disconnect watcher
        if self._disconnect_watch_task and not self._disconnect_watch_task.done():
            self._disconnect_watch_task.cancel()
        self._disconnect_watch_task = None
        self._empty_since = None
        
        self.clear_queue()
        self.current = None

        for t in list(self._bg_tasks):
            t.cancel()
        self._bg_tasks.clear()

        await self._stop_nowplaying_updater()

        if self.voice and self.voice.is_connected():
            await self.voice.disconnect()
        self.voice = None

        if self._player_task and not self._player_task.done():
            self._player_task.cancel()
        self._player_task = None

        # Update persistent panel (if any) to the idle state
        if self._panel_message is not None:
            await self.update_panel_message(force=True)

    # ---------------- Now playing embed ----------------
    def _elapsed_seconds(self) -> int:
        if self._started_monotonic is None:
            return 0
        now = time.monotonic()
        paused_extra = 0.0
        if self._paused_at is not None:
            paused_extra = now - self._paused_at
        elapsed = (now - self._started_monotonic) - (self._paused_total + paused_extra)
        return max(0, int(elapsed))

    def _remaining_seconds(self) -> Optional[int]:
        if not self.current or self.current.duration is None:
            return None
        return max(0, int(self.current.duration - self._elapsed_seconds()))

    def format_queue(self, max_items: int = 15) -> str:
        items = self.queue_snapshot()
        lines = []
        if self.current and self.current.title:
            lines.append(f"**Now:** {self.current.title}")
        elif self.current:
            lines.append("**Now:** (resolving…)")
        else:
            lines.append("**Now:** (nothing playing)")

        if not items:
            lines.append("**Queue:** (empty)")
        else:
            lines.append("**Queue:**")
            for i, t in enumerate(items[:max_items], start=1):
                label = t.title or t.query
                lines.append(f"{i}. {label}")
            if len(items) > max_items:
                lines.append(f"...and {len(items) - max_items} more")
        return "\n".join(lines)

    def nowplaying_embed(self) -> discord.Embed:
        # Modernized embed layout (clean fields + thumbnail)
        if not self.current:
            embed = discord.Embed(
                title="🎶 Music Panel",
                description="Nothing is playing right now.\n\nUse **/play** to queue something.",
                color=discord.Color.dark_grey(),
            )
            embed.set_footer(text=f"Volume: {int(self.volume*100)}%")
            return embed

        t = self.current
        title = t.title or "Resolving…"
        artist = t.artist or "Unknown artist"
        requester = t.requested_by
        url = t.webpage_url or ""

        dur = t.duration
        elapsed = self._elapsed_seconds()
        remaining = self._remaining_seconds()

        state = "⏸️ Paused" if (self.voice and self.voice.is_paused()) else "▶️ Playing"
        color = discord.Color.blurple() if state.startswith("▶️") else discord.Color.orange()

        embed = discord.Embed(
            title="🎶 Music Panel",
            description=f"**[{title}]({url})**" if url else f"**{title}**",
            color=color,
        )

        # Thumbnail (if available from yt-dlp)
        if getattr(t, "thumbnail", None):
            embed.set_thumbnail(url=t.thumbnail)

        embed.add_field(name="Artist", value=artist, inline=True)
        embed.add_field(name="Requested by", value=requester, inline=True)
        embed.add_field(name="Status", value=state, inline=True)

        if dur is not None:
            bar = progress_bar(elapsed, dur, width=24)
            prog_lines = [
                f"`{fmt_time(elapsed)} / {fmt_time(dur)}`",
                f"`{bar}`",
            ]
            if remaining is not None:
                prog_lines.append(f"⏳ Remaining: **{fmt_time(remaining)}**")
            embed.add_field(name="Progress", value="\n".join(prog_lines), inline=False)
        else:
            embed.add_field(name="Progress", value="⏳ Remaining: **unknown**", inline=False)

        qsize = self.queue.qsize()
        embed.add_field(name="Up next", value=f"{qsize} track(s) in queue" if qsize else "—", inline=True)
        embed.add_field(name="Volume", value=f"{int(self.volume*100)}%", inline=True)

        embed.set_footer(text="Use the buttons below to control playback and manage the queue.")
        return embed

    async def post_nowplaying_message(self):
        """Legacy: posts a one-off Now Playing message."""
        if not self.text_channel:
            return
        try:
            self._nowplaying_message = await self.text_channel.send(
                embed=self.nowplaying_embed(),
                view=NowPlayingView(self),
            )
        except Exception:
            self._nowplaying_message = None

    async def ensure_panel_message(self) -> Optional[discord.Message]:
        """Ensure there is a Music Panel message; reuse existing if possible."""
        if not self.text_channel:
            return None

        if self._panel_message is not None:
            try:
                await self._panel_message.edit(embed=self.nowplaying_embed(), view=NowPlayingView(self))
                return self._panel_message
            except Exception:
                self._panel_message = None

        # Create a new one (do NOT delete anything here)
        try:
            self._panel_message = await self.text_channel.send(
                embed=self.nowplaying_embed(),
                view=NowPlayingView(self),
            )
            return self._panel_message
        except Exception:
            self._panel_message = None
            return None

    async def post_new_panel_message(self, delete_previous: bool = True) -> Optional[discord.Message]:
        """Post a fresh Music Panel message (optionally deleting the previous one)."""
        if not self.text_channel:
            return None

        if delete_previous and self._panel_message is not None:
            try:
                await self._panel_message.delete()
            except Exception:
                pass

        try:
            self._panel_message = await self.text_channel.send(
                embed=self.nowplaying_embed(),
                view=NowPlayingView(self),
            )
            return self._panel_message
        except Exception:
            self._panel_message = None
            return None
        if self._panel_message is not None:
            try:
                await self._panel_message.edit(embed=self.nowplaying_embed(), view=NowPlayingView(self))
                return self._panel_message
            except Exception:
                self._panel_message = None

        try:
            self._panel_message = await self.text_channel.send(
                embed=self.nowplaying_embed(),
                view=NowPlayingView(self),
            )
            return self._panel_message
        except Exception:
            self._panel_message = None
            return None

    async def update_panel_message(self, force: bool = False):
        """Update the persistent panel message (preferred UI)."""
        if not self.text_channel:
            return
        if not self.current and not force:
            return

        if self._panel_message is None:
            await self.ensure_panel_message()
            return

        now = time.monotonic()
        if not force and (now - self._last_np_edit) < self._np_interval:
            return

        try:
            await self._panel_message.edit(embed=self.nowplaying_embed(), view=NowPlayingView(self))
            self._last_np_edit = now
            self._np_interval = max(1.0, self._np_interval * 0.9)
        except discord.HTTPException:
            self._np_interval = min(5.0, self._np_interval * 1.5)
        except Exception:
            pass

    async def update_ui_message(self, force: bool = False):
        """Update whichever UI message is active (panel preferred, else legacy nowplaying)."""
        if self._panel_message is not None:
            await self.update_panel_message(force=force)
        else:
            await self.update_nowplaying_message(force=force)

    async def update_nowplaying_message(self, force: bool = False):
        if not self._nowplaying_message or not self.text_channel:
            return
        if not self.current and not force:
            return

        now = time.monotonic()
        if not force and (now - self._last_np_edit) < self._np_interval:
            return

        try:
            await self._nowplaying_message.edit(embed=self.nowplaying_embed(), view=NowPlayingView(self))
            self._last_np_edit = now
            self._np_interval = max(1.0, self._np_interval * 0.9)
        except discord.HTTPException:
            self._np_interval = min(5.0, self._np_interval * 1.5)
        except Exception:
            pass

    async def _start_nowplaying_updater(self):
        await self._stop_nowplaying_updater()

        async def loop():
            try:
                while True:
                    await asyncio.sleep(0.5)
                    if not self.voice or not self.current:
                        return
                    if not (self.voice.is_playing() or self.voice.is_paused()):
                        return
                    await self.update_ui_message()
            except asyncio.CancelledError:
                return

        self._np_update_task = asyncio.create_task(loop())

    async def _stop_nowplaying_updater(self):
        if self._np_update_task and not self._np_update_task.done():
            self._np_update_task.cancel()
        self._np_update_task = None

    async def _player_loop(self):
        while True:
            self.current = await self.queue.get()
            if not self.voice or not self.voice.is_connected():
                self.current = None
                return

            self._track_done = asyncio.Event()

            self._started_monotonic = None
            self._paused_at = None
            self._paused_total = 0.0
            self._last_np_edit = 0.0
            self._np_interval = 1.0

            try:
                info = await ytdlp_resolve(self.current.query)
                self.current.title = self.current.title or (info.get("title") or "Unknown title")
                self.current.webpage_url = info.get("webpage_url") or self.current.webpage_url or self.current.query
                self.current.duration = info.get("duration") if self.current.duration is None else self.current.duration
                self.current.artist = self.current.artist or _pick_artist_from_info(info)
                self.current.thumbnail = getattr(self.current, "thumbnail", None) or info.get("thumbnail")

                stream_url = info["url"]
                source = make_audio_source(stream_url, self.volume)
                self._started_monotonic = time.monotonic()

                def _after_play(err: Optional[Exception]):
                    if err:
                        logger.error("Playback error: %s", err)
                    if self._track_done:
                        self.client.loop.call_soon_threadsafe(self._track_done.set)

                self.voice.play(source, after=_after_play)
                self.mark_activity()
                self._start_disconnect_watcher()

                await self.post_new_panel_message(delete_previous=True)
                await self._start_nowplaying_updater()

                await self._track_done.wait()
                await self._stop_nowplaying_updater()

                if self.current:
                    self.history.append(self.current)

            except Exception as e:
                logger.exception("Failed to play track: %s", e)
                await self._stop_nowplaying_updater()

    def status_dict(self) -> dict:
        cur = self.current
        voice = None
        if self.voice and self.voice.is_connected():
            voice = {
                "guild": getattr(self.voice.guild, "name", None),
                "channel": getattr(self.voice.channel, "name", None),
                "playing": self.voice.is_playing(),
                "paused": self.voice.is_paused(),
                "volume": self.volume,
            }

        return {
            "instance": INSTANCE_NAME,
            "guild_id": self.guild_id,
            "configured_channel_id": get_guild_channel_id(self.guild_id),
            "uptime_sec": int(time.time() - STARTED_AT),
            "bot_user": str(self.client.user) if self.client.user else None,
            "bot_id": self.client.user.id if self.client.user else None,
            "voice": voice,
            "current": None
            if not cur
            else {
                "title": cur.title,
                "artist": cur.artist,
                "url": cur.webpage_url,
                "requested_by": cur.requested_by,
                "duration": cur.duration,
                "elapsed": self._elapsed_seconds(),
                "remaining": self._remaining_seconds(),
            },
            "queue_len": self.queue.qsize(),
            "queue_preview": [(t.title or t.query) for t in self.queue_snapshot()[:10]],
            "history_len": len(self.history),
        }

# ---------- UI: Queue Management ----------
class QueueIndexModal(discord.ui.Modal):
    def __init__(self, title: str, player: MusicPlayer, action: str):
        super().__init__(title=title)
        self.player = player
        self.action = action  # "remove" or "skipto"

        self.index = discord.ui.TextInput(
            label="Track number (from the queue list)",
            placeholder="Example: 1",
            required=True,
            max_length=6,
        )
        self.add_item(self.index)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(str(self.index.value).strip())
            if self.action == "remove":
                removed = self.player.remove_from_queue(n)
                label = removed.title or removed.query
                await interaction.response.send_message(f"🗑 Removed **{label}** from the queue.", ephemeral=True)
            elif self.action == "skipto":
                dropped = self.player.skip_to_queue_index(n)
                await interaction.response.send_message(
                    f"⏭ Skipping to track #{n}. Dropped **{dropped}** queued track(s).",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message("Unknown action.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)
        except IndexError:
            await interaction.response.send_message("That track number is out of range.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Error: `{e}`", ephemeral=True)

class QueueManagementSelect(discord.ui.Select):
    def __init__(self, player: MusicPlayer):
        self.player = player
        options = [
            discord.SelectOption(label="View Queue", value="view", emoji="📜"),
            discord.SelectOption(label="Shuffle Queue", value="shuffle", emoji="🔀"),
            discord.SelectOption(label="Clear Queue", value="clear", emoji="🧹"),
            discord.SelectOption(label="Remove Track (by number)", value="remove", emoji="🗑"),
            discord.SelectOption(label="Skip To Track (by number)", value="skipto", emoji="⏭"),
        ]
        super().__init__(placeholder="Choose a queue action…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]

        if choice == "view":
            await interaction.response.send_message(self.player.format_queue(max_items=20), ephemeral=True)
            return
        if choice == "shuffle":
            n = self.player.shuffle_queue()
            await interaction.response.send_message(f"🔀 Shuffled **{n}** queued track(s).", ephemeral=True)
            return
        if choice == "clear":
            n = self.player.clear_queue()
            await interaction.response.send_message(f"🧹 Cleared **{n}** queued track(s).", ephemeral=True)
            return
        if choice == "remove":
            await interaction.response.send_modal(QueueIndexModal("Remove a Track", self.player, "remove"))
            return
        if choice == "skipto":
            await interaction.response.send_modal(QueueIndexModal("Skip To a Track", self.player, "skipto"))
            return

class QueueManagementView(discord.ui.View):
    def __init__(self, player: MusicPlayer):
        super().__init__(timeout=60)
        self.add_item(QueueManagementSelect(player))

class NowPlayingView(discord.ui.View):
    """Modernized control panel for the persistent Music Panel message."""

    def __init__(self, player: MusicPlayer):
        super().__init__(timeout=None)
        self.player = player

        # Dynamically disable/enable buttons based on current state
        is_playing = bool(player.voice and player.voice.is_playing())
        is_paused = bool(player.voice and player.voice.is_paused())
        has_prev = len(player.history) > 0

        # These attributes are created by the decorators below
        try:
            self.prev_btn.disabled = not has_prev
            self.playpause_btn.disabled = player.current is None
            self.skip_btn.disabled = player.current is None
            self.stop_btn.disabled = player.current is None and player.queue.qsize() == 0
            self.voldown_btn.disabled = False
            self.volup_btn.disabled = False
        except Exception:
            pass

        # Update play/pause label + style for a more modern feel
        try:
            if is_paused:
                self.playpause_btn.emoji = "▶️"
                self.playpause_btn.label = "Play"
                self.playpause_btn.style = discord.ButtonStyle.success
            else:
                self.playpause_btn.emoji = "⏸️"
                self.playpause_btn.label = "Pause"
                self.playpause_btn.style = discord.ButtonStyle.primary
        except Exception:
            pass

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary, emoji="⏮️", row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok = self.player.previous()
        await self.player.update_ui_message(force=True)
        await interaction.response.send_message("⏮️ Previous." if ok else "No previous track yet.", ephemeral=True)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary, emoji="⏸️", row=0)
    async def playpause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Toggle play/pause
        if self.player.voice and self.player.voice.is_paused():
            self.player.resume()
            msg = "▶️ Resumed."
        else:
            self.player.pause()
            msg = "⏸️ Paused."
        await self.player.update_ui_message(force=True)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️", row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.skip()
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️", row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.stop()
        await interaction.response.send_message("⏹️ Stopped and disconnected.", ephemeral=True)

    @discord.ui.button(label="Vol -", style=discord.ButtonStyle.secondary, emoji="🔉", row=1)
    async def voldown_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = self.player.volume_down(VOLUME_STEP)
        await self.player.update_ui_message(force=True)
        await interaction.response.send_message(f"🔉 Volume: {int(v*100)}%", ephemeral=True)

    @discord.ui.button(label="Vol +", style=discord.ButtonStyle.secondary, emoji="🔊", row=1)
    async def volup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = self.player.volume_up(VOLUME_STEP)
        await self.player.update_ui_message(force=True)
        await interaction.response.send_message(f"🔊 Volume: {int(v*100)}%", ephemeral=True)

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.secondary, emoji="🔀", row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        n = self.player.shuffle_queue()
        await self.player.update_ui_message(force=True)
        await interaction.response.send_message(f"🔀 Shuffled **{n}** queued track(s).", ephemeral=True)

    @discord.ui.button(label="Queue", style=discord.ButtonStyle.secondary, emoji="🧰", row=1)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Queue Management:", view=QueueManagementView(self.player), ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.update_ui_message(force=True)
        await interaction.response.send_message("✅ Refreshed.", ephemeral=True)


# -------------------- SETUP UI (Channel Picker) --------------------
class SetupChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select the text channel for the music bot…",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )

    async def _resolve_text_channel(self, interaction: discord.Interaction) -> Optional[discord.TextChannel]:
        """Resolve the selected channel reliably (works even if the channel isn't cached)."""
        if interaction.guild is None:
            return None

        # discord.py usually gives actual channel objects here.
        try:
            if getattr(self, "values", None):
                v = self.values[0]
                if isinstance(v, discord.TextChannel):
                    return v
                if isinstance(v, discord.abc.GuildChannel) and getattr(v, "id", None):
                    ch = interaction.guild.get_channel(v.id)
                    if isinstance(ch, discord.TextChannel):
                        return ch
        except Exception:
            pass

        # Fallback: read raw IDs from interaction payload
        try:
            data = interaction.data or {}
            raw_values = data.get("values") or []
            if raw_values:
                cid = int(raw_values[0])
                ch = interaction.guild.get_channel(cid)
                if isinstance(ch, discord.TextChannel):
                    return ch
                # Not in cache? Fetch it.
                fetched = await interaction.guild.fetch_channel(cid)
                if isinstance(fetched, discord.TextChannel):
                    return fetched
        except Exception:
            return None

        return None

    async def callback(self, interaction: discord.Interaction):
        selected = await self._resolve_text_channel(interaction)

        if not isinstance(selected, discord.TextChannel):
            await interaction.response.send_message("Please select a **text** channel.", ephemeral=True)
            return

        if isinstance(self.view, SetupView):
            self.view.selected_channel = selected

        await interaction.response.send_message(
            f"✅ Selected {selected.mention}. Now click **Confirm** to finish setup.",
            ephemeral=True,
        )

class SetupView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=300)
        self.guild = guild
        self.selected_channel: Optional[discord.TextChannel] = None
        self.add_item(SetupChannelSelect())

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only server managers can run setup.", ephemeral=True)
            return

        if self.selected_channel is None:
            await interaction.response.send_message("Please select a channel first.", ephemeral=True)
            return

        set_guild_channel_id(self.guild.id, self.selected_channel.id)

        me = interaction.guild.me or interaction.guild.get_member(interaction.client.user.id)
        if me is None:
            await interaction.response.send_message("Bot member not ready yet. Try again in a moment.", ephemeral=True)
            return

        status = await ensure_music_role_and_permissions(interaction.guild, self.selected_channel, me)

        msg = (
            "✅ **Thanks for confirming — your bot is ready to use!**\n"
            f"Commands are now restricted to {self.selected_channel.mention}.\n\n"
            f"{status}"
        )
        await interaction.response.send_message(msg, ephemeral=True)
        self.stop()

# -------------------- CONTROL API --------------------
def _authorized(request: web.Request) -> bool:
    if not CONTROL_KEY:
        return True
    return request.headers.get("X-API-Key", "") == CONTROL_KEY

PLAYERS: dict[int, "MusicPlayer"] = {}

def get_player(guild_id: int) -> "MusicPlayer":
    raise RuntimeError("get_player not initialized")

async def handle_status(request: web.Request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    guilds = []
    try:
        guilds = [p.status_dict() for _, p in sorted(PLAYERS.items(), key=lambda kv: kv[0])]
    except Exception:
        guilds = []

    return web.json_response({
        "instance": INSTANCE_NAME,
        "uptime_sec": int(time.time() - STARTED_AT),
        "bot_user": None,
        "bot_id": None,
        "guilds": guilds,
    })

async def handle_logs(request: web.Request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    tail = int(request.query.get("tail", "200"))
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-tail:]
        return web.Response(text="".join(lines), content_type="text/plain")
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def start_control_server():
    app = web.Application()
    app.router.add_get("/status", handle_status)
    app.router.add_get("/logs", handle_logs)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, CONTROL_HOST, CONTROL_PORT)
    await site.start()
    logger.info("[%s] Control API listening on http://%s:%s", INSTANCE_NAME, CONTROL_HOST, CONTROL_PORT)

# -------------------- DISCORD CLIENT (SLASH) --------------------
intents = discord.Intents.default()
intents.guilds = True

class SlashMusicClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.loop.create_task(start_control_server())

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("[%s] Synced %s command(s) to guild %s (fast)", INSTANCE_NAME, len(synced), GUILD_ID)
        else:
            synced = await self.tree.sync()
            logger.info("[%s] Synced %s global command(s) (can take time to appear)", INSTANCE_NAME, len(synced))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return True

        configured = get_guild_channel_id(interaction.guild.id)
        if configured == 0:
            return True

        if interaction.channel and interaction.channel.id != configured:
            try:
                ch = interaction.guild.get_channel(configured)
                where = ch.mention if ch else f"<#{configured}>"
                if interaction.response.is_done():
                    await interaction.followup.send(f"⚠️ This bot is configured to only accept commands in {where}.", ephemeral=True)
                else:
                    await interaction.response.send_message(f"⚠️ This bot is configured to only accept commands in {where}.", ephemeral=True)
            except Exception:
                pass
            return False

        return True

client = SlashMusicClient()
tree = client.tree

PLAYERS = {}

def get_player(guild_id: int) -> MusicPlayer:
    gid = int(guild_id)
    p = PLAYERS.get(gid)
    if p is None:
        p = MusicPlayer(client, gid)
        PLAYERS[gid] = p
    return p

def queue_position_for_append(p: MusicPlayer) -> int:
    # Position within the pending queue (not counting currently-playing track)
    return p.pending_queue_len() + 1

@client.event
async def on_ready():
    logger.info("[%s] Logged in as %s (ID: %s)", INSTANCE_NAME, client.user, client.user.id)

@client.event
async def on_guild_join(guild: discord.Guild):
    logger.info("[%s] Joined guild: %s (%s)", INSTANCE_NAME, guild.name, guild.id)

    if get_guild_channel_id(guild.id):
        return

    target = guild.system_channel
    if target is None:
        for ch in guild.text_channels:
            me = guild.me or guild.get_member(client.user.id)
            if me is None:
                continue
            perms = ch.permissions_for(me)
            if perms.view_channel and perms.send_messages:
                target = ch
                break

    if target is None:
        logger.warning("No channel found to post setup message in guild %s", guild.id)
        return

    await target.send(
        "👋 **Thanks for adding me!**\n"
        "What channel would you like to use this bot in?\n\n"
        "Select a channel below and click **Confirm**.\n"
        "_(You need **Manage Server** to complete setup.)_",
        view=SetupView(guild),
    )

def _require_manager():
    async def predicate(interaction: discord.Interaction) -> bool:
        return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild
    return app_commands.check(predicate)

# -------------------- SLASH COMMANDS --------------------
@tree.command(name="setup", description="Pick the channel where the bot will accept commands (auto-config)")
@_require_manager()
async def setup_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    await interaction.response.send_message(
        "Select the **text channel** where the bot will accept commands:",
        view=SetupView(interaction.guild),
        ephemeral=True,
    )

@tree.command(name="join", description="Join your voice channel")
async def join_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    p = get_player(interaction.guild.id)
    p.set_channel(interaction.channel)
    await p.ensure_voice(interaction)
    await interaction.followup.send("✅ Joined your voice channel.")

@tree.command(name="play", description="Play a YouTube/Spotify query or URL")
@app_commands.describe(query="Search text or URL (YouTube/Spotify)")
async def play_cmd(interaction: discord.Interaction, query: str):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    p = get_player(interaction.guild.id)
    p.set_channel(interaction.channel)
    await p.ensure_voice(interaction)

    if "open.spotify.com/" in query:
        async def enqueue_spotify_buffered():
            try:
                gen = spotify_track_objects(query, interaction.user.mention)

                first: Optional[Track] = None
                async for t in gen:
                    first = t
                    break

                if first is None:
                    await interaction.followup.send("⚠️ Spotify link had no playable tracks.")
                    return

                pos = queue_position_for_append(p)
                await p.add_track(first)
                await interaction.followup.send(f"✅ Queued: **{first.title or 'Unknown'}** — Position **#{pos}**\nBuffering the rest…")

                buffered = 1
                async for t in gen:
                    await p.add_track(t)
                    buffered += 1

                await interaction.followup.send(f"✅ Finished buffering. Total queued from Spotify: **{buffered}**")
            except Exception as e:
                try:
                    await interaction.followup.send(f"⚠️ Spotify buffering failed:\n`{e}`")
                except Exception:
                    pass

        p.spawn_bg(enqueue_spotify_buffered())
        return

    # Non-Spotify: resolve now so we can show actual track name immediately
    title = "Unknown title"
    artist = None
    url = None
    dur = None
    try:
        title, artist, url, dur = await resolve_title_for_queue_display(query)
    except Exception:
        pass

    pos = queue_position_for_append(p)
    await p.add_track(
        Track(
            query=query,
            requested_by=interaction.user.mention,
            title=title,
            artist=artist,
            webpage_url=url,
            duration=dur,
        )
    )
    await interaction.followup.send(f"✅ Queued: **{title}** — Position **#{pos}**")

@tree.command(name="playnext", description="Queue a track to play next (top of the queue)")
@app_commands.describe(query="Search text or URL (YouTube/Spotify)")
async def playnext_cmd(interaction: discord.Interaction, query: str):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    p = get_player(interaction.guild.id)
    p.set_channel(interaction.channel)
    await p.ensure_voice(interaction)

    # For Spotify links here, we intentionally only take the first track and put it next.
    if "open.spotify.com/" in query:
        try:
            gen = spotify_track_objects(query, interaction.user.mention)
            first: Optional[Track] = None
            async for t in gen:
                first = t
                break
            if first is None:
                await interaction.followup.send("⚠️ Spotify link had no playable tracks.")
                return
            await p.add_track_next(first)
            await interaction.followup.send(f"⏭️ Queued next: **{first.title or 'Unknown'}** — Position **#1**")
        except Exception as e:
            await interaction.followup.send(f"⚠️ Failed:\n`{e}`")
        return

    title = "Unknown title"
    artist = None
    url = None
    dur = None
    try:
        title, artist, url, dur = await resolve_title_for_queue_display(query)
    except Exception:
        pass

    await p.add_track_next(
        Track(
            query=query,
            requested_by=interaction.user.mention,
            title=title,
            artist=artist,
            webpage_url=url,
            duration=dur,
        )
    )
    await interaction.followup.send(f"⏭️ Queued next: **{title}** — Position **#1**")

@tree.command(name="nowplaying", description="Post a one-off now playing message (legacy)")
async def nowplaying_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    await interaction.response.defer()
    p = get_player(interaction.guild.id)
    p.set_channel(interaction.channel)
    await p.post_nowplaying_message()
    await interaction.followup.send("✅ Posted Now Playing (one-off). Use /panel for the persistent GUI.", ephemeral=True)

@tree.command(name="panel", description="Create (or reuse) a persistent Music Panel GUI that auto-updates")
async def panel_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    await interaction.response.defer()
    p = get_player(interaction.guild.id)
    p.set_channel(interaction.channel)
    msg = await p.ensure_panel_message()
    if msg is None:
        await interaction.followup.send("⚠️ Couldn't create the panel message (missing perms?).", ephemeral=True)
        return
    await p.update_panel_message(force=True)
    await interaction.followup.send("✅ Music Panel is ready and will auto-update while playing.", ephemeral=True)

@tree.command(name="queue", description="Show the current queue")
async def queue_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    p = get_player(interaction.guild.id)
    await interaction.response.send_message(p.format_queue(max_items=20))

@tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    p = get_player(interaction.guild.id)
    n = p.shuffle_queue()
    await interaction.response.send_message(f"🔀 Shuffled **{n}** queued track(s).")

@tree.command(name="remove", description="Remove a track from the queue (by number)")
@app_commands.describe(index="Queue index (1 = first)")
async def remove_cmd(interaction: discord.Interaction, index: int):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    p = get_player(interaction.guild.id)
    try:
        removed = p.remove_from_queue(index)
        label = removed.title or removed.query
        await interaction.response.send_message(f"🗑 Removed **{label}** from the queue.")
    except Exception:
        await interaction.response.send_message("That track number is out of range.", ephemeral=True)

@tree.command(name="skipto", description="Skip to a track in the queue (by number)")
@app_commands.describe(index="Queue index to skip to (1 = first)")
async def skipto_cmd(interaction: discord.Interaction, index: int):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    p = get_player(interaction.guild.id)
    try:
        dropped = p.skip_to_queue_index(index)
        await interaction.response.send_message(f"⏭ Skipping to track #{index}. Dropped **{dropped}** queued track(s).")
    except Exception:
        await interaction.response.send_message("That track number is out of range.", ephemeral=True)

@tree.command(name="previous", description="Play the previous finished track")
async def previous_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    p = get_player(interaction.guild.id)
    ok = p.previous()
    await interaction.response.send_message("⏮️ Previous." if ok else "No previous track yet.", ephemeral=True)

@tree.command(name="skip", description="Skip the current track")
async def skip_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    p = get_player(interaction.guild.id)
    p.skip()
    await interaction.response.send_message("⏭️ Skipped.")

@tree.command(name="pause", description="Pause playback")
async def pause_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    p = get_player(interaction.guild.id)
    p.pause()
    await interaction.response.send_message("⏸️ Paused.")

@tree.command(name="resume", description="Resume playback")
async def resume_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    p = get_player(interaction.guild.id)
    p.resume()
    await interaction.response.send_message("▶️ Resumed.")

@tree.command(name="volume", description="Set playback volume (0-200)")
@app_commands.describe(percent="Volume percent (0-200)")
async def volume_cmd(interaction: discord.Interaction, percent: int):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    p = get_player(interaction.guild.id)
    v = p.set_volume(percent / 100.0)
    await interaction.response.send_message(f"🔊 Volume set to {int(v*100)}%")

@tree.command(name="clear", description="Clear the queue")
async def clear_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    p = get_player(interaction.guild.id)
    n = p.clear_queue()
    await interaction.response.send_message(f"🧹 Cleared **{n}** queued track(s).")

@tree.command(name="stop", description="Stop playback, clear queue, and disconnect")
async def stop_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    p = get_player(interaction.guild.id)
    await p.stop()
    await interaction.response.send_message("🛑 Stopped and disconnected.")

client.run(DISCORD_TOKEN)
