# Changelog

## 1.2.0 — 2026-11-03
- Owner mode (default): the first person who writes to the bot becomes its owner, everybody else is
  refused. No Telegram ID has to be configured. ACCESS_MODE=public keeps the old open behaviour,
  ALLOWED_USER_IDS still overrides both.
- One-file server installer for Oracle Cloud Always Free (deploy/oracle): chosen as the cloud-init
  script when the server is created. Installs FFmpeg/Python/Git, swap on small machines, downloads the
  code from GitHub (waits while the repository is private), runs the bot as a systemd service with
  automatic restarts and logs on the serial console, and checks GitHub every 6 hours for a new version
  (verified before switching, postponed while the bot is busy, rolled back if it does not start).
  build_installer.py fills in the token and API keys; built files are git-ignored.

## 1.1.0 — 2026-11-03
- Uniquelizer: 1–5 different copies per run with random zoom/shift, slight rotation without black corners,
  colour, hue, grain, sharpness, vignette, speed, edge trims, audio level/EQ, sample rate, fresh metadata
  (no FFmpeg versions, no x264 info SEI) and varied encoder settings. Intensity, copies, mirror, speed and
  9:16/1:1 format are set in an inline panel.
- Big files without a separate server: with TG_API_ID/TG_API_HASH the bot downloads (>20 MB) and uploads
  (>49 MB) through MTProto (Telethon). Defaults 300 MB in / 1000 MB out, configurable up to 2000 MB.
- Jobs are tracked per upload: double taps, new uploads during processing and old buttons can no longer
  delete or overwrite files of a running job. Running jobs can be stopped; FFmpeg is killed on stop/timeout.
- Source is kept for 30 minutes (and after errors) to make more variants without re-uploading; abandoned
  uploads are cleaned up automatically.
- FFmpeg runs asynchronously with progress, a real overall timeout and UTF-8-safe output decoding.
- Fixes: 96 kHz audio after loudnorm (now 48 kHz), odd-width videos failing in keep/warm presets, warm
  colour lost in the size fallback, phone rotation shown with swapped size, non-square pixels, only the first
  audio track is used, size fallback now targets the limit by bitrate, 30 s download / 60 s upload timeouts
  for big files, unclosed SQLite connections, /start with parameters, webhook left from another framework.
- Access list ALLOWED_USER_IDS, /id, /help, /cancel commands, video notes and animations accepted.
- Docker: named volume (works on Linux servers), Windows starter picks a supported Python and checks FFmpeg.
- 72 tests: unit tests, real FFmpeg runs on generated clips and full bot scenarios on a fake Telegram server.

## 1.0.0 — 2026-11-03
- First runnable MVP for Telegram video preparation.
- Aiogram 3 polling bot, FFmpeg/FFprobe processing, SQLite history.
- Reels/Shorts/Square/Keep/Warm presets.
- Basic file validation, rate limit, semaphore, cleanup and Telegram size guards.
- Dockerfile, Compose, Windows starter, Russian documentation and Claude Code task prompt.
