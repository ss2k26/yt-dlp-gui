"""
yt-dlp GUI - a lightweight Tkinter front-end for https://github.com/yt-dlp/yt-dlp

* Pure Python standard library (Tkinter) - nothing to pip install.
* Manages the official yt-dlp standalone binary itself (download / update).
* Works with every site yt-dlp supports, not only YouTube.
* Download queue with Queue / Success / Failed / Log tabs, retry for failed items.
* Windows first, but also runs on Linux / macOS if Python + Tk are present.
"""

import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from collections import deque
from tkinter import filedialog, messagebox, ttk

APP_NAME = "yt-dlp GUI"
IS_WIN = sys.platform.startswith("win")
NOFLAG = subprocess.CREATE_NO_WINDOW if IS_WIN else 0

APP_DIR = os.path.dirname(
    os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
)
BIN_DIR = os.path.join(APP_DIR, "bin")
CONFIG_PATH = os.path.join(APP_DIR, "settings.json")
STATE_PATH = os.path.join(APP_DIR, "state.json")  # queued + failed items survive restarts
LOG_MAX_LINES = 4000

RELEASE_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
RELEASE_DL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
FFMPEG_ZIP = (
    "https://github.com/yt-dlp/FFmpeg-Builds/releases/latest/download/"
    "ffmpeg-master-latest-win64-gpl.zip"
)

if IS_WIN:
    ASSET, LOCAL_NAME = "yt-dlp.exe", "yt-dlp.exe"
elif sys.platform == "darwin":
    ASSET, LOCAL_NAME = "yt-dlp_macos", "yt-dlp"
else:
    ASSET, LOCAL_NAME = "yt-dlp_linux", "yt-dlp"

QUALITIES = ["Best available", "2160p (4K)", "1440p", "1080p", "720p",
             "480p", "360p", "240p", "144p"]
CONTAINERS = ["mp4", "mkv", "webm"]
AUDIO_FORMATS = ["mp3", "m4a", "opus", "flac", "wav", "best (original)"]
AUDIO_QUALITIES = ["Best", "320K", "256K", "192K", "128K", "96K"]
BROWSERS = ["none", "chrome", "edge", "firefox", "brave", "opera", "vivaldi", "safari"]

DEFAULTS = {
    "outdir": os.path.join(os.path.expanduser("~"), "Downloads"),
    "mode": "video",
    "container": "mp4",
    "vq": "1080p",
    "aformat": "mp3",
    "aq": "192K",
    "playlist": False,
    "embed": True,
    "subs": False,
    "langs": "en,id",
    "cookies": "none",
    "extra": "",
}

# yt-dlp output parsing
RE_PROGRESS = re.compile(r"(\d+(?:\.\d+)?)%")
RE_SPEED = re.compile(r"at\s+(\S+)")
RE_ETA = re.compile(r"ETA\s+(\S+)")
RE_DEST = re.compile(r"^\[(?:download|ExtractAudio)\]\s+Destination:\s+(.+)$")
RE_MERGE = re.compile(r'^\[Merger\]\s+Merging formats into\s+"(.+)"$')
RE_HAVE = re.compile(r"^\[download\]\s+(.+?)\s+has already been downloaded")
RE_ITEM = re.compile(r"Downloading item (\d+) of (\d+)")
RE_FRAG = re.compile(r"\.f[\w-]+\.\w+$")  # intermediate stream such as name.f137.mp4


# ----------------------------------------------------------------- helpers
def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass  # read-only install dir: just don't persist


def local_path(name):
    return os.path.join(BIN_DIR, name + (".exe" if IS_WIN else ""))


def find_tool(name):
    """Prefer the copy in ./bin, then fall back to PATH."""
    p = local_path(name)
    if os.path.isfile(p):
        return p
    return shutil.which(name)


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "yt-dlp-gui"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download_file(url, dest, cb=None):
    req = urllib.request.Request(url, headers={"User-Agent": "yt-dlp-gui"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if cb and total:
                cb(done * 100 / total, f"{done / 1048576:.1f} / {total / 1048576:.1f} MiB")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_ytdlp(path):
    """True = hash matches, False = mismatch, None = could not verify."""
    try:
        txt = http_get(RELEASE_DL + "SHA2-256SUMS").decode("utf-8", "replace")
    except Exception:
        return None
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == ASSET:
            return parts[0].lower() == sha256_of(path).lower()
    return None


def get_local_version(exe):
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=60, creationflags=NOFLAG,
                           stdin=subprocess.DEVNULL)
        return r.stdout.strip() or None
    except Exception:
        return None


def get_latest_version():
    data = json.loads(http_get(RELEASE_API).decode("utf-8"))
    return data["tag_name"].strip()


def kill_tree(proc):
    if proc and proc.poll() is None:
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, creationflags=NOFLAG,
                               stdin=subprocess.DEVNULL)
            else:
                proc.terminate()
        except Exception:
            pass


def split_args(text):
    if not text.strip():
        return []
    try:
        parts = shlex.split(text, posix=not IS_WIN)
    except ValueError:
        parts = text.split()
    return [p[1:-1] if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]


def height_of(label):
    m = re.match(r"(\d+)p", label)
    return int(m.group(1)) if m else None


def safe_name(name, limit=100):
    """Make a playlist title usable as a folder name."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return s[:limit].rstrip(" .") or "playlist"


def classify(line):
    """Log level used for colouring / filtering."""
    u = line.lstrip()
    if u.startswith("ERROR"):
        return "error"
    if u.startswith("WARNING"):
        return "warn"
    if (u.startswith(("[download] Destination", "[Merger]", "[ExtractAudio]"))
            or "has already been downloaded" in u):
        return "info"
    return "normal"


def open_path(path):
    if not path or not os.path.exists(path):
        return
    if IS_WIN:
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def item_label(item):
    base = item["name"] or item["title"] or item["url"]
    if item["total"] > 1:
        if item["status"] == "success":
            return f"{item['total']} items (last: {base})"
        return f"[{item['idx']}/{item['total']}] {base}"
    return base


def build_args(c, outdir, ffmpeg):
    """Turn the UI settings dict into yt-dlp arguments."""
    a = ["--newline", "--encoding", "utf-8", "--no-colors", "-P", outdir]
    if ffmpeg and os.path.dirname(ffmpeg) == BIN_DIR:
        a += ["--ffmpeg-location", BIN_DIR]

    if c["playlist"]:
        a += ["--yes-playlist", "-o",
              "%(playlist_title&{}/|)s%(playlist_index&{} - |)s%(title).150B.%(ext)s"]
    else:
        prefix = c.get("prefix", "").replace("%", "%%")  # "01 - " for expanded playlist items
        a += ["--no-playlist", "-o", prefix + "%(title).150B.%(ext)s"]

    mode, cont = c["mode"], c["container"]
    af = c["aformat"].split()[0]
    if mode == "video":
        h = height_of(c["vq"])
        flt = f"[height<={h}]" if h else ""
        if ffmpeg:
            a += ["-f", f"bv*{flt}+ba/b{flt}"]
            if cont == "mp4":
                a += ["-S", "res,ext:mp4:m4a"]
            elif cont == "webm":
                a += ["-S", "res,vcodec:vp9,acodec:opus"]
            a += ["--merge-output-format", cont]
        else:  # no ffmpeg -> only pre-merged single files
            a += ["-f", f"b{flt}/b"]
    else:
        if ffmpeg:
            a += ["-f", "bestaudio/best", "-x", "--audio-format", af]
            if af in ("mp3", "m4a", "opus"):
                a += ["--audio-quality", "0" if c["aq"] == "Best" else c["aq"]]
        else:
            a += ["-f", "bestaudio[ext=m4a]/bestaudio"]

    if ffmpeg and c["embed"]:
        a += ["--embed-metadata"]
        thumb_ok = (mode == "video" and cont in ("mp4", "mkv")) or \
                   (mode == "audio" and af in ("mp3", "m4a", "opus", "flac"))
        if thumb_ok:
            a += ["--embed-thumbnail"]

    if c["subs"]:
        a += ["--write-subs", "--write-auto-subs", "--sub-langs", c["langs"].strip() or "en"]
        if ffmpeg and mode == "video":
            a += ["--embed-subs"]

    if c["cookies"] != "none":
        a += ["--cookies-from-browser", c["cookies"]]

    a += split_args(c["extra"])
    return a


# --------------------------------------------------------------------- app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("840x880")
        self.minsize(760, 720)
        self.cfg = load_config()
        self.q = queue.Queue()          # worker threads -> UI
        self.proc = None
        self.items = {}                 # id -> item dict (UI thread only)
        self.next_id = 1
        self.seq = 0                    # queue order
        self.busy = False               # an item is downloading
        self.updating = False           # yt-dlp / ffmpeg download in progress
        self.want_run = False           # queue is "started"
        self.expanding = 0              # playlists still being read
        self.paused = False             # queue paused by the user
        self.pausing = False            # current download is being stopped for a pause
        self._btn_states = None
        self.cancelling = False
        self.run_ok = 0
        self.run_fail = 0
        self.warned_ffmpeg = False
        self.local_ver = None
        self.latest_ver = None

        style = ttk.Style(self)
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        self._build_ui()
        self._apply_mode()
        self._refresh_ffmpeg_label()
        self._load_state()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self.check_updates()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True)

        # --- tools / update
        tools = ttk.LabelFrame(root, text="yt-dlp")
        tools.pack(fill="x", **pad)
        self.ver_label = ttk.Label(tools, text="Checking yt-dlp...")
        self.ver_label.grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.btn_check = ttk.Button(tools, text="Check for updates", command=self.check_updates)
        self.btn_check.grid(row=0, column=1, padx=4)
        self.btn_update = ttk.Button(tools, text="Update now", command=self.update_ytdlp,
                                     state="disabled")
        self.btn_update.grid(row=0, column=2, padx=4)
        self.ff_label = ttk.Label(tools, text="")
        self.ff_label.grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.btn_ffmpeg = ttk.Button(tools, text="Get ffmpeg", command=self.get_ffmpeg)
        self.btn_ffmpeg.grid(row=1, column=1, columnspan=2, sticky="w", padx=4)
        tools.columnconfigure(0, weight=1)
        if not IS_WIN:
            self.btn_ffmpeg.state(["disabled"])

        # --- URLs
        urls = ttk.LabelFrame(root, text="Links (one per line, any site supported by yt-dlp)")
        urls.pack(fill="x", **pad)
        self.url_text = tk.Text(urls, height=3, wrap="none", undo=True)
        self.url_text.pack(fill="x", padx=6, pady=6)
        self.url_text.bind("<Control-Return>", lambda e: (self.add_to_queue(), "break")[1])

        # --- options
        opt = ttk.LabelFrame(root, text="Format & quality (applies to links added from now on)")
        opt.pack(fill="x", **pad)
        self.mode = tk.StringVar(value=self.cfg["mode"])
        ttk.Radiobutton(opt, text="Video", variable=self.mode, value="video",
                        command=self._apply_mode).grid(row=0, column=0, sticky="w", padx=8)
        ttk.Radiobutton(opt, text="Audio only", variable=self.mode, value="audio",
                        command=self._apply_mode).grid(row=1, column=0, sticky="w", padx=8)

        self.container = tk.StringVar(value=self.cfg["container"])
        self.vq = tk.StringVar(value=self.cfg["vq"])
        self.aformat = tk.StringVar(value=self.cfg["aformat"])
        self.aq = tk.StringVar(value=self.cfg["aq"])

        def combo(var, values, row, col, w=14):
            cb = ttk.Combobox(opt, textvariable=var, values=values, state="readonly", width=w)
            cb.grid(row=row, column=col, padx=4, pady=3, sticky="w")
            return cb

        ttk.Label(opt, text="Container").grid(row=0, column=1, sticky="e")
        self.cb_container = combo(self.container, CONTAINERS, 0, 2, 8)
        ttk.Label(opt, text="Max quality").grid(row=0, column=3, sticky="e")
        self.cb_vq = combo(self.vq, QUALITIES, 0, 4)
        ttk.Label(opt, text="Format").grid(row=1, column=1, sticky="e")
        self.cb_af = combo(self.aformat, AUDIO_FORMATS, 1, 2, 14)
        ttk.Label(opt, text="Bitrate").grid(row=1, column=3, sticky="e")
        self.cb_aq = combo(self.aq, AUDIO_QUALITIES, 1, 4)

        self.playlist = tk.BooleanVar(value=self.cfg["playlist"])
        self.embed = tk.BooleanVar(value=self.cfg["embed"])
        self.subs = tk.BooleanVar(value=self.cfg["subs"])
        self.langs = tk.StringVar(value=self.cfg["langs"])
        self.cookies = tk.StringVar(value=self.cfg["cookies"])
        self.extra = tk.StringVar(value=self.cfg["extra"])

        row2 = ttk.Frame(opt)
        row2.grid(row=2, column=0, columnspan=6, sticky="we", padx=8, pady=(6, 2))
        ttk.Checkbutton(row2, text="Download whole playlist", variable=self.playlist).pack(side="left")
        ttk.Checkbutton(row2, text="Embed metadata + thumbnail", variable=self.embed).pack(side="left", padx=12)
        row3 = ttk.Frame(opt)
        row3.grid(row=3, column=0, columnspan=6, sticky="we", padx=8, pady=2)
        ttk.Checkbutton(row3, text="Subtitles, languages:", variable=self.subs).pack(side="left")
        ttk.Entry(row3, textvariable=self.langs, width=14).pack(side="left", padx=4)
        ttk.Label(row3, text="Cookies from browser:").pack(side="left", padx=(16, 4))
        ttk.Combobox(row3, textvariable=self.cookies, values=BROWSERS, state="readonly",
                     width=9).pack(side="left")
        row4 = ttk.Frame(opt)
        row4.grid(row=4, column=0, columnspan=6, sticky="we", padx=8, pady=(2, 6))
        ttk.Label(row4, text="Extra yt-dlp arguments:").pack(side="left")
        ttk.Entry(row4, textvariable=self.extra).pack(side="left", fill="x", expand=True, padx=4)

        # --- output folder
        out = ttk.Frame(root)
        out.pack(fill="x", **pad)
        ttk.Label(out, text="Save to:").pack(side="left")
        self.outdir = tk.StringVar(value=self.cfg["outdir"])
        ttk.Entry(out, textvariable=self.outdir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(out, text="Browse...", command=self._browse).pack(side="left")
        ttk.Button(out, text="Open", command=lambda: open_path(self.outdir.get())
                   ).pack(side="left", padx=(4, 0))

        # --- actions
        act = ttk.Frame(root)
        act.pack(fill="x", **pad)
        self.btn_add = ttk.Button(act, text="Add to queue", command=self.add_to_queue)
        self.btn_add.pack(side="left")
        self.btn_start = ttk.Button(act, text="Start queue", command=self.start_queue)
        self.btn_start.pack(side="left", padx=(6, 0))
        self.btn_pause = ttk.Button(act, text="Pause", command=self.pause_queue,
                                    state="disabled")
        self.btn_pause.pack(side="left", padx=(6, 0))
        self.btn_resume = ttk.Button(act, text="Resume", command=self.start_queue,
                                     state="disabled")
        self.btn_resume.pack(side="left", padx=(6, 0))
        self.btn_cancel = ttk.Button(act, text="Cancel current", command=self.cancel_download,
                                     state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        self.status = ttk.Label(act, text="Ready")
        self.status.pack(side="left", padx=10)

        self.progress = ttk.Progressbar(root, maximum=100)
        self.progress.pack(fill="x", **pad)

        # --- tabs: Queue / Success / Failed / Log
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.tab_queue = ttk.Frame(self.nb)
        self.tab_success = ttk.Frame(self.nb)
        self.tab_failed = ttk.Frame(self.nb)
        self.tab_log = ttk.Frame(self.nb)
        self.nb.add(self.tab_queue, text="Queue (0)")
        self.nb.add(self.tab_success, text="Success (0)")
        self.nb.add(self.tab_failed, text="Failed (0)")
        self.nb.add(self.tab_log, text="Log")

        # Queue tab
        bar = ttk.Frame(self.tab_queue)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(bar, text="Remove selected",
                   command=lambda: self._remove_items(self._sel(self.queue_tree), "queued")
                   ).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Clear queued",
                   command=lambda: self._remove_items(self._ids_with("queued"), "queued")
                   ).pack(side="left")
        self.queue_tree = self._make_tree(
            self.tab_queue,
            [("name", "Item", 430, True), ("status", "Status", 90, False),
             ("progress", "Progress", 200, False)])

        # Success tab
        bar = ttk.Frame(self.tab_success)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(bar, text="Open file", command=self._open_success_file).pack(side="left")
        ttk.Button(bar, text="Open folder", command=self._open_success_folder
                   ).pack(side="left", padx=6)
        ttk.Button(bar, text="Remove selected",
                   command=lambda: self._remove_items(self._sel(self.success_tree), "success")
                   ).pack(side="left")
        ttk.Button(bar, text="Clear list",
                   command=lambda: self._remove_items(self._ids_with("success"), "success")
                   ).pack(side="left", padx=6)
        self.success_tree = self._make_tree(
            self.tab_success,
            [("name", "File", 380, True), ("url", "Link", 260, True),
             ("time", "Done", 60, False)])
        self.success_tree.bind("<Double-1>", lambda e: self._open_success_file())

        # Failed tab
        bar = ttk.Frame(self.tab_failed)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(bar, text="Retry selected",
                   command=lambda: self.retry_failed(False)).pack(side="left")
        ttk.Button(bar, text="Retry all",
                   command=lambda: self.retry_failed(True)).pack(side="left", padx=6)
        ttk.Button(bar, text="Copy links", command=self._copy_failed_links).pack(side="left")
        ttk.Button(bar, text="Remove selected",
                   command=lambda: self._remove_items(self._sel(self.failed_tree), "failed")
                   ).pack(side="left", padx=6)
        ttk.Button(bar, text="Clear list",
                   command=lambda: self._remove_items(self._ids_with("failed"), "failed")
                   ).pack(side="left")
        self.failed_tree = self._make_tree(
            self.tab_failed,
            [("url", "Link", 330, True), ("reason", "Reason", 330, True),
             ("time", "Time", 60, False)])
        self.failed_tree.tag_configure("bad", foreground="#b00020")
        self.failed_tree.bind("<<TreeviewSelect>>", self._show_failed_details)
        self.fail_detail = tk.Text(self.tab_failed, height=5, wrap="word", state="disabled",
                                   relief="flat", background=self.cget("background"))
        self.fail_detail.pack(fill="x", padx=6, pady=(0, 6))

        # Log tab
        bar = ttk.Frame(self.tab_log)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(bar, text="Clear", command=self._clear_log).pack(side="left")
        ttk.Button(bar, text="Save...", command=self._save_log).pack(side="left", padx=6)
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Auto-scroll", variable=self.autoscroll).pack(side="left", padx=6)
        self.log_errors_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Errors && warnings only", variable=self.log_errors_only,
                        command=self._apply_log_filter).pack(side="left")
        logf = ttk.Frame(self.tab_log)
        logf.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.log = tk.Text(logf, height=8, wrap="word", state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("normal")
        self.log.tag_configure("info", foreground="#1565c0")
        self.log.tag_configure("warn", foreground="#b26a00")
        self.log.tag_configure("error", foreground="#c62828")
        self.log.tag_configure("ok", foreground="#2e7d32")
        self.log.tag_configure("head", foreground="#000000", background="#eceff1")
        self.log.tag_configure("ts", foreground="#888888")  # created last = highest priority

    def _make_tree(self, parent, cols):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings",
                            selectmode="extended", height=6)
        for key, title, width, stretch in cols:
            tree.heading(key, text=title, anchor="w")
            tree.column(key, width=width, stretch=stretch, anchor="w")
        sb = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        return tree

    def _apply_mode(self):
        video = self.mode.get() == "video"
        for w in (self.cb_container, self.cb_vq):
            w.configure(state="readonly" if video else "disabled")
        for w in (self.cb_af, self.cb_aq):
            w.configure(state="disabled" if video else "readonly")

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get() or None)
        if d:
            self.outdir.set(os.path.normpath(d))

    def _refresh_ffmpeg_label(self):
        if find_tool("ffmpeg"):
            self.ff_label.configure(text="ffmpeg: found (merging, MP3 conversion enabled)")
            self.btn_ffmpeg.state(["disabled"])
        else:
            self.ff_label.configure(
                text="ffmpeg: not found - needed for 1080p+ merging and MP3/FLAC conversion")
            if IS_WIN:
                self.btn_ffmpeg.state(["!disabled"])

    # --------------------------------------------------------------- log
    def _log(self, text, level=None):
        self.log.configure(state="normal")
        for line in str(text).rstrip("\n").split("\n"):
            lvl = level or classify(line)
            self.log.insert("end", time.strftime("%H:%M:%S  "), (lvl, "ts"))
            self.log.insert("end", line + "\n", (lvl,))
        extra = int(self.log.index("end-1c").split(".")[0]) - LOG_MAX_LINES
        if extra > 0:
            self.log.delete("1.0", f"{extra + 1}.0")
        if self.autoscroll.get():
            self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _save_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", initialfile="yt-dlp-gui-log.txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")])
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.log.get("1.0", "end-1c"))
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Could not save log:\n{e}")

    def _apply_log_filter(self):
        hide = self.log_errors_only.get()
        for tag in ("normal", "info"):
            self.log.tag_configure(tag, elide=hide)
        if self.autoscroll.get():
            self.log.see("end")

    # ------------------------------------------------- items / tab state
    def _sel(self, tree):
        return [int(i) for i in tree.selection()]

    def _ids_with(self, status):
        return [i["id"] for i in self.items.values() if i["status"] == status]

    def _new_item(self, url, cfg, status="queued", error="", title=""):
        self.seq += 1
        item = {"id": self.next_id, "seq": self.seq, "url": url, "cfg": dict(cfg),
                "status": status, "name": "", "title": title, "path": "", "idx": 0, "total": 0,
                "paused": False,
                "error": error, "tail": deque(maxlen=8), "progress": "", "time": ""}
        self.next_id += 1
        self.items[item["id"]] = item
        self._place(item)
        return item

    def _place(self, item):
        """Show the item in the tab matching its status."""
        iid = str(item["id"])
        st = item["status"]
        target = {"queued": self.queue_tree, "running": self.queue_tree,
                  "success": self.success_tree, "failed": self.failed_tree}[st]
        for t in (self.queue_tree, self.success_tree, self.failed_tree):
            if t is not target and t.exists(iid):
                t.delete(iid)
        if st == "queued":
            values = (item_label(item), "Paused" if item["paused"] else "Queued",
                      item["progress"] if item["paused"] else "")
        elif st == "running":
            values = (item_label(item), "Downloading", item["progress"])
        elif st == "success":
            values = (item_label(item), item["url"], item["time"])
        else:
            values = (item["url"], item["error"], item["time"])
        tags = ("bad",) if st == "failed" else ()
        if target.exists(iid):
            target.item(iid, values=values, tags=tags)
        else:
            target.insert("", "end" if target is self.queue_tree else 0,
                          iid=iid, values=values, tags=tags)
        self._update_counts()

    def _update_counts(self):
        n = {"queued": 0, "running": 0, "success": 0, "failed": 0}
        for it in self.items.values():
            n[it["status"]] += 1
        self.nb.tab(self.tab_queue, text=f"Queue ({n['queued'] + n['running']})")
        self.nb.tab(self.tab_success, text=f"Success ({n['success']})")
        self.nb.tab(self.tab_failed, text=f"Failed ({n['failed']})")

    def _remove_items(self, ids, status):
        removed = 0
        for iid in ids:
            it = self.items.get(iid)
            if it and it["status"] == status:
                del self.items[iid]
                for t in (self.queue_tree, self.success_tree, self.failed_tree):
                    if t.exists(str(iid)):
                        t.delete(str(iid))
                removed += 1
        if removed:
            self._update_counts()
            self._save_state()
            self._show_failed_details()

    def _show_failed_details(self, _event=None):
        sel = self._sel(self.failed_tree)
        text = ""
        if len(sel) == 1 and sel[0] in self.items:
            it = self.items[sel[0]]
            text = f"{it['url']}\n{it['error']}"
            if it["tail"]:
                text += "\n\nLast output:\n" + "\n".join(it["tail"])
        self.fail_detail.configure(state="normal")
        self.fail_detail.delete("1.0", "end")
        self.fail_detail.insert("end", text)
        self.fail_detail.configure(state="disabled")

    def _copy_failed_links(self):
        ids = self._sel(self.failed_tree) or self._ids_with("failed")
        links = [self.items[i]["url"] for i in ids if i in self.items]
        if links:
            self.clipboard_clear()
            self.clipboard_append("\n".join(links))
            self.status.configure(text=f"Copied {len(links)} link(s)")

    def _success_item(self):
        sel = self._sel(self.success_tree)
        return self.items.get(sel[0]) if sel else None

    def _open_success_file(self):
        it = self._success_item()
        if it:
            open_path(it["path"] if os.path.exists(it["path"] or "") else it["cfg"]["outdir"])

    def _open_success_folder(self):
        it = self._success_item()
        if it:
            folder = os.path.dirname(it["path"]) if it["path"] else ""
            open_path(folder if os.path.isdir(folder) else it["cfg"]["outdir"])

    # ------------------------------------------------- persisted state
    def _save_state(self):
        data = []
        for it in sorted(self.items.values(), key=lambda i: i["seq"]):
            if it["status"] in ("queued", "running", "failed"):
                data.append({"url": it["url"], "cfg": it["cfg"], "error": it["error"],
                             "title": it["title"],
                             "status": "failed" if it["status"] == "failed" else "queued"})
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
        except Exception:
            pass

    def _load_state(self):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        nq = nf = 0
        for d in data if isinstance(data, list) else []:
            if not isinstance(d, dict) or not isinstance(d.get("url"), str):
                continue
            cfg = dict(DEFAULTS)
            if isinstance(d.get("cfg"), dict):
                cfg.update(d["cfg"])
            failed = d.get("status") == "failed"
            self._new_item(d["url"], cfg, "failed" if failed else "queued",
                           str(d.get("error") or "") if failed else "",
                           str(d.get("title") or ""))
            nf += failed
            nq += not failed
        if nq or nf:
            self._log(f"Restored {nq} queued and {nf} failed item(s) from the last session."
                      + (" Press 'Start queue' to continue." if nq else ""), "head")

    # ----------------------------------------------------- update logic
    def check_updates(self):
        if self.updating:
            return
        self.ver_label.configure(text="Checking yt-dlp...")
        self.btn_check.state(["disabled"])
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self):
        exe = find_tool("yt-dlp")
        local = get_local_version(exe) if exe else None
        latest, err = None, None
        try:
            latest = get_latest_version()
        except Exception as e:
            err = str(e)
        self.q.put(("ver", local, latest, err))

    def update_ytdlp(self):
        if self.busy or self.updating or self.expanding:
            return
        self.updating = True
        self.btn_update.state(["disabled"])
        self.btn_check.state(["disabled"])
        self._log("Downloading latest yt-dlp...", "head")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        try:
            os.makedirs(BIN_DIR, exist_ok=True)
            dest = local_path("yt-dlp")
            tmp = dest + ".new"
            download_file(RELEASE_DL + ASSET, tmp,
                          lambda p, t: self.q.put(("uprogress", p, "Updating yt-dlp  " + t)))
            ok = verify_ytdlp(tmp)
            if ok is False:
                os.remove(tmp)
                raise RuntimeError("SHA-256 checksum mismatch - download discarded.")
            if ok is None:
                self.q.put(("log", "Could not verify checksum (continuing).", "warn"))
            os.replace(tmp, dest)
            if not IS_WIN:
                os.chmod(dest, 0o755)
            self.q.put(("log", "yt-dlp updated successfully.", "ok"))
        except Exception as e:
            self.q.put(("log", f"Update failed: {e}", "error"))
        self.q.put(("update_done",))

    def get_ffmpeg(self):
        if not IS_WIN or self.busy or self.updating:
            return
        if not messagebox.askyesno(
                APP_NAME, "Download ffmpeg (about 100-150 MB) into the app's bin folder?"):
            return
        self.updating = True
        self.btn_ffmpeg.state(["disabled"])
        self._log("Downloading ffmpeg...", "head")
        threading.Thread(target=self._ffmpeg_worker, daemon=True).start()

    def _ffmpeg_worker(self):
        zpath = os.path.join(BIN_DIR, "ffmpeg.zip.part")
        try:
            os.makedirs(BIN_DIR, exist_ok=True)
            download_file(FFMPEG_ZIP, zpath,
                          lambda p, t: self.q.put(("uprogress", p, "Downloading ffmpeg  " + t)))
            with zipfile.ZipFile(zpath) as z:
                for name in z.namelist():
                    base = name.rsplit("/", 1)[-1]
                    if "/bin/" in name and base in ("ffmpeg.exe", "ffprobe.exe"):
                        with z.open(name) as src, open(os.path.join(BIN_DIR, base), "wb") as dst:
                            shutil.copyfileobj(src, dst)
            self.q.put(("log", "ffmpeg installed.", "ok"))
        except Exception as e:
            self.q.put(("log", f"ffmpeg download failed: {e}", "error"))
        finally:
            try:
                os.remove(zpath)
            except OSError:
                pass
        self.q.put(("update_done",))

    # ------------------------------------------------------ queue logic
    def _settings(self):
        return {
            "outdir": self.outdir.get().strip(),
            "mode": self.mode.get(),
            "container": self.container.get(),
            "vq": self.vq.get(),
            "aformat": self.aformat.get(),
            "aq": self.aq.get(),
            "playlist": self.playlist.get(),
            "embed": self.embed.get(),
            "subs": self.subs.get(),
            "langs": self.langs.get(),
            "cookies": self.cookies.get(),
            "extra": self.extra.get(),
        }

    def add_to_queue(self):
        urls = []
        for u in re.split(r"\s+", self.url_text.get("1.0", "end").strip()):
            if u and u not in urls:
                urls.append(u)
        if not urls:
            messagebox.showinfo(APP_NAME, "Paste at least one link.")
            return
        c = self._settings()
        try:
            os.makedirs(c["outdir"], exist_ok=True)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Cannot use output folder:\n{e}")
            return
        save_config(c)

        active = {it["url"] for it in self.items.values() if it["status"] in ("queued", "running")}
        added = fetching = 0
        for u in urls:
            if c["playlist"]:
                # read the playlist first, then queue every video as its own item
                fetching += 1
                self.expanding += 1
                threading.Thread(target=self._expand_worker, args=(u, dict(c)),
                                 daemon=True).start()
                continue
            if u in active:
                self._log(f"Skipped (already in queue): {u}", "warn")
                continue
            self._new_item(u, c)
            added += 1
        self.url_text.delete("1.0", "end")
        if fetching:
            self._log(f"Reading playlist contents for {fetching} link(s)...", "head")
            self.status.configure(text="Reading playlist contents...")
            self.btn_update.state(["disabled"])
        if added:
            self._log(f"Added {added} link(s) to the queue.", "head")
            self._save_state()
            self.nb.select(self.tab_queue)
            self._start_next()          # only acts if the queue is already running
            if not fetching:
                self._hint_idle()

    def _expand_worker(self, url, cfg):
        """List every video of a playlist (fast, no downloading)."""
        entries, err = [], ""
        exe = find_tool("yt-dlp")
        if not exe:
            err = "yt-dlp is not installed"
        else:
            args = [exe, "--flat-playlist", "--yes-playlist", "--no-warnings",
                    "--encoding", "utf-8",
                    "--print", "%(webpage_url,url)s\t%(title)s\t%(playlist_title)s"]
            if cfg["cookies"] != "none":
                args += ["--cookies-from-browser", cfg["cookies"]]
            args += ["--", url]
            try:
                r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=900, creationflags=NOFLAG,
                                   stdin=subprocess.DEVNULL)
                for line in r.stdout.splitlines():
                    parts = (line.split("\t") + ["", ""])[:3]
                    eurl, title, ptitle = (p.strip() for p in parts)
                    if eurl and eurl != "NA":
                        entries.append((eurl, "" if title == "NA" else title,
                                        "" if ptitle == "NA" else ptitle))
                if r.returncode != 0:
                    errs = [l for l in r.stderr.splitlines() if l.startswith("ERROR")]
                    err = re.sub(r"^ERROR:\s*", "", errs[-1]) if errs else f"exit code {r.returncode}"
            except Exception as e:
                err = str(e)
        self.q.put(("expanded", url, cfg, entries, err))

    def _on_expanded(self, url, cfg, entries, err):
        self.expanding -= 1
        if not entries:
            # could not list it: let yt-dlp handle the whole playlist as one queue item
            self._log(f"Could not read playlist ({err or 'no entries'}); "
                      f"queued the link as a single item.", "warn")
            self._new_item(url, cfg)
        else:
            if err:
                self._log(f"Playlist listing was incomplete: {err}", "warn")
            n = len(entries)
            width = max(2, len(str(n)))
            ptitle = entries[0][2] if n > 1 else ""
            folder = safe_name(ptitle) if ptitle else ""
            active = {it["url"] for it in self.items.values()
                      if it["status"] in ("queued", "running")}
            added = 0
            for i, (eurl, title, _pt) in enumerate(entries, 1):
                if eurl in active:
                    continue
                active.add(eurl)
                c = dict(cfg, playlist=False)
                if n > 1:
                    if folder:
                        c["outdir"] = os.path.join(cfg["outdir"], folder)
                    c["prefix"] = f"{i:0{width}d} - "
                self._new_item(eurl, c, title=title)
                added += 1
            self._log(f"Playlist{' ' + repr(ptitle) if ptitle else ''}: "
                      f"queued {added} of {n} item(s).", "head")
        self._save_state()
        self.nb.select(self.tab_queue)
        self._start_next()          # only acts if the queue is already running
        if not self.expanding:
            self._hint_idle()
            if not self.busy:
                self._on_version(self.local_ver, self.latest_ver, None)

    def start_queue(self):
        """Start the queue, or resume it after a pause."""
        if not self.want_run and not self.paused:
            self.run_ok = self.run_fail = 0
        self.paused = False
        self.want_run = True
        if self.updating:
            self.status.configure(text="Waiting for update to finish...")
        self._start_next()

    def pause_queue(self):
        """Stop the current download (partial file is kept) and hold the queue."""
        if not self.want_run:
            return
        self.want_run = False
        self.paused = True
        if self.busy:
            self.pausing = True
            kill_tree(self.proc)
            self.status.configure(text="Pausing...")
        else:
            self.status.configure(
                text=f"Paused - {len(self._ids_with('queued'))} in queue (press Resume)")

    def _hint_idle(self):
        """Tell the user how to begin when items are waiting and nothing is running."""
        if self.want_run or self.busy or self.updating:
            return
        n = len(self._ids_with("queued"))
        if n:
            self.status.configure(
                text=f"Paused - {n} in queue (press Resume)" if self.paused
                else f"{n} item(s) in queue - press 'Start queue'")

    def _update_buttons(self):
        has_queued = any(i["status"] == "queued" for i in self.items.values())
        states = (not self.want_run and not self.paused and has_queued and not self.updating,
                  self.want_run, self.paused)
        if states == self._btn_states:
            return
        self._btn_states = states
        for btn, on in zip((self.btn_start, self.btn_pause, self.btn_resume), states):
            btn.state(["!disabled" if on else "disabled"])

    def _start_next(self):
        if self.busy or self.updating or not self.want_run:
            return
        queued = [i for i in self.items.values() if i["status"] == "queued"]
        if not queued:
            if self.expanding:   # more items are about to arrive
                self.status.configure(text="Reading playlist contents...")
                return
            self._finish_run()
            return
        exe = find_tool("yt-dlp")
        if not exe:
            self.want_run = False
            messagebox.showinfo(APP_NAME, "yt-dlp is not installed yet. Click 'Download yt-dlp' first, "
                                          "then press 'Start queue'.")
            return
        item = min(queued, key=lambda i: i["seq"])
        c = item["cfg"]
        try:
            os.makedirs(c["outdir"], exist_ok=True)
        except Exception as e:
            item.update(status="failed", error=f"Cannot use output folder: {e}",
                        time=time.strftime("%H:%M"))
            self.run_fail += 1
            self._place(item)
            self._save_state()
            self._start_next()
            return

        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg and not self.warned_ffmpeg:
            self.warned_ffmpeg = True
            self._log("ffmpeg not found - using single-file formats only "
                      "(max ~720p on YouTube, no MP3 conversion).", "warn")
        args = build_args(c, c["outdir"], ffmpeg)

        item.update(status="running", error="", progress="starting...", idx=0, total=0,
                    paused=False)
        item["tail"].clear()
        self._place(item)
        self.busy = True
        self.cancelling = False
        self.btn_cancel.state(["!disabled"])
        self.btn_update.state(["disabled"])
        self.progress["value"] = 0
        self.status.configure(text=f"Downloading: {item['url']}")
        self._log(f"=== {item['url']}", "head")
        threading.Thread(target=self._download_worker, args=(item["id"], exe, args, item["url"]),
                         daemon=True).start()

    def _download_worker(self, iid, exe, args, url):
        rc = -1
        try:
            # "--" stops a pasted link starting with "-" from being read as an option
            self.proc = subprocess.Popen(
                [exe] + args + ["--", url], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1, creationflags=NOFLAG)
            for line in self.proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                if line.startswith("[download]") and "ETA" in line:
                    m = RE_PROGRESS.search(line)
                    if m:
                        sp, eta = RE_SPEED.search(line), RE_ETA.search(line)
                        txt = f"{m.group(1)}%"
                        if sp:
                            txt += f"  {sp.group(1)}"
                        if eta:
                            txt += f"  ETA {eta.group(1)}"
                        self.q.put(("progress", iid, float(m.group(1)), txt))
                else:
                    self.q.put(("line", iid, line))
            rc = self.proc.wait()
        except Exception as e:
            self.q.put(("line", iid, f"ERROR: {e}"))
        self.q.put(("item_done", iid, rc))

    def _on_line(self, iid, line):
        lvl = classify(line)
        item = self.items.get(iid)
        if item:
            item["tail"].append(line)
            m = RE_ITEM.search(line)
            if m:
                item["idx"], item["total"] = int(m.group(1)), int(m.group(2))
            path = None
            m = RE_MERGE.match(line) or RE_HAVE.match(line)
            if m:
                path = m.group(1)
            else:
                m = RE_DEST.match(line)
                if m and not RE_FRAG.search(m.group(1)):
                    path = m.group(1)
            if path:
                item["path"] = path.strip()
                item["name"] = os.path.basename(item["path"])
            if m or lvl == "error":
                if lvl == "error":
                    item["error"] = re.sub(r"^ERROR:\s*", "", line.strip())
                if self.queue_tree.exists(str(iid)):
                    self.queue_tree.set(str(iid), "name", item_label(item))
        self._log(line, lvl)

    def _on_item_done(self, iid, rc):
        item = self.items.get(iid)
        self.busy = False
        self.proc = None
        cancelled, self.cancelling = self.cancelling, False
        pausing, self.pausing = self.pausing, False
        if item:
            item["time"] = time.strftime("%H:%M")
            item["progress"] = ""
            if cancelled:
                item.update(status="failed", error="Cancelled")
                self.run_fail += 1
                self._log("Cancelled.", "warn")
            elif rc == 0:
                item.update(status="success", error="")
                self.run_ok += 1
                self._log(f"Done: {item['name'] or item['url']}", "ok")
            elif pausing:
                # back to the front of the queue; yt-dlp continues from the partial file
                first = min([i["seq"] for i in self.items.values() if i["status"] == "queued"]
                            + [item["seq"]])
                item.update(status="queued", seq=first - 1, paused=True, progress="partial kept",
                            error="")
                self._log("Paused - the partial download is kept and continues on Resume.",
                          "warn")
            else:
                if not item["error"]:
                    item["error"] = f"yt-dlp exited with code {rc}"
                item["status"] = "failed"
                self.run_fail += 1
                self._log(f"Failed: {item['error']}", "error")
            self._place(item)
            if item["status"] == "failed":
                self._show_failed_details()
        self._save_state()
        if self.want_run:
            self._start_next()
        else:
            self.btn_cancel.state(["disabled"])
            self.progress["value"] = 0
            waiting = len(self._ids_with("queued"))
            if self.paused:
                text = f"Paused - {waiting} in queue (press Resume)"
            else:
                text = "Stopped" + (f" - {waiting} still queued (press 'Start queue')"
                                    if waiting else "")
            self.status.configure(text=text)
            self._on_version(self.local_ver, self.latest_ver, None)

    def _finish_run(self):
        self.want_run = False
        self.warned_ffmpeg = False
        self.btn_cancel.state(["disabled"])
        if self.run_ok or self.run_fail:
            text = f"Queue finished - {self.run_ok} succeeded, {self.run_fail} failed"
            self._log(text, "ok" if not self.run_fail else "warn")
            self.progress["value"] = 100 if not self.run_fail else 0
        else:
            text = "Ready"
            self.progress["value"] = 0
        self.status.configure(text=text)
        self._on_version(self.local_ver, self.latest_ver, None)

    def retry_failed(self, everything):
        ids = self._ids_with("failed") if everything else self._sel(self.failed_tree)
        ids = [i for i in ids if i in self.items and self.items[i]["status"] == "failed"]
        if not ids:
            messagebox.showinfo(APP_NAME, "Select one or more failed items first "
                                          "(or use 'Retry all').")
            return
        for iid in sorted(ids, key=lambda i: self.items[i]["seq"]):
            it = self.items[iid]
            self.seq += 1
            it.update(seq=self.seq, status="queued", error="", progress="", name="",
                      path="", idx=0, total=0)
            it["tail"].clear()
            self._place(it)
        self._log(f"Retrying {len(ids)} item(s).", "head")
        self._show_failed_details()
        self._save_state()
        self.nb.select(self.tab_queue)
        self._start_next()          # only acts if the queue is already running
        self._hint_idle()

    def cancel_download(self):
        if self.busy:
            self.cancelling = True
            self.pausing = False
            self.paused = False
            self.want_run = False   # stop the whole queue; remaining items stay queued
            kill_tree(self.proc)
            self.status.configure(text="Cancelling...")

    # --------------------------------------------------------- event loop
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1], msg[2] if len(msg) > 2 else None)
                elif kind == "line":
                    self._on_line(msg[1], msg[2])
                elif kind == "progress":
                    _, iid, pct, txt = msg
                    item = self.items.get(iid)
                    if item and item["status"] == "running":
                        item["progress"] = txt
                        if self.queue_tree.exists(str(iid)):
                            self.queue_tree.set(str(iid), "progress", txt)
                        self.progress["value"] = pct
                        self.status.configure(text=f"Downloading: {txt}")
                elif kind == "uprogress":
                    self.progress["value"] = msg[1]
                    self.status.configure(text=msg[2])
                elif kind == "expanded":
                    self._on_expanded(*msg[1:])
                elif kind == "item_done":
                    self._on_item_done(msg[1], msg[2])
                elif kind == "ver":
                    self._on_version(*msg[1:])
                elif kind == "update_done":
                    self.updating = False
                    self.progress["value"] = 0
                    self.status.configure(text="Ready")
                    self._refresh_ffmpeg_label()
                    self.check_updates()
                    self._start_next()   # resumes a queue that was waiting for the update
        except queue.Empty:
            pass
        self._update_buttons()
        self.after(100, self._poll)

    def _on_version(self, local, latest, err):
        self.local_ver, self.latest_ver = local, latest
        self.btn_check.state(["!disabled"])
        if not local:
            self.ver_label.configure(text="yt-dlp: not installed")
            self.btn_update.configure(text="Download yt-dlp")
            self.btn_update.state(["!disabled"])
        elif err or not latest:
            self.ver_label.configure(text=f"yt-dlp {local}  (could not check for updates)")
            self.btn_update.configure(text="Update now")
            self.btn_update.state(["!disabled"])
        elif local == latest:
            self.ver_label.configure(text=f"yt-dlp {local}  - up to date")
            self.btn_update.configure(text="Update now")
            self.btn_update.state(["disabled"])
        else:
            self.ver_label.configure(text=f"yt-dlp {local}  - update available: {latest}")
            self.btn_update.configure(text=f"Update to {latest}")
            self.btn_update.state(["!disabled"])
        if self.busy or self.updating or self.expanding:
            self.btn_update.state(["disabled"])

    def _on_close(self):
        if self.busy:
            if not messagebox.askyesno(APP_NAME, "A download is running. Cancel it and quit?"):
                return
            self.want_run = False
            kill_tree(self.proc)
        save_config(self._settings())
        self._save_state()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
