import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from filelock import FileLock, Timeout

from .bridge import Bridge
from .config import load_config
from .notion import Notion
from .state import State
from .watcher import watch


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ingest local Buzz TXT transcripts into Notion")
    parser.add_argument("--env", type=Path, default=Path(".env"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("watch")
    sub.add_parser("status")
    sub.add_parser("retry")
    sub.add_parser("check", help="Validate Notion schema and course access without writing")
    sub.add_parser("process").add_argument("file", type=Path)
    resolve = sub.add_parser(
        "reconcile", help="Clear an uncertain-write journal after manual inspection"
    )
    resolve.add_argument("file", type=Path)
    resolve.add_argument("--confirm-not-applied", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.env)
        config.log.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[
                logging.StreamHandler(),
                RotatingFileHandler(
                    config.log, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
                ),
            ],
        )
        logging.getLogger("httpx").setLevel(logging.WARNING)
        config.state.parent.mkdir(parents=True, exist_ok=True)
        if args.command == "status":
            state = State(config.state)
            try:
                print(json.dumps(state.jobs(), indent=2))
            finally:
                state.close()
            return 0
        with FileLock(str(config.state) + ".lock", timeout=0):
            state, notion = State(config.state), Notion(config)
            try:
                bridge = Bridge(config, state, notion)
                if args.command == "watch":
                    watch(bridge)
                elif args.command == "process":
                    return 0 if bridge.process(args.file) else 1
                elif args.command == "retry":
                    results = [
                        bridge.process(row["path"])
                        for row in state.jobs()
                        if row["status"] != "done"
                    ]
                    return 0 if all(results) else 1
                elif args.command == "check":
                    if bridge.transcriber:
                        bridge.transcriber.check()
                        logging.info("Groq audio model and bundled audio decoder validated")
                    if bridge.reviewer:
                        bridge.reviewer.check()
                        logging.info("Groq model access validated; transcript review enabled")
                    else:
                        logging.info("Groq review disabled (set GROQ_ENABLED=true to enable)")
                    notion.prepare()
                    if not config.courses:
                        raise ValueError("Add course page IDs to courses.yaml")
                    for code, page_id in config.courses.items():
                        page = notion.request("GET", f"pages/{page_id}")
                        relation = notion.schema[config.props["course"]]["relation"]
                        parent = page.get("parent", {})
                        target = relation.get("data_source_id") or relation.get("database_id")
                        actual = parent.get("data_source_id") or parent.get("database_id")
                        if target and actual and target.replace("-", "") != actual.replace("-", ""):
                            raise ValueError(
                                f"Course {code} is not in the Course relation's database"
                            )
                    logging.info(
                        "Notion schema and %d course mappings validated", len(config.courses)
                    )
                else:
                    path = args.file.resolve()
                    if not state.get(path) or not state.get(path)["pending"]:
                        raise ValueError("No uncertain write recorded for this file")
                    state.journal(path, None)
                    state.set(path, next_retry=0)
                    logging.info("Journal cleared; run retry to resume: %s", path)
            finally:
                notion.close()
                state.close()
        return 0
    except Timeout:
        logging.error(
            "Another bridge process holds this state database; stop watch before retry/process"
        )
        return 1
    except (Exception, KeyboardInterrupt) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
