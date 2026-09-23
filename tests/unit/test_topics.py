from app.services.video_pipeline import cap_topics


def test_within_budget_is_unchanged():
    labels = ["A", "A", "B", "B", "C"]
    assert cap_topics(labels, 3) == labels


def test_rarest_topic_folds_into_neighbour():
    labels = ["Intro", "Intro", "Light", "Light", "Light+Dark", "Dark", "Dark"]
    out = cap_topics(labels, 3)
    assert len(set(out)) == 3
    assert out[:4] == ["Intro", "Intro", "Light", "Light"] and out[5:] == ["Dark", "Dark"]
    assert out[4] in {"Light", "Dark"}


def test_budget_of_two_on_many_topics():
    out = cap_topics(["a", "b", "c", "d", "e", "f"], 2)
    assert len(set(out)) == 2 and len(out) == 6
