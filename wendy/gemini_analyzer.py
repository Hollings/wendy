"""Gemini media analysis for the internal API.

Extracted from :mod:`wendy.api_server` to keep that module focused on routing.
Exposes :func:`handle_analyze_file`, the ``POST /api/analyze_file`` route
handler, plus the media-type detection and validation helpers it relies on.
"""
from __future__ import annotations

import base64
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import aiohttp
from aiohttp import web

_LOG = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Model is config-driven so it's a one-line bump and can't silently go
# deprecated again. gemini-3.1-pro-preview is the strongest current multimodal
# model (verified via the API model list, 2026-06) and handles image / audio /
# video / document input. Override with WENDY_GEMINI_MODEL (e.g. gemini-pro-latest
# to auto-track the newest pro).
GEMINI_MODEL = os.getenv("WENDY_GEMINI_MODEL", "gemini-3.1-pro-preview")
GEMINI_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB (inline_data limit; see Files-API TODO)
GEMINI_MAX_VIDEO_DURATION = 5 * 60       # 5 minutes
GEMINI_MAX_AUDIO_DURATION = 30 * 60      # 30 minutes

SUPPORTED_IMAGE_TYPES = {
    "image/png", "image/jpeg", "image/webp", "image/heic", "image/heif",
}
SUPPORTED_AUDIO_TYPES = {
    "audio/wav", "audio/mp3", "audio/mpeg", "audio/aiff",
    "audio/aac", "audio/ogg", "audio/flac",
}
SUPPORTED_VIDEO_TYPES = {
    "video/mp4", "video/mpeg", "video/quicktime", "video/avi",
    "video/x-flv", "video/webm", "video/x-ms-wmv", "video/3gpp",
}
# Documents + plain-text/code formats Gemini can read directly (PDF especially).
SUPPORTED_DOCUMENT_TYPES = {
    "application/pdf",
    "text/plain", "text/html", "text/css", "text/markdown", "text/md",
    "text/csv", "text/xml", "application/xml", "text/x-python",
    "application/x-javascript", "text/javascript", "application/rtf", "text/rtf",
}
SUPPORTED_MEDIA_TYPES = (
    SUPPORTED_IMAGE_TYPES | SUPPORTED_AUDIO_TYPES
    | SUPPORTED_VIDEO_TYPES | SUPPORTED_DOCUMENT_TYPES
)

EXTENSION_TO_MIME: dict[str, str] = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".aiff": "audio/aiff",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".flac": "audio/flac",
    ".mp4": "video/mp4", ".mpeg": "video/mpeg", ".mpg": "video/mpeg",
    ".mov": "video/quicktime", ".avi": "video/avi", ".flv": "video/x-flv",
    ".webm": "video/webm", ".wmv": "video/x-ms-wmv", ".3gp": "video/3gpp",
    ".pdf": "application/pdf", ".txt": "text/plain", ".html": "text/html",
    ".htm": "text/html", ".css": "text/css", ".md": "text/markdown",
    ".markdown": "text/markdown", ".csv": "text/csv", ".xml": "text/xml",
    ".py": "text/x-python", ".js": "text/javascript", ".rtf": "application/rtf",
}


def _infer_media_type(filename: str | None, content_type: str | None) -> str:
    """Determine MIME type from the Content-Type header or file extension.

    Falls back to extension-based lookup when the header is missing or is the
    generic ``application/octet-stream``.
    """
    if content_type and content_type != "application/octet-stream":
        return content_type
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in EXTENSION_TO_MIME:
            return EXTENSION_TO_MIME[ext]
    return content_type or ""


def _get_media_duration(content: bytes, media_type: str) -> float | None:
    """Use ``ffprobe`` to determine duration of an audio or video file.

    Returns ``None`` for non-AV media or when ffprobe is unavailable.
    """
    if media_type not in SUPPORTED_AUDIO_TYPES and media_type not in SUPPORTED_VIDEO_TYPES:
        return None
    try:
        suffix = ".mp4" if media_type.startswith("video/") else ".mp3"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(content)
            temp_path = f.name
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", temp_path],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        finally:
            Path(temp_path).unlink(missing_ok=True)
    except Exception:
        pass
    return None


def _get_gemini_model(media_type: str) -> str:
    """Return the configured Gemini model.

    gemini-3.1-pro-preview is multimodal across image / audio / video / document
    input, so a single model now serves every media type (no per-type split).
    The argument is kept for signature stability and future per-type overrides.
    """
    return GEMINI_MODEL


def _get_video_resolution(duration: float | None) -> str:
    """Choose Gemini media resolution setting based on video duration."""
    if duration is None:
        return "MEDIA_RESOLUTION_MEDIUM"
    if duration <= 30:
        return "MEDIA_RESOLUTION_HIGH"
    if duration <= 120:
        return "MEDIA_RESOLUTION_MEDIUM"
    return "MEDIA_RESOLUTION_LOW"


def _validate_media(file_content: bytes, media_type: str) -> tuple[str | None, float | None]:
    """Check file size and duration limits.

    Returns ``(error_string, duration)``.  *error_string* is ``None`` when
    validation passes.  *duration* is the media duration in seconds (or
    ``None`` for images / when ffprobe is unavailable).
    """
    if len(file_content) > GEMINI_MAX_FILE_SIZE:
        return f"File too large ({len(file_content) / 1024 / 1024:.1f}MB). Max 20MB.", None

    duration = _get_media_duration(file_content, media_type)
    if duration is not None:
        if media_type in SUPPORTED_VIDEO_TYPES and duration > GEMINI_MAX_VIDEO_DURATION:
            return f"Video too long ({duration / 60:.1f} min). Max 5 min.", duration
        if media_type in SUPPORTED_AUDIO_TYPES and duration > GEMINI_MAX_AUDIO_DURATION:
            return f"Audio too long ({duration / 60:.1f} min). Max 30 min.", duration

    return None, duration


def _build_gemini_request_body(
    file_content: bytes,
    media_type: str,
    prompt: str,
    duration: float | None = None,
) -> dict:
    """Assemble the JSON body for the Gemini ``generateContent`` endpoint.

    *duration* should be pre-computed by ``_validate_media`` so ffprobe is not
    invoked a second time.
    """
    file_b64 = base64.standard_b64encode(file_content).decode()
    body: dict = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": media_type, "data": file_b64}},
            {"text": prompt},
        ]}],
    }
    if media_type in SUPPORTED_VIDEO_TYPES:
        body["generation_config"] = {"media_resolution": _get_video_resolution(duration)}
    return body


async def handle_analyze_file(request: web.Request) -> web.Response:
    """POST /api/analyze_file -- analyse media (image/audio/video) via Gemini.

    Expects a multipart form with ``prompt`` (text) and ``file`` (binary)
    fields.  Validates media type and size/duration, then proxies the request
    to the Gemini API and returns the analysis text.
    """
    if not GEMINI_API_KEY:
        return web.json_response({"error": "GEMINI_API_KEY not configured"}, status=500)

    try:
        reader = await request.multipart()
        prompt: str | None = None
        file_content: bytes | None = None
        filename: str | None = None
        content_type: str | None = None

        async for part in reader:
            if part.name == "prompt":
                prompt = (await part.read()).decode()
            elif part.name == "file":
                filename = part.filename
                content_type = part.headers.get("Content-Type")
                file_content = await part.read()

        if not prompt or file_content is None:
            return web.json_response({"error": "prompt and file fields required"}, status=400)

        media_type = _infer_media_type(filename, content_type)
        if media_type not in SUPPORTED_MEDIA_TYPES:
            return web.json_response({"error": f"Unsupported file type: {media_type}"}, status=400)

        err, duration = _validate_media(file_content, media_type)
        if err:
            return web.json_response({"error": err}, status=400)

        model = _get_gemini_model(media_type)
        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = _build_gemini_request_body(file_content, media_type, prompt, duration)

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
            async with session.post(
                gemini_url, headers={"x-goog-api-key": GEMINI_API_KEY}, json=body,
            ) as resp:
                if resp.status != 200:
                    detail = await resp.text()
                    return web.json_response({"error": f"Gemini API error: {detail}"}, status=502)
                result = await resp.json()

        try:
            analysis = result["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            return web.json_response({"error": f"Unexpected Gemini response: {result}"}, status=502)

        return web.json_response({
            "success": True, "analysis": analysis, "media_type": media_type, "model": model,
        })
    except aiohttp.ClientError as e:
        return web.json_response({"error": f"Gemini connection error: {e}"}, status=502)
    except Exception as e:
        _LOG.error("analyze_file error: %s", e)
        return web.json_response({"error": "Internal server error"}, status=500)
