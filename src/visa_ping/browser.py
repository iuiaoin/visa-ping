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

log = logging.getLogger("visa_ping.browser")

SCHEDULE_URL = "https://www.usvisascheduling.com/zh-CN/schedule/"


async def eval_js(page: "uc.Tab", js: str):
    """Evaluate JS returning a plain Python value (not a CDP RemoteObject)."""
    return await page.evaluate(js, return_by_value=True)


class PageState(Enum):
    READY = auto()           # schedule page usable (#post_select present)
    LOGIN_REQUIRED = auto()  # B2C login page or a password field is showing
    WAITING_ROOM = auto()    # site parked us in its high-traffic waiting room
    UNKNOWN = auto()         # nothing recognizable within the timeout


class BrowserSession:
    """Owns the Chrome instance and the single tab used for everything."""

    def __init__(self, profile_dir: Path, screenshots_dir: Path):
        self._profile_dir = profile_dir
        self._screenshots_dir = screenshots_dir
        self.browser: uc.Browser | None = None
        self.page: uc.Tab | None = None

    async def start(self) -> None:
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._screenshots_dir.mkdir(parents=True, exist_ok=True)
        cfg = uc.Config()
        cfg.user_data_dir = str(self._profile_dir)
        cfg.headless = False  # manual login requires a visible window
        self.browser = await uc.start(cfg)
        log.info("Chrome started (profile: %s)", self._profile_dir)

    async def goto_schedule(self) -> None:
        """Navigate the tab to the schedule page (also serves as refresh)."""
        if self.page is None:
            self.page = await self.browser.get(SCHEDULE_URL)
        else:
            await self.page.get(SCHEDULE_URL)
        await asyncio.sleep(2)  # let the initial navigation settle

    async def refresh(self) -> None:
        await self.goto_schedule()

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
        while True:
            url = await self._eval("location.href") or ""
            if "b2clogin.com" in url:
                return PageState.LOGIN_REQUIRED

            style = await self._eval("document.body && document.body.getAttribute('style')")
            if style and "waiting_room_background" in style:
                return PageState.WAITING_ROOM

            if await self._eval("!!document.querySelector('input[type=password]')"):
                return PageState.LOGIN_REQUIRED

            if await self._eval("!!document.querySelector('#post_select')"):
                return PageState.READY

            if asyncio.get_running_loop().time() > deadline:
                log.warning("detect_state timed out; current URL: %s", url)
                return PageState.UNKNOWN
            await asyncio.sleep(1)

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
                log.info("Login detected — session recovered.")
                return True
            await asyncio.sleep(self._poll_seconds)
