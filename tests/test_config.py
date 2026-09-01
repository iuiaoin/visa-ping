from datetime import date, timedelta
from pathlib import Path

import pytest

from visa_ping.config import ConfigError, load_config, load_email_creds

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
    # relative paths resolve against the config file's directory
    assert cfg.paths.state_file == tmp_path / "state.json"


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


def test_bad_interval_ordering(tmp_path):
    bad = VALID + "\n[monitor]\ncheck_interval_min_seconds = 300\ncheck_interval_max_seconds = 100\n"
    with pytest.raises(ConfigError, match="check_interval_min_seconds"):
        load_config(write(tmp_path, bad))


def test_unknown_monitor_key(tmp_path):
    bad = VALID + "\n[monitor]\nbogus_key = 1\n"
    with pytest.raises(ConfigError, match="unknown or invalid"):
        load_config(write(tmp_path, bad))


def test_email_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_SENDER", "a@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "secret")
    monkeypatch.setenv("GMAIL_RECIPIENT", "b@gmail.com, c@gmail.com")
    creds = load_email_creds()
    assert creds.recipients == ("b@gmail.com", "c@gmail.com")


def test_email_creds_missing(monkeypatch):
    for var in ("GMAIL_SENDER", "GMAIL_APP_PASSWORD", "GMAIL_RECIPIENT"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ConfigError, match="GMAIL_SENDER"):
        load_email_creds()
