from types import SimpleNamespace

import pytest

from app.llm.schemas import GeneratedQuestion
from app.services.question_gen import question_counts, select_representative, validate_questions


def test_question_counts_sum_and_proportions():
    # 3.2 / 1.6 / 3.2 → floors 3/1/3, the leftover question goes to the largest remainder (true_false)
    assert question_counts(8, {"mcq": 0.4, "true_false": 0.2, "short_answer": 0.4}) == {
        "mcq": 3, "true_false": 2, "short_answer": 3,
    }
    assert question_counts(5, {"mcq": 1}) == {"mcq": 5, "true_false": 0, "short_answer": 0}
    for n in range(1, 21):
        assert sum(question_counts(n, {"mcq": 2, "true_false": 1, "short_answer": 2}).values()) == n


def test_question_counts_rejects_bad_input():
    with pytest.raises(ValueError):
        question_counts(0, {"mcq": 1})
    with pytest.raises(ValueError):
        question_counts(5, {"mcq": 0})


def _chunk(i, topic):
    return SimpleNamespace(id=f"c{i}", chunk_index=i, topic=topic, text=f"text {i}", start_time=i * 10.0, end_time=i * 10.0 + 10)


def test_select_representative_covers_every_topic():
    chunks = [_chunk(i, "A") for i in range(10)] + [_chunk(10 + i, "B") for i in range(2)] + [_chunk(12, "C")]
    picked = select_representative(chunks, 6)
    assert len(picked) == 6
    assert {c.topic for c in picked} == {"A", "B", "C"}
    assert [c.chunk_index for c in picked] == sorted(c.chunk_index for c in picked)


def test_select_representative_small_video_returns_all():
    chunks = [_chunk(i, "A") for i in range(3)]
    assert len(select_representative(chunks, 8)) == 3


def _q(**kw):
    base = dict(type="mcq", prompt="Why?", options=["a", "b", "c", "d"], correct_answer="b",
                reference_answer="b", rubric="", explanation="", topic="A", difficulty="medium",
                source_chunk_ids=["c1"])
    return GeneratedQuestion(**(base | kw))


def test_validate_questions_rules():
    by_id = {"c1": _chunk(1, "A"), "c2": _chunk(2, "B")}
    qs = [
        _q(prompt="ok mcq", correct_answer="B"),                                   # case-insensitive match → kept
        _q(prompt="answer not in options", correct_answer="z"),                     # rejected
        _q(prompt="three options", options=["a", "b", "c"]),                       # rejected
        _q(prompt="bad source", source_chunk_ids=["nope"]),                         # rejected
        _q(prompt="ok tf", type="true_false", options=[], correct_answer="False"),  # normalised
        _q(prompt="tf not bool", type="true_false", options=[], correct_answer="maybe"),
        _q(prompt="ok short", type="short_answer", options=[], correct_answer="", reference_answer="because"),
        _q(prompt="short no ref", type="short_answer", options=[], correct_answer="", reference_answer=" "),
        _q(prompt="ok mcq", correct_answer="b"),                                    # duplicate prompt
        _q(prompt="unknown topic", topic="Made Up", source_chunk_ids=["c2"]),       # topic → chunk's topic
    ]
    valid, rejected = validate_questions(qs, by_id)
    prompts = [q.prompt for q in valid]
    assert prompts == ["ok mcq", "ok tf", "ok short", "unknown topic"]
    assert valid[0].correct_answer == "b"
    assert valid[1].correct_answer == "false"
    assert valid[3].topic == "B"
    assert len(rejected) == 6
