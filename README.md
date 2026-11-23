# 🚀 Bot de explosiones para BingX (Telegram + Railway)

Escanea **todas las monedas de futuros perpetuos USDT de BingX** cada 15 minutos y busca las que están **a punto de explotar** (por arriba o por abajo) durante el día.
Te avisa por **Telegram** y, si lo activas, **opera en BingX** con riesgo controlado.

> ⚠️ **Aviso:** esto es software, no asesoramiento financiero. Los futuros con apalancamiento pueden hacerte perder todo el capital. Empieza en modo `signals`, luego en demo, y solo después con dinero real y riesgo mínimo. Ningún bot garantiza beneficios.

---

## Qué detecta

| Tipo | Significado | ¿Lo opera el bot? |
|---|---|---|
| 🔥 **A PUNTO DE EXPLOTAR** (`LOADED`) | Volatilidad comprimida (squeeze de Bollinger), volumen vivo y precio pegado al borde de su rango de 20 velas. Indica sesgo LONG / SHORT o "dirección abierta" con ambos disparadores. | No, solo avisa |
| 🚀 **EXPLOSIÓN CONFIRMADA** (`TRIGGERED`) | La última vela de 15m **cerró fuera** del rango de 20 velas, viniendo de compresión y con volumen ≥ 1.5× | Sí (si `TRADING_MODE=live` y score ≥ `MIN_SCORE_TRADE`) |

**Score 0-100** = Squeeze (25) + Volumen relativo (25) + Posición/ruptura (20) + Tendencia 15m/1h/VWAP (15) + Margen restante vs ADR diario (10) + RSI (5) − penalización si va contra la tendencia de BTC (−8).

Filtros: liquidez mínima 24h, spread máximo, ADR mínimo (descarta monedas dormidas) y descarta rupturas que ya recorrieron más de 2× su rango diario ("llegas tarde").

El escaneo usa solo **velas cerradas** y dos fases para ir rápido: 1) velas de 15m de todas las monedas líquidas, 2) contexto de 1h (tendencia y ADR) solo para las mejores.

## Gestión del riesgo (modo live)

- Riesgo fijo por operación: `RISK_PCT` % del equity (por defecto 0.5%).
- **SL y TP adjuntos a la misma orden** (SL = 1.5× ATR "normal" de 15m, TP = 2R).
- Máx. posiciones abiertas, cooldown por moneda, no perseguir el precio si ya se alejó.
- Límite de pérdida diaria (3%): si se alcanza, no abre más ese día (UTC).
- Stop por tiempo: cierra a mercado tras `MAX_HOLD_HOURS` (10 h).
- Comando `/closeall SI` para cerrar todo desde Telegram y `/pause` para parar nuevas entradas.

---

## Archivos (todos en la raíz, sin carpetas)

| Archivo | Función |
|---|---|
| `main.py` | Arranque, bucle de escaneo y comandos de Telegram |
| `config.py` | Variables de entorno |
| `scanner.py` | Detección de squeeze/ruptura y scoring |
| `indicators.py` | EMA, RSI, ATR, Bollinger, VWAP |
| `bingx_client.py` | Conexión a BingX (ccxt) |
| `trader.py` | Ejecución, tamaño de posición y riesgo |
| `telegram_bot.py` | Envío de señales y comandos |
| `test_scanner.py` | Pruebas sin red (`python test_scanner.py`) |
| `requirements.txt`, `Procfile`, `railway.json`, `.python-version` | Despliegue en Railway |
| `.env.example`, `.gitignore` | Plantilla de variables y exclusiones |

---

## Puesta en marcha

### 1) Telegram
1. En Telegram habla con **@BotFather** → `/newbot` → copia el **token**.
2. Escribe `/start` a tu bot nuevo. Si aún no has configurado `TELEGRAM_CHAT_ID`, el bot te responderá con tu `chat_id` (necesitas haberlo desplegado antes; también puedes obtenerlo con @userinfobot).

### 2) API de BingX (solo si vas a operar)
1. BingX → Perfil → **API Management** → crear clave.
2. Permisos: **lectura + trading de futuros perpetuos**. **NUNCA actives retiros.**
3. Railway no tiene IP fija en planes estándar, así que normalmente no podrás restringir por IP; razón de más para no dar permiso de retiro.
4. Para probar la ejecución sin riesgo, usa la **cuenta demo (VST)** de BingX con sus propias claves y pon `BINGX_SANDBOX=true`.

### 3) GitHub
Crea un repositorio nuevo y sube **todos los archivos tal cual, en la raíz** (Add file → Upload files). No subas un `.env` con claves reales.

### 4) Railway
1. [railway.com](https://railway.com) → **New Project → Deploy from GitHub repo** → elige el repo.
2. Pestaña **Variables**: copia las de `.env.example` (mínimo `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` y `TRADING_MODE=signals`).
3. Railway detecta Python por `requirements.txt` y ejecuta `python main.py` (es un *worker*, no necesita puerto).
4. En **Logs** deberías ver "Mercados swap USDT cargados" y recibirás un mensaje de arranque en Telegram.

### 5) Camino recomendado (no te saltes pasos)
1. **Días 1-3:** `TRADING_MODE=signals`. Mira si las alertas tienen sentido y qué pasa después con esas monedas.
2. **Después:** `TRADING_MODE=live` + `BINGX_SANDBOX=true` (demo) para comprobar que las órdenes, SL y TP se crean bien en tu cuenta.
3. **Por último:** cuenta real con `RISK_PCT=0.25`–`0.5` y `LEVERAGE` bajo. Sube solo si los resultados lo justifican.

---

## Comandos de Telegram

`/status` · `/scan` (escanea ya) · `/positions` · `/pause` · `/resume` · `/closeall SI` · `/id` · `/help`

Solo responde al `TELEGRAM_CHAT_ID` configurado.

## Ajustes importantes (variables)

Todas están comentadas en `.env.example`. Las más útiles:

- **Más/menos señales:** `MIN_SCORE_ALERT` (65), `MIN_SCORE_TRADE` (75), `MIN_QUOTE_VOLUME_USDT`, `MAX_SYMBOLS`.
- **Riesgo:** `RISK_PCT`, `LEVERAGE`, `MAX_OPEN_POSITIONS`, `DAILY_LOSS_LIMIT_PCT`, `SL_ATR_MULT`, `TP_R_MULT`, `MAX_HOLD_HOURS`.
- **Solo un lado:** `LONG_ENABLED=false` o `SHORT_ENABLED=false`.
- **Más sensible/estricto el squeeze:** `SQUEEZE_PCT_MAX`, `SQUEEZE_PCT_LOADED`, `MIN_RVOL_TRIGGER`.

## Limitaciones conocidas (sé honesto contigo mismo)

- El código se ha probado con datos sintéticos y un exchange simulado (`test_scanner.py`), y revisando el código de `ccxt 4.5.94` para BingX. **No se ha probado contra la API real de BingX**: por eso existe el modo `signals` y el demo.
- Las órdenes usan SL/TP adjuntos con `ccxt` y `HEDGE_MODE=true`. Si tu cuenta está en modo one-way, pon `HEDGE_MODE=false`.
- Sin trailing ni break-even: la salida es SL, TP o tiempo. El PnL exacto de cada cierre no se calcula (solo el equity del día).
- Los umbrales son un punto de partida, **no están optimizados con backtest**. Ajústalos observando las señales reales.
- Si Railway reinicia el proceso, el bot readopta las posiciones abiertas que vea en BingX (con stop por tiempo desde ese momento).
- Los scores no predicen el futuro: miden cuánto se parece una moneda a un patrón de compresión + volumen + ruptura.
