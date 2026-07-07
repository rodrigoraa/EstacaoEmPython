import importlib
import os
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))


class WeatherServiceConfigTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("AMBIENT_PUBLIC_SLUG", None)

    def test_usa_slug_padrao_quando_env_nao_existe(self):
        os.environ.pop("AMBIENT_PUBLIC_SLUG", None)
        import services.weather_service as weather_service

        weather_service = importlib.reload(weather_service)

        self.assertEqual(
            weather_service.PUBLIC_SLUG,
            weather_service.DEFAULT_PUBLIC_SLUG,
        )
        self.assertIn(
            f"public.slug={weather_service.DEFAULT_PUBLIC_SLUG}",
            weather_service.URL,
        )

    def test_usa_slug_do_env(self):
        os.environ["AMBIENT_PUBLIC_SLUG"] = "slug-teste"
        import services.weather_service as weather_service

        weather_service = importlib.reload(weather_service)

        self.assertEqual(weather_service.PUBLIC_SLUG, "slug-teste")
        self.assertIn("public.slug=slug-teste", weather_service.URL)

    def test_env_vazio_volta_para_slug_padrao(self):
        os.environ["AMBIENT_PUBLIC_SLUG"] = "   "
        import services.weather_service as weather_service

        weather_service = importlib.reload(weather_service)

        self.assertEqual(
            weather_service.PUBLIC_SLUG,
            weather_service.DEFAULT_PUBLIC_SLUG,
        )


if __name__ == "__main__":
    unittest.main()
