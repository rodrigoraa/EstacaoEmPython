import os
import json
import logging
import sqlite3

from config import env_str

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.environ.get("ESTACAO_DB", os.path.join(BASE_DIR, "estacao.db"))
ALERT_STATE_KEY = "principal"
SCHEMA_VERSION = 2
logger = logging.getLogger(__name__)


def configurar_conexao(conn):
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = FULL;")
    return conn


def get_db():

    conn = sqlite3.connect(DATABASE, timeout=30)

    conn.row_factory = sqlite3.Row
    configurar_conexao(conn)

    return conn


def coluna_existe(conn, tabela, coluna):
    return any(row["name"] == coluna for row in conn.execute(f"PRAGMA table_info({tabela})"))


def garantir_coluna(conn, tabela, coluna, definicao):
    if not coluna_existe(conn, tabela, coluna):
        conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")


def tabela_existe(conn, tabela):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (tabela,),
    ).fetchone() is not None


def garantir_schema_version(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            versao INTEGER NOT NULL,
            atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        INSERT INTO schema_version (id, versao)
        VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET
            versao = CASE WHEN versao < excluded.versao THEN excluded.versao ELSE versao END,
            atualizado_em = CASE
                WHEN versao < excluded.versao THEN CURRENT_TIMESTAMP
                ELSE atualizado_em
            END
        """,
        (SCHEMA_VERSION,),
    )


def garantir_tabela_estado_alertas(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS estado_alertas (
        chave TEXT PRIMARY KEY,
        valor_json TEXT NOT NULL,
        atualizado_em TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)


def garantir_tabela_acumulados_diarios(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS acumulados_diarios (
        data TEXT PRIMARY KEY,
        chuva_total_corrigida REAL NOT NULL DEFAULT 0,
        chuva_ultima_leitura REAL,
        chuva_reset_count INTEGER NOT NULL DEFAULT 0,
        rajada_max_corrigida REAL NOT NULL DEFAULT 0,
        atualizado_em TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)


def garantir_tabela_usuarios(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        nome TEXT NOT NULL,
        telefone TEXT NOT NULL UNIQUE,
        endereco TEXT,
        ativo INTEGER DEFAULT 1,
        receber_whatsapp INTEGER DEFAULT 0,
        criado_em TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    garantir_coluna(conn, "usuarios", "ativo", "INTEGER DEFAULT 1")
    garantir_coluna(conn, "usuarios", "receber_whatsapp", "INTEGER DEFAULT 0")
    garantir_coluna(conn, "usuarios", "criado_em", "TEXT")
    # DEFAULT ativo preserva todos os cadastros legados. Apenas novos opt-ins
    # solicitados pelo site são gravados explicitamente como pendentes.
    garantir_coluna(conn, "usuarios", "status_cadastro", "TEXT DEFAULT 'ativo'")
    garantir_coluna(conn, "usuarios", "confirmado_em", "TEXT")


def garantir_tabela_alertas_envios(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS alertas_envios (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
        usuario_id INTEGER,
        nome TEXT,
        telefone TEXT,
        status TEXT NOT NULL,
        mensagem TEXT,
        erro TEXT
    )
    """)
    garantir_coluna(conn, "alertas_envios", "evento_id", "TEXT")


def garantir_tabela_alertas_fila(conn):
    tabela_nova = not tabela_existe(conn, "alertas_fila")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS alertas_fila (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        criado_em TEXT DEFAULT CURRENT_TIMESTAMP,
        atualizado_em TEXT DEFAULT CURRENT_TIMESTAMP,
        usuario_id INTEGER,
        nome TEXT,
        telefone TEXT NOT NULL,
        mensagem TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pendente',
        tentativas INTEGER NOT NULL DEFAULT 0,
        erro TEXT,
        enviado_em TEXT
    )
    """)
    garantir_coluna(conn, "alertas_fila", "criado_em", "TEXT")
    garantir_coluna(conn, "alertas_fila", "atualizado_em", "TEXT")
    garantir_coluna(conn, "alertas_fila", "usuario_id", "INTEGER")
    garantir_coluna(conn, "alertas_fila", "nome", "TEXT")
    garantir_coluna(conn, "alertas_fila", "telefone", "TEXT")
    garantir_coluna(conn, "alertas_fila", "mensagem", "TEXT")
    garantir_coluna(conn, "alertas_fila", "status", "TEXT DEFAULT 'pendente'")
    garantir_coluna(conn, "alertas_fila", "tentativas", "INTEGER DEFAULT 0")
    garantir_coluna(conn, "alertas_fila", "erro", "TEXT")
    garantir_coluna(conn, "alertas_fila", "enviado_em", "TEXT")
    garantir_coluna(conn, "alertas_fila", "evento_id", "TEXT")
    garantir_coluna(conn, "alertas_fila", "prioridade", "INTEGER DEFAULT 50")
    garantir_coluna(conn, "alertas_fila", "proxima_tentativa_em", "TEXT")
    garantir_coluna(conn, "alertas_fila", "max_tentativas", "INTEGER DEFAULT 4")
    garantir_coluna(conn, "alertas_fila", "erro_permanente", "INTEGER DEFAULT 0")
    if tabela_nova:
        conn.execute(
            "CREATE INDEX idx_alertas_fila_status_id ON alertas_fila(status, id)"
        )
        conn.execute(
            "CREATE INDEX idx_alertas_fila_prioridade "
            "ON alertas_fila(status, prioridade DESC, id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_alertas_fila_evento_usuario "
            "ON alertas_fila(evento_id, usuario_id) "
            "WHERE evento_id IS NOT NULL AND usuario_id IS NOT NULL"
        )


def garantir_tabela_alertas_eventos(conn):
    tabela_nova = not tabela_existe(conn, "alertas_eventos")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS alertas_eventos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        evento_id TEXT NOT NULL UNIQUE,
        data_referencia TEXT NOT NULL,
        tipo TEXT NOT NULL,
        nivel INTEGER NOT NULL,
        valor REAL,
        unidade TEXT,
        ocorrido_em_local TEXT,
        fonte TEXT,
        status TEXT NOT NULL DEFAULT 'detectado',
        destinatarios INTEGER NOT NULL DEFAULT 0,
        enfileirados INTEGER NOT NULL DEFAULT 0,
        enviados INTEGER NOT NULL DEFAULT 0,
        falhas INTEGER NOT NULL DEFAULT 0,
        mensagem TEXT,
        detalhe TEXT,
        criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """)
    if tabela_nova:
        conn.execute(
            "CREATE INDEX idx_alertas_eventos_data_tipo "
            "ON alertas_eventos(data_referencia DESC, tipo, nivel)"
        )


def garantir_tabela_logs_persistencia(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS logs_persistencia (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        data_hora TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        nivel TEXT NOT NULL,
        origem TEXT,
        mensagem TEXT NOT NULL,
        detalhe TEXT
    )
    """)


def garantir_tabela_health_check_estado(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS health_check_estado (
        chave TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        assinatura TEXT,
        mensagem TEXT,
        notificado_em TEXT,
        atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """)


def garantir_tabela_campanhas_whatsapp_envios(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS campanhas_whatsapp_envios (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        campanha_id TEXT NOT NULL,
        data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
        usuario_id INTEGER NOT NULL,
        nome TEXT,
        telefone TEXT,
        status TEXT NOT NULL,
        mensagem TEXT,
        erro TEXT,
        UNIQUE(campanha_id, usuario_id)
    )
    """)


def garantir_tabela_cadastro_eventos(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS cadastro_eventos (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
        acao TEXT NOT NULL,
        usuario_id INTEGER,
        nome TEXT,
        telefone TEXT,
        endereco TEXT,
        receber_whatsapp INTEGER,
        detalhe TEXT
    )
    """)


def registrar_cadastro_evento(
    conn,
    acao,
    usuario_id=None,
    nome=None,
    telefone=None,
    endereco=None,
    receber_whatsapp=None,
    detalhe=None,
):
    garantir_tabela_cadastro_eventos(conn)
    conn.execute(
        """
        INSERT INTO cadastro_eventos (
            acao,
            usuario_id,
            nome,
            telefone,
            endereco,
            receber_whatsapp,
            detalhe
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            acao,
            usuario_id,
            nome,
            telefone,
            endereco,
            receber_whatsapp,
            detalhe,
        ),
    )


def obter_estado_alertas():
    conn = get_db()
    try:
        garantir_tabela_estado_alertas(conn)
        conn.commit()
        row = conn.execute(
            "SELECT valor_json FROM estado_alertas WHERE chave = ?",
            (ALERT_STATE_KEY,),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["valor_json"])
    finally:
        conn.close()


def salvar_estado_alertas(estado):
    conn = get_db()
    try:
        garantir_tabela_estado_alertas(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO estado_alertas (
                chave,
                valor_json,
                atualizado_em
            ) VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (ALERT_STATE_KEY, json.dumps(estado, ensure_ascii=False, sort_keys=True)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():

    conn = get_db()

    cursor = conn.cursor()

    historico_novo = not tabela_existe(conn, "historico_clima")
    leituras_nova = not tabela_existe(conn, "leituras_brutas")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS historico_clima (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        temp REAL,
        sensacao REAL,
        umidade REAL,
        pressao REAL,

        uv REAL,
        radiacao REAL,

        vento_vel REAL,
        vento_rajada REAL,
        vento_dir REAL,

        chuva_rate REAL,
        chuva_evento REAL,
        chuva_hoje REAL,

        station_timestamp_ms INTEGER,
        station_data_hora_utc TEXT,
        station_data_hora_local TEXT,
        data_hora_utc TEXT,
        data_hora_local TEXT,
        bateria TEXT,
        sinal TEXT,
        leitura_bruta_id INTEGER,
        data_hora TEXT
    )
    """)

    garantir_coluna(conn, "historico_clima", "leitura_bruta_id", "INTEGER")
    garantir_coluna(conn, "historico_clima", "station_timestamp_ms", "INTEGER")
    garantir_coluna(conn, "historico_clima", "station_data_hora_utc", "TEXT")
    garantir_coluna(conn, "historico_clima", "station_data_hora_local", "TEXT")
    garantir_coluna(conn, "historico_clima", "data_hora_utc", "TEXT")
    garantir_coluna(conn, "historico_clima", "data_hora_local", "TEXT")
    garantir_coluna(conn, "historico_clima", "bateria", "TEXT")
    garantir_coluna(conn, "historico_clima", "sinal", "TEXT")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS leituras_brutas (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        origem TEXT NOT NULL DEFAULT 'ambientweather',
        station_timestamp_ms INTEGER,
        station_data_hora_utc TEXT,
        station_data_hora_local TEXT,
        recebido_em TEXT NOT NULL,
        recebido_em_utc TEXT,
        recebido_em_local TEXT,
        persistido_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

        payload_json TEXT NOT NULL,
        dados_convertidos_json TEXT,

        chuva_rate REAL,
        chuva_evento REAL,
        chuva_hoje REAL,
        bateria TEXT,
        sinal TEXT
    )
    """)

    garantir_coluna(conn, "leituras_brutas", "station_data_hora_utc", "TEXT")
    garantir_coluna(conn, "leituras_brutas", "station_data_hora_local", "TEXT")
    garantir_coluna(conn, "leituras_brutas", "recebido_em_utc", "TEXT")
    garantir_coluna(conn, "leituras_brutas", "recebido_em_local", "TEXT")
    garantir_coluna(conn, "leituras_brutas", "bateria", "TEXT")
    garantir_coluna(conn, "leituras_brutas", "sinal", "TEXT")

    garantir_tabela_logs_persistencia(conn)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS historico_diario (

        data TEXT PRIMARY KEY,
        temp_min REAL,
        temp_max REAL,
        temp_media REAL,
        umidade_min REAL,
        umidade_max REAL,
        vento_rajada_max REAL,
        chuva_total REAL,
        pressao_min REAL,
        pressao_max REAL,
        uv_max REAL
    )
    """)

    garantir_tabela_estado_alertas(conn)
    garantir_tabela_acumulados_diarios(conn)

    # Índices de tabelas potencialmente grandes só são automáticos em bancos
    # recém-criados. Em bancos existentes, maintenance --create-indexes faz a
    # criação explícita em janela operacional.
    if leituras_nova:
        cursor.execute(
            "CREATE INDEX idx_leituras_brutas_recebido_em ON leituras_brutas(recebido_em)"
        )
        cursor.execute(
            "CREATE INDEX idx_leituras_brutas_origem_station_ts "
            "ON leituras_brutas(origem, station_timestamp_ms)"
        )
    if historico_novo:
        cursor.execute(
            "CREATE INDEX idx_historico_clima_data_hora ON historico_clima(data_hora)"
        )
        cursor.execute(
            "CREATE INDEX idx_historico_clima_data_hora_utc ON historico_clima(data_hora_utc)"
        )
        cursor.execute(
            "CREATE INDEX idx_historico_clima_data_hora_local ON historico_clima(data_hora_local)"
        )

    garantir_tabela_usuarios(conn)
    garantir_tabela_alertas_envios(conn)
    garantir_tabela_alertas_fila(conn)
    garantir_tabela_alertas_eventos(conn)
    garantir_tabela_health_check_estado(conn)
    garantir_tabela_campanhas_whatsapp_envios(conn)
    garantir_tabela_cadastro_eventos(conn)
    garantir_schema_version(conn)

    conn.commit()

    conn.close()

    logger.info("Banco pronto; schema version %s", SCHEMA_VERSION)
