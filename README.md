[README.md](https://github.com/user-attachments/files/33521519/README.md)
# Forum Audio Search Bot

A Discord bot that searches the audio files **already posted in your server's forum channels**. Nothing to upload by hand: it reads the forum posts, remembers each audio attachment, and lets you find them by post title, forum tags, file name or the text of the post.

## Commands

| Command | What it does |
| --- | --- |
| `/search [query]` | Search the forums' audio, 10 results per page with ◀ Prev / Next ▶ buttons (up to 200 matches). Pick a result from the dropdown to post the file in the channel; you can keep browsing and pick more. Each result links back to the original post |
| `/send clip` | Post a file by choosing it from the suggestions |
| `/play clip` | Play a file in your current voice channel |
| `/reindex` | Rescan the forums for audio the bot missed (needs Manage Server) |

Search matches word starts, so `kick` finds "Kick drum 01". Post titles rank highest, then file names, forum tags, then the post text.

## How indexing works

- On startup the bot scans every forum post (active and archived) and indexes audio attachments (mp3, wav, ogg, m4a, flac, aac, opus, webm and anything Discord marks as audio). The first scan of a big forum can take a few minutes. Later startups only look at new messages.
- New posts and replies with audio are indexed as they're posted.
- Deleted messages or posts are removed from the index, and renamed or re-tagged posts are updated.
- Files are not copied. The bot re-fetches the original message whenever it needs the file, so it always has a working link.

## Setup

1. In the [Discord developer portal](https://discord.com/developers/applications), open your app → **Bot** → **Privileged Gateway Intents** and switch on **Message Content Intent**. Save. Without it the bot can't read post text or attachments and won't start.
2. Make sure the bot can see your forums: it needs **View Channel** and **Read Message History** in each forum channel (check private forums too). It also needs **Send Messages** and **Attach Files** where you use `/search`, and **Connect** and **Speak** for `/play`.
3. Install dependencies (Python 3.10+):
   ```
   pip3 install -r requirements.txt
   ```
4. For `/play`, install [FFmpeg](https://ffmpeg.org/download.html) (on a Mac: `brew install ffmpeg`).
5. Copy `token.env.example` to `token.env` and set `DISCORD_TOKEN`. To limit indexing to certain forums, set `FORUM_CHANNEL_IDS` (turn on Developer Mode in Discord, then right-click a forum → Copy Channel ID).
6. Run it and leave the window open:
   ```
   python3 bot.py
   ```

## Notes

- The index lives in `forum_index.db` next to `bot.py` and can be deleted at any time; the bot rebuilds it from the forums.
- Each server has its own index.
- Posting a file is limited by the server's upload size limit.
- The old `/add`, `/list` and `/remove` commands are gone because the bot now reads the forums instead.

## Ideas for later

- Speech-to-text (e.g. Whisper) so you can search what's *said* in a clip
- Index audio from normal text channels as well as forums
