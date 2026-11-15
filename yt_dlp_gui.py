"""
yt-dlp GUI - a lightweight Tkinter front-end for https://github.com/yt-dlp/yt-dlp

* Pure Python standard library (Tkinter) - nothing to pip install.
* Manages the official yt-dlp standalone binary itself (download / update).
* Works with every site yt-dlp supports, not only YouTube.
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
import tkinter as tk
import urllib.request
import zipfile
from tkinter import filedialog, messagebox, ttk

APP_NAME = "yt-dlp GUI"
IS_WIN = sys.platform.startswith("win")
NOFLAG = subprocess.CREATE_NO_WINDOW if IS_WIN else 0

APP_DIR = os.path.dirname(
    os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
)
BIN_DIR = os.path.join(APP_DIR, "bin")
CONFIG_PATH = os.path.join(APP_DIR, "settings.json")

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
                           timeout=60, creationflags=NOFLAG)
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
                               capture_output=True, creationflags=NOFLAG)
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


def build_args(c, outdir, ffmpeg):
    """Turn the UI settings dict into yt-dlp arguments."""
    a = ["--newline", "--encoding", "utf-8", "--no-colors", "-P", outdir]
    if ffmpeg and os.path.dirname(ffmpeg) == BIN_DIR:
        a += ["--ffmpeg-location", BIN_DIR]

    if c["playlist"]:
        a += ["--yes-playlist", "-o",
              "%(playlist_title&{}/|)s%(playlist_index&{} - |)s%(title).150B.%(ext)s"]
    else:
        a += ["--no-playlist", "-o", "%(title).150B.%(ext)s"]

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
        self.geometry("800x700")
        self.minsize(700, 600)
        self.cfg = load_config()
        self.q = queue.Queue()
        self.proc = None
        self.cancelled = False
        self.busy = False        # downloading
        self.updating = False
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
        self.url_text = tk.Text(urls, height=4, wrap="none", undo=True)
        self.url_text.pack(fill="x", padx=6, pady=6)

        # --- options
        opt = ttk.LabelFrame(root, text="Format & quality")
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
        ttk.Button(out, text="Open", command=self._open_folder).pack(side="left", padx=(4, 0))

        # --- actions
        act = ttk.Frame(root)
        act.pack(fill="x", **pad)
        self.btn_dl = ttk.Button(act, text="Download", command=self.start_download)
        self.btn_dl.pack(side="left")
        self.btn_cancel = ttk.Button(act, text="Cancel", command=self.cancel_download,
                                     state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        self.status = ttk.Label(act, text="Ready")
        self.status.pack(side="left", padx=10)

        self.progress = ttk.Progressbar(root, maximum=100)
        self.progress.pack(fill="x", **pad)

        # --- log
        logf = ttk.Frame(root)
        logf.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log = tk.Text(logf, height=8, wrap="word", state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

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

    def _open_folder(self):
        d = self.outdir.get()
        if not os.path.isdir(d):
            return
        if IS_WIN:
            os.startfile(d)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", d])
        else:
            subprocess.Popen(["xdg-open", d])

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _refresh_ffmpeg_label(self):
        if find_tool("ffmpeg"):
            self.ff_label.configure(text="ffmpeg: found (merging, MP3 conversion enabled)")
            self.btn_ffmpeg.state(["disabled"])
        else:
            self.ff_label.configure(
                text="ffmpeg: not found - needed for 1080p+ merging and MP3/FLAC conversion")
            if IS_WIN:
                self.btn_ffmpeg.state(["!disabled"])

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
        if self.busy or self.updating:
            return
        self.updating = True
        self.btn_update.state(["disabled"])
        self.btn_check.state(["disabled"])
        self._log("Downloading latest yt-dlp...\n")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        try:
            os.makedirs(BIN_DIR, exist_ok=True)
            dest = local_path("yt-dlp")
            tmp = dest + ".new"
            download_file(RELEASE_DL + ASSET, tmp,
                          lambda p, t: self.q.put(("progress", p, "Updating yt-dlp  " + t)))
            ok = verify_ytdlp(tmp)
            if ok is False:
                os.remove(tmp)
                raise RuntimeError("SHA-256 checksum mismatch - download discarded.")
            if ok is None:
                self.q.put(("log", "Note: could not verify checksum (continuing).\n"))
            os.replace(tmp, dest)
            if not IS_WIN:
                os.chmod(dest, 0o755)
            self.q.put(("log", "yt-dlp updated successfully.\n"))
        except Exception as e:
            self.q.put(("log", f"Update failed: {e}\n"))
        self.q.put(("update_done",))

    def get_ffmpeg(self):
        if not IS_WIN or self.busy or self.updating:
            return
        if not messagebox.askyesno(
                APP_NAME, "Download ffmpeg (about 100-150 MB) into the app's bin folder?"):
            return
        self.updating = True
        self.btn_ffmpeg.state(["disabled"])
        self._log("Downloading ffmpeg...\n")
        threading.Thread(target=self._ffmpeg_worker, daemon=True).start()

    def _ffmpeg_worker(self):
        zpath = os.path.join(BIN_DIR, "ffmpeg.zip.part")
        try:
            os.makedirs(BIN_DIR, exist_ok=True)
            download_file(FFMPEG_ZIP, zpath,
                          lambda p, t: self.q.put(("progress", p, "Downloading ffmpeg  " + t)))
            with zipfile.ZipFile(zpath) as z:
                for name in z.namelist():
                    base = name.rsplit("/", 1)[-1]
                    if "/bin/" in name and base in ("ffmpeg.exe", "ffprobe.exe"):
                        with z.open(name) as src, open(os.path.join(BIN_DIR, base), "wb") as dst:
                            shutil.copyfileobj(src, dst)
            self.q.put(("log", "ffmpeg installed.\n"))
        except Exception as e:
            self.q.put(("log", f"ffmpeg download failed: {e}\n"))
        finally:
            try:
                os.remove(zpath)
            except OSError:
                pass
        self.q.put(("update_done",))

    # --------------------------------------------------- download logic
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

    def start_download(self):
        if self.busy or self.updating:
            return
        exe = find_tool("yt-dlp")
        if not exe:
            messagebox.showinfo(APP_NAME, "yt-dlp is not installed yet. Click 'Update now' first.")
            return
        urls = [u for u in re.split(r"\s+", self.url_text.get("1.0", "end").strip()) if u]
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

        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            self._log("Warning: ffmpeg not found - using single-file formats only "
                      "(max ~720p on YouTube, no MP3 conversion).\n")
        args = build_args(c, c["outdir"], ffmpeg)

        self.busy, self.cancelled = True, False
        self.btn_dl.state(["disabled"])
        self.btn_cancel.state(["!disabled"])
        self.btn_update.state(["disabled"])
        self.progress["value"] = 0
        threading.Thread(target=self._download_worker, args=(exe, args, urls),
                         daemon=True).start()

    def _download_worker(self, exe, args, urls):
        failed = 0
        for i, url in enumerate(urls, 1):
            if self.cancelled:
                break
            self.q.put(("log", f"\n=== [{i}/{len(urls)}] {url}\n"))
            self.q.put(("status", f"Item {i}/{len(urls)}: starting..."))
            try:
                self.proc = subprocess.Popen(
                    [exe] + args + [url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=NOFLAG)
                for line in self.proc.stdout:
                    line = line.rstrip()
                    if not line:
                        continue
                    if line.startswith("[download]") and "ETA" in line:
                        m = re.search(r"(\d+(?:\.\d+)?)%", line)
                        sp = re.search(r"at\s+(\S+)", line)
                        eta = re.search(r"ETA\s+(\S+)", line)
                        if m:
                            txt = f"Item {i}/{len(urls)}: {m.group(1)}%"
                            if sp:
                                txt += f"  {sp.group(1)}"
                            if eta:
                                txt += f"  ETA {eta.group(1)}"
                            self.q.put(("progress", float(m.group(1)), txt))
                    else:
                        self.q.put(("log", line + "\n"))
                rc = self.proc.wait()
            except Exception as e:
                self.q.put(("log", f"Error: {e}\n"))
                rc = -1
            if rc != 0 and not self.cancelled:
                failed += 1
        self.q.put(("done", failed))

    def cancel_download(self):
        if self.busy:
            self.cancelled = True
            kill_tree(self.proc)
            self.status.configure(text="Cancelling...")

    # --------------------------------------------------------- event loop
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "status":
                    self.status.configure(text=msg[1])
                elif kind == "progress":
                    self.progress["value"] = msg[1]
                    self.status.configure(text=msg[2])
                elif kind == "ver":
                    self._on_version(*msg[1:])
                elif kind == "update_done":
                    self.updating = False
                    self.progress["value"] = 0
                    self.status.configure(text="Ready")
                    self._refresh_ffmpeg_label()
                    self.check_updates()
                elif kind == "done":
                    self.busy = False
                    self.btn_dl.state(["!disabled"])
                    self.btn_cancel.state(["disabled"])
                    self._on_version(self.local_ver, self.latest_ver, None)
                    failed = msg[1]
                    if self.cancelled:
                        text = "Cancelled"
                    elif failed:
                        text = f"Finished - {failed} failed (see log)"
                    else:
                        text = "Finished"
                        self.progress["value"] = 100
                    self.status.configure(text=text)
        except queue.Empty:
            pass
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
        if self.busy or self.updating:
            self.btn_update.state(["disabled"])

    def _on_close(self):
        if self.busy:
            if not messagebox.askyesno(APP_NAME, "A download is running. Cancel it and quit?"):
                return
            self.cancelled = True
            kill_tree(self.proc)
        save_config(self._settings())
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
