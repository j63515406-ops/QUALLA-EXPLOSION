"""Configuración del bot: todo se controla con variables de entorno (Railway → Variables)."""
import os


def _s(name, default=""):
    return os.getenv(name, default).strip()


def _f(name, default):
    try:
        return float(os.getenv(name, str(default)).strip())
    except ValueError:
        return float(default)


def _i(name, default):
    try:
        return int(float(os.getenv(name, str(default)).strip()))
    except ValueError:
        return int(default)


def _b(name, default):
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "si", "sí", "on")


def _list(name, default=""):
    return [x.strip().upper() for x in os.getenv(name, default).split(",") if x.strip()]


class Config:
    def __init__(self):
        # ── Telegram ──
        self.telegram_token = _s("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = _s("TELEGRAM_CHAT_ID")

        # ── BingX ──
        self.api_key = _s("BINGX_API_KEY")
        self.api_secret = _s("BINGX_API_SECRET")
        self.sandbox = _b("BINGX_SANDBOX", False)          # True = cuenta demo VST de BingX
        self.hedge_mode = _b("HEDGE_MODE", True)           # BingX usa modo hedge por defecto
        self.margin_mode = _s("MARGIN_MODE", "isolated").lower()

        # ── Modo de operación ──
        # signals = solo avisa por Telegram (RECOMENDADO al empezar) | live = opera en BingX
        self.trading_mode = _s("TRADING_MODE", "signals").lower()
        self.live = self.trading_mode == "live"
        self.long_enabled = _b("LONG_ENABLED", True)
        self.short_enabled = _b("SHORT_ENABLED", True)

        # ── Escáner ──
        self.scan_timeframe = _s("SCAN_TIMEFRAME", "15m")
        self.scan_delay = _i("SCAN_DELAY_SECONDS", 8)          # segundos tras el cierre de vela
        self.candles = _i("CANDLES", 200)
        self.concurrency = _i("CONCURRENCY", 6)
        self.min_quote_volume = _f("MIN_QUOTE_VOLUME_USDT", 3_000_000)
        self.max_symbols = _i("MAX_SYMBOLS", 0)                # 0 = todas las que pasen el filtro
        self.max_spread_pct = _f("MAX_SPREAD_PCT", 0.15)
        self.stage2_top = _i("STAGE2_TOP", 25)
        self.exclude = _list("EXCLUDE_BASES", "USDC,USDT,BUSD,TUSD,FDUSD")
        self.min_adr_pct = _f("MIN_ADR_PCT", 2.0)              # descarta monedas "dormidas"
        self.max_room_ratio = _f("MAX_ROOM_RATIO", 2.0)        # rango de hoy / ADR: si ya explotó, tarde
        self.use_btc_filter = _b("USE_BTC_FILTER", True)

        # umbrales de detección
        self.squeeze_pct_max = _f("SQUEEZE_PCT_MAX", 0.35)     # percentil BB width para "venía comprimida"
        self.squeeze_pct_loaded = _f("SQUEEZE_PCT_LOADED", 0.30)
        self.min_rvol_trigger = _f("MIN_RVOL_TRIGGER", 1.5)
        self.min_rvol_loaded = _f("MIN_RVOL_LOADED", 1.0)
        self.max_edge_dist_atr = _f("MAX_EDGE_DIST_ATR", 0.9)
        self.max_body_atr = _f("MAX_BODY_ATR", 3.0)            # vela de ruptura demasiado grande = tarde

        # ── Alertas ──
        self.min_score_alert = _f("MIN_SCORE_ALERT", 65)
        self.max_alerts_per_scan = _i("MAX_ALERTS_PER_SCAN", 6)
        self.signal_cooldown_min = _i("SIGNAL_COOLDOWN_MIN", 120)
        self.heartbeat_hours = _f("HEARTBEAT_HOURS", 6)

        # ── Trading / riesgo ──
        self.min_score_trade = _f("MIN_SCORE_TRADE", 75)
        self.risk_pct = _f("RISK_PCT", 0.5)                    # % del equity arriesgado por operación
        self.leverage = _i("LEVERAGE", 5)
        self.max_open_positions = _i("MAX_OPEN_POSITIONS", 3)
        self.sl_atr_mult = _f("SL_ATR_MULT", 1.5)
        self.tp_r_mult = _f("TP_R_MULT", 2.0)
        self.min_sl_pct = _f("MIN_SL_PCT", 0.35)
        self.max_sl_pct = _f("MAX_SL_PCT", 3.0)
        self.max_chase_atr = _f("MAX_CHASE_ATR", 1.0)          # no entrar si el precio ya se alejó tanto
        self.max_hold_hours = _f("MAX_HOLD_HOURS", 10)
        self.symbol_cooldown_hours = _f("SYMBOL_COOLDOWN_HOURS", 6)
        self.daily_loss_limit_pct = _f("DAILY_LOSS_LIMIT_PCT", 3.0)
        self.monitor_seconds = _i("MONITOR_SECONDS", 30)

        self.log_level = _s("LOG_LEVEL", "INFO").upper()

    def validate(self):
        problems = []
        if self.live and not (self.api_key and self.api_secret):
            problems.append("TRADING_MODE=live requiere BINGX_API_KEY y BINGX_API_SECRET.")
        if not self.telegram_token:
            problems.append("Falta TELEGRAM_BOT_TOKEN: no se enviarán señales por Telegram.")
        if self.risk_pct > 2:
            problems.append("RISK_PCT > 2%: es un riesgo alto por operación.")
        if self.leverage > 20:
            problems.append("LEVERAGE > 20x: muy peligroso.")
        return problems
