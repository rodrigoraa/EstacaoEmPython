"""Verificação local, somente leitura, sem chaves na saída e sem envios."""

import argparse
import importlib.metadata
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

from dotenv import load_dotenv
from packaging.version import Version

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import env_bool, env_str, validar_configuracao_web


def coletar_status():
    checks = []

    def check(name, ok, detail):
        checks.append({"item": name, "status": "ok" if ok else "pendente", "detalhe": detail})

    check("ambiente_producao", env_str("APP_ENV", "development").lower() == "production",
          "O serviço publicado deve definir APP_ENV=production.")
    try:
        validar_configuracao_web()
        valid = True
    except RuntimeError:
        valid = False
    check("configuracao_producao", valid,
          "Validador exige segredos fortes, hash bcrypt, HTTPS, cookies seguros e Redis em produção.")
    check("senha_sem_texto", bool(env_str("ADMIN_PASSWORD_HASH")) and not env_str("ADMIN_PASSWORD"),
          "Usar somente ADMIN_PASSWORD_HASH; nenhuma senha é exibida neste diagnóstico.")
    check("limites_compartilhados", env_bool("RATELIMIT_ENABLED", True) and
          (env_str("RATELIMIT_STORAGE_URI", "") or "").startswith(("redis://", "rediss://", "redis+unix://")),
          "Redis compartilha os contadores entre processos; este comando não testa sua conexão.")
    production = env_str("APP_ENV", "development").lower() == "production"
    check("cookie_secure", env_bool("SESSION_COOKIE_SECURE", production), "Secure deve estar ativo no HTTPS.")
    check("hsts", env_bool("HSTS_ENABLED", production), "HSTS deve estar ativo no domínio HTTPS.")
    for package, minimum in (("pip", "26.2.0"), ("urllib3", "2.8.0"), ("Werkzeug", "3.1.9")):
        try:
            installed = importlib.metadata.version(package)
            ok = Version(installed) >= Version(minimum)
        except (importlib.metadata.PackageNotFoundError, ValueError):
            installed, ok = "ausente", False
        check("dependencia_" + package, ok,
              f"Instalada: {installed}; mínimo corrigido na auditoria: {minimum}. Consultar OSV para avisos posteriores.")
    db_path = Path(env_str("ESTACAO_DB") or BASE_DIR / "estacao.db").resolve()
    try:
        conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {row[1] for row in conn.execute("PRAGMA table_info(usuarios)")}
            check("migracao_cadastros", {"telefone_canonico", "cadastro_nonce", "duplicado_de"} <= columns,
                  "Migração conserva registros legados e impede duplicatas canônicas ativas.")
            check("migracao_sessoes", "admin_sessoes" in tables,
                  "Sessões administrativas revogáveis devem estar presentes no banco.")
        finally:
            conn.close()
    except sqlite3.Error:
        check("banco_leitura", False, "Banco existente indisponível em modo somente leitura.")
    for name, path in (("arquivo_env", BASE_DIR / ".env"), ("arquivo_banco", db_path)):
        if os.name == "posix" and path.exists():
            mode = stat.S_IMODE(path.stat().st_mode)
            check(name, not bool(mode & 0o077), "Permissão recomendada: 600, acessível somente ao usuário do serviço.")
        else:
            checks.append({"item": name, "status": "verificar_no_servidor",
                           "detalhe": "Verificar permissões no Linux publicado."})
    checks.append({"item": "proxy_e_exposicao_publica", "status": "verificar_no_servidor",
                   "detalhe": "Verificar nginx -t, Gunicorn em loopback, 404 de arquivos sensíveis, HTTPS e CORS na URL publicada."})
    return {"somente_leitura": True, "pendencias": sum(c["status"] == "pendente" for c in checks), "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    load_dotenv(BASE_DIR / ".env", encoding="utf-8")
    report = coletar_status()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for check in report["checks"]:
            print(f"[{check['status']}] {check['item']}: {check['detalhe']}")
    return int(bool(report["pendencias"]))


if __name__ == "__main__":
    raise SystemExit(main())
