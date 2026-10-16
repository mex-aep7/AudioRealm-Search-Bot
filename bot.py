"""Forum audio search bot for Discord.

Indexes audio files that are already posted in your server's forum channels
(thread title, forum tags, file name and post text) so people can find them with
/search. Nothing needs uploading by hand: new posts are indexed as they appear,
and older posts are picked up by a scan on startup and by /reindex.
"""

import asyncio
import os
import re
import sqlite3
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

# Settings live in token.env next to this file (no leading dot, so Finder allows it).
load_dotenv(Path(__file__).with_name("token.env"))

TOKEN = os.environ["DISCORD_TOKEN"]
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")  # optional: instant command sync for one server
DB_PATH = Path(__file__).parent / os.getenv("DB_PATH", "forum_index.db")
# Optional: only index these forum channels (comma-separated IDs). Empty = all forums.
FORUM_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("FORUM_CHANNEL_IDS", ""))}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".oga", ".m4a", ".flac", ".aac", ".opus", ".webm"}
PAGE_SIZE = 10      # results shown per page
MAX_RESULTS = 200   # most matches a single search will return (20 pages)

# ---------------------------------------------------------------- database

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS clips (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id      INTEGER NOT NULL,
        attachment_id INTEGER NOT NULL UNIQUE,
        filename      TEXT NOT NULL,
        thread_title  TEXT NOT NULL,
        tags          TEXT NOT NULL DEFAULT '',
        text          TEXT NOT NULL DEFAULT '',
        author        TEXT NOT NULL DEFAULT '',
        forum_id      INTEGER NOT NULL,
        thread_id     INTEGER NOT NULL,
        message_id    INTEGER NOT NULL,
        jump_url      TEXT NOT NULL,
        created_at    INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS clips_thread ON clips (thread_id);
    CREATE INDEX IF NOT EXISTS clips_message ON clips (message_id);
    CREATE VIRTUAL TABLE IF NOT EXISTS clips_fts USING fts5(
        thread_title, filename, tags, text, clip_id UNINDEXED
    );
    CREATE TABLE IF NOT EXISTS threads_seen (
        thread_id       INTEGER PRIMARY KEY,
        last_message_id INTEGER NOT NULL
    );
    """
)
db.commit()


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: every word must match as a prefix."""
    tokens = re.findall(r"[^\W_]+", text.lower())
    return " ".join(f'"{t}"*' for t in tokens)


def is_audio(att) -> bool:
    return (att.content_type or "").startswith("audio/") or Path(att.filename).suffix.lower() in AUDIO_EXTS


def tag_names(thread) -> str:
    return ", ".join(t.name for t in (getattr(thread, "applied_tags", None) or []))


def index_message(message, thread) -> int:
    """Add the audio attachments of one forum message to the index. Returns how many were new."""
    tags = tag_names(thread)
    text = (message.content or "").strip()[:1000]
    added = 0
    for att in message.attachments:
        if not is_audio(att):
            continue
        cur = db.execute(
            "INSERT OR IGNORE INTO clips (guild_id, attachment_id, filename, thread_title, tags, text,"
            " author, forum_id, thread_id, message_id, jump_url, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread.guild.id, att.id, att.filename, thread.name, tags, text,
                message.author.display_name, thread.parent_id, thread.id, message.id,
                message.jump_url, int(message.created_at.timestamp()),
            ),
        )
        if cur.rowcount:
            db.execute(
                "INSERT INTO clips_fts (thread_title, filename, tags, text, clip_id) VALUES (?, ?, ?, ?, ?)",
                (thread.name, Path(att.filename).stem, tags, text, cur.lastrowid),
            )
            added += 1
    if added:
        db.commit()
    return added


def delete_clip(clip_id: int) -> None:
    db.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
    db.execute("DELETE FROM clips_fts WHERE clip_id = ?", (clip_id,))
    db.commit()


def remove_by_message(message_id: int) -> None:
    for r in db.execute("SELECT id FROM clips WHERE message_id = ?", (message_id,)).fetchall():
        delete_clip(r["id"])


def remove_by_thread(thread_id: int) -> None:
    for r in db.execute("SELECT id FROM clips WHERE thread_id = ?", (thread_id,)).fetchall():
        delete_clip(r["id"])
    db.execute("DELETE FROM threads_seen WHERE thread_id = ?", (thread_id,))
    db.commit()


def refresh_thread_meta(thread) -> None:
    """Thread was renamed or re-tagged: update its clips and their search entries."""
    tags = tag_names(thread)
    rows = db.execute("SELECT id, filename, text FROM clips WHERE thread_id = ?", (thread.id,)).fetchall()
    db.execute("UPDATE clips SET thread_title = ?, tags = ? WHERE thread_id = ?", (thread.name, tags, thread.id))
    for r in rows:
        db.execute("DELETE FROM clips_fts WHERE clip_id = ?", (r["id"],))
        db.execute(
            "INSERT INTO clips_fts (thread_title, filename, tags, text, clip_id) VALUES (?, ?, ?, ?, ?)",
            (thread.name, Path(r["filename"]).stem, tags, r["text"], r["id"]),
        )
    db.commit()


def get_clip(clip_id: int, guild_id: int):
    return db.execute("SELECT * FROM clips WHERE id = ? AND guild_id = ?", (clip_id, guild_id)).fetchone()


def search_clips(guild_id: int, query: str, limit: int = MAX_RESULTS):
    match = fts_query(query)
    if not match:
        return db.execute(
            "SELECT * FROM clips WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()
    # Thread title weighs most, then file name, forum tags, and finally the post text.
    return db.execute(
        "SELECT c.* FROM clips_fts JOIN clips c ON c.id = clips_fts.clip_id "
        "WHERE clips_fts MATCH ? AND c.guild_id = ? "
        "ORDER BY bm25(clips_fts, 10.0, 6.0, 4.0, 1.0, 0.0) LIMIT ?",
        (match, guild_id, limit),
    ).fetchall()


def count_clips(guild_id: int) -> int:
    return db.execute("SELECT COUNT(*) FROM clips WHERE guild_id = ?", (guild_id,)).fetchone()[0]


def label(row) -> str:
    return f"{row['thread_title']} · {row['filename']}"


# ---------------------------------------------------------------- indexing

def watched_forum(channel) -> bool:
    return isinstance(channel, discord.ForumChannel) and (not FORUM_IDS or channel.id in FORUM_IDS)


async def backfill_thread(thread) -> int:
    """Index messages in a thread that we haven't seen yet."""
    seen = db.execute("SELECT last_message_id FROM threads_seen WHERE thread_id = ?", (thread.id,)).fetchone()
    newest = seen["last_message_id"] if seen else 0
    after = discord.Object(id=newest) if seen else None
    added = 0
    async for msg in thread.history(limit=None, after=after, oldest_first=True):
        added += index_message(msg, thread)
        newest = max(newest, msg.id)
    if newest:
        db.execute("INSERT OR REPLACE INTO threads_seen (thread_id, last_message_id) VALUES (?, ?)", (thread.id, newest))
        db.commit()
    return added


_backfilling: set[int] = set()
_background: set[asyncio.Task] = set()


async def run_backfill(guild: discord.Guild):
    """Scan every watched forum in the guild. Returns (threads scanned, clips added) or None if already running."""
    if guild.id in _backfilling:
        return None
    _backfilling.add(guild.id)
    threads_scanned = clips_added = 0
    try:
        forums = [f for f in guild.forums if watched_forum(f)]
        try:
            active = await guild.active_threads()
        except discord.HTTPException as e:
            print(f"[{guild.name}] couldn't list active threads: {e}")
            active = []
        for forum in forums:
            threads = {t.id: t for t in active if t.parent_id == forum.id}
            try:
                async for t in forum.archived_threads(limit=None):
                    threads.setdefault(t.id, t)
            except discord.HTTPException as e:
                print(f"[{guild.name}] couldn't list archived posts in #{forum.name}: {e}")
            for thread in threads.values():
                try:
                    clips_added += await backfill_thread(thread)
                except discord.HTTPException as e:
                    print(f"[{guild.name}] couldn't read post '{thread.name}': {e}")
                threads_scanned += 1
        print(f"[{guild.name}] scanned {threads_scanned} posts, added {clips_added} audio files")
        return threads_scanned, clips_added
    finally:
        _backfilling.discard(guild.id)


def start_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# ------------------------------------------------------------------ bot

class AudioBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # also switch this on in the developer portal
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._started = False

    async def setup_hook(self):
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")
        if not self._started:
            self._started = True
            for guild in self.guilds:
                start_background(run_backfill(guild))

    async def on_guild_join(self, guild: discord.Guild):
        start_background(run_backfill(guild))

    async def on_message(self, message: discord.Message):
        channel = message.channel
        if not message.attachments or message.author.id == self.user.id:
            return
        if isinstance(channel, discord.Thread) and watched_forum(channel.parent):
            if index_message(message, channel):
                print(f"Indexed new audio in '{channel.name}'")

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        remove_by_message(payload.message_id)

    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent):
        remove_by_thread(payload.thread_id)

    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        if not watched_forum(after.parent):
            return
        if before.name != after.name or before.applied_tags != after.applied_tags:
            refresh_thread_meta(after)


bot = AudioBot()
play_tokens: dict[int, object] = {}


async def fresh_attachment(row):
    """Re-fetch the message so we get a working (non-expired) file link."""
    try:
        channel = bot.get_channel(row["thread_id"]) or await bot.fetch_channel(row["thread_id"])
        msg = await channel.fetch_message(row["message_id"])
    except discord.NotFound:
        delete_clip(row["id"])
        return None
    except discord.HTTPException:
        return None
    for att in msg.attachments:
        if att.id == row["attachment_id"]:
            return att
    delete_clip(row["id"])
    return None


async def send_clip(channel, row, requester: str):
    """Post the audio file in `channel`. Returns an error message, or None on success."""
    att = await fresh_attachment(row)
    if att is None:
        return "That file is no longer available (the post may have been deleted)."
    try:
        file = await att.to_file()
        await channel.send(f"**{label(row)}** (requested by {requester})\n{row['jump_url']}", file=file)
    except discord.HTTPException:
        return "Couldn't post that file (it may exceed this server's upload limit)."
    return None


async def clip_autocomplete(interaction: discord.Interaction, current: str):
    rows = search_clips(interaction.guild_id, current, limit=25)
    return [app_commands.Choice(name=label(r)[:100], value=str(r["id"])) for r in rows]


def clip_from_choice(value: str, guild_id: int):
    try:
        return get_clip(int(value), guild_id)
    except ValueError:
        return None


# ------------------------------------------------------------- search UI

def results_embed(query: str, rows, start: int, total: int, page: int, pages: int) -> discord.Embed:
    title = f"Results for “{query}”" if query else "Latest audio"
    lines = []
    for i, r in enumerate(rows, start + 1):
        extra = " · ".join(x for x in (r["tags"], r["text"].replace("\n", " ")) if x)
        line = f"**{i}. {discord.utils.escape_markdown(r['thread_title'])}** · `{r['filename']}`"
        if extra:
            line += f"\n{extra[:150]}"
        line += f"\n[Open post]({r['jump_url']})"
        lines.append(line)
    embed = discord.Embed(title=title, description="\n\n".join(lines), color=0x5865F2)
    if total >= MAX_RESULTS:
        count = f"first {MAX_RESULTS} results, narrow your search to see others"
    else:
        count = f"{total} result{'s' if total != 1 else ''}"
    embed.set_footer(text=f"Page {page + 1} of {pages} · {count}")
    return embed


class ResultSelect(discord.ui.Select):
    def __init__(self, rows, start: int):
        options = [
            discord.SelectOption(
                label=f"{i}. {label(r)}"[:100],
                value=str(r["id"]),
                description=((r["tags"] or r["text"] or "").replace("\n", " ")[:100] or None),
            )
            for i, r in enumerate(rows, start + 1)
        ]
        super().__init__(placeholder="Pick a file to post in this channel", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        row = get_clip(int(self.values[0]), interaction.guild_id)
        if row is None:
            await interaction.response.send_message("That file is no longer in the index.", ephemeral=True)
            return
        # Keep the result list on screen so you can keep browsing and pick more.
        await interaction.response.defer(thinking=True, ephemeral=True)
        error = await send_clip(interaction.channel, row, interaction.user.mention)
        await interaction.followup.send(error or f"Posted **{label(row)}**.", ephemeral=True)


class PageButton(discord.ui.Button):
    def __init__(self, delta: int, disabled: bool):
        super().__init__(
            label="◀ Prev" if delta < 0 else "Next ▶",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
            row=1,
        )
        self.delta = delta

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        view.page = min(max(view.page + self.delta, 0), view.pages - 1)
        view.render()
        await interaction.response.edit_message(embed=view.embed(), view=view)


class ResultView(discord.ui.View):
    """One page of results at a time, with Prev/Next buttons and a dropdown for the current page."""

    def __init__(self, rows, query: str):
        super().__init__(timeout=600)
        self.rows = rows
        self.query = query
        self.page = 0
        self.pages = max(1, -(-len(rows) // PAGE_SIZE))
        self.render()

    def page_rows(self):
        start = self.page * PAGE_SIZE
        return self.rows[start:start + PAGE_SIZE]

    def embed(self) -> discord.Embed:
        return results_embed(
            self.query, self.page_rows(), self.page * PAGE_SIZE, len(self.rows), self.page, self.pages
        )

    def render(self):
        self.clear_items()
        self.add_item(ResultSelect(self.page_rows(), self.page * PAGE_SIZE))
        if self.pages > 1:
            self.add_item(PageButton(-1, self.page == 0))
            self.add_item(
                discord.ui.Button(
                    label=f"{self.page + 1} / {self.pages}",
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                    row=1,
                )
            )
            self.add_item(PageButton(1, self.page >= self.pages - 1))


# -------------------------------------------------------------- commands

@bot.tree.command(name="search", description="Search audio files posted in the forums")
@app_commands.describe(query="Words from the post title, tags, file name or text (empty = latest)")
@app_commands.guild_only()
async def search(interaction: discord.Interaction, query: str = ""):
    rows = search_clips(interaction.guild_id, query)
    if not rows:
        if count_clips(interaction.guild_id) == 0:
            msg = ("No audio indexed yet. If this is a new setup, wait a minute for the first scan, "
                   "or ask a server manager to run /reindex.")
        else:
            msg = f"No audio found for “{query}”."
        await interaction.response.send_message(msg, ephemeral=True)
        return
    view = ResultView(rows, query)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


@bot.tree.command(name="send", description="Post one of the indexed audio files in this channel")
@app_commands.describe(clip="Start typing to find a file")
@app_commands.autocomplete(clip=clip_autocomplete)
@app_commands.guild_only()
async def send(interaction: discord.Interaction, clip: str):
    row = clip_from_choice(clip, interaction.guild_id)
    if row is None:
        await interaction.response.send_message("Pick a file from the suggestions list.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    error = await send_clip(interaction.channel, row, interaction.user.mention)
    await interaction.followup.send(error or "Posted.", ephemeral=True)


@bot.tree.command(name="play", description="Play an indexed audio file in your voice channel")
@app_commands.describe(clip="Start typing to find a file")
@app_commands.autocomplete(clip=clip_autocomplete)
@app_commands.guild_only()
async def play(interaction: discord.Interaction, clip: str):
    row = clip_from_choice(clip, interaction.guild_id)
    if row is None:
        await interaction.response.send_message("Pick a file from the suggestions list.", ephemeral=True)
        return
    voice_state = interaction.user.voice
    if voice_state is None or voice_state.channel is None:
        await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    att = await fresh_attachment(row)
    if att is None:
        await interaction.followup.send("That file is no longer available.", ephemeral=True)
        return

    guild = interaction.guild
    vc = guild.voice_client
    if vc is None:
        vc = await voice_state.channel.connect()
    elif vc.channel != voice_state.channel:
        await vc.move_to(voice_state.channel)
    if vc.is_playing():
        vc.stop()

    token = object()
    play_tokens[guild.id] = token

    def after_play(error):
        if error:
            print(f"Playback error: {error}")
        # Leave when this clip finishes, unless a newer clip has taken over.
        if play_tokens.get(guild.id) is token:
            bot.loop.create_task(vc.disconnect())

    source = discord.FFmpegPCMAudio(
        att.url, before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    )
    vc.play(source, after=after_play)
    await interaction.followup.send(f"Playing **{label(row)}**.", ephemeral=True)


@bot.tree.command(name="reindex", description="Rescan the forums for new audio files (server managers only)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def reindex(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need the Manage Server permission.", ephemeral=True)
        return
    if interaction.guild_id in _backfilling:
        await interaction.response.send_message("A scan is already running. Try again in a bit.", ephemeral=True)
        return
    await interaction.response.send_message(
        "Scanning forum posts… this can take a while on big forums. I'll post here when it's done.",
        ephemeral=True,
    )
    result = await run_backfill(interaction.guild)
    if result is None:
        return
    posts, added = result
    try:
        await interaction.channel.send(
            f"{interaction.user.mention} Scan finished: {posts} posts checked, {added} new audio files added "
            f"({count_clips(interaction.guild_id)} indexed in total)."
        )
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    bot.run(TOKEN)
