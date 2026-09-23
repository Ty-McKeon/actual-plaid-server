from flask import Blueprint

simplefin_bp = Blueprint("simplefin", __name__, url_prefix="/simplefin")


@simplefin_bp.get("/token")
def get_setup_token():
    pass
