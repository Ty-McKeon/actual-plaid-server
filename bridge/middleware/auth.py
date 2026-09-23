from flask import request, jsonify
from functools import wraps
import jwt


def require_cloudflare_auth(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        pass

    return wrapper
