# wakeUpClaude

Script para "despertar" la ventana de sesión de 5 horas del plan **Max** todos los
días a las 4am hora de Argentina, vía cron en una VPS.

Manda una consulta mínima a Claude Code en modo headless usando el crédito de la
suscripción (no la API).

## Requisitos en la VPS

1. **Instalar el CLI de Claude Code.**
2. **Login una sola vez con la suscripción**, de forma interactiva:
   ```bash
   claude
   # o
   claude login
   ```
   La auth queda guardada en disco y la reutiliza el modo headless.
3. Verificá la ruta del binario:
   ```bash
   which claude
   ```
   Si no es `/home/ramiro/.local/bin/claude`, ajustá `CLAUDE_BIN` en el script
   o exportá la variable de entorno `CLAUDE_BIN`.
4. **Instalar `at`** (para reprogramar el reintento cuando se topa con el límite
   de plan; ver más abajo):
   ```bash
   sudo apt-get update && sudo apt-get install -y at
   sudo systemctl enable --now atd     # enable = arranca en cada boot; --now = ya
   ```
   Verificá que quede permanente (clave para que sobreviva a reinicios de la VPS):
   ```bash
   systemctl is-enabled atd   # debe decir: enabled
   systemctl is-active atd    # debe decir: active
   ```
   Si `at` no está, el script igual funciona: cae a un fallback que duerme en
   proceso (menos robusto ante reboots).

> **Importante:** NO debe estar definida `ANTHROPIC_API_KEY` para esta corrida.
> El script ya la excluye del entorno del subproceso, así que aunque esté en tu
> `.bashrc` no te va a cobrar por API. Pero si querés, dejala fuera del cron.

## Probarlo a mano

```bash
python3 wake_up_claude.py
```

Salida esperada:

```
[2026-06-19T04:00:01-03:00] OK: sesión despertada y verificada. Respuesta: '1098'
```

El script manda una mini cuenta con números aleatorios (única en cada corrida,
así no puede venir de caché) y verifica que el resultado sea correcto. Eso
garantiza que la request realmente llegó al modelo y arrancó la ventana de 5hs,
gastando muy pocos tokens.

## Configurar el cron

Editá el crontab:

```bash
crontab -e
```

Agregá:

```cron
0 4 * * * /usr/bin/python3 /ruta/a/wakeUpClaude/wake_up_claude.py >> /ruta/a/wakeUpClaude/wake.log 2>&1
```

- La VPS ya está en hora Argentina, así que `0 4` es directamente las 4am ART;
  no hace falta `CRON_TZ` ni convertir a UTC.
- El `>> wake.log 2>&1` deja registro de cada corrida y de errores.

> Si alguna vez movés esto a una máquina en otra zona horaria (p. ej. UTC),
> agregá `CRON_TZ=America/Argentina/Buenos_Aires` arriba de la línea, o ajustá
> la hora a mano (4am ART = 07:00 UTC).

## Qué pasa si falla

El script distingue dos tipos de fallo:

- **Transitorio** (overload, hipo de red, timeout, JSON roto): reintenta en el
  acto con backoff exponencial. Controlable con `WAKE_RETRIES` (default 2) y
  `WAKE_BACKOFF_BASE` (default 5s).
- **Límite de plan** (`usage limit reached`: la ventana está en cooldown):
  no sirve reintentar ya. El script extrae el instante de reset del envelope,
  **agenda un único reintento con `at`** para reset + margen, y sale. Cuando el
  job de `at` corre, vuelve a despertar la ventana ni bien se reabre.

En **cualquier** error, el log incluye el envelope crudo completo (stdout +
stderr), así nunca te quedás sin saber qué pasó.

Variables de entorno del manejo de límite:

| Variable | Default | Qué hace |
|---|---|---|
| `WAKE_LIMIT_MARGIN_SEC` | 120 | Segundos a sumar tras el reset antes de reintentar. |
| `WAKE_LIMIT_COOLDOWN_MIN` | 60 | Cooldown fijo si NO se pudo extraer el epoch de reset. |
| `WAKE_RETRY_MAX_DEPTH` | 6 | Tope de reintentos encadenados por límite (anti-loop). |
| `WAKE_SLEEP_CAP_MIN` | 360 | Tope del sleep del fallback si no hay `at`. |
| `WAKE_LOG` | `wake.log` del proyecto | Adónde escribe el reintento agendado por `at`. |

Para ver reintentos agendados: `atq` (y `at -c <job>` para ver el contenido).

> Nota: el reset sale de un header HTTP (`anthropic-ratelimit-unified-reset`). Si
> alguna versión del CLI no lo expone en el JSON de `-p`, el script cae al
> cooldown fijo y el re-agendado se autocorrige. La primera vez que pase de
> verdad, el envelope crudo en el log confirma el formato exacto.

## Cómo verificar que cuenta contra el Max y no contra la API

- En la salida JSON de `claude -p`, una corrida exitosa con suscripción no
  reporta `is_error`.
- Revisá tu consumo en la cuenta: la consulta diaria debería aparecer como uso
  de la suscripción, no como cargo de API.
