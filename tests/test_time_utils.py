import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))

import time_utils


class TimeUtilsTest(unittest.TestCase):
    def test_minutos_desde_preserva_timezone_piso_e_clamp(self):
        agora = datetime(2026, 7, 19, 12, 0, tzinfo=time_utils.LOCAL_TZ)

        with mock.patch.object(time_utils, "agora_local", return_value=agora):
            self.assertEqual(
                time_utils.minutos_desde(
                    "2026-07-19T11:54:30-04:00",
                    assume_utc=False,
                ),
                5,
            )
            self.assertEqual(
                time_utils.minutos_desde("2026-07-19 15:50:00", assume_utc=True),
                10,
            )
            self.assertEqual(
                time_utils.minutos_desde(
                    "2026-07-19T12:05:00-04:00",
                    assume_utc=False,
                ),
                0,
            )
            self.assertIsNone(time_utils.minutos_desde("data-invalida"))


if __name__ == "__main__":
    unittest.main()
