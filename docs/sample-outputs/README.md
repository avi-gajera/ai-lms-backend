# Sample outputs

These are real outputs from the system, produced by `python scripts/demo.py` against the running API. The setup was:

- LLM: Groq `openai/gpt-oss-120b` / `gpt-oss-20b`
- Transcription: faster-whisper `base`
- Embeddings: `bge-small-en-v1.5`
- Videos: the three sample lectures listed in [`sample_data/README.md`](../../sample_data/README.md)

Nothing here was edited by hand.

A **simulated learner** answers each assessment. The profile rotates by video (strong, average, struggling) so that the reports show a range of outcomes. The answer sheet sent is saved in `05_attempt_request.json`.

| Video | Learner profile | Report |
|---|---|---|
| Introduction to inflation (Khan Academy) | strong | [inflation_khan/06_report.md](inflation_khan/06_report.md) |
| Photosynthesis (Khan Academy) | average | [photosynthesis_khan/06_report.md](photosynthesis_khan/06_report.md) |
| 1.10.7 Recursive Functions (MIT OCW) | struggling | [recursion_mit/06_report.md](recursion_mit/06_report.md) |

## Files per video

| File | API call | Shows |
|---|---|---|
| `01_video.json` | `GET /videos/{id}` | Processing result: status, duration, language, chunk count, topics |
| `01_task.json` | `GET /tasks/{id}` | Background job status and result |
| `02_retrieval.json` | `POST /videos/{id}/retrieve` | Top-3 chunks for a question, with similarity scores, timestamps and topics |
| `03_assessment_locked_409.json` | `POST /assessments` before any progress | The completion-threshold gate (409 plus details) |
| `03_progress.json` | `POST /videos/{id}/progress` | Progress recorded, assessment unlocked |
| `04_assessment.json` | `POST /assessments` | Generated questions as the learner sees them (no answers) |
| `05_attempt_request.json` | — | The simulated learner's answer sheet |
| `05_attempt_evaluation.json` | `POST /attempts` | Per-question score, correctness, feedback and improvement areas, with correct and reference answers revealed |
| `06_report.json` | `GET /attempts/{id}/report` | The learning report |
| `06_report.md` | — | The same report rendered for reading, plus a question-by-question review |

The same run is stored in [`data/demo.db`](../../data/demo.db).
