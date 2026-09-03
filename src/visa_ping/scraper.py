"""Consulate selection and calendar scraping.

All DOM access goes through small JS snippets returning JSON strings, which
keeps the Python<->browser boundary simple and robust across nodriver
serialization quirks. The jQuery UI datepicker encodes availability as
td[data-handler='selectDay'].greenday cells; data-month is 0-indexed.

Note: the page also exposes a JSON API observable in-page
(/custom-actions/?route=/api/v1/schedule-group/get-family-consular-schedule-days
returning {"ScheduleDays": [...]}) — intercepting it via a fetch hook is a
possible future enhancement; DOM scraping is the proven primary path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import date

from .browser import eval_js
from .config import ConsulateCfg

log = logging.getLogger("visa_ping.scraper")


class ScrapeError(Exception):
    """Raised when the page structure does not match expectations."""


_JS_LIST_OPTIONS = """
JSON.stringify(Array.from(document.querySelectorAll('#post_select option'))
  .map(o => ({value: o.value, text: (o.textContent || '').trim()})))
"""

_JS_COLLECT_GREENDAYS = """
JSON.stringify(Array.from(
  document.querySelectorAll("td[data-handler='selectDay'].greenday")
).map(td => {
  const a = td.querySelector('a.ui-state-default');
  return {
    year: parseInt(td.getAttribute('data-year'), 10),
    month: parseInt(td.getAttribute('data-month'), 10),
    day: a ? parseInt(a.textContent, 10) : null,
  };
}))
"""

# Calendar is considered rendered when the datepicker table exists; the
# zh-CN ready message is an additional (locale-specific) positive signal.
_JS_CALENDAR_READY = """
(function() {
  const msg = document.querySelector('#datepicker-message');
  if (msg && (msg.textContent || '').includes('选择日期')) return true;
  return !!document.querySelector('.ui-datepicker-calendar');
})()
"""

_JS_NEXT_ARROW_STATE = """
(function() {
  const btn = document.querySelector('a.ui-datepicker-next');
  if (!btn) return 'missing';
  return btn.classList.contains('ui-state-disabled') ? 'disabled' : 'enabled';
})()
"""

_JS_CLICK_NEXT = """
(function() {
  const btn = document.querySelector('a.ui-datepicker-next');
  if (btn && !btn.classList.contains('ui-state-disabled')) { btn.click(); return true; }
  return false;
})()
"""


async def _eval_json(page, js: str):
    raw = await eval_js(page, js)
    if raw is None:
        raise ScrapeError(f"JS evaluation returned nothing: {js[:80]}...")
    return json.loads(raw)


async def select_consulate(page, cfg: ConsulateCfg) -> tuple[str, str]:
    """Pick the consulate in #post_select; returns (guid, display_name).

    Options are read dynamically so consulates whose GUIDs we don't know
    (e.g. Guangzhou/Beijing) still work via name matching.
    """
    # The <select> renders before its options arrive via AJAX, so READY can
    # fire during that window — wait for real options instead of failing.
    deadline = asyncio.get_running_loop().time() + 30
    while True:
        options = await _eval_json(page, _JS_LIST_OPTIONS)
        options = [o for o in options if o.get("value")]  # drop placeholders
        if options:
            break
        if asyncio.get_running_loop().time() > deadline:
            raise ScrapeError("#post_select still has no options after 30 s")
        await asyncio.sleep(1)

    if cfg.guid:
        matches = [o for o in options if o["value"].lower() == cfg.guid.lower()]
    else:
        needle = cfg.name.lower()
        matches = [o for o in options if needle in o["text"].lower()]

    listing = "\n".join(f"  {o['value']}  {o['text']}" for o in options)
    if not matches:
        raise ScrapeError(
            f"No consulate option matches {cfg.guid or cfg.name!r}. Available options:\n{listing}"
        )
    if len(matches) > 1:
        raise ScrapeError(
            f"Consulate name {cfg.name!r} is ambiguous "
            f"({len(matches)} matches). Use `guid` in config. Options:\n{listing}"
        )

    guid, label = matches[0]["value"], matches[0]["text"]
    await eval_js(page, 
        "(function() {"
        "  const s = document.querySelector('#post_select');"
        f" s.value = {json.dumps(guid)};"
        "  s.dispatchEvent(new Event('change', {bubbles: true}));"
        "})()"
    )
    await _wait_calendar_ready(page)
    log.info("Consulate selected: %s (%s)", label, guid)
    return guid, label


async def _wait_calendar_ready(page, timeout: float = 45) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            if await eval_js(page, _JS_CALENDAR_READY):
                return
        except Exception:
            pass
        if asyncio.get_running_loop().time() > deadline:
            raise ScrapeError("Calendar did not render within timeout")
        await asyncio.sleep(1)


async def _collect_month(page) -> set[date]:
    cells = await _eval_json(page, _JS_COLLECT_GREENDAYS)
    result: set[date] = set()
    for c in cells:
        year, month, day = c.get("year"), c.get("month"), c.get("day")
        if year is None or month is None or day is None:
            continue
        try:
            # data-month is 0-indexed (jQuery UI convention)
            result.add(date(int(year), int(month) + 1, int(day)))
        except (ValueError, TypeError):
            log.warning("Skipping malformed calendar cell: %r", c)
    return result


async def click_next_month(page) -> bool:
    """Advance the datepicker one month; False if the arrow is disabled/absent."""
    state = await eval_js(page, _JS_NEXT_ARROW_STATE)
    if state != "enabled":
        return False
    await eval_js(page, _JS_CLICK_NEXT)
    # Each hop fires a calendar AJAX call; pace them human-like so a burst
    # of month flips can't contribute to the site's aggressive rate limit.
    await asyncio.sleep(random.uniform(5.0, 10.0))
    await _wait_calendar_ready(page)
    return True


async def scrape_available_dates(page, months_to_scan: int) -> set[date]:
    """Collect green (bookable) days across up to `months_to_scan` months.

    Every monitor cycle starts from a fresh page load, so no back-navigation
    is needed afterwards.
    """
    all_dates: set[date] = set()
    for i in range(months_to_scan):
        month_dates = await _collect_month(page)
        all_dates |= month_dates
        if i < months_to_scan - 1:
            if not await click_next_month(page):
                break
    log.info("Scraped %d available date(s) across <=%d month(s)", len(all_dates), months_to_scan)
    return all_dates


# --- Pure date logic (unit-tested) -----------------------------------------

def filter_in_range(dates: set[date], earliest: date, latest: date) -> set[date]:
    return {d for d in dates if earliest <= d <= latest}


def diff_dates(old: set[date], new: set[date]) -> tuple[set[date], set[date]]:
    """Returns (added, removed) relative to `old`."""
    return new - old, old - new
