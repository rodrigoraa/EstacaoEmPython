"""Contabilidade e substituição de alertas dentro da transação do chamador."""


def atualizar_evento(conn, evento_id):
    if not evento_id:
        return
    totais = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'enviado' THEN 1 ELSE 0 END) AS enviados,
            SUM(CASE WHEN status = 'falhou' THEN 1 ELSE 0 END) AS falhas,
            SUM(CASE WHEN status IN ('pendente', 'enviando') THEN 1 ELSE 0 END) AS abertos,
            SUM(CASE WHEN status = 'substituido' THEN 1 ELSE 0 END) AS substituidos
        FROM alertas_fila
        WHERE evento_id = ?
        """,
        (evento_id,),
    ).fetchone()
    enviados = int(totais["enviados"] or 0)
    evento = conn.execute(
        "SELECT destinatarios FROM alertas_eventos WHERE evento_id = ?", (evento_id,)
    ).fetchone()
    faltantes = max(0, int(evento["destinatarios"] or 0) - totais["total"]) if evento else 0
    falhas = int(totais["falhas"] or 0) + faltantes
    abertos = int(totais["abertos"] or 0)
    substituidos = int(totais["substituidos"] or 0)
    if abertos:
        status = "processando"
    elif falhas:
        status = "concluido_com_falhas"
    elif substituidos and not enviados:
        status = "substituido"
    else:
        status = "concluido"
    conn.execute(
        """
        UPDATE alertas_eventos
        SET enviados = ?, falhas = ?, substituidos = ?, status = ?,
            atualizado_em = CURRENT_TIMESTAMP
        WHERE evento_id = ?
        """,
        (enviados, falhas, substituidos, status, evento_id),
    )


def _substituir_campanha_superada(conn, condicao, parametros):
    # A condição é SQL fixo fornecido somente pelas duas funções abaixo.
    # A escalada encerra a campanha inferior inteira, inclusive para quem não
    # entrou na nova fila. Ciclos separam episódios após recuperação. Condições
    # instantâneas continuam após meia-noite; chuva e vento têm controle diário.
    linhas = conn.execute(
        f"""
        SELECT DISTINCT menor.id, menor.evento_id
        FROM alertas_fila menor
        JOIN alertas_eventos evento_menor ON evento_menor.evento_id = menor.evento_id
        JOIN alertas_eventos evento_maior
          ON evento_maior.tipo = evento_menor.tipo
         AND evento_maior.ciclo = evento_menor.ciclo
         AND (evento_menor.tipo IN ('calor', 'frio', 'umidade')
              OR evento_maior.data_referencia = evento_menor.data_referencia)
         AND evento_maior.nivel > evento_menor.nivel
        WHERE evento_menor.tipo IN ('chuva', 'vento', 'calor', 'frio', 'umidade')
          AND {condicao}
        """,
        parametros,
    ).fetchall()
    if not linhas:
        return False
    conn.executemany(
        """
        UPDATE alertas_fila
        SET status = 'substituido', proxima_tentativa_em = NULL,
            atualizado_em = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        [(linha["id"],) for linha in linhas],
    )
    for evento_id in {linha["evento_id"] for linha in linhas}:
        atualizar_evento(conn, evento_id)
    return True


def substituir_alertas_pendentes(conn, evento_id_superior):
    """Encerrar a campanha inferior na mesma transação que cria o evento superior."""
    return _substituir_campanha_superada(
        conn,
        "menor.status = 'pendente' AND evento_maior.evento_id = ?",
        (evento_id_superior,),
    )


def substituir_envio_superado(conn, item_id):
    """Dentro do claim, substituir só o candidato já elegível, inclusive retry.

    Não recupera um nível inferior mesmo se o enqueue ou envio superior falhou:
    a campanha inferior já foi encerrada. Não há nova tentativa ou envio.
    """
    return _substituir_campanha_superada(conn, "menor.id = ?", (item_id,))
