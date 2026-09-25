import pytest

from app.services.chunking import Segment, chunk_segments


def _segs(n: int, words: int = 10, dur: float = 5.0) -> list[Segment]:
    return [
        Segment(start=i * dur, end=(i + 1) * dur, text=" ".join(f"w{i}_{j}" for j in range(words))) for i in range(n)
    ]


def test_empty_and_blank_segments():
    assert chunk_segments([]) == []
    assert chunk_segments([Segment(0, 1, "   ")]) == []


def test_chunks_respect_target_and_keep_segment_timestamps():
    segs = _segs(30)  # 300 words
    chunks = chunk_segments(segs, target_words=50, overlap_words=10)
    assert all(c.word_count >= 50 for c in chunks[:-1])
    for c in chunks:  # boundaries always coincide with segment boundaries
        assert c.start in {s.start for s in segs}
        assert c.end in {s.end for s in segs}
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_full_coverage_and_overlap():
    segs = _segs(30)
    chunks = chunk_segments(segs, target_words=50, overlap_words=10)
    assert chunks[0].start == segs[0].start and chunks[-1].end == segs[-1].end
    covered = set(" ".join(c.text for c in chunks).split())
    assert covered == set(" ".join(s.text for s in segs).split())
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.start < prev.end  # consecutive chunks overlap by at least one segment
        assert nxt.start > prev.start  # and always advance


def test_single_segment_larger_than_target():
    chunks = chunk_segments([Segment(0, 60, " ".join(["x"] * 500))], target_words=100, overlap_words=20)
    assert len(chunks) == 1 and chunks[0].word_count == 500


def test_no_overlap_partitions_segments():
    chunks = chunk_segments(_segs(10), target_words=20, overlap_words=0)
    assert len(chunks) == 5
    assert all(b.start == a.end for a, b in zip(chunks, chunks[1:], strict=False))


@pytest.mark.parametrize("target,overlap", [(0, 0), (10, 10), (10, -1)])
def test_invalid_parameters(target, overlap):
    with pytest.raises(ValueError):
        chunk_segments(_segs(3), target, overlap)


def test_short_remainder_is_absorbed_not_emitted_as_tiny_chunk():
    segs = _segs(11)  # 110 words; target 50 → 50 + 50 + a 10-word tail
    chunks = chunk_segments(segs, target_words=50, overlap_words=0)
    assert chunks[-1].end == segs[-1].end
    assert all(c.word_count >= 50 for c in chunks)
