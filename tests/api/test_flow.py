"""End-to-end API tests: ingest → retrieve → assess → evaluate → report (offline)."""

import pytest

from app.core.exceptions import LLMError
from app.llm.schemas import JudgedAnswers, ReportNarrative
from tests.conftest import wait_jobs


def _ingest(client, sample_video, name, topics):
    r = client.post("/videos", data={"sample_path": sample_video(name, topics)})
    assert r.status_code == 202, r.text
    body = r.json()
    wait_jobs()
    return body


def _ready_assessment(client, sample_video, learner="learner-1", **kw):
    v = _ingest(client, sample_video, "cs.mp4", ["recursion", "photosynthesis", "inflation"])
    client.post(f"/videos/{v['video_id']}/progress", json={"learner_id": learner, "progress": 1.0})
    r = client.post("/assessments", json={"video_id": v["video_id"], "learner_id": learner, **kw})
    assert r.status_code == 201, r.text
    return v, r.json()


def _correct_answers(client, assessment_id):
    """Look up the stored correct answers (not exposed by the API) to build a perfect attempt."""
    from app.db.models import Assessment
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        return {q.id: q for q in db.get(Assessment, assessment_id).questions}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ingestion_indexes_video_with_topics(client, sample_video):
    v = _ingest(client, sample_video, "v.mp4", ["recursion", "photosynthesis"])
    assert client.get(v["task_url"]).json()["state"] == "SUCCESS"
    video = client.get(v["status_url"]).json()
    assert video["status"] == "indexed"
    assert video["chunk_count"] >= 2 and video["language"] == "en"
    assert video["topics"]
    chunks = client.get(f"/videos/{v['video_id']}/chunks").json()
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    assert all(c["end_time"] > c["start_time"] for c in chunks)


def test_reprocessing_is_idempotent(client, sample_video):
    from app.config import get_settings
    from app.db.models import TranscriptChunk
    from app.db.session import SessionLocal
    from app.llm.factory import get_llm
    from app.services import retrieval, video_pipeline

    v = _ingest(client, sample_video, "v.mp4", ["recursion", "inflation"])
    vid = v["video_id"]
    with SessionLocal() as db:
        before = db.query(TranscriptChunk).filter_by(video_id=vid).count()
        video_pipeline.run(db, get_llm(), get_settings(), vid)
        video_pipeline.run(db, get_llm(), get_settings(), vid)
        assert db.query(TranscriptChunk).filter_by(video_id=vid).count() == before
    assert retrieval.count_points(vid) == before


def test_retrieval_is_scoped_to_one_video(client, sample_video):
    a = _ingest(client, sample_video, "a.mp4", ["recursion"])
    b = _ingest(client, sample_video, "b.mp4", ["photosynthesis"])
    r = client.post(f"/videos/{a['video_id']}/retrieve", json={"query": "chlorophyll sunlight glucose", "top_k": 10})
    assert r.status_code == 200
    a_chunks = {c["id"] for c in client.get(f"/videos/{a['video_id']}/chunks").json()}
    assert r.json()["results"] and all(h["chunk_id"] in a_chunks for h in r.json()["results"])

    r = client.post(f"/videos/{b['video_id']}/retrieve", json={"query": "chlorophyll absorbs sunlight", "top_k": 2})
    top = r.json()["results"][0]
    assert "chlorophyll" in top["text"].lower() or "photosynthesis" in top["text"].lower()
    assert top["timestamp"].count(":") == 2


def test_upload_and_validation_errors(client, sample_video):
    r = client.post("/videos", files={"file": ("lecture.mp4", b"recursion", "video/mp4")})
    assert r.status_code == 202
    wait_jobs()
    assert client.get(r.json()["status_url"]).json()["status"] == "indexed"

    assert client.post("/videos", files={"file": ("x.exe", b"x", "application/octet-stream")}).status_code == 422
    assert client.post("/videos", data={"sample_path": "../../etc/passwd.mp4"}).status_code == 422
    assert client.post("/videos", data={"sample_path": "missing.mp4"}).status_code == 404
    assert client.post("/videos", data={}).status_code == 422
    err = client.get("/videos/does-not-exist").json()["error"]
    assert err["code"] == "not_found" and err["request_id"]


def test_failed_processing_marks_video_failed(client, sample_video):
    r = client.post("/videos", data={"sample_path": sample_video("bad.mp4", ["unknown-topic"])})
    wait_jobs()
    video = client.get(r.json()["status_url"]).json()
    assert video["status"] == "failed" and "KeyError" in video["error"]
    assert client.get(r.json()["task_url"]).json()["state"] == "FAILURE"


def test_assessment_requires_indexed_video_and_threshold(client, sample_video):
    v = _ingest(client, sample_video, "v.mp4", ["recursion", "inflation"])
    body = {"video_id": v["video_id"], "learner_id": "l1"}
    r = client.post("/assessments", json=body)
    assert r.status_code == 409 and r.json()["error"]["details"]["progress"] == 0.0

    p = client.post(f"/videos/{v['video_id']}/progress", json={"learner_id": "l1", "progress": 0.5}).json()
    assert p["assessment_unlocked"] is False
    assert client.post("/assessments", json=body).status_code == 409
    # progress never goes backwards
    client.post(f"/videos/{v['video_id']}/progress", json={"learner_id": "l1", "progress": 0.95})
    p = client.post(f"/videos/{v['video_id']}/progress", json={"learner_id": "l1", "progress": 0.1}).json()
    assert p["progress"] == 0.95 and p["assessment_unlocked"] is True
    assert client.post("/assessments", json=body).status_code == 201
    assert client.post(f"/videos/{v['video_id']}/progress", json={"learner_id": "l1", "progress": 1.5}).status_code == 422


def test_assessment_mix_topics_and_hidden_answers(client, sample_video):
    _, a = _ready_assessment(client, sample_video, num_questions=6, mix={"mcq": 1, "true_false": 1, "short_answer": 1})
    types = [q["type"] for q in a["questions"]]
    assert types == ["mcq"] * 2 + ["true_false"] * 2 + ["short_answer"] * 2
    assert all(q["topic"] for q in a["questions"])
    assert all(len(q["options"]) == 4 for q in a["questions"] if q["type"] == "mcq")
    assert all("correct_answer" not in q and "rubric" not in q for q in a["questions"])
    assert client.get(f"/assessments/{a['id']}").json() == a


def test_perfect_attempt_and_report(client, sample_video):
    _, a = _ready_assessment(client, sample_video)
    truth = _correct_answers(client, a["id"])
    answers = [
        {"question_id": qid, "response": q.correct_answer if q.type != "short_answer" else q.reference_answer}
        for qid, q in truth.items()
    ]
    r = client.post("/attempts", json={"assessment_id": a["id"], "learner_id": "learner-1", "answers": answers})
    assert r.status_code == 201, r.text
    att = r.json()
    assert att["percentage"] == 100.0
    assert all(x["is_correct"] and x["feedback"] for x in att["answers"])
    assert {x["evaluator"] for x in att["answers"]} == {"rule", "llm"}

    rep = client.get(att["report_url"]).json()
    assert rep["overall"]["percentage"] == 100.0 and not rep["weaknesses"] and rep["strengths"]
    assert rep["ai_summary"] and rep["summary_source"] == "llm"


def test_mixed_attempt_scores_feedback_and_weaknesses(client, sample_video):
    _, a = _ready_assessment(client, sample_video)
    truth = _correct_answers(client, a["id"])
    answers = []
    for i, (qid, q) in enumerate(truth.items()):
        if i == 0:
            continue  # unanswered
        if q.type == "mcq":
            wrong = next(o for o in q.options if o != q.correct_answer)
            answers.append({"question_id": qid, "response": wrong})
        elif q.type == "true_false":
            answers.append({"question_id": qid, "response": "false" if q.correct_answer == "true" else "true"})
        else:
            answers.append({"question_id": qid, "response": "I am not sure about this one."})
    att = client.post("/attempts", json={"assessment_id": a["id"], "learner_id": "learner-1", "answers": answers}).json()
    assert att["total_score"] < att["max_score"]
    first = att["answers"][0]
    assert first["evaluator"] == "none" and first["score"] == 0 and first["feedback"].startswith("Not attempted")
    wrong_mcq = next(x for x in att["answers"] if x["type"] == "mcq" and x["response"])
    assert not wrong_mcq["is_correct"] and "correct answer is" in wrong_mcq["feedback"]
    assert wrong_mcq["improvement_areas"]

    rep = client.get(att["report_url"]).json()
    assert rep["weaknesses"] and rep["improvement_areas"] and rep["suggestions"]
    assert {t["topic"] for t in rep["topic_breakdown"]} == {x["topic"] for x in att["answers"]}
    assert all(w["rewatch"] for w in rep["weaknesses"])


def test_attempt_validation(client, sample_video):
    _, a = _ready_assessment(client, sample_video)
    base = {"assessment_id": a["id"], "learner_id": "learner-1"}
    assert client.post("/attempts", json=base | {"answers": [{"question_id": "nope", "response": "a"}]}).status_code == 422
    q = a["questions"][0]["id"]
    dup = [{"question_id": q, "response": "a"}] * 2
    assert client.post("/attempts", json=base | {"answers": dup}).status_code == 422
    assert client.post("/attempts", json=base | {"learner_id": "other", "answers": []}).status_code == 422
    assert client.post("/attempts", json={**base, "assessment_id": "nope", "answers": []}).status_code == 404


def test_judge_failure_returns_502_and_persists_nothing(client, sample_video, monkeypatch):
    from app.db.models import Attempt
    from app.db.session import SessionLocal
    from app.llm.factory import get_llm

    _, a = _ready_assessment(client, sample_video)
    llm = get_llm()
    original = llm.structured

    def failing(system, user, schema, *, tier="default"):
        if schema is JudgedAnswers:
            raise LLMError("rate limited")
        return original(system, user, schema, tier=tier)

    monkeypatch.setattr(llm, "structured", failing)
    answers = [{"question_id": q["id"], "response": "some text"} for q in a["questions"]]
    r = client.post("/attempts", json={"assessment_id": a["id"], "learner_id": "learner-1", "answers": answers})
    assert r.status_code == 502 and r.json()["error"]["code"] == "llm_error"
    with SessionLocal() as db:
        assert db.query(Attempt).count() == 0


def test_report_falls_back_to_template_when_llm_fails(client, sample_video, monkeypatch):
    from app.llm.factory import get_llm

    _, a = _ready_assessment(client, sample_video)
    llm = get_llm()
    original = llm.structured

    def failing(system, user, schema, *, tier="default"):
        if schema is ReportNarrative:
            raise LLMError("down")
        return original(system, user, schema, tier=tier)

    monkeypatch.setattr(llm, "structured", failing)
    r = client.post("/attempts", json={"assessment_id": a["id"], "learner_id": "learner-1", "answers": []})
    assert r.status_code == 201
    rep = client.get(r.json()["report_url"]).json()
    assert rep["summary_source"] == "template" and "0.0%" in rep["ai_summary"]


@pytest.mark.parametrize("path", ["/videos/x/retrieve"])
def test_retrieve_unknown_video(client, path):
    assert client.post(path, json={"query": "q"}).status_code == 404
