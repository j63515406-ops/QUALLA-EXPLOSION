"""Telegram: envío de señales y comandos (sin librerías extra, solo aiohttp)."""
import asyncio
import html
import logging
from typing import Awaitable, Callable, Optional

import aiohttp

log = logging.getLogger("telegram")
API = "https://api.telegram.org/bot{token}/{method}"


def fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "-"
    if p >= 100:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    return f"{p:.8f}"


def fmt_money(x: float) -> str:
    if x >= 1e9:
        return f"{x / 1e9:.2f}B"
    if x >= 1e6:
        return f"{x / 1e6:.1f}M"
    if x >= 1e3:
        return f"{x / 1e3:.0f}K"
    return f"{x:.0f}"


def pretty(symbol: str) -> str:
    return symbol.replace(":USDT", "")


def format_candidate(c, cfg) -> str:
    i = c.info
    p = i.get("parts", {})
    breakdown = " · ".join(f"{k} {v:.0f}" for k, v in p.items() if k != "btc" or v != 0)
    sym = html.escape(pretty(c.symbol))
    lines = []
    if c.kind == "TRIGGERED":
        arrow = "🟢" if c.direction == "LONG" else "🔴"
        lines.append(f"🚀 <b>EXPLOSIÓN CONFIRMADA · {c.direction}</b> {arrow} <b>{sym}</b>")
    else:
        if c.direction == "NEUTRAL":
            lines.append(f"🔥 <b>A PUNTO DE EXPLOTAR</b> · <b>{sym}</b> (dirección abierta)")
        else:
            lines.append(f"🔥 <b>A PUNTO DE EXPLOTAR · sesgo {c.direction}</b> · <b>{sym}</b>")
    lines.append(f"Score <b>{c.score:.0f}</b>/100  <i>({breakdown})</i>")
    lines.append(f"Precio {fmt_price(c.price)} · ATR15 {i.get('atr_pct', 0):.2f}% · Vol24h {fmt_money(i.get('qv24', 0))} USDT")
    lines.append(f"Squeeze p{(i['pct_min12'] if c.kind == 'TRIGGERED' else i['pct_now']) * 100:.0f} · "
                 f"RVOL {i['rvol']:.1f}x · RSI {i['rsi']:.0f}")
    if "adr" in i:
        lines.append(f"Rango hoy {i['room_ratio']:.1f}× ADR (ADR {i['adr']:.1f}%) · BTC 1h: {i.get('btc', '?')}")
    if c.kind == "TRIGGERED":
        if c.sl and c.tp:
            lines.append(f"Entrada ~{fmt_price(c.price)} · SL {fmt_price(c.sl)} (−{i['sl_pct']:.2f}%) · "
                         f"TP {fmt_price(c.tp)} (+{i['sl_pct'] * cfg.tp_r_mult:.2f}%) · R:R {cfg.tp_r_mult:.1f}")
        if not i.get("tradable", True):
            lines.append("⚠️ SL demasiado ancho: el bot no la operará.")
    else:
        up, dn = fmt_price(i["hh"]), fmt_price(i["ll"])
        if c.direction == "LONG":
            lines.append(f"Disparador: cierre 15m &gt; {up} → LONG · SL ≈ {i['sl_pct']:.2f}%")
        elif c.direction == "SHORT":
            lines.append(f"Disparador: cierre 15m &lt; {dn} → SHORT · SL ≈ {i['sl_pct']:.2f}%")
        else:
            lines.append(f"Arriba de {up} → LONG · Abajo de {dn} → SHORT · SL ≈ {i['sl_pct']:.2f}%")
        lines.append("<i>Aviso previo: el bot solo opera cuando rompe (explosión confirmada).</i>")
    return "\n".join(lines)


class Telegram:
    def __init__(self, cfg):
        self.token = cfg.telegram_token
        self.chat_id = str(cfg.telegram_chat_id or "")
        self.enabled = bool(self.token and self.chat_id)
        self._session: Optional[aiohttp.ClientSession] = None
        self._offset = 0

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        return self._session

    async def send(self, text: str, chat_id: Optional[str] = None):
        target = chat_id or self.chat_id
        if not self.token or not target:
            log.info("[Telegram desactivado] %s", text.replace("\n", " | "))
            return
        s = await self._sess()
        for i in range(0, len(text), 3900):
            chunk = text[i:i + 3900]
            try:
                async with s.post(API.format(token=self.token, method="sendMessage"),
                                  json={"chat_id": target, "text": chunk, "parse_mode": "HTML",
                                        "disable_web_page_preview": True}) as r:
                    if r.status != 200:
                        log.warning("Telegram %s: %s", r.status, (await r.text())[:200])
            except Exception as e:
                log.warning("Telegram send error: %s", e)

    async def command_loop(self, handler: Callable[[str, list], Awaitable[Optional[str]]]):
        if not self.token:
            await asyncio.Event().wait()
        s = await self._sess()
        try:
            async with s.get(API.format(token=self.token, method="deleteWebhook")):
                pass
        except Exception:
            pass
        while True:
            try:
                s = await self._sess()
                async with s.get(API.format(token=self.token, method="getUpdates"),
                                 params={"timeout": 25, "offset": self._offset,
                                         "allowed_updates": '["message"]'}) as r:
                    data = await r.json()
                if not data.get("ok"):
                    await asyncio.sleep(5)
                    continue
                for upd in data.get("result", []):
                    self._offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    text = (msg.get("text") or "").strip()
                    chat = str((msg.get("chat") or {}).get("id", ""))
                    if not text.startswith("/"):
                        continue
                    parts = text.split()
                    cmd = parts[0].split("@")[0].lower()
                    if chat != self.chat_id:
                        if cmd in ("/start", "/id"):
                            await self.send(f"Tu chat_id es <code>{chat}</code>. Ponlo en la variable "
                                            f"TELEGRAM_CHAT_ID de Railway.", chat_id=chat)
                        continue
                    reply = await handler(cmd, parts[1:])
                    if reply:
                        await self.send(reply)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("command_loop: %s", e)
                await asyncio.sleep(5)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
