#!/usr/bin/env python3
"""Assigns Actual Budget containers to users by name.

Every user is served by their own Actual Budget container, `actual_<name>`, with
their data in `actual-data/users/<name>`. This script maintains
`config/actual-users.json` (email -> name), which the bridge reads to route
requests. Changes take effect immediately; nothing needs restarting.

With ACTUAL_SELF_SERVICE on, users who are not listed still get a container with
a generated name. List someone here to give them a readable name, to let several
email addresses share one container, or, with self-service off, to let them in.

Usage:
  scripts/actual-users.py add NAME EMAIL    assign NAME to EMAIL
  scripts/actual-users.py remove NAME       remove NAME (their data is kept)
  scripts/actual-users.py list              show the current assignments

The email must also be allowed by the Cloudflare Access policy for the Actual hostname.
"""

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USERS_FILE = ROOT / "config" / "actual-users.json"

# Must match NAME_PATTERN in bridge/services/routing_service.py
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")


def fail(message: str) -> None:
    sys.exit(f"error: {message}")


def load_users() -> dict[str, str]:
    if not USERS_FILE.exists():
        return {}
    users = json.loads(USERS_FILE.read_text())
    if not isinstance(users, dict):
        fail(f"{USERS_FILE} does not contain a JSON object")
    return users


def save(users: dict[str, str]) -> None:
    # Replace the file in one step so the bridge never reads a half-written one
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = USERS_FILE.with_name(USERS_FILE.name + ".tmp")
    temporary.write_text(json.dumps(users, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, USERS_FILE)


def add(name: str, email: str) -> None:
    email = email.strip().lower()
    if not NAME_PATTERN.fullmatch(name):
        fail("NAME must be lowercase letters, digits and hyphens (max 31 characters)")
    if "@" not in email:
        fail("EMAIL does not look like an email address")

    users = load_users()
    if users.get(email, name) != name:
        fail(f"{email} is already assigned to {users[email]}")

    users[email] = name
    save(users)
    print(f"{email} -> actual_{name}")
    print("Also allow this email in the Cloudflare Access policy for Actual.")


def remove(name: str) -> None:
    users = load_users()
    remaining = {email: n for email, n in users.items() if n != name}
    if len(remaining) == len(users):
        fail(f"no user named {name}")

    save(remaining)
    print(f"Removed {name}. Their data is kept in actual-data/users/{name}.")


def show() -> None:
    users = load_users()
    if not users:
        print("No users assigned.")
    for email, name in sorted(users.items(), key=lambda item: (item[1], item[0])):
        print(f"actual_{name}\t{email}")


def main(argv: list[str]) -> None:
    command, args = (argv[0], argv[1:]) if argv else (None, [])

    if command == "add" and len(args) == 2:
        add(*args)
    elif command == "remove" and len(args) == 1:
        remove(*args)
    elif command == "list" and not args:
        show()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
