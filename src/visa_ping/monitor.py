"""The monitor state machine: MONITORING / WAITING_ROOM / SESSION_LOST /
BOOKING / DONE, with JSON state persistence and randomized pacing."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from .booking import book_earliest
from .browser import BrowserSession, LoginStrategy, PageState
from .config import Config
from .notify import Notifier
from .scraper import diff_dates, filter_in_range, scrape_available_dates, select_consulate

log = logging.getLogger("visa_ping.monitor")

# When Cloudflare blocks us, hammering refresh only prolongs the block —
# back off much longer than for ordinary errors.
BLOCKED_BACKOFF_SECONDS = 600


# --- Persistent state -------------------------------------------------------

@dataclass
class MonitorState:
    known_in_range_dates: list[str] = field(default_factory=list)  # ISO strings
    session_alert_sent: bool = False
    last_heartbeat_iso: str | None = None

    @property
    def known_dates(self) -> set[date]:
        return {date.fromisoformat(s) for s in self.known_in_range_dates}

    def set_known_dates(self, dates: set[date]) -> None:
        self.known_in_range_dates = sorted(d.isoformat() for d in dates)


def load_state(path: Path) -> MonitorState:
    """Missing or corrupt state file yields a fresh state (with a warning)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return MonitorState(
            known_in_range_dates=list(raw.get("known_in_range_dates", [])),
            session_alert_sent=bool(raw.get("session_alert_sent", False)),
            last_heartbeat_iso=raw.get("last_heartbeat_iso"),
        )
    except FileNotFoundError:
        return MonitorState()
    except Exception as e:
        log.warning("State file %s unreadable (%s); starting fresh", path, e)
        return MonitorState()


def save_state(path: Path, state: MonitorState) -> None:
    """Atomic write: temp file in the same directory + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(state), f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- Randomized pacing ------------------------------------------------------

class Pacer:
    """Two-tier randomized intervals (anti-bot: no detectable period).

    Normal checks wait uniform(min, max); after a random number of checks a
    longer randomized rest is taken, and the threshold is re-randomized.
    """

    def __init__(self, cfg, rng: random.Random | None = None):
        self._cfg = cfg
        self._rng = rng or random.Random()
        self._checks_since_rest = 0
        self._threshold = self._rng.randint(
            cfg.checks_before_rest_min, cfg.checks_before_rest_max
        )

    def next_wait_seconds(self) -> float:
        self._checks_since_rest += 1
        if self._checks_since_rest >= self._threshold:
            self._checks_since_rest = 0
            self._threshold = self._rng.randint(
                self._cfg.checks_before_rest_min, self._cfg.checks_before_rest_max
            )
            wait = self._rng.uniform(self._cfg.rest_min_seconds, self._cfg.rest_max_seconds)
            log.info("Taking a longer rest: %.0f s", wait)
        else:
            wait = self._rng.uniform(
                self._cfg.check_interval_min_seconds, self._cfg.check_interval_max_seconds
            )
        return wait


# --- The monitor ------------------------------------------------------------

class Monitor:
    def __init__(
        self,
        session: BrowserSession,
        notifier: Notifier,
        login_strategy: LoginStrategy,
        cfg: Config,
    ):
        self._session = session
        self._notifier = notifier
        self._login = login_strategy
        self._cfg = cfg
        self._state = load_state(cfg.paths.state_file)
        self._pacer = Pacer(cfg.monitor)
        self._started_at = datetime.now()
        self._cycles = 0
        self._blocked_alert_sent = False  # once per blocked episode, in-memory

    def _save(self) -> None:
        save_state(self._cfg.paths.state_file, self._state)

    async def _handle_session_lost(self) -> None:
        if not self._state.session_alert_sent:
            self._notifier.session_lost()
            self._state.session_alert_sent = True
            self._save()
        await self._login.recover(self._session)
        self._notifier.session_recovered()
        self._state.session_alert_sent = False
        self._save()

    async def wait_until_ready(self, startup: bool) -> None:
        """Bring the page to READY, handling login and waiting room."""
        while True:
            state = await self._session.detect_state()
            if state is PageState.READY:
                self._blocked_alert_sent = False  # blocked episode is over
                return
            if state is PageState.BLOCKED:
                log.warning(
                    "Cloudflare block page detected; retrying in %d s "
                    "(if on a VPN, try switching the exit node)",
                    BLOCKED_BACKOFF_SECONDS,
                )
                if not self._blocked_alert_sent:
                    self._notifier.blocked()
                    self._blocked_alert_sent = True
                await asyncio.sleep(BLOCKED_BACKOFF_SECONDS)
                # Re-enter through the home page (deep links are what the
                # WAF blocks); hop to the schedule page only if logged in.
                await self._session.goto_home()
                if await self._session.is_logged_in():
                    await self._session.goto_schedule()
                continue
            if state is PageState.WAITING_ROOM:
                log.info(
                    "In the site's waiting room; re-checking in %.0f s",
                    self._cfg.monitor.waiting_room_poll_seconds,
                )
                await asyncio.sleep(self._cfg.monitor.waiting_room_poll_seconds)
                continue
            if state is PageState.LOGIN_REQUIRED:
                if startup:
                    # Expected on first run: instructions on console, no email.
                    self._log_login_instructions()
                    await self._login.recover(self._session)
                else:
                    await self._handle_session_lost()
                return
            # UNKNOWN: reload and retry after a backoff.
            log.warning("Page state UNKNOWN; reloading after backoff")
            await asyncio.sleep(self._cfg.monitor.error_backoff_seconds)
            await self._session.refresh()

    @staticmethod
    def _log_login_instructions() -> None:
        log.info(
            "LOGIN REQUIRED: please log in manually in the Chrome window "
            "(captcha + security questions). Monitoring starts automatically "
            "afterwards."
        )

    async def startup(self) -> None:
        """Enter the site the way a human does: home page first, then the
        schedule page only once a logged-in session exists (a session-less
        deep link to /schedule/ gets blocked by the Cloudflare WAF)."""
        await self._session.goto_home()
        if await self._session.is_logged_in():
            log.info("Existing session detected on the home page.")
            await self._session.goto_schedule()
        else:
            self._log_login_instructions()
            await self._login.recover(self._session)
        await self.wait_until_ready(startup=True)

    def _heartbeat_due(self) -> bool:
        if not self._cfg.monitor.heartbeat_enabled:
            return False
        if self._state.last_heartbeat_iso is None:
            return True
        last = datetime.fromisoformat(self._state.last_heartbeat_iso)
        elapsed_h = (datetime.now() - last).total_seconds() / 3600
        return elapsed_h >= self._cfg.monitor.heartbeat_interval_hours

    async def run_cycle(self) -> bool:
        """One monitoring cycle. Returns True when the monitor should stop."""
        await self._session.refresh()
        await self.wait_until_ready(startup=False)

        _, label = await select_consulate(self._session.page, self._cfg.consulate)
        all_dates = await scrape_available_dates(
            self._session.page, self._cfg.monitor.months_to_scan
        )
        in_range = filter_in_range(all_dates, self._cfg.dates.earliest, self._cfg.dates.latest)
        added, removed = diff_dates(self._state.known_dates, in_range)
        log.info(
            "Cycle result: %d visible, %d in range (+%d / -%d)",
            len(all_dates), len(in_range), len(added), len(removed),
        )

        if added or removed:
            self._notifier.slots_changed(added, removed, in_range, all_dates)
            self._state.set_known_dates(in_range)
            self._save()

        if self._heartbeat_due():
            self._notifier.heartbeat(self._started_at, self._cycles, in_range)
            self._state.last_heartbeat_iso = datetime.now().isoformat()
            self._save()

        if self._cfg.booking.enabled and in_range:
            result = await book_earliest(self._session, in_range, self._cfg.booking)
            self._notifier.booking_result(result, self._cfg.booking.dry_run)
            if result.submitted:
                # Real submit clicked: NEVER retry, regardless of verification.
                log.info("Booking submitted — monitor is done.")
                return True
            if self._cfg.booking.dry_run and result.error is None:
                log.info("Dry-run booking completed — monitor is done.")
                return True
            log.warning("Booking failed before submit; resuming monitoring.")

        return False

    async def run(self) -> None:
        log.info(
            "Starting monitor: consulate=%s range=%s..%s booking=%s dry_run=%s",
            self._cfg.consulate.guid or self._cfg.consulate.name,
            self._cfg.dates.earliest, self._cfg.dates.latest,
            self._cfg.booking.enabled, self._cfg.booking.dry_run,
        )
        await self.startup()

        while True:
            try:
                done = await self.run_cycle()
                self._cycles += 1
                if done:
                    break
            except Exception:
                log.exception("Cycle failed; backing off %.0f s",
                              self._cfg.monitor.error_backoff_seconds)
                await asyncio.sleep(self._cfg.monitor.error_backoff_seconds)
                continue
            wait = self._pacer.next_wait_seconds()
            log.info("Next check in %.0f s (cycle %d done)", wait, self._cycles)
            await asyncio.sleep(wait)

        self._save()
        log.info(
            "Monitor finished after %d cycle(s). The browser stays open so "
            "you can verify the appointment; press Ctrl+C to exit.", self._cycles
        )
        while True:
            await asyncio.sleep(3600)
