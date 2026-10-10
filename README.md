# MiniPlayer 🎬

Desktop video player (Python + Tkinter + VLC + ffmpeg).

## Fitur

- **Playlist folder rekursif** — buka folder, semua video termasuk subfolder ikut ter-list (tampil sebagai path relatif)
- **Scroll / ↑↓ / PgUp-PgDn?** — scroll mouse di mana saja (selama window aktif) atau panah `↑`/`↓` untuk pindah video; `N`/`P` juga bisa
- **Ingat terakhir** — folder, file, posisi play (auto-resume), volume, rasio aspek, panjang seek, ukuran/posisi window
- **Seek** — klik di mana saja pada timeline untuk lompat, drag untuk scrub, tombol `±Ns`, panah `←`/`→`
- **Panjang seek** — dropdown Lompat 1–60 detik (di popup ⚙)
- **Volume** — slider + mute (`M`)
- **Klik video = play/pause** (YouTube-style, polling tanpa hook; tangkapan via hook pynput sebagai cadangan)
- **Random play** — mode acak + tombol 🎲 acak urutan
- **Rasio aspek** — Default/16:9/16:10/21:9/4:3/3:2/1:1, diingat (`A` untuk ganti cepat)
- **Fullscreen khusus video** (`F`/`F11`, keluar `Esc`)
- **Playlist hide/show**, **panel ⚙ Pengaturan popup**
- **Cut clip (ffmpeg)** — tentukan Mulai/Selesai (ketik atau `Set ⏺` dari posisi), label durasi otomatis, hasil di `~/Videos/MiniPlayerClips`
- **Share ke YouTube** — buka halaman upload + path ter-copy; upload otomatis via API bila ada `client_secrets.json`

## Syarat

- Windows + Python 3.10+
- [VLC](https://www.videolan.org/) terinstal (untuk playback)
- [ffmpeg](https://ffmpeg.org/) di PATH (untuk potong clip)

## Jalankan dari source

```bat
jalankan.bat
```

atau manual:

```powershell
pip install -r requirements.txt
python main.py
```

## Build exe

```powershell
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name MiniPlayer main.py
```

Hasil: `dist\MiniPlayer.exe` (diabaikan git). PC target tetap butuh VLC + ffmpeg terinstal.
`config.json` tersimpan di samping exe (mode frozen) atau di samping `main.py` (mode source).

## Upload YouTube otomatis (opsional)

1. `pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib`
2. Taruh `client_secrets.json` dari Google Cloud Console di folder aplikasi
3. Klik `📤 Share` → pilih upload via API

## Struktur

```
MiniPlayer/
├── main.py           # seluruh aplikasi
├── icon.ico/.png     # ikon app + exe
├── requirements.txt  # python-vlc, pynput
├── jalankan.bat      # install + run cepat
├── config.json       # ingatan app (diabaikan git, dibuat otomatis)
└── README.md
```

## Shortcut

| Tombol | Aksi |
|---|---|
| `Space` / klik video | play / pause |
| `←` / `→` | seek ∓ langkah |
| `↑` / `↓` | video sebelum / berikut |
| `N` / `P` | next / prev |
| `M` | mute |
| `A` | ganti rasio aspek |
| `F` / `F11` | fullscreen video |
| `Esc` | keluar fullscreen |
