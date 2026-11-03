"""Real FFmpeg runs on small colour test clips generated locally (lavfi), nothing is downloaded."""
import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.media import (
    FFmpegTimeoutError,
    MediaError,
    make_thumbnail,
    probe_video,
    process_video,
    run_ffmpeg,
    sha256_file,
)
from app.unique import UniqueOptions, generate_params, output_duration, process_unique

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg не установлен"
)
MB = 1024 * 1024


def make_video(path: Path, *, size: str = "640x360", duration: float = 3.5, audio: bool = True,
               video_args: tuple[str, ...] = ("-pix_fmt", "yuv420p")) -> Path:
    command = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
               "-i", f"testsrc2=size={size}:rate=30:duration={duration}"]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={duration}"]
    command += ["-c:v", "libx264", *video_args]
    if audio:
        command += ["-c:a", "aac", "-shortest"]
    command.append(str(path))
    subprocess.run(command, check=True)
    return path


def streams(path: Path) -> list[dict]:
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True,
    ).stdout
    return json.loads(output)["streams"]


def audio_rate(path: Path) -> int | None:
    audio = [s for s in streams(path) if s["codec_type"] == "audio"]
    return int(audio[0]["sample_rate"]) if audio else None


@pytest.fixture(scope="module")
def sample(tmp_path_factory) -> Path:
    return make_video(tmp_path_factory.mktemp("media") / "sample.mp4")


@pytest.mark.parametrize("key,expected", [
    ("reels_fill", (1080, 1920)), ("reels_fit", (1080, 1920)), ("shorts_fill", (1080, 1920)),
    ("square", (1080, 1080)), ("keep", (640, 360)), ("warm", (640, 360)),
])
def test_presets_produce_expected_frames(sample, tmp_path, key, expected):
    info = probe_video(sample)
    out = tmp_path / f"{key}.mp4"
    result = asyncio.run(process_video(sample, out, key, info=info, timeout=120, max_output_bytes=47 * MB))
    assert (result.width, result.height) == expected
    assert abs(result.duration - info.duration) < 0.2
    assert audio_rate(out) == 48000  # not 96 kHz after loudnorm


@pytest.mark.parametrize("options,expected", [
    (UniqueOptions(intensity="light"), (640, 360)),
    (UniqueOptions(intensity="medium", fmt="9:16", mirror=True), (1080, 1920)),
    (UniqueOptions(intensity="strong", fmt="1:1"), (1080, 1080)),
    (UniqueOptions(intensity="strong", speed=False), (640, 360)),
])
def test_unique_copy_is_valid(sample, tmp_path, options, expected):
    info = probe_video(sample)
    params = generate_params(options, info, seed=2024)
    out = tmp_path / "unique.mp4"
    result = asyncio.run(process_unique(sample, out, params, info=info, timeout=120, max_output_bytes=47 * MB))
    assert (result.width, result.height) == expected
    assert abs(result.duration - output_duration(params, info)) < 0.15
    assert audio_rate(out) == params.sample_rate
    data = out.read_bytes()
    assert b"x264 - core" not in data and b"Lavf" not in data


def test_two_copies_are_different_files(sample, tmp_path):
    info = probe_video(sample)
    hashes = set()
    for seed in (1, 2):
        out = tmp_path / f"copy{seed}.mp4"
        params = generate_params(UniqueOptions(), info, seed=seed)
        asyncio.run(process_unique(sample, out, params, info=info, timeout=120, max_output_bytes=47 * MB))
        hashes.add(sha256_file(out))
    assert len(hashes) == 2 and sha256_file(sample) not in hashes


def test_result_is_recompressed_to_fit_limit(sample, tmp_path):
    info = probe_video(sample)
    out = tmp_path / "fit.mp4"
    limit = 400_000
    result = asyncio.run(process_video(sample, out, "reels_fill", info=info, timeout=120, max_output_bytes=limit))
    assert out.stat().st_size <= limit
    assert (result.width, result.height) == (720, 1280)  # fallback size


def test_too_long_for_limit_is_reported(sample, tmp_path):
    info = probe_video(sample)
    out = tmp_path / "tiny.mp4"
    with pytest.raises(MediaError):
        asyncio.run(process_video(sample, out, "reels_fill", info=info, timeout=120, max_output_bytes=60_000))
    assert not out.exists()


def test_progress_is_reported(sample, tmp_path):
    info = probe_video(sample)
    seen: list[float] = []
    asyncio.run(process_video(sample, tmp_path / "p.mp4", "keep", info=info, timeout=120,
                              max_output_bytes=47 * MB, on_progress=seen.append))
    assert seen and max(seen) > 0.8 and all(0 <= value <= 1 for value in seen)


def test_timeout_kills_ffmpeg():
    started = time.monotonic()
    with pytest.raises(FFmpegTimeoutError):
        asyncio.run(run_ffmpeg(
            ["-re", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=600", "-f", "null", "-"],
            timeout=1.5,
        ))
    assert time.monotonic() - started < 10


def test_cancel_kills_ffmpeg():
    async def scenario():
        task = asyncio.create_task(run_ffmpeg(
            ["-re", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=600", "-f", "null", "-"],
            timeout=600,
        ))
        await asyncio.sleep(0.7)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    started = time.monotonic()
    asyncio.run(scenario())
    assert time.monotonic() - started < 10


def test_video_without_audio(tmp_path):
    silent = make_video(tmp_path / "silent.mp4", audio=False)
    info = probe_video(silent)
    assert not info.has_audio
    out = tmp_path / "silent_out.mp4"
    asyncio.run(process_video(silent, out, "warm", info=info, timeout=120, max_output_bytes=47 * MB))
    assert audio_rate(out) is None
    params = generate_params(UniqueOptions(intensity="strong"), info, seed=9)
    asyncio.run(process_unique(silent, out, params, info=info, timeout=120, max_output_bytes=47 * MB))
    assert audio_rate(out) is None


def test_odd_size_and_non_square_pixels(tmp_path):
    odd = make_video(tmp_path / "odd.mp4", size="900x500", audio=False,
                     video_args=("-vf", "scale=887:501", "-pix_fmt", "yuv444p"))
    info = probe_video(odd)
    assert abs(info.sar - 1.0) > 0.01
    out = tmp_path / "odd_out.mp4"
    result = asyncio.run(process_video(odd, out, "keep", info=info, timeout=120, max_output_bytes=47 * MB))
    assert result.width % 2 == 0 and result.height % 2 == 0
    params = generate_params(UniqueOptions(intensity="medium"), info, seed=4)
    result = asyncio.run(process_unique(odd, out, params, info=info, timeout=120, max_output_bytes=47 * MB))
    assert result.width % 2 == 0 and result.height % 2 == 0


def test_rotated_phone_video(tmp_path):
    help_text = subprocess.run(["ffmpeg", "-hide_banner", "-h", "full"], capture_output=True, text=True).stdout
    if "display_rotation" not in help_text:
        pytest.skip("this FFmpeg cannot write rotation metadata")
    plain = make_video(tmp_path / "plain.mp4", size="1280x720", duration=2)
    rotated = tmp_path / "rotated.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-display_rotation", "90",
                    "-i", str(plain), "-c", "copy", str(rotated)], check=True)
    info = probe_video(rotated)
    assert (info.width, info.height) == (720, 1280)
    out = tmp_path / "rot_out.mp4"
    result = asyncio.run(process_video(rotated, out, "keep", info=info, timeout=120, max_output_bytes=47 * MB))
    assert (result.width, result.height) == (720, 1280)
    params = generate_params(UniqueOptions(), info, seed=11)
    result = asyncio.run(process_unique(rotated, out, params, info=info, timeout=120, max_output_bytes=47 * MB))
    assert (result.width, result.height) == (720, 1280)


def test_thumbnail_is_small_jpeg(sample, tmp_path):
    thumb = asyncio.run(make_thumbnail(sample, tmp_path / "thumb.jpg", 3.5))
    assert thumb is not None and thumb.stat().st_size < 200 * 1024
    assert thumb.read_bytes()[:2] == b"\xff\xd8"
