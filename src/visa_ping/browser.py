"""Browser session management on top of nodriver.

nodriver drives a real, non-headless Chrome over CDP (no webdriver flag),
with a persistent user profile so the login session survives restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

import nodriver as uc
from nodriver import cdp

from .config import LoginCreds

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

    async def _find_challenge_iframe_box(self) -> tuple[float, float, float, float] | None:
        """Locate the Turnstile iframe and return its viewport box (x, y, w, h).

        The widget iframe lives inside a (closed) shadow root. CSS selector
        queries — even CDP-side ones like DOM.querySelectorAll — do NOT
        cross shadow boundaries, so walk the pierced CDP DOM tree manually
        (children + shadowRoots + contentDocument).
        """
        try:
            doc = await self.page.send(cdp.dom.get_document(depth=-1, pierce=True))
        except Exception as e:
            log.warning("DOM snapshot failed: %s", e)
            return None

        hit: cdp.dom.Node | None = None

        def walk(node: cdp.dom.Node) -> None:
            nonlocal hit
            if hit is not None:
                return
            if node.node_name == "IFRAME":
                flat = node.attributes or []
                attrs = dict(zip(flat[0::2], flat[1::2]))
                blob = " ".join(str(v) for v in attrs.values()).lower()
                if (
                    "challenges.cloudflare.com" in blob
                    or "turnstile" in blob
                    or str(attrs.get("id", "")).startswith("cf-")
                ):
                    hit = node
                    return
            for child in (node.children or []) + (node.shadow_roots or []):
                walk(child)
            if node.content_document is not None:
                walk(node.content_document)

        walk(doc)
        if hit is None:
            return None
        try:
            box = await self.page.send(cdp.dom.get_box_model(backend_node_id=hit.backend_node_id))
        except Exception as e:
            log.warning("Box model for challenge iframe failed: %s", e)
            return None
        q = box.content  # quad: x1,y1 x2,y2 x3,y3 x4,y4
        x, y = q[0], q[1]
        return x, y, q[2] - q[0], q[5] - q[1]

    async def _cdp_human_click(self, x: float, y: float) -> None:
        """Click via CDP with a human-like approach: a curved mouse-move
        trail, a hover pause, and realistic press/release timing. Turnstile
        scores pointer history, so a bare synthetic click often fails."""
        import random

        sx = max(5.0, x + random.uniform(-350, -150))
        sy = max(5.0, y + random.uniform(90, 220))
        cx = (sx + x) / 2 + random.uniform(-80, 80)   # bezier control point
        cy = (sy + y) / 2 + random.uniform(-50, 50)
        steps = random.randint(16, 28)
        for i in range(steps + 1):
            t = i / steps
            px = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t**2 * x
            py = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t**2 * y
            await self.page.send(cdp.input_.dispatch_mouse_event("mouseMoved", x=px, y=py))
            await asyncio.sleep(random.uniform(0.008, 0.025))
        await asyncio.sleep(random.uniform(0.15, 0.4))
        button = cdp.input_.MouseButton.LEFT
        await self.page.send(cdp.input_.dispatch_mouse_event(
            "mousePressed", x=x, y=y, button=button, buttons=1, click_count=1))
        await asyncio.sleep(random.uniform(0.06, 0.15))
        await self.page.send(cdp.input_.dispatch_mouse_event(
            "mouseReleased", x=x, y=y, button=button, buttons=0, click_count=1))

    async def _os_level_click(self, x: float, y: float) -> bool:
        """Click with the REAL system mouse via pyautogui — OS-level input
        is fully trusted by the browser, giving the best pass rate.

        Needs: pyautogui installed, the Chrome window visible on screen,
        and macOS Accessibility permission for the terminal running us.
        Briefly moves the user's cursor (and restores it afterwards).
        """
        try:
            import pyautogui
        except ImportError:
            log.warning("pyautogui not installed; skipping OS-level click")
            return False

        import json as _json
        import random
        import subprocess

        raw = await eval_js(self.page, """
            JSON.stringify({sx: window.screenX, sy: window.screenY,
                            ow: window.outerWidth, oh: window.outerHeight,
                            iw: window.innerWidth, ih: window.innerHeight})
        """)
        m = _json.loads(raw)
        # Viewport -> screen: side borders are symmetric (~0 on macOS), the
        # remaining outer/inner height delta is the toolbar at the top.
        side = (m["ow"] - m["iw"]) / 2
        screen_x = m["sx"] + side + x
        screen_y = m["sy"] + (m["oh"] - m["ih"]) - side + y

        # Raise our Chrome window: activate the tab, then bring the exact
        # bot Chrome process (by pid) frontmost so the click lands on it.
        await self.page.activate()
        pid = getattr(self.browser, "_process_pid", None)
        if pid:
            script = (
                'tell application "System Events" to set frontmost of '
                f"(first process whose unix id is {pid}) to true"
            )
            try:
                await asyncio.to_thread(
                    subprocess.run, ["osascript", "-e", script],
                    check=False, capture_output=True, timeout=10,
                )
            except Exception as e:
                log.warning("Could not raise Chrome window: %s", e)
        await asyncio.sleep(0.5)

        def do_click() -> None:
            original = pyautogui.position()
            pyautogui.moveTo(
                screen_x + random.uniform(-2, 2), screen_y + random.uniform(-2, 2),
                duration=random.uniform(0.35, 0.7), tween=pyautogui.easeInOutQuad,
            )
            pyautogui.click()
            pyautogui.moveTo(original.x, original.y, duration=0.2)

        try:
            await asyncio.to_thread(do_click)
            return True
        except Exception as e:
            log.warning("OS-level click failed: %s", e)
            return False

    async def try_click_challenge(self, attempt: int = 0, os_level: bool = False) -> bool:
        """Attempt to click the Cloudflare Turnstile checkbox.

        Locates the widget through shadow DOM, then clicks the checkbox
        position (left edge of the widget, vertically centered) — via a
        humanized CDP pointer trail, or the real OS mouse when os_level is
        set. Returns True if a click was actually performed.
        """
        box = await self._find_challenge_iframe_box()
        if box is None:
            log.warning("Challenge widget iframe not found in DOM (attempt %d)", attempt + 1)
            return False
        import random

        x, y, w, h = box
        tx = x + random.uniform(22, 34)  # checkbox sits near the left edge
        ty = y + h / 2 + random.uniform(-4, 4)
        try:
            await self.page.activate()  # mouse events need a focused tab
            await asyncio.sleep(0.2)
            if os_level:
                ok = await self._os_level_click(tx, ty)
            else:
                await self._cdp_human_click(tx, ty)
                ok = True
            if ok:
                log.info(
                    "Clicked the human-verification checkbox (%s, attempt %d)",
                    "OS mouse" if os_level else "CDP humanized", attempt + 1,
                )
            return ok
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


# Auto-click attempts for the Cloudflare human-verification checkbox:
# humanized-CDP clicks first, then (when enabled) clicks with the real OS
# mouse. Shared by the monitor loop and the auto-login flow.
CHALLENGE_CDP_ATTEMPTS = 3
CHALLENGE_OS_ATTEMPTS = 3


async def resolve_challenge(session: BrowserSession, os_click_enabled: bool) -> bool:
    """Run the full challenge auto-click sequence; True once it clears."""
    total = CHALLENGE_CDP_ATTEMPTS + (CHALLENGE_OS_ATTEMPTS if os_click_enabled else 0)
    for attempt in range(total):
        use_os = attempt >= CHALLENGE_CDP_ATTEMPTS
        log.info(
            "Challenge auto-click attempt %d/%d (%s)",
            attempt + 1, total, "OS mouse" if use_os else "CDP humanized",
        )
        await session.try_click_challenge(attempt, os_level=use_os)
        # Turnstile takes a few seconds to verify after a click.
        await asyncio.sleep(random.uniform(5, 9))
        if await session.detect_state(timeout=5) is not PageState.CHALLENGE:
            log.info("Challenge cleared.")
            return True
    return False


def match_kba_answer(label_text: str, qa_pairs: tuple[tuple[str, str], ...]) -> str | None:
    """Match an on-screen security-question label to a configured answer.

    Comparison is case-, whitespace- and punctuation-insensitive, and a
    substring in either direction counts (labels often carry decorations
    like a trailing '*' or embedded numbering).
    """

    def norm(s: str) -> str:
        return re.sub(r"[^0-9a-z一-鿿]", "", s.lower())

    target = norm(label_text)
    if not target:
        return None
    for question, answer in qa_pairs:
        q = norm(question)
        if q and (q in target or target in q):
            return answer
    return None


async def type_slowly(element, text: str) -> None:
    """Focus a field and type with human-like per-character delays."""
    await element.click()
    await asyncio.sleep(random.uniform(0.2, 0.5))
    try:
        await element.apply('(el) => { el.value = ""; }')
    except Exception:
        pass
    for ch in text:
        await element.send_keys(ch)
        await asyncio.sleep(random.uniform(0.03, 0.12))


class LoginStrategy(ABC):
    """Pluggable session-recovery strategy.

    The monitor calls recover() when it detects session loss. Strategies
    with handles_own_alerts=True email the user themselves (only when human
    action is actually needed); for the others the monitor sends its
    session-lost alert before calling recover().
    """

    handles_own_alerts = False

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


# JS probes for the auto-login flow. B2C pre-renders hidden error
# containers, so error text only counts when the element is visible.
_JS_CLICK_SIGNIN_LINK = """
(function() {
  let a = document.querySelector("a[href*='signin' i], a[href*='sign-in' i]");
  if (!a) {
    a = [...document.querySelectorAll('a')]
      .find(x => /登录|sign\\s*in/i.test((x.textContent || '').trim()));
  }
  if (a) { a.click(); return true; }
  return false;
})()
"""

_JS_VISIBLE_ERROR = """
(function() {
  const texts = [...document.querySelectorAll("[role=alert], .error, .alert-danger")]
    .filter(el => el.offsetParent !== null)
    .map(el => (el.textContent || '').trim())
    .filter(Boolean);
  return texts.length ? texts.join(' | ') : null;
})()
"""

# The answer input's own <label> is empty; the question lives in a
# SEPARATE preceding <li> as <p class="textInParagraph"> (its id is
# inconsistent: kbq1ReadOnly, kbq2bReadOnly, ...). Pair each answer input
# with the nearest question paragraph before it in DOM order.
_JS_READ_KBA_QUESTIONS = """
JSON.stringify([...document.querySelectorAll("input[id^='kba'][id$='_response']")]
  .filter(inp => inp.offsetParent !== null)
  .map(inp => {
    let q = '';
    const li = inp.closest('li');
    let prev = li ? li.previousElementSibling : null;
    while (prev) {
      if (prev.querySelector("input[id^='kba'][id$='_response']")) break;
      const p = prev.querySelector('.textInParagraph, p[aria-label]');
      if (p) { q = (p.textContent || p.getAttribute('aria-label') || '').trim(); break; }
      prev = prev.previousElementSibling;
    }
    if (!q) {
      const lbl = document.querySelector("label[for='" + inp.id + "']");
      if (lbl) q = (lbl.textContent || '').trim();
    }
    return {id: inp.id, question: q};
  }))
"""


class AutoLoginStrategy(LoginStrategy):
    """Log in automatically with configured credentials; fall back to
    manual (with one email alert) when automation fails.

    Flow per attempt: clear any Cloudflare challenge -> click the sign-in
    link on the home page -> B2C screen 1 (#signInName/#password) -> B2C
    screen 2 (a random 2 of 3 security questions, matched against the
    on-screen label text) -> schedule page.
    """

    handles_own_alerts = True

    def __init__(
        self,
        creds: LoginCreds,
        notifier,
        poll_seconds: float,
        os_click_enabled: bool,
        max_attempts: int = 2,
        attempt_timeout: float = 240,
    ):
        self._creds = creds
        self._notifier = notifier
        self._os_click = os_click_enabled
        self._max_attempts = max_attempts
        self._attempt_timeout = attempt_timeout
        self._manual = ManualLoginStrategy(poll_seconds)

    async def recover(self, session: BrowserSession) -> bool:
        for attempt in range(1, self._max_attempts + 1):
            log.info("Auto-login attempt %d/%d", attempt, self._max_attempts)
            try:
                if await self._attempt(session):
                    log.info("Auto-login succeeded.")
                    return True
            except Exception:
                log.exception("Auto-login attempt %d crashed", attempt)
            log.warning("Auto-login attempt %d/%d failed", attempt, self._max_attempts)
        shot = await session.screenshot("auto-login-failed")
        self._notifier.auto_login_failed(shot)
        log.warning("Falling back to MANUAL login — see the alert email.")
        return await self._manual.recover(session)

    async def _attempt(self, session: BrowserSession) -> bool:
        deadline = asyncio.get_running_loop().time() + self._attempt_timeout
        filled_credentials = False
        filled_kba = False
        while asyncio.get_running_loop().time() < deadline:
            state = await session.detect_state(timeout=5)
            if state is PageState.READY:
                return True
            if state is PageState.CHALLENGE:
                if not await resolve_challenge(session, self._os_click):
                    log.warning("Challenge did not clear during auto-login")
                    return False
                continue
            if state is PageState.BLOCKED:
                log.warning("Blocked page during auto-login")
                return False

            # B2C screen 1: credentials.
            if await eval_js(session.page, "!!document.querySelector('#signInName')"):
                error = await eval_js(session.page, _JS_VISIBLE_ERROR)
                if error:
                    log.error("Login page error: %s", error)
                    return False
                if filled_credentials:
                    await asyncio.sleep(2)  # submitted; wait for transition
                    continue
                await self._fill_credentials(session)
                filled_credentials = True
                continue

            # B2C screen 2: security questions (2 of 3 rendered).
            kba = await self._read_kba(session)
            if kba:
                error = await eval_js(session.page, _JS_VISIBLE_ERROR)
                if error:
                    log.error("Security-question page error: %s", error)
                    return False
                if filled_kba:
                    await asyncio.sleep(2)
                    continue
                if not await self._fill_kba(session, kba):
                    return False
                filled_kba = True
                continue

            # Logged in but somewhere else -> go to the schedule page.
            if await session.is_logged_in():
                log.info("Logged in — navigating to the schedule page...")
                await session.goto_schedule()
                continue

            # Logged-out home page -> click the sign-in link.
            if await eval_js(session.page, _JS_CLICK_SIGNIN_LINK):
                log.info("Clicked the sign-in link")
                await asyncio.sleep(random.uniform(3, 5))
                continue

            await asyncio.sleep(2)
        log.warning("Auto-login attempt timed out after %.0f s", self._attempt_timeout)
        return False

    async def _fill_credentials(self, session: BrowserSession) -> None:
        log.info("Filling username/password...")
        page = session.page
        user_el = await page.select("#signInName", timeout=10)
        await type_slowly(user_el, self._creds.username)
        pass_el = await page.select("#password", timeout=10)
        await type_slowly(pass_el, self._creds.password)
        await asyncio.sleep(random.uniform(0.5, 1.2))
        await eval_js(page, "document.querySelector('#continue')?.click()")
        log.info("Submitted credentials")
        await asyncio.sleep(random.uniform(2, 4))

    async def _read_kba(self, session: BrowserSession) -> list[dict]:
        raw = await eval_js(session.page, _JS_READ_KBA_QUESTIONS)
        try:
            return json.loads(raw) if raw else []
        except (TypeError, ValueError):
            return []

    async def _fill_kba(self, session: BrowserSession, kba: list[dict]) -> bool:
        # The two questions can render with a short stagger; give the page
        # a moment and re-read so we fill both in one pass.
        if len(kba) < 2:
            await asyncio.sleep(3)
            kba = await self._read_kba(session) or kba
        page = session.page
        for item in kba:
            question = item.get("question") or ""
            answer = match_kba_answer(question, self._creds.security_questions)
            if answer is None:
                log.error(
                    "No configured answer matches security question %r — "
                    "add it to credentials.toml exactly as displayed",
                    question,
                )
                return False
            el = await page.select(f"#{item['id']}", timeout=10)
            log.info("Answering security question: %s", question[:60])
            await type_slowly(el, answer)
            await asyncio.sleep(random.uniform(0.4, 1.0))
        await eval_js(page, "document.querySelector('#continue')?.click()")
        log.info("Submitted security answers")
        await asyncio.sleep(random.uniform(2, 4))
        return True
