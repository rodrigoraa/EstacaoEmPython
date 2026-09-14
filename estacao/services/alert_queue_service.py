"""Fan-out compartilhado; a transação pertence ao chamador. Sem HTTP."""
import logging
from unsubscribe_tokens import telefone_com_codigo_pais
logger = logging.getLogger(__name__)

def enfileirar_alerta_usuario(
    conn,
    usuario,
    telefone,
    mensagem,
    evento_id=None,
    prioridade=50,
):
    conn.execute(
        """
        INSERT INTO alertas_fila (
            usuario_id,
            nome,
            telefone,
            mensagem,
            status,
            tentativas,
            evento_id,
            prioridade
        ) VALUES (?, ?, ?, ?, 'pendente', 0, ?, ?)
        """,
        (
            usuario["id"],
            usuario["nome"],
            telefone,
            mensagem,
            evento_id,
            prioridade,
        ),
    )


def enfileirar_alerta(conn, mensagem, evento=None, *, montar_mensagem_alerta,
                      enfileirar_alerta_usuario=enfileirar_alerta_usuario, atomico=True):
    enfileirados = falhas = 0
    evento_id = evento.get("evento_id") if evento else None
    if evento:
        cursor_evento = conn.execute(
            """
            INSERT OR IGNORE INTO alertas_eventos (
                evento_id, data_referencia, tipo, nivel, valor, unidade,
                ocorrido_em_local, fonte, mensagem
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evento_id,
                evento["data_referencia"],
                evento["tipo"],
                evento["nivel"],
                evento.get("valor"),
                evento.get("unidade"),
                evento.get("ocorrido_em_local"),
                evento.get("fonte"),
                mensagem,
            ),
        )
        if cursor_evento.rowcount == 0:
            return {
                "total": 0,
                "enfileirados": 0,
                "falhas": 0,
                "duplicado": True,
            }

    usuarios = conn.execute(
        """
        SELECT id, nome, telefone
        FROM usuarios
        WHERE (ativo = 1 OR ativo IS NULL)
        AND receber_whatsapp = 1
        AND (status_cadastro = 'ativo' OR status_cadastro IS NULL)
        ORDER BY id
        """
    ).fetchall()

    for usuario in usuarios:
        telefone = telefone_com_codigo_pais(usuario["telefone"])
        mensagem_final = montar_mensagem_alerta(usuario, mensagem)

        try:
            enfileirar_alerta_usuario(
                conn,
                usuario,
                telefone,
                mensagem_final,
                evento_id=evento_id,
                prioridade=evento.get("prioridade", 50) if evento else 50,
            )
            enfileirados += 1
        except Exception as e:
            if atomico:
                raise
            falhas += 1
            logger.warning("Falha ao enfileirar alerta (%s)", type(e).__name__)

    if evento:
        status = "enfileirado" if enfileirados else "sem_destinatarios"
        conn.execute(
            """
            UPDATE alertas_eventos
            SET status = ?, destinatarios = ?, enfileirados = ?,
                falhas = ?, atualizado_em = CURRENT_TIMESTAMP
            WHERE evento_id = ?
            """,
            (status, len(usuarios), enfileirados, falhas, evento_id),
        )
    return {"total": len(usuarios), "enfileirados": enfileirados, "falhas": falhas}
