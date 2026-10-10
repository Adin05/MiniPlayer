"""
MiniPlayer - Desktop Video Player (Tkinter + VLC + ffmpeg)
Fitur:
1. Scroll up/down untuk next/prev video (aktif di mana saja selama window aktif + global hook)
2. Open folder & play semua video
3. Ingat folder terakhir + posisi play terakhir (auto-resume)
4. Seek (slider + tombol +-5s + panah kiri/kanan)
5. Volume (slider + mute)
6. Cut clip (ffmpeg) & Share ke YouTube
7. Random play (acak urutan / next acak)
Jalankan:  pip install -r requirements.txt
           python main.py
Butuh: VLC terinstal + ffmpeg terinstal (sudah ada di PC ini).
"""
import ctypes
import json
import os
import queue
import random
import re
import subprocess
import sys
import shutil
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
import webbrowser

try:
    import vlc
except ImportError:
    print("ERROR: install dulu: pip install python-vlc")
    sys.exit(1)

if getattr(sys, "frozen", False):
    # Dijalankan sebagai .exe PyInstaller → config di samping file exe
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"


def res_path(name: str) -> Path:
    """Path resource: dari bundle PyInstaller (_MEIPASS) bila frozen, else samping source/exe."""
    try:
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base) / name
    except Exception:
        pass
    return APP_DIR / name
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
              ".webm", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts", ".ogv"}

CLIP_DIR_DEFAULT = Path.home() / "Videos" / "MiniPlayerClips"


def parse_time_to_sec(s: str) -> float:
    """Terima '90', '1:30', '01:02:30.5' -> detik (float)."""
    s = str(s).strip().replace(",", ".")
    if not s:
        raise ValueError("waktu kosong")
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        elif len(parts) == 2:
            m, sec = float(parts[0]), float(parts[1])
            return m * 60 + sec
        elif len(parts) == 3:
            h, m, sec = float(parts[0]), float(parts[1]), float(parts[2])
            return h * 3600 + m * 60 + sec
        else:
            raise ValueError("format salah")
    except ValueError:
        raise ValueError(f"Format waktu tidak valid: '{s}'. Contoh: 75 / 1:15 / 01:02:30")


def format_ms(ms: int) -> str:
    if ms is None or ms < 0:
        ms = 0
    s = ms // 1000
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def load_config() -> dict:
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print("gagal simpan config:", e)


class MiniPlayer:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MiniPlayer 🎬")
        try:
            _ico = res_path("icon.ico")
            if _ico.exists():
                self.root.iconbitmap(str(_ico))
        except Exception:
            pass
        self.root.geometry("1150x750")
        self.root.minsize(960, 620)

        self.cfg = load_config()
        self.playlist: list[Path] = []
        self.playlist_folder: Path | None = None
        self.index = -1
        self.is_seeking = False
        self.last_clip: Path | None = None
        self._timer_id = None

        # --- Opsi baru ---
        self.random_mode: bool = bool(self.cfg.get("random_mode", False))
        self.resume_enabled: bool = bool(self.cfg.get("resume", True))
        self.scroll_enabled: bool = bool(self.cfg.get("scroll_global", True))
        self.seek_step: int = int(self.cfg.get("seek_step", 5) or 5)
        self.playlist_visible: bool = bool(self.cfg.get("playlist_visible", True))
        self.click_toggle: bool = bool(self.cfg.get("click_toggle", True))
        self.aspect_ratio: str = str(self.cfg.get("aspect_ratio", "") or "")  # "" = Default/otomatis
        self._fs = False  # mode fullscreen video-saja
        self._fs_pl_was_visible = True
        self._last_click_toggle = 0.0
        self._lbutton_down = False  # status tombol kiri mouse (untuk polling klik)
        self._hook_shown = None  # cache teks indikator hook
        self._cmd_queue: queue.Queue = queue.Queue()  # perintah dari hook mouse (antar-thread)
        self._history: list[int] = []
        self._wheel_last = 0.0  # debounce scroll (detik)
        self._pending_resume_ms: int = int(self.cfg.get("last_time", 0) or 0)
        self._resume_done = False
        self._global_listener = None
        self._tick_count = 0

        # --- VLC ---
        args = ["--no-qt-bgcone", "--no-video-title-show"]
        self.vlc_instance = vlc.Instance(*args)
        self.player: vlc.MediaPlayer = self.vlc_instance.media_player_new()
        self.player.audio_set_volume(int(self.cfg.get("volume", 80)))
        events = self.player.event_manager()
        try:
            events.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_media_end)
        except Exception:
            pass

        self._build_ui()
        self._bind_shortcuts()
        self._sync_option_ui()

        # Auto-load: utamakan file terakhir bila masih ada (ingat last play),
        # fallback ke folder terakhir.
        start_folder = ""
        last_file = self.cfg.get("last_file", "")
        if last_file and Path(last_file).is_file():
            start_folder = str(Path(last_file).parent)
        else:
            start_folder = self.cfg.get("last_folder", "")
        if start_folder and Path(start_folder).is_dir():
            files = self._scan_videos(Path(start_folder))
            if files:
                self.playlist = files
                self.playlist_folder = Path(start_folder)
                self._refresh_playlist_ui()
                # cari index file terakhir
                start_idx = 0
                for k, p in enumerate(files):
                    if str(p) == last_file:
                        start_idx = k
                        break
                self.play_index(start_idx)
                # siapkan resume posisi (dikerjakan setelah player jalan)
                if (self.resume_enabled and self._pending_resume_ms > 5000
                        and str(files[start_idx]) == last_file):
                    self.root.after(800, self._do_resume)
                    self.status(f"Memuat terakhir: {files[start_idx].name} — lanjut dari {format_ms(self._pending_resume_ms)}")
                else:
                    self.status(f"Memuat folder terakhir: {start_folder} ({len(files)} video)")
            else:
                self.status(f"Folder terakhir kosong: {start_folder}")
        else:
            self.status("Klik 📂 Buka Folder untuk mulai.")

        if self.scroll_enabled or self.click_toggle:
            self._update_global_listener()

        self._restore_window()
        self._tick()
        self._click_poll()  # polling klik-kiri di video (jalan di thread utama)

    # ============ UI ============
    def _restore_window(self):
        """Kembalikan ukuran/posisi/maximize terakhir. Kalau belum ada, % resolusi layar."""
        try:
            if bool(self.cfg.get("window_zoomed", False)):
                try:
                    self.root.state("zoomed")
                    return
                except Exception:
                    pass
            geo = str(self.cfg.get("window_geo", "") or "")
            if re.match(r"^\d+x\d+([+-]\d+){2}$", geo):
                self.root.geometry(geo)
            else:
                self._center_window()  # ukuran = % resolusi layar
        except Exception:
            pass

    # Persentase ukuran default terhadap resolusi layar
    DEF_W_PCT = 0.80
    DEF_H_PCT = 0.85

    def _default_size(self):
        """Hitung ukuran default sebagai persentase resolusi layar aktual."""
        try:
            self.root.update_idletasks()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            w = int(sw * self.DEF_W_PCT)
            h = int(sh * self.DEF_H_PCT)
            w = max(960, min(w, sw - 40))
            h = max(620, min(h, sh - 80))  # sisakan ruang taskbar
            return w, h
        except Exception:
            return 1150, 750

    def _center_window(self, w=None, h=None):
        try:
            if w is None or h is None:
                w, h = self._default_size()
            self.root.update_idletasks()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2 - 20)
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            try:
                self.root.geometry(f"{w}x{h}")
            except Exception:
                pass

    def _build_ui(self):
        # (tombol folder pindah ke baris transport; info folder di status bar)

        # Popup pengaturan (dibuka via tombol ⚙ di baris transport)
        pop = tk.Toplevel(self.root)
        pop.title("⚙ Pengaturan")
        pop.resizable(False, False)
        pop.withdraw()
        try:
            pop.transient(self.root)
        except Exception:
            pass
        pop.protocol("WM_DELETE_WINDOW", self.hide_settings_popup)
        self.set_popup = pop
        self._popup_was_open = False

        rowA = ttk.Frame(pop, padding=6)
        rowA.pack(fill=tk.X)
        self.scroll_var = tk.BooleanVar(value=self.scroll_enabled)
        self.resume_var = tk.BooleanVar(value=self.resume_enabled)
        self.random_var = tk.BooleanVar(value=self.random_mode)
        ttk.Checkbutton(rowA, text="🖱 Scroll = ganti video (di mana saja)",
                        variable=self.scroll_var, command=self._on_toggle_scroll).pack(side=tk.LEFT, padx=2)
        ttk.Checkbutton(rowA, text="↺ Resume posisi terakhir",
                        variable=self.resume_var, command=self._on_toggle_resume).pack(side=tk.LEFT, padx=10)
        ttk.Checkbutton(rowA, text="🔀 Random play",
                        variable=self.random_var, command=self._on_toggle_random).pack(side=tk.LEFT, padx=2)
        ttk.Button(rowA, text="🎲 Acak urutan", command=self.shuffle_now).pack(side=tk.LEFT, padx=10)
        self.lbl_resume = ttk.Label(rowA, text="", foreground="gray")
        self.lbl_resume.pack(side=tk.RIGHT, padx=4)

        rowB = ttk.Frame(pop, padding=6)
        rowB.pack(fill=tk.X)
        self.click_var = tk.BooleanVar(value=self.click_toggle)
        ttk.Checkbutton(rowB, text="👆 Klik video = play/pause",
                        variable=self.click_var, command=self._on_toggle_click).pack(side=tk.LEFT, padx=2)
        ttk.Label(rowB, text="Rasio:").pack(side=tk.LEFT, padx=(10, 0))
        self.aspect_var = tk.StringVar(value=self.aspect_ratio or "Default")
        self.aspect_box = ttk.Combobox(rowB, textvariable=self.aspect_var, width=9,
                                       values=["Default", "16:9", "16:10", "21:9", "4:3", "3:2", "1:1"],
                                       state="readonly")
        self.aspect_box.pack(side=tk.LEFT, padx=4)
        self.aspect_box.bind("<<ComboboxSelected>>", self._on_aspect_change)
        ttk.Label(rowB, text="Lompat:").pack(side=tk.LEFT, padx=(10, 0))
        self.step_var = tk.StringVar(value=str(self.seek_step))
        self.step_box = ttk.Combobox(rowB, textvariable=self.step_var, width=3,
                                     values=["1", "2", "3", "5", "10", "15", "30", "60"],
                                     state="readonly")
        self.step_box.pack(side=tk.LEFT, padx=3)
        self.step_box.bind("<<ComboboxSelected>>", self._on_step_change)
        ttk.Label(rowB, text="dtk").pack(side=tk.LEFT)
        self.lbl_hook = ttk.Label(rowB, text="hook: ...", foreground="gray")
        self.lbl_hook.pack(side=tk.RIGHT, padx=4)
        self._refresh_step_buttons()

        # Area utama: video kiri + playlist kanan
        main = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        self.main_pane = main

        left = ttk.Frame(main)
        main.add(left, weight=3)

        self.video_frame = tk.Frame(left, bg="black")
        self.video_frame.pack(fill=tk.BOTH, expand=True)
        hint = tk.Label(self.video_frame, text="Klik 📂 Buka Folder untuk mulai\n\nScroll mouse / tombol ↑/↓ di mana saja = ganti video\nKlik video = play/pause • Space = play/pause • ←/→ = seek",
                        fg="white", bg="black", justify=tk.CENTER, font=("Segoe UI", 11))
        hint.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self._hint_label = hint

        # Info file + waktu
        info = ttk.Frame(left)
        info.pack(fill=tk.X, pady=(2, 0))
        self.info_bar = info
        self.lbl_title = ttk.Label(info, text="Tidak ada video", font=("Segoe UI", 10, "bold"))
        self.lbl_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.time_var = tk.StringVar(value="00:00 / 00:00")
        self.lbl_time = ttk.Label(info, textvariable=self.time_var)
        self.lbl_time.pack(side=tk.RIGHT)

        # Seek slider
        seek_row = ttk.Frame(left)
        seek_row.pack(fill=tk.X)
        self.seek_row = seek_row
        self.btn_back = ttk.Button(seek_row, width=8, command=self.seek_back)
        self.btn_back.pack(side=tk.LEFT)
        self.seek_var = tk.DoubleVar(value=0)
        self.seek = ttk.Scale(seek_row, from_=0, to=1000, variable=self.seek_var, orient=tk.HORIZONTAL)
        self.seek.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.seek.bind("<ButtonPress-1>", self._on_seek_press)
        self.seek.bind("<B1-Motion>", self._on_seek_drag)
        self.seek.bind("<ButtonRelease-1>", self._on_seek_release)
        self.btn_fwd = ttk.Button(seek_row, width=8, command=self.seek_fwd)
        self.btn_fwd.pack(side=tk.LEFT)

        # Kontrol transport (compact: ikon + lompat + volume sebaris)
        ctrl = ttk.Frame(left)
        ctrl.pack(fill=tk.X, pady=2)
        self.ctrl_bar = ctrl
        ttk.Button(ctrl, text="⏮", width=4, command=self.prev_video).pack(side=tk.LEFT, padx=1)
        self.btn_play = ttk.Button(ctrl, text="▶", width=4, command=self.toggle_play)
        self.btn_play.pack(side=tk.LEFT, padx=1)
        ttk.Button(ctrl, text="⏹", width=4, command=self.stop).pack(side=tk.LEFT, padx=1)
        ttk.Button(ctrl, text="⏭", width=4, command=self.next_video).pack(side=tk.LEFT, padx=1)
        ttk.Button(ctrl, text="⛶", width=4, command=self.toggle_fullscreen).pack(side=tk.LEFT, padx=(6, 1))
        self.btn_settings = ttk.Button(ctrl, text="⚙", width=4, command=self.toggle_settings_popup)
        self.btn_settings.pack(side=tk.LEFT, padx=1)
        ttk.Separator(ctrl, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=2)
        ttk.Button(ctrl, text="📂 Buka Folder", command=self.open_folder).pack(side=tk.LEFT, padx=1)
        ttk.Button(ctrl, text="↺ Buka Terakhir", command=self.open_last).pack(side=tk.LEFT, padx=1)
        self.btn_pl = ttk.Button(ctrl, text="☰ Playlist", command=self.toggle_playlist)
        self.btn_pl.pack(side=tk.LEFT, padx=1)

        vol_frame = ttk.Frame(ctrl)
        vol_frame.pack(side=tk.RIGHT)
        self.btn_mute = ttk.Button(vol_frame, text="🔊", width=4, command=self.toggle_mute)
        self.btn_mute.pack(side=tk.LEFT)
        self.vol_var = tk.IntVar(value=int(self.cfg.get("volume", 80)))
        self.vol = ttk.Scale(vol_frame, from_=0, to=100, variable=self.vol_var,
                             orient=tk.HORIZONTAL, length=100, command=self._on_volume)
        self.vol.pack(side=tk.LEFT, padx=4)
        self.lbl_vol = ttk.Label(vol_frame, text=f"{self.vol_var.get()}%")
        self.lbl_vol.pack(side=tk.LEFT)

        # Panel Cut clip & Share (rapat, baris melebar penuh)
        clip = ttk.LabelFrame(left, text="✂ Cut Clip & Share ke YouTube", padding=4)
        clip.pack(fill=tk.X, pady=2)
        self.clip_box = clip
        row1 = ttk.Frame(clip)
        row1.pack(fill=tk.X)
        ttk.Label(row1, text="Mulai:").pack(side=tk.LEFT)
        self.ent_start = ttk.Entry(row1, width=10)
        self.ent_start.pack(side=tk.LEFT, padx=3)
        self.ent_start.insert(0, "0:00")
        ttk.Button(row1, text="Set ⏺", command=self.set_start_now).pack(side=tk.LEFT, padx=1)
        ttk.Label(row1, text=" Selesai:").pack(side=tk.LEFT)
        self.ent_end = ttk.Entry(row1, width=10)
        self.ent_end.pack(side=tk.LEFT, padx=3)
        self.ent_end.insert(0, "0:10")
        ttk.Button(row1, text="Set ⏺", command=self.set_end_now).pack(side=tk.LEFT, padx=1)
        self.lbl_dur = ttk.Label(row1, text="", foreground="gray")
        self.lbl_dur.pack(side=tk.LEFT, padx=8)

        row2 = ttk.Frame(clip)
        row2.pack(fill=tk.X, pady=(2, 0))
        ttk.Button(row2, text="✂ Potong", command=self.cut_clip_threaded).pack(side=tk.LEFT, padx=2)
        ttk.Button(row2, text="📤 Share", command=self.share_youtube).pack(side=tk.LEFT, padx=2)
        ttk.Button(row2, text="📁 Clip", command=self.open_clip_folder).pack(side=tk.LEFT, padx=2)
        self.lbl_lastclip = ttk.Label(row2, text="detik / mnt:dtk / jam:mnt:dtk", foreground="gray")
        self.lbl_lastclip.pack(side=tk.LEFT, padx=8, fill=tk.X, expand=True)
        self.ent_start.bind("<KeyRelease>", lambda e: self._refresh_dur())
        self.ent_end.bind("<KeyRelease>", lambda e: self._refresh_dur())
        self._refresh_dur()

        # Playlist kanan
        right = ttk.Frame(main)
        main.add(right, weight=1)
        self.playlist_frame = right
        ttk.Label(right, text="📃 Playlist (scroll di mana saja = ganti video)", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, padx=4, pady=2)
        list_frame = ttk.Frame(right)
        list_frame.pack(fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
        self.listbox = tk.Listbox(list_frame, yscrollcommand=sb.set, selectmode=tk.SINGLE,
                                  font=("Segoe UI", 9), activestyle="dotbox")
        sb.config(command=self.listbox.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)
        self.listbox.bind("<Double-Button-1>", lambda e: self._play_selected())

        # Status bar (pesan kiri + info folder kanan)
        statusbar = ttk.Frame(self.root)
        statusbar.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_bar = statusbar
        self.lbl_status = ttk.Label(statusbar, text="Siap.", relief=tk.SUNKEN, anchor=tk.W, padding=4)
        self.lbl_status.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.lbl_finfo = ttk.Label(statusbar, text="", relief=tk.SUNKEN, anchor=tk.E, padding=4)
        self.lbl_finfo.pack(side=tk.RIGHT)
        self._refresh_finfo()

    def _bind_shortcuts(self):
        # Scroll up/down -> next/prev video, AKTIF DI MANA SAJA selama window aktif.
        # bind_all menangkap wheel di semua widget (tombol, video, playlist, slider...).
        self.root.bind_all("<MouseWheel>", self._on_wheel)      # Windows
        self.root.bind_all("<Button-4>", lambda e: self.prev_video())  # Linux up
        self.root.bind_all("<Button-5>", lambda e: self.next_video())   # Linux down
        self.root.bind("<space>", lambda e: self.toggle_play())
        self.root.bind("<Left>", lambda e: self.seek_back())
        self.root.bind("<Right>", lambda e: self.seek_fwd())
        self.root.bind("<Up>", lambda e: self._on_up_down(e, prev=True))
        self.root.bind("<Down>", lambda e: self._on_up_down(e, prev=False))
        self.root.bind("n", lambda e: self.next_video())
        self.root.bind("N", lambda e: self.next_video())
        self.root.bind("p", lambda e: self.prev_video())
        self.root.bind("P", lambda e: self.prev_video())
        self.root.bind("m", lambda e: self.toggle_mute())
        self.root.bind("M", lambda e: self.toggle_mute())
        self.root.bind("a", lambda e: self.cycle_aspect())
        self.root.bind("A", lambda e: self.cycle_aspect())
        self.root.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.root.bind("<Escape>", lambda e: self.exit_fullscreen())
        self.root.bind("f", lambda e: self.toggle_fullscreen())
        self.root.bind("F", lambda e: self.toggle_fullscreen())
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ============ Folder / playlist ============
    def _scan_videos(self, folder: Path) -> list[Path]:
        """List semua video di folder + seluruh subfolder-nya (rekursif)."""
        out: list[Path] = []
        try:
            for p in sorted(folder.rglob("*")):
                try:
                    if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                        out.append(p)
                except OSError:
                    continue
        except OSError:
            pass
        return out

    def open_folder(self):
        initial = self.cfg.get("last_folder") or str(Path.home())
        folder = filedialog.askdirectory(title="Pilih folder video (termasuk subfolder)", initialdir=initial)
        if not folder:
            return
        files = self._scan_videos(Path(folder))
        if not files:
            messagebox.showinfo("Kosong", f"Tidak ada video di:\n{folder}\n\nEkstensi: {', '.join(sorted(VIDEO_EXTS))}")
            return
        self.playlist = files
        self.playlist_folder = Path(folder)
        self.cfg["last_folder"] = folder
        save_config(self.cfg)
        self._refresh_playlist_ui()
        self.play_index(0)
        self.status(f"Dibuka: {folder} — {len(files)} video")

    def open_last(self):
        last = self.cfg.get("last_folder", "")
        if not last or not Path(last).is_dir():
            messagebox.showinfo("Info", "Belum ada folder terakhir yang tersimpan.")
            return
        files = self._scan_videos(Path(last))
        if not files:
            messagebox.showinfo("Info", f"Folder terakhir kosong:\n{last}")
            return
        self.playlist = files
        self.playlist_folder = Path(last)
        self._refresh_playlist_ui()
        self.play_index(0)
        self.status(f"Dibuka kembali: {last}")

    def _refresh_playlist_ui(self):
        self.listbox.delete(0, tk.END)
        for p in self.playlist:
            try:
                if self.playlist_folder is not None:
                    self.listbox.insert(tk.END, str(p.relative_to(self.playlist_folder)))
                else:
                    self.listbox.insert(tk.END, p.name)
            except ValueError:
                self.listbox.insert(tk.END, p.name)
        self._refresh_finfo()

    def _refresh_finfo(self):
        """Info folder terakhir + jumlah video di status bar kanan."""
        try:
            last = self.cfg.get("last_folder", "")
            base = Path(last).name if last else "-"
            self.lbl_finfo.config(text=f"📁 {base} • {len(self.playlist)} video")
        except Exception:
            pass

    def _on_list_select(self, _e=None):
        pass  # seleksi saja, play saat double-click / Enter

    def _play_selected(self):
        sel = self.listbox.curselection()
        if sel:
            self.play_index(sel[0])

    # ============ Playback ============
    def _attach_video(self):
        """Tempel output VLC ke panel video (Windows)."""
        try:
            self.video_frame.update_idletasks()
            hwnd = self.video_frame.winfo_id()
            if sys.platform.startswith("win"):
                self.player.set_hwnd(hwnd)
            elif sys.platform.startswith("linux"):
                self.player.set_xwindow(hwnd)
            elif sys.platform == "darwin":
                self.player.set_nsobject(hwnd)
            if hasattr(self, "_hint_label"):
                self._hint_label.place_forget()
        except Exception as e:
            print("attach video gagal:", e)

    def play_index(self, i: int, push_history: bool = False):
        if not self.playlist:
            return
        if push_history and self.index >= 0 and self.index != i:
            self._history.append(self.index)
            del self._history[:-100]  # batasi 100 entri
        i = max(0, min(i, len(self.playlist) - 1))
        self.index = i
        path = self.playlist[i]
        self._attach_video()
        media = self.vlc_instance.media_new(str(path))
        self.player.set_media(media)
        self.player.play()
        self.player.audio_set_volume(self.vol_var.get())
        self._apply_aspect()
        self.btn_play.config(text="⏸")
        self.lbl_title.config(text=f"{i+1}/{len(self.playlist)} — {path.name}")
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(i)
        self.listbox.see(i)
        self.cfg["last_file"] = str(path)
        save_config(self.cfg)

    def toggle_play(self):
        if not self.playlist:
            self.open_folder()
            return
        if self.index < 0:
            self.play_index(0)
            return
        if self.player.is_playing():
            self.player.pause()
            self.btn_play.config(text="▶")
        else:
            # jika sudah stop total, play ulang file aktif
            if self.player.get_time() < 0:
                self.play_index(self.index)
            else:
                self.player.play()
                self.btn_play.config(text="⏸")

    def stop(self):
        try:
            self.player.stop()
        except Exception:
            pass
        self.btn_play.config(text="▶")

    def next_video(self, _from_wheel=False):
        if not self.playlist:
            return
        if self.random_mode and len(self.playlist) > 1:
            nxt = self.index
            while nxt == self.index:
                nxt = random.randrange(len(self.playlist))
            self._history.append(self.index)
            del self._history[:-100]
            self.play_index(nxt)
        else:
            self._history.append(self.index)
            del self._history[:-100]
            self.play_index((self.index + 1) % len(self.playlist))
        self.status(f"⏭ {self.playlist[self.index].name}")

    def prev_video(self, _from_wheel=False):
        if not self.playlist:
            return
        # mode random: kembali via history bila ada
        if self.random_mode and self._history:
            prev = self._history.pop()
            if 0 <= prev < len(self.playlist):
                self.play_index(prev, push_history=False)
                self.status(f"⏮ {self.playlist[self.index].name}")
                return
        self.play_index((self.index - 1) % len(self.playlist))
        self.status(f"⏮ {self.playlist[self.index].name}")

    def _on_up_down(self, event, prev: bool):
        """Panah atas/bawah = pindah video. Dikecualikan saat fokus di widget yang
        memakai panah secara native (slider volume/seek, listbox, combobox, entry)
        agar tidak double-aksi."""
        try:
            try:
                w = self.root.focus_get()
                cls = w.winfo_class() if w is not None else ""
            except Exception:
                cls = ""
            if cls in ("TScale", "Scale", "TCombobox", "Combobox", "Listbox",
                       "Spinbox", "TSpinbox", "Entry", "Text"):
                return None  # biarkan perilaku asli widget
            if prev:
                self.prev_video()
            else:
                self.next_video()
            return "break"
        except Exception:
            pass

    def _on_wheel(self, event):
        # Aktif di mana saja, kecuali di kolom ketikan waktu (Entry) agar bisa scroll teks.
        # Windows: event.delta > 0 = scroll up, < 0 = scroll down
        try:
            if not self.scroll_var.get():
                return None  # biarkan perilaku default (mis. scroll playlist)
            w = getattr(event, "widget", None)
            try:
                cls = w.winfo_class() if w is not None else ""
            except Exception:
                cls = ""
            if cls in ("Entry", "Text", "Spinbox", "TCombobox"):
                return None
            now = time.time()
            if now - self._wheel_last < 0.30:  # debounce 300ms
                return "break"
            self._wheel_last = now
            if getattr(event, "delta", 0) > 0:
                self.prev_video(_from_wheel=True)   # scroll up = sebelumnya
            elif getattr(event, "delta", 0) < 0:
                self.next_video(_from_wheel=True)   # scroll down = berikutnya
            return "break"
        except Exception:
            pass

    # ---- Scroll global + klik video (via pynput, hanya saat window aktif) ----
    def _on_toggle_scroll(self):
        self.scroll_enabled = bool(self.scroll_var.get())
        self.cfg["scroll_global"] = self.scroll_enabled
        save_config(self.cfg)
        self._update_global_listener()
        self.status("Scroll global AKTIF — scroll di mana saja ganti video."
                    if self.scroll_enabled else "Scroll global MATI.")

    def _on_toggle_click(self):
        self.click_toggle = bool(self.click_var.get())
        self.cfg["click_toggle"] = self.click_toggle
        save_config(self.cfg)
        self._update_global_listener()
        self.status("Klik video = play/pause AKTIF." if self.click_toggle
                    else "Klik video = play/pause MATI.")

    def toggle_settings_popup(self):
        try:
            if self.set_popup.winfo_viewable():
                self.hide_settings_popup()
            else:
                self.show_settings_popup()
        except Exception:
            pass

    def show_settings_popup(self):
        try:
            try:
                x = self.root.winfo_rootx() + 40
                y = self.root.winfo_rooty() + 80
                self.set_popup.geometry(f"+{x}+{y}")
            except Exception:
                pass
            self.set_popup.deiconify()
            try:
                self.set_popup.lift()
            except Exception:
                pass
        except Exception:
            pass

    def hide_settings_popup(self):
        try:
            self.set_popup.withdraw()
        except Exception:
            pass

    # ---- Rasio aspek video (diingat) ----
    def _apply_aspect(self):
        """Terapkan rasio ke video yang sedang diputar. ''/None = Default (otomatis, tidak stretch)."""
        try:
            self.player.video_set_aspect_ratio(self.aspect_ratio or None)
        except Exception:
            pass

    def _on_aspect_change(self, _e=None):
        v = self.aspect_var.get()
        self.aspect_ratio = "" if v == "Default" else v
        self.cfg["aspect_ratio"] = self.aspect_ratio
        save_config(self.cfg)
        self._apply_aspect()
        self.status(f"Rasio video: {v} (diingat untuk berikutnya).")

    def cycle_aspect(self):
        opts = ["Default", "16:9", "16:10", "21:9", "4:3", "3:2", "1:1"]
        try:
            cur = self.aspect_var.get()
            nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
        except Exception:
            nxt = opts[0]
        self.aspect_var.set(nxt)
        self._on_aspect_change()

    def _update_global_listener(self):
        want = bool(getattr(self, "scroll_var", None) and self.scroll_var.get()) \
            or bool(getattr(self, "click_var", None) and self.click_var.get())
        if want and self._global_listener is None:
            self._start_global_listener()
        elif not want and self._global_listener is not None:
            self._stop_global_scroll()

    def _update_hook_label(self):
        """Indikator di UI: apakah hook mouse global (pynput) hidup atau mati."""
        try:
            lis = self._global_listener
            alive = bool(lis is not None and lis.is_alive())
            txt = "hook mouse: ON" if alive else "hook mouse: OFF"
            if txt != self._hook_shown:
                self._hook_shown = txt
                self.lbl_hook.config(text=txt,
                                     foreground="green" if alive else "red")
        except Exception:
            pass

    def _is_own_window_active(self) -> bool:
        """Cek apakah window MiniPlayer sedang foreground (Windows)."""
        try:
            if not sys.platform.startswith("win"):
                return bool(self.root.focus_displayof() is not None)
            fg = ctypes.windll.user32.GetForegroundWindow()
            if not fg:
                return False
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            return int(pid.value) == os.getpid()
        except Exception:
            try:
                return self.root.focus_displayof() is not None
            except Exception:
                return True

    def _start_global_listener(self):
        if self._global_listener is not None:
            return
        try:
            from pynput import mouse as _mouse
            from pynput.mouse import Button as _Btn
        except ImportError:
            self.status("pynput belum terinstal: pip install pynput (scroll/klik global nonaktif)")
            return

        def _on_scroll(x, y, dx, dy):
            # Thread listener: TANPA sentuh Tk, hanya kirim perintah via queue.
            try:
                if not self.scroll_enabled:
                    return
                if not self._is_own_window_active():
                    return  # hanya saat window program aktif
                now = time.time()
                if now - self._wheel_last < 0.35:
                    return
                self._wheel_last = now
                if dy > 0:
                    self._cmd_queue.put("prev")
                elif dy < 0:
                    self._cmd_queue.put("next")
            except Exception:
                pass

        def _on_click(x, y, button, pressed):
            # Klik kiri di area video = play/pause (VLC menutup event Tk, jadi via hook).
            # Validasi geometri dilakukan di thread utama via queue.
            try:
                if not pressed or button != _Btn.left:
                    return
                if not self.click_toggle:
                    return
                if not self._is_own_window_active():
                    return
                if not self.playlist or self.index < 0:
                    return
                self._cmd_queue.put(("click", x, y))
            except Exception:
                pass

        try:
            self._global_listener = _mouse.Listener(on_scroll=_on_scroll, on_click=_on_click)
            self._global_listener.daemon = True
            self._global_listener.start()
        except Exception as e:
            self._global_listener = None
            print("global listener gagal:", e)

    def _stop_global_scroll(self):
        try:
            if self._global_listener is not None:
                self._global_listener.stop()
        except Exception:
            pass
        self._global_listener = None

    def _drain_global_commands(self):
        """Jalankan perintah dari hook mouse. HARUS di thread utama (dipanggil _tick)."""
        try:
            for _ in range(8):
                try:
                    cmd = self._cmd_queue.get_nowait()
                except queue.Empty:
                    break
                if cmd == "prev":
                    self.prev_video()
                elif cmd == "next":
                    self.next_video()
                elif isinstance(cmd, tuple) and len(cmd) == 3 and cmd[0] == "click":
                    self._handle_video_click(cmd[1], cmd[2])
        except Exception:
            pass

    def _handle_video_click(self, x, y):
        """Validasi + toggle play/pause. HARUS di thread utama."""
        try:
            if not self.click_toggle:
                return
            if not self._is_own_window_active():
                return
            if not self.playlist or self.index < 0:
                return
            if not self._point_in(x, y, self.video_frame):
                return
            now = time.time()
            if now - self._last_click_toggle < 0.35:
                return
            self._last_click_toggle = now
            self.toggle_play()
        except Exception:
            pass

    # ---- Klik-kiri di video = play/pause via polling (anti-gagal, tanpa hook) ----
    def _click_poll(self):
        """Cek tombol kiri mouse tiap 50ms di THREAD UTAMA. Tangkap sisi-naik (press)."""
        try:
            if (self.playlist and self.index >= 0 and self.click_toggle
                    and sys.platform.startswith("win")):
                try:
                    down = bool(self._lbutton_is_down())
                except Exception:
                    down = False
                was = self._lbutton_down
                self._lbutton_down = down
                if down and not was:
                    self._on_lbutton_press()
        except Exception:
            pass
        finally:
            try:
                self.root.after(50, self._click_poll)
            except Exception:
                pass

    def _lbutton_is_down(self) -> bool:
        try:
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
        except Exception:
            return False

    def _cursor_pos(self):
        """Posisi kursor layar (fisik). Return (x, y) atau None."""
        try:
            pt = ctypes.wintypes.POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                return (int(pt.x), int(pt.y))
        except Exception:
            pass
        return None

    def _on_lbutton_press(self):
        try:
            if not self.click_toggle:
                return
            try:
                if self.root.grab_current():
                    return  # dialog modal (buka folder/dll) terbuka → abaikan
            except Exception:
                pass
            if not self._is_own_window_active():
                return
            if not self.playlist or self.index < 0:
                return
            pos = self._cursor_pos()
            if not pos:
                return
            if not self._point_in(pos[0], pos[1], self.video_frame):
                return
            now = time.time()
            if now - self._last_click_toggle < 0.35:
                return
            self._last_click_toggle = now
            self.toggle_play()
        except Exception:
            pass

    # ---- Bantuan geometri layar (untuk klik video) ----
    def _point_in(self, x, y, widget) -> bool:
        try:
            rx, ry = widget.winfo_rootx(), widget.winfo_rooty()
            rw, rh = widget.winfo_width(), widget.winfo_height()
            return rx <= x <= rx + rw and ry <= y <= ry + rh
        except Exception:
            return False

    def _pointer_in_window(self, px, py) -> bool:
        return self._point_in(px, py, self.root)

    # ---- Random play ----
    def _on_toggle_random(self):
        self.random_mode = bool(self.random_var.get())
        self.cfg["random_mode"] = self.random_mode
        save_config(self.cfg)
        self.status("🔀 Random play AKTIF." if self.random_mode else "Random play MATI (urutan normal).")

    def shuffle_now(self):
        if len(self.playlist) < 2:
            messagebox.showinfo("Info", "Playlist kurang dari 2 video.")
            return
        cur = self.current_path()
        random.shuffle(self.playlist)
        self._refresh_playlist_ui()
        # pertahankan posisi file yang sedang diputar
        if cur is not None:
            for k, p in enumerate(self.playlist):
                if p == cur:
                    self.index = k
                    self.listbox.selection_clear(0, tk.END)
                    self.listbox.selection_set(k)
                    self.listbox.see(k)
                    self.lbl_title.config(text=f"{k+1}/{len(self.playlist)} — {p.name}")
                    break
        self._history.clear()
        self.status(f"🎲 Urutan diacak ({len(self.playlist)} video).")

    # ---- Ingat posisi terakhir (resume) ----
    def _on_toggle_resume(self):
        self.resume_enabled = bool(self.resume_var.get())
        self.cfg["resume"] = self.resume_enabled
        save_config(self.cfg)
        self.status("Resume posisi AKTIF." if self.resume_enabled else "Resume posisi MATI.")

    def _sync_option_ui(self):
        try:
            self.scroll_var.set(self.scroll_enabled)
            self.resume_var.set(self.resume_enabled)
            self.random_var.set(self.random_mode)
            self.click_var.set(self.click_toggle)
            self.aspect_var.set(self.aspect_ratio or "Default")
            self.step_var.set(str(self.seek_step))
            self._refresh_step_buttons()
            self.btn_pl.config(text="☰ Sembunyi Playlist" if self.playlist_visible else "☰ Tampil Playlist")
            if not self.playlist_visible:
                self.main_pane.remove(self.playlist_frame)
        except Exception:
            pass

    def _do_resume(self):
        try:
            if self._resume_done or not self.resume_enabled:
                return
            self._resume_done = True
            # batalkan jika user sudah pindah file lain sebelum resume jalan
            cur = self.current_path()
            if cur is None or str(cur) != str(self.cfg.get("last_file", "")):
                return
            if self._pending_resume_ms and self._pending_resume_ms > 5000:
                length = self.player.get_length() or 0
                # jangan resume kalau sisa < 10 detik (anggap sudah tamat)
                if length and self._pending_resume_ms > length - 10000:
                    return
                self.player.set_time(int(self._pending_resume_ms))
                self.lbl_resume.config(text=f"▶ Lanjut dari {format_ms(self._pending_resume_ms)}")
                self.status(f"▶ Melanjutkan dari {format_ms(self._pending_resume_ms)}")
        except Exception:
            pass

    def _on_media_end(self, _event):
        # Auto-next saat video habis (jalan di thread VLC -> lempar ke Tk)
        try:
            self.root.after(100, self.next_video)
        except Exception:
            pass

    def toggle_fullscreen(self):
        if getattr(self, "_fs", False):
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self):
        """Fullscreen khusus video: semua panel/toolbar/playlist disembunyikan."""
        if getattr(self, "_fs", False):
            return
        self._fs = True
        self._fs_pl_was_visible = self.playlist_visible
        try:
            self.status_bar.pack_forget()
            try:
                self._popup_was_open = bool(self.set_popup.winfo_viewable())
            except Exception:
                self._popup_was_open = False
            self.hide_settings_popup()
            self.info_bar.pack_forget()
            self.seek_row.pack_forget()
            self.ctrl_bar.pack_forget()
            self.clip_box.pack_forget()
            if self.playlist_visible:
                self.main_pane.remove(self.playlist_frame)
            self.root.attributes("-fullscreen", True)
        except Exception:
            pass

    def exit_fullscreen(self):
        """Kembalikan tampilan normal."""
        if not getattr(self, "_fs", False):
            return
        self._fs = False
        try:
            self.root.attributes("-fullscreen", False)
            # repack urutan semula: video, top, status (+ popup bila tadi terbuka)
            self.main_pane.pack_forget()
            self.main_pane.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
            self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)
            if getattr(self, "_popup_was_open", False):
                self._popup_was_open = False
                self.show_settings_popup()
            # panel kiri urutan semula (video tetap paling atas)
            self.info_bar.pack(fill=tk.X, pady=(2, 0))
            self.seek_row.pack(fill=tk.X)
            self.ctrl_bar.pack(fill=tk.X, pady=2)
            self.clip_box.pack(fill=tk.X, pady=2)
            if self.playlist_visible:
                try:
                    self.main_pane.add(self.playlist_frame, weight=1)
                except tk.TclError:
                    pass  # sudah ada
            self.video_frame.update_idletasks()
        except Exception:
            pass

    # ============ Seek & volume ============
    def _tick(self):
        try:
            if self.player and not self.is_seeking:
                length = self.player.get_length()  # ms
                t = self.player.get_time()
                if length and length > 0 and t is not None and t >= 0:
                    self.seek_var.set(t / length * 1000)
                    self.time_var.set(f"{format_ms(t)} / {format_ms(length)}")
                elif self.playlist and self.index >= 0:
                    pass
            if self.player and self.player.is_playing():
                self.btn_play.config(text="⏸")
            # simpan posisi play tiap ~2 detik (ingat last play)
            self._tick_count += 1
            if self._tick_count % 10 == 0 and self.playlist and self.index >= 0:
                try:
                    t = self.player.get_time()
                    if t is not None and t > 0:
                        self.cfg["last_time"] = int(t)
                        cur = self.current_path()
                        if cur is not None:
                            self.cfg["last_file"] = str(cur)
                        save_config(self.cfg)
                except Exception:
                    pass
            self._drain_global_commands()
            self._update_hook_label()
        except Exception:
            pass
        self._timer_id = self.root.after(200, self._tick)

    def _seek_fraction(self, widget, e):
        """Hitung posisi klik (0.0–1.0) dari koordinat mouse di atas slider."""
        try:
            w = widget.winfo_width()
            if w <= 1:
                return None
            return min(max(e.x / w, 0.0), 1.0)
        except Exception:
            return None

    def _on_seek_press(self, e):
        # Klik di mana saja pada timeline = langsung lompat ke sana
        self.is_seeking = True
        frac = self._seek_fraction(self.seek, e)
        if frac is not None:
            self.seek_var.set(frac * 1000)

    def _on_seek_drag(self, e):
        # Geser sambil tahan = scrub mengikuti mouse
        if self.is_seeking:
            frac = self._seek_fraction(self.seek, e)
            if frac is not None:
                self.seek_var.set(frac * 1000)

    def _on_seek_release(self, _e=None):
        try:
            self.is_seeking = False
            if not self.playlist or self.index < 0:
                return
            length = self.player.get_length()
            if length and length > 0:
                target = int(self.seek_var.get() / 1000 * length)
                self.player.set_time(max(0, target))
        except Exception:
            self.is_seeking = False

    def seek_back(self):
        self.seek_relative(-self.seek_step * 1000)

    def seek_fwd(self):
        self.seek_relative(self.seek_step * 1000)

    def _refresh_step_buttons(self):
        try:
            self.btn_back.config(text=f"⏮ -{self.seek_step}s")
            self.btn_fwd.config(text=f"+{self.seek_step}s ⏭")
        except Exception:
            pass

    def _on_step_change(self, _e=None):
        try:
            v = int(self.step_var.get())
        except Exception:
            return
        v = max(1, min(v, 300))
        self.seek_step = v
        self.cfg["seek_step"] = v
        save_config(self.cfg)
        self._refresh_step_buttons()
        self.status(f"Panjang seek: {v} detik.")

    def toggle_playlist(self):
        """Sembunyikan / tampilkan panel playlist kanan."""
        try:
            if self.playlist_visible:
                self.main_pane.remove(self.playlist_frame)
                self.playlist_visible = False
                self.btn_pl.config(text="☰ Tampil Playlist")
                self.status("Playlist disembunyikan — area video lebih lega.")
            else:
                self.main_pane.add(self.playlist_frame, weight=1)
                self.playlist_visible = True
                self.btn_pl.config(text="☰ Sembunyi Playlist")
                self.status("Playlist ditampilkan.")
            self.cfg["playlist_visible"] = self.playlist_visible
            save_config(self.cfg)
        except Exception as e:
            messagebox.showerror("Gagal", str(e))

    def seek_relative(self, ms: int):
        try:
            t = self.player.get_time()
            length = self.player.get_length()
            if t is None or t < 0:
                return
            nt = max(0, min(t + ms, (length or t + ms)))
            self.player.set_time(nt)
        except Exception:
            pass

    def _on_volume(self, _v=None):
        try:
            v = int(self.vol_var.get())
            self.player.audio_set_volume(v)
            self.lbl_vol.config(text=f"{v}%")
            self.btn_mute.config(text="🔇" if v == 0 else "🔊")
            self.cfg["volume"] = v
        except Exception:
            pass

    def toggle_mute(self):
        try:
            muted = self.player.audio_get_mute()
            self.player.audio_set_mute(not muted)
            self.btn_mute.config(text="🔊" if muted else "🔇")
        except Exception:
            # fallback: volume 0 / restore
            if self.vol_var.get() == 0:
                self.vol_var.set(80)
            else:
                self.vol_var.set(0)
            self._on_volume()

    # ============ Cut clip ============
    def current_path(self) -> Path | None:
        if self.playlist and 0 <= self.index < len(self.playlist):
            return self.playlist[self.index]
        return None

    def _refresh_dur(self):
        """Tampilkan durasi clip (selesai − mulai) di panel."""
        try:
            s = parse_time_to_sec(self.ent_start.get())
            e = parse_time_to_sec(self.ent_end.get())
            d = e - s
            self.lbl_dur.config(text=f"⏱ {d:.1f} dtk" if d > 0 else "⚠ selesai ≤ mulai")
        except Exception:
            try:
                self.lbl_dur.config(text="")
            except Exception:
                pass

    def set_start_now(self):
        t = self.player.get_time()
        if t is None or t < 0:
            t = 0
        self.ent_start.delete(0, tk.END)
        self.ent_start.insert(0, format_ms(t))
        self._refresh_dur()

    def set_end_now(self):
        t = self.player.get_time()
        if t is None or t < 0:
            t = 10000
        self.ent_end.delete(0, tk.END)
        self.ent_end.insert(0, format_ms(t))
        self._refresh_dur()

    def cut_clip_threaded(self):
        th = threading.Thread(target=self.cut_clip, daemon=True)
        th.start()

    def cut_clip(self):
        src = self.current_path()
        if not src:
            messagebox.showwarning("Belum ada video", "Buka folder dulu.")
            return
        try:
            start = parse_time_to_sec(self.ent_start.get())
            end = parse_time_to_sec(self.ent_end.get())
        except ValueError as e:
            messagebox.showerror("Waktu salah", str(e))
            return
        if end <= start:
            messagebox.showerror("Waktu salah", "Waktu selesai harus lebih besar dari mulai.")
            return
        if not shutil.which("ffmpeg"):
            messagebox.showerror("ffmpeg tidak ada", "ffmpeg tidak ditemukan di PATH. Install ffmpeg dulu.")
            return

        outdir = Path(self.cfg.get("clip_dir", str(CLIP_DIR_DEFAULT)))
        outdir.mkdir(parents=True, exist_ok=True)
        s_tag = self.ent_start.get().strip().replace(":", "-")
        e_tag = self.ent_end.get().strip().replace(":", "-")
        out = outdir / f"{src.stem}_clip_{s_tag}-{e_tag}.mp4"
        i = 1
        while out.exists():
            out = outdir / f"{src.stem}_clip_{s_tag}-{e_tag}_{i}.mp4"
            i += 1

        try:
            self.lbl_lastclip.config(text="⏳ Memotong...", foreground="orange")
        except Exception:
            pass
        self.status(f"Memotong {src.name} [{start:.1f}s → {end:.1f}s] ...")

        # 1) coba stream-copy (cepat), 2) fallback re-encode
        cmd_fast = ["ffmpeg", "-y", "-ss", str(start), "-to", str(end),
                    "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(out)]
        cmd_slow = ["ffmpeg", "-y", "-ss", str(start), "-to", str(end),
                    "-i", str(src), "-c:v", "libx264", "-preset", "veryfast",
                    "-c:a", "aac", "-movflags", "+faststart", str(out)]
        try:
            r = subprocess.run(cmd_fast, capture_output=True, text=True, timeout=600)
            if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                r2 = subprocess.run(cmd_slow, capture_output=True, text=True, timeout=1200)
                if r2.returncode != 0:
                    raise RuntimeError(r2.stderr[-2000:])
            self.last_clip = out
            try:
                self.lbl_lastclip.config(text=f"✅ {out.name}", foreground="green")
            except Exception:
                pass
            self.status(f"Clip tersimpan: {out}")
            self.root.bell()
            messagebox.showinfo("Berhasil", f"Clip tersimpan:\n{out}\n\nKlik 'Share ke YouTube' untuk upload.")
        except Exception as e:
            try:
                self.lbl_lastclip.config(text="❌ Gagal memotong", foreground="red")
            except Exception:
                pass
            messagebox.showerror("Gagal", f"Gagal memotong:\n{e}")

    def open_clip_folder(self):
        outdir = Path(self.cfg.get("clip_dir", str(CLIP_DIR_DEFAULT)))
        outdir.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(outdir)  # type: ignore
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(outdir)])
            else:
                subprocess.Popen(["xdg-open", str(outdir)])
        except Exception as e:
            messagebox.showerror("Gagal", str(e))

    # ============ Share YouTube ============
    def share_youtube(self):
        clip = self.last_clip
        # tawarkan file jika belum ada clip sesi ini: pakai file dialog
        if not clip or not Path(clip).exists():
            ans = messagebox.askyesno("Belum ada clip",
                                      "Belum ada hasil potongan di sesi ini.\n\nPilih file video untuk di-share ke YouTube?")
            if ans:
                f = filedialog.askopenfilename(title="Pilih video untuk YouTube",
                                               filetypes=[("Video", "*.mp4 *.mkv *.mov *.avi *.webm")])
                if f:
                    clip = Path(f)
                    self.last_clip = clip
                else:
                    return
            else:
                return
        assert clip is not None
        # 1) coba upload API bila kredensial tersedia
        if (APP_DIR / "client_secrets.json").exists():
            if messagebox.askyesno("Upload API?",
                                   "client_secrets.json ditemukan.\nUpload langsung via YouTube API?\n\nYes = upload API\nNo = buka browser youtube.com/upload"):
                ok = self._upload_via_api(clip)
                if ok:
                    return
        # 2) fallback manual: buka folder + browser + copy path
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(clip))
        except Exception:
            pass
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(f'explorer /select,"{clip}"')
            else:
                self.open_clip_folder()
        except Exception:
            pass
        webbrowser.open("https://www.youtube.com/upload")
        messagebox.showinfo("Share ke YouTube",
                            f"Browser dibuka ke halaman upload YouTube.\n\nFile clip:\n{clip}\n\n"
                            "Path sudah di-copy ke clipboard.\n"
                            "Tinggal drag-drop / pilih file di halaman upload.\n\n"
                            "TIP: untuk upload otomatis, taruh client_secrets.json\n"
                            "dari Google Cloud Console di folder aplikasi.")

    def _upload_via_api(self, clip: Path) -> bool:
        """Upload via YouTube Data API v3 jika library tersedia. Return True bila sukses."""
        try:
            import pickle
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
            from google.auth.transport.requests import Request
        except ImportError:
            messagebox.showinfo("API belum terinstal",
                                "Untuk upload otomatis install dulu:\n\npip install google-api-python-client google-auth-httplib2 google-auth-oauthlib\n\n"
                                "Lalu taruh client_secrets.json di folder aplikasi.")
            return False
        SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
        creds = None
        token_p = APP_DIR / "token.pickle"
        try:
            if token_p.exists():
                with open(token_p, "rb") as f:
                    creds = pickle.load(f)
            if not creds or not getattr(creds, "valid", False):
                if creds and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
                    creds.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(str(APP_DIR / "client_secrets.json"), SCOPES)
                    creds = flow.run_local_server(port=0)
                with open(token_p, "wb") as f:
                    pickle.dump(creds, f)
            yt = build("youtube", "v3", credentials=creds)
            body = {"snippet": {"title": clip.stem[:100],
                                "description": f"Dipotong dengan MiniPlayer 🎬\nFile: {clip.name}",
                                "categoryId": "22"},
                    "status": {"privacyStatus": "unlisted"}}
            media = MediaFileUpload(str(clip), resumable=True)
            req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
            self.status("Mengupload ke YouTube...")
            resp = req.execute()
            vid = resp.get("id", "")
            webbrowser.open(f"https://youtu.be/{vid}" if vid else "https://www.youtube.com/upload")
            messagebox.showinfo("Upload sukses", f"Video terupload!\nhttps://youtu.be/{vid}")
            self.status(f"Upload sukses: https://youtu.be/{vid}")
            return True
        except Exception as e:
            messagebox.showerror("Upload gagal", f"{e}\n\nFallback: buka browser manual.")
            webbrowser.open("https://www.youtube.com/upload")
            return False

    # ============ Misc ============
    def status(self, msg: str):
        self.lbl_status.config(text=msg)

    def on_close(self):
        try:
            if getattr(self, "_fs", False):
                try:
                    self.exit_fullscreen()
                except Exception:
                    pass
            try:
                zoomed = (self.root.state() == "zoomed")
            except Exception:
                zoomed = False
            self.cfg["window_zoomed"] = bool(zoomed)
            if not zoomed:
                try:
                    self.root.update_idletasks()
                    self.cfg["window_geo"] = self.root.geometry()
                except Exception:
                    pass
            self.cfg["volume"] = int(self.vol_var.get())
            self.cfg["random_mode"] = bool(self.random_var.get())
            self.cfg["resume"] = bool(self.resume_var.get())
            self.cfg["scroll_global"] = bool(self.scroll_var.get())
            self.cfg["click_toggle"] = bool(self.click_var.get())
            self.cfg["aspect_ratio"] = self.aspect_ratio
            self.cfg["seek_step"] = int(self.seek_step)
            self.cfg["playlist_visible"] = bool(self.playlist_visible)
            try:
                t = self.player.get_time()
                if t is not None and t > 0:
                    self.cfg["last_time"] = int(t)
            except Exception:
                pass
            cur = self.current_path()
            if cur is not None:
                self.cfg["last_file"] = str(cur)
            save_config(self.cfg)
        except Exception:
            pass
        self._stop_global_scroll()
        try:
            self.player.stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    if sys.platform.startswith("win"):
        # Identitas taskbar (agar pin/ikon taskbar konsisten, tidak generik)
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MiniPlayer.VideoPlayer")
        except Exception:
            pass
        # Samakan koordinat Tk dengan piksel fisik kursor (penting di layar berskala 125%+,
        # kalau tidak klik-video meleset dari area video).
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    root = tk.Tk()
    try:
        style = ttk.Style()
        if sys.platform.startswith("win"):
            style.theme_use("vista")
    except Exception:
        pass
    MiniPlayer(root)
    root.mainloop()


if __name__ == "__main__":
    main()
