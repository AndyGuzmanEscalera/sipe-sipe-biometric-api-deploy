"""Regresiones de seguridad de la operación. No requieren Docker ni dispositivo."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("operations", ROOT / "scripts/operations.py")
op = importlib.util.module_from_spec(spec)
spec.loader.exec_module(op)

TOKEN = "synthetic_bearer_token_" + "t" * 32


def env_values():
    values = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


class ManifestTests(unittest.TestCase):
    def test_release_valido(self):
        data = {
            "release": "qa-bd0461d039b2",
            "source_sha": "b" * 40,
            "source_branch": "develop",
            "build_date": "2026-11-11T00:00:00+00:00",
            "status": "prepared",
            "notes": "sintetico",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "release.json").write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(op.manifest(root)["source_branch"], "develop")

    def test_claves_incompletas_se_rechazan(self):
        data = {"release": "qa-1", "source_sha": "b" * 40, "source_branch": "develop"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "release.json").write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(op.Refusal):
                op.manifest(root)

    def test_sha_invalido_se_rechaza(self):
        data = {
            "release": "qa-1",
            "source_sha": "no-es-un-sha",
            "source_branch": "develop",
            "build_date": "2026-11-11T00:00:00+00:00",
            "status": "prepared",
            "notes": "sintetico",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "release.json").write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(op.Refusal):
                op.manifest(root)


class DotenvTests(unittest.TestCase):
    def write(self, content):
        handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
        handle.write(content)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return Path(handle.name)

    def test_lectura_simple(self):
        path = self.write("BIOMETRIC_HOST=192.168.30.90\n# comentario\n\nBIOMETRIC_PORT=4370\n")
        values = op.dotenv(path)
        self.assertEqual(values["BIOMETRIC_HOST"], "192.168.30.90")
        self.assertEqual(values["BIOMETRIC_PORT"], "4370")

    def test_valores_con_comillas_se_rechazan(self):
        path = self.write('BIOMETRIC_HOST="192.168.30.90"\n')
        with self.assertRaises(op.Refusal):
            op.dotenv(path)

    def test_duplicados_se_rechazan(self):
        path = self.write("BIOMETRIC_HOST=a\nBIOMETRIC_HOST=b\n")
        with self.assertRaises(op.Refusal):
            op.dotenv(path)

    def test_archivo_inexistente_se_rechaza(self):
        with self.assertRaises(op.Refusal):
            op.dotenv(Path("no-existe-este-archivo.env"))


class ValidateEnvTests(unittest.TestCase):
    def setUp(self):
        self.values = env_values()
        self.values["BIOMETRIC_API_TOKEN"] = TOKEN

    def test_entorno_valido(self):
        op.validate_env(self.values)

    def test_token_corto_se_rechaza(self):
        self.values["BIOMETRIC_API_TOKEN"] = "corto"
        with self.assertRaises(op.Refusal):
            op.validate_env(self.values)

    def test_token_vacio_se_rechaza(self):
        self.values["BIOMETRIC_API_TOKEN"] = ""
        with self.assertRaises(op.Refusal):
            op.validate_env(self.values)

    def test_host_vacio_se_rechaza(self):
        self.values["BIOMETRIC_HOST"] = ""
        with self.assertRaises(op.Refusal):
            op.validate_env(self.values)

    def test_app_env_invalido_se_rechaza(self):
        self.values["APP_ENV"] = "produccion"
        with self.assertRaises(op.Refusal):
            op.validate_env(self.values)

    def test_limites_incoherentes_se_rechazan(self):
        self.values["ATTENDANCE_DEFAULT_LIMIT"] = "5000"
        self.values["ATTENDANCE_MAX_LIMIT"] = "100"
        with self.assertRaises(op.Refusal):
            op.validate_env(self.values)

    def test_timeout_invalido_se_rechaza(self):
        self.values["BIOMETRIC_TIMEOUT"] = "0"
        with self.assertRaises(op.Refusal):
            op.validate_env(self.values)

    def test_puerto_publicado_no_puede_ser_el_del_biometrico(self):
        self.values["BIOMETRIC_API_PORT"] = self.values["BIOMETRIC_PORT"]
        with self.assertRaises(op.Refusal):
            op.validate_env(self.values)

    def test_transporte_invalido_se_rechaza(self):
        self.values["BIOMETRIC_TRANSPORT"] = "serial"
        with self.assertRaises(op.Refusal):
            op.validate_env(self.values)

    def test_password_no_numerica_se_rechaza(self):
        self.values["BIOMETRIC_PASSWORD"] = "secreto"
        with self.assertRaises(op.Refusal):
            op.validate_env(self.values)


class ComposeContractTests(unittest.TestCase):
    """El compose no debe publicar el puerto del biométrico ni abrir 0.0.0.0."""

    def setUp(self):
        self.text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    def test_el_servicio_publica_solo_loopback(self):
        self.assertIn("'127.0.0.1:${BIOMETRIC_API_PORT:-8200}:8000'", self.text)

    def test_no_se_publica_el_puerto_del_biometrico(self):
        self.assertNotIn(":4370:4370", self.text)
        self.assertNotIn("4370:4370", self.text)

    def test_hardening_presente(self):
        self.assertIn("read_only: true", self.text)
        self.assertIn("cap_drop: [ALL]", self.text)
        self.assertIn("no-new-privileges:true", self.text)
        self.assertIn("user: '10001:10001'", self.text)
        self.assertIn("restart: unless-stopped", self.text)

    def test_red_con_salida_hacia_la_lan(self):
        networks = self.text.split("networks:", 1)[1]
        # Se ignoran los comentarios: sólo importa la configuración efectiva.
        effective = "\n".join(
            line for line in networks.splitlines() if not line.strip().startswith("#")
        )
        self.assertNotIn("internal:", effective)
        self.assertIn("driver: bridge", effective)

    def test_no_se_usa_network_mode_host(self):
        self.assertNotIn("network_mode", self.text)


if __name__ == "__main__":
    unittest.main()
