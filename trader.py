"""Ejecución y gestión de riesgo en BingX.

Reglas de seguridad:
  · Solo opera si TRADING_MODE=live. En modo "signals" nunca envía órdenes.
  · Riesgo fijo por operación (RISK_PCT del equity) con SL y TP adjuntos a la orden.
  · Límite de posiciones abiertas, cooldown por moneda y límite de pérdida diaria.
  · Stop por tiempo (MAX_HOLD_HOURS): las explosiones son para el día, no para quedarse atrapado.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from telegram_bot import fmt_price, pretty

log = logging.getLogger("trader")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Trader:
    def __init__(self, cfg, bx, tg):
        self.cfg, self.bx, self.tg = cfg, bx, tg
        self.positions: Dict[str, dict] = {}       # operaciones que el bot sigue
        self.cooldown: Dict[str, datetime] = {}
        self.paused = False                        # /pause manual
        self.halted_today = False                  # límite de pérdida diaria alcanzado
        self.day = now_utc().date()
        self.day_start_equity: Optional[float] = None
        self.equity: Optional[float] = None

    @property
    def live(self) -> bool:
        return self.cfg.live

    # ── Arranque ──
    async def init(self):
        if not self.live:
            return
        await self.bx.set_position_mode()
        self.equity, _ = await self.bx.balance()
        self.day_start_equity = self.equity
        await self._sync(adopt=True)

    # ── Tamaño de posición ──
    def _size(self, equity: float, price: float, risk: float, symbol: str) -> float:
        cfg = self.cfg
        qty = (equity * cfg.risk_pct / 100.0) / risk
        qty = min(qty, equity * cfg.leverage * 0.9 / price)
        qty = float(self.bx.amount_to_precision(symbol, qty))
        lim = self.bx.market(symbol).get("limits", {}) or {}
        min_amt = (lim.get("amount") or {}).get("min")
        min_cost = (lim.get("cost") or {}).get("min")
        if min_amt and qty < min_amt:
            return 0.0
        if min_cost and qty * price < min_cost:
            return 0.0
        return qty

    # ── Entrada ──
    async def maybe_trade(self, c) -> str:
        cfg = self.cfg
        if not self.live:
            return "signals"
        if self.paused or self.halted_today:
            return "paused"
        if c.kind != "TRIGGERED" or c.direction not in ("LONG", "SHORT"):
            return "skip"
        if (c.direction == "LONG" and not cfg.long_enabled) or (c.direction == "SHORT" and not cfg.short_enabled):
            return "disabled"
        if c.score < cfg.min_score_trade:
            return "score"
        if not c.info.get("tradable", True) or c.sl is None or c.tp is None:
            return "sl_wide"
        if c.symbol in self.positions:
            return "open"
        if len(self.positions) >= cfg.max_open_positions:
            return "max_positions"
        until = self.cooldown.get(c.symbol)
        if until and now_utc() < until:
            return "cooldown"
        try:
            return await self._open(c)
        except Exception as e:
            log.exception("Error abriendo %s", c.symbol)
            await self.tg.send(f"⚠️ <b>Error al abrir {pretty(c.symbol)}</b>\n<code>{str(e)[:300]}</code>")
            return "error"

    async def _open(self, c) -> str:
        cfg, bx = self.cfg, self.bx
        price = await bx.last_price(c.symbol)
        # no perseguir: si el precio ya se alejó demasiado del cierre de la señal, se descarta
        if c.direction == "LONG" and price > c.price + cfg.max_chase_atr * c.atr:
            return "chase"
        if c.direction == "SHORT" and price < c.price - cfg.max_chase_atr * c.atr:
            return "chase"

        risk = c.info["risk"]
        if c.direction == "LONG":
            sl, tp = price - risk, price + cfg.tp_r_mult * risk
        else:
            sl, tp = price + risk, price - cfg.tp_r_mult * risk

        equity, available = await bx.balance()
        self.equity = equity
        qty = self._size(equity, price, risk, c.symbol)
        if qty <= 0:
            return "size"
        margin = qty * price / cfg.leverage
        if margin > available * 0.95:
            qty = float(bx.amount_to_precision(c.symbol, available * 0.95 * cfg.leverage / price))
            if qty <= 0:
                return "margin"

        await bx.prepare(c.symbol, c.direction)
        order = await bx.open(c.symbol, c.direction, qty, sl, tp)
        self.positions[c.symbol] = {
            "direction": c.direction, "entry": price, "sl": sl, "tp": tp, "qty": qty,
            "opened_at": now_utc(), "score": c.score,
        }
        self.cooldown[c.symbol] = now_utc() + timedelta(hours=cfg.symbol_cooldown_hours)
        risk_usdt = qty * risk
        await self.tg.send(
            f"✅ <b>ORDEN ABIERTA · {c.direction}</b> {pretty(c.symbol)}{' [DEMO]' if cfg.sandbox else ''}\n"
            f"Cantidad {qty} · Entrada ~{fmt_price(price)} · x{cfg.leverage} {cfg.margin_mode}\n"
            f"SL {fmt_price(sl)} · TP {fmt_price(tp)} · Riesgo ≈ {risk_usdt:.2f} USDT ({cfg.risk_pct}% equity)\n"
            f"<i>id {order.get('id', '?')}</i>"
        )
        return "opened"

    # ── Monitor ──
    async def monitor_loop(self):
        while True:
            try:
                await asyncio.sleep(self.cfg.monitor_seconds)
                if self.live:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("monitor")

    async def _sync(self, adopt: bool = False) -> Dict[str, dict]:
        current = await self.bx.positions()
        for sym, p in current.items():
            if sym not in self.positions and adopt:
                side = (p.get("side") or "long").lower()
                self.positions[sym] = {
                    "direction": "LONG" if side == "long" else "SHORT",
                    "entry": p.get("entryPrice"), "sl": None, "tp": None,
                    "qty": abs(float(p.get("contracts") or 0)),
                    "opened_at": now_utc(), "score": None, "adopted": True,
                }
        return current

    async def _tick(self):
        cfg = self.cfg
        # reinicio diario (UTC)
        today = now_utc().date()
        if today != self.day:
            self.day = today
            self.halted_today = False
            self.equity, _ = await self.bx.balance()
            self.day_start_equity = self.equity
            await self.tg.send(f"🌅 Nuevo día UTC. Equity inicial: {self.equity:.2f} USDT. Trading reactivado.")

        current = await self._sync(adopt=True)

        # posiciones cerradas por SL/TP/manual
        for sym in list(self.positions):
            if sym not in current:
                pos = self.positions.pop(sym)
                await self.bx.cancel_orders(sym)          # limpia SL/TP que hayan quedado colgados
                self.equity, _ = await self.bx.balance()
                chg = ""
                if self.day_start_equity:
                    chg = f"\nEquity {self.equity:.2f} USDT ({(self.equity / self.day_start_equity - 1) * 100:+.2f}% hoy)"
                await self.tg.send(f"🏁 <b>Posición cerrada</b> {pretty(sym)} ({pos['direction']}){chg}")

        # stop por tiempo
        limit = timedelta(hours=cfg.max_hold_hours)
        for sym, pos in list(self.positions.items()):
            if now_utc() - pos["opened_at"] >= limit and sym in current:
                p = current[sym]
                await self._close(sym, (p.get("side") or "long").lower(), abs(float(p.get("contracts") or 0)),
                                  f"⏱ Stop por tiempo ({cfg.max_hold_hours:g} h)")

        # límite de pérdida diaria
        if self.day_start_equity:
            eq, _ = await self.bx.balance()
            self.equity = eq
            if not self.halted_today and eq <= self.day_start_equity * (1 - cfg.daily_loss_limit_pct / 100.0):
                self.halted_today = True
                await self.tg.send(
                    f"🛑 <b>Límite de pérdida diaria alcanzado</b> ({cfg.daily_loss_limit_pct}%). "
                    f"El bot no abrirá más operaciones hasta mañana (UTC). Las posiciones abiertas mantienen su SL."
                )

    async def _close(self, sym: str, side: str, amount: float, reason: str):
        try:
            await self.bx.close_position(sym, side, amount)
            await self.bx.cancel_orders(sym)
            self.positions.pop(sym, None)
            await self.tg.send(f"{reason}\nCerrada {pretty(sym)} ({side.upper()}) a mercado.")
        except Exception as e:
            log.exception("close %s", sym)
            await self.tg.send(f"⚠️ <b>No se pudo cerrar {pretty(sym)}</b>: <code>{str(e)[:300]}</code>")

    async def close_all(self) -> str:
        if not self.live:
            return "Modo signals: no hay posiciones que cerrar."
        current = await self.bx.positions()
        if not current:
            return "No hay posiciones abiertas."
        for sym, p in current.items():
            await self._close(sym, (p.get("side") or "long").lower(), abs(float(p.get("contracts") or 0)),
                              "🧯 Cierre manual (/closeall)")
        return f"Cerradas {len(current)} posiciones."

    async def positions_text(self) -> str:
        if not self.live:
            return "Modo signals: el bot no opera."
        current = await self.bx.positions()
        if not current:
            return "Sin posiciones abiertas."
        lines = ["<b>Posiciones abiertas</b>"]
        for sym, p in current.items():
            lines.append(
                f"{pretty(sym)} {str(p.get('side')).upper()} · qty {p.get('contracts')} · "
                f"entrada {fmt_price(p.get('entryPrice'))} · PnL {float(p.get('unrealizedPnl') or 0):+.2f} USDT"
            )
        return "\n".join(lines)
