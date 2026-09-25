"""End-to-end demo against a running API; saves every response under docs/sample-outputs/.

    python scripts/demo.py                         # all videos in sample_data/videos/
    python scripts/demo.py --video recursion_mit.mp4 --base-url http://localhost:8000

Flow per video: register → poll until indexed → retrieval query → report progress (100%) →
generate assessment → submit a *simulated learner's* answers → fetch evaluation + report.

A simulated learner answers each assessment. Profiles (strong / average / struggling) rotate across
videos so the sample evaluations and reports show different outcomes. It reads the answer
key straight from the local database, because the API (correctly) never exposes correct answers.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "docs" / "sample-outputs"
QUERIES = {
    "recursion": "What happens if a recursive function has no base case?",
    "inflation": "Why does inflation reduce the purchasing power of money?",
    "photosynthesis": "Where does the oxygen released during photosynthesis come from?",
}


def _save(folder: Path, name: str, data) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _check(r: requests.Response, expected: int) -> dict:
    if r.status_code != expected:
        raise SystemExit(f"{r.request.method} {r.url} -> {r.status_code}: {r.text}")
    return r.json()


# Simulated learners of different ability, so the sample reports show different outcomes.
# Each profile maps a question's position to: "right", "wrong", "partial" (short answers) or "skip".
PROFILES = {
    "strong": lambda i, n: "wrong" if i == 1 else "partial" if i == n - 2 else "right",
    "average": lambda i, n: "skip" if i == n - 1 else "wrong" if i % 3 == 2 else "partial" if i % 2 else "right",
    "struggling": lambda i, n: (
        "right" if i in (0, n - 3) else "skip" if i == n - 1 else "partial" if i % 2 else "wrong"
    ),
}


def simulated_answers(assessment_id: str, profile: str) -> list[dict]:
    """Build a deterministic answer sheet for the given learner profile from the answer key."""
    from app.db.models import Assessment
    from app.db.session import SessionLocal

    decide = PROFILES[profile]
    with SessionLocal() as db:
        questions = sorted(db.get(Assessment, assessment_id).questions, key=lambda q: q.position)
        answers = []
        for i, q in enumerate(questions):
            outcome = decide(i, len(questions))
            if outcome == "skip":
                continue
            if q.type == "mcq":
                opts = q.options or []
                resp = q.correct_answer if outcome == "right" else next(o for o in opts if o != q.correct_answer)
            elif q.type == "true_false":
                flipped = "false" if q.correct_answer == "true" else "true"
                resp = q.correct_answer if outcome == "right" else flipped
            else:
                ref = q.reference_answer or ""
                words = ref.split()
                if outcome == "right":
                    resp = ref
                elif outcome == "partial":  # a vague answer with only part of the idea
                    resp = " ".join(words[: max(4, len(words) // 3)]) + "... I think."
                else:
                    resp = "I'm not sure, maybe it is just a definition from the video."
            answers.append({"question_id": q.id, "response": resp})
        return answers


def report_markdown(video: dict, assessment: dict, attempt: dict, report: dict) -> str:
    o = report["overall"]
    lines = [
        f"# Learning report — {video['title']}",
        "",
        f"**Learner:** `{attempt['learner_id']}` · **Score:** {o['score']}/{o['max_score']:g} "
        f"({o['percentage']}%, {o['band']}) · **Correct:** {o['correct']}/{o['total_questions']} "
        f"· **Summary by:** {report['summary_source']}",
        "",
        "## AI summary",
        "",
        report["ai_summary"],
        "",
        "## Topic-wise analysis",
        "",
        "| Topic | Questions | Correct | Avg score | Status | Video segments |",
        "|---|---|---|---|---|---|",
        *[
            f"| {t['topic']} | {t['questions']} | {t['correct']} | {t['avg_score']} | {t['status']} "
            f"| {', '.join(t['video_segments'])} |"
            for t in report["topic_breakdown"]
        ],
        "",
        "## By question type",
        "",
        "| Type | Questions | Correct | Avg score |",
        "|---|---|---|---|",
        *[f"| {k} | {v['questions']} | {v['correct']} | {v['avg_score']} |" for k, v in report["by_type"].items()],
        "",
        "## Strengths",
        "",
        *([f"- {s['topic']} (avg {s['avg_score']})" for s in report["strengths"]] or ["- (none yet)"]),
        "",
        "## Weaknesses",
        "",
        *(
            [f"- {w['topic']} (avg {w['avg_score']}) — rewatch {', '.join(w['rewatch'])}" for w in report["weaknesses"]]
            or ["- (none)"]
        ),
        "",
        "## Suggestions",
        "",
        *[f"- {s}" for s in report["suggestions"]],
        "",
        "## Areas of improvement",
        "",
        *([f"- {a}" for a in report["improvement_areas"]] or ["- (none)"]),
        "",
        "## Question-by-question evaluation",
        "",
    ]
    for a in attempt["answers"]:
        mark = "✅" if a["is_correct"] else "❌"
        lines += [
            f"### Q{a['position'] + 1}. [{a['type']}] {a['prompt']}",
            "",
            f"- **Topic:** {a['topic']}",
            f"- **Learner answer:** {a['response'] if a['response'] is not None else '_(not answered)_'}",
            f"- **Result:** {mark} score {a['score']} (graded by: {a['evaluator']})",
            f"- **Feedback:** {a['feedback']}",
        ]
        if a["improvement_areas"]:
            lines.append(f"- **Improve:** {'; '.join(a['improvement_areas'])}")
        lines.append("")
    return "\n".join(lines)


def run_one(base: str, sample: str, learner: str, num_questions: int, profile: str, out: Path = OUT) -> None:
    slug = Path(sample).stem
    folder = out / slug
    print(f"\n=== {sample} ===")

    from scripts.fetch_sample_videos import SAMPLES

    title = SAMPLES[slug][2] if slug in SAMPLES else None
    accepted = _check(requests.post(f"{base}/videos", data={"sample_path": sample, "title": title}), 202)
    vid = accepted["video_id"]
    print(f"queued video {vid} (task {accepted['task_id']})")
    t0 = time.time()
    while True:
        video = _check(requests.get(f"{base}/videos/{vid}"), 200)
        if video["status"] in ("indexed", "failed"):
            break
        print(f"  status={video['status']} ({time.time() - t0:.0f}s)")
        time.sleep(5)
    if video["status"] == "failed":
        raise SystemExit(f"processing failed: {video['error']}")
    print(f"indexed in {time.time() - t0:.0f}s: {video['chunk_count']} chunks, topics={video['topics']}")
    _save(folder, "01_video.json", video)
    _save(folder, "01_task.json", _check(requests.get(f"{base}/tasks/{accepted['task_id']}"), 200))

    query = next((q for k, q in QUERIES.items() if k in slug.lower()), f"What is the main idea of {video['title']}?")
    retrieval = _check(requests.post(f"{base}/videos/{vid}/retrieve", json={"query": query, "top_k": 3}), 200)
    _save(folder, "02_retrieval.json", retrieval)
    print(f"retrieval: top hit {retrieval['results'][0]['timestamp']} score={retrieval['results'][0]['score']}")

    locked = requests.post(f"{base}/assessments", json={"video_id": vid, "learner_id": learner})
    _save(folder, "03_assessment_locked_409.json", {"status_code": locked.status_code, **locked.json()})
    _save(
        folder,
        "03_progress.json",
        _check(requests.post(f"{base}/videos/{vid}/progress", json={"learner_id": learner, "progress": 1.0}), 200),
    )

    assessment = _check(
        requests.post(
            f"{base}/assessments", json={"video_id": vid, "learner_id": learner, "num_questions": num_questions}
        ),
        201,
    )
    _save(folder, "04_assessment.json", assessment)
    print(f"assessment {assessment['id']}: {[q['type'] for q in assessment['questions']]}")

    answers = simulated_answers(assessment["id"], profile)
    _save(
        folder,
        "05_attempt_request.json",
        {"learner_profile": profile, "assessment_id": assessment["id"], "learner_id": learner, "answers": answers},
    )
    attempt = _check(
        requests.post(
            f"{base}/attempts", json={"assessment_id": assessment["id"], "learner_id": learner, "answers": answers}
        ),
        201,
    )
    _save(folder, "05_attempt_evaluation.json", attempt)
    print(f"attempt {attempt['id']}: {attempt['total_score']}/{attempt['max_score']} ({attempt['percentage']}%)")

    report = _check(requests.get(f"{base}{attempt['report_url']}"), 200)
    _save(folder, "06_report.json", report)
    (folder / "06_report.md").write_text(report_markdown(video, assessment, attempt, report), encoding="utf-8")
    print(f"report saved -> {folder}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("LMS_BASE_URL", "http://localhost:8000"))
    ap.add_argument("--video", action="append", help="file name in sample_data/videos (repeatable)")
    ap.add_argument("--learner", default="demo-learner-001")
    ap.add_argument("--num-questions", type=int, default=8)
    ap.add_argument("--out", type=Path, default=OUT, help="output directory (default: docs/sample-outputs)")
    ap.add_argument("--profile", choices=list(PROFILES), help="learner profile (default: rotate per video)")
    args = ap.parse_args()

    from app.config import get_settings

    videos = args.video or sorted(
        p.name
        for p in Path(get_settings().sample_videos_dir).iterdir()
        if p.suffix.lower() in get_settings().allowed_video_extensions
    )
    if not videos:
        raise SystemExit("No sample videos found — run: python scripts/fetch_sample_videos.py")
    health = _check(requests.get(f"{args.base_url}/health"), 200)
    print(f"API health: {health}")
    profiles = list(PROFILES)
    for i, v in enumerate(videos):
        run_one(
            args.base_url, v, args.learner, args.num_questions, args.profile or profiles[i % len(profiles)], args.out
        )


if __name__ == "__main__":
    main()
