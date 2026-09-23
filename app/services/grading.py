"""Deterministic grading for closed question types (MCQ, true/false).

No LLM involved: the correct answer is known, so a normalised comparison is cheaper, faster,
reproducible and cannot hallucinate.
"""

import re
import string

_TRUE = {"true", "t", "yes", "y", "1"}
_FALSE = {"false", "f", "no", "n", "0"}
_LETTER = re.compile(r"^\(?([a-z])[\).:]?$")


def normalize(text: str | None) -> str:
    if text is None:
        return ""
    text = text.strip().lower()
    text = text.translate(str.maketrans("", "", string.punctuation.replace("-", "")))
    return " ".join(text.split())


def parse_bool(text: str | None) -> bool | None:
    n = normalize(text)
    if n in _TRUE:
        return True
    if n in _FALSE:
        return False
    return None


def resolve_mcq_choice(response: str | None, options: list[str]) -> str | None:
    """Map a learner response to one of the options.

    Accepts the option text itself (case/punctuation-insensitive), a letter ("b", "B)", "(c)"),
    or a 1-based number ("2"). Returns the matching option text, or None if it matches nothing.
    """
    if response is None:
        return None
    raw = response.strip().lower()
    m = _LETTER.match(raw)
    if m:
        idx = ord(m.group(1)) - ord("a")
        if 0 <= idx < len(options):
            return options[idx]
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return options[idx]
    n = normalize(response)
    for opt in options:
        if normalize(opt) == n:
            return opt
    return None


def grade_mcq(response: str | None, options: list[str], correct: str) -> tuple[bool, str | None]:
    chosen = resolve_mcq_choice(response, options)
    return (chosen is not None and normalize(chosen) == normalize(correct)), chosen


def grade_true_false(response: str | None, correct: str) -> tuple[bool, bool | None]:
    given = parse_bool(response)
    expected = parse_bool(correct)
    return (given is not None and given == expected), given


def fmt_ts(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"
