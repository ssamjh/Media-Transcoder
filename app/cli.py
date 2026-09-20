"""Process entry point: start the daemon.

There are no subcommands. Everything this tool does - scanning, planning,
processing a single file, editing libraries, modes and integrations, taking
and restoring backups - is done from the web panel or its API, and having
one way to do a thing is worth more than having two that can disagree.

What is left here is what has to happen before the panel exists: read the
config (writing it on first start, with an API key), check that the
directories it will write to are writable, and hand off to the engine.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import signal
import sys
import time
from pathlib import Path

from . import config as config_mod
from .db import Db
from .engine import Engine

DEFAULT_CONFIG = "/config/config.toml"


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="transcoder", description=__doc__)
    ap.add_argument("-c", "--config",
                    default=os.environ.get("TRANSCODER_CONFIG", DEFAULT_CONFIG),
                    help="path to config.toml")
    ap.add_argument("--db", help="override the state database path")
    ap.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "info"))
    return ap


def _owner() -> str | None:
    """This process's uid:gid, or None off POSIX."""
    if hasattr(os, "getuid"):
        return f"{os.getuid()}:{os.getgid()}"
    return None


def _writable(d: Path) -> str | None:
    """None if files can be created in d, else why not."""
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / f".write-test-{os.getpid()}"
        probe.touch()
        probe.unlink()
    except OSError as exc:
        return str(exc)
    return None


def _preflight(cfg, log, check_temp: bool = True) -> bool:
    """Check the writable directories up front.

    A bind mount carries the host directory's ownership, so a container
    running as a non-root uid cannot write to a directory the host created
    as root. Finding that out per file means every encode dies mid-flight
    and burns an attempt, so it is worth one check at startup.
    """
    checks = [("state database directory", Path(cfg.state_db).parent)]
    if check_temp:
        checks.append(("encode scratch directory", Path(cfg.output.temp_dir)))

    ok = True
    for what, d in checks:
        why = _writable(d)
        if why is None:
            continue
        ok = False
        owner = _owner()
        log.error("%s %s is not writable as %s: %s", what, d,
                  f"uid:gid {owner}" if owner else "this user", why)
        if owner:
            log.error("  a bind mount keeps the host directory's ownership. "
                      "Set PUID/PGID to whoever owns it on the host, or "
                      "chown -R %s <the host directory mounted at %s>",
                      owner, d)
    return ok


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.log_level)
    log = logging.getLogger("transcoder")

    try:
        cfg = config_mod.load(args.config)
    except (config_mod.ConfigError, OSError) as exc:
        log.error("config: %s", exc)
        return 2

    # First start: materialise the config file, and mint an API key so Sonarr
    # and Radarr have a credential to present. Both are written once and then
    # left alone.
    if args.config:
        fresh = not Path(args.config).exists()
        if not cfg.web.api_key.strip():
            cfg.web.api_key = secrets.token_urlsafe(24)
            fresh = True
        if fresh:
            try:
                config_mod.save(cfg, args.config)
                log.info("wrote %s", args.config)
            except OSError as exc:
                log.warning("could not write %s: %s", args.config, exc)

    if args.db:
        cfg.state_db = args.db

    if not _preflight(cfg, log, check_temp=not cfg.dry_run):
        return 2

    db = Db(cfg.state_db)
    engine = Engine(cfg, db, config_path=args.config)

    def handle_signal(signum, _frame):
        log.warning("signal %s received, stopping", signum)
        engine.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, AttributeError):
            pass

    httpd = None
    try:
        if cfg.web.enabled:
            from .web import serve
            httpd = serve(cfg, engine)
        else:
            log.warning("the web panel is disabled: nothing can drive this "
                        "daemon until web.enabled is set in %s", args.config)
        engine.start()
        while not engine.stopping:
            time.sleep(0.5)
    finally:
        engine.stop()
        engine.join()
        if httpd:
            httpd.shutdown()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
