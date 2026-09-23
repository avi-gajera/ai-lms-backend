import pytest

from app.services.grading import fmt_ts, grade_mcq, grade_true_false, resolve_mcq_choice

OPTS = ["A base case", "A loop counter", "A global variable", "A second function"]


@pytest.mark.parametrize(
    "response,expected",
    [
        ("A base case", "A base case"),
        ("  a BASE case. ", "A base case"),
        ("a", "A base case"),
        ("B)", "A loop counter"),
        ("(c)", "A global variable"),
        ("4", "A second function"),
        ("e", None),
        ("5", None),
        ("something else", None),
        (None, None),
    ],
)
def test_resolve_mcq_choice(response, expected):
    assert resolve_mcq_choice(response, OPTS) == expected


def test_grade_mcq():
    assert grade_mcq("a", OPTS, "A base case") == (True, "A base case")
    assert grade_mcq("B", OPTS, "A base case") == (False, "A loop counter")
    assert grade_mcq("", OPTS, "A base case") == (False, None)


@pytest.mark.parametrize(
    "response,correct,ok",
    [("true", "true", True), ("T", "true", True), ("Yes", "true", True), ("false", "true", False),
     ("no", "false", True), ("maybe", "true", False), (None, "false", False)],
)
def test_grade_true_false(response, correct, ok):
    assert grade_true_false(response, correct)[0] is ok


def test_fmt_ts():
    assert fmt_ts(0) == "00:00"
    assert fmt_ts(125.9) == "02:05"
    assert fmt_ts(None) == "?"
