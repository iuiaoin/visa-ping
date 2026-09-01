"""Auto-booking: click the earliest in-range date, earliest time, submit.

Safety rules:
- dry_run performs every step EXCEPT the final submit click.
- Once the real submit has been clicked, the caller must NEVER retry the
  booking automatically, even if verification is inconclusive — the result
  carries `submitted=True` and the monitor transitions to DONE.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date

from .browser import BrowserSession, eval_js
from .config import BookingCfg
from .scraper import ScrapeError, click_next_month

log = logging.getLogger("visa_ping.booking")


@dataclass
class BookingResult:
    attempted: bool = False
    submitted: bool = False          # False in dry-run or pre-submit failure
    verified: bool | None = None     # None if dry-run / not applicable
    booked_date: date | None = None
    booked_time: str | None = None
    error: str | None = None
    screenshot: bytes | None = None


def _js_click_day(target: date) -> str:
    # data-month is 0-indexed; match year+month+day together so the same day
    # number in an adjacent-month cell can't be clicked by mistake.
    xpath = (
        f"//td[@data-year='{target.year}' and @data-month='{target.month - 1}']"
        f"//a[normalize-space(text())='{target.day}']"
    )
    return (
        "(function() {"
        f"  const r = document.evaluate({json.dumps(xpath)}, document, null,"
        "     XPathResult.FIRST_ORDERED_NODE_TYPE, null);"
        "  if (r.singleNodeValue) { r.singleNodeValue.click(); return true; }"
        "  return false;"
        "})()"
    )


_JS_FIRST_TIME_RADIO = """
(function() {
  const radio = document.querySelector("#time_select input[name='schedule-entries']");
  if (!radio) return null;
  const label = radio.parentElement ? (radio.parentElement.textContent || '').trim() : '';
  radio.click();
  return label;
})()
"""

_JS_SUBMIT_PRESENT = "!!document.querySelector('#submitbtn')"
_JS_CLICK_SUBMIT = """
(function() {
  const btn = document.querySelector('#submitbtn');
  if (btn) { btn.click(); return true; }
  return false;
})()
"""

_JS_POST_SUBMIT_GONE = """
(function() {
  const noSubmit = !document.querySelector('#submitbtn');
  const noCalendar = !document.querySelector('.ui-datepicker-calendar');
  return noSubmit || noCalendar;
})()
"""


async def _wait_js_truthy(page, js: str, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            if await eval_js(page, js):
                return True
        except Exception:
            pass
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(1)


async def book_earliest(
    session: BrowserSession,
    in_range_dates: set[date],
    cfg: BookingCfg,
    max_month_hops: int = 12,
) -> BookingResult:
    """Book the earliest in-range date at its earliest time slot.

    Assumes the schedule page is READY and the consulate is already selected
    (the monitor calls this right after a successful scrape).
    """
    page = session.page
    result = BookingResult(attempted=True)
    target = min(in_range_dates)
    result.booked_date = target
    log.info("Booking attempt: %s (dry_run=%s)", target, cfg.dry_run)

    try:
        # Step 1: bring the target month into view and click the day cell.
        clicked = False
        for _ in range(max_month_hops):
            if await eval_js(page, _js_click_day(target)):
                clicked = True
                break
            if not await click_next_month(page):
                break
        if not clicked:
            raise ScrapeError(f"Day cell for {target} not found in the calendar")

        # Step 2: wait for time slots, pick the earliest (first radio).
        deadline = asyncio.get_running_loop().time() + 30
        label = None
        while label is None:
            label = await eval_js(page, _JS_FIRST_TIME_RADIO)
            if label is not None:
                break
            if asyncio.get_running_loop().time() > deadline:
                raise ScrapeError("No time slots appeared after clicking the date")
            await asyncio.sleep(1)
        result.booked_time = label or None
        log.info("Selected time slot: %s", label)

        # Step 3: wait for the submit button.
        if not await _wait_js_truthy(page, _JS_SUBMIT_PRESENT, timeout=20):
            raise ScrapeError("Submit button did not appear")

        # Step 4: submit (or stop here in dry-run).
        if cfg.dry_run:
            log.info("DRY RUN: skipping the final submit click.")
            result.screenshot = await session.screenshot("dryrun")
            return result

        await eval_js(page, _JS_CLICK_SUBMIT)
        result.submitted = True
        log.info("Submit clicked — verifying...")

        # Post-submit verification (best-effort): the schedule UI should go
        # away (navigation or DOM change). Screenshot regardless so a human
        # can judge from the email.
        verified = False
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            try:
                url = await eval_js(page, "location.href") or ""
                if "/schedule" not in url:
                    verified = True
                    break
                if await eval_js(page, _JS_POST_SUBMIT_GONE):
                    verified = True
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
        result.verified = verified
        result.screenshot = await session.screenshot("post-submit")
        log.info("Booking verification: %s", "confirmed" if verified else "INCONCLUSIVE")
        return result

    except Exception as e:
        if not result.submitted:
            # Pre-submit failure: safe for the monitor to keep monitoring.
            log.error("Booking failed before submit: %s", e)
            result.error = str(e)
            result.screenshot = await session.screenshot("booking-failed")
            return result
        # Post-submit failure: report but never let the caller retry.
        log.error("Error after submit click: %s", e)
        result.error = f"after submit: {e}"
        result.screenshot = await session.screenshot("post-submit-error")
        return result
