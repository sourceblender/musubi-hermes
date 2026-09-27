"""Operator status command discovered as ``hermes musubi status``."""

import json

from hermes_constants import get_hermes_home

from .musubi import Outbox


def _run(args) -> None:
    if args.musubi_command != "status":
        return
    path = get_hermes_home() / "musubi-outbox.db"
    if not path.exists():
        print(json.dumps({"state": "not-initialized", "outbox": str(path)}))
        return
    print(json.dumps({"state": "ready", "outbox": str(path), **Outbox(path).health()}, sort_keys=True))


def register_cli(subparser) -> None:
    commands = subparser.add_subparsers(dest="musubi_command")
    commands.add_parser("status", help="Show durable Musubi outbox health")
    subparser.set_defaults(func=_run)
