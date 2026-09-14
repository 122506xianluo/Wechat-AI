"""Compatibility routes for the passwordless, loopback-only console.

Existing account records remain in SQLite for backup compatibility, but confer
no privileges. The application's central local/CSRF guard protects every route.
"""
from flask import Blueprint, g, jsonify, redirect


def register_auth(app, storage, error):
    api = Blueprint("auth", __name__)

    @api.get("/login")
    def login_page():
        return redirect("/")

    @api.get("/api/v1/auth/status")
    def status():
        return jsonify(ok=True, initialized=True, authenticated=True,
                       local_mode=True, level="local", csrf_token=g.csrf)

    @api.post("/api/v1/auth/logout")
    def logout():
        response = jsonify(ok=True, local_mode=True)
        response.delete_cookie("wechat_ai_session", httponly=True, samesite="Strict")
        return response

    @api.post("/api/v1/auth/init")
    @api.post("/api/v1/auth/login")
    @api.post("/api/v1/auth/password")
    @api.post("/api/v1/auth/recover")
    @api.post("/api/v1/auth/accounts")
    def retired():
        return jsonify(ok=False, error="本地控制台已免登录，请刷新页面直接使用。"), 410

    app.register_blueprint(api)
