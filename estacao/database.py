import sqlite3

DATABASE = "estacao.db"


def get_db():

    conn = sqlite3.connect(DATABASE)

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    conn = get_db()

    cursor = conn.cursor()

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

        data_hora TEXT
    )
    """)

    cursor.execute("""
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

    conn.commit()

    conn.close()

    print("Banco pronto")