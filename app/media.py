from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

# Called with a value from 0.0 to 1.0 while FFmpeg works. May be sync or async.
ProgressCallback = Callable[[float], "Awaitable[None] | None"]
# Builds the FFmpeg arguments; called with video_kbps=None for the normal pass and
# with a bitrate cap when the first result was larger than the size limit.
ArgsBuilder = Callable[[int | None], list[str]]

AUDIO_SAMPLE_RATE = 48000
MAX_LONG_SIDE = 1920
FALLBACK_LONG_SIDE = 1280
MAX_FPS = 60
MIN_FALLBACK_VIDEO_KBPS = 250


@dataclass(frozen=True)
class EncodeProfile:
    """How heavy the processing may be. "lite" is for weak free servers (0.1 CPU, 512 MB)."""

    name: str = "normal"
    long_side: int = MAX_LONG_SIDE
    fallback_long_side: int = FALLBACK_LONG_SIDE
    vertical: tuple[int, int] = (1080, 1920)
    vertical_fallback: tuple[int, int] = (720, 1280)
    square: tuple[int, int] = (1080, 1080)
    square_fallback: tuple[int, int] = (720, 720)
    x264_preset: str = "veryfast"
    crf_offset: int = 0
    threads: int = 0  # 0 = FFmpeg decides
    heavy_filters: bool = True  # rotation, grain, sharpening, vignette in the uniquelizer

    def fill_size(self, aspect: str, fallback: bool) -> tuple[int, int]:
        if aspect == "9:16":
            return self.vertical_fallback if fallback else self.vertical
        return self.square_fallback if fallback else self.square

    def thread_args(self) -> list[str]:
        if not self.threads:
            return []
        return ["-threads", str(self.threads), "-filter_threads", str(self.threads)]


NORMAL_PROFILE = EncodeProfile()
LITE_PROFILE = EncodeProfile(
    name="lite", long_side=1280, fallback_long_side=960, vertical=(720, 1280), vertical_fallback=(540, 960),
    square=(720, 720), square_fallback=(540, 540), x264_preset="ultrafast", crf_offset=2, threads=1,
    heavy_filters=False,
)


class MediaError(RuntimeError):
    """Problem with the video itself or with FFmpeg; the message is shown to the user."""


class FFmpegNotFoundError(MediaError):
    pass


class FFmpegTimeoutError(MediaError, TimeoutError):
    pass


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    width: int  # display size: rotation from phone metadata is already applied
    height: int
    has_audio: bool
    video_codec: str
    audio_codec: str | None
    size_bytes: int
    fps: float = 0.0
    rotation: int = 0
    sar: float = 1.0


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


def _decode(data: bytes) -> str:
    # FFmpeg prints file metadata as UTF-8; never trust the console code page (cp1251 on Windows).
    return data.decode("utf-8", errors="replace")


def _run_sync(command: list[str], timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise FFmpegNotFoundError("Не найден FFmpeg/FFprobe. Установи FFmpeg и добавь его в PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegTimeoutError("FFprobe слишком долго анализирует файл") from exc
    if result.returncode != 0:
        tail = _decode(result.stderr or result.stdout)[-1200:]
        raise MediaError(f"Не удалось прочитать видео: {tail.strip()}")
    return _decode(result.stdout)


def _parse_rate(value: Any) -> float:
    try:
        num, _, den = str(value).partition("/")
        return float(num) / float(den or 1) if float(den or 1) else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _parse_sar(value: Any) -> float:
    try:
        num, _, den = str(value).partition(":")
        sar = float(num) / float(den)
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0
    return sar if 0.2 < sar < 5 else 1.0


def _parse_rotation(stream: dict[str, Any]) -> int:
    rotation = 0.0
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and "rotation" in side_data:
            try:
                rotation = float(side_data["rotation"])
            except (TypeError, ValueError):
                pass
    if not rotation:
        try:
            rotation = float((stream.get("tags") or {}).get("rotate", 0))
        except (TypeError, ValueError):
            rotation = 0.0
    return int(round(rotation / 90.0)) * 90 % 360


def parse_probe(data: dict[str, Any], size_bytes: int) -> VideoInfo:
    streams = data.get("streams") or []
    video = next(
        (s for s in streams if s.get("codec_type") == "video"
         and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if not video:
        raise MediaError("В файле не найдена видеодорожка")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        for stream in streams:
            try:
                duration = max(duration, float(stream.get("duration") or 0))
            except (TypeError, ValueError):
                continue

    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if duration <= 0 or not width or not height:
        raise MediaError("Не удалось определить длительность или разрешение видео")

    rotation = _parse_rotation(video)
    if rotation in (90, 270):
        width, height = height, width
    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    if fps > 1000:  # nonsense values from some containers
        fps = 0.0
    return VideoInfo(
        duration=duration, width=width, height=height, has_audio=audio is not None,
        video_codec=str(video.get("codec_name", "unknown")),
        audio_codec=str(audio.get("codec_name")) if audio else None,
        size_bytes=size_bytes, fps=round(fps, 3), rotation=rotation,
        sar=_parse_sar(video.get("sample_aspect_ratio")),
    )


def probe_video(path: Path) -> VideoInfo:
    if not path.exists() or path.stat().st_size == 0:
        raise MediaError("Файл пустой или не найден")
    output = _run_sync([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path),
    ], timeout=60)
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise MediaError("FFprobe вернул непонятный ответ") from exc
    return parse_probe(data, path.stat().st_size)


def even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def sar_fix_filter(info: VideoInfo | None) -> str | None:
    """Anamorphic sources (non-square pixels) are converted to square pixels first."""
    if info is not None and abs(info.sar - 1.0) > 0.01:
        return "scale='trunc(iw*sar/2)*2':ih,setsar=1"
    return None


def fps_cap_filter(source_fps: float, speed: float = 1.0) -> str | None:
    if source_fps * speed > MAX_FPS + 0.5:
        return f"fps={MAX_FPS}"
    return None


def build_video_filter(
    preset: Preset, fallback: bool = False, *, info: VideoInfo | None = None,
    profile: EncodeProfile = NORMAL_PROFILE,
) -> str:
    parts: list[str] = []
    sar_fix = sar_fix_filter(info)
    if sar_fix:
        parts.append(sar_fix)
    if preset.framing == "fill":
        target_w, target_h = profile.fill_size(preset.aspect, fallback)
        parts.extend([
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase",
            f"crop={target_w}:{target_h}",
        ])
    elif preset.framing == "fit":
        target_w, target_h = profile.fill_size("9:16", fallback)
        parts.extend([
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black",
        ])
    else:
        # Fit into a box without upscaling; the longer side is limited for both
        # landscape and portrait videos, and the size always stays even for H.264.
        box = profile.fallback_long_side if fallback else profile.long_side
        parts.append(
            f"scale='min(iw,{box})':'min(ih,{box})':force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
    if preset.color_filter:
        parts.append(preset.color_filter)
    fps_cap = fps_cap_filter(info.fps if info else 0.0)
    if fps_cap:
        parts.append(fps_cap)
    parts.append("setsar=1")
    return ",".join(parts)


def video_rate_args(crf: int, video_kbps: int | None) -> list[str]:
    if video_kbps is None:
        return ["-crf", str(crf)]
    return ["-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.3)}k", "-bufsize", f"{video_kbps * 2}k"]


def build_preset_args(
    source: Path, destination: Path, preset: Preset, *, info: VideoInfo, video_kbps: int | None = None,
    profile: EncodeProfile = NORMAL_PROFILE,
) -> list[str]:
    fallback = video_kbps is not None
    args = [
        *profile.thread_args(),
        "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", build_video_filter(preset, fallback, info=info, profile=profile),
        "-c:v", "libx264", "-preset", profile.x264_preset, *video_rate_args(23 + profile.crf_offset, video_kbps),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k" if fallback else "128k",
        "-map_metadata", "-1", "-map_chapters", "-1",
    ]
    if info.has_audio:
        audio_filters = []
        if preset.normalize_audio:
            audio_filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
        # loudnorm works at 192 kHz internally; bring it back to a normal rate.
        audio_filters.append(f"aresample={AUDIO_SAMPLE_RATE}")
        args.extend(["-af", ",".join(audio_filters), "-ar", str(AUDIO_SAMPLE_RATE)])
    args.extend(["-movflags", "+faststart", "-max_muxing_queue_size", "1024", str(destination)])
    return args


async def _maybe_await(value: Any) -> None:
    if inspect.isawaitable(value):
        await value


def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def run_ffmpeg(
    args: list[str], *, timeout: float, duration: float | None = None,
    on_progress: ProgressCallback | None = None,
) -> None:
    """Runs FFmpeg without a shell. Kills it on timeout or task cancellation."""
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-progress", "pipe:1", "-nostats", *args,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=1 << 20,
        )
    except FileNotFoundError as exc:
        raise FFmpegNotFoundError("Не найден FFmpeg. Установи FFmpeg и добавь его в PATH.") from exc

    stderr_tail: deque[str] = deque(maxlen=30)

    async def read_stderr() -> None:
        assert process.stderr is not None
        async for line in process.stderr:
            text = _decode(line).strip()
            if text:
                stderr_tail.append(text)

    async def read_progress() -> None:
        assert process.stdout is not None
        async for line in process.stdout:
            key, _, value = _decode(line).strip().partition("=")
            if on_progress and duration and key in ("out_time_us", "out_time_ms") and value.isdigit():
                # Both keys are in microseconds (out_time_ms is misnamed in FFmpeg).
                try:
                    await _maybe_await(on_progress(min(1.0, int(value) / 1_000_000 / duration)))
                except Exception:  # progress display must never break processing
                    log.debug("progress callback failed", exc_info=True)

    try:
        await asyncio.wait_for(asyncio.gather(read_stderr(), read_progress(), process.wait()), timeout)
    except asyncio.TimeoutError as exc:
        _kill(process)
        await process.wait()
        raise FFmpegTimeoutError("FFmpeg превысил допустимое время обработки") from exc
    except BaseException:
        _kill(process)
        try:
            await asyncio.shield(process.wait())
        except BaseException:
            pass
        raise
    if process.returncode != 0:
        tail = "\n".join(stderr_tail)[-1200:]
        raise MediaError(f"FFmpeg завершился с ошибкой (код {process.returncode}): {tail}")


def fallback_video_kbps(max_output_bytes: int, duration: float, audio_kbps: int) -> int:
    total_kbps = max_output_bytes * 8 / max(duration, 0.5) / 1000 * 0.92
    return int(total_kbps - audio_kbps)


async def encode_with_size_limit(
    build_args: ArgsBuilder, destination: Path, *, output_duration: float, audio_kbps_fallback: int,
    timeout: int, max_output_bytes: int, on_progress: ProgressCallback | None = None,
) -> VideoInfo:
    """Encodes once; if the file is over the limit, re-encodes with a bitrate that fits."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    def remaining() -> float:
        left = deadline - loop.time()
        if left <= 1:
            raise FFmpegTimeoutError("FFmpeg превысил допустимое время обработки")
        return left

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        await run_ffmpeg(build_args(None), timeout=remaining(), duration=output_duration, on_progress=on_progress)
        if not destination.exists() or destination.stat().st_size == 0:
            raise MediaError("FFmpeg не создал готовый файл")
        if destination.stat().st_size > max_output_bytes:
            destination.unlink(missing_ok=True)
            kbps = fallback_video_kbps(max_output_bytes, output_duration, audio_kbps_fallback)
            if kbps < MIN_FALLBACK_VIDEO_KBPS:
                raise MediaError(
                    f"Видео слишком длинное, чтобы уложиться в лимит {max_output_bytes // (1024 * 1024)} МБ даже со сжатием. "
                    "Пришли ролик короче."
                )
            log.info("Результат больше лимита, пережимаю с битрейтом %s kbps", kbps)
            await run_ffmpeg(build_args(kbps), timeout=remaining(), duration=output_duration, on_progress=on_progress)
        if not destination.exists() or destination.stat().st_size == 0:
            raise MediaError("Не удалось создать итоговый файл")
        if destination.stat().st_size > max_output_bytes:
            raise MediaError(
                "После повторного сжатия файл всё ещё превышает лимит Telegram. "
                "Попробуй более короткое видео или исходник меньшего размера."
            )
        return await asyncio.to_thread(probe_video, destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


async def process_video(
    source: Path,
    destination: Path,
    preset_key: str,
    *,
    info: VideoInfo,
    timeout: int,
    max_output_bytes: int,
    on_progress: ProgressCallback | None = None,
    profile: EncodeProfile = NORMAL_PROFILE,
) -> VideoInfo:
    if preset_key not in PRESETS:
        raise ValueError("Неизвестный режим обработки")
    preset = PRESETS[preset_key]
    return await encode_with_size_limit(
        lambda kbps: build_preset_args(source, destination, preset, info=info, video_kbps=kbps, profile=profile),
        destination, output_duration=info.duration, audio_kbps_fallback=96,
        timeout=timeout, max_output_bytes=max_output_bytes, on_progress=on_progress,
    )


async def make_thumbnail(video: Path, destination: Path, duration: float) -> Path | None:
    """JPEG preview for Telegram (≤320 px). Best effort: returns None on any problem."""
    position = f"{min(1.0, max(0.0, duration / 3)):.2f}"
    try:
        await run_ffmpeg([
            "-ss", position, "-i", str(video), "-frames:v", "1",
            "-vf", "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", "5", str(destination),
        ], timeout=60)
    except (MediaError, OSError):
        log.warning("Не удалось сделать превью для %s", video.name, exc_info=True)
        return None
    if destination.exists() and 0 < destination.stat().st_size < 200 * 1024:
        return destination
    destination.unlink(missing_ok=True)
    return None
