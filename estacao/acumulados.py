import json

import database
from time_utils import data_local


DATA_HISTORICO_EXPR = "COALESCE(substr(data_hora_local, 1, 10), date(data_hora))"
DATA_BRUTA_EXPR = (
    "COALESCE(substr(station_data_hora_local, 1, 10), "
    "substr(recebido_em_local, 1, 10), date(recebido_em))"
)


def valor_float(valor, padrao=0.0):
    if valor is None:
        return padrao
    try:
        return float(valor)
    except (TypeError, ValueError):
        return padrao


def calcular_chuva_corrigida(valores):
    total = 0.0
    ultima = None
    resets = 0

    for valor in valores:
        if valor is None:
            continue

        leitura = max(valor_float(valor), 0.0)
        if ultima is None:
            total = leitura
        elif leitura >= ultima:
            total += leitura - ultima
        else:
            resets += 1
            total += leitura

        ultima = leitura

    return {
        "chuva_total_corrigida": round(total, 1),
        "chuva_ultima_leitura": round(ultima, 1) if ultima is not None else None,
        "chuva_reset_count": resets,
    }


def maior_rajada_convertida_json(conn, data):
    maior = 0.0
    rows = conn.execute(
        f"""
        SELECT dados_convertidos_json
        FROM leituras_brutas
        WHERE {DATA_BRUTA_EXPR} = ?
        ORDER BY id
        """,
        (data,),
    ).fetchall()

    for row in rows:
        if not row["dados_convertidos_json"]:
            continue
        try:
            dados = json.loads(row["dados_convertidos_json"])
        except (TypeError, ValueError):
            continue

        maior = max(
            maior,
            valor_float(dados.get("rajada")),
        )

    return maior


def calcular_acumulado_pelo_historico(conn, data):
    database.garantir_tabela_acumulados_diarios(conn)

    rows = conn.execute(
        f"""
        SELECT chuva_hoje, vento_rajada
        FROM historico_clima
        WHERE {DATA_HISTORICO_EXPR} = ?
        ORDER BY id
        """,
        (data,),
    ).fetchall()

    chuva = calcular_chuva_corrigida(row["chuva_hoje"] for row in rows)
    rajada_historico = max(
        (valor_float(row["vento_rajada"]) for row in rows),
        default=0.0,
    )
    rajada_bruta = maior_rajada_convertida_json(conn, data)

    return {
        "data": data,
        "chuva_total_corrigida": chuva["chuva_total_corrigida"],
        "chuva_ultima_leitura": chuva["chuva_ultima_leitura"],
        "chuva_reset_count": chuva["chuva_reset_count"],
        "rajada_max_corrigida": round(max(rajada_historico, rajada_bruta), 1),
    }


def linha_para_dict(row):
    if not row:
        return None
    return {
        "data": row["data"],
        "chuva_total_corrigida": valor_float(row["chuva_total_corrigida"]),
        "chuva_ultima_leitura": (
            valor_float(row["chuva_ultima_leitura"])
            if row["chuva_ultima_leitura"] is not None
            else None
        ),
        "chuva_reset_count": int(row["chuva_reset_count"] or 0),
        "rajada_max_corrigida": valor_float(row["rajada_max_corrigida"]),
    }


def salvar_acumulado(conn, acumulado):
    conn.execute(
        """
        INSERT OR REPLACE INTO acumulados_diarios (
            data,
            chuva_total_corrigida,
            chuva_ultima_leitura,
            chuva_reset_count,
            rajada_max_corrigida,
            atualizado_em
        ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            acumulado["data"],
            acumulado["chuva_total_corrigida"],
            acumulado["chuva_ultima_leitura"],
            acumulado["chuva_reset_count"],
            acumulado["rajada_max_corrigida"],
        ),
    )


def atualizar_acumulado_diario(dados, data=None):
    data = data or data_local()
    chuva_leitura = valor_float(dados.get("chuva_hoje")) if dados else 0.0
    rajada_atual = valor_float(dados.get("rajada")) if dados else 0.0
    rajada_max_nuvem = valor_float(dados.get("rajada_max")) if dados else 0.0

    conn = database.get_db()
    try:
        database.garantir_tabela_acumulados_diarios(conn)
        calculado = calcular_acumulado_pelo_historico(conn, data)
        existente = linha_para_dict(
            conn.execute(
                "SELECT * FROM acumulados_diarios WHERE data = ?",
                (data,),
            ).fetchone()
        )

        acumulado = {
            "data": data,
            "chuva_total_corrigida": calculado["chuva_total_corrigida"],
            "chuva_ultima_leitura": chuva_leitura,
            "chuva_reset_count": calculado["chuva_reset_count"],
            "rajada_max_corrigida": max(
                calculado["rajada_max_corrigida"],
                rajada_atual,
                rajada_max_nuvem,
            ),
        }

        if existente:
            acumulado["chuva_total_corrigida"] = max(
                acumulado["chuva_total_corrigida"],
                existente["chuva_total_corrigida"],
            )
            acumulado["chuva_reset_count"] = max(
                acumulado["chuva_reset_count"],
                existente["chuva_reset_count"],
            )
            acumulado["rajada_max_corrigida"] = max(
                acumulado["rajada_max_corrigida"],
                existente["rajada_max_corrigida"],
            )

        acumulado["chuva_total_corrigida"] = round(
            acumulado["chuva_total_corrigida"], 1
        )
        acumulado["rajada_max_corrigida"] = round(
            acumulado["rajada_max_corrigida"], 1
        )
        acumulado["chuva_ultima_leitura"] = round(
            acumulado["chuva_ultima_leitura"], 1
        )

        salvar_acumulado(conn, acumulado)
        conn.commit()
        return acumulado
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def obter_acumulado_diario(data=None):
    data = data or data_local()
    conn = database.get_db()
    try:
        database.garantir_tabela_acumulados_diarios(conn)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM acumulados_diarios WHERE data = ?",
            (data,),
        ).fetchone()
        if row:
            return linha_para_dict(row)
        return calcular_acumulado_pelo_historico(conn, data)
    finally:
        conn.close()


def serie_chuva_corrigida_por_hora(data=None):
    data = data or data_local()
    conn = database.get_db()
    try:
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(substr(data_hora_local, 12, 2), strftime('%H', data_hora)) || ':00' as hora,
                chuva_hoje
            FROM historico_clima
            WHERE {DATA_HISTORICO_EXPR} = ?
            ORDER BY id
            """,
            (data,),
        ).fetchall()
    finally:
        conn.close()

    total = 0.0
    ultima = None
    serie = {}

    for row in rows:
        leitura = max(valor_float(row["chuva_hoje"]), 0.0)
        if ultima is None:
            total = leitura
        elif leitura >= ultima:
            total += leitura - ultima
        else:
            total += leitura

        ultima = leitura
        serie[row["hora"]] = round(total, 1)

    return serie
