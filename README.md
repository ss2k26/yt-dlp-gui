# yt-dlp GUI

A tiny Tkinter front-end for [yt-dlp](https://github.com/yt-dlp/yt-dlp). No third-party Python packages needed.

## Run

1. Install Python 3.9+ from python.org (tick "tcl/tk" - it is on by default).
2. Double-click `yt_dlp_gui.pyw`, or run `py yt_dlp_gui.pyw`.
3. On first start click **Download yt-dlp**. Optionally click **Get ffmpeg** (Windows).

### Or make a standalone .exe
Run `build.bat`. The result is `dist\yt-dlp-gui.exe` (copy it anywhere; it keeps `bin\` and `settings.json` next to itself).

## Features

- **Update check and one-click update** - compares your yt-dlp version with the latest GitHub release, downloads `yt-dlp.exe` and verifies its SHA-256.
- **Any supported site** - paste one or many links (one per line); not limited to YouTube.
- **Format and quality** - Video: mp4 / mkv / webm, max quality from 144p to 4K. Audio: mp3 / m4a / opus / flac / wav / original, with bitrate choice.
- Playlist download, subtitles, metadata and thumbnail embedding, cookies from your browser (for login-only or age-gated videos), and a free-text box for any extra yt-dlp argument.
- **Whole playlists become queue items** - with *Download whole playlist* ticked, the app first reads the playlist, then adds every video as its own item (so each can succeed, fail and be retried separately). Videos go into a folder named after the playlist, numbered `01 - ...`. If the playlist can't be read, the link is queued as a single item and yt-dlp handles it.
- **Download queue** - *Add to queue* only adds links (it never starts anything by itself). Press **Start queue** when you are ready; items then download one after another. Options (format, quality...) are saved per link when you add it. Links added while the queue is running are picked up automatically.
- **Pause / Resume** - *Pause* stops the current download, keeps its partial file and holds the queue (the item shows as "Paused" and goes first again). *Resume* continues from the partial file where the site allows it, then carries on with the rest.
- **Tabs**
  - **Queue** - waiting and running items with live progress. *Start queue* resumes after a cancel; remove items you no longer want.
  - **Success** - finished downloads. Double-click (or *Open file* / *Open folder*) to open them.
  - **Failed** - items that errored or were cancelled, with the reason and the last yt-dlp output. **Retry selected** / **Retry all** puts them back in the queue (press *Start queue* to run them) with their original settings; *Copy links* copies the URLs.
  - **Log** - timestamped, colour-coded (errors red, warnings orange, saved files blue). Filter to errors and warnings only, auto-scroll, clear, or save to a text file.
- Queued and failed items are remembered in `state.json`, so you can retry after restarting the app.
- **Cancel current** stops the current download, marks it as failed (so you can retry it) and stops the queue; the rest stay queued until you press *Start queue*.

## About ffmpeg

Without ffmpeg, yt-dlp can only grab pre-merged files (about 720p max on YouTube) and cannot convert to MP3.
With it you get 1080p / 1440p / 4K merging and audio conversion. The **Get ffmpeg** button downloads it into `bin\`.

## Files

- `bin\` - yt-dlp and ffmpeg live here (a copy found on PATH is used as fallback).
- `settings.json` - remembers your choices.
- `state.json` - remembers queued and failed items.
- `.gitignore` - keeps `bin\`, `settings.json`, `state.json`, build output and zips out of git.
