"""Learning report: deterministic statistics + one LLM call for the prose.

Scores, topic breakdown, strengths and weaknesses are *computed* (reproducible, auditable, free).
Only the learner-facing summary and suggestions are generated; if that call fails the report is
still produced with a template summary (`summary_source="template"`).
"""

from collections import OrderedDict

from sqlalchemy.orm import Session

from app.config import Settings
from app.core.exceptions import LLMError
from app.core.logging import get_logger
from app.db.models import Attempt, Report, TranscriptChunk, Video
from app.llm import prompts
from app.llm.base import LLMProvider
from app.llm.schemas import ReportNarrative
from app.services.grading import fmt_ts

logger = get_logger(__name__)


def _band(pct: float) -> str:
    if pct >= 85:
        return "excellent"
    if pct >= 70:
        return "good"
    if pct >= 50:
        return "fair"
    return "needs improvement"


def compute_stats(attempt: Attempt, chunks: dict[str, TranscriptChunk], strength: float, weakness: float) -> dict:
    """Pure aggregation over an evaluated attempt (unit-tested)."""
    answers = sorted(attempt.answers, key=lambda a: a.question.position)
    total = len(answers)
    score = sum(a.score for a in answers)
    pct = round(100 * score / total, 1) if total else 0.0

    overall = {
        "score": round(score, 2),
        "max_score": float(total),
        "percentage": pct,
        "correct": sum(a.is_correct for a in answers),
        "total_questions": total,
        "attempted": sum(a.response is not None for a in answers),
        "band": _band(pct),
    }

    by_type: dict[str, dict] = {}
    for a in answers:
        t = by_type.setdefault(a.question.type, {"questions": 0, "correct": 0, "score": 0.0})
        t["questions"] += 1
        t["correct"] += int(a.is_correct)
        t["score"] += a.score
    for t in by_type.values():
        t["avg_score"] = round(t.pop("score") / t["questions"], 2)
        t["accuracy"] = round(t["correct"] / t["questions"], 2)

    topics: OrderedDict[str, dict] = OrderedDict()
    for a in answers:
        q = a.question
        t = topics.setdefault(q.topic, {"topic": q.topic, "questions": 0, "correct": 0, "score": 0.0, "_chunks": set()})
        t["questions"] += 1
        t["correct"] += int(a.is_correct)
        t["score"] += a.score
        t["_chunks"].update(c for c in q.source_chunk_ids if c in chunks)

    breakdown = []
    for t in topics.values():
        src = sorted((chunks[c] for c in t.pop("_chunks")), key=lambda c: c.start_time)
        avg = round(t.pop("score") / t["questions"], 2)
        breakdown.append(
            {
                **t,
                "avg_score": avg,
                "accuracy": round(t["correct"] / t["questions"], 2),
                "status": "strength" if avg >= strength else "weakness" if avg < weakness else "developing",
                "video_segments": [f"{fmt_ts(c.start_time)}-{fmt_ts(c.end_time)}" for c in src],
            }
        )

    strengths = [{"topic": t["topic"], "avg_score": t["avg_score"]} for t in breakdown if t["status"] == "strength"]
    weaknesses = [
        {"topic": t["topic"], "avg_score": t["avg_score"], "rewatch": t["video_segments"]}
        for t in breakdown
        if t["status"] == "weakness"
    ]

    seen: set[str] = set()
    improvement: list[str] = []
    for a in answers:
        for area in a.improvement_areas:
            if area.lower() not in seen:
                seen.add(area.lower())
                improvement.append(area)

    return {
        "overall": overall,
        "by_type": by_type,
        "topic_breakdown": breakdown,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "improvement_areas": improvement,
    }


def _template_narrative(stats: dict) -> ReportNarrative:
    o = stats["overall"]
    strong = ", ".join(s["topic"] for s in stats["strengths"])
    weak = ", ".join(w["topic"] for w in stats["weaknesses"])
    summary = (
        f"You scored {o['score']}/{o['max_score']:g} ({o['percentage']}%, {o['band']}), answering "
        f"{o['correct']} of {o['total_questions']} questions correctly."
        + (f" You showed strong understanding of {strong}." if strong else "")
        + (f" The topics that need more work are {weak}." if weak else "")
    )
    suggestions = [
        f"Rewatch {', '.join(w['rewatch']) or 'the section'} on {w['topic']} and summarise it in your own words."
        for w in stats["weaknesses"]
    ] or ["Consolidate by explaining the video's main ideas to someone else without notes."]
    return ReportNarrative(summary=summary, suggestions=suggestions)


def build_report(db: Session, llm: LLMProvider, settings: Settings, attempt: Attempt) -> Report:
    chunk_ids = {c for a in attempt.answers for c in a.question.source_chunk_ids}
    chunks = {c.id: c for c in db.query(TranscriptChunk).filter(TranscriptChunk.id.in_(chunk_ids))}
    stats = compute_stats(attempt, chunks, settings.strength_threshold, settings.weakness_threshold)

    video = db.get(Video, attempt.answers[0].question.assessment.video_id) if attempt.answers else None
    source = "llm"
    try:
        llm_stats = {k: stats[k] for k in ("overall", "by_type", "topic_breakdown", "strengths", "weaknesses")}
        llm_stats["improvement_areas"] = stats["improvement_areas"][:10]
        system, user = prompts.report_narrative(video.title if video else "", llm_stats)
        narrative = llm.structured(system, user, ReportNarrative)
    except LLMError as exc:
        logger.warning("report narrative fell back to template", extra={"error": exc.message})
        narrative, source = _template_narrative(stats), "template"

    report = Report(
        attempt_id=attempt.id,
        overall=stats["overall"],
        by_type=stats["by_type"],
        topic_breakdown=stats["topic_breakdown"],
        strengths=stats["strengths"],
        weaknesses=stats["weaknesses"],
        improvement_areas=stats["improvement_areas"],
        suggestions=narrative.suggestions,
        ai_summary=narrative.summary,
        summary_source=source,
    )
    db.add(report)
    db.flush()
    return report
