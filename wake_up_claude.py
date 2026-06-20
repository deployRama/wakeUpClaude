#!/usr/bin/env python3
"""
wake_up_claude.py

Envía una consulta mínima a Claude Code en modo headless para "despertar" la
ventana de sesión de 5 horas del plan Max. Pensado para correr por cron.

Regla de oro: `claude -p` decide cómo cobrar según el entorno.
  - Si ANTHROPIC_API_KEY está en el env  -> cobra por API (NO sirve para el Max).
  - Si la borramos del env del subproceso -> usa el crédito de la suscripción.

Por eso copiamos os.environ excluyendo ANTHROPIC_API_KEY y se lo pasamos a
subprocess.run(..., env=env).

Requisito previo en la VPS:
  - Tener instalado el CLI de Claude Code.
  - Haber hecho login una vez con la suscripción (claude interactivo / claude login).
    Esa auth queda guardada en disco y la usa el modo headless.
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

    # Fallback: que falle con un mensaje claro en main().
    return "claude"


CLAUDE_BIN = resolve_claude_bin()

# Modelo a usar: haiku es el más barato y para "despertar" la ventana alcanza
# de sobra. El alias "haiku" resuelve al Haiku más reciente (claude-haiku-4-5).
MODEL = os.environ.get("WAKE_MODEL", "haiku")

# Timeout defensivo (segundos) para que el cron no quede colgado.
TIMEOUT = 120

# Reintentos para sobrevivir a errores transitorios de claude -p (overloaded,
# hipos de red, refresh de auth puntual). El total de intentos es 1 + RETRIES.
RETRIES = int(os.environ.get("WAKE_RETRIES", "2"))
# Backoff base (segundos). La espera es BACKOFF_BASE * 2**intento.
BACKOFF_BASE = float(os.environ.get("WAKE_BACKOFF_BASE", "5"))

# --- Manejo del límite de plan (cooldown de la ventana Max) ---
# Cuando claude devuelve "usage limit reached" (distinto del overload), la
# request rebota hasta que la ventana se reabre. En vez de reintentar en vano,
# agendamos UN reintento con `at` para justo después del reset.
#
# Margen (segundos) a sumar al instante de reset, para no pegarle en el borde.
LIMIT_MARGIN_SEC = int(os.environ.get("WAKE_LIMIT_MARGIN_SEC", "120"))
# Cooldown fijo (minutos) que usamos SOLO si no logramos extraer el epoch de
# reset del envelope. El re-agendado se autocorrige si sigue limitado.
LIMIT_COOLDOWN_MIN = int(os.environ.get("WAKE_LIMIT_COOLDOWN_MIN", "60"))
# Tope de re-agendados encadenados, para no loopear indefinidamente.
MAX_RETRY_DEPTH = int(os.environ.get("WAKE_RETRY_MAX_DEPTH", "6"))
# Tope de sleep (segundos) del fallback en proceso si `at` no estuviera.
SLEEP_CAP_SEC = int(os.environ.get("WAKE_SLEEP_CAP_MIN", "360")) * 60
# Adónde redirige su salida el reintento agendado por `at` (que corre sin la
# tubería del cron). Por defecto, el mismo wake.log del proyecto.
LOG_PATH = os.environ.get(
    "WAKE_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "wake.log"),
)

# El texto que delata un límite de plan (no un overload transitorio).
USAGE_LIMIT_RE = re.compile(r"usage limit reached", re.IGNORECASE)
# El epoch de reset, ya sea como campo JSON ("resetsAt":1718...) o tras un "|".
RESET_FIELD_RE = re.compile(r'resetsAt"?\s*[:=]\s*"?(\d{10,13})', re.IGNORECASE)
RESET_PIPE_RE = re.compile(r"\|\s*(\d{10,13})\b")


def build_env():
    """Copia el entorno actual pero sin ANTHROPIC_API_KEY, para forzar el uso
    del crédito de la suscripción en lugar de la facturación por API."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def build_challenge():
    """Genera una mini cuenta con números aleatorios.

    Sirve para dos cosas:
      - Cada corrida es ÚNICA -> ninguna respuesta puede venir de caché, así que
        garantizamos que la request realmente llega al modelo y arranca la ventana.
      - La respuesta es verificable: si el número vuelve correcto, el modelo
        efectivamente procesó la consulta (no fue un short-circuit).
    Y sigue siendo trivial en tokens.
    """
    a = random.randint(100, 999)
    b = random.randint(100, 999)
    prompt = f"Cuanto es {a}+{b}? Responde solo el numero, sin nada mas."
    return prompt, a + b


def now_ts():
    return datetime.now().astimezone().isoformat(timespec="seconds")


# Sentinela para distinguir un fallo transitorio (conviene reintentar) de uno
# fatal (no tiene sentido reintentar, p. ej. falta el binario).
class TransientError(Exception):
    pass


class FatalError(Exception):
    pass


class PlanLimitError(Exception):
    """Límite de plan alcanzado (ventana en cooldown). No tiene sentido
    reintentar de inmediato: hay que esperar al reset.

    reset_epoch: epoch (segundos) del reset si lo pudimos extraer, o None.
    raw: el detalle crudo (stdout+stderr) para dejarlo en el log.
    """

    def __init__(self, reset_epoch, raw):
        super().__init__("usage limit reached")
        self.reset_epoch = reset_epoch
        self.raw = raw


def extract_reset_epoch(text):
    """Busca el epoch (segundos) del reset en el texto del error. Devuelve None
    si no aparece. Normaliza milisegundos a segundos."""
    for rx in (RESET_FIELD_RE, RESET_PIPE_RE):
        m = rx.search(text or "")
        if m:
            v = int(m.group(1))
            if v > 10_000_000_000:  # viene en milisegundos
                v //= 1000
            return v
    return None


def classify_and_raise(msg, proc, raw):
    """Decide si el fallo es un límite de plan (esperar al reset) o un error
    transitorio (reintentar con backoff), y lanza la excepción apropiada.

    En ambos casos el detalle incluye stdout Y stderr completos, porque
    claude -p escribe el grueso del error (el envelope JSON) en stdout."""
    detail = (f"{msg}\n"
              f"  stdout: {proc.stdout.strip()}\n"
              f"  stderr: {proc.stderr.strip()}")
    if USAGE_LIMIT_RE.search(raw):
        raise PlanLimitError(extract_reset_epoch(raw), detail)
    raise TransientError(detail)


def attempt_wake(prompt, expected):
    """Hace una corrida de claude y verifica el resultado.

    Devuelve el string de resultado en caso de éxito. Lanza TransientError si
    falla de una forma que vale la pena reintentar, o FatalError si no.

    Importante: ante CUALQUIER error logueamos tanto stderr como stdout, porque
    claude -p suele escribir el detalle del error (especialmente el envelope
    JSON con is_error) en stdout, no en stderr.
    """
    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--model", MODEL,
        "--output-format", "json",
    ]

    try:
        proc = subprocess.run(
            cmd,
            env=build_env(),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except FileNotFoundError:
        raise FatalError(f"no se encontró el binario claude en {CLAUDE_BIN}")
    except subprocess.TimeoutExpired:
        # Un timeout puede ser un cuelgue puntual -> reintentamos.
        raise TransientError(f"timeout de {TIMEOUT}s esperando a claude")

    raw = f"{proc.stdout or ''}\n{proc.stderr or ''}"

    # returncode != 0 es señal de error por sí solo.
    if proc.returncode != 0:
        classify_and_raise(f"claude devolvió returncode={proc.returncode}", proc, raw)

    # Parseo robusto del envelope JSON.
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        classify_and_raise("no se pudo parsear el JSON de salida", proc, raw)

    if envelope.get("is_error"):
        classify_and_raise(
            f"is_error=True (api_error_status={envelope.get('api_error_status')}) "
            f"-> {envelope.get('result')}",
            proc, raw,
        )

    result = (envelope.get("result") or "").strip()

    # Verificamos que el modelo realmente procesó: el resultado debe contener
    # la suma esperada. Si no coincide, la request llegó igual (la ventana
    # arrancó), pero lo logueamos como advertencia para enterarnos.
    digits = re.search(r"-?\d+", result)
    answer = int(digits.group()) if digits else None
    if answer != expected:
        print(f"[{now_ts()}] WARN: sesión despertada pero la respuesta no coincide "
              f"(esperado {expected}, recibido {result!r})", file=sys.stderr)

    return result


def reexec_self(depth):
    """Re-lanza este mismo script incrementando la profundidad de reintento.
    Reemplaza el proceso actual (no retorna)."""
    env = dict(os.environ)
    env["WAKE_RETRY_DEPTH"] = str(depth + 1)
    os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)], env)


def schedule_with_at(target, depth):
    """Agenda con `at` UN reintento de este script para el instante `target`
    (epoch en segundos). Devuelve True si quedó agendado."""
    if not shutil.which("at"):
        return False

    timestr = datetime.fromtimestamp(target).strftime("%Y%m%d%H%M.%S")
    # Inline WAKE_RETRY_DEPTH y redirigimos al log, porque el job de `at` corre
    # sin la tubería del cron. El resto del entorno lo preserva el propio `at`.
    inner = (f"WAKE_RETRY_DEPTH={depth + 1} "
             f"{shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))} "
             f">> {shlex.quote(LOG_PATH)} 2>&1")
    try:
        proc = subprocess.run(
            ["at", "-t", timestr],
            input=inner, text=True, capture_output=True,
        )
    except Exception as exc:  # pragma: no cover - defensivo
        print(f"[{now_ts()}] WARN: no se pudo invocar at: {exc}", file=sys.stderr)
        return False

    if proc.returncode != 0:
        print(f"[{now_ts()}] WARN: at devolvió rc={proc.returncode}: {proc.stderr.strip()}",
              file=sys.stderr)
        return False

    # `at` informa el job id por stderr ("job N at ...").
    print(f"[{now_ts()}] reintento agendado con at para {timestr} "
          f"(depth={depth + 1}). {proc.stderr.strip()}")
    return True


def handle_plan_limit(exc, depth):
    """Ante un límite de plan: loguea el envelope crudo y agenda un reintento
    para después del reset (con `at`; fallback a sleep en proceso)."""
    # Red de seguridad: dejamos el envelope crudo para conocer el formato real.
    print(f"[{now_ts()}] LÍMITE DE PLAN detectado:\n{exc.raw}", file=sys.stderr)

    if depth >= MAX_RETRY_DEPTH:
        print(f"[{now_ts()}] ERROR: alcanzado el máximo de reintentos por límite "
              f"({MAX_RETRY_DEPTH}); abandono hasta la próxima corrida del cron.",
              file=sys.stderr)
        return 1

    now = time.time()
    if exc.reset_epoch:
        target = exc.reset_epoch + LIMIT_MARGIN_SEC
        print(f"[{now_ts()}] reset informado: epoch={exc.reset_epoch}", file=sys.stderr)
    else:
        target = now + LIMIT_COOLDOWN_MIN * 60
        print(f"[{now_ts()}] WARN: no pude extraer el epoch de reset del envelope; "
              f"uso cooldown fijo de {LIMIT_COOLDOWN_MIN}min.", file=sys.stderr)

    # Si el reset ya pasó (reloj desfasado, dato viejo), reintentamos enseguida.
    if target <= now:
        target = now + LIMIT_MARGIN_SEC

    when = datetime.fromtimestamp(target).astimezone().isoformat(timespec="seconds")
    print(f"[{now_ts()}] próximo intento previsto para {when}", file=sys.stderr)

    if schedule_with_at(target, depth):
        return 0

    # Fallback sin `at`: dormimos (con tope) y re-ejecutamos.
    wait = max(0, min(target - time.time(), SLEEP_CAP_SEC))
    print(f"[{now_ts()}] WARN: `at` no disponible; duermo {wait:.0f}s en proceso "
          f"y reintento (instalá `at` para algo más robusto).", file=sys.stderr)
    time.sleep(wait)
    reexec_self(depth)  # no retorna


def main():
    prompt, expected = build_challenge()
    depth = int(os.environ.get("WAKE_RETRY_DEPTH", "0"))

    for attempt in range(RETRIES + 1):
        try:
            result = attempt_wake(prompt, expected)
        except FatalError as exc:
            print(f"[{now_ts()}] ERROR: {exc}", file=sys.stderr)
            return 1
        except PlanLimitError as exc:
            return handle_plan_limit(exc, depth)
        except TransientError as exc:
            last = attempt == RETRIES
            level = "ERROR" if last else "WARN"
            print(f"[{now_ts()}] {level}: intento {attempt + 1}/{RETRIES + 1} falló -> {exc}",
                  file=sys.stderr)
            if last:
                return 1
            wait = BACKOFF_BASE * (2 ** attempt)
            print(f"[{now_ts()}] reintentando en {wait:.0f}s...", file=sys.stderr)
            time.sleep(wait)
            continue

        print(f"[{now_ts()}] OK: sesión despertada y verificada. Respuesta: {result!r}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
