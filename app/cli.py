"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import signal
import sys
import time
from pathlib import Path

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
                f'title "{s.title}"' if s.title else "",
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

    p = sub.add_parser("status", help="print stored state")
    p.add_argument("--failed", action="store_true", help="list failures")

    p = sub.add_parser("config", help="show or change global settings")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", default=[],
                   help="set a dotted key, e.g. --set workers.pools=8")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("modes", help="list, add, change or remove processing modes")
    p.add_argument("-m", "--mode", help="operate on this mode id")
    p.add_argument("--add", metavar="NAME", help="create a mode")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", default=[],
                   help="override a library setting, e.g. --set video.enabled=false")
    p.add_argument("--unset", action="append", metavar="KEY", default=[],
                   help="remove an override, falling back to the library")
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
                   help="set a library key, e.g. --set subtitles.enabled=false")
    p.add_argument("--remove", action="store_true", help="delete the library")
    p.add_argument("--json", action="store_true")
    return ap


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
        stages = ", ".join(filter(None, [
            "video" if lib.video.enabled else "",
            "audio" if lib.audio.enabled else "",
            "subs" if lib.subtitles.enabled else "",
        ])) or "nothing"
        rows.append([
            lib.id,
            lib.name,
            "yes" if lib.enabled else "no",
            stages,
            lib.output.container,
            "yes" if lib.output.replace_original else "no",
            ", ".join(lib.paths),
        ])
    print(_table(rows, ["Id", "Name", "On", "Processes", "Container",
                        "Replace", "Paths"]))


def _print_modes(cfg) -> None:
    rows = []
    for m in cfg.modes:
        overrides = ", ".join(f"{k}={_fmt_value(v)}"
                              for k, v in m.overrides.items()) or "library profile"
        rows.append([m.id, m.name, overrides])
    print(_table(rows, ["Mode", "Name", "Overrides"]))
    for m in cfg.modes:
        if m.description:
            print("")
            print(f"  {m.id}")
            print(f"      {m.description}")


def _fmt_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _modes(args, cfg, log) -> int:
    if args.add:
        try:
            mode = config_mod.add_mode(cfg, args.add)
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

    if args.set or args.unset or args.name or args.description:
        if not args.mode:
            log.error("changing a mode needs --mode")
            return 2
        mode = cfg.mode(args.mode)
        if mode is None:
            log.error("no such mode: %s", args.mode)
            return 2

        overrides = dict(mode.overrides)
        for item in args.set:
            key, sep, value = item.partition("=")
            if not sep:
                log.error("--set expects KEY=VALUE, got %r", item)
                return 2
            overrides[key.strip()] = value.strip()
        for key in args.unset:
            overrides.pop(key.strip(), None)

        updates = {"overrides": overrides}
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
              "overrides": m.overrides} for m in cfg.modes], indent=2))
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

    if args.cmd == "status":
        stats = db.stats()
        print(f"tracked : {stats['total']}")
        for k, v in stats["counts"].items():
            if v:
                print(f"  {k:<8}: {v}")
        print(f"encoded : {stats['encoded']}")
        print(f"saved   : {_size(stats['bytes_saved'])}")
        print(f"spent   : {hms(stats['encode_seconds'])} encoding")
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
