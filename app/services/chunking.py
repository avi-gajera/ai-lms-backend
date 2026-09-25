"""Transcript chunking.

Whisper returns short timestamped segments. We group them into ~`target_words` windows with
~`overlap_words` of overlap, always cutting on segment boundaries so each chunk keeps exact
start/end timestamps (which is what lets a report say "rewatch 03:10-04:25"). A generic text
splitter would lose those timestamps.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Chunk:
    index: int
    start: float
    end: float
    text: str

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def chunk_segments(segments: list[Segment], target_words: int = 220, overlap_words: int = 40) -> list[Chunk]:
    if target_words <= 0:
        raise ValueError("target_words must be positive")
    if not 0 <= overlap_words < target_words:
        raise ValueError("overlap_words must be in [0, target_words)")

    segs = [s for s in segments if s.text.strip()]
    if not segs:
        return []

    counts = [len(s.text.split()) for s in segs]
    chunks: list[Chunk] = []
    start = 0
    while start < len(segs):
        # Grow the window until it reaches the target size (always at least one segment).
        end, words = start, 0
        while end < len(segs) and (words < target_words or end == start):
            words += counts[end]
            end += 1
        # Absorb a short remainder instead of emitting a tiny trailing chunk: a 15-second tail
        # embeds as a vague vector and out-ranks real content at retrieval time.
        if sum(counts[end:]) < target_words // 2:
            end = len(segs)

        window = segs[start:end]
        chunks.append(
            Chunk(
                index=len(chunks),
                start=window[0].start,
                end=window[-1].end,
                text=" ".join(s.text.strip() for s in window),
            )
        )
        if end >= len(segs):
            break

        # Step back from `end` by ~overlap_words worth of whole segments, but always advance.
        back, overlap = end, 0
        while back - 1 > start and overlap + counts[back - 1] <= overlap_words:
            back -= 1
            overlap += counts[back]
        start = max(back, start + 1)

    return chunks
