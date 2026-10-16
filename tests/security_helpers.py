"""Test helpers that issue the same server-side sessions used by real logins."""


def autenticar_admin(client, csrf_token="csrf-teste"):
    from admin_auth import iniciar_sessao_admin
    from flask import session

    with client.application.test_request_context():
        iniciar_sessao_admin()
        valores = dict(session)
    with client.session_transaction() as sessao:
        sessao.clear()
        sessao.update(valores)
        sessao["csrf_token"] = csrf_token
    return valores["admin_session"]
