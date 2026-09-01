import email
from datetime import date

from visa_ping.booking import BookingResult
from visa_ping.config import DateRangeCfg, EmailCreds
from visa_ping.notify import Notifier, render_slots_body


RANGE = DateRangeCfg(earliest=date(2026, 9, 15), latest=date(2026, 12, 31))
CREDS = EmailCreds(sender="a@gmail.com", app_password="pw", recipients=("b@gmail.com",))


class FakeSMTP:
    """Records the last sent message; injectable in place of smtplib.SMTP_SSL."""

    sent: list[tuple[str, list[str], str]] = []
    fail = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, user, password):
        if FakeSMTP.fail:
            raise RuntimeError("auth failed")

    def sendmail(self, sender, recipients, message):
        FakeSMTP.sent.append((sender, recipients, message))


def make_notifier() -> Notifier:
    FakeSMTP.sent = []
    FakeSMTP.fail = False
    return Notifier(CREDS, "SHANGHAI", RANGE, smtp_factory=FakeSMTP)


def last_message() -> tuple[str, str]:
    """Return (subject, decoded plain-text body) of the last sent email."""
    _, _, raw = FakeSMTP.sent[-1]
    msg = email.message_from_string(raw)
    part = next(p for p in msg.walk() if p.get_content_type() == "text/plain")
    return msg["Subject"], part.get_payload(decode=True).decode("utf-8")


def test_render_slots_body_contains_sections():
    body = render_slots_body(
        added={date(2026, 10, 3)},
        removed={date(2026, 11, 1)},
        in_range={date(2026, 10, 3)},
        all_dates={date(2026, 10, 3), date(2027, 2, 1)},
        date_range=RANGE,
    )
    assert "+ 2026-10-03" in body
    assert "- 2026-11-01" in body
    assert "All in-range dates now:" in body
    assert "2027-02-01" in body
    assert "2026-09-15 .. 2026-12-31" in body


def test_send_success_and_subject_prefix():
    n = make_notifier()
    assert n.slots_changed({date(2026, 10, 3)}, set(), {date(2026, 10, 3)}, {date(2026, 10, 3)})
    sender, recipients, _ = FakeSMTP.sent[0]
    assert sender == "a@gmail.com"
    assert recipients == ["b@gmail.com"]
    subject, body = last_message()
    assert subject.startswith("[visa-ping] SHANGHAI:")
    assert "+ 2026-10-03" in body


def test_send_failure_returns_false():
    n = make_notifier()
    FakeSMTP.fail = True
    assert n.session_lost() is False  # never raises


def test_booking_result_email_real_unverified():
    n = make_notifier()
    result = BookingResult(
        attempted=True, submitted=True, verified=False,
        booked_date=date(2026, 10, 3), booked_time="09:15",
    )
    assert n.booking_result(result, dry_run=False)
    subject, body = last_message()
    assert "BOOKED 2026-10-03" in subject
    assert "CHECK THE OFFICIAL SITE NOW" in body


def test_booking_result_email_dry_run():
    n = make_notifier()
    result = BookingResult(attempted=True, booked_date=date(2026, 10, 3), booked_time="09:15")
    assert n.booking_result(result, dry_run=True)
    subject, body = last_message()
    assert "DRY-RUN" in subject
    assert "DRY RUN (no submit)" in body
