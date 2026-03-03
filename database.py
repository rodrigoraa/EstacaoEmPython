import sqlite3

DATABASE = 'estacao.db'

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
        umidade REAL,
        chuva REAL,
        vento_dir REAL,
        vento_vel REAL,
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
        criado_em TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()

    print("Banco pronto (produção)")

if __name__ == '__main__':
    init_db()