"""Tests for wendy.gemini_analyzer pure helpers."""

import base64

from wendy import gemini_analyzer as ga


def test_infer_media_type_prefers_content_type():
    assert ga._infer_media_type("clip.mp4", "image/png") == "image/png"


def test_infer_media_type_falls_back_to_extension_for_octet_stream():
    assert ga._infer_media_type("photo.jpg", "application/octet-stream") == "image/jpeg"


def test_infer_media_type_uses_extension_when_no_header():
    assert ga._infer_media_type("song.mp3", None) == "audio/mpeg"


def test_infer_media_type_empty_when_unknown():
    assert ga._infer_media_type("file.xyz", None) == ""


def test_get_gemini_model_uses_configured_model_for_all_media():
    # One multimodal model now serves every media type.
    assert ga._get_gemini_model("video/mp4") == ga.GEMINI_MODEL
    assert ga._get_gemini_model("image/png") == ga.GEMINI_MODEL
    assert ga._get_gemini_model("application/pdf") == ga.GEMINI_MODEL
    assert ga.GEMINI_MODEL == "gemini-3.5-flash"


def test_document_types_supported():
    # "Accept all media Gemini supports" includes documents / text / code.
    assert "application/pdf" in ga.SUPPORTED_MEDIA_TYPES
    assert ga._infer_media_type("notes.pdf", "application/octet-stream") == "application/pdf"
    assert ga._infer_media_type("readme.md", None) == "text/markdown"


def test_video_resolution_thresholds():
    assert ga._get_video_resolution(None) == "MEDIA_RESOLUTION_MEDIUM"
    assert ga._get_video_resolution(10) == "MEDIA_RESOLUTION_HIGH"
    assert ga._get_video_resolution(30) == "MEDIA_RESOLUTION_HIGH"
    assert ga._get_video_resolution(60) == "MEDIA_RESOLUTION_MEDIUM"
    assert ga._get_video_resolution(120) == "MEDIA_RESOLUTION_MEDIUM"
    assert ga._get_video_resolution(300) == "MEDIA_RESOLUTION_LOW"


def test_validate_media_rejects_oversize_file():
    big = b"x" * (ga.GEMINI_MAX_FILE_SIZE + 1)
    err, duration = ga._validate_media(big, "image/png")
    assert err is not None
    assert "too large" in err.lower()
    assert duration is None


def test_validate_media_passes_small_image():
    # Images skip ffprobe (no duration), so a small image validates cleanly.
    err, duration = ga._validate_media(b"tiny", "image/png")
    assert err is None
    assert duration is None


def test_build_request_body_encodes_inline_data():
    content = b"hello"
    body = ga._build_gemini_request_body(content, "image/png", "describe this")
    part = body["contents"][0]["parts"][0]["inline_data"]
    assert part["mime_type"] == "image/png"
    assert base64.standard_b64decode(part["data"]) == content
    assert body["contents"][0]["parts"][1]["text"] == "describe this"
    # Images get no media_resolution generation_config.
    assert "generation_config" not in body


def test_build_request_body_sets_video_resolution():
    body = ga._build_gemini_request_body(b"vid", "video/mp4", "summarize", duration=10)
    assert body["generation_config"]["media_resolution"] == "MEDIA_RESOLUTION_HIGH"
