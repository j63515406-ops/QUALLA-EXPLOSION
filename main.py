"""Punto de entrada: escáner + trader + Telegram en un solo proceso (Railway worker)."""
import asyncio
import logging
import time
from datetime import datetime, timezone

from bingx_client import BingX
from config import Config
from scanner import Scanner
from telegram_bot import Telegram, format_candidate, pretty
from trader import Trader

log = logging.getLogger("main")

HELP = (
    "<b>Comandos</b>\n"
    "/status – estado del bot\n"
    "/scan – escanear ahora y ver el top\n"
    "/positions – posiciones abiertas\n"
    "/pause – no abrir más operaciones\n"
    "/resume – reactivar\n"
    "/closeall SI – cerrar todo a mercado\n"
    "/id – tu chat_id"
)


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bx = BingX(cfg)
        self.tg = Telegram(cfg)
        self.scanner = Scanner(cfg, self.bx)
        self.trader = Trader(cfg, self.bx, self.tg)
        self.alerted = {}
        self.last_scan = None
        self.last_heartbeat = time.time()
        self.scan_lock = asyncio.Lock()
        self.tf_seconds = int(self.bx.ex.parse_timeframe(cfg.scan_timeframe))

    # ── Escaneo ──
    def _seconds_to_next(self) -> float:
        now = time.time()
        nxt = (now // self.tf_seconds + 1) * self.tf_seconds + self.cfg.scan_delay
        return max(5.0, nxt - now)

    async def run_scan(self, manual: bool = False):
        async with self.scan_lock:
            cands, stats = await self.scanner.scan()
        self.last_scan = {"time": datetime.now(timezone.utc), "stats": stats, "top": cands[:5]}
        if manual:
            return cands, stats

        cfg, now = self.cfg, time.time()
        for k in [k for k, t in self.alerted.items() if now - t > 24 * 3600]:
            self.alerted.pop(k, None)

        pool = []
        for c in cands:
            if c.score < cfg.min_score_alert or c.direction == "NEUTRAL" and c.kind == "TRIGGERED":
                continue
            key = (c.symbol, c.kind, c.direction)
            last = self.alerted.get(key)
            if last and now - last < cfg.signal_cooldown_min * 60:
                continue
            pool.append(c)
        pool.sort(key=lambda c: (c.kind != "TRIGGERED", -c.score))
        for c in pool[: cfg.max_alerts_per_scan]:
            await self.tg.send(format_candidate(c, cfg))
            self.alerted[(c.symbol, c.kind, c.direction)] = now

        # operar solo las explosiones confirmadas
        for c in [c for c in cands if c.kind == "TRIGGERED"]:
            res = await self.trader.maybe_trade(c)
            log.info("Trade %s %s → %s", c.symbol, c.direction, res)

        # latido
        if cfg.heartbeat_hours > 0 and now - self.last_heartbeat > cfg.heartbeat_hours * 3600:
            self.last_heartbeat = now
            await self.tg.send(self._status_text(short=True))

    async def scan_loop(self):
        await asyncio.sleep(3)
        while True:
            try:
                await self.run_scan()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("scan")
                await self.tg.send(f"⚠️ Error en el escaneo: <code>{str(e)[:300]}</code>")
            await asyncio.sleep(self._seconds_to_next())

    # ── Telegram ──
    def _status_text(self, short: bool = False) -> str:
        cfg, t = self.cfg, self.trader
        mode = "LIVE" if cfg.live else "SIGNALS (solo avisos)"
        if cfg.live and cfg.sandbox:
            mode += " · DEMO"
        lines = [f"🤖 <b>Bot activo</b> · modo {mode}"]
        if cfg.live:
            eq = f"{t.equity:.2f}" if t.equity else "?"
            lines.append(f"Equity {eq} USDT · posiciones {len(t.positions)}/{cfg.max_open_positions}"
                         f"{' · ⏸ PAUSADO' if t.paused else ''}{' · 🛑 límite diario' if t.halted_today else ''}")
        if self.last_scan:
            s = self.last_scan["stats"]
            lines.append(f"Último escaneo {self.last_scan['time'].strftime('%H:%M')} UTC: {s['universe']} monedas · "
                         f"{s['triggered']} explosiones · {s['loaded']} cargadas · BTC {s['btc']} · {s['seconds']}s")
            if not short:
                for c in self.last_scan["top"][:3]:
                    lines.append(f"• {pretty(c.symbol)} {c.kind} {c.direction} {c.score:.0f}")
        return "\n".join(lines)

    async def handle_command(self, cmd: str, args: list):
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/id":
            return f"chat_id: <code>{self.tg.chat_id}</code>"
        if cmd == "/status":
            return self._status_text()
        if cmd == "/pause":
            self.trader.paused = True
            return "⏸ Pausado: seguirás recibiendo señales, pero no se abrirán operaciones."
        if cmd == "/resume":
            self.trader.paused = False
            return "▶️ Reactivado."
        if cmd == "/positions":
            return await self.trader.positions_text()
        if cmd == "/closeall":
            if args and args[0].upper() in ("SI", "SÍ", "YES"):
                return await self.trader.close_all()
            return "Esto cierra TODAS las posiciones a mercado. Confirma con <code>/closeall SI</code>"
        if cmd == "/scan":
            await self.tg.send("🔎 Escaneando…")
            cands, stats = await self.run_scan(manual=True)
            if not cands:
                return f"Sin candidatas ahora ({stats['universe']} monedas analizadas)."
            for c in cands[:6]:
                await self.tg.send(format_candidate(c, self.cfg))
            return f"{stats['universe']} monedas · {stats['triggered']} explosiones · {stats['loaded']} cargadas · {stats['seconds']}s"
        return "Comando no reconocido. /help"

    async def start(self):
        cfg = self.cfg
        for p in cfg.validate():
            log.warning(p)
        await self.bx.load()
        await self.trader.init()
        await self.tg.send(
            f"🚀 <b>Bot de explosiones iniciado</b>\n{self._status_text(short=True)}\n"
            f"Escaneo cada {cfg.scan_timeframe} · score alerta ≥ {cfg.min_score_alert:g} · operar ≥ {cfg.min_score_trade:g}\n"
            f"{HELP}"
        )
        await asyncio.gather(self.scan_loop(), self.trader.monitor_loop(), self.tg.command_loop(self.handle_command))

    async def shutdown(self):
        await self.tg.close()
        await self.bx.close()


async def main():
    cfg = Config()
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    app = App(cfg)
    try:
        await app.start()
    finally:
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
