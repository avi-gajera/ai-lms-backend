"""Prompt templates (LangChain `ChatPromptTemplate`, used purely for templating).

Conventions:
- Every piece of input data is passed as ONE fenced ```json block in the user message. This keeps
  the model's view of the data unambiguous and lets the offline FakeLLMProvider read the same input.
- Every prompt that produces learner-facing content is *grounded*: the model is told to use only
  the supplied transcript context.
- `PROMPT_VERSION` is logged with each call so outputs can be traced to the prompt that made them.
"""

import json
from typing import Any

from langchain_core.prompts import ChatPromptTemplate

PROMPT_VERSION = "2026-09-23.4"


def _render(template: ChatPromptTemplate, payload: dict[str, Any], **kwargs: Any) -> tuple[str, str]:
    data = json.dumps(payload, ensure_ascii=False, indent=1)
    system_msg, user_msg = template.format_messages(data=data, **kwargs)
    return str(system_msg.content), str(user_msg.content)


# --- Topic labelling ---------------------------------------------------------------------------

_TOPIC_LABEL = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You label segments of an educational video transcript with the topic they cover. "
            "Use short topic names (2-5 words, Title Case). Topics are used to group assessment "
            "results, so they must be BROAD: one topic should normally span several consecutive "
            "chunks, and sub-aspects of one subject (its definition, an example, a calculation) "
            "belong to the SAME topic. Reuse names from `known_topics` whenever they fit, and never "
            "use more than `max_topics` distinct topics for the whole video.",
        ),
        (
            "human",
            "Video title: {title}\n\nLabel every chunk below. Return one label per chunk_index.\n\n"
            "```json\n{data}\n```",
        ),
    ]
)


def topic_label(title: str, chunks: list[dict], known_topics: list[str], max_topics: int) -> tuple[str, str]:
    return _render(
        _TOPIC_LABEL,
        {"max_topics": max_topics, "known_topics": known_topics, "chunks": chunks},
        title=title,
    )


# --- Question generation -----------------------------------------------------------------------

_TYPE_RULES = {
    "mcq": (
        "multiple-choice questions",
        "Each has exactly 4 distinct options: one correct and three plausible distractors built "
        "from common misunderstandings of the content. `correct_answer` is the exact text of the "
        "correct option. Vary the position of the correct option.",
    ),
    "true_false": (
        "true/false statements",
        "Each `prompt` is a single statement whose truth can only be judged by understanding the "
        "content (not by spotting a copied sentence). Set `is_true` accordingly and include both "
        "true and false statements; false ones should contain a plausible misconception.",
    ),
    "short_answer": (
        "short-answer questions",
        "Each requires a 1-3 sentence explanation. Give a `reference_answer` and a `rubric` listing "
        "the 2-4 key points a full-credit answer must contain.",
    ),
}

_QUESTION_GEN = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are an expert instructional designer writing an assessment for a learner who has "
            "just watched an educational video. You are writing {type_name}. Rules:\n"
            "1. Use ONLY the transcript chunks provided. Every question must be answerable from "
            "them; never rely on outside knowledge.\n"
            "2. Test UNDERSTANDING, not recall: ask why/how, apply a concept to a new example, "
            "compare ideas, identify a misconception. Avoid trivia about wording, numbers or names.\n"
            "3. {type_rules}\n"
            "4. `topic` is the `topic` value of the chunk the question is based on, copied exactly; "
            "`source_chunk_ids` lists the ids of the chunk(s) that contain the answer.\n"
            "5. Spread the questions across different chunks and topics.\n"
            "6. Every question tests a DIFFERENT idea, and none may repeat an idea from "
            "`already_asked`.",
        ),
        (
            "human",
            "Video title: {title}\n\nWrite exactly {n} {type_name}.\n\n```json\n{data}\n```",
        ),
    ]
)


def question_gen(title: str, chunks: list[dict], qtype: str, n: int, already_asked: list[str]) -> tuple[str, str]:
    type_name, type_rules = _TYPE_RULES[qtype]
    return _render(
        _QUESTION_GEN,
        {"count": n, "already_asked": already_asked, "chunks": chunks},
        title=title,
        n=n,
        type_name=type_name,
        type_rules=type_rules,
    )


# --- Short-answer judge ------------------------------------------------------------------------

_JUDGE = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are a fair, rigorous grader of short written answers about an educational video. "
            "For each item, compare the learner's response to the reference answer and rubric, "
            "using the source transcript context as ground truth. Grade meaning, not wording: a "
            "correct answer in different words gets full credit; spelling and grammar do not "
            "matter.\n"
            "Scoring rubric: 1.0 = all key points, accurate; 0.7-0.9 = mostly correct, minor "
            "omission or imprecision; 0.4-0.6 = partially correct, missing a key idea; "
            "0.1-0.3 = mostly wrong but shows some relevant understanding; 0.0 = wrong, "
            "irrelevant or empty.\n"
            "feedback: 2-3 sentences to the learner — what they got right, what was missing or "
            "wrong. improvement_areas: specific concepts to revisit (empty list if score is 1.0). "
            "Return exactly one result per item, echoing its question_id.",
        ),
        (
            "human",
            "Grade each entry of `answers_to_grade`. Respond with `results`: a flat JSON array "
            "containing one object per entry.\n\n```json\n{data}\n```",
        ),
    ]
)


def judge(items: list[dict]) -> tuple[str, str]:
    return _render(_JUDGE, {"answers_to_grade": items})


# --- Report narrative --------------------------------------------------------------------------

_REPORT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are a supportive learning coach. Given pre-computed statistics from a learner's "
            "assessment on one video, write (a) a summary paragraph addressed to the learner "
            "(80-150 words): overall performance, what they understand well, where they struggle; "
            "and (b) 3-5 specific, actionable suggestions. Where a weak topic has a timestamp, "
            "point the learner to rewatch that part (format mm:ss). Base everything strictly on "
            "the statistics given; do not invent scores.",
        ),
        ("human", "Video title: {title}\n\n```json\n{data}\n```"),
    ]
)


def report_narrative(title: str, stats: dict) -> tuple[str, str]:
    return _render(_REPORT, stats, title=title)
