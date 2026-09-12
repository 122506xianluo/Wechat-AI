"""Authentication routes. Loopback/origin/CSRF enforced centrally by app."""

from flask import Blueprint, g, jsonify, request, render_template, make_response
from auth import Auth


def register_auth(app, storage, error):
    api = Blueprint("auth", __name__)

    @api.errorhandler(ValueError)
    def invalid(exc):
        return error(exc)

    @api.get("/login")
    def login_page():
        return render_template(
            "login.html",
            csrf_token=app.config["LOCAL_CSRF_TOKEN"],
            initialized=Auth(storage()).initialized(),
        )

    @api.get("/api/v1/auth/status")
    def status():
        s = Auth(storage()).session(request.cookies.get("wechat_ai_session"))
        return jsonify(
            ok=True,
            initialized=Auth(storage()).initialized(),
            authenticated=bool(s),
            level=s[0].access_level if s else None,
        )

    @api.post("/api/v1/auth/init")
    def init():
        d = request.get_json()
        recovery = Auth(storage()).create(d.get("username"), d.get("password"))
        return jsonify(ok=True, recovery_code=recovery)

    @api.post("/api/v1/auth/login")
    def login():
        d = request.get_json()
        token, csrf = Auth(storage()).login(
            d.get("username"), d.get("password"), request.remote_addr
        )
        r = make_response(jsonify(ok=True, csrf_token=csrf))
        r.set_cookie(
            "wechat_ai_session",
            token,
            max_age=43200,
            httponly=True,
            samesite="Strict",
            secure=False,
        )
        return r

    @api.post("/api/v1/auth/logout")
    def logout():
        Auth(storage()).logout(request.cookies.get("wechat_ai_session"))
        r = make_response(jsonify(ok=True))
        r.delete_cookie("wechat_ai_session", httponly=True, samesite="Strict")
        return r

    @api.post("/api/v1/auth/password")
    def password():
        d = request.get_json()
        Auth(storage()).change_password(
            g.account_id, d.get("current"), d.get("password"), g.actor
        )
        return jsonify(ok=True)

    @api.post("/api/v1/auth/recover")
    def recover():
        d = request.get_json()
        code = Auth(storage()).recover_password(
            d.get("username"), d.get("code"), d.get("password"), request.remote_addr
        )
        return jsonify(ok=True, recovery_code=code)

    @api.post("/api/v1/auth/accounts")
    def create_admin():
        d = request.get_json()
        Auth(storage()).create(d.get("username"), d.get("password"), g.actor)
        return jsonify(ok=True)

    app.register_blueprint(api)
