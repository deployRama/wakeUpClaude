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
import shutil
import subprocess
import sys
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


def main():
    ts = datetime.now().astimezone().isoformat(timespec="seconds")

    prompt, expected = build_challenge()

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
        print(f"[{ts}] ERROR: no se encontró el binario claude en {CLAUDE_BIN}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print(f"[{ts}] ERROR: timeout de {TIMEOUT}s esperando a claude", file=sys.stderr)
        return 1

    # returncode != 0 es señal de error por sí solo.
    if proc.returncode != 0:
        print(f"[{ts}] ERROR: claude devolvió returncode={proc.returncode}", file=sys.stderr)
        print(f"[{ts}] stderr: {proc.stderr.strip()}", file=sys.stderr)
        return proc.returncode

    # Parseo robusto del envelope JSON.
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"[{ts}] ERROR: no se pudo parsear el JSON de salida", file=sys.stderr)
        print(f"[{ts}] stdout: {proc.stdout.strip()}", file=sys.stderr)
        return 1

    if envelope.get("is_error"):
        print(f"[{ts}] ERROR: claude reportó is_error=True -> {envelope.get('result')}", file=sys.stderr)
        return 1

    result = envelope.get("result", "").strip()

    # Verificamos que el modelo realmente procesó: el resultado debe contener
    # la suma esperada. Si no coincide, la request llegó igual (la ventana
    # arrancó), pero lo logueamos como advertencia para enterarnos.
    digits = re.search(r"-?\d+", result)
    answer = int(digits.group()) if digits else None
    if answer == expected:
        print(f"[{ts}] OK: sesión despertada y verificada. Respuesta: {result!r}")
    else:
        print(f"[{ts}] WARN: sesión despertada pero la respuesta no coincide "
              f"(esperado {expected}, recibido {result!r})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
