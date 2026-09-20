"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import signal
import sqlite3
import sys
import time
from pathlib import Path

from . import backup as backup_mod
from . import config as config_mod
from .db import Db
from .engine import Engine, hms
from .plan import FilePlan
from .probe import ProbeError

DEFAULT_CONFIG = "/config/config.toml"


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}" if unit == "GB" else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    widths[0] = min(widths[0], 58)

    def fmt(cells: list[str]) -> str:
        out = []
        for i, c in enumerate(cells):
            c = c if len(c) <= widths[i] else c[: widths[i] - 3] + "..."
            out.append(c.ljust(widths[i]))
        return "  ".join(out).rstrip()

    return "\n".join([fmt(headers), "  ".join("-" * w for w in widths)]
                     + [fmt(r) for r in rows])


def _print_scan(res) -> None:
    work = res.needs_work
    rows = [[
        Path(p.path).name,
        p.library_name,
        f"{p.height}p" if p.height else "?",
        p.video_summary,
        p.audio_summary,
        p.subs_summary,
        _size(p.size),
    ] for p in sorted(work, key=lambda x: x.path)]

    if rows:
        print(_table(rows, ["File", "Library", "Res", "Video", "Audio",
                            "Subs", "Size"]))
        print()
        for p in sorted(work, key=lambda x: x.path):
            print(f"  {Path(p.path).name}")
            for r in p.reasons:
                print(f"      - {r}")
            for d in p.dropped:
                print(f"      x drop {d}")
        print()
    else:
        print("Nothing needs work.\n")

    print(
        f"{len(res.planned) + res.cached} file(s) examined - {len(work)} need work "
        f"({_size(sum(p.size for p in work))}) - {res.skipped} already fine - "
        f"{res.cached} cached - {len(res.errors)} error(s) - {res.elapsed:.1f}s"
    )
    for lib_id, t in (res.per_library or {}).items():
        print(f"    {t['name']}: {t['examined']} examined, "
              f"{t['need_work']} need work, {t['already_fine']} already fine, "
              f"{t['cached']} cached")
    for path, err in res.errors[:20]:
        print(f"  ! {Path(path).name}: {err}")


def _print_plan(plan: FilePlan) -> None:
    print(Path(plan.path).name)
    print(f"  library    : {plan.library_name} ({plan.library})")
    print(f"  container  : {plan.container}")
    print(f"  resolution : {plan.height or '?'}p")
    print(f"  video      : {plan.video_summary}")
    print(f"  audio      : {plan.audio_summary}")
    print(f"  subtitles  : {plan.subs_summary}")
    if plan.skip_reason:
        print(f"  skipped    : {plan.skip_reason}")
    elif plan.needs_work:
        print("  work to do :")
        for r in plan.reasons:
            print(f"      - {r}")
        for d in plan.dropped:
            print(f"      x drop {d}")
        print("  output streams:")
        for i, s in enumerate(plan.streams):
            extra = ", ".join(filter(None, [
                s.note,
                "default" if s.disposition == "default" else "",
            ]))
            print(f"      {i}: {s.kind:<9} {s.codec:<8} from 0:{s.src_index}"
                  f"{'  (' + extra + ')' if extra else ''}")
    else:
        print("  nothing to do")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="transcoder", description=__doc__)
    ap.add_argument("-c", "--config",
                    default=os.environ.get("TRANSCODER_CONFIG", DEFAULT_CONFIG),
                    help="path to config.toml")
    ap.add_argument("--db", help="override the state database path")
    ap.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "info"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daemon", help="run the scheduler, workers and web panel")

    p = sub.add_parser("scan", help="report what would change, touch nothing")
    p.add_argument("paths", nargs="*", help="restrict to these paths")
    p.add_argument("-L", "--library", help="scan only this library (by id)")
    p.add_argument("--no-cache", action="store_true",
                   help="re-probe every file, ignoring stored state")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("check", help="inspect a single file and print its plan")
    p.add_argument("path")
    p.add_argument("-m", "--mode", help="plan under this mode instead of the "
                                        "library's own profile")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("run", help="scan, then process everything pending")
    p.add_argument("paths", nargs="*")
    p.add_argument("--no-scan", action="store_true",
                   help="process what is already queued without rescanning")
    p.add_argument("--dry-run", action="store_true",
                   help="log what would be encoded without encoding")

    p = sub.add_parser("process", help="process one file now")
    p.add_argument("path")
    p.add_argument("-m", "--mode", help="apply this mode for this run only")

    p = sub.add_parser("backup", help="snapshot the state database, or restore one")
    p.add_argument("--list", action="store_true", dest="list_only",
                   help="list the snapshots that exist and do nothing else")
    p.add_argument("--restore", metavar="FILE",
                   help="replace the state database with this snapshot "
                        "(stop the daemon first)")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("status", help="print stored state")
    p.add_argument("--failed", action="store_true", help="list failures")

    p = sub.add_parser("config", help="show or change global settings")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", default=[],
                   help="set a dotted key, e.g. --set workers.pools=8")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("modes", help="list, add, change or remove processing modes")
    p.add_argument("-m", "--mode", help="operate on this mode id")
    p.add_argument("--add", metavar="NAME", help="create a mode")
    p.add_argument("--copy-from", metavar="MODE", dest="copy_from",
                   help="start the new mode as a copy of this one")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", default=[],
                   help="set a processing setting, e.g. --set video.enabled=false")
    p.add_argument("--name", help="rename the mode")
    p.add_argument("--description", help="set the description")
    p.add_argument("--remove", action="store_true", help="delete the mode")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("libraries", help="list, add, change or remove libraries")
    p.add_argument("-L", "--library", help="operate on this library id")
    p.add_argument("--add", metavar="NAME", help="create a library")
    p.add_argument("--path", action="append", default=[], metavar="DIR",
                   help="path for --add, repeatable")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", default=[],
                   help="set a library key, e.g. --set mode=cleanup")
    p.add_argument("--remove", action="store_true", help="delete the library")
    p.add_argument("--json", action="store_true")
    return ap


# Subcommands that will actually encode something, and so need the scratch
# directory to be writable before they start rather than three attempts in.
_ENCODES = {"daemon", "run", "process"}


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


def _preflight(cfg, log, check_temp: bool) -> bool:
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
    if getattr(args, "dry_run", False):
        cfg.dry_run = True

    if not _preflight(cfg, log, check_temp=args.cmd in _ENCODES and not cfg.dry_run):
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

    try:
        return _dispatch(args, cfg, db, engine, log)
    finally:
        db.close()


def _print_libraries(cfg) -> None:
    if not cfg.libraries:
        print("No libraries configured. Add one in the web panel, or put a "
              "[[libraries]] block in the config file.")
        return
    rows = []
    for lib in cfg.libraries:
        rows.append([
            lib.id,
            lib.name,
            "yes" if lib.enabled else "no",
            lib.mode,
            str(lib.min_size_mb),
            ", ".join(lib.paths),
        ])
    print(_table(rows, ["Id", "Name", "On", "Mode", "Min MB", "Paths"]))


def _print_modes(cfg) -> None:
    rows = []
    for m in cfg.modes:
        stages = ", ".join(filter(None, [
            "video" if m.video.enabled else "",
            "audio" if m.audio.enabled else "",
            "subs" if m.subtitles.enabled else "",
        ])) or "nothing"
        users = ", ".join(l.name for l in cfg.libraries if l.mode == m.id) or "-"
        rows.append([m.id, m.name, stages, m.output.container,
                     "yes" if m.output.replace_original else "no", users])
    print(_table(rows, ["Mode", "Name", "Processes", "Container", "Replace",
                        "Used by"]))
    for m in cfg.modes:
        if m.description:
            print("")
            print(f"  {m.id}")
            print(f"      {m.description}")


def _modes(args, cfg, log) -> int:
    if args.add:
        try:
            mode = config_mod.add_mode(cfg, args.add, args.copy_from)
        except config_mod.ConfigError as exc:
            log.error("%s", exc)
            return 2
        config_mod.save(cfg, args.config)
        print(f"added mode {mode.name!r} as {mode.id}")
        return 0

    if args.remove:
        if not args.mode:
            log.error("--remove needs --mode")
            return 2
        try:
            mode = config_mod.remove_mode(cfg, args.mode)
        except config_mod.ConfigError as exc:
            log.error("%s", exc)
            return 2
        config_mod.save(cfg, args.config)
        print(f"removed mode {mode.name!r}")
        return 0

    if args.set or args.name or args.description:
        if not args.mode:
            log.error("changing a mode needs --mode")
            return 2
        mode = cfg.mode(args.mode)
        if mode is None:
            log.error("no such mode: %s", args.mode)
            return 2

        updates = {}
        for item in args.set:
            key, sep, value = item.partition("=")
            if not sep:
                log.error("--set expects KEY=VALUE, got %r", item)
                return 2
            updates[key.strip()] = value.strip()
        if args.name:
            updates["name"] = args.name
        if args.description:
            updates["description"] = args.description
        try:
            changed = config_mod.apply_mode_updates(cfg, mode, updates)
        except config_mod.ConfigError as exc:
            log.error("%s", exc)
            return 2
        config_mod.save(cfg, args.config)
        print(f"updated {len(changed)} field(s): {', '.join(changed)}"
              if changed else "no changes")
        return 0

    if args.json:
        print(json.dumps(
            [{"id": m.id, "name": m.name, "description": m.description,
              "libraries": [l.id for l in cfg.libraries if l.mode == m.id],
              "settings": {block["section"] or "mode":
                           {f["name"]: f["value"] for f in block["fields"]}
                           for block in config_mod.mode_schema(m)}}
             for m in cfg.modes], indent=2))
    else:
        _print_modes(cfg)
    return 0


def _libraries(args, cfg, log) -> int:
    if args.add:
        try:
            lib = config_mod.add_library(cfg, args.add, args.path)
        except config_mod.ConfigError as exc:
            log.error("%s", exc)
            return 2
        config_mod.save(cfg, args.config)
        print(f"added library {lib.name!r} as {lib.id}")
        return 0

    if args.remove:
        if not args.library:
            log.error("--remove needs --library")
            return 2
        try:
            lib = config_mod.remove_library(cfg, args.library)
        except config_mod.ConfigError as exc:
            log.error("%s", exc)
            return 2
        config_mod.save(cfg, args.config)
        print(f"removed library {lib.name!r}")
        return 0

    if args.set:
        if not args.library:
            log.error("--set needs --library")
            return 2
        lib = cfg.library(args.library)
        if lib is None:
            log.error("no such library: %s", args.library)
            return 2
        updates = {}
        for item in args.set:
            key, sep, value = item.partition("=")
            if not sep:
                log.error("--set expects KEY=VALUE, got %r", item)
                return 2
            updates[key.strip()] = value.strip()
        try:
            changed = config_mod.apply_library_updates(cfg, lib, updates)
        except config_mod.ConfigError as exc:
            log.error("%s", exc)
            return 2
        config_mod.save(cfg, args.config)
        print(f"updated {len(changed)} setting(s): {', '.join(changed)}"
              if changed else "no changes")
        return 0

    if args.json:
        print(json.dumps(
            [{"id": l.id, "schema": config_mod.library_schema(l)}
             for l in cfg.libraries], indent=2))
    else:
        _print_libraries(cfg)
    return 0


def _backup(args, cfg, db, engine: Engine, log) -> int:
    directory = backup_mod.backup_dir(cfg)

    if args.restore:
        # The database has to be closed before its file is replaced, and
        # nothing may reopen it in this process afterwards - main() calls
        # db.close() again, which is harmless.
        db.close()
        try:
            kept = backup_mod.restore(args.restore, cfg.state_db)
        except (ValueError, OSError) as exc:
            log.error("%s", exc)
            return 2
        print(f"restored {args.restore} to {cfg.state_db}")
        if kept:
            print(f"previous database kept at {kept}")
        return 0

    if not args.list_only:
        try:
            path = engine.backup_now()
        except (OSError, sqlite3.Error) as exc:
            log.error("backup failed: %s", exc)
            return 2
        if not args.json:
            print(f"wrote {path}")

    found = backup_mod.list_backups(directory)
    if args.json:
        print(json.dumps({
            "dir": str(directory),
            "keep": cfg.backup.keep,
            "backups": [{"path": str(f), "name": f.name,
                         "size": f.stat().st_size,
                         "taken": f.stat().st_mtime} for f in reversed(found)],
        }, indent=2))
        return 0

    if not found:
        print(f"no backups in {directory}")
        return 0
    print(_table([[f.name, _size(f.stat().st_size),
                   time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))]
                  for f in reversed(found)],
                 ["File", "Size", "Taken"]))
    print("")
    print(f"{len(found)} of {cfg.backup.keep} kept in {directory}")
    return 0


def _dispatch(args, cfg, db, engine: Engine, log) -> int:
    if args.cmd == "daemon":
        httpd = None
        if cfg.web.enabled:
            from .web import serve
            httpd = serve(cfg, engine)
        engine.start()
        try:
            while not engine.stopping:
                time.sleep(0.5)
        finally:
            engine.stop()
            engine.join()
            if httpd:
                httpd.shutdown()
        return 0

    if args.cmd == "scan":
        try:
            res = engine.scan(args.paths or None, use_cache=not args.no_cache,
                              library=args.library)
        except LookupError as exc:
            log.error("%s", exc)
            return 2
        if args.json:
            print(json.dumps({
                "need_work": [p.to_dict() for p in res.needs_work],
                "cached": res.cached, "skipped": res.skipped,
                "removed": res.removed,
                "errors": [{"path": p, "error": e} for p, e in res.errors],
                "elapsed": res.elapsed,
            }, indent=2))
        else:
            _print_scan(res)
        return 0

    if args.cmd == "check":
        try:
            plan = engine.check_one(str(Path(args.path)), mode=args.mode)
        except (ProbeError, LookupError, config_mod.ConfigError) as exc:
            log.error("%s", exc)
            return 1
        if args.json:
            print(json.dumps(plan.to_dict(), indent=2))
        else:
            _print_plan(plan)
        return 0

    if args.cmd == "run":
        if not args.no_scan:
            engine.scan(args.paths or None)
        engine.process_all()
        return 0

    if args.cmd == "process":
        try:
            ok, message = engine.enqueue(str(Path(args.path)), mode=args.mode)
        except config_mod.ConfigError as exc:
            log.error("%s", exc)
            return 2
        if not ok:
            log.error("%s", message)
            return 1
        engine.start()
        while engine.queue_depth and not engine.stopping:
            time.sleep(0.5)
        engine.stop()
        engine.join()
        return 0

    if args.cmd == "backup":
        return _backup(args, cfg, db, engine, log)

    if args.cmd == "status":
        stats = db.stats()
        print(f"tracked : {stats['total']}")
        for k, v in stats["counts"].items():
            if v:
                print(f"  {k:<8}: {v}")
        print(f"encoded : {stats['encoded']}")
        print(f"saved   : {_size(stats['bytes_saved'])}")
        print(f"spent   : {hms(stats['encode_seconds'])} encoding")
        snapshots = backup_mod.list_backups(backup_mod.backup_dir(cfg))
        if snapshots:
            taken = time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime(snapshots[-1].stat().st_mtime))
            print(f"backups : {len(snapshots)} kept, newest {taken}")
        else:
            print("backups : none yet")
        if args.failed:
            rows, _ = db.list_files(status="failed", limit=500)
            print()
            for r in rows:
                print(f"  {r['name']}\n      [{r['attempts']} attempts] {r['error']}")
        return 0

    if args.cmd == "modes":
        return _modes(args, cfg, log)

    if args.cmd == "libraries":
        return _libraries(args, cfg, log)

    if args.cmd == "config":
        if args.set:
            updates = {}
            for item in args.set:
                key, sep, value = item.partition("=")
                if not sep:
                    log.error("--set expects KEY=VALUE, got %r", item)
                    return 2
                updates[key.strip()] = value.strip()
            try:
                changed = config_mod.apply_updates(cfg, updates)
            except config_mod.ConfigError as exc:
                log.error("%s", exc)
                return 2
            config_mod.save(cfg, args.config)
            print(f"updated {len(changed)} setting(s): {', '.join(changed)}"
                  if changed else "no changes")
            return 0
        if args.json:
            print(json.dumps(config_mod.schema(cfg), indent=2))
        else:
            print(config_mod.dump_toml(cfg))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
