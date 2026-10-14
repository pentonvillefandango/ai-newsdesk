"""The `ai-newsdesk` command line."""

from __future__ import annotations

import argparse
import getpass
import sys

from ai_newsdesk import cli_db
from ai_newsdesk.web.auth import hash_password

MIN_PASSWORD_LENGTH = 12


def _too_short(password: str) -> bool:
    if len(password) < MIN_PASSWORD_LENGTH:
        print(
            f"Use at least {MIN_PASSWORD_LENGTH} characters. Nothing was changed.", file=sys.stderr
        )
        return True
    return False


def cmd_hash_password(args: argparse.Namespace) -> int:
    """Print the argon2id hash for OWNER_PASSWORD_HASH.

    On a terminal: prompt twice without echo. Otherwise (ops/set_password.sh pipes it in, never
    via argv): read exactly one line from stdin. Only the hash ever reaches stdout.
    """
    if not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\r\n")
        if _too_short(password):
            return 2
    else:
        password = getpass.getpass("New owner password: ")
        if _too_short(password):
            return 2
        if getpass.getpass("Type it again: ") != password:
            print("The two passwords don't match. Nothing was changed.", file=sys.stderr)
            return 2
    print(hash_password(password))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-newsdesk")
    sub = parser.add_subparsers(dest="command", required=True)
    hash_cmd = sub.add_parser(
        "hash-password",
        help=(
            "Read the owner password (twice on a terminal, one stdin line otherwise) "
            "and print its argon2 hash."
        ),
    )
    hash_cmd.set_defaults(func=cmd_hash_password)
    cli_db.register(sub)
    args = parser.parse_args(argv)
    return int(args.func(args))


def run() -> None:
    """Console-script entry point (`ai-newsdesk`)."""
    sys.exit(main())


if __name__ == "__main__":
    run()
