import os
import subprocess
import sys
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


if __name__ == "__main__":
    unittest.main()
