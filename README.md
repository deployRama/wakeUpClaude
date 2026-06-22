# wakeUpClaude

Mantiene **siempre activa** la ventana de sesión de 5 horas del plan **Max**,
con cobertura 24/7 anclada a los resets **reales** del servidor, vía cron en una VPS.

Corre como un **poller cada ~30 min**: consulta gratis el uso real (endpoint
interno de la suscripción, sin gastar tokens ni ventana), y según el estado:

- **Sin ventana de 5h activa** → manda una consulta mínima a Claude Code en
  modo headless (con el crédito de la suscripción, no la API) y **abre la ventana**.
- **Ventana activa** → no despierta; agenda con `at` un único disparo para el
  instante exacto del reset, así reabre la ventana ni bien se cierra.
- **Cap semanal agotado** → agenda el reintento directo al reset semanal.

El disparo preciso lo hace `at`; el poll de 30 min lo mantiene **auto-reparable**
(si se pierde un `at` por un reboot, el siguiente poll lo reagenda). Por eso ya
**no hace falta un cron diario** aparte.

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
4. **Instalar `at`** (para disparar el wake al segundo, en el instante exacto del
   reset; ver más abajo):
   ```bash
   sudo apt-get update && sudo apt-get install -y at
   sudo systemctl enable --now atd     # enable = arranca en cada boot; --now = ya
   ```
   Verificá que quede permanente (clave para que sobreviva a reinicios de la VPS):
   ```bash
   systemctl is-enabled atd   # debe decir: enabled
   systemctl is-active atd    # debe decir: active
   ```
   Si `at` no está, el script igual funciona, pero pierde la precisión: la
   cobertura queda con la granularidad del cron (~30 min) en vez de al segundo.

> **Importante:** NO debe estar definida `ANTHROPIC_API_KEY` para esta corrida.
> El script ya la excluye del entorno del subproceso, así que aunque esté en tu
> `.bashrc` no te va a cobrar por API. Pero si querés, dejala fuera del cron.

## Probarlo a mano

```bash
python3 wake_up_claude.py
```

Salida esperada:

```
[2026-06-22T13:30:02-03:00] OK: ventana despertada y verificada. Respuesta: '1098'
[2026-06-22T13:30:03-03:00] próximo wake agendado para 2026-06-22T18:31:59-03:00 (fin de la ventana recién abierta). job 7 at ...
```

Cuando hay que abrir ventana, el script manda una mini cuenta con números
aleatorios (única en cada corrida, así no puede venir de caché) y verifica que el
resultado sea correcto. Eso garantiza que la request realmente llegó al modelo y
arrancó la ventana de 5hs, gastando muy pocos tokens. La consulta de estado
(`/api/oauth/usage`) no gasta nada.

## Cómo sabe cuándo reabrir (el dato clave)

El reset exacto sale del endpoint interno que usa el propio CLI:

```
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer <accessToken de ~/.claude/.credentials.json>
```

Devuelve JSON autoritativo del **servidor** con `five_hour.resets_at` y
`seven_day.resets_at` (ISO 8601). Es gratis y no consume la ventana.

- Si el `accessToken` está vencido (HTTP 401), el script fuerza un refresh
  corriendo `claude -p "/usage"` (que reescribe el `credentials.json`) y reintenta.
- Si el endpoint estuviera caído, cae a un **fallback** que parsea el texto de
  `claude -p "/usage"` (recupera al menos el reset de la sesión de 5h).

## Configurar el cron

Editá el crontab:

```bash
crontab -e
```

Agregá (un solo poller cada 30 min; **ya no hace falta el cron de las 4am**):

```cron
*/30 * * * * /usr/bin/python3 /ruta/a/wakeUpClaude/wake_up_claude.py >> /ruta/a/wakeUpClaude/wake.log 2>&1
```

- El poll de 30 min solo define cada cuánto se **re-chequea y auto-repara**; el
  wake en sí se dispara al segundo vía `at`, en el instante real del reset.
- El `>> wake.log 2>&1` deja registro de cada corrida y de errores.

> La VPS ya está en hora Argentina. Si la movés a otra zona (p. ej. UTC) no
> importa para el agendado (el script trabaja con epochs absolutos del servidor),
> pero el `at` usa la hora local de la máquina, así que mantené la TZ correcta.

## Qué pasa en cada caso

| Estado (según `/api/oauth/usage`) | Acción |
|---|---|
| **Ventana de 5h activa** (`resets_at` futuro) | No despierta. (Re)agenda **un** `at` para `resets_at + margen`. |
| **Sin ventana de 5h** (`resets_at` nulo/pasado) | **Despierta ya**, re-consulta el nuevo reset y agenda el próximo wake. |
| **Cap semanal agotado** (`seven_day` ≥ umbral) | No despierta (estarías bloqueado igual). Agenda el reintento al **reset semanal**. |
| **No se pudo leer el uso** (endpoint y CLI caídos) | Reintento corto y log; el cron acota igual. |
| **El wake falla** (overload/red/timeout) | Reintenta en el acto con backoff; si agota, reintento corto. |
| **Límite justo durante el wake** (carrera) | Extrae el reset del envelope y agenda al reset. |

En cualquier error el log incluye el detalle crudo (stdout + stderr), así nunca
te quedás sin saber qué pasó.

Variables de entorno:

| Variable | Default | Qué hace |
|---|---|---|
| `WAKE_LIMIT_MARGIN_SEC` | 120 | Segundos a sumar tras el reset antes de reabrir. |
| `WAKE_WEEKLY_BLOCK_PCT` | 100 | % de uso semanal a partir del cual se agenda al reset semanal. |
| `WAKE_RETRY_SOON_SEC` | 300 | Reintento corto si no se pudo leer el uso o el wake falló. |
| `WAKE_RETRIES` / `WAKE_BACKOFF_BASE` | 2 / 5s | Reintentos del wake ante errores transitorios. |
| `WAKE_USAGE_URL` | `…/api/oauth/usage` | Endpoint de uso (override para pruebas). |
| `WAKE_CREDENTIALS` | `~/.claude/.credentials.json` | De dónde lee el token OAuth. |
| `WAKE_LOG` | `wake.log` del proyecto | Adónde escribe el job agendado por `at`. |

Para ver el wake agendado: `atq` (y `at -c <job>` para ver el contenido; los
nuestros llevan el marcador `WAKEUPCLAUDE_AT`).

## Cómo verificar que cuenta contra el Max y no contra la API

- En la salida JSON de `claude -p`, una corrida exitosa con suscripción no
  reporta `is_error`.
- Revisá tu consumo en la cuenta: la consulta diaria debería aparecer como uso
  de la suscripción, no como cargo de API.
