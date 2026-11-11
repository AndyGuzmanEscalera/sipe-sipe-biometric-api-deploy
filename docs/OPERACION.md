# Operación — API Biométrica Sipe Sipe

Servidor de referencia: Debian / Ubuntu con Docker Engine y Compose v2.

Requisitos:

```bash
docker --version
docker compose version      # se requiere Compose >= 2.30
git --version
python3 --version           # >= 3.10 para ejecutar scripts/operations.py
```

El repositorio de despliegue vive en el servidor, por ejemplo
`/opt/sipe-sipe-biometric-api-deploy`.

---

## 1. Primera instalación

### 1.1 Clonar el repositorio deploy

```bash
cd /opt
git clone https://github.com/AndyGuzmanEscalera/sipe-sipe-biometric-api-deploy.git
cd sipe-sipe-biometric-api-deploy
```

### 1.2 Crear la configuración privada

```bash
cp .env.example .env
chmod 600 .env
```

Generar el token de la API **en el servidor** (no se versiona nunca):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copiar el resultado en `BIOMETRIC_API_TOKEN` dentro de `.env` y revisar el resto
de variables:

| Variable | Valor en QA |
|---|---|
| `BIOMETRIC_HOST` | `192.168.30.90` |
| `BIOMETRIC_PORT` | `4370` |
| `BIOMETRIC_PASSWORD` | clave del equipo (vacía o `0` si no la exige) |
| `BIOMETRIC_TIMEOUT` | `10` |
| `BIOMETRIC_DEVICE_ID` | `alcaldia-central` |
| `BIOMETRIC_TRANSPORT` | `tcp` |
| `BIOMETRIC_API_TOKEN` | valor generado en el servidor |
| `APP_ENV` | `qa` |
| `BIOMETRIC_API_PORT` | `8200` |

Alternativa: usar un archivo de entorno fuera del checkout:

```bash
export BIOMETRIC_ENV_FILE=/etc/sipe-biometric/runtime.env
chmod 600 /etc/sipe-biometric/runtime.env
```

### 1.3 Comprobar acceso al biométrico (opcional, antes del despliegue)

```bash
timeout 5 bash -c 'cat < /dev/null > /dev/tcp/192.168.30.90/4370' && echo "puerto 4370 alcanzable" || echo "puerto 4370 no alcanzable"
```

Este paso no consulta el dispositivo: sólo comprueba el TCP.

### 1.4 Preflight

```bash
bash scripts/preflight.sh
```

Valida Docker Linux, Compose, `.env` con permisos correctos, variables
obligatorias, `docker compose config`, checkout Git limpio y que el puerto 8200
no esté usado por otro proceso. **No crea nada.**

### 1.5 Despliegue

```bash
bash scripts/deploy.sh
```

Construye la imagen, levanta el contenedor con `up -d --no-deps --wait` y
verifica `GET /health`.

### 1.6 Verificación inicial

```bash
curl http://127.0.0.1:8200/health
# {"status":"ok","service":"sipe-sipe-biometric-api","version":"1.0.0"}

docker compose ps
```

---

## 2. Actualizar a una versión nueva

La promoción se hace desde la PC: `scripts/prepare_deploy.ps1` copia el
runtime confirmado y escribe `release.json` con el SHA de origen. En el servidor
sólo se hace pull + deploy.

En el servidor:

```bash
cd /opt/sipe-sipe-biometric-api-deploy
git pull --ff-only
bash scripts/preflight.sh
bash scripts/deploy.sh
curl http://127.0.0.1:8200/health
```

El servicio se reinicia sin downtime prolongado: `docker compose up -d` recrea
sólo el contenedor de la API. No hay base de datos, no hay migraciones y no hay
volúmenes de datos: nada que respaldar ni que reconciliar.

Comprobar la versión desplegada:

```bash
bash scripts/healthcheck.sh
python3 -B scripts/operations.py version
```

---

## 3. Estado

```bash
bash scripts/healthcheck.sh          # GET /health en 127.0.0.1:8200
docker compose ps                    # contenedores y healthchecks
docker compose logs -f api           # log en vivo
python3 -B scripts/operations.py status
python3 -B scripts/operations.py version
```

El release vigente se lee en `release.json`:

```bash
cat release.json
```

---

## 4. Rollback

El rollback es un `git checkout --detach` de un release anterior aprobado,
seguido de build y reinicio. No hay base de datos, así que no hay estado que
restaurar.

```bash
cd /opt/sipe-sipe-biometric-api-deploy
git log --oneline -5                       # identificar el commit anterior
bash scripts/rollback.sh --ref <SHA_COMPLETO_40>
curl http://127.0.0.1:8200/health
```

Requisitos del rollback: SHA completo, checkout limpio y `release.json`
presente en ese commit. El script se detiene antes de tocar nada si algo no
cuadra.

Después del rollback, el checkout queda en modo detached. Para volver a la
versión publicada:

```bash
git checkout main
git pull --ff-only
```

---

## 5. Prueba del hardware (NO ejecutar sin autorización)

Estos pasos **contactan el reloj biométrico**. El despliegue y el healthcheck no
los ejecutan solos.

Primero, el servicio sin tocar el dispositivo:

```bash
curl http://127.0.0.1:8200/health
```

Después, con el token real:

```bash
curl -H "Authorization: Bearer $BIOMETRIC_API_TOKEN" http://127.0.0.1:8200/device
```

```bash
curl -H "Authorization: Bearer $BIOMETRIC_API_TOKEN" http://127.0.0.1:8200/users
```

Y finalmente una prueba pequeña de asistencia:

```bash
curl -H "Authorization: Bearer $BIOMETRIC_API_TOKEN" "http://127.0.0.1:8200/attendance?date=2026-11-11"
```

> **Advertencia:** `/attendance` descarga **primero el histórico completo** del
> dispositivo (~100.000 registros) y después filtra en memoria. La primera
> llamada puede tardar varios minutos y saturar la red del equipo. Para una
> primera prueba conviene usar `?date=<día>&limit=20` o `/users`, que es más
> ligero.

El token se lee del entorno del servidor:

```bash
set -a; . ./.env; set +a
```

Alternativa equivalente con el script de operación (consulta autenticada
explícita, con confirmación en el código):

```bash
python3 -B scripts/operations.py healthcheck --device
```

Diagnóstico completo del equipo, sólo lectura (conexión, información básica,
conteo de usuarios y de marcaciones):

```bash
docker compose exec api python -B -m app.tools.device_check
```

---

## 6. Cloudflare Tunnel (pendiente)

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

Todavía **no** hay túnel. Cuando se cree:

1. `cloudflared` se instala y corre en el servidor, no como contenedor de este
   compose.
2. `8200` sigue ligado a `127.0.0.1`: no se abre ningún puerto en el router ni
   en el firewall del servidor.
3. La API conserva su autenticación Bearer: el túnel es el punto de entrada
   público, el token es la barrera de aplicación.
4. El hostname final será el institucional; no está hardcodeado en el código.

---

## 7. Notas de operación

- No ejecutar `docker compose down -v` salvo autorización expresa. Este servicio
  no tiene volúmenes de datos, pero `-v` sigue siendo una operación destructiva
  sobre otros proyectos del host.
- No publicar `8200` en `0.0.0.0`. El único acceso externo será el túnel.
- No copiar el `.env` del servidor a la PC ni a ningún repositorio.
- La API es de **solo lectura**: no expone comandos de escritura sobre el
  dispositivo y `release.json` no se modifica a mano.
