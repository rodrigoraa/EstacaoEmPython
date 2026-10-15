import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"


class WorkerEntrypointsTest(unittest.TestCase):
    def test_updater_pode_ser_carregado_pelo_caminho_de_execucao_direta(self):
        codigo = (
            "import runpy; "
            "modulo = runpy.run_path('workers/updater.py', run_name='cleanup_test'); "
            "assert callable(modulo['executar'])"
        )

        resultado = subprocess.run(
            [sys.executable, "-c", codigo],
            cwd=ESTACAO_DIR,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(resultado.returncode, 0, resultado.stderr)

    def test_whatsapp_sender_expoe_os_modos_cli_de_producao(self):
        ambiente = os.environ.copy()
        ambiente.update(
            {
                "EVOLUTION_URL": "http://localhost",
                "EVOLUTION_API_KEY": "cleanup-test",
                "EVOLUTION_INSTANCE": "cleanup-test",
            }
        )

        resultado = subprocess.run(
            [sys.executable, "workers/whatsapp_sender.py", "--help"],
            cwd=ESTACAO_DIR,
            env=ambiente,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        for opcao in ("--once", "--limite", "--intervalo", "--retry-failed"):
            self.assertIn(opcao, resultado.stdout)

    def test_whatsapp_sender_carrega_env_antes_do_banco_e_preserva_ambiente(self):
        codigo = textwrap.dedent("""
            import os
            import runpy
            import sys
            from pathlib import Path
            from unittest.mock import patch

            os.environ.pop('ESTACAO_DB', None)
            banco_processo = sys.argv[2]
            if banco_processo:
                os.environ['ESTACAO_DB'] = banco_processo
            env_esperado = Path(sys.argv[1]).resolve().parents[1] / '.env'
            carregamentos = []

            def carregar_env(path=None, **kwargs):
                carregamentos.append((path, kwargs))
                assert kwargs.get('override', False) is False
                if path is not None and Path(path).resolve() == env_esperado:
                    os.environ.setdefault('ESTACAO_DB', 'banco-somente-env.sqlite')
                return True

            with patch('dotenv.load_dotenv', side_effect=carregar_env):
                runpy.run_path(sys.argv[1], run_name='sender_entrypoint_test')
            assert Path(carregamentos[0][0]).resolve() == env_esperado
            assert carregamentos[0][1]['encoding'] == 'utf-8'
            banco_esperado = banco_processo or 'banco-somente-env.sqlite'
            assert sys.modules['database'].DATABASE == banco_esperado
        """)
        for banco_processo in ("", "banco-do-processo.sqlite"):
            with self.subTest(banco_processo=banco_processo):
                resultado = subprocess.run(
                    [sys.executable, "-c", codigo,
                     str(ESTACAO_DIR / "workers" / "whatsapp_sender.py"), banco_processo],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(resultado.returncode, 0, resultado.stderr)


if __name__ == "__main__":
    unittest.main()
