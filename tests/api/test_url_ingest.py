"""POST /videos with a `url`: validation in the request, download in the background job (offline).

The real yt-dlp call is replaced: API tests patch `video_pipeline.download_audio` with a fake that
writes a "media" file the fake transcriber understands; the downloader's own checks are tested
against a stubbed `yt_dlp` module.
"""

import sys
import types
from pathlib import Path

import pytest

from app.config import get_settings
from app.core.exceptions import ValidationFailed
from app.services import downloader, video_pipeline
from app.services.downloader import Downloaded, DownloadFailed
from tests.conftest import wait_jobs

URL = "https://www.youtube.com/watch?v=AaR1mPrdbTc"


@pytest.fixture
def fake_download(monkeypatch):
    calls = []

    def _fake(url, dest_dir, stem, settings, topics=("inflation",), title="Introduction to inflation"):
        calls.append(url)
        path = Path(dest_dir) / f"{stem}.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(",".join(topics), encoding="utf-8")  # read by the fake transcriber
        return Downloaded(path=path, title=title, duration_s=452.0)

    monkeypatch.setattr(video_pipeline, "download_audio", _fake)
    return calls


def test_url_video_is_downloaded_and_indexed(client, fake_download):
    r = client.post("/videos", data={"url": URL})
    assert r.status_code == 202, r.text
    wait_jobs()
    video = client.get(r.json()["status_url"]).json()
    assert video["status"] == "indexed"
    assert video["source_url"] == URL
    assert video["title"] == "Introduction to inflation"  # taken from the video when none is given
    assert video["chunk_count"] >= 1
    assert fake_download == [URL]


def test_given_title_is_kept(client, fake_download):
    r = client.post("/videos", data={"url": URL, "title": "My lecture"})
    wait_jobs()
    assert client.get(r.json()["status_url"]).json()["title"] == "My lecture"


def test_retry_reuses_downloaded_file(client, fake_download):
    from app.db.session import SessionLocal
    from app.llm.factory import get_llm

    r = client.post("/videos", data={"url": URL})
    wait_jobs()
    with SessionLocal() as db:
        video_pipeline.run(db, get_llm(), get_settings(), r.json()["video_id"])
    assert len(fake_download) == 1


def test_download_failure_marks_video_failed(client, monkeypatch):
    def _fail(*_):
        raise DownloadFailed("Video is 95 min long; the limit is 60 min")

    monkeypatch.setattr(video_pipeline, "download_audio", _fail)
    r = client.post("/videos", data={"url": URL})
    wait_jobs()
    video = client.get(r.json()["status_url"]).json()
    assert video["status"] == "failed"
    assert "95 min" in video["error"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://www.youtube.com/watch?v=x",
        "https://example.com/video.mp4",
        "http://169.254.169.254/latest/meta-data",
        "https://youtube.com.evil.example/watch?v=x",
        "not a url",
    ],
)
def test_url_outside_allowlist_is_rejected(client, url):
    r = client.post("/videos", data={"url": url})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"


def test_exactly_one_source_is_required(client, sample_video):
    name = sample_video("a.mp4", ["recursion"])
    assert client.post("/videos", data={"url": URL, "sample_path": name}).status_code == 422
    assert client.post("/videos", data={}).status_code == 422


def test_allowlist_accepts_subdomains_and_short_links():
    s = get_settings()
    for url in ("https://m.youtube.com/watch?v=x", "https://youtu.be/x", "https://vimeo.com/123"):
        assert downloader.validate_url(url, s) == url
    with pytest.raises(ValidationFailed):
        downloader.validate_url("https://notyoutube.com/x", s)


# --- download_audio against a stubbed yt_dlp ---------------------------------------------------


def _stub_yt_dlp(monkeypatch, info: dict, *, write_file: Path | None = None):
    class DownloadError(Exception):
        pass

    class YoutubeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def extract_info(self, url, download=False):
            if info.get("_error"):
                raise DownloadError("ERROR: " + info["_error"])
            return dict(info)

        def process_ie_result(self, result, download=True):
            if write_file is None:  # yt-dlp skips files over max_filesize without raising
                return result
            write_file.write_bytes(b"audio")
            return {**result, "requested_downloads": [{"filepath": str(write_file)}]}

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = YoutubeDL
    module.utils = types.SimpleNamespace(DownloadError=DownloadError)
    monkeypatch.setitem(sys.modules, "yt_dlp", module)


def test_download_audio_returns_file_and_metadata(monkeypatch, tmp_path):
    _stub_yt_dlp(monkeypatch, {"title": "T", "duration": 120}, write_file=tmp_path / "v.m4a")
    dl = downloader.download_audio(URL, str(tmp_path), "v", get_settings())
    assert dl.path == tmp_path / "v.m4a" and dl.title == "T" and dl.duration_s == 120


@pytest.mark.parametrize(
    ("info", "message"),
    [
        ({"duration": 5 * 3600}, "limit"),
        ({"is_live": True}, "Live streams"),
        ({"_type": "playlist"}, "Playlists"),
        ({"_error": "Video unavailable"}, "Video unavailable"),
        ({"duration": 60}, "Nothing was downloaded"),  # skipped for size: no file written
    ],
)
def test_download_audio_refusals(monkeypatch, tmp_path, info, message):
    _stub_yt_dlp(monkeypatch, info)
    with pytest.raises(DownloadFailed, match=message):
        downloader.download_audio(URL, str(tmp_path), "v", get_settings())
