"""Envoltorio de ccxt para BingX (futuros perpetuos lineales USDT)."""
import logging
from typing import Dict, List, Optional, Tuple

import ccxt.async_support as ccxt
import pandas as pd

log = logging.getLogger("bingx")


class BingX:
    def __init__(self, cfg):
        self.cfg = cfg
        opts = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
        if cfg.api_key and cfg.api_secret:
            opts["apiKey"] = cfg.api_key
            opts["secret"] = cfg.api_secret
        self.ex = ccxt.bingx(opts)
        if cfg.sandbox:
            self.ex.set_sandbox_mode(True)
            log.warning("BINGX_SANDBOX activo: usando la cuenta demo (VST).")
        self.swaps: Dict[str, dict] = {}
        self.btc_symbol: Optional[str] = None

    # ── Mercados ──
    async def load(self):
        await self.ex.load_markets()
        self.swaps = {
            s: m for s, m in self.ex.markets.items()
            if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active") is not False
        }
        self.btc_symbol = "BTC/USDT:USDT" if "BTC/USDT:USDT" in self.swaps else None
        log.info("Mercados swap USDT cargados: %d", len(self.swaps))

    def market(self, symbol: str) -> dict:
        return self.ex.market(symbol)

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return self.ex.amount_to_precision(symbol, amount)

    async def tickers(self) -> dict:
        return await self.ex.fetch_tickers()

    async def last_price(self, symbol: str) -> float:
        t = await self.ex.fetch_ticker(symbol)
        return float(t["last"])

    async def ohlcv(self, symbol: str, tf: str, limit: int) -> Optional[pd.DataFrame]:
        raw = await self.ex.fetch_ohlcv(symbol, tf, limit=limit)
        if not raw:
            return None
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.drop_duplicates("ts").sort_values("ts").set_index("ts").astype(float)
        tf_ms = int(self.ex.parse_timeframe(tf) * 1000)
        last_open = int(df.index[-1].timestamp() * 1000)
        if last_open + tf_ms > self.ex.milliseconds():
            df = df.iloc[:-1]          # descarta la vela aún sin cerrar
        return df

    # ── Cuenta ──
    async def balance(self) -> Tuple[float, float]:
        """Devuelve (equity, disponible) en USDT."""
        bal = await self.ex.fetch_balance({"type": "swap"})
        equity = available = None
        data = (bal.get("info") or {}).get("data")
        row = None
        if isinstance(data, list) and data:
            row = next((x for x in data if x.get("asset") == "USDT"), data[0])
        elif isinstance(data, dict):
            row = data.get("balance") if isinstance(data.get("balance"), dict) else data
        if isinstance(row, dict):
            try:
                equity = float(row.get("equity") or row.get("balance"))
                available = float(row.get("availableMargin") or row.get("availableBalance"))
            except (TypeError, ValueError):
                equity = available = None
        if equity is None:
            u = bal.get("USDT") or {}
            equity = float(u.get("total") or 0.0)
            available = float(u.get("free") or 0.0)
        return equity, available

    async def positions(self) -> Dict[str, dict]:
        out = {}
        for p in await self.ex.fetch_positions():
            contracts = abs(float(p.get("contracts") or 0.0))
            if contracts <= 0:
                continue
            out[p["symbol"]] = p
        return out

    # ── Configuración y órdenes ──
    async def set_position_mode(self):
        try:
            await self.ex.set_position_mode(self.cfg.hedge_mode)
            log.info("Modo de posición: %s", "hedge" if self.cfg.hedge_mode else "one-way")
        except Exception as e:
            log.warning("set_position_mode: %s (se continúa; revisa que coincida con HEDGE_MODE)", e)

    async def prepare(self, symbol: str, direction: str):
        try:
            await self.ex.set_margin_mode(self.cfg.margin_mode, symbol)
        except Exception as e:
            log.info("margin mode %s: %s", symbol, e)       # suele fallar si ya está configurado
        side = direction if self.cfg.hedge_mode else "BOTH"
        try:
            await self.ex.set_leverage(self.cfg.leverage, symbol, {"side": side})
        except Exception as e:
            log.warning("leverage %s: %s", symbol, e)

    async def open(self, symbol: str, direction: str, amount: float, sl: float, tp: float) -> dict:
        """Orden de mercado con SL y TP adjuntos (se envían en la misma petición)."""
        side = "buy" if direction == "LONG" else "sell"
        params = {"stopLoss": {"triggerPrice": sl}, "takeProfit": {"triggerPrice": tp}}
        if self.cfg.hedge_mode:
            params["hedged"] = True
        return await self.ex.create_order(symbol, "market", side, amount, None, params)

    async def close_position(self, symbol: str, side: str, amount: float) -> dict:
        """Cierra una posición a mercado. side = 'long' | 'short' (la posición que se cierra)."""
        order_side = "sell" if side == "long" else "buy"
        params = {"reduceOnly": True}
        if self.cfg.hedge_mode:
            params["hedged"] = True
        return await self.ex.create_order(symbol, "market", order_side, amount, None, params)

    async def cancel_orders(self, symbol: str):
        try:
            await self.ex.cancel_all_orders(symbol)
        except Exception as e:
            log.info("cancel_all_orders %s: %s", symbol, e)

    async def close(self):
        try:
            await self.ex.close()
        except Exception:
            pass
