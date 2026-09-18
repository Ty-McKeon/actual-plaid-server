from flask import Blueprint
import jwt

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

def require_cloudflare_auth():
    pass

# TODO: Implement
def get_user_email():
    return "user@example.com"

# TODO: Implement 
def get_user_sub():
    return "7335d417-61da-459d-899c-0a01c76a2f94"