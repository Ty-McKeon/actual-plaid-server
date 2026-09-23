from functools import wraps
from flask import request, jsonify


def require_json(func):
    """
    Validates that the incoming request has a valid JSON body
    that parses into a Python dictionary.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        # 1. Check Content-Type header
        if not request.is_json:
            return (
                jsonify(
                    {
                        "error": "Unsupported Media Type",
                        "message": "Request Content-Type must be 'application/json'",
                    }
                ),
                415,
            )

        # 2. Safely parse JSON payload (silent=True returns None instead of raising BadRequest)
        data = request.get_json(silent=True)

        # 3. Ensure parsing succeeded and the root structure is a dictionary
        if data is None or not isinstance(data, dict):
            return (
                jsonify(
                    {
                        "error": "Bad Request",
                        "message": "Request body must be a valid non-empty JSON object",
                    }
                ),
                400,
            )

        return func(*args, **kwargs)

    return wrapper
