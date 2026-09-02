"""Configuration loading and validation.

Secrets come from .env (python-dotenv); behavior comes from config.toml
(stdlib tomllib, hence Python >= 3.11). All validation errors are collected
and raised together so the user can fix everything in one pass.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class EmailCreds:
    sender: str
    app_password: str
    recipients: tuple[str, ...]


@dataclass(frozen=True)
class ConsulateCfg:
    name: str | None
    guid: str | None


@dataclass(frozen=True)
class DateBound:
    """A range bound: either a fixed date or an offset from 'today'.

    Relative bounds are resolved at USE time, not load time — a monitor
    running for days must keep honoring "at least N days from now".
    """

    fixed: date | None = None
    offset_days: int | None = None

    def resolve(self, today: date | None = None) -> date:
        if self.fixed is not None:
            return self.fixed
        return (today or date.today()) + timedelta(days=self.offset_days)

    def describe(self, today: date | None = None) -> str:
        if self.fixed is not None:
            return self.fixed.isoformat()
        return f"today+{self.offset_days} ({self.resolve(today).isoformat()})"


@dataclass(frozen=True)
class DateRangeCfg:
    earliest: DateBound
    latest: DateBound

    @classmethod
    def from_dates(cls, earliest: date, latest: date) -> "DateRangeCfg":
        return cls(DateBound(fixed=earliest), DateBound(fixed=latest))

    def resolve(self, today: date | None = None) -> tuple[date, date]:
        return self.earliest.resolve(today), self.latest.resolve(today)

    def describe(self, today: date | None = None) -> str:
        return f"{self.earliest.describe(today)} .. {self.latest.describe(today)}"


@dataclass(frozen=True)
class MonitorCfg:
    months_to_scan: int = 3
    check_interval_min_seconds: float = 180
    check_interval_max_seconds: float = 300
    rest_min_seconds: float = 420
    rest_max_seconds: float = 540
    checks_before_rest_min: int = 4
    checks_before_rest_max: int = 7
    error_backoff_seconds: float = 60
    session_poll_seconds: float = 30
    waiting_room_poll_seconds: float = 20
    # Minimum spacing between our page loads; the site rate-limits over a
    # ~30 s window (Cloudflare error 1015), so stay above that.
    nav_min_interval_seconds: float = 35
    heartbeat_enabled: bool = False
    heartbeat_interval_hours: float = 6


@dataclass(frozen=True)
class BookingCfg:
    enabled: bool = False
    dry_run: bool = True


@dataclass(frozen=True)
class PathsCfg:
    profile_dir: Path
    state_file: Path
    log_file: Path
    screenshots_dir: Path


@dataclass(frozen=True)
class Config:
    consulate: ConsulateCfg
    dates: DateRangeCfg
    monitor: MonitorCfg
    booking: BookingCfg
    paths: PathsCfg
    # "reschedule": change an existing appointment (default);
    # "schedule": book a brand-new appointment.
    scenario: str = "reschedule"


def load_email_creds(env_path: Path | None = None) -> EmailCreds:
    """Load Gmail credentials from .env / environment variables."""
    if env_path is not None:
        load_dotenv(env_path)
    else:
        load_dotenv()

    errors = []
    sender = os.environ.get("GMAIL_SENDER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    recipient_raw = os.environ.get("GMAIL_RECIPIENT", "").strip()
    if not sender:
        errors.append("GMAIL_SENDER is not set (put it in .env)")
    if not password:
        errors.append("GMAIL_APP_PASSWORD is not set (put it in .env)")
    recipients = tuple(r.strip() for r in recipient_raw.split(",") if r.strip())
    if not recipients:
        errors.append("GMAIL_RECIPIENT is not set (put it in .env)")
    if errors:
        raise ConfigError("Invalid email configuration:\n- " + "\n- ".join(errors))
    return EmailCreds(sender=sender, app_password=password, recipients=recipients)


# "today" or "today+N" (case-insensitive, spaces allowed around '+')
_RELATIVE_DATE_RE = re.compile(r"^\s*today\s*(?:\+\s*(\d+)\s*)?$", re.IGNORECASE)


def _as_bound(value: object, key: str, errors: list[str]) -> DateBound:
    if isinstance(value, date):
        return DateBound(fixed=value)
    if isinstance(value, str):
        m = _RELATIVE_DATE_RE.match(value)
        if m:
            return DateBound(offset_days=int(m.group(1) or 0))
        try:
            return DateBound(fixed=date.fromisoformat(value.strip()))
        except ValueError:
            pass
    errors.append(
        f"[dates] {key} must be a date (YYYY-MM-DD) or relative "
        f"(\"today\", \"today+N\"), got: {value!r}"
    )
    return DateBound(fixed=date.today())  # placeholder; errors will abort anyway


def load_config(config_path: Path) -> Config:
    """Load and validate config.toml. Raises ConfigError with all problems."""
    if not config_path.is_file():
        raise ConfigError(
            f"Config file not found: {config_path}\n"
            f"Copy config.example.toml to {config_path.name} and edit it."
        )
    with open(config_path, "rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Failed to parse {config_path}: {e}") from e

    errors: list[str] = []
    base_dir = config_path.resolve().parent

    scenario = raw.get("scenario", "reschedule")
    if scenario not in ("schedule", "reschedule"):
        errors.append(f"scenario must be 'schedule' or 'reschedule', got: {scenario!r}")

    consulate_raw = raw.get("consulate", {})
    consulate = ConsulateCfg(
        name=consulate_raw.get("name"),
        guid=consulate_raw.get("guid"),
    )
    if not consulate.name and not consulate.guid:
        errors.append("[consulate] requires `name` or `guid`")

    dates_raw = raw.get("dates", {})
    if "earliest" not in dates_raw or "latest" not in dates_raw:
        errors.append("[dates] requires both `earliest` and `latest`")
        dates = DateRangeCfg.from_dates(date.today(), date.today())
    else:
        earliest = _as_bound(dates_raw["earliest"], "earliest", errors)
        latest = _as_bound(dates_raw["latest"], "latest", errors)
        dates = DateRangeCfg(earliest=earliest, latest=latest)
        # Sanity-check the range as resolved today; relative bounds shift
        # together over time so this ordering stays representative.
        earliest_r, latest_r = dates.resolve()
        if earliest_r > latest_r:
            errors.append(
                f"[dates] earliest ({dates.earliest.describe()}) is after "
                f"latest ({dates.latest.describe()})"
            )
        if latest.fixed is not None and latest.fixed < date.today():
            errors.append(f"[dates] latest ({latest.fixed}) is in the past")

    monitor_raw = raw.get("monitor", {})
    try:
        monitor = MonitorCfg(**monitor_raw)
    except TypeError as e:
        errors.append(f"[monitor] unknown or invalid key: {e}")
        monitor = MonitorCfg()
    if monitor.months_to_scan < 1:
        errors.append("[monitor] months_to_scan must be >= 1")
    for lo_key, hi_key in [
        ("check_interval_min_seconds", "check_interval_max_seconds"),
        ("rest_min_seconds", "rest_max_seconds"),
        ("checks_before_rest_min", "checks_before_rest_max"),
    ]:
        lo, hi = getattr(monitor, lo_key), getattr(monitor, hi_key)
        if lo > hi:
            errors.append(f"[monitor] {lo_key} ({lo}) > {hi_key} ({hi})")
        if lo <= 0:
            errors.append(f"[monitor] {lo_key} must be > 0")
    if monitor.nav_min_interval_seconds <= 0:
        errors.append("[monitor] nav_min_interval_seconds must be > 0")

    booking_raw = raw.get("booking", {})
    try:
        booking = BookingCfg(**booking_raw)
    except TypeError as e:
        errors.append(f"[booking] unknown or invalid key: {e}")
        booking = BookingCfg()

    paths_raw = raw.get("paths", {})

    def _path(key: str, default: str) -> Path:
        p = Path(paths_raw.get(key, default))
        return p if p.is_absolute() else base_dir / p

    paths = PathsCfg(
        profile_dir=_path("profile_dir", "chrome_profile"),
        state_file=_path("state_file", "state.json"),
        log_file=_path("log_file", "logs/visa-ping.log"),
        screenshots_dir=_path("screenshots_dir", "screenshots"),
    )

    if errors:
        raise ConfigError(f"Invalid configuration in {config_path}:\n- " + "\n- ".join(errors))

    return Config(
        consulate=consulate, dates=dates, monitor=monitor, booking=booking,
        paths=paths, scenario=scenario,
    )


def setup_logging(log_file: Path) -> logging.Logger:
    """Console + rotating file logging for the whole app."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("visa_ping")
    logger.setLevel(logging.INFO)
    if logger.handlers:  # avoid duplicate handlers on repeated setup
        return logger
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger
