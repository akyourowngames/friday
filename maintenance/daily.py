from __future__ import annotations

import argparse
import json
import sys

from .engine import build_engine
from .steps import register_default_steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KING daily maintenance routine")
    parser.add_argument("--config", default=None, help="Path to maintenance markdown config")
    parser.add_argument("--dry-run", action="store_true", help="Plan without executing handlers")
    parser.add_argument("--force", action="store_true", help="Run even if already ran today")
    parser.add_argument("--status", action="store_true", help="Print status and exit")
    parser.add_argument("--triggered-by", default="cli", help="Source label for the run")
    args = parser.parse_args(argv)

    engine = build_engine(".", args.config)
    register_default_steps(engine)

    if args.status:
        print(json.dumps(engine.status(), indent=2, sort_keys=True, default=str))
        return 0

    result = engine.run(triggered_by=args.triggered_by, dry_run=args.dry_run, force=args.force)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    if result.status in ("ok", "dry_run", "skipped"):
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
