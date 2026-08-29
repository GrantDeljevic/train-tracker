from __future__ import annotations

import argparse
import json
import logging

from . import init_service


def main() -> None:
    parser = argparse.ArgumentParser(description="Charlotte freight-train warning service")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="resolve FRA metadata and choose sentinels")
    init_parser.add_argument("--live", action="store_true", help="also validate TomTom flow geometry")
    args = parser.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "init":
        print(json.dumps(init_service.initialize(live=args.live), indent=2, default=str))


if __name__ == "__main__":
    main()
