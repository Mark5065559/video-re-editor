"""Uniquelizer: makes technically different versions of the user's own video.

Every copy gets its own random set of small changes: zoom and frame shift, a slight
rotation, colour, grain, sharpness, speed, trimmed edges, audio level/EQ, fresh
metadata and different encoder settings. All values put into FFmpeg filters are
numbers generated here, never text coming from the user.

This does not promise anything about how social platforms treat the result.
"""
from __future__ import annotations

import math
import random
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .media import (
    MAX_FPS,
    NORMAL_PROFILE,
    EncodeProfile,
    ProgressCallback,
    VideoInfo,
    encode_with_size_limit,
    sar_fix_filter,
    video_rate_args,
)

INTENSITIES: dict[str, str] = {
    "light": "🔅 Лёгкая",
    "medium": "⚖️ Средняя",
    "strong": "🔥 Сильная",
}
FORMATS: dict[str, str] = {
    "original": "исходный",
    "9:16": "9:16 (Reels/Shorts/TikTok)",
    "1:1": "1:1 (квадрат)",
}
FORMAT_SIZES: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    # format: (normal size, fallback size used when the file must be smaller)
    "9:16": ((1080, 1920), (720, 1280)),
    "1:1": ((1080, 1080), (720, 720)),
}
COPY_CHOICES: tuple[int, ...] = (1, 2, 3, 5)
EQ_FREQUENCIES: tuple[int, ...] = (250, 500, 1000, 2000, 4000)
# value -> exact rational for FFmpeg
STANDARD_FPS: dict[float, str] = {
    23.976: "24000/1001", 24.0: "24", 25.0: "25", 29.97: "30000/1001", 30.0: "30",
    48.0: "48", 50.0: "50", 59.94: "60000/1001", 60.0: "60",
}


@dataclass
class UniqueOptions:
    """What the user picked in the uniquelizer panel."""

    intensity: str = "medium"
    copies: int = 1
    mirror: bool = False
    speed: bool = True
    fmt: str = "original"

    def normalized(self, max_copies: int) -> "UniqueOptions":
        return UniqueOptions(
            intensity=self.intensity if self.intensity in INTENSITIES else "medium",
            copies=min(max(1, int(self.copies)), max(1, max_copies)),
            mirror=bool(self.mirror),
            speed=bool(self.speed),
            fmt=self.fmt if self.fmt in FORMATS else "original",
        )


@dataclass(frozen=True)
class Profile:
    zoom: tuple[float, float]
    rotate: tuple[float, float]
    brightness: tuple[float, float]
    contrast: tuple[float, float]
    saturation: tuple[float, float]
    gamma: tuple[float, float]
    hue: tuple[float, float]
    noise: tuple[int, int]
    sharpen: tuple[float, float]
    vignette: tuple[float, float] | None
    speed: tuple[float, float]
    trim: tuple[float, float]
    volume_db: tuple[float, float]
    eq_db: tuple[float, float]
    crf: tuple[int, int]


PROFILES: dict[str, Profile] = {
    "light": Profile(
        zoom=(1.015, 1.03), rotate=(0.0, 0.0), brightness=(0.005, 0.015), contrast=(1.0, 1.03),
        saturation=(0.98, 1.06), gamma=(1.0, 1.0), hue=(0.5, 2.0), noise=(0, 2), sharpen=(0.0, 0.3),
        vignette=None, speed=(1.01, 1.025), trim=(0.0, 0.12), volume_db=(0.2, 0.6), eq_db=(0.5, 1.0),
        crf=(21, 23),
    ),
    "medium": Profile(
        zoom=(1.025, 1.045), rotate=(0.3, 0.6), brightness=(0.01, 0.03), contrast=(0.98, 1.06),
        saturation=(0.95, 1.10), gamma=(0.97, 1.03), hue=(1.5, 4.0), noise=(2, 5), sharpen=(-0.2, 0.5),
        vignette=None, speed=(1.02, 1.04), trim=(0.05, 0.25), volume_db=(0.4, 1.0), eq_db=(0.8, 1.6),
        crf=(21, 24),
    ),
    "strong": Profile(
        zoom=(1.04, 1.07), rotate=(0.7, 1.5), brightness=(0.02, 0.045), contrast=(0.96, 1.10),
        saturation=(0.92, 1.16), gamma=(0.94, 1.06), hue=(3.0, 7.0), noise=(4, 8), sharpen=(-0.3, 0.7),
        vignette=(0.2, 0.4), speed=(1.03, 1.06), trim=(0.1, 0.4), volume_db=(0.6, 1.5), eq_db=(1.2, 2.5),
        crf=(20, 24),
    ),
}


@dataclass(frozen=True)
class UniqueParams:
    """Concrete random values for one copy."""

    seed: int
    intensity: str
    fmt: str
    mirror: bool
    zoom: float  # total zoom before cropping back to the frame size (covers rotation too)
    shift_x: float  # crop window offset from the centre, as a share of the frame width
    shift_y: float
    rotate_deg: float
    brightness: float
    contrast: float
    saturation: float
    gamma: float
    hue_deg: float
    noise: int
    sharpen: float
    vignette: float  # radians, 0 = off
    speed: float
    trim_start: float
    trim_end: float
    volume_db: float
    eq_freq: int
    eq_gain_db: float
    crf: int
    gop_seconds: float
    bframes: int
    sample_rate: int
    audio_kbps: int
    creation_time: str


def rotation_zoom(angle_deg: float, width: int, height: int) -> float:
    """Zoom needed so a frame rotated by angle_deg has no black corners."""
    theta = math.radians(abs(angle_deg))
    if theta == 0 or width <= 0 or height <= 0:
        return 1.0
    ratio = max(width / height, height / width)
    return math.cos(theta) + ratio * math.sin(theta)


def _signed(rng: random.Random, bounds: tuple[float, float]) -> float:
    return rng.uniform(*bounds) * rng.choice((-1.0, 1.0))


def generate_params(
    options: UniqueOptions, info: VideoInfo, *, seed: int | None = None, now: datetime | None = None,
    encode: EncodeProfile = NORMAL_PROFILE,
) -> UniqueParams:
    seed = secrets.randbits(32) if seed is None else seed
    rng = random.Random(seed)
    profile = PROFILES.get(options.intensity, PROFILES["medium"])

    rotate = _signed(rng, profile.rotate) if profile.rotate[1] > 0 else 0.0
    zoom_extra = rng.uniform(*profile.zoom)
    zoom = rotation_zoom(rotate, info.width, info.height) * zoom_extra
    # The crop window may move only inside the area free of rotation corners.
    max_shift = (zoom_extra - 1.0) / 2 * 0.9
    shift_x = rng.uniform(-1.0, 1.0) * max_shift
    shift_y = rng.uniform(-1.0, 1.0) * max_shift

    trim_start = trim_end = 0.0
    if info.duration > 3:
        trim_start = rng.uniform(*profile.trim)
        trim_end = rng.uniform(*profile.trim)
        max_total = max(0.0, info.duration - max(1.0, info.duration * 0.9))
        if trim_start + trim_end > max_total:
            scale = max_total / (trim_start + trim_end) if trim_start + trim_end else 0.0
            trim_start, trim_end = trim_start * scale, trim_end * scale

    vignette = 0.0
    if profile.vignette and rng.random() < 0.6:
        vignette = rng.uniform(*profile.vignette)

    moment = (now or datetime.now(timezone.utc)) - timedelta(seconds=rng.randint(0, 3600))
    noise = rng.randint(*profile.noise)
    sharpen = round(rng.uniform(*profile.sharpen), 3)
    if not encode.heavy_filters:
        # Weak server: skip the CPU-hungry filters, keep zoom/shift/colour/speed/audio/metadata.
        zoom, rotate, noise, sharpen, vignette = zoom_extra, 0.0, 0, 0.0, 0.0
    return UniqueParams(
        seed=seed,
        intensity=options.intensity if options.intensity in PROFILES else "medium",
        fmt=options.fmt if options.fmt in FORMATS else "original",
        mirror=bool(options.mirror),
        zoom=round(zoom, 5),
        shift_x=round(shift_x, 5),
        shift_y=round(shift_y, 5),
        rotate_deg=round(rotate, 3),
        brightness=round(_signed(rng, profile.brightness), 4),
        contrast=round(rng.uniform(*profile.contrast), 4),
        saturation=round(rng.uniform(*profile.saturation), 4),
        gamma=round(rng.uniform(*profile.gamma), 4),
        hue_deg=round(_signed(rng, profile.hue), 2),
        noise=noise,
        sharpen=sharpen,
        vignette=round(vignette, 4),
        speed=round(rng.uniform(*profile.speed), 4) if options.speed else 1.0,
        trim_start=round(trim_start, 3),
        trim_end=round(trim_end, 3),
        volume_db=round(_signed(rng, profile.volume_db), 2),
        eq_freq=rng.choice(EQ_FREQUENCIES),
        eq_gain_db=round(_signed(rng, profile.eq_db), 2),
        crf=rng.randint(*profile.crf),
        gop_seconds=rng.choice((1.5, 2.0, 2.5, 3.0, 4.0)),
        bframes=rng.choice((2, 3)),
        sample_rate=rng.choice((44100, 48000)),
        audio_kbps=rng.choice((128, 160)),
        creation_time=moment.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
    )


def output_fps(source_fps: float) -> tuple[float, str]:
    """Constant output frame rate: the source rate snapped to a standard value, at most 60."""
    if source_fps <= 0:
        return 30.0, "30"
    fps = min(source_fps, float(MAX_FPS))
    nearest = min(STANDARD_FPS, key=lambda value: abs(value - fps))
    if abs(nearest - fps) / fps < 0.03:
        return nearest, STANDARD_FPS[nearest]
    fps = max(1.0, round(fps, 3))
    return fps, f"{fps:g}"


def output_duration(params: UniqueParams, info: VideoInfo) -> float:
    kept = max(0.5, info.duration - params.trim_start - params.trim_end)
    return kept / params.speed


def build_unique_video_filter(
    params: UniqueParams, info: VideoInfo, *, fallback: bool = False, encode: EncodeProfile = NORMAL_PROFILE,
) -> str:
    parts: list[str] = []
    sar_fix = sar_fix_filter(info)
    if sar_fix:
        parts.append(sar_fix)
    box = encode.fallback_long_side if fallback else encode.long_side
    parts.append(
        f"scale='min(iw,{box})':'min(ih,{box})':force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    if params.mirror:
        parts.append("hflip")
    if params.rotate_deg:
        parts.append(f"rotate={math.radians(params.rotate_deg):.6f}:ow=iw:oh=ih:c=black")
    if params.zoom > 1.0005:
        z = f"{params.zoom:.5f}"
        # ceil here + trunc in crop gives back exactly the frame size before zooming
        parts.append(f"scale='ceil(iw*{z}/2)*2':'ceil(ih*{z}/2)*2'")
        parts.append(
            f"crop='trunc(iw/{z}/2)*2':'trunc(ih/{z}/2)*2'"
            f":'(iw-ow)/2{params.shift_x:+.5f}*ow':'(ih-oh)/2{params.shift_y:+.5f}*oh'"
        )
    if params.fmt in FORMAT_SIZES:
        width, height = encode.fill_size(params.fmt, fallback)
        parts.append(f"scale={width}:{height}:force_original_aspect_ratio=increase")
        parts.append(f"crop={width}:{height}")
    parts.append(
        f"eq=brightness={params.brightness:.4f}:contrast={params.contrast:.4f}"
        f":saturation={params.saturation:.4f}:gamma={params.gamma:.4f}"
    )
    if params.hue_deg:
        parts.append(f"hue=h={params.hue_deg:.2f}")
    if params.sharpen:
        parts.append(f"unsharp=5:5:{params.sharpen:.3f}:5:5:0")
    if params.noise:
        parts.append(f"noise=alls={params.noise}:allf=t")
    if params.vignette:
        parts.append(f"vignette=angle={params.vignette:.4f}")
    if params.speed != 1.0:
        parts.append(f"setpts=(PTS-STARTPTS)/{params.speed:.4f}")
    # Constant frame rate: keeps sound in sync after the speed change and caps 120/240 fps.
    parts.append(f"fps={output_fps(info.fps)[1]}")
    parts.append("setsar=1")
    parts.append("format=yuv420p")
    return ",".join(parts)


def build_unique_audio_filter(params: UniqueParams) -> str:
    parts = ["asetpts=PTS-STARTPTS"]
    if params.speed != 1.0:
        parts.append(f"atempo={params.speed:.4f}")
    if params.eq_gain_db:
        parts.append(f"equalizer=f={params.eq_freq}:t=q:w=1.2:g={params.eq_gain_db:.2f}")
    if params.volume_db:
        parts.append(f"volume={params.volume_db:.2f}dB")
    parts.append(f"aresample={params.sample_rate}")
    return ",".join(parts)


def build_unique_args(
    source: Path, destination: Path, params: UniqueParams, *, info: VideoInfo, video_kbps: int | None = None,
    encode: EncodeProfile = NORMAL_PROFILE,
) -> list[str]:
    fallback = video_kbps is not None
    kept = max(0.5, info.duration - params.trim_start - params.trim_end)
    gop = max(10, round(params.gop_seconds * output_fps(info.fps)[0]))
    args: list[str] = [*encode.thread_args()]
    if params.trim_start > 0:
        args.extend(["-ss", f"{params.trim_start:.3f}"])
    args.extend(["-t", f"{kept:.3f}", "-i", str(source)])
    args.extend([
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", build_unique_video_filter(params, info, fallback=fallback, encode=encode),
        "-c:v", "libx264", "-preset", encode.x264_preset,
        *video_rate_args(params.crf + encode.crf_offset, video_kbps),
        "-g", str(gop), *(["-bf", str(params.bframes)] if encode.heavy_filters else []), "-pix_fmt", "yuv420p",
        # Drop encoder info SEI (x264 version/options) from the video stream.
        "-bsf:v", "filter_units=remove_types=6",
    ])
    if info.has_audio:
        args.extend([
            "-af", build_unique_audio_filter(params),
            "-c:a", "aac", "-b:a", f"{96 if fallback else params.audio_kbps}k",
            "-ar", str(params.sample_rate), "-ac", "2",
        ])
    args.extend([
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-metadata", f"creation_time={params.creation_time}",
        # No FFmpeg version strings in the file.
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        "-movflags", "+faststart", "-max_muxing_queue_size", "1024", str(destination),
    ])
    return args


async def process_unique(
    source: Path,
    destination: Path,
    params: UniqueParams,
    *,
    info: VideoInfo,
    timeout: int,
    max_output_bytes: int,
    on_progress: ProgressCallback | None = None,
    encode: EncodeProfile = NORMAL_PROFILE,
) -> VideoInfo:
    return await encode_with_size_limit(
        lambda kbps: build_unique_args(source, destination, params, info=info, video_kbps=kbps, encode=encode),
        destination, output_duration=output_duration(params, info), audio_kbps_fallback=96,
        timeout=timeout, max_output_bytes=max_output_bytes, on_progress=on_progress,
    )


def describe(params: UniqueParams) -> str:
    """Short human summary of what was changed in this copy."""
    items = [f"зум +{(params.zoom - 1) * 100:.1f}%"]
    if params.shift_x or params.shift_y:
        items.append("сдвиг кадра")
    if params.rotate_deg:
        items.append(f"поворот {params.rotate_deg:+.1f}°")
    if params.mirror:
        items.append("зеркало")
    if params.fmt in FORMAT_SIZES:
        items.append(f"формат {params.fmt}")
    items.append("цвет/контраст")
    if params.hue_deg:
        items.append(f"оттенок {params.hue_deg:+.1f}°")
    if params.noise:
        items.append(f"зерно {params.noise}")
    if params.vignette:
        items.append("виньетка")
    if params.speed != 1.0:
        items.append(f"скорость ×{params.speed:.3f}")
    if params.trim_start or params.trim_end:
        items.append(f"обрезка {params.trim_start:.2f}/{params.trim_end:.2f} с")
    items.append(f"звук {params.volume_db:+.1f} дБ")
    items.append("новые метаданные")
    return ", ".join(items)
