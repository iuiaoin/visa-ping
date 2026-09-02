"""Browser session management on top of nodriver.

nodriver drives a real, non-headless Chrome over CDP (no webdriver flag),
with a persistent user profile so the login session survives restarts.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

import nodriver as uc
from nodriver import cdp

log = logging.getLogger("visa_ping.browser")

# Deep-linking to the schedule page WITHOUT a logged-in session trips the
# Cloudflare WAF ("Sorry, you have been blocked"), while the home page loads
# fine — so the flow is: home page -> human logs in -> then navigate to the
# schedule/reschedule page.
HOME_URL = "https://www.usvisascheduling.com/"
SCHEDULE_URL = "https://www.usvisascheduling.com/zh-CN/schedule/"
RESCHEDULE_URL = "https://www.usvisascheduling.com/zh-CN/schedule/?reschedule=true"


def target_url_for(scenario: str) -> str:
    return SCHEDULE_URL if scenario == "schedule" else RESCHEDULE_URL


async def eval_js(page: "uc.Tab", js: str):
    """Evaluate JS returning a plain Python value.

    nodriver's Tab.evaluate(return_by_value=True) leaks the raw RemoteObject
    when the JS result is falsy (null/false/""/0), and returns an
    ExceptionDetails object on JS errors — unwrap both here so callers can
    rely on ordinary Python truthiness.
    """
    result = await page.evaluate(js, return_by_value=True)
    if isinstance(result, cdp.runtime.ExceptionDetails):
        raise RuntimeError(f"JS evaluation error: {result.text} ({js[:80]}...)")
    if isinstance(result, cdp.runtime.RemoteObject):
        return result.value
    return result


class PageState(Enum):
    READY = auto()           # schedule page usable (#post_select present)
    LOGIN_REQUIRED = auto()  # B2C login page or a password field is showing
    WAITING_ROOM = auto()    # queue / waiting-room page holding our spot
    CHALLENGE = auto()       # Cloudflare human-verification (Turnstile checkbox)
    BLOCKED = auto()         # Cloudflare hard block / rate limit (1015)
    UNKNOWN = auto()         # nothing recognizable within the timeout


# Hard Cloudflare block/rate-limit fingerprints. Error 1015 ("You are being
# rate limited") is common here: the site rate-limits page loads over
# roughly a 30-second window. Checked BEFORE the challenge fingerprints —
# block pages can carry Cloudflare scripts too.
_JS_CF_HARD_BLOCK = """
(function() {
  const title = document.title || '';
  if (title.includes('Attention Required!')) return true;
  const body = document.body ? (document.body.innerText || '') : '';
  return body.includes('you have been blocked')
      || body.includes('Error 1015')
      || body.includes('being rate limited');
})()
"""

# Interactive Cloudflare challenge (Turnstile "Verify you are human"
# checkbox on a "Just a moment..." interstitial). Unlike a hard block this
# is recoverable by clicking the checkbox.
_JS_CF_CHALLENGE = """
(function() {
  const title = document.title || '';
  if (title.includes('Just a moment')) return true;
  const srcs = [...document.querySelectorAll('script[src]')].map(s => s.src);
  return srcs.some(s => s.includes('cdn-cgi/challenge-platform')
                     || s.includes('turnstile/v0/api.js')
                     || s.includes('challenges.cloudflare.com'));
})()
"""

# Queue / waiting-room pages: the site's own image-based waiting room, or a
# text-based "You are now in line, estimated time N minutes" page. These
# auto-advance and hold our place — NEVER navigate away from them.
# Returns null when not queued, else a status string for logging.
_JS_WAITING_ROOM = """
(function() {
  const style = document.body ? (document.body.getAttribute('style') || '') : '';
  const body = document.body ? (document.body.innerText || '') : '';
  const title = document.title || '';
  const queued = style.includes('waiting_room_background')
      || /now (?:wait(?:ing)? )?in line/i.test(body)
      || /estimated (?:wait(?:ing)? )?time/i.test(body)
      || title.includes('Waiting Room');
  if (!queued) return null;
  const m = body.match(/estimated[^0-9]{0,40}(\\d+)\\s*min/i);
  return m ? ('~' + m[1] + ' min') : 'unknown wait';
})()
"""


# Heuristic for "logged in": a sign-out/log-off link in the page chrome
# (Power Pages portals render one once authenticated).
_JS_LOGGED_IN = """
!!document.querySelector(
  "a[href*='signout' i], a[href*='sign-out' i], a[href*='logout' i], a[href*='logoff' i]"
)
"""


class BrowserSession:
    """Owns the Chrome instance and the single tab used for everything."""

    def __init__(
        self,
        profile_dir: Path,
        screenshots_dir: Path,
        target_url: str = RESCHEDULE_URL,
        nav_min_interval: float = 35.0,
    ):
        self._profile_dir = profile_dir
        self._screenshots_dir = screenshots_dir
        self._target_url = target_url
        # The site rate-limits page loads (~30 s window -> Cloudflare 1015);
        # never issue two programmatic navigations closer than this.
        self._nav_min_interval = nav_min_interval
        self._last_nav: float | None = None
        self.browser: uc.Browser | None = None
        self.page: uc.Tab | None = None
        self.queue_status: str | None = None  # set when WAITING_ROOM detected

    async def start(self, attempts: int = 3) -> None:
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._screenshots_dir.mkdir(parents=True, exist_ok=True)
        # A cold Chrome launch (Gatekeeper verification, first-run init, an
        # in-flight update) can outlast nodriver's CDP connect window, so
        # retry a few times before giving up.
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            cfg = uc.Config()
            cfg.user_data_dir = str(self._profile_dir)
            cfg.headless = False  # manual login requires a visible window
            try:
                self.browser = await uc.start(cfg)
                log.info("Chrome started (profile: %s)", self._profile_dir)
                return
            except Exception as e:
                last_error = e
                log.warning(
                    "Chrome start attempt %d/%d failed: %s", attempt, attempts,
                    str(e).strip().splitlines()[-1] if str(e).strip() else e,
                )
                if attempt < attempts:
                    await asyncio.sleep(5 * attempt)
        raise RuntimeError(
            f"Could not connect to Chrome after {attempts} attempts. "
            "Check that Google Chrome launches normally by hand, close any "
            "stray Chrome using the same profile dir, then retry."
        ) from last_error

    async def _goto(self, url: str) -> None:
        # Global navigation throttle (see nav_min_interval above).
        now = asyncio.get_running_loop().time()
        if self._last_nav is not None:
            wait = self._last_nav + self._nav_min_interval - now
            if wait > 0:
                log.info("Rate-limit guard: waiting %.0f s before next page load", wait)
                await asyncio.sleep(wait)
        if self.page is None:
            self.page = await self.browser.get(url)
        else:
            await self.page.get(url)
        self._last_nav = asyncio.get_running_loop().time()
        await asyncio.sleep(2)  # let the navigation settle

    async def goto_home(self) -> None:
        """Open the site home page (safe to load without a session)."""
        await self._goto(HOME_URL)

    async def goto_schedule(self) -> None:
        """Navigate to the schedule/reschedule page. Only do this once a
        logged-in session exists — a session-less deep link gets WAF-blocked."""
        await self._goto(self._target_url)

    async def refresh(self) -> None:
        await self.goto_schedule()

    async def is_logged_in(self) -> bool:
        """Best-effort check for an authenticated portal session on the
        CURRENT page (any page of the site, home included)."""
        return bool(await self._eval(_JS_LOGGED_IN))

    async def _eval(self, js: str):
        """Evaluate JS in the page, returning None on any CDP hiccup."""
        try:
            return await eval_js(self.page, js)
        except Exception as e:
            log.debug("evaluate failed (%s): %s", js[:60], e)
            return None

    async def detect_state(self, timeout: float = 30) -> PageState:
        """Classify the current page, waiting up to `timeout` for READY."""
        deadline = asyncio.get_running_loop().time() + timeout
        self.queue_status = None  # populated when WAITING_ROOM is detected
        while True:
            url = await self._eval("location.href") or ""
            if "b2clogin.com" in url:
                return PageState.LOGIN_REQUIRED

            # Don't classify a half-parsed document: head scripts appear
            # before body text, which would misread e.g. a 1015 block page
            # (body not yet parsed) as a challenge (scripts already visible).
            if await self._eval("document.readyState") == "loading":
                if asyncio.get_running_loop().time() > deadline:
                    return PageState.UNKNOWN
                await asyncio.sleep(0.5)
                continue

            if await self._eval(_JS_CF_HARD_BLOCK):
                return PageState.BLOCKED

            if await self._eval(_JS_CF_CHALLENGE):
                return PageState.CHALLENGE

            queue_status = await self._eval(_JS_WAITING_ROOM)
            if queue_status:
                self.queue_status = queue_status
                return PageState.WAITING_ROOM

            if await self._eval("!!document.querySelector('input[type=password]')"):
                return PageState.LOGIN_REQUIRED

            if await self._eval("!!document.querySelector('#post_select')"):
                return PageState.READY

            if asyncio.get_running_loop().time() > deadline:
                log.warning("detect_state timed out; current URL: %s", url)
                return PageState.UNKNOWN
            await asyncio.sleep(1)

    async def try_click_challenge(self, attempt: int = 0) -> bool:
        """Attempt to click the Cloudflare Turnstile checkbox.

        The widget's iframe sits inside a shadow root, invisible to page
        JS — but nodriver's CDP-based select_all pierces shadow DOM. Even
        attempts click the iframe center; odd attempts click where the
        checkbox actually sits (left edge of the widget, vertically
        centered). Returns True if something was clicked.
        """
        try:
            iframes = await self.page.select_all("iframe", timeout=3)
        except Exception:
            iframes = []
        target = None
        for iframe in iframes or []:
            attrs = " ".join(
                str(iframe.attrs.get(k, "")) for k in ("src", "id", "class")
            ).lower()
            if "challenges.cloudflare.com" in attrs or "turnstile" in attrs or "cf-" in attrs:
                target = iframe
                break
        if target is None:
            log.debug("Challenge iframe not found (attempt %d)", attempt)
            return False
        try:
            await self.page.activate()  # CDP mouse events need a focused tab
            await asyncio.sleep(0.2)
            if attempt % 2 == 0:
                await target.mouse_click()
            else:
                pos = await target.get_position()
                # Checkbox sits ~28 px from the widget's left edge.
                await self.page.mouse_click(pos.left + 28, pos.top + pos.height / 2)
            log.info("Clicked the human-verification checkbox (attempt %d)", attempt + 1)
            return True
        except Exception as e:
            log.warning("Challenge click failed: %s", e)
            return False

    async def screenshot(self, tag: str = "page") -> bytes | None:
        """Save a screenshot to the screenshots dir and return its bytes."""
        try:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self._screenshots_dir / f"{ts}-{tag}.png"
            await self.page.save_screenshot(filename=str(path), format="png")
            return path.read_bytes()
        except Exception as e:
            log.error("Screenshot failed: %s", e)
            return None

    def stop(self) -> None:
        if self.browser is not None:
            try:
                self.browser.stop()
            except Exception:
                pass


class LoginStrategy(ABC):
    """Pluggable session-recovery strategy.

    The monitor calls recover() after it has detected session loss (and sent
    its one-time alert). An automated implementation (credential fill + LLM
    captcha OCR against the B2C form) can be added later without touching
    the monitor loop.
    """

    @abstractmethod
    async def recover(self, session: BrowserSession) -> bool:
        """Block until logged in again. Return True once the page is READY."""


class ManualLoginStrategy(LoginStrategy):
    """Wait for a human to complete the login in the open Chrome window."""

    def __init__(self, poll_seconds: float = 30):
        self._poll_seconds = poll_seconds

    async def recover(self, session: BrowserSession) -> bool:
        log.info(
            "Waiting for manual login in the open Chrome window "
            "(checking every %.0f s)...", self._poll_seconds
        )
        while True:
            # No refresh here: reloading could interrupt the human mid-login.
            state = await session.detect_state(timeout=5)
            if state is PageState.READY:
                log.info("Schedule page ready — session recovered.")
                return True
            # The human may have finished logging in while the tab sits on
            # the home (or any other) page — hop to the schedule page then.
            # Never navigate while the B2C login form is showing.
            if state is not PageState.LOGIN_REQUIRED and await session.is_logged_in():
                log.info("Login detected — navigating to the schedule page...")
                await session.goto_schedule()
                continue
            await asyncio.sleep(self._poll_seconds)
