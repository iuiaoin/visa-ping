"""CLI entry point.

Note: nodriver owns its event loop, so the async main is driven by
uc.loop().run_until_complete(...) rather than asyncio.run().
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, ConfigError, EmailCreds, load_config, load_email_creds, setup_logging


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="visa-ping",
        description="Monitor US visa appointment slots on usvisascheduling.com",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config.toml"),
        help="Path to config.toml (default: ./config.toml)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single check cycle then exit (for supervised testing)",
    )
    parser.add_argument(
        "--test-email", action="store_true",
        help="Send a test email and exit (no browser)",
    )
    return parser.parse_args(argv)


def _banner(cfg: Config, log) -> None:
    log.info("=" * 60)
    log.info("visa-ping effective configuration:")
    log.info("  scenario  : %s", cfg.scenario)
    log.info("  consulate : %s", cfg.consulate.guid or cfg.consulate.name)
    log.info("  date range: %s .. %s", cfg.dates.earliest, cfg.dates.latest)
    log.info("  months    : %d", cfg.monitor.months_to_scan)
    log.info("  booking   : %s", "ENABLED" if cfg.booking.enabled else "disabled")
    if cfg.booking.enabled:
        log.info("  dry_run   : %s", "yes (safe mode)" if cfg.booking.dry_run else
                 "NO — REAL BOOKINGS WILL BE SUBMITTED")
    log.info("=" * 60)


def _test_email(cfg: Config, creds: EmailCreds) -> int:
    from .notify import Notifier

    label = cfg.consulate.name or cfg.consulate.guid or "?"
    notifier = Notifier(creds, label, cfg.dates)
    ok = notifier.send("test email", "visa-ping email configuration works.")
    print("Test email sent." if ok else "Test email FAILED — check .env and logs.")
    return 0 if ok else 1


async def _async_main(cfg: Config, creds: EmailCreds, once: bool) -> None:
    from .browser import BrowserSession, ManualLoginStrategy, target_url_for
    from .monitor import Monitor
    from .notify import Notifier

    label = cfg.consulate.name or cfg.consulate.guid or "?"
    notifier = Notifier(creds, label, cfg.dates)
    session = BrowserSession(
        cfg.paths.profile_dir,
        cfg.paths.screenshots_dir,
        target_url_for(cfg.scenario),
        nav_min_interval=cfg.monitor.nav_min_interval_seconds,
    )
    login = ManualLoginStrategy(cfg.monitor.session_poll_seconds)
    monitor = Monitor(session, notifier, login, cfg)

    await session.start()
    try:
        if once:
            await monitor.startup()
            await monitor.run_cycle(refresh=False)
        else:
            await monitor.run()
    finally:
        session.stop()


def cli(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        cfg = load_config(args.config)
        creds = load_email_creds()
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    log = setup_logging(cfg.paths.log_file)
    _banner(cfg, log)

    if args.test_email:
        return _test_email(cfg, creds)

    import nodriver as uc

    try:
        uc.loop().run_until_complete(_async_main(cfg, creds, args.once))
    except KeyboardInterrupt:
        log.info("Interrupted — exiting.")
    return 0


if __name__ == "__main__":
    sys.exit(cli())
