#!/usr/bin/env python3
"""
wake_up_claude.py

Mantiene SIEMPRE activa la ventana de sesión de 5 horas del plan Max, con
cobertura 24/7 anclada a los resets REALES del servidor (no estimados).

Cómo funciona (modo "poller", pensado para correr por cron cada ~30 min):

  1. Consulta GRATIS el uso real en GET /api/oauth/usage (con el token OAuth de
     la suscripción). Devuelve el `resets_at` exacto de la ventana de 5h y del
     cap semanal, en ISO 8601. No gasta tokens ni ventana.
  2. Según el estado decide:
       - Semanal al tope  -> agenda el reintento al reset SEMANAL (no sirve
                             despertar la ventana de 5h: estás bloqueado igual).
       - Sin ventana 5h   -> DESPIERTA ya (abre ventana nueva) y agenda el
                             próximo wake al nuevo reset.
       - Ventana 5h activa -> no despierta; (re)agenda UN `at` para reset+margen,
                             así reabrimos la ventana ni bien se cierra.
  3. El disparo preciso lo hace `at`: el poller agenda un único job para el
     instante del reset; cuando corre, vuelve a entrar acá en modo poller, ve
     que no hay ventana y despierta. La cadena se mantiene sola.

Auto-reparable: si se pierde un `at` (reboot, hipo), el siguiente poll (≤30 min)
lo vuelve a agendar. Por eso NO hace falta un cron diario aparte.

Regla de oro de facturación: `claude -p` cobra por API si ANTHROPIC_API_KEY está
en el entorno; si la borramos del env del subproceso, usa el crédito de la
suscripción. Por eso build_env() la excluye.

Requisito previo en la VPS:
  - CLI de Claude Code instalado y login hecho una vez con la suscripción
    (la auth OAuth queda en ~/.claude/.credentials.json y la reutilizamos).
  - `at` + `atd` activos (para el disparo preciso). Sin `at`, cae a cobertura
    con granularidad del cron (~30 min) en vez de al segundo.
"""

import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime


def resolve_claude_bin():
    """Resuelve la ruta al binario de claude.

    En cron el PATH es mínimo, así que conviene una ruta absoluta. Orden:
      1) Variable de entorno CLAUDE_BIN (si la definís en el cron).
      2) Autodetección con `which` en los PATH habituales.
      3) Rutas típicas de instalación como último recurso.
    """
    env_bin = os.environ.get("CLAUDE_BIN")
    if env_bin:
        return env_bin

    found = shutil.which("claude")
    if found:
        return found

    home = os.path.expanduser("~")
    for candidate in (
        os.path.join(home, ".local/bin/claude"),
        "/usr/local/bin/claude",
        "/usr/bin/claude",
    ):
        if os.path.exists(candidate):
            return candidate

    # Fallback: que falle con un mensaje claro más adelante.
    return "claude"


CLAUDE_BIN = resolve_claude_bin()
SCRIPT_PATH = os.path.abspath(__file__)

# Modelo del wake: haiku es el más barato y para "despertar" la ventana alcanza
# de sobra. El alias "haiku" resuelve al Haiku más reciente.
MODEL = os.environ.get("WAKE_MODEL", "haiku")

# Timeout defensivo (segundos) para que el cron no quede colgado.
TIMEOUT = 120

# Reintentos del wake ante errores transitorios (overload, hipo de red, refresh
# de auth puntual). El total de intentos es 1 + RETRIES.
RETRIES = int(os.environ.get("WAKE_RETRIES", "2"))
# Backoff base (segundos). La espera es BACKOFF_BASE * 2**intento.
BACKOFF_BASE = float(os.environ.get("WAKE_BACKOFF_BASE", "5"))

# --- Fuente de verdad del uso/reset (server-side, gratis) ---
# Endpoint interno que usa el propio CLI (fetchUtilization). Devuelve el reset
# exacto de cada ventana. Se consulta con el accessToken OAuth de la suscripción.
USAGE_URL = os.environ.get("WAKE_USAGE_URL", "https://api.anthropic.com/api/oauth/usage")
CREDENTIALS_PATH = os.environ.get(
    "WAKE_CREDENTIALS", os.path.expanduser("~/.claude/.credentials.json")
)
OAUTH_BETA = os.environ.get("WAKE_OAUTH_BETA", "oauth-2025-04-20")

# --- Parámetros de agendado ---
# Margen (segundos) a sumar al instante de reset, para no pegarle en el borde.
LIMIT_MARGIN_SEC = int(os.environ.get("WAKE_LIMIT_MARGIN_SEC", "120"))
# Umbral (%) a partir del cual consideramos un cap "agotado". Si la ventana de
# 5h llega acá pero el reset es futuro, igual esperamos al reset; el umbral
# importa sobre todo para el semanal (caso D).
WEEKLY_BLOCK_PCT = float(os.environ.get("WAKE_WEEKLY_BLOCK_PCT", "100"))
# Si no logramos saber el reset (endpoint y CLI caídos) o el wake falla, en
# cuánto reintentar de forma corta (segundos). El cron de todas formas acota.
RETRY_SOON_SEC = int(os.environ.get("WAKE_RETRY_SOON_SEC", "300"))
# Cooldown fijo (minutos) usado SOLO si pegamos un límite y no pudimos extraer
# el epoch de reset de ningún lado.
LIMIT_COOLDOWN_MIN = int(os.environ.get("WAKE_LIMIT_COOLDOWN_MIN", "60"))
# Duración nominal de la ventana (segundos). Solo se usa como último fallback si
# tras despertar el endpoint todavía no reporta el nuevo reset.
WINDOW_SEC = int(os.environ.get("WAKE_WINDOW_MIN", "300")) * 60

# Adónde redirige su salida el job agendado por `at` (corre sin la tubería del
# cron). Por defecto, el mismo wake.log del proyecto.
LOG_PATH = os.environ.get(
    "WAKE_LOG", os.path.join(os.path.dirname(SCRIPT_PATH), "wake.log")
)

# Marcador para identificar NUESTROS jobs de `at` entre los de la máquina, así
# limpiamos solo los propios antes de reagendar (dedupe idempotente).
AT_MARKER = "WAKEUPCLAUDE_AT"

# El texto que delata un límite de plan (no un overload transitorio).
USAGE_LIMIT_RE = re.compile(r"usage limit reached", re.IGNORECASE)
# El epoch de reset en el texto del error, ya sea como campo JSON o tras un "|".
RESET_FIELD_RE = re.compile(r'resetsAt"?\s*[:=]\s*"?(\d{10,13})', re.IGNORECASE)
RESET_PIPE_RE = re.compile(r"\|\s*(\d{10,13})\b")
# Fallback: línea de sesión del comando `/usage` ("resets Jun 22, 1:30pm").
USAGE_CLI_SESSION_RE = re.compile(
    r"Current session:\s*([\d.]+)%.*?resets\s+([A-Z][a-z]{2})\s+(\d{1,2}),\s*"
    r"(\d{1,2}):(\d{2})\s*(am|pm)",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def build_env():
    """Copia el entorno actual sin ANTHROPIC_API_KEY, para forzar el uso del
    crédito de la suscripción en lugar de la facturación por API."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def build_challenge():
    """Genera una mini cuenta con números aleatorios.

    Única en cada corrida -> ninguna respuesta puede venir de caché, así que la
    request llega de verdad al modelo y arranca la ventana. Y es verificable: si
    el número vuelve correcto, el modelo realmente procesó (no short-circuit).
    """
    a = random.randint(100, 999)
    b = random.randint(100, 999)
    prompt = f"Cuanto es {a}+{b}? Responde solo el numero, sin nada mas."
    return prompt, a + b


def now_ts():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg):
    print(f"[{now_ts()}] {msg}")


def warn(msg):
    print(f"[{now_ts()}] WARN: {msg}", file=sys.stderr)


def err(msg):
    print(f"[{now_ts()}] ERROR: {msg}", file=sys.stderr)


# Sentinelas para distinguir un fallo transitorio (conviene reintentar) de uno
# fatal (no tiene sentido reintentar, p. ej. falta el binario) o de un límite.
class TransientError(Exception):
    pass


class FatalError(Exception):
    pass


class PlanLimitError(Exception):
    """Límite de plan alcanzado (ventana en cooldown).

    reset_epoch: epoch (segundos) del reset si lo pudimos extraer, o None.
    raw: el detalle crudo (stdout+stderr) para dejarlo en el log.
    """

    def __init__(self, reset_epoch, raw):
        super().__init__("usage limit reached")
        self.reset_epoch = reset_epoch
        self.raw = raw


# --------------------------------------------------------------------------- #
# Consulta del uso real (server-side, gratis)
# --------------------------------------------------------------------------- #

def read_oauth_token():
    """Lee el accessToken OAuth de la suscripción desde el credentials.json.
    Devuelve (token, expires_at_ms) o (None, None) si no se puede."""
    try:
        with open(CREDENTIALS_PATH) as fh:
            data = json.load(fh)
        oauth = data.get("claudeAiOauth") or {}
        return oauth.get("accessToken"), oauth.get("expiresAt")
    except Exception as exc:
        warn(f"no pude leer credenciales OAuth ({CREDENTIALS_PATH}): {exc}")
        return None, None


def force_token_refresh():
    """Fuerza un refresh del token corriendo `claude -p /usage` (gratis: no gasta
    tokens ni ventana). El CLI renueva el accessToken y reescribe credentials.json."""
    try:
        subprocess.run(
            [CLAUDE_BIN, "-p", "/usage", "--output-format", "json"],
            env=build_env(), capture_output=True, text=True, timeout=TIMEOUT,
        )
    except Exception as exc:
        warn(f"no pude forzar refresh del token vía CLI: {exc}")


def _iso_to_epoch(value):
    """Convierte un ISO 8601 (con tz) a epoch segundos. None si no se puede."""
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def _window(node):
    """Normaliza un nodo {utilization, resets_at} del endpoint."""
    node = node or {}
    return {
        "utilization": float(node.get("utilization") or 0.0),
        "resets_at": _iso_to_epoch(node.get("resets_at")),
    }


def query_usage(allow_refresh=True):
    """Consulta GET /api/oauth/usage y devuelve {'five_hour':..., 'seven_day':...}
    con resets_at en epoch. None si falla (red/endpoint). Maneja 401 refrescando
    el token una vez."""
    token, _ = read_oauth_token()
    if not token:
        return None

    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": "wakeUpClaude/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and allow_refresh:
            warn("usage 401: token vencido, fuerzo refresh y reintento")
            force_token_refresh()
            return query_usage(allow_refresh=False)
        warn(f"usage HTTP {exc.code}: {exc.reason}")
        return None
    except Exception as exc:
        warn(f"no pude consultar el uso: {exc}")
        return None

    return {
        "five_hour": _window(data.get("five_hour")),
        "seven_day": _window(data.get("seven_day")),
    }


def query_usage_via_cli():
    """Fallback: si el endpoint está caído, parsea el texto de `claude -p /usage`
    (también gratis). Solo recupera el reset de la SESIÓN de 5h (best-effort)."""
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "/usage", "--output-format", "json"],
            env=build_env(), capture_output=True, text=True, timeout=TIMEOUT,
        )
        result = (json.loads(proc.stdout).get("result") or "")
    except Exception as exc:
        warn(f"fallback /usage por CLI falló: {exc}")
        return None

    m = USAGE_CLI_SESSION_RE.search(result)
    if not m:
        warn("fallback /usage: no pude parsear la línea de sesión")
        return None

    pct, mon, day, hh, mm, ap = m.groups()
    month = _MONTHS.get(mon.lower())
    if not month:
        return None
    hour = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
    now = datetime.now().astimezone()
    try:
        dt = now.replace(month=month, day=int(day), hour=hour, minute=int(mm),
                         second=0, microsecond=0)
    except ValueError:
        return None
    # El texto no trae año: si la fecha quedó muy en el pasado, es del año que viene.
    epoch = dt.timestamp()
    if epoch < now.timestamp() - 86400:
        epoch = dt.replace(year=dt.year + 1).timestamp()

    log("usando fallback /usage por CLI (endpoint no disponible)")
    return {
        "five_hour": {"utilization": float(pct), "resets_at": epoch},
        "seven_day": {"utilization": 0.0, "resets_at": None},
    }


# --------------------------------------------------------------------------- #
# Agendado con `at` (dedupe idempotente vía marcador)
# --------------------------------------------------------------------------- #

def _our_at_jobs():
    """Devuelve los ids de jobs de `at` que creamos nosotros (por el marcador)."""
    if not shutil.which("at"):
        return []
    try:
        listing = subprocess.run(["atq"], capture_output=True, text=True).stdout
    except Exception:
        return []
    ours = []
    for line in listing.splitlines():
        parts = line.split()
        if not parts:
            continue
        jid = parts[0]
        try:
            content = subprocess.run(["at", "-c", jid], capture_output=True,
                                     text=True).stdout
        except Exception:
            continue
        if AT_MARKER in content:
            ours.append(jid)
    return ours


def clear_our_at_jobs():
    """Borra nuestros jobs de `at` pendientes, para no duplicar al reagendar."""
    for jid in _our_at_jobs():
        try:
            subprocess.run(["atrm", jid], capture_output=True, text=True)
        except Exception:
            pass


def schedule_at(target, reason):
    """Agenda con `at` UN job que vuelve a correr este script (modo poller) en el
    instante `target` (epoch). Limpia primero los nuestros: queda exactamente uno.
    Devuelve True si quedó agendado."""
    now = time.time()
    if target <= now:
        target = now + LIMIT_MARGIN_SEC

    clear_our_at_jobs()

    if not shutil.which("at"):
        warn("`at` no disponible: no puedo agendar el disparo preciso "
             f"({reason}); dependo del próximo poll del cron.")
        return False

    timestr = datetime.fromtimestamp(target).strftime("%Y%m%d%H%M.%S")
    when = datetime.fromtimestamp(target).astimezone().isoformat(timespec="seconds")
    # El marcador va como comentario para que `at -c` lo muestre y lo podamos
    # reconocer. El job corre el script sin args (modo poller).
    inner = (f"#{AT_MARKER}\n"
             f"{shlex.quote(sys.executable)} {shlex.quote(SCRIPT_PATH)} "
             f">> {shlex.quote(LOG_PATH)} 2>&1\n")
    try:
        proc = subprocess.run(["at", "-t", timestr], input=inner, text=True,
                              capture_output=True)
    except Exception as exc:
        warn(f"no se pudo invocar at: {exc}")
        return False

    if proc.returncode != 0:
        warn(f"at devolvió rc={proc.returncode}: {proc.stderr.strip()}")
        return False

    log(f"próximo wake agendado para {when} ({reason}). {proc.stderr.strip()}")
    return True


# --------------------------------------------------------------------------- #
# El wake propiamente dicho (abre/renueva la ventana)
# --------------------------------------------------------------------------- #

def extract_reset_epoch(text):
    """Busca el epoch (segundos) del reset en el texto del error. None si no
    aparece. Normaliza milisegundos a segundos."""
    for rx in (RESET_FIELD_RE, RESET_PIPE_RE):
        m = rx.search(text or "")
        if m:
            v = int(m.group(1))
            if v > 10_000_000_000:  # venía en milisegundos
                v //= 1000
            return v
    return None


def classify_and_raise(msg, proc, raw):
    """Decide si el fallo es límite de plan (esperar al reset) o transitorio
    (reintentar), y lanza la excepción apropiada. Incluye stdout Y stderr porque
    claude -p escribe el envelope de error en stdout."""
    detail = (f"{msg}\n"
              f"  stdout: {proc.stdout.strip()}\n"
              f"  stderr: {proc.stderr.strip()}")
    if USAGE_LIMIT_RE.search(raw):
        raise PlanLimitError(extract_reset_epoch(raw), detail)
    raise TransientError(detail)


def attempt_wake(prompt, expected):
    """Una corrida de claude que abre/renueva la ventana y verifica el resultado.
    Devuelve el string de resultado, o lanza Transient/Fatal/PlanLimitError."""
    cmd = [CLAUDE_BIN, "-p", prompt, "--model", MODEL, "--output-format", "json"]

    try:
        proc = subprocess.run(cmd, env=build_env(), capture_output=True,
                              text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        raise FatalError(f"no se encontró el binario claude en {CLAUDE_BIN}")
    except subprocess.TimeoutExpired:
        raise TransientError(f"timeout de {TIMEOUT}s esperando a claude")

    raw = f"{proc.stdout or ''}\n{proc.stderr or ''}"

    if proc.returncode != 0:
        classify_and_raise(f"claude devolvió returncode={proc.returncode}", proc, raw)

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        classify_and_raise("no se pudo parsear el JSON de salida", proc, raw)

    if envelope.get("is_error"):
        classify_and_raise(
            f"is_error=True (api_error_status={envelope.get('api_error_status')}) "
            f"-> {envelope.get('result')}", proc, raw)

    result = (envelope.get("result") or "").strip()

    # Verificación: el resultado debe contener la suma esperada.
    digits = re.search(r"-?\d+", result)
    answer = int(digits.group()) if digits else None
    if answer != expected:
        warn(f"sesión despertada pero la respuesta no coincide "
             f"(esperado {expected}, recibido {result!r})")

    return result


def do_wake():
    """Despierta la ventana con reintentos ante errores transitorios.

    Devuelve el string de resultado en éxito. Ante un límite de plan (carrera:
    pegamos justo el borde), agenda el reintento al reset y propaga PlanLimitError
    para que el caller NO siga agendando. Lanza FatalError/TransientError si no."""
    prompt, expected = build_challenge()
    for attempt in range(RETRIES + 1):
        try:
            return attempt_wake(prompt, expected)
        except PlanLimitError as exc:
            tgt = (exc.reset_epoch or time.time() + LIMIT_COOLDOWN_MIN * 60)
            warn(f"LÍMITE DE PLAN durante el wake:\n{exc.raw}")
            schedule_at(tgt + LIMIT_MARGIN_SEC, "límite alcanzado durante el wake")
            raise
        except TransientError as exc:
            last = attempt == RETRIES
            print(f"[{now_ts()}] {'ERROR' if last else 'WARN'}: intento "
                  f"{attempt + 1}/{RETRIES + 1} falló -> {exc}", file=sys.stderr)
            if last:
                raise
            wait = BACKOFF_BASE * (2 ** attempt)
            log(f"reintentando en {wait:.0f}s...")
            time.sleep(wait)
    raise TransientError("se agotaron los reintentos del wake")


# --------------------------------------------------------------------------- #
# Orquestación: una corrida del poller
# --------------------------------------------------------------------------- #

def poll_once():
    now = time.time()

    usage = query_usage() or query_usage_via_cli()
    if not usage:
        err("no pude determinar el estado de uso (endpoint y CLI caídos); "
            f"reintento en ~{RETRY_SOON_SEC}s.")
        schedule_at(now + RETRY_SOON_SEC, "reintento: no pude leer el uso")
        return 1

    five = usage["five_hour"]
    week = usage["seven_day"]

    # Caso D: semanal agotado. Despertar la ventana de 5h no sirve; agendamos al
    # reset semanal (días) y nos frenamos.
    if week["resets_at"] and week["utilization"] >= WEEKLY_BLOCK_PCT:
        when = datetime.fromtimestamp(week["resets_at"]).astimezone().isoformat(timespec="seconds")
        log(f"CAP SEMANAL agotado ({week['utilization']:.0f}%). No tiene sentido "
            f"despertar la ventana de 5h hasta el reset semanal ({when}).")
        schedule_at(week["resets_at"] + LIMIT_MARGIN_SEC, "reset semanal")
        return 0

    reset = five["resets_at"]

    # Caso C: no hay ventana de 5h activa -> hay un hueco AHORA -> despertar ya.
    if not reset or reset <= now:
        log(f"sin ventana de 5h activa (uso 5h={five['utilization']:.0f}%, "
            f"semanal={week['utilization']:.0f}%); despierto ahora.")
        try:
            result = do_wake()
        except FatalError as exc:
            err(str(exc))
            return 1
        except PlanLimitError:
            return 0  # do_wake ya agendó al reset
        except TransientError:
            err(f"no pude despertar la ventana; reintento en ~{RETRY_SOON_SEC}s.")
            schedule_at(now + RETRY_SOON_SEC, "reintento: el wake falló")
            return 1

        log(f"OK: ventana despertada y verificada. Respuesta: {result!r}")
        # Re-consultamos para conocer el nuevo reset y agendar el próximo wake.
        fresh = query_usage()
        new_reset = (fresh or {}).get("five_hour", {}).get("resets_at")
        target = new_reset or (time.time() + WINDOW_SEC)
        schedule_at(target + LIMIT_MARGIN_SEC, "fin de la ventana recién abierta")
        return 0

    # Casos A/B: la ventana de 5h ya está abierta (con o sin crédito). No hace
    # falta despertar; solo (re)agendamos el wake para cuando se cierre.
    when = datetime.fromtimestamp(reset).astimezone().isoformat(timespec="seconds")
    log(f"ventana de 5h activa (uso 5h={five['utilization']:.0f}%, "
        f"semanal={week['utilization']:.0f}%); cierra {when}.")
    schedule_at(reset + LIMIT_MARGIN_SEC, "fin de la ventana de 5h activa")
    return 0


if __name__ == "__main__":
    sys.exit(poll_once())
