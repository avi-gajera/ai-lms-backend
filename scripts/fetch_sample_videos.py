"""Download the sample lecture videos into sample_data/videos/ (not committed to git).

    python scripts/fetch_sample_videos.py            # all samples
    python scripts/fetch_sample_videos.py --only inflation_khan

Downloads a small (<=360p) video stream plus the English audio track and merges them into an MP4
(speech is all the pipeline needs). Merging uses the ffmpeg binary bundled with `imageio-ffmpeg`,
so no system ffmpeg is required. See sample_data/README.md for sources and licensing.
"""

import argparse
import sys
from pathlib import Path

import imageio_ffmpeg
import yt_dlp

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "sample_data" / "videos"

SAMPLES = {
    # name: (YouTube id, publisher, title)
    "recursion_mit": ("tOsdeaYDCMk", "MIT OpenCourseWare", "1.10.7 Recursive Functions"),
    "inflation_khan": ("AaR1mPrdbTc", "Khan Academy", "Introduction to inflation"),
    "photosynthesis_khan": ("-rsYk4eCKnA", "Khan Academy", "Photosynthesis"),
}


def fetch(name: str) -> Path:
    vid, publisher, title = SAMPLES[name]
    target = DEST / f"{name}.mp4"
    if target.exists():
        print(f"✓ {target.name} already present")
        return target
    print(f"↓ {name}: {publisher} — {title}")
    opts = {
        # Prefer the original-language (English) audio; YouTube now serves dubbed tracks too.
        "format": "bv*[height<=360][ext=mp4]+ba[ext=m4a][language^=en]/bv*[height<=360]+ba/b[height<=360]/b",
        "merge_output_format": "mp4",
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "outtmpl": str(DEST / f"{name}.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={vid}"])
    print(f"✓ saved {target.relative_to(ROOT)} ({target.stat().st_size / 1e6:.1f} MB)")
    return target


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=list(SAMPLES), action="append")
    args = ap.parse_args()
    DEST.mkdir(parents=True, exist_ok=True)
    failed = []
    for name in args.only or SAMPLES:
        try:
            fetch(name)
        except Exception as exc:  # keep going; report at the end
            print(f"✗ {name}: {exc}")
            failed.append(name)
    if failed:
        sys.exit(f"failed: {failed}")


if __name__ == "__main__":
    main()
