from app.core.intent import classify_intent
from app.core.models import Intent


def test_classifies_ltc_points_query():
    assert classify_intent("附近有哪些長照據點") == Intent.LTC_POINTS


def test_classifies_law_query():
    assert classify_intent("長照法規對居家服務的規定是什麼") == Intent.LAW_SOP


def test_classifies_identity_query():
    assert classify_intent("你是誰？") == Intent.IDENTITY


def test_defaults_to_law_sop_when_ambiguous():
    assert classify_intent("我想了解一下") == Intent.LAW_SOP
