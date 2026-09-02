# visa-ping

Monitor US visa appointment slots on [usvisascheduling.com](https://www.usvisascheduling.com)
(the portal used for consulates in China), email yourself when dates inside your
configured range appear, and optionally auto-book the earliest one.

- Drives a **real, visible Chrome** via [nodriver](https://github.com/ultrafunkamsterdam/nodriver)
  (no webdriver fingerprint) with a persistent profile, so your login survives restarts.
- **You log in manually** (captcha + security questions) — the program never touches
  your credentials. If the session expires later, you get an email and simply log in
  again in the still-open window; monitoring resumes automatically.
- **Randomized polling** (default 3–5 min between checks, plus a longer 7–9 min rest
  after every 4–7 checks) so there is no fixed request period for the site to detect.
- **Auto-booking is OFF by default**, and even when enabled it defaults to
  `dry_run = true`, which performs every step except the final submit click.

> Personal-use tool. The site's terms prohibit automated access — use at your own
> risk, keep the intervals conservative, and prefer notify-only mode.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or plain pip)
- Google Chrome installed
- A Gmail account with an **App Password** for sending notifications
- The machine must stay awake while monitoring (macOS: run `caffeinate -i` in
  another terminal, or use Amphetamine)

## Setup

```bash
git clone <this repo> && cd visa-ping
uv sync                       # or: pip install -e .
cp .env.example .env          # fill in Gmail credentials
cp config.example.toml config.toml   # edit consulate + date range
```

> In China, if PyPI is slow/unreachable:
> `UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync`

### Gmail App Password

1. Google Account → Security → enable **2-Step Verification** (required).
2. Google Account → Security → **App passwords** → create one for "Mail".
3. Put the 16-character password in `.env` as `GMAIL_APP_PASSWORD`.

Verify with:

```bash
uv run visa-ping --test-email
```

## Configuration

Secrets live in `.env`; behavior lives in `config.toml` (see the comments in
`config.example.toml` for every key). The important ones:

| Key | Meaning |
|---|---|
| `scenario` | `"reschedule"` (default): change an existing appointment; `"schedule"`: book a brand-new one |
| `[consulate] name` | Substring matched against the consulate dropdown (e.g. `"SHANGHAI"`); use `guid` for an exact match |
| `[dates] earliest` / `latest` | Only slots inside this inclusive range trigger alerts/booking. Fixed (`2026-09-15`) or relative (`"today+5"`, re-resolved every cycle) |
| `[monitor] months_to_scan` | How many calendar months to scan per check |
| `[monitor] check_interval_*` / `rest_*` | Randomized anti-bot pacing (seconds) |
| `[monitor] heartbeat_enabled` | Optional periodic "still alive" email |
| `[booking] enabled` | Master switch for auto-booking (default `false`) |
| `[booking] dry_run` | When booking is enabled, stop just before the submit click (default `true`) |

## Running

```bash
uv run visa-ping              # continuous monitoring
uv run visa-ping --once       # one supervised check cycle, then exit
```

First run: Chrome opens on the **home page** (deep-linking to `/schedule/`
without a session trips the Cloudflare WAF) → log in manually (username,
password, captcha, security questions) → once a logged-in session is detected
the program navigates to the schedule/reschedule page itself and monitoring
starts. The Chrome profile is stored in `chrome_profile/`, so subsequent runs
usually skip the login entirely.

If the program keeps printing "Waiting for manual login" even though you have
logged in, just open the schedule page manually in that same tab — it detects
the ready page directly and proceeds.

### What the emails mean

| Subject | Meaning |
|---|---|
| `... N in-range date(s) available!` | New dates in your range (with +/− diff and full lists) |
| `... ACTION REQUIRED: session expired` | Go log in again in the open Chrome window (sent once per outage) |
| `... session recovered` | Monitoring resumed |
| `... BOOKED / DRY-RUN / BOOKING FAILED` | Booking outcome, screenshot attached |
| `... heartbeat` | Periodic liveness report (if enabled) |

Change notifications are diff-based and persisted in `state.json`, so restarts
don't re-send old alerts. Delete `state.json` to force re-notification.

## Auto-booking

1. Set `[booking] enabled = true` and keep `dry_run = true`.
2. Wait for (or provoke, with a wide date range) a hit. Verify from the email +
   screenshot that the right date/time were selected and no submit happened.
3. Only then set `dry_run = false` with your real, narrow date range.

When a real submit is clicked the monitor **stops permanently** — it never
retries or reschedules, even if post-submit verification is inconclusive
(in that case the email says `CHECK THE OFFICIAL SITE NOW`).

## Session expiry

The monitor's own polling keeps the session alive (sliding expiration), so in
practice re-login is needed roughly once a day — usually caused by site
maintenance, waiting-room events, or IP changes rather than idling. On expiry
you get one email; log in again in the open window and monitoring resumes.

## Development

```bash
uv sync --extra dev
uv run pytest             # pure unit tests: no browser, no network
```

Manual E2E checklist (against the live site, always with `--once` or default
intervals — never a tight loop):

1. `--once`: verify login flow, consulate selection, and the scraped dates in the log.
2. Delete `state.json`, run `--once` → change email arrives; run again → no duplicate.
3. Log out in the browser mid-run → one alert email; log in → recovery email + resumed monitoring.
4. `enabled = true, dry_run = true` with a wide range → 4 booking steps execute, no submit, screenshot email arrives.
5. Only if actually wanted: real booking with a truthful narrow range.

## Troubleshooting

- **"No consulate option matches ..."** — the error lists every dropdown option;
  copy the exact GUID into `[consulate] guid`.
- **Cloudflare Error 1015 (rate limited)** — the site rate-limits page loads
  over a ~30 s window. The monitor spaces its own page loads at least
  `nav_min_interval_seconds` (35 s) apart and backs off 10 minutes when it
  sees the block page; just let it wait. Avoid manually refreshing the
  monitored tab in quick succession.
- **Human-verification challenge ("Verify you are human")** — the monitor
  locates the checkbox through the widget's shadow DOM and clicks it: first
  3 attempts with a humanized CDP pointer trail, then 3 with the REAL system
  mouse via pyautogui (`challenge_os_click = true`; your cursor moves briefly
  and is restored). If all 6 fail you get one email with a screenshot — click
  the checkbox in the open Chrome window and monitoring resumes automatically.
  For the OS-mouse clicks on macOS, grant **Accessibility** permission to the
  terminal app running visa-ping (System Settings → Privacy & Security →
  Accessibility), and keep the Chrome window visible (not minimized, not on
  another Space).
- **Stuck in waiting room / "You are now in line"** — the site is queueing
  everyone; the monitor waits passively until the queue clears (the log shows
  the estimated wait when the page states one). Do NOT refresh or navigate
  that tab manually — the queue page holds your place and advances itself.
- **Emails not arriving** — run `--test-email`; check `logs/visa-ping.log`.
  Gmail SMTP from mainland China may need a proxy.
- **Calendar never renders** — the site markup may have changed; check the
  selectors in `src/visa_ping/scraper.py` against the live page.
