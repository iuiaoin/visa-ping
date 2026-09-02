"""Gmail SMTP notifications.

Notifier.send() never raises: an email failure must not kill the monitor
loop. Body rendering is split into pure functions for unit testing.
"""

from __future__ import annotations

import logging
import smtplib
from datetime import date, datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from .config import DateRangeCfg, EmailCreds

log = logging.getLogger("visa_ping.notify")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def _footer(date_range: DateRangeCfg) -> str:
    return (
        f"\n--\nvisa-ping | configured range: {date_range.earliest} .. {date_range.latest}"
        f" | sent at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )


def _fmt_dates(dates: set[date], prefix: str = "  ") -> str:
    if not dates:
        return f"{prefix}(none)\n"
    return "".join(f"{prefix}{d.isoformat()}\n" for d in sorted(dates))


def render_slots_body(
    added: set[date],
    removed: set[date],
    in_range: set[date],
    all_dates: set[date],
    date_range: DateRangeCfg,
) -> str:
    parts = []
    if added:
        parts.append("Newly available (in range):\n" + _fmt_dates(added, "  + "))
    if removed:
        parts.append("No longer available (in range):\n" + _fmt_dates(removed, "  - "))
    parts.append("All in-range dates now:\n" + _fmt_dates(in_range))
    parts.append("All visible dates (any month scanned):\n" + _fmt_dates(all_dates))
    return "\n".join(parts) + _footer(date_range)


def render_session_lost_body(date_range: DateRangeCfg) -> str:
    return (
        "The usvisascheduling.com session has expired and monitoring is PAUSED.\n\n"
        "What to do:\n"
        "  1. Go to the machine running visa-ping.\n"
        "  2. The Chrome window is still open on the login page.\n"
        "  3. Log in again (username/password, captcha, security questions).\n\n"
        "Monitoring resumes automatically once the schedule page is detected.\n"
        "This alert is sent only once per outage."
    ) + _footer(date_range)


def render_session_recovered_body(date_range: DateRangeCfg) -> str:
    return "Session restored — monitoring has resumed." + _footer(date_range)


def render_heartbeat_body(
    started_at: datetime,
    cycles: int,
    in_range: set[date],
    date_range: DateRangeCfg,
) -> str:
    uptime = datetime.now() - started_at
    hours = uptime.total_seconds() / 3600
    return (
        f"visa-ping is alive.\n\n"
        f"Uptime: {hours:.1f} h (started {started_at.strftime('%Y-%m-%d %H:%M')})\n"
        f"Cycles completed: {cycles}\n"
        f"Current in-range dates:\n" + _fmt_dates(in_range)
    ) + _footer(date_range)


class Notifier:
    """Sends plain-text (optionally with a PNG attachment) email via Gmail."""

    def __init__(
        self,
        creds: EmailCreds,
        consulate_label: str,
        date_range: DateRangeCfg,
        smtp_factory=smtplib.SMTP_SSL,
    ):
        self._creds = creds
        self._label = consulate_label
        self._range = date_range
        self._smtp_factory = smtp_factory

    def send(self, subject: str, body: str, png_attachment: bytes | None = None) -> bool:
        """Send an email; returns False (and logs) on any failure."""
        try:
            full_subject = f"[visa-ping] {self._label}: {subject}"
            if png_attachment:
                msg = MIMEMultipart()
                msg.attach(MIMEText(body, "plain", "utf-8"))
                img = MIMEImage(png_attachment, _subtype="png")
                img.add_header("Content-Disposition", "attachment", filename="screenshot.png")
                msg.attach(img)
            else:
                msg = MIMEText(body, "plain", "utf-8")
            msg["From"] = formataddr(("visa-ping", self._creds.sender))
            msg["To"] = ", ".join(self._creds.recipients)
            msg["Subject"] = full_subject

            with self._smtp_factory(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.login(self._creds.sender, self._creds.app_password)
                server.sendmail(self._creds.sender, list(self._creds.recipients), msg.as_string())
            log.info("Email sent: %s", full_subject)
            return True
        except Exception as e:
            log.error("Failed to send email %r: %s", subject, e)
            return False

    # --- Typed helpers -----------------------------------------------------

    def slots_changed(
        self,
        added: set[date],
        removed: set[date],
        in_range: set[date],
        all_dates: set[date],
    ) -> bool:
        if added:
            subject = f"{len(in_range)} in-range date(s) available!"
        else:
            subject = "slot changes (removals only)"
        body = render_slots_body(added, removed, in_range, all_dates, self._range)
        return self.send(subject, body)

    def blocked(self) -> bool:
        body = (
            "usvisascheduling.com is showing a Cloudflare block page "
            "('Sorry, you have been blocked' or 'Error 1015: rate limited').\n"
            "Monitoring is paused and will retry with a long backoff.\n\n"
            "Things that help:\n"
            "  - Rate limit (1015): just wait — retrying too fast extends "
            "the block. The monitor already backs off automatically.\n"
            "  - IP block: if on a VPN/proxy, switch to a different exit node "
            "(or try without the VPN — the site serves China directly).\n\n"
            "This alert is sent only once per blocked episode."
        ) + _footer(self._range)
        return self.send("blocked by Cloudflare", body)

    def session_lost(self) -> bool:
        return self.send("ACTION REQUIRED: session expired", render_session_lost_body(self._range))

    def session_recovered(self) -> bool:
        return self.send("session recovered", render_session_recovered_body(self._range))

    def booking_result(self, result, dry_run: bool) -> bool:
        # `result` is a booking.BookingResult; imported lazily to avoid a cycle.
        if result.error and not result.submitted:
            subject = "BOOKING FAILED"
        elif dry_run:
            subject = f"DRY-RUN booking simulated {result.booked_date} {result.booked_time or ''}"
        else:
            subject = f"BOOKED {result.booked_date} {result.booked_time or ''}"

        lines = [
            f"Mode: {'DRY RUN (no submit)' if dry_run else 'REAL booking'}",
            f"Date: {result.booked_date}",
            f"Time: {result.booked_time or 'unknown'}",
        ]
        if result.error:
            lines.append(f"Error: {result.error}")
        if result.submitted:
            if result.verified:
                lines.append("Verification: page confirmed the submission.")
            else:
                lines.append(
                    "Verification: COULD NOT CONFIRM — CHECK THE OFFICIAL SITE NOW.\n"
                    "The submit button was clicked but no confirmation was detected."
                )
        body = "\n".join(lines) + _footer(self._range)
        return self.send(subject, body, png_attachment=result.screenshot)

    def heartbeat(self, started_at: datetime, cycles: int, in_range: set[date]) -> bool:
        return self.send(
            "heartbeat", render_heartbeat_body(started_at, cycles, in_range, self._range)
        )
