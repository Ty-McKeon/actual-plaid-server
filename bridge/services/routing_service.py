"""Maps authenticated users to their own Actual Budget container.

Each user is served by a container called `actual_<name>` on the internal Docker
network. The name comes from one of two places:

  1. `config/actual-users.json`, a JSON object of `{"email": "name"}` pairs
     maintained by `scripts/actual-users.py`.
  2. With self-service enabled, any other verified user gets a name derived from
     their email address, so nothing they type ever reaches a container name.

When a provisioner is configured it is asked to create and start that container
on demand; it also stops containers nobody has used for a while.
"""

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

DEFAULT_USERS_FILE = "/config/actual-users.json"

# The Compose profile that starts the provisioner, and where it then listens
PROVISIONER_PROFILE = "provisioner"
DEFAULT_PROVISIONER_URL = "http://provisioner:8090"
CONTAINER_PREFIX = "actual_"

# Names become part of a hostname the proxy connects to, so keep them strict
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")

# Creating a container, and pulling its image the first time, can take a while
PROVISIONER_TIMEOUT_SECONDS = 60

# The provisioner is told about a user at most this often. It doubles as the
# heartbeat that keeps an active user's container from being stopped as idle.
ENSURE_INTERVAL_SECONDS = 15

# Parsed mapping, reused until the file's modification time changes
_cache: dict = {"path": None, "mtime": None, "users": {}}

# When each name was last confirmed running by the provisioner
_last_ensured: dict[str, float] = {}


class ActualUnavailable(Exception):
    """The user's Actual Budget container could not be made available."""


def _load_users() -> dict[str, str]:
    """Returns the email to name mapping, or an empty one if it cannot be read."""
    path = Path(os.getenv("ACTUAL_USERS_FILE", DEFAULT_USERS_FILE))

    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}

    if _cache["path"] == path and _cache["mtime"] == mtime:
        return _cache["users"]

    users: dict[str, str] = {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        logger.exception("Could not read the Actual users file at %s.", path)
        raw = {}

    if isinstance(raw, dict):
        for email, name in raw.items():
            if isinstance(name, str) and NAME_PATTERN.fullmatch(name):
                users[email.strip().lower()] = name
            else:
                logger.warning("Ignoring invalid Actual user entry for %s.", email)

    _cache.update(path=path, mtime=mtime, users=users)
    return users


def provisioner_url() -> str:
    """Returns the provisioner's address, or an empty string when it is not in use.

    The provisioner only runs when the Compose profile of the same name is switched
    on. Without it, each user's container is defined in the Compose configuration
    and is always running, so there is nothing to start on demand.
    """
    explicit = os.getenv("ACTUAL_PROVISIONER_URL", "").rstrip("/")
    if explicit:
        return explicit

    profiles = {p.strip() for p in os.getenv("COMPOSE_PROFILES", "").split(",")}
    return DEFAULT_PROVISIONER_URL if PROVISIONER_PROFILE in profiles else ""


def self_service_enabled() -> bool:
    """Whether verified users without an assigned name get a container anyway.

    Only the provisioner can create a container for someone who was not set up in
    advance, so self-service is off whenever the provisioner is.
    """
    requested = os.getenv("ACTUAL_SELF_SERVICE", "").lower() in ("1", "true", "yes")
    return requested and bool(provisioner_url())


def resolve_actual_name(email: str | None) -> str | None:
    """Returns the name of the user's Actual container, or None if they have none."""
    if not email:
        return None

    email = email.strip().lower()
    name = _load_users().get(email)
    if name:
        return name

    if self_service_enabled():
        return "u-" + hashlib.sha256(email.encode()).hexdigest()[:16]

    return None


def _ensure_running(name: str) -> None:
    """Asks the provisioner to create and start the container if there is one."""
    base_url = provisioner_url()
    if not base_url:
        return

    now = time.monotonic()
    if (
        now - _last_ensured.get(name, -ENSURE_INTERVAL_SECONDS)
        < ENSURE_INTERVAL_SECONDS
    ):
        return

    try:
        response = requests.post(
            f"{base_url}/containers/{name}/ensure", timeout=PROVISIONER_TIMEOUT_SECONDS
        )
    except requests.RequestException as err:
        logger.error("Could not reach the Actual provisioner: %s", err)
        raise ActualUnavailable("Actual Budget is temporarily unavailable.") from err

    if response.status_code == 429:
        raise ActualUnavailable(
            "The maximum number of Actual Budget servers is already running."
        )
    if not response.ok:
        logger.error("The Actual provisioner answered %s.", response.status_code)
        raise ActualUnavailable("Your Actual Budget server could not be started.")

    _last_ensured[name] = now


def get_actual_upstream(email: str | None) -> str | None:
    """Returns the hostname of the user's running Actual container.

    Returns None if the user has no container.

    Raises:
        ActualUnavailable: If the container exists in principle but could not be
            started.
    """
    name = resolve_actual_name(email)
    if not name:
        return None

    _ensure_running(name)
    return CONTAINER_PREFIX + name
