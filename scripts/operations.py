#!/usr/bin/env python3
"""Operación explícita del despliegue de la API biométrica.

Filosofía heredada de los despliegues municipales: toda acción es explícita,
valida antes de tocar el servidor y se detiene con un mensaje accionable.
Este servicio no tiene base de datos, no aplica migraciones y no contacta el
dispositivo biométrico salvo que se pida expresamente con --device.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "sipe-biometric-api-deploy"
MANIFEST_KEYS = {"release", "source_sha", "source_branch", "build_date", "status", "notes"}
SECRET_KEYS = ("BIOMETRIC_API_TOKEN", "BIOMETRIC_PASSWORD")


class Refusal(RuntimeError):
    """Acción detenida a propósito, con motivo legible para el operador."""


def require(ok, message):
    if not ok:
        raise Refusal(message)


def manifest(root=ROOT):
    data = json.loads((Path(root) / "release.json").read_text(encoding="utf-8"))
    require(set(data) == MANIFEST_KEYS, "release.json inválido: faltan o sobran claves")
    require(re.fullmatch(r"[0-9a-f]{40}", data["source_sha"]), "SHA fuente inválido")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", data["release"]), "Release inválido")
    require(re.fullmatch(r"[A-Za-z0-9/_-]{1,80}", data["source_branch"]), "Rama fuente inválida")
    require(data["status"] in {"prepared", "validated"}, "Release no preparado")
    require(data["notes"].strip(), "Release sin notas")
    return data


def dotenv(path):
    """Lee un archivo de entorno estricto: sin comillas, sin expansión, sin duplicados."""
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "Falta archivo de entorno regular")
    if os.name == "posix":
        require(path.stat().st_mode & 0o077 == 0, "El entorno debe tener permisos 600/400")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        require(sep and re.fullmatch(r"[A-Z][A-Z0-9_]*", key), "Formato de entorno inválido")
        require(
            key not in values and value == value.strip() and not any(c in value for c in "\x00$`\"'"),
            "Usar valores literales sin comillas ni expansión",
        )
        values[key] = value
    return values


def validate_env(v):
    """Comprueba el contrato mínimo del entorno de ejecución."""
    for key in (
        "BIOMETRIC_HOST",
        "BIOMETRIC_PORT",
        "BIOMETRIC_TIMEOUT",
        "BIOMETRIC_DEVICE_ID",
        "BIOMETRIC_API_TOKEN",
        "ATTENDANCE_DEFAULT_LIMIT",
        "ATTENDANCE_MAX_LIMIT",
    ):
        require(v.get(key), "Variable vacía: " + key)
    require(v.get("APP_ENV") in {"qa", "production", "development"}, "APP_ENV debe ser qa, production o development")
    require(v.get("BIOMETRIC_TRANSPORT", "auto") in {"auto", "tcp", "udp"}, "BIOMETRIC_TRANSPORT inválido")
    require(v.get("BIOMETRIC_ENCODING", "UTF-8").strip(), "BIOMETRIC_ENCODING vacío")
    port = v["BIOMETRIC_PORT"]
    require(port.isdigit() and 1 <= int(port) <= 65535, "BIOMETRIC_PORT inválido")
    timeout = v["BIOMETRIC_TIMEOUT"]
    require(re.fullmatch(r"\d+(\.\d+)?", timeout) and 0 < float(timeout) <= 300, "BIOMETRIC_TIMEOUT inválido")
    require(len(v["BIOMETRIC_API_TOKEN"]) >= 32, "BIOMETRIC_API_TOKEN debe tener al menos 32 caracteres")
    for key in ("ATTENDANCE_DEFAULT_LIMIT", "ATTENDANCE_MAX_LIMIT"):
        require(v[key].isdigit() and int(v[key]) >= 1, key + " inválido")
    require(
        int(v["ATTENDANCE_DEFAULT_LIMIT"]) <= int(v["ATTENDANCE_MAX_LIMIT"]),
        "ATTENDANCE_DEFAULT_LIMIT no puede superar ATTENDANCE_MAX_LIMIT",
    )
    api_port = v.get("BIOMETRIC_API_PORT", "8200")
    require(api_port.isdigit() and 1 <= int(api_port) <= 65535, "BIOMETRIC_API_PORT inválido")
    require(v.get("BIOMETRIC_API_PORT", "8200") != v["BIOMETRIC_PORT"], "El puerto publicado no puede ser el del biométrico")
    if v.get("BIOMETRIC_PASSWORD"):
        require(re.fullmatch(r"\d{1,10}", v["BIOMETRIC_PASSWORD"]), "BIOMETRIC_PASSWORD debe ser numérica")
    return v


class Context:
    """Estado de la operación: release, entorno y comandos de Compose."""

    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.m = manifest(self.root)
        self.envfile = Path(os.environ.get("BIOMETRIC_ENV_FILE", self.root / ".env")).resolve()
        self.v = validate_env(dotenv(self.envfile))
        self.env = {**os.environ, **self.v, "RUNTIME_ENV_FILE": str(self.envfile)}
        # La trazabilidad de la imagen sale de release.json, no del .env: el
        # release es la única fuente de verdad sobre qué código se despliega.
        self.env["RELEASE_VERSION"] = self.m["release"]
        self.env["SOURCE_SHA"] = self.m["source_sha"]
        self.base = [
            "docker", "compose",
            "--project-directory", str(self.root),
            "--project-name", PROJECT,
            "--env-file", str(self.envfile),
            "-f", str(self.root / "docker-compose.yml"),
        ]

    def run(self, args, *, output=None, input_stream=None, timeout=300, label="Comando"):
        try:
            p = subprocess.run(
                args, env=self.env, cwd=self.root,
                stdout=subprocess.PIPE if output is None else output,
                stderr=subprocess.PIPE, stdin=input_stream, timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise Refusal(label + ": herramienta no disponible o timeout; revisar estado antes de reintentar") from None
        if p.returncode:
            error = p.stderr.decode("utf-8", errors="replace")
            for key in SECRET_KEYS:
                if self.v.get(key):
                    error = error.replace(self.v[key], "<redacted>")
            raise Refusal(label + ": " + error[-2000:])
        return p.stdout.decode("utf-8") if output is None else ""

    def dc(self, *args, **kwargs):
        return self.run([*self.base, *args], **kwargs)

    @property
    def api_port(self):
        return int(self.v.get("BIOMETRIC_API_PORT", "8200"))

    @property
    def api_url(self):
        return "http://127.0.0.1:%d" % self.api_port


def git_clean(c):
    require(not c.run(["git", "status", "--porcelain"]).strip(), "El checkout tiene cambios sin commit")


def port_owner(c):
    """Devuelve el nombre del contenedor que publica el puerto, o None si está libre."""
    names = c.run(
        ["docker", "ps", "--filter", "publish=%d" % c.api_port, "--format", "{{.Names}}"]
    ).split()
    return names[0] if names else None


def port_free(c):
    """True si el puerto local está libre; si lo ocupa nuestro contenedor, True."""
    owner = port_owner(c)
    if owner:
        require(owner == c.v.get("BIOMETRIC_CONTAINER", "sipe_biometric_api"),
                "El puerto %d ya lo publica otro contenedor: %s" % (c.api_port, owner))
        return False
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", c.api_port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def http_get(url, token=None, timeout=10):
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def preflight(c):
    """Valida el servidor sin crear ni modificar nada."""
    require(c.run(["docker", "info", "--format", "{{.OSType}}"], label="Docker").strip() == "linux",
            "Se requiere Docker Linux")
    c.run(["docker", "compose", "version"], label="Docker Compose")
    c.dc("config", "--quiet", label="Compose config")
    git_clean(c)
    port_free(c)
    return True


def health(c, device=False):
    """Comprueba /health del servicio publicado en loopback."""
    status, body = http_get(c.api_url + "/health")
    require(status == 200, "El servicio no responde 200 en " + c.api_url + "/health")
    try:
        payload = json.loads(body)
    except ValueError:
        raise Refusal("La respuesta de /health no es JSON válido") from None
    require(payload.get("status") == "ok", "El servicio no está saludable")
    print("Health OK: " + c.api_url + "/health -> " + json.dumps(payload, ensure_ascii=False))
    if device:
        # Consulta explícita al dispositivo: sólo con --device.
        token = c.v["BIOMETRIC_API_TOKEN"]
        status, body = http_get(c.api_url + "/device", token=token)
        require(status == 200, "La consulta autenticada a /device devolvió " + str(status))
        print("Device OK: " + json.dumps(json.loads(body), ensure_ascii=False))
    return True


def deploy(c):
    """Construye, levanta y verifica el servicio."""
    require(c.m["status"] == "validated", "Falta validación local del runtime (status=prepared)")
    git_clean(c)
    preflight(c)
    c.dc("build", timeout=3600, label="Build Docker local")
    c.dc("up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", "api", timeout=300)
    health(c)


def rollback(c, ref):
    """Vuelve al release anterior aprobado y verifica la salud."""
    require(re.fullmatch(r"[0-9a-f]{40}", ref or ""), "Indicar SHA completo del deploy anterior aprobado")
    git_clean(c)
    preflight(c)
    target = json.loads(c.run(["git", "show", ref + ":release.json"], label="Release anterior"))
    require(target.get("status") in {"prepared", "validated"}, "El release anterior no es válido")
    c.run(["git", "checkout", "--detach", ref], label="Checkout rollback aprobado")
    c.dc("build", timeout=3600, label="Build rollback")
    c.dc("up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", "api", timeout=300)
    health(c)


def status(c):
    print(c.dc("ps"))
    owner = port_owner(c)
    print("Puerto %d publicado por: %s" % (c.api_port, owner or "nadie"))
    print("Release: %s (source %s@%s)" % (c.m["release"], c.m["source_branch"], c.m["source_sha"][:12]))


def version(c):
    print(json.dumps(c.m, ensure_ascii=False, indent=2))


@contextmanager
def operation_lock(c):
    """Impide dos operaciones simultáneas sobre el mismo despliegue."""
    path = c.root / ".operation.lock"
    with path.open("a+b") as stream:
        if os.name == "posix":
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(stream, fcntl.LOCK_UN)
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("preflight", "deploy", "healthcheck", "rollback", "status", "logs", "restart", "stop", "start", "version"),
    )
    parser.add_argument("--ref", help="SHA completo del deploy anterior aprobado (rollback)")
    parser.add_argument("--device", action="store_true", help="Consulta autenticada a /device (contacta el biométrico)")
    parser.add_argument("--tail", default="100", help="Líneas de log (comando logs)")
    parser.add_argument("--follow", action="store_true", help="Seguir logs en vivo (comando logs)")
    args = parser.parse_args(argv)
    c = Context()
    os.umask(0o077)
    if args.command == "preflight":
        preflight(c)
        print("Preflight Docker/env/compose/puerto: OK")
    elif args.command == "healthcheck":
        health(c, device=args.device)
    elif args.command == "version":
        version(c)
    elif args.command == "status":
        status(c)
    elif args.command == "logs":
        args_ = ["logs", "--tail", str(args.tail)]
        if args.follow:
            args_.append("--follow")
        c.dc(*args_, output=sys.stdout)
    elif args.command == "restart":
        c.dc("restart", "api")
    elif args.command == "stop":
        c.dc("stop", "api")
    elif args.command == "start":
        c.dc("up", "-d", "--no-deps", "api")
    else:
        with operation_lock(c):
            if args.command == "deploy":
                deploy(c)
            else:
                rollback(c, args.ref)


if __name__ == "__main__":
    try:
        main()
    except (Refusal, OSError, ValueError) as exc:
        print("DETENIDO: " + str(exc), file=sys.stderr)
        sys.exit(1)
