"""Validation, registration identity and atomic per-recipient message cooldown.

The migration retains legacy records and their history. Duplicate destinations
are quarantined rather than deleted or granted new consent.
"""
import re
import secrets
import time
import unicodedata


MAX_NOME = 120
MAX_ENDERECO = 240
DDD_VALIDOS = frozenset(
    "11 12 13 14 15 16 17 18 19 21 22 24 27 28 31 32 33 34 35 37 38 "
    "41 42 43 44 45 46 47 48 49 51 53 54 55 61 62 63 64 65 66 67 68 69 "
    "71 73 74 75 77 79 81 82 83 84 85 86 87 88 89 91 92 93 94 95 96 97 98 99".split()
)


def validar_telefone(valor):
    if not isinstance(valor, str) or len(valor) > 32:
        raise ValueError("Informe um telefone brasileiro válido com DDD.")
    if not re.fullmatch(r"[0-9+() .-]+", valor):
        raise ValueError("Informe um telefone brasileiro válido com DDD.")
    numero = re.sub(r"[^0-9]", "", valor)
    if len(numero) in (12, 13) and numero.startswith("55"):
        numero = numero[2:]
    if len(numero) not in (10, 11) or numero[:2] not in DDD_VALIDOS:
        raise ValueError("Informe um telefone brasileiro válido com DDD.")
    assinante = numero[2:]
    if (len(assinante) == 9 and not assinante.startswith("9")) or (
        len(assinante) == 8 and assinante[0] not in "2345"
    ) or len(set(numero)) == 1:
        raise ValueError("Informe um telefone brasileiro válido com DDD.")
    return "55" + numero


def _texto(valor, nome, limite):
    if not isinstance(valor, str):
        raise ValueError(f"Informe {nome}.")
    valor = valor.strip()
    if not 2 <= len(valor) <= limite or any(
        unicodedata.category(letra).startswith("C") for letra in valor
    ):
        raise ValueError(f"Informe {nome} entre 2 e {limite} caracteres.")
    return valor


def validar_cadastro(nome, telefone, endereco):
    return (_texto(nome, "o nome", MAX_NOME), validar_telefone(telefone),
            _texto(endereco, "o endereço", MAX_ENDERECO))


def garantir_seguranca_cadastros(conn):
    import database

    database.garantir_coluna(conn, "usuarios", "telefone_canonico", "TEXT")
    database.garantir_coluna(conn, "usuarios", "cadastro_nonce", "TEXT")
    database.garantir_coluna(conn, "usuarios", "duplicado_de", "INTEGER")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cadastro_mensagens_controle (
            telefone TEXT NOT NULL, finalidade TEXT NOT NULL,
            reservado_em REAL NOT NULL, PRIMARY KEY(telefone, finalidade)
        )
    """)

    # Only newly migrated rows need reconciliation; the unique index prevents
    # competing registrations from creating another active destination later.
    rows = conn.execute("""
        SELECT id, telefone, receber_whatsapp, ativo, status_cadastro,
               telefone_canonico, cadastro_nonce, duplicado_de
        FROM usuarios WHERE cadastro_nonce IS NULL OR cadastro_nonce = '' ORDER BY
            CASE WHEN receber_whatsapp = 1 AND (ativo = 1 OR ativo IS NULL)
                      AND (status_cadastro = 'ativo' OR status_cadastro IS NULL)
                 THEN 0 ELSE 1 END, id
    """).fetchall()
    proprietarios = {row["telefone_canonico"]: row["id"] for row in conn.execute("""
        SELECT id, telefone_canonico FROM usuarios
        WHERE cadastro_nonce IS NOT NULL AND cadastro_nonce != ''
          AND telefone_canonico IS NOT NULL AND duplicado_de IS NULL
    """)} if rows else {}
    for row in rows:
        if row["duplicado_de"] is not None:
            continue
        try:
            canonico = validar_telefone(row["telefone"])
        except ValueError:
            canonico = None
        if not row["cadastro_nonce"]:
            conn.execute("UPDATE usuarios SET cadastro_nonce = ? WHERE id = ?",
                         (secrets.token_hex(32), row["id"]))
        if canonico is None:
            # Invalid historical numbers cannot safely receive messages. The
            # row remains available for correction in the administrator UI.
            conn.execute("""UPDATE usuarios SET telefone_canonico = NULL,
                            ativo = 0, receber_whatsapp = 0 WHERE id = ?""", (row["id"],))
        elif canonico in proprietarios:
            conn.execute("""UPDATE usuarios SET telefone_canonico = ?,
                            duplicado_de = ?, ativo = 0, receber_whatsapp = 0
                            WHERE id = ?""", (canonico, proprietarios[canonico], row["id"]))
        else:
            proprietarios[canonico] = row["id"]
            conn.execute("UPDATE usuarios SET telefone_canonico = ? WHERE id = ?",
                         (canonico, row["id"]))

    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_telefone_canonico
                    ON usuarios(telefone_canonico)
                    WHERE duplicado_de IS NULL AND telefone_canonico IS NOT NULL""")

    # Normalization is SQL-only so every worker/maintenance connection observes
    # the same uniqueness constraint without registering a Python SQL function.
    digits = "NEW.telefone"
    for char in (" ", "+", "(", ")", "-", "."):
        digits = f"replace({digits}, '{char}', '')"
    canon = (f"CASE WHEN length({digits}) IN (10, 11) THEN '55' || {digits} "
             f"WHEN length({digits}) IN (12, 13) AND substr({digits}, 1, 2) = '55' "
             f"THEN {digits} ELSE NULL END")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS usuarios_seguranca_insert
        AFTER INSERT ON usuarios BEGIN
          UPDATE usuarios SET telefone_canonico = {canon},
            cadastro_nonce = lower(hex(randomblob(32))) WHERE id = NEW.id;
        END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS usuarios_seguranca_telefone
        AFTER UPDATE OF telefone ON usuarios WHEN OLD.telefone != NEW.telefone BEGIN
          UPDATE usuarios SET telefone_canonico = {canon},
            cadastro_nonce = lower(hex(randomblob(32))), duplicado_de = NULL
            WHERE id = NEW.id;
        END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS usuarios_seguranca_duplicado
        BEFORE UPDATE OF ativo, receber_whatsapp ON usuarios
        WHEN NEW.duplicado_de IS NOT NULL AND OLD.telefone = NEW.telefone AND
             (NEW.ativo = 1 OR NEW.receber_whatsapp = 1)
        BEGIN SELECT RAISE(ABORT, 'Cadastro duplicado precisa de telefone distinto'); END""")


def buscar_cadastro(conn, telefone):
    canonico = validar_telefone(telefone)
    return conn.execute("""
        SELECT id, nome, telefone, endereco, receber_whatsapp, ativo,
               status_cadastro, telefone_canonico, cadastro_nonce
        FROM usuarios WHERE telefone_canonico = ? AND duplicado_de IS NULL
    """, (canonico,)).fetchone()


def reservar_envio(conn, telefone, finalidade, *, agora=None, intervalo=300):
    """Reserve before network, then commit before sending; failures keep cooldown.

    UPSERT has a single write condition, so concurrent workers cannot both win.
    The short reservation also bounds repeat sends after a provider timeout.
    """
    instante = time.time() if agora is None else agora
    resultado = conn.execute("""
        INSERT INTO cadastro_mensagens_controle (telefone, finalidade, reservado_em)
        VALUES (?, ?, ?)
        ON CONFLICT(telefone, finalidade) DO UPDATE SET reservado_em = excluded.reservado_em
        WHERE cadastro_mensagens_controle.reservado_em <= excluded.reservado_em - ?
    """, (validar_telefone(telefone), finalidade, instante, intervalo))
    return resultado.rowcount == 1
