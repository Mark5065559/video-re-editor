from pathlib import Path

from app.media import PRESETS, build_video_filter


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
