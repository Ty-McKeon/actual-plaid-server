"""Gunicorn settings for the access log.

The access log records who requested what, but never the parts of a request that
act as credentials:

  - SimpleFIN claim IDs, which are part of the URL path, are redacted.
  - Query strings and the HTTP Basic Auth username are left out of the format.

Successful routing checks from the reverse proxy are skipped, because there is one
for every request made to Actual Budget.
"""

import re

from gunicorn.glogging import Logger

CLAIM_PATH = re.compile(r"(/simplefin/claim/)[^/?\s]+")
ROUTING_PATH = "/auth/route"


class RedactingLogger(Logger):
    """Access logger that hides claim IDs and skips routine routing checks."""

    def atoms(self, resp, req, environ, request_time):
        atoms = super().atoms(resp, req, environ, request_time)
        # "r" is the full request line and "U" the path on its own
        for key in ("r", "U"):
            if isinstance(atoms.get(key), str):
                atoms[key] = CLAIM_PATH.sub(r"\1[redacted]", atoms[key])
        return atoms

    def access(self, resp, req, environ, request_time):
        succeeded = str(resp.status).startswith("2")
        if environ.get("PATH_INFO") == ROUTING_PATH and succeeded:
            return
        super().access(resp, req, environ, request_time)


logger_class = RedactingLogger

# time, peer address, client address reported by Cloudflare, request, status, seconds
access_log_format = '%(t)s %(h)s %({cf-connecting-ip}i)s "%(m)s %(U)s" %(s)s %(L)s'
