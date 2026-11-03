import re

import pytest

from app.media import VideoInfo
from app.unique import (
    FORMATS,
    INTENSITIES,
    PROFILES,
    UniqueOptions,
    build_unique_args,
    build_unique_audio_filter,
    build_unique_video_filter,
    describe,
    generate_params,
    output_duration,
    output_fps,
    rotation_zoom,
)

SAFE_FILTER = re.compile(r"^[A-Za-z0-9_=:.,'()*/+\-]+$")


def make_info(**overrides) -> VideoInfo:
    values = dict(duration=30.0, width=1080, height=1920, has_audio=True, video_codec="h264",
                  audio_codec="aac", size_bytes=50_000_000, fps=30.0)
    values.update(overrides)
    return VideoInfo(**values)


def test_same_seed_gives_same_copy_and_different_seeds_differ():
    info = make_info()
    first = generate_params(UniqueOptions(), info, seed=7)
    assert first == generate_params(UniqueOptions(), info, seed=7)
    others = {generate_params(UniqueOptions(), info, seed=seed) for seed in range(8, 20)}
    assert first not in others and len(others) == 12


@pytest.mark.parametrize("intensity", list(INTENSITIES))
def test_values_stay_inside_profile(intensity):
    profile = PROFILES[intensity]
    info = make_info()
    for seed in range(300):
        p = generate_params(UniqueOptions(intensity=intensity), info, seed=seed)
        assert profile.rotate[0] <= abs(p.rotate_deg) <= profile.rotate[1]
        assert p.zoom >= rotation_zoom(p.rotate_deg, info.width, info.height) * profile.zoom[0] - 1e-4
        assert p.zoom <= rotation_zoom(p.rotate_deg, info.width, info.height) * profile.zoom[1] + 1e-4
        assert abs(p.shift_x) <= (profile.zoom[1] - 1) / 2 and abs(p.shift_y) <= (profile.zoom[1] - 1) / 2
        assert profile.brightness[0] - 1e-4 <= abs(p.brightness) <= profile.brightness[1] + 1e-4
        assert profile.contrast[0] - 1e-4 <= p.contrast <= profile.contrast[1] + 1e-4
        assert profile.saturation[0] - 1e-4 <= p.saturation <= profile.saturation[1] + 1e-4
        assert profile.hue[0] - 0.01 <= abs(p.hue_deg) <= profile.hue[1] + 0.01
        assert profile.noise[0] <= p.noise <= profile.noise[1]
        assert profile.speed[0] - 1e-4 <= p.speed <= profile.speed[1] + 1e-4
        assert profile.crf[0] <= p.crf <= profile.crf[1]
        assert p.trim_start + p.trim_end <= info.duration * 0.1 + 1e-6
        assert p.sample_rate in (44100, 48000)


def test_rotation_zoom_hides_corners():
    assert rotation_zoom(0, 1920, 1080) == 1.0
    zoom = rotation_zoom(1.5, 1920, 1080)
    assert 1.04 < zoom < 1.05
    assert rotation_zoom(-1.5, 1080, 1920) == pytest.approx(zoom)


def test_short_video_is_not_trimmed():
    p = generate_params(UniqueOptions(intensity="strong"), make_info(duration=2.5), seed=1)
    assert p.trim_start == p.trim_end == 0.0


def test_options_switch_mirror_speed_and_format():
    info = make_info()
    plain = generate_params(UniqueOptions(speed=False), info, seed=3)
    assert plain.speed == 1.0
    graph = build_unique_video_filter(plain, info)
    assert "setpts" not in graph and "hflip" not in graph
    assert "atempo" not in build_unique_audio_filter(plain)

    mirrored = generate_params(UniqueOptions(mirror=True, fmt="9:16"), info, seed=3)
    graph = build_unique_video_filter(mirrored, info)
    assert "hflip" in graph and "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920" in graph
    assert "crop=720:1280" in build_unique_video_filter(mirrored, info, fallback=True)
    assert "setpts=(PTS-STARTPTS)/" in graph and "atempo=" in build_unique_audio_filter(mirrored)
    assert output_duration(mirrored, info) < info.duration


@pytest.mark.parametrize("intensity", list(INTENSITIES))
@pytest.mark.parametrize("fmt", list(FORMATS))
def test_filters_contain_only_generated_numbers(intensity, fmt, tmp_path):
    info = make_info(fps=59.94, sar=1.1)
    for seed in range(40):
        p = generate_params(UniqueOptions(intensity=intensity, fmt=fmt, mirror=bool(seed % 2)), info, seed=seed)
        video = build_unique_video_filter(p, info)
        audio = build_unique_audio_filter(p)
        assert SAFE_FILTER.match(video), video
        assert SAFE_FILTER.match(audio), audio
        assert "movie" not in video and ";" not in video
        args = build_unique_args(tmp_path / "in.mov", tmp_path / "out.mp4", p, info=info)
        assert args[-1] == str(tmp_path / "out.mp4")
        assert "-map_metadata" in args and "filter_units=remove_types=6" in args


def test_output_fps_is_standard_and_capped():
    assert output_fps(29.97) == (29.97, "30000/1001")
    assert output_fps(30.02)[1] == "30"
    assert output_fps(120.0)[1] == "60"
    assert output_fps(0.0) == (30.0, "30")
    assert output_fps(15.0) == (15.0, "15")


def test_options_are_normalized():
    opts = UniqueOptions(intensity="crazy", copies=50, fmt="4:3").normalized(5)
    assert (opts.intensity, opts.copies, opts.fmt) == ("medium", 5, "original")
    assert UniqueOptions(copies=0).normalized(5).copies == 1


def test_description_is_human_readable():
    text = describe(generate_params(UniqueOptions(intensity="strong", mirror=True), make_info(), seed=5))
    assert "зум" in text and "зеркало" in text and "метаданные" in text
