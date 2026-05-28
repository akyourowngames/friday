from __future__ import annotations

import argparse
import json
import sys

from .engine import build_scheduler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KING scheduler CLI")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all scheduled items")
    sub.add_parser("status", help="Show scheduler config and counts")

    schedule_parser = sub.add_parser("schedule", help="Schedule a new action")
    schedule_parser.add_argument("--title", required=True)
    schedule_parser.add_argument("--action", required=True)
    schedule_parser.add_argument("--at", required=True, help="ISO datetime, e.g. 2026-05-29T08:30:00")
    schedule_parser.add_argument("--args", default="{}", help="JSON object of action arguments")
    schedule_parser.add_argument("--tags", default="")

    cancel_parser = sub.add_parser("cancel", help="Cancel a pending item")
    cancel_parser.add_argument("--id", type=int, required=True)

    delete_parser = sub.add_parser("delete", help="Delete an item")
    delete_parser.add_argument("--id", type=int, required=True)

    run_parser = sub.add_parser("run-due", help="Run any due items now")
    run_parser.add_argument("--horizon-minutes", type=int, default=0)

    args = parser.parse_args(argv)
    scheduler = build_scheduler(".", args.config)

    if args.command == "list":
        print(json.dumps(scheduler.list_items(), indent=2, default=str))
        return 0
    if args.command == "status":
        payload = {
            "config": scheduler.config.public_dict(),
            "items": scheduler.list_items(),
            "allowed_actions_runtime": sorted(scheduler.allowed_actions),
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if args.command == "schedule":
        try:
            arguments = json.loads(args.args or "{}")
        except json.JSONDecodeError as exc:
            print(json.dumps({"error": f"invalid --args JSON: {exc}"}))
            return 2
        if not isinstance(arguments, dict):
            print(json.dumps({"error": "--args must be a JSON object"}))
            return 2
        try:
            record = scheduler.schedule(
                title=args.title,
                action=args.action,
                scheduled_for=args.at,
                arguments=arguments,
                tags=[tag.strip() for tag in (args.tags or "").split(",") if tag.strip()],
            )
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}))
            return 2
        print(json.dumps(record, indent=2, default=str))
        return 0
    if args.command == "cancel":
        ok = scheduler.cancel(args.id)
        print(json.dumps({"cancelled": ok, "id": args.id}))
        return 0 if ok else 2
    if args.command == "delete":
        ok = scheduler.delete(args.id)
        print(json.dumps({"deleted": ok, "id": args.id}))
        return 0 if ok else 2
    if args.command == "run-due":
        result = scheduler.run_due(horizon_minutes=args.horizon_minutes)
        print(json.dumps(result, indent=2, default=str))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
