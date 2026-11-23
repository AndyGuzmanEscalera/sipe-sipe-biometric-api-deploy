# Despliegue — API Biométrica Sipe Sipe

Repositorio de despliegue de la **API Biométrica Sipe Sipe** (Gobierno Autónomo
Municipal de Sipe Sipe).

Contiene únicamente lo necesario para construir y ejecutar el servicio en el
servidor Debian/Ubuntu con Docker Compose:

- Runtime de la API (`backend/app/`, copiado desde el repositorio fuente).
- `Dockerfile` de producción con etiquetas OCI del SHA de origen.
- `docker-compose.yml` con el servicio publicado sólo en loopback.
- `release.json` con la trazabilidad del release desplegado.
- Scripts operativos (`scripts/`).
- Documentación de operación (`docs/`).

**No** incluye las pruebas del repositorio fuente: el servidor no ejecuta tests.

---

## Arquitectura de publicación

```
Internet
   ↓
Cloudflare Tunnel   (cloudflared corre en el servidor; todavía NO configurado)
   ↓
127.0.0.1:8200
   ↓
contenedor sipe_biometric_api   (FastAPI, uvicorn, usuario no root)
   ↓
conexión saliente TCP 192.168.30.90:4370
   ↓
reloj biométrico ZKTeco (uFace802 Plus / ZMM720_TFT)
```

El puerto `8200` queda ligado a `127.0.0.1`: **nunca** se abre en el router ni
en el firewall. El puerto `4370` del biométrico **no** se publica con Docker:
el contenedor se conecta hacia afuera.

No hay base de datos: el servicio es sin estado y sólo lee del dispositivo.

---

## 1. Repositorios

| Repositorio | Rama | Contenido |
|---|---|---|
| `sipe-sipe-biometric-api` | `develop` | código fuente, tests, Dockerfile de desarrollo |
| `sipe-sipe-biometric-api-deploy` | `main` | este repositorio: runtime, compose, scripts, operación |

---

## 2. Estructura

```text
.env.example              Plantilla de configuración (sin secretos)
.gitattributes
.gitignore
README.md
docker-compose.yml        Servicio api publicado en 127.0.0.1:8200
release.json              Release desplegado: source_sha, source_branch, estado
backend/
  Dockerfile              Generado con las etiquetas OCI del SHA de origen
  .dockerignore
  requirements.txt
  app/                    Runtime copiado desde el repositorio fuente
scripts/
  operations.py           Utilidad de operación (preflight, deploy, rollback, ...)
  preflight.sh
  deploy.sh
  healthcheck.sh
  rollback.sh
docs/
  OPERACION.md
tests/
  test_operations.py      Pruebas de la lógica de operación (no requieren Docker)
```

---

## 3. Preparar el release en la PC

El repositorio fuente trae `scripts/prepare_deploy.ps1`, que copia el runtime
confirmado y escribe `release.json`:

```powershell
cd C:\dev\sipe_sipe_biometric_api
.\scripts\prepare_deploy.ps1 -ExpectedSourceSha (git rev-parse HEAD)
```

El script exige rama `develop`, SHA exacto y árbol limpio. No hace commit ni
push. Después se revisa el diff, se valida localmente y se sube el repositorio
deploy.

Para promover una versión nueva (flujo completo):

```powershell
cd C:\dev\sipe_sipe_biometric_api
git checkout develop
git pull --ff-only
.\scripts\prepare_deploy.ps1 -ExpectedSourceSha (git rev-parse HEAD)

cd C:\dev\sipe_sipe_biometric_api_deploy
git checkout main
git pull --ff-only
git add -A
git commit -m "release: <SHA corto del source>"
git push origin main
```

En el servidor:

```bash
cd sipe-sipe-biometric-api-deploy
git pull --ff-only
bash scripts/preflight.sh
bash scripts/deploy.sh
```

---

## 4. Primer despliegue en el servidor

Ver [docs/OPERACION.md](docs/OPERACION.md) para el procedimiento completo.

Resumen:

```bash
git clone https://github.com/AndyGuzmanEscalera/sipe-sipe-biometric-api-deploy.git
cd sipe-sipe-biometric-api-deploy
cp .env.example .env
chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # -> BIOMETRIC_API_TOKEN
bash scripts/preflight.sh
bash scripts/deploy.sh
curl http://127.0.0.1:8200/health
```

---

## 5. Variables de entorno

Todas están documentadas en `.env.example`. Las que importan:

| Variable | Obligatoria | Descripción |
|---|---|---|
| `BIOMETRIC_HOST` | sí | IP del reloj biométrico |
| `BIOMETRIC_PORT` | sí | puerto del protocolo ZKTeco (4370) |
| `BIOMETRIC_PASSWORD` | no | clave de comunicación del equipo |
| `BIOMETRIC_TIMEOUT` | sí | timeout de red en segundos |
| `BIOMETRIC_DEVICE_ID` | sí | identificador del equipo en las respuestas |
| `BIOMETRIC_TRANSPORT` | no | `auto`, `tcp` o `udp` |
| `BIOMETRIC_ENCODING` | no | `UTF-8` |
| `BIOMETRIC_API_TOKEN` | sí | token Bearer; mínimo 32 caracteres |
| `ATTENDANCE_DEFAULT_LIMIT` | no | `500` |
| `ATTENDANCE_MAX_LIMIT` | no | `5000` |
| `ATTENDANCE_SNAPSHOT_TTL` | no | segundos que vive un snapshot de consulta en memoria (`300`) |
| `ATTENDANCE_SNAPSHOT_MAX` | no | número máximo de snapshots simultáneos, con evicción LRU (`8`) |
| `APP_ENV` | sí | `qa`, `production` o `development` |
| `LOG_LEVEL` | no | `INFO` |
| `BIOMETRIC_ENABLE_DOCS` | no | `false` |
| `BIOMETRIC_API_PORT` | no | puerto local publicado (8200) |
| `BIOMETRIC_CONTAINER` | no | nombre del contenedor |
| `RELEASE_VERSION` / `SOURCE_SHA` | no | los escribe `prepare_deploy.ps1` |

`BIOMETRIC_API_TOKEN` y `BIOMETRIC_PASSWORD` nunca se versionan. `.env` está en
`.gitignore` y debe quedar con permisos `600`.

---

## 6. Publicación con Cloudflare Tunnel (pendiente)

```
biometric.sipesipe.gob.bo
        ↓
Cloudflare Tunnel
        ↓
http://127.0.0.1:8200
        ↓
sipe-sipe-biometric-api
        ↓
192.168.30.90:4370
```

**El túnel todavía no existe.** Cuando se cree:

- `cloudflared` correrá en el servidor, no como contenedor de este compose.
- `8200` seguirá ligado a `127.0.0.1`; no se abre ningún puerto en el router.
- La API exige `Authorization: Bearer <token>` en todas las rutas salvo
  `/health`: el túnel es el punto de entrada, el token es la segunda barrera.
- El dominio no está hardcodeado en la aplicación.

---

## 7. Pruebas locales de operación

```bash
python3 -B -m unittest discover -s tests -p "test_*.py" -v
```

No requieren Docker ni el dispositivo biométrico.
