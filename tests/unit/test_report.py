from types import SimpleNamespace

from app.services.report import _template_narrative, compute_stats


def _answer(pos, qtype, topic, score, correct, chunk, areas=(), response="x"):
    q = SimpleNamespace(position=pos, type=qtype, topic=topic, source_chunk_ids=[chunk])
    return SimpleNamespace(
        question=q, score=score, is_correct=correct, improvement_areas=list(areas), response=response
    )


def test_compute_stats_topic_breakdown_and_bands():
    chunks = {
        "c1": SimpleNamespace(start_time=0.0, end_time=60.0),
        "c2": SimpleNamespace(start_time=60.0, end_time=130.0),
    }
    attempt = SimpleNamespace(
        answers=[
            _answer(0, "mcq", "Recursion", 1.0, True, "c1"),
            _answer(1, "short_answer", "Recursion", 0.8, True, "c1"),
            _answer(2, "mcq", "Stacks", 0.0, False, "c2", ["Stacks: revisit"]),
            _answer(3, "true_false", "Stacks", 0.0, False, "c2", ["Stacks: revisit", "LIFO order"], response=None),
        ]
    )
    s = compute_stats(attempt, chunks, strength=0.8, weakness=0.6)

    assert s["overall"] == {
        "score": 1.8,
        "max_score": 4.0,
        "percentage": 45.0,
        "correct": 2,
        "total_questions": 4,
        "attempted": 3,
        "band": "needs improvement",
    }
    assert s["by_type"]["mcq"] == {"questions": 2, "correct": 1, "avg_score": 0.5, "accuracy": 0.5}
    topics = {t["topic"]: t for t in s["topic_breakdown"]}
    assert topics["Recursion"]["status"] == "strength" and topics["Recursion"]["avg_score"] == 0.9
    assert topics["Stacks"]["status"] == "weakness" and topics["Stacks"]["video_segments"] == ["01:00-02:10"]
    assert s["strengths"] == [{"topic": "Recursion", "avg_score": 0.9}]
    assert s["weaknesses"][0]["rewatch"] == ["01:00-02:10"]
    assert s["improvement_areas"] == ["Stacks: revisit", "LIFO order"]  # de-duplicated, order kept

    narrative = _template_narrative(s)
    assert "45.0%" in narrative.summary and "Stacks" in narrative.summary
    assert narrative.suggestions and "01:00-02:10" in narrative.suggestions[0]
