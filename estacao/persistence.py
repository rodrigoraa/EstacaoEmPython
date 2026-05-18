import json
import sqlite3
import time

import database
from time_utils import (
    de_timestamp_ms_utc,
    iso_local,
    iso_utc,
    sqlite_local,
)


_SCHEMA_GARANTIDO = False


def garantir_schema():
    global _SCHEMA_GARANTIDO

    if not _SCHEMA_GARANTIDO:
        database.init_db()
        _SCHEMA_GARANTIDO = True


def agora_iso():
    return sqlite_local()


def extrair_primeiro(raw, nomes):
    if not isinstance(raw, dict):
        return None

    for nome in nomes:
        if nome in raw and raw[nome] is not None:
            return raw[nome]
    return None


def extrair_bateria(raw):
    if not isinstance(raw, dict):
        return None

    campos = {}
    for chave, valor in raw.items():
        chave_lower = str(chave).lower()
        if "batt" in chave_lower or "battery" in chave_lower:
            campos[chave] = valor

    if not campos:
        return None
    return json.dumps(campos, ensure_ascii=False, sort_keys=True)


def extrair_sinal(raw):
    valor = extrair_primeiro(raw, ("signal", "rssi", "signalrssi", "signal_strength"))
    if valor is None:
        return None
    return str(valor)


def dados_tempo_estacao(station_timestamp_ms):
    dt_estacao_utc = de_timestamp_ms_utc(station_timestamp_ms)
    if not dt_estacao_utc:
        return None, None
    return iso_utc(dt_estacao_utc), iso_local(dt_estacao_utc)


def executar_com_retry(operacao, tentativas=3, espera_inicial=0.2):
    ultimo_erro = None

    for tentativa in range(tentativas):
        try:
            return operacao()
        except sqlite3.OperationalError as erro:
            ultimo_erro = erro
            if tentativa == tentativas - 1:
                break
            time.sleep(espera_inicial * (tentativa + 1))

    raise ultimo_erro


def registrar_log_persistencia(nivel, origem, mensagem, detalhe=None):
    try:
        conn = database.get_db()
        try:
            conn.execute(
                """
                INSERT INTO logs_persistencia (nivel, origem, mensagem, detalhe)
                VALUES (?, ?, ?, ?)
                """,
                (nivel, origem, mensagem, detalhe),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as erro:
        print(f"[persistencia] falha ao registrar log: {erro}", flush=True)


def salvar_leitura_bruta(raw, dados_convertidos=None, origem="ambientweather"):
    garantir_schema()

    recebido_em = agora_iso()
    station_timestamp_ms = raw.get("dateutc") if isinstance(raw, dict) else None
    station_data_hora_utc, station_data_hora_local = dados_tempo_estacao(
        station_timestamp_ms
    )
    recebido_em_utc = iso_utc()
    recebido_em_local = iso_local()
    payload_json = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    dados_json = (
        json.dumps(dados_convertidos, ensure_ascii=False, sort_keys=True)
        if dados_convertidos
        else None
    )
    bateria = extrair_bateria(raw)
    sinal = extrair_sinal(raw)

    def operacao():
        conn = database.get_db()
        try:
            # Persistencia critica: todo dado recebido da estacao entra no banco
            # antes de qualquer alerta, cache, resumo diario ou processamento lento.
            cursor = conn.execute(
                """
                INSERT INTO leituras_brutas (
                    origem,
                    station_timestamp_ms,
                    station_data_hora_utc,
                    station_data_hora_local,
                    recebido_em,
                    recebido_em_utc,
                    recebido_em_local,
                    payload_json,
                    dados_convertidos_json,
                    chuva_rate,
                    chuva_evento,
                    chuva_hoje,
                    bateria,
                    sinal
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    origem,
                    station_timestamp_ms,
                    station_data_hora_utc,
                    station_data_hora_local,
                    recebido_em,
                    recebido_em_utc,
                    recebido_em_local,
                    payload_json,
                    dados_json,
                    (dados_convertidos or {}).get("chuva_rate"),
                    (dados_convertidos or {}).get("chuva_evento"),
                    (dados_convertidos or {}).get("chuva_hoje"),
                    bateria,
                    sinal,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    try:
        return executar_com_retry(operacao)
    except Exception as erro:
        registrar_log_persistencia(
            "ERRO",
            origem,
            "Falha ao persistir leitura bruta recebida da estacao",
            str(erro),
        )
        raise


def normalizar_dados_historico(dados):
    if isinstance(dados, dict):
        return (
            dados["temp"],
            dados["sensacao"],
            dados["umidade"],
            dados["pressao"],
            dados["uv"],
            dados["radiacao"],
            dados["vento"],
            dados["rajada"],
            dados["vento_dir"],
            dados["chuva_rate"],
            dados["chuva_evento"],
            dados["chuva_hoje"],
        )

    return tuple(dados)


def salvar_historico_clima(dados, leitura_bruta_id=None):
    garantir_schema()
    valores = normalizar_dados_historico(dados)
    data_hora = agora_iso()
    data_hora_utc = iso_utc()
    data_hora_local = iso_local()
    station_timestamp_ms = dados.get("station_timestamp_ms") if isinstance(dados, dict) else None
    station_data_hora_utc = dados.get("station_data_hora_utc") if isinstance(dados, dict) else None
    station_data_hora_local = dados.get("station_data_hora_local") if isinstance(dados, dict) else None
    bateria = dados.get("bateria") if isinstance(dados, dict) else None
    sinal = dados.get("sinal") if isinstance(dados, dict) else None

    def operacao():
        conn = database.get_db()
        try:
            cursor = conn.execute(
                """
                INSERT INTO historico_clima (
                    temp,
                    sensacao,
                    umidade,
                    pressao,
                    uv,
                    radiacao,
                    vento_vel,
                    vento_rajada,
                    vento_dir,
                    chuva_rate,
                    chuva_evento,
                    chuva_hoje,
                    station_timestamp_ms,
                    station_data_hora_utc,
                    station_data_hora_local,
                    data_hora_utc,
                    data_hora_local,
                    bateria,
                    sinal,
                    leitura_bruta_id,
                    data_hora
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *valores,
                    station_timestamp_ms,
                    station_data_hora_utc,
                    station_data_hora_local,
                    data_hora_utc,
                    data_hora_local,
                    bateria,
                    sinal,
                    leitura_bruta_id,
                    data_hora,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    try:
        return executar_com_retry(operacao)
    except Exception as erro:
        registrar_log_persistencia(
            "ERRO",
            "historico_clima",
            "Falha ao persistir leitura processada no historico",
            str(erro),
        )
        raise
