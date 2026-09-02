from datetime import date, timedelta
from pathlib import Path

import pytest

from visa_ping.config import (
    ConfigError,
    DateBound,
    load_config,
    load_email_creds,
    load_login_creds,
)

FUTURE = (date.today() + timedelta(days=200)).isoformat()
NEAR = (date.today() + timedelta(days=30)).isoformat()

VALID = f"""
[consulate]
name = "SHANGHAI"

[dates]
earliest = {NEAR}
latest = {FUTURE}
"""


def write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(content, encoding="utf-8")
    return p


def test_valid_config_with_defaults(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.consulate.name == "SHANGHAI"
    assert cfg.booking.enabled is False
    assert cfg.booking.dry_run is True
    assert cfg.monitor.months_to_scan == 3
    assert cfg.scenario == "reschedule"  # default
    # relative paths resolve against the config file's directory
    assert cfg.paths.state_file == tmp_path / "state.json"


def test_scenario_schedule(tmp_path):
    cfg = load_config(write(tmp_path, 'scenario = "schedule"\n' + VALID))
    assert cfg.scenario == "schedule"


def test_scenario_invalid(tmp_path):
    with pytest.raises(ConfigError, match="scenario"):
        load_config(write(tmp_path, 'scenario = "bogus"\n' + VALID))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_missing_consulate_and_dates(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, "[monitor]\nmonths_to_scan = 2\n"))
    msg = str(e.value)
    assert "name` or `guid" in msg
    assert "earliest" in msg


def test_earliest_after_latest(tmp_path):
    bad = VALID.replace(f"earliest = {NEAR}", f"earliest = {FUTURE}").replace(
        f"latest = {FUTURE}", f"latest = {NEAR}"
    )
    with pytest.raises(ConfigError, match="is after"):
        load_config(write(tmp_path, bad))


def test_latest_in_past(tmp_path):
    bad = f"""
[consulate]
name = "SHANGHAI"
[dates]
earliest = 2020-01-01
latest = 2020-06-01
"""
    with pytest.raises(ConfigError, match="in the past"):
        load_config(write(tmp_path, bad))


def test_relative_dates(tmp_path):
    cfg_text = """
[consulate]
name = "SHANGHAI"
[dates]
earliest = "today+5"
latest = "today + 90"
"""
    cfg = load_config(write(tmp_path, cfg_text))
    today = date.today()
    earliest, latest = cfg.dates.resolve(today)
    assert earliest == today + timedelta(days=5)
    assert latest == today + timedelta(days=90)
    assert cfg.dates.earliest.describe(today).startswith("today+5 (")


def test_relative_bare_today_and_mixed(tmp_path):
    cfg_text = f"""
[consulate]
name = "SHANGHAI"
[dates]
earliest = "today"
latest = {FUTURE}
"""
    cfg = load_config(write(tmp_path, cfg_text))
    today = date.today()
    earliest, latest = cfg.dates.resolve(today)
    assert earliest == today
    assert latest == date.fromisoformat(FUTURE)


def test_relative_resolution_tracks_today():
    bound = DateBound(offset_days=5)
    d1 = date(2026, 9, 1)
    d2 = date(2026, 9, 10)
    assert bound.resolve(d1) == date(2026, 9, 6)
    assert bound.resolve(d2) == date(2026, 9, 15)  # re-resolves, no caching


def test_relative_invalid_string(tmp_path):
    cfg_text = """
[consulate]
name = "SHANGHAI"
[dates]
earliest = "yesterday+5"
latest = "today+90"
"""
    with pytest.raises(ConfigError, match="today"):
        load_config(write(tmp_path, cfg_text))


def test_relative_earliest_after_fixed_latest(tmp_path):
    cfg_text = f"""
[consulate]
name = "SHANGHAI"
[dates]
earliest = "today+400"
latest = {FUTURE}
"""
    with pytest.raises(ConfigError, match="is after"):
        load_config(write(tmp_path, cfg_text))


def test_bad_interval_ordering(tmp_path):
    bad = VALID + "\n[monitor]\ncheck_interval_min_seconds = 300\ncheck_interval_max_seconds = 100\n"
    with pytest.raises(ConfigError, match="check_interval_min_seconds"):
        load_config(write(tmp_path, bad))


def test_unknown_monitor_key(tmp_path):
    bad = VALID + "\n[monitor]\nbogus_key = 1\n"
    with pytest.raises(ConfigError, match="unknown or invalid"):
        load_config(write(tmp_path, bad))


VALID_CREDS = """
username = "me@example.com"
password = "hunter2"
[[security_questions]]
question = "你母亲的姓名"
answer = "mom"
[[security_questions]]
question = "第一只宠物的名字"
answer = "cat"
[[security_questions]]
question = "出生的城市"
answer = "wuhan"
"""


def test_login_creds_valid(tmp_path):
    p = tmp_path / "credentials.toml"
    p.write_text(VALID_CREDS, encoding="utf-8")
    creds = load_login_creds(p)
    assert creds.username == "me@example.com"
    assert len(creds.security_questions) == 3
    assert creds.security_questions[0] == ("你母亲的姓名", "mom")


def test_login_creds_missing_file(tmp_path):
    assert load_login_creds(tmp_path / "nope.toml") is None


def test_login_creds_placeholder_rejected(tmp_path):
    p = tmp_path / "credentials.toml"
    p.write_text('username = "your-login-email"\npassword = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="username"):
        load_login_creds(p)


def test_login_creds_too_few_questions(tmp_path):
    p = tmp_path / "credentials.toml"
    p.write_text(
        'username = "a@b.c"\npassword = "x"\n'
        '[[security_questions]]\nquestion = "q1"\nanswer = "a1"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="at least 2"):
        load_login_creds(p)


def test_email_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_SENDER", "a@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "secret")
    monkeypatch.setenv("GMAIL_RECIPIENT", "b@gmail.com, c@gmail.com")
    creds = load_email_creds()
    assert creds.recipients == ("b@gmail.com", "c@gmail.com")


def test_email_creds_missing(tmp_path, monkeypatch):
    for var in ("GMAIL_SENDER", "GMAIL_APP_PASSWORD", "GMAIL_RECIPIENT"):
        monkeypatch.delenv(var, raising=False)
    # Point at a nonexistent .env so a real project .env can't leak in.
    with pytest.raises(ConfigError, match="GMAIL_SENDER"):
        load_email_creds(env_path=tmp_path / "nonexistent.env")
