from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    width: int
    height: int
    has_audio: bool
    video_codec: str
    audio_codec: str | None
    size_bytes: int


@dataclass(frozen=True)
class Preset:
    key: str
    title: str
    description: str
    aspect: str
    framing: str
    color_filter: str | None = None
    normalize_audio: bool = True


PRESETS: dict[str, Preset] = {
    "reels_fill": Preset("reels_fill", "Reels · заполнить кадр", "9:16; края кадра могут обрезаться", "9:16", "fill"),
    "reels_fit": Preset("reels_fit", "Reels · сохранить весь кадр", "9:16; весь исходный кадр сохранится с чёрными полями", "9:16", "fit"),
    "shorts_fill": Preset("shorts_fill", "Shorts · заполнить кадр", "9:16; края кадра могут обрезаться", "9:16", "fill"),
    "square": Preset("square", "Квадрат · 1:1", "Квадратный формат; возможна обрезка краёв", "1:1", "fill"),
    "keep": Preset("keep", "Сохранить исходный кадр", "Не меняет композицию; пересобирает MP4 и нормализует звук", "original", "keep"),
    "warm": Preset("warm", "Мягкая тёплая коррекция", "Лёгкая художественная коррекция без смены кадрирования", "original", "keep", "eq=contrast=1.03:saturation=1.04:brightness=0.01"),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Не найден FFmpeg/FFprobe. Установи FFmpeg и добавь его в PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("FFmpeg превысил допустимое время обработки") from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout)[-1800:]
        raise RuntimeError(f"Команда обработки завершилась с ошибкой: {tail}")
    return result.stdout


def probe_video(path: Path) -> VideoInfo:
    if not path.exists() or path.stat().st_size == 0:
        raise ValueError("Файл пустой или не найден")
    output = _run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height",
        "-of", "json", str(path),
    ])
    data = json.loads(output)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("В файле не найдена видеодорожка")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or 0)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if not duration or not width or not height:
        raise ValueError("Не удалось определить длительность или разрешение видео")
    return VideoInfo(
        duration=duration, width=width, height=height, has_audio=audio is not None,
        video_codec=str(video.get("codec_name", "unknown")),
        audio_codec=str(audio.get("codec_name")) if audio else None,
        size_bytes=path.stat().st_size,
    )


def build_video_filter(preset: Preset, fallback: bool = False) -> str:
    parts: list[str] = []
    if preset.framing == "fill":
        target_w, target_h = ((720, 1280) if fallback else (1080, 1920)) if preset.aspect == "9:16" else ((720, 720) if fallback else (1080, 1080))
        parts.extend([
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase",
            f"crop={target_w}:{target_h}",
        ])
    elif preset.framing == "fit":
        target_w, target_h = ((720, 1280) if fallback else (1080, 1920))
        parts.extend([
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease",
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black",
        ])
    else:
        max_width = 1280 if fallback else 1920
        parts.append(f"scale='min({max_width},iw)':-2")
    if preset.color_filter and not fallback:
        parts.append(preset.color_filter)
    parts.append("setsar=1")
    return ",".join(parts)


def process_video(
    source: Path,
    destination: Path,
    preset_key: str,
    *,
    has_audio: bool,
    timeout: int,
    max_output_bytes: int,
) -> VideoInfo:
    if preset_key not in PRESETS:
        raise ValueError("Неизвестный режим обработки")
    preset = PRESETS[preset_key]
    destination.parent.mkdir(parents=True, exist_ok=True)

    def command_for(fallback: bool) -> list[str]:
        command = [
            "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a?", "-vf", build_video_filter(preset, fallback),
            "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "28" if fallback else "23", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-c:a", "aac",
            "-b:a", "96k" if fallback else "128k", "-map_metadata", "-1",
        ]
        if has_audio and preset.normalize_audio:
            command.extend(["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"])
        command.extend(["-max_muxing_queue_size", "1024", str(destination)])
        return command

    try:
        _run(command_for(False), timeout=timeout)
        if not destination.exists() or destination.stat().st_size == 0:
            raise RuntimeError("FFmpeg не создал готовый файл")
        if destination.stat().st_size > max_output_bytes:
            destination.unlink(missing_ok=True)
            _run(command_for(True), timeout=timeout)
        if not destination.exists() or destination.stat().st_size == 0:
            raise RuntimeError("Не удалось создать итоговый файл")
        if destination.stat().st_size > max_output_bytes:
            destination.unlink(missing_ok=True)
            raise ValueError(
                "После повторного сжатия файл всё ещё превышает лимит Telegram. "
                "Попробуй более короткое видео или исходник меньшего размера."
            )
        return probe_video(destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
