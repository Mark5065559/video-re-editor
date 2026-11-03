from pathlib import Path

from app.media import (
    PRESETS,
    VideoInfo,
    build_preset_args,
    build_video_filter,
    fallback_video_kbps,
    parse_probe,
)


def make_info(**overrides) -> VideoInfo:
    values = dict(duration=10.0, width=1920, height=1080, has_audio=True, video_codec="h264",
                  audio_codec="aac", size_bytes=1_000_000, fps=30.0)
    values.update(overrides)
    return VideoInfo(**values)


def test_all_presets_have_safe_fixed_filters():
    for preset in PRESETS.values():
        graph = build_video_filter(preset)
        assert "setsar=1" in graph
        assert "movie=" not in graph
        assert ";" not in graph


def test_reels_fill_is_vertical_1080p():
    graph = build_video_filter(PRESETS["reels_fill"])
    assert "scale=1080:1920" in graph
    assert "crop=1080:1920" in graph


def test_square_is_square():
    graph = build_video_filter(PRESETS["square"])
    assert "scale=1080:1080" in graph
    assert "crop=1080:1080" in graph


def test_fit_preserves_full_frame_with_padding():
    graph = build_video_filter(PRESETS["reels_fit"])
    assert "force_original_aspect_ratio=decrease" in graph
    assert "pad=1080:1920" in graph


def test_sha256_is_repeatable(tmp_path: Path):
    from app.media import sha256_file

    sample = tmp_path / "test.bin"
    sample.write_bytes(b"video-re-editor")
    assert sha256_file(sample) == sha256_file(sample)


def test_keep_mode_limits_long_side_and_keeps_even_size():
    graph = build_video_filter(PRESETS["keep"])
    assert "min(iw,1920)" in graph and "min(ih,1920)" in graph
    assert "force_divisible_by=2" in graph
    assert "min(iw,1280)" in build_video_filter(PRESETS["keep"], True)


def test_warm_keeps_colour_correction_in_fallback():
    assert "eq=" in build_video_filter(PRESETS["warm"], True)


def test_high_fps_is_capped_and_sar_is_fixed():
    graph = build_video_filter(PRESETS["keep"], info=make_info(fps=120.0, sar=1.333))
    assert "fps=60" in graph
    assert graph.startswith("scale='trunc(iw*sar/2)*2':ih,setsar=1")
    assert "fps=" not in build_video_filter(PRESETS["keep"], info=make_info(fps=30.0))


def test_preset_args_take_first_audio_track_and_resample_after_loudnorm(tmp_path: Path):
    args = build_preset_args(tmp_path / "in.mp4", tmp_path / "out.mp4", PRESETS["keep"], info=make_info())
    assert args[args.index("-map", args.index("-map") + 1) + 1] == "0:a:0?"
    audio_filter = args[args.index("-af") + 1]
    assert audio_filter.startswith("loudnorm=") and audio_filter.endswith("aresample=48000")
    assert "-crf" in args

    silent = build_preset_args(tmp_path / "in.mp4", tmp_path / "out.mp4", PRESETS["keep"],
                               info=make_info(has_audio=False))
    assert "-af" not in silent

    fallback = build_preset_args(tmp_path / "in.mp4", tmp_path / "out.mp4", PRESETS["keep"],
                                 info=make_info(), video_kbps=900)
    assert "-crf" not in fallback and fallback[fallback.index("-b:v") + 1] == "900k"


def test_fallback_bitrate_fits_the_limit():
    limit = 47 * 1024 * 1024
    kbps = fallback_video_kbps(limit, 120.0, 96)
    expected_bytes = (kbps + 96) * 1000 / 8 * 120
    assert expected_bytes < limit


def test_probe_swaps_size_for_rotated_phone_video():
    data = {
        "format": {"duration": "12.5"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
             "avg_frame_rate": "30000/1001", "side_data_list": [{"rotation": -90}]},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    info = parse_probe(data, 123)
    assert (info.width, info.height, info.rotation) == (1080, 1920, 270)
    assert abs(info.fps - 29.97) < 0.01 and info.has_audio

    data["streams"][0].pop("side_data_list")
    data["streams"][0]["tags"] = {"rotate": "90"}
    assert (parse_probe(data, 1).width, parse_probe(data, 1).height) == (1080, 1920)


def test_probe_uses_stream_duration_and_ignores_cover_art():
    data = {
        "format": {},
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg", "width": 600, "height": 600,
             "disposition": {"attached_pic": 1}},
            {"codec_type": "video", "codec_name": "vp9", "width": 1280, "height": 720,
             "r_frame_rate": "25/1", "duration": "7.0"},
        ],
    }
    info = parse_probe(data, 1)
    assert (info.width, info.height, info.video_codec, info.duration) == (1280, 720, "vp9", 7.0)
    assert not info.has_audio
