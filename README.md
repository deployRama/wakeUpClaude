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

Agregá (fijando la zona horaria por si la VPS está en UTC):

```cron
CRON_TZ=America/Argentina/Buenos_Aires
0 4 * * * /usr/bin/python3 /ruta/a/wakeUpClaude/wake_up_claude.py >> /ruta/a/wakeUpClaude/wake.log 2>&1
```

- `CRON_TZ` hace que el `0 4` sea 4am Argentina sin importar la TZ del sistema.
  (Soportado por cron de Linux/Vixie y systemd; si tu cron no lo soporta y la
  VPS está en UTC, usá `0 7 * * *`, que es 4am ART = 07:00 UTC.)
- El `>> wake.log 2>&1` deja registro de cada corrida y de errores.

## Cómo verificar que cuenta contra el Max y no contra la API

- En la salida JSON de `claude -p`, una corrida exitosa con suscripción no
  reporta `is_error`.
- Revisá tu consumo en la cuenta: la consulta diaria debería aparecer como uso
  de la suscripción, no como cargo de API.
