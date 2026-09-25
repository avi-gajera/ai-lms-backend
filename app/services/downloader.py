"""Fetch the audio of a video from a URL (YouTube, Vimeo, ...) with yt-dlp.

Only the audio track is downloaded: speech is all the pipeline needs, it is ~10x smaller than the
video, and a single audio stream needs no ffmpeg merge (faster-whisper decodes it via PyAV, D17).

`validate_url` runs in the request (cheap, returns 422); `download_audio` runs in the background
job. Hosts are restricted to an allowlist, which also rules out requests to internal addresses.
"""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from app.config import Settings
from app.core.exceptions import ValidationFailed
from app.core.logging import get_logger

logger = get_logger(__name__)

# Prefer the original English audio (YouTube also serves dubbed tracks), then any audio, then any file.
AUDIO_FORMAT = "ba[ext=m4a][language^=en]/ba[ext=m4a]/ba/b"


class DownloadFailed(Exception):
    """The URL could not be turned into a local audio file (permanent: the job marks the video failed)."""


@dataclass
class Downloaded:
    path: Path
    title: str | None
    duration_s: float | None


def validate_url(url: str, settings: Settings) -> str:
    url = url.strip()
    if len(url) > 2048:
        raise ValidationFailed("url is too long")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValidationFailed("url must be an http(s) link")
    host = parts.hostname.lower()
    allowed = [d.lower() for d in settings.url_allowed_domains]
    if not any(host == d or host.endswith("." + d) for d in allowed):
        raise ValidationFailed(f"Links from '{host}' are not supported", details={"allowed_domains": allowed})
    return url


def download_audio(url: str, dest_dir: str, stem: str, settings: Settings) -> Downloaded:
    """Download the audio track to `<dest_dir>/<stem>.<ext>`. Checks length before downloading."""
    import yt_dlp  # heavy import, only needed in the job

    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    opts = {
        "format": AUDIO_FORMAT,
        "outtmpl": str(Path(dest_dir) / f"{stem}.%(ext)s"),
        "noplaylist": True,
        "max_filesize": settings.max_upload_mb * 1024 * 1024,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist":
                raise DownloadFailed("Playlists are not supported; link a single video")
            if info.get("is_live"):
                raise DownloadFailed("Live streams are not supported")
            duration = info.get("duration")
            if duration and duration > settings.url_max_duration_s:
                raise DownloadFailed(
                    f"Video is {duration / 60:.0f} min long; the limit is {settings.url_max_duration_s / 60:.0f} min"
                )
            info = ydl.process_ie_result(info, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadFailed(str(exc).removeprefix("ERROR: ")[:500]) from exc

    downloads = info.get("requested_downloads") or []
    path = Path(downloads[0]["filepath"]) if downloads else None
    if path is None or not path.is_file():
        # yt-dlp skips files over max_filesize without raising.
        raise DownloadFailed(f"Nothing was downloaded (the file may exceed {settings.max_upload_mb} MB)")
    logger.info("downloaded", extra={"url": url, "bytes": path.stat().st_size, "duration_s": duration})
    return Downloaded(path=path, title=info.get("title"), duration_s=duration)
