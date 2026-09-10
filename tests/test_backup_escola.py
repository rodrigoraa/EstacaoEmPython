"""Executa o shell real com destinos temporários e rclone inteiramente simulado."""
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = (shutil.which("bash") if os.name != "nt" else
        next((str(p) for p in (Path("C:/Program Files/Git/bin/bash.exe"),)
              if p.is_file()), None))


def shell_path(path):
    value = Path(path).absolute().as_posix()
    if os.name == "nt":
        return f"/{value[0].lower()}{value[2:]}"
    return value


@unittest.skipUnless(BASH, "Bash indisponível")
class BackupEscolaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for nome in ("secretaria", "estacao", "logs", "uploads", "bin", "remote"):
            (self.root / nome).mkdir()
        for nome in ("secretaria.db", "estacao.db", "worker.py", "rclone.conf", "uploads/exemplo.pdf"):
            (self.root / nome).write_text("original", encoding="utf-8")
        for nome in ("estacao", "secretaria", "logs", "outra"):
            (self.root / "remote" / nome).mkdir()
        self.artefatos = {
            "secretaria_latest.db.gz": ("secretaria", "secretaria_2026-09-09_22-00.db.gz"),
            "uploads_latest.tar.gz": ("secretaria", "uploads_2026-09-09_22-00.tar.gz"),
            "estacao_latest.db.gz": ("estacao", "estacao_2026-09-09_22-00.db.gz"),
        }
        self.remotos = {
            nome: self.root / "remote" / pasta / nome
            for nome, (pasta, _) in self.artefatos.items()
        }
        for path in self.remotos.values():
            path.write_text("backup anterior", encoding="utf-8")
        self.latest = self.remotos["estacao_latest.db.gz"]
        self.historicos = []
        for nome in ("estacao/estacao_2020-01-01_22-00.db.gz",
                     "secretaria/secretaria_2020-01-01_22-00.db.gz",
                     "secretaria/uploads_2020-01-01_22-00.tar.gz"):
            path = self.root / "remote" / nome
            path.write_text("historico", encoding="utf-8")
            self.historicos.append(path)
        self.protegidos = [self.root / "remote/logs/backup.log", self.root / "remote/outra/documento.pdf"]
        for path in self.protegidos:
            path.write_text("nao tocar", encoding="utf-8")
        self.trace = self.root / "trace"
        self.env = os.environ.copy()
        self.env.update({"MOCK_ROOT": shell_path(self.root), "MOCK_TRACE": shell_path(self.trace)})
        self.helper("python", '''echo "worker:$*" >>"$MOCK_TRACE"
if [[ "${FAIL_WORKER:-0}" == 1 ]]; then echo 'erro worker' >&2; exit 21; fi
printf 'snapshot sqlite validado' >"${@: -1}"
echo 'Backup SQLite consistente criado.'
''')
        self.helper("rclone", '''echo "rclone:$*" >>"$MOCK_TRACE"
echo "rclone stderr: $1" >&2
comando=$1
shift
shopt -s nullglob dotglob
if [[ "$comando" == copyto ]]; then remoto=$2; else remoto=$1; fi
case "$remoto" in
gdrive:BackupsServidor/estacao/*) pasta="$MOCK_ROOT/remote/estacao";;
gdrive:BackupsServidor/secretaria/*) pasta="$MOCK_ROOT/remote/secretaria";;
*) exit 90;;
esac
case "$comando" in
copyto)
    nome=${remoto##*/}
    if [[ "${FAIL_UPLOAD:-0}" == "$nome" ]]; then echo "erro upload $nome" >&2; exit 22; fi
    cp -- "$1" "$pasta/$nome"
    echo "uploaded:$remoto" >>"$MOCK_TRACE";;
delete)
    if [[ "${FAIL_DELETE:-0}" == 1 ]]; then exit 24; fi
    [[ "$remoto" == "gdrive:BackupsServidor/${pasta##*/}/" ]] || exit 91
    regras=()
    profundidade=
    shift
    while (( $# )); do
        case "$1" in
        --filter) regras+=("$2"); shift 2;;
        --max-depth) profundidade=$2; shift 2;;
        *) shift;;
        esac
    done
    [[ "$profundidade" == 1 ]] || exit 92
    # Modelo dos filtros ordenados: primeira correspondência decide.
    for arquivo in "$pasta"/*; do
        [[ -f "$arquivo" && ! -L "$arquivo" ]] || continue
        incluir=1
        for regra in "${regras[@]}"; do
            padrao=${regra:2}
            if [[ "/${arquivo##*/}" == $padrao ]]; then
                if [[ "${regra:0:1}" == - ]]; then incluir=0; fi
                break
            fi
        done
        if (( incluir )); then
            echo "deleted:${pasta##*/}/${arquivo##*/}" >>"$MOCK_TRACE"
            rm -- "$arquivo"
        fi
    done;;
lsf)
    if [[ "${FAIL_LIST:-0}" == "${pasta##*/}" ]]; then exit 26; fi
    for arquivo in "$pasta"/*; do
        if [[ -d "$arquivo" ]]; then printf '%s/\\n' "${arquivo##*/}";
        else printf '%s\\n' "${arquivo##*/}"; fi
    done;;
*) exit 92;;
esac
''')
        self.helper("ionice", 'shift; exec "$@"\n')
        self.helper("nice", '''shift 2
if [[ "${FAIL_GZIP:-0}" == 1 && "$1" == gzip ]]; then echo 'erro gzip' >&2; exit 23; fi
exec "$@"
''')
        # A exclusão real via flock é exercitada em Linux; aqui também simulamos
        # contenção e falha do comando de forma portátil (Git Bash não tem flock).
        self.helper("flock", 'exit "${LOCK_RESULT:-0}"\n')
        self.helper("stat", '''if [[ "$1" == -f ]]; then
    if [[ "${FAIL_SPACE_STAT:-0}" == 1 ]]; then echo 'erro stat filesystem' >&2; exit 25; fi
    contador=0
    if [[ -f "$MOCK_ROOT/space_checks" ]]; then read -r contador <"$MOCK_ROOT/space_checks"; fi
    contador=$((contador + 1))
    echo "$contador" >"$MOCK_ROOT/space_checks"
    blocos=${AVAILABLE_BLOCKS:-100000000}
    if [[ "${SPACE_DROP_AT:-0}" != 0 && "$contador" -ge "$SPACE_DROP_AT" ]]; then blocos=0; fi
    printf '%s 4096\\n' "$blocos"
else
    exec /usr/bin/stat "$@"
fi
''')
        source = (ROOT / "deploy/backup_escola.sh").read_text(encoding="utf-8")
        paths = {
            "PYTHON": self.root / "bin/python", "WORKER": self.root / "worker.py",
            "SECRETARIA_DB": self.root / "secretaria.db", "UPLOADS": self.root / "uploads",
            "ESTACAO_DB": self.root / "estacao.db", "BACKUP_SECRETARIA": self.root / "secretaria",
            "BACKUP_ROOT": self.root,
            "BACKUP_ESTACAO": self.root / "estacao", "LOG_DIR": self.root / "logs",
            "RCLONE": self.root / "bin/rclone", "RCLONE_CONFIG": self.root / "rclone.conf",
            "LOCK": self.root / "backup.lock",
        }
        lines = source.splitlines()
        for index, line in enumerate(lines):
            for nome, path in paths.items():
                if line.startswith(f"readonly {nome}="):
                    lines[index] = f"readonly {nome}='{shell_path(path)}'"
        source = "\n".join(lines) + "\n"
        # Duas execuções pertencem ao mesmo minuto mesmo ao cruzar o relógio real.
        source = source.replace("DATA=$(date +%F_%H-%M)", "DATA=2026-09-09_22-00")
        for nome in ("ionice", "nice", "flock", "stat"):
            source = source.replace(f"/usr/bin/{nome}", f"'{shell_path(self.root / ('bin/' + nome))}'")
        self.script = self.root / "backup.sh"
        self.script.write_text(source, encoding="utf-8", newline="\n")

    def helper(self, nome, source):
        path = self.root / "bin" / nome
        path.write_text("#!/bin/bash\nset -e\n" + source, encoding="utf-8", newline="\n")
        path.chmod(0o700)

    def run_backup(self, **env):
        result = subprocess.run([BASH, shell_path(self.script)], env={**self.env, **env},
                                capture_output=True, text=True, timeout=30)
        self.logs = "\n".join(p.read_text(encoding="utf-8") for p in (self.root / "logs").glob("*.log"))
        self.calls = self.trace.read_text(encoding="utf-8") if self.trace.exists() else ""
        return result

    def assert_no_partials(self):
        self.assertFalse(list(self.root.glob("*/.backup-exec-*")))

    def test_bash_syntax(self):
        result = subprocess.run([BASH, "-n", shell_path(ROOT / "deploy/backup_escola.sh")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sucesso_envia_tres_artefatos_atuais_e_limpa_depois(self):
        # Históricos locais, logs e auxiliares não podem ser escolhidos no upload.
        for nome in ("estacao/estacao_2026-01-01_22-00.db.gz",
                     "secretaria/secretaria_2026-01-01_22-00.db.gz",
                     "secretaria/uploads_2026-01-01_22-00.tar.gz",
                     "logs/anterior.log", "estacao/antigo.db-wal",
                     "estacao/antigo.db-journal", "estacao/antigo.db-shm"):
            (self.root / nome).write_text("nao enviar")
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr + self.logs)
        uploads = [line for line in self.calls.splitlines() if line.startswith("rclone:copyto")]
        self.assertEqual(len(uploads), 3)
        for chamada, (nome, (pasta, arquivo)) in zip(uploads, self.artefatos.items()):
            atual = self.root / pasta / arquivo
            self.assertTrue(chamada.startswith(
                f"rclone:copyto {shell_path(atual)} gdrive:BackupsServidor/{pasta}/{nome} "))
            self.assertEqual(self.remotos[nome].read_bytes(), atual.read_bytes())
            self.assertIn("--checksum", chamada)
        for chamada in self.calls.splitlines():
            if chamada.startswith("rclone:"):
                self.assertIn("--transfers 1 --checkers 2", chamada)
                self.assertIn("--config", chamada)
                for opcao in ("--contimeout 1m", "--timeout 10m", "--retries 3",
                              "--low-level-retries 3", "--retries-sleep 30s"):
                    self.assertIn(opcao, chamada)
                self.assertNotIn("--max-duration", chamada)
        self.assertLess(self.calls.rindex("uploaded:"), self.calls.index("rclone:delete"))
        self.assertEqual(self.calls.count("rclone:delete"), 2)
        self.assertEqual(
            {line for line in self.calls.splitlines() if line.startswith("deleted:")},
            {f"deleted:{p.parent.name}/{p.name}" for p in self.historicos},
        )
        self.assertIn("rclone stderr: copyto", self.logs)
        self.assertIn("Backup finalizado com sucesso.", self.logs)
        self.assertEqual({p.name for p in (self.root / "remote/estacao").iterdir()}, {"estacao_latest.db.gz"})
        self.assertEqual({p.name for p in (self.root / "remote/secretaria").iterdir()},
                         {"secretaria_latest.db.gz", "uploads_latest.tar.gz"})
        for path in self.protegidos:
            self.assertEqual(path.read_text(), "nao tocar")
        self.assertEqual((self.root / "estacao.db").read_text(), "original")
        self.assert_no_partials()

    def verificar_falha_upload(self, artefato):
        result = self.run_backup(FAIL_UPLOAD=artefato)
        self.assertEqual(result.returncode, 22, self.logs)
        self.assertNotIn("rclone:delete", self.calls)
        self.assertNotIn("rclone:lsf", self.calls)
        ordem = list(self.artefatos)
        indice = ordem.index(artefato)
        self.assertEqual(self.calls.count("rclone:copyto"), indice + 1)
        for nome in ordem[indice:]:
            self.assertEqual(self.remotos[nome].read_text(), "backup anterior")
        for path in self.historicos:
            self.assertTrue(path.exists())
        for pasta, arquivo in self.artefatos.values():
            self.assertTrue((self.root / pasta / arquivo).is_file())
        self.assertIn(f'ERRO na etapa "upload Google Drive: {artefato}"', self.logs)
        self.assert_no_partials()

    def test_falha_upload_secretaria_preserva_nuvem_e_backups_locais(self):
        self.verificar_falha_upload("secretaria_latest.db.gz")

    def test_falha_upload_uploads_preserva_nuvem_e_backups_locais(self):
        self.verificar_falha_upload("uploads_latest.tar.gz")

    def test_falha_upload_estacao_preserva_nuvem_e_backups_locais(self):
        self.verificar_falha_upload("estacao_latest.db.gz")

    def preparar_legados_estacao(self):
        nomes = [f"estacao_2026-09-04_22-00{extensao}"
                 for extensao in (".db", ".db-journal", ".db-wal", ".db-shm")]
        nomes.append("estacao_2026-09-03_22-00.db.gz")
        legados = [self.root / "remote/estacao" / nome for nome in nomes]
        for arquivo in legados:
            arquivo.write_text("legado")
        return legados

    def test_migracao_remove_db_e_auxiliares_legados_so_apos_tres_uploads(self):
        legados = self.preparar_legados_estacao()
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, self.logs)
        self.assertEqual(self.calls.count("rclone:copyto"), 3)
        self.assertLess(self.calls.rindex("uploaded:"), self.calls.index("rclone:delete"))
        self.assertEqual({p.name for p in self.latest.parent.iterdir()}, {"estacao_latest.db.gz"})
        for arquivo in legados:
            self.assertFalse(arquivo.exists())
            self.assertIn(f"deleted:estacao/{arquivo.name}", self.calls)

    def test_migracao_preserva_desconhecidos_e_reporta_estado_inesperado(self):
        legados = self.preparar_legados_estacao()
        desconhecidos = [self.latest.parent / nome for nome in
                        ("arquivo-desconhecido.txt", "teste-rclone.txt")]
        for arquivo in desconhecidos:
            arquivo.write_text("preservar")
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("estado remoto inesperado", self.logs)
        for arquivo in desconhecidos:
            self.assertEqual(arquivo.read_text(), "preservar")
            self.assertIn(arquivo.name, self.logs)
        for arquivo in legados:
            self.assertFalse(arquivo.exists())
        self.assertTrue(self.latest.is_file())

    def test_migracao_nao_remove_legados_se_qualquer_upload_falhar(self):
        legados = self.preparar_legados_estacao()
        for artefato in self.artefatos:
            with self.subTest(artefato=artefato):
                # Nova execução local, mantendo todos os objetos remotos anteriores.
                for pasta, nome in self.artefatos.values():
                    (self.root / pasta / nome).unlink(missing_ok=True)
                result = self.run_backup(FAIL_UPLOAD=artefato)
                self.assertEqual(result.returncode, 22, self.logs)
                self.assertNotIn("rclone:delete", self.calls)
                for arquivo in legados:
                    self.assertEqual(arquivo.read_text(), "legado")

    def test_migracao_nao_amplia_padroes_datas_ou_escopo(self):
        nomes = [f"estacao/estacao_abcd-09-04_22-00{extensao}"
                 for extensao in (".db", ".db.gz", ".db-journal", ".db-wal", ".db-shm")]
        nomes += ["estacao/estacao_2026-09-04_22-00.db.bak",
                  "secretaria/estacao_2026-09-04_22-00.db-journal",
                  "secretaria/secretaria_2026-09-04_22-00.db",
                  "secretaria/uploads_2026-09-04_22-00.zip"]
        protegidos = [self.root / "remote" / nome for nome in nomes]
        for arquivo in protegidos:
            arquivo.write_text("preservar")
        # Diretório com nome de legado também não deve ser removido.
        diretorio = self.latest.parent / "estacao_2026-09-04_22-00.db"
        diretorio.mkdir()
        arquivo_interno = diretorio / "estacao_2026-09-04_22-00.db-wal"
        arquivo_interno.write_text("preservar")
        self.assertNotEqual(self.run_backup().returncode, 0)
        for arquivo in protegidos + [arquivo_interno]:
            self.assertEqual(arquivo.read_text(), "preservar")

    def test_espaco_insuficiente_preserva_tudo_sem_copia_nem_rclone(self):
        antigo = self.root / "estacao/estacao_2020-01-01_22-00.db.gz"
        antigo.write_bytes(b"backup local anterior")
        os.utime(antigo, (time.time() - 8 * 86400,) * 2)
        result = self.run_backup(AVAILABLE_BLOCKS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("espaço insuficiente", self.logs)
        self.assertIn("disponível=4096 bytes", self.logs)
        self.assertRegex(self.logs, r"mínimo necessário=\d+ bytes")
        self.assertEqual(self.calls, "", "Nem o worker nem o rclone devem ser chamados")
        self.assertEqual(antigo.read_bytes(), b"backup local anterior")
        self.assertEqual(self.latest.read_text(), "backup anterior")
        for path in self.historicos:
            self.assertTrue(path.exists())
        for path in ("estacao.db", "secretaria.db", "uploads/exemplo.pdf"):
            self.assertEqual((self.root / path).read_text(), "original")
        self.assert_no_partials()

    def test_estimativa_acompanha_crescimento_de_bancos_wal_e_uploads(self):
        self.run_backup(AVAILABLE_BLOCKS="0")
        minimo_anterior = int(re.findall(r"mínimo necessário=(\d+)", self.logs)[-1])
        self.assertGreater(minimo_anterior, 1024 ** 3)
        for nome, tamanho in (("estacao.db", 5 * 1024 ** 2),
                              ("secretaria.db-wal", 3 * 1024 ** 2),
                              ("uploads/novo.pdf", 1024 ** 2)):
            with (self.root / nome).open("wb") as arquivo:
                arquivo.truncate(tamanho)
        self.run_backup(AVAILABLE_BLOCKS="0")
        minimo_atual = int(re.findall(r"mínimo necessário=(\d+)", self.logs)[-1])
        # Reserva bancos crus + comprimidos mesmo para dados esparsos/compressíveis.
        self.assertGreater(minimo_atual - minimo_anterior, 17 * 1024 ** 2)
        self.assertEqual(self.calls, "")

    def test_espaco_reverificado_antes_de_uploads_e_compactacao(self):
        for etapa in (2, 4):
            with self.subTest(etapa=etapa):
                (self.root / "space_checks").unlink(missing_ok=True)
                result = self.run_backup(SPACE_DROP_AT=str(etapa))
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("rclone:", self.calls)
                self.assertFalse(list((self.root / "estacao").glob("*.gz")))
                self.assert_no_partials()

    def test_falha_na_medicao_de_espaco_impede_backup(self):
        self.assertNotEqual(self.run_backup(FAIL_SPACE_STAT="1").returncode, 0)
        self.assertEqual(self.calls, "")
        self.assert_no_partials()

    def test_falhas_locais_nao_acessam_drive(self):
        for flag in ("FAIL_WORKER", "FAIL_GZIP"):
            with self.subTest(flag=flag):
                result = self.run_backup(**{flag: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("rclone:", self.calls)
                self.assertEqual(self.latest.read_text(), "backup anterior")
                self.assert_no_partials()

    def test_lock_ocupado_sai_sem_iniciar_backup(self):
        result = self.run_backup(LOCK_RESULT="75")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.calls, "")
        self.assertIn("Outro backup ainda está em execução", self.logs)

    def test_erro_de_flock_nao_e_tratado_como_concorrencia(self):
        result = self.run_backup(LOCK_RESULT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls, "")

    @unittest.skipUnless(os.name == "posix" and shutil.which("flock"), "flock real requer Linux")
    def test_flock_real_impede_segunda_execucao(self):
        import fcntl
        source = self.script.read_text().replace(
            f"'{shell_path(self.root / 'bin/flock')}'", shutil.which("flock"))
        self.script.write_text(source)
        with (self.root / "backup.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_backup()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.calls, "")
        self.assertIn("Outro backup ainda está em execução", self.logs)

    def test_destino_existente_nao_sobrescreve(self):
        self.assertEqual(self.run_backup().returncode, 0)
        antes = self.latest.read_bytes()
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Destino já existe", self.logs)
        self.assertEqual(self.latest.read_bytes(), antes)
        self.assertEqual(self.calls.count("rclone:copyto"), 3)

    def test_retencao_limitada_a_backups_e_logs_completos(self):
        antigo = self.root / "estacao/estacao_2020-01-01_22-00.db.gz"
        protegido = self.root / "estacao/parcial-antigo.db-journal"
        recente = self.root / "secretaria/secretaria_2020-01-01_22-00.db.gz"
        for path in (antigo, protegido, recente):
            path.write_text("preservar quando apropriado")
        for path in (antigo, protegido):
            os.utime(path, (time.time() - 8 * 86400,) * 2)
        self.assertEqual(self.run_backup().returncode, 0, self.logs)
        self.assertFalse(antigo.exists())
        self.assertTrue(protegido.exists())
        self.assertTrue(recente.exists())

    def test_erro_na_limpeza_ou_listagem_nao_relata_sucesso(self):
        result = self.run_backup(FAIL_DELETE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Backup finalizado com sucesso.", self.logs)
        self.assertNotEqual(self.latest.read_bytes(), b"backup anterior")

    def test_subpasta_remota_e_reportada_sem_remocao(self):
        preservados = []
        for pasta in ("estacao", "secretaria"):
            subpasta = self.root / "remote" / pasta / "subpasta"
            subpasta.mkdir()
            arquivo = subpasta / f"{pasta}_2020-01-01_22-00.db.gz"
            arquivo.write_text("nao remover")
            preservados.append(arquivo)
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nenhuma subpasta será removida", self.logs)
        self.assertIn("subpasta/", self.logs)
        self.assertNotIn("purge", self.calls)
        for arquivo in preservados:
            self.assertEqual(arquivo.read_text(), "nao remover")

    def test_objetos_inesperados_e_historicos_da_outra_pasta_sao_preservados(self):
        preservados = []
        for nome in ("estacao/notas.txt", "secretaria/contrato.pdf",
                     "estacao/secretaria_2020-01-01_22-00.db.gz",
                     "secretaria/estacao_2020-01-01_22-00.db.gz",
                     "estacao/estacao_abcd-ef-gh_ij-kl.db.gz"):
            arquivo = self.root / "remote" / nome
            arquivo.write_text("nao remover")
            preservados.append(arquivo)
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("estado remoto inesperado", self.logs)
        for arquivo in preservados:
            self.assertEqual(arquivo.read_text(), "nao remover")
            self.assertIn(arquivo.name, self.logs)
        for arquivo in self.historicos:
            self.assertFalse(arquivo.exists())

    def test_falha_na_listagem_secretaria_nao_e_ocultada_pelo_sort(self):
        result = self.run_backup(FAIL_LIST="secretaria")
        self.assertEqual(result.returncode, 26, self.logs)
        self.assertNotIn("Backup finalizado com sucesso.", self.logs)

    def test_origem_ausente_falha_antes_de_copiar(self):
        (self.root / "secretaria.db").unlink()
        self.assertNotEqual(self.run_backup().returncode, 0)
        self.assertEqual(self.calls, "")

    def test_falha_na_limpeza_local_nao_relata_sucesso(self):
        source = self.script.read_text(encoding="utf-8").replace("rm -rf --", "false --")
        self.script.write_text(source, encoding="utf-8", newline="\n")
        self.assertNotEqual(self.run_backup().returncode, 0)
        self.assertNotIn("Backup finalizado com sucesso.", self.logs)


if __name__ == "__main__":
    unittest.main()
