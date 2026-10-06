"""Tells the reverse proxy which Actual Budget container a request belongs to.

Every user has their own Actual Budget container behind a single public hostname.
Caddy calls this endpoint (`forward_auth`) with the headers of each incoming request,
then proxies the request to the container named in the `X-Actual-Upstream` response
header. Any non-2xx response is returned to the browser instead.

Looking up the container also starts it if it is not running yet.
"""

import logging
import os

from flask import Blueprint, Response, g, jsonify
from middleware import authenticate_request
from services import ActualUnavailable, get_actual_upstream

# Blueprint definition: Registered in app.py with url_prefix="/auth"
routing_bp = Blueprint("routing", __name__)

logger = logging.getLogger(__name__)

UPSTREAM_HEADER = "X-Actual-Upstream"


@routing_bp.get("/route")
def route_actual_user():
    """Verifies the Cloudflare Access session and names the user's Actual container."""
    # Actual sits behind its own Access application unless it shares the dashboard's
    payload, err_response = authenticate_request(
        audience=os.getenv("CLOUDFLARE_ACTUAL_AUD") or None
    )
    if err_response:
        return err_response
    # Service-token common names are not verified human email addresses.
    if not payload.get("email"):
        return jsonify(
            {"error": "Forbidden", "message": "A user session is required."}
        ), 403

    try:
        upstream = get_actual_upstream(g.email)
    except ActualUnavailable as err:
        return jsonify({"error": "Service Unavailable", "message": str(err)}), 503

    if not upstream:
        logger.warning("No Actual Budget server is assigned to %s.", g.email)
        return (
            jsonify(
                {
                    "error": "Forbidden",
                    "message": "No Actual Budget server is assigned to this account.",
                }
            ),
            403,
        )

    response = Response(status=204)
    response.headers[UPSTREAM_HEADER] = upstream
    return response
