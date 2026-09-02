from visa_ping.browser import match_kba_answer

QA = (
    ("你母亲的姓名", "mom"),
    ("What was your first pet's name?", "rex"),
    ("出生的城市", "wuhan"),
)


def test_exact_match():
    assert match_kba_answer("你母亲的姓名", QA) == "mom"


def test_label_with_decorations():
    # Labels often carry a trailing asterisk/colon or numbering.
    assert match_kba_answer("2. 你母亲的姓名 *", QA) == "mom"


def test_case_whitespace_punctuation_insensitive():
    assert match_kba_answer("what was your FIRST pet's  name", QA) == "rex"


def test_substring_either_direction():
    # Configured question may be a fragment of the on-screen label...
    assert match_kba_answer("请回答：出生的城市是哪里？", QA) == "wuhan"
    # ...or the label a fragment of the configured question.
    long_qa = (("请回答：出生的城市是哪里？", "wuhan"),)
    assert match_kba_answer("出生的城市", long_qa) == "wuhan"


def test_no_match_returns_none():
    assert match_kba_answer("最喜欢的老师", QA) is None


def test_empty_label():
    assert match_kba_answer("", QA) is None
    assert match_kba_answer("？*：", QA) is None
