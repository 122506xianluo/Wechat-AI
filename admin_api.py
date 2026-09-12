"""Versioned management endpoints; all requests pass the application's auth guard."""
from flask import Blueprint, g, jsonify, request, render_template
from roles import Roles


def register_admin(app, get_storage, error):
    api = Blueprint('management', __name__)

    def payload():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError('请求必须为 JSON 对象')
        return value

    @api.errorhandler(ValueError)
    def invalid(exc):
        return error(exc)

    @api.get('/manage')
    def manage():
        return render_template('manage.html', csrf_token=getattr(g, 'csrf', app.config['LOCAL_CSRF_TOKEN']))

    @api.get('/api/v1/roles')
    def roles():
        return jsonify(ok=True, items=Roles(get_storage()).list())

    @api.post('/api/v1/roles')
    @api.post('/api/v1/roles/<int:role_id>')
    def role_save(role_id=None):
        return jsonify(ok=True, id=Roles(get_storage()).save(payload(), g.actor, role_id))

    @api.get('/api/v1/roles/<int:role_id>/revisions')
    def role_revisions(role_id):
        return jsonify(ok=True, items=Roles(get_storage()).revisions(role_id))

    @api.post('/api/v1/roles/<int:role_id>/rollback')
    def role_rollback(role_id):
        return jsonify(ok=True, id=Roles(get_storage()).rollback(role_id, int(payload()['revision']), g.actor))

    @api.post('/api/v1/roles/<int:role_id>/delete')
    def role_delete(role_id):
        Roles(get_storage()).delete(role_id, g.actor, payload().get('replacement'))
        return jsonify(ok=True)

    @api.post('/api/v1/roles/<int:role_id>/bind')
    def role_bind(role_id):
        data=payload()
        Roles(get_storage()).bind(role_id, data.get('chat_id'), data.get('principal_id'), g.actor)
        return jsonify(ok=True)

    app.register_blueprint(api)
