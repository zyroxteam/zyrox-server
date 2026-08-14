# 🎧 ZYROX MUSIC ENHANCE — Server Edition

> **Full audio studio with a Python backend.** Bass boosted songs, 8D audio, 3D mixes, equalizer, slow reverb, voice enhancement & more — powered by **Flask + ffmpeg + numpy** on Render.

Upload a file → the server processes it with a professional effect chain → download MP3 (192 kbps). Files are **deleted immediately after processing**. 🔒

---

## ⚡ Tools

| Tool | Server pipeline |
|---|---|
| 🎛️ **Bass Boosted** | `bass` filter + low EQ + loudness normalize |
| 🎚️ **Equalizer** | 8-band peaking EQ (`equalizer` chain) |
| 🌊 **Slow Reverb** | IR convolution (`afir`) + optional 0.85× slow-down |
| ✂️ **Cut Audio** | Sample-accurate trim (`-ss` / `-to`) |
| 🎬 **Video → Audio** | `ffmpeg -vn` extraction in seconds |
| 🎙️ **Enhance Voice** | high-pass + presence EQ + compressor |
| 🎧 **8D Audio** | 360° rotating pan (numpy, streaming, low memory) |
| ✨ **Make All Types** | 8D Bass Boosted · 3D Mix · 8D Slow Reverb · Ultra Bass 8D |

## 🚀 Deploy on Render (free)

This repo contains `render.yaml` — one-click Blueprint deploy:

1. Render dashboard → **New + → Blueprint**
2. Paste this repo URL → **Apply**
3. Render builds (installs ffmpeg + Python deps) and goes live at
   `https://zyrox-music-enhance.onrender.com`

> ⚠️ Free tier: server sleeps after ~15 min idle — first request wakes it in ~30–60 s.

## 🛠️ Run locally

```bash
# needs: python 3.10+, ffmpeg on PATH
pip install -r requirements.txt
python app.py            # http://localhost:8000
```

## 🔌 API

`POST /api/process` — multipart form data:

| field | values |
|---|---|
| `file` | audio/video file (max 300 MB) |
| `tool` | `bass` `eq` `reverb` `cut` `v2a` `voice` `8d` `all` |
| `bass` | 0–14 dB |
| `bands` | 8 comma-separated dB values (-12…12) |
| `preset` / `wet` / `slow` | reverb: `room`/`hall`/`cathedral`, 10–90 %, `0`/`1` |
| `start_pct` / `end_pct` | cut range 0–100 |
| `clarity` / `loud` | voice: 0–100, 0–8 dB |
| `speed` / `depth` | 8D: 2–20 rpm, 0.3–3 |
| `song_type` | `8d-bass-boosted` `3d-mix` `8d-slow-reverb` `ultra-bass-8d` |

Returns the processed MP3 (`Content-Disposition: attachment`).

`GET /api/health` → `{"status":"ok", ...}`

## 📁 Structure

```
app.py            Flask server (routes, upload handling, cleanup)
processor.py      DSP engine (ffmpeg chains + numpy 8D panning)
static/index.html Frontend UI
render.yaml       Render Blueprint config
```

## 🔒 Privacy

Uploaded files live only in a temp folder during processing and are wiped the moment the response is sent. No storage, no logs, no tracking.

## 📜 License

[MIT](./LICENSE)
