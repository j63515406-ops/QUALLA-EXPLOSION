"""Escáner de explosiones.

Busca monedas de BingX (swaps USDT) que están:
  · LOADED     → comprimidas (squeeze de Bollinger), con volumen vivo y pegadas al borde de su rango:
                 "a punto de explotar" (por arriba o por abajo).
  · TRIGGERED  → acaban de romper su rango en la última vela cerrada con volumen, viniendo de compresión:
                 "explosión confirmada" (es lo único que el bot opera).
Cada moneda recibe un score 0-100.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import indicators as ind

log = logging.getLogger("scanner")


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


@dataclass
class Candidate:
    symbol: str
    direction: str            # LONG | SHORT | NEUTRAL
    kind: str                 # TRIGGERED | LOADED
    score: float
    price: float
    atr: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    info: Dict = field(default_factory=dict)


# ───────────────────────── Puntuación ─────────────────────────
def score_parts(info: dict, direction: str, kind: str, s2: Optional[dict] = None) -> Tuple[float, dict]:
    pct = info["pct_min12"] if kind == "TRIGGERED" else info["pct_now"]
    sq = 25.0 * _clip((0.5 - pct) / 0.5, 0.0, 1.0)
    vol = 25.0 * _clip((info["rvol"] - 1.0) / 2.5, 0.0, 1.0)

    if kind == "TRIGGERED":
        pos = 20.0 * (1.0 - _clip((info["body_atr"] - 1.5) / 1.5, 0.0, 1.0))
    else:
        if direction == "LONG":
            edge = info["dist_up"]
        elif direction == "SHORT":
            edge = info["dist_dn"]
        else:
            edge = min(info["dist_up"], info["dist_dn"])
        pos = 20.0 * _clip(1.0 - edge / 1.0, 0.0, 1.0)

    trend = 0.0
    if direction == "LONG":
        trend += 4.0 if info["e21_gt_e50"] else 0.0
        trend += 3.0 if info["above_vwap"] else 0.0
    elif direction == "SHORT":
        trend += 4.0 if not info["e21_gt_e50"] else 0.0
        trend += 3.0 if not info["above_vwap"] else 0.0
    else:
        trend += 3.0

    r = info["rsi"]
    if direction == "LONG":
        rsi_pts = 5.0 if 55 <= r <= 72 else (2.5 if (50 <= r < 55 or 72 < r <= 78) else 0.0)
    elif direction == "SHORT":
        rsi_pts = 5.0 if 28 <= r <= 45 else (2.5 if (45 < r <= 50 or 22 <= r < 28) else 0.0)
    else:
        rsi_pts = 2.5 if 40 < r < 60 else 0.0

    if s2 is None:
        trend1h, room, btc = 0.0, 5.0, 0.0
    else:
        trend1h, room, btc = s2["trend1h"], s2["room"], s2["btc"]

    parts = {"squeeze": sq, "volumen": vol, "posición": pos, "tendencia": trend + trend1h,
             "margen": room, "rsi": rsi_pts, "btc": btc}
    total = _clip(sq + vol + pos + trend + trend1h + rsi_pts + room + btc, 0.0, 100.0)
    return total, parts


def build_levels(c: Candidate, cfg) -> None:
    """SL por ATR y TP por múltiplo de R. En LOADED, los niveles parten del borde del rango."""
    ref = c.price
    risk = max(cfg.sl_atr_mult * c.atr, cfg.min_sl_pct / 100.0 * ref)
    sl_pct = risk / ref * 100.0
    c.info["risk"] = risk
    c.info["sl_pct"] = sl_pct
    c.info["tradable"] = sl_pct <= cfg.max_sl_pct
    if c.direction == "LONG":
        entry = c.price if c.kind == "TRIGGERED" else c.info["hh"]
        c.sl, c.tp = entry - risk, entry + cfg.tp_r_mult * risk
        c.info["entry"] = entry
    elif c.direction == "SHORT":
        entry = c.price if c.kind == "TRIGGERED" else c.info["ll"]
        c.sl, c.tp = entry + risk, entry - cfg.tp_r_mult * risk
        c.info["entry"] = entry


# ───────────────────────── Fase 1: velas de 15m ─────────────────────────
def analyze_15m(symbol: str, df: Optional[pd.DataFrame], qv: float, cfg) -> Optional[Candidate]:
    if df is None or len(df) < 120:
        return None
    c, h, l, o, v = df["close"], df["high"], df["low"], df["open"], df["volume"]
    price = float(c.iloc[-1])
    if price <= 0:
        return None
    atr_s = ind.atr(df, 14)
    # ATR "normal" de la moneda (media de 96 velas). Tras un squeeze el ATR actual es diminuto y haría que
    # cualquier ruptura pareciera enorme, así que las distancias, el SL y el filtro de "vela extendida" usan este.
    atr = float(atr_s.iloc[-96:].mean())
    if not np.isfinite(atr) or atr <= 0:
        return None

    bbw = ind.bb_width(c, 20, 2.0)
    n = len(bbw)
    pct_now = ind.rank_at(bbw, n - 1, 96)
    pcts = [ind.rank_at(bbw, i, 96) for i in range(n - 12, n)]
    pcts = [p for p in pcts if np.isfinite(p)]
    if not pcts or not np.isfinite(pct_now):
        return None
    pct_min12 = min(pcts)

    vma_prev = float(v.iloc[-21:-1].mean())
    base3 = float(v.iloc[-23:-3].mean())
    rvol_now = float(v.iloc[-1]) / vma_prev if vma_prev > 0 else 0.0
    rvol3 = float(v.iloc[-3:].mean()) / base3 if base3 > 0 else 0.0
    rvol = max(rvol_now, rvol3)

    hh = float(h.iloc[-21:-1].max())
    ll = float(l.iloc[-21:-1].min())
    broke_up, broke_dn = price > hh, price < ll
    body_atr = abs(float(c.iloc[-1] - o.iloc[-1])) / atr
    dist_up, dist_dn = (hh - price) / atr, (price - ll) / atr

    e21 = float(ind.ema(c, 21).iloc[-1])
    e50 = float(ind.ema(c, 50).iloc[-1])
    rsi = float(ind.rsi(c, 14).iloc[-1])
    vwap = float(ind.daily_vwap(df).iloc[-1])
    last8 = df.iloc[-8:]
    vsum = float(last8["volume"].sum())
    pressure = float((np.sign(last8["close"] - last8["open"]) * last8["volume"]).sum() / vsum) if vsum > 0 else 0.0

    kind, direction = None, None
    if (broke_up or broke_dn) and pct_min12 <= cfg.squeeze_pct_max and rvol >= cfg.min_rvol_trigger \
            and body_atr <= cfg.max_body_atr:
        kind = "TRIGGERED"
        direction = "LONG" if broke_up else "SHORT"
    elif (not broke_up and not broke_dn) and pct_now <= cfg.squeeze_pct_loaded \
            and min(dist_up, dist_dn) <= cfg.max_edge_dist_atr and rvol3 >= cfg.min_rvol_loaded:
        kind = "LOADED"
        b = 0
        b += 1 if e21 > e50 else -1
        b += 1 if (np.isfinite(vwap) and price > vwap) else -1
        b += 1 if pressure > 0.10 else (-1 if pressure < -0.10 else 0)
        b += 1 if dist_up < dist_dn else -1
        direction = "LONG" if b >= 2 else ("SHORT" if b <= -2 else "NEUTRAL")
    else:
        return None

    info = {
        "pct_now": pct_now, "pct_min12": pct_min12, "rvol": rvol, "rvol3": rvol3,
        "body_atr": body_atr, "dist_up": dist_up, "dist_dn": dist_dn, "hh": hh, "ll": ll,
        "e21_gt_e50": e21 > e50, "above_vwap": bool(np.isfinite(vwap) and price > vwap),
        "rsi": rsi, "pressure": pressure, "qv24": qv, "atr_pct": atr / price * 100.0,
    }
    cand = Candidate(symbol=symbol, direction=direction, kind=kind, score=0.0, price=price, atr=atr, info=info)
    cand.score, cand.info["parts"] = score_parts(info, direction, kind, None)
    return cand


# ───────────────────────── Fase 2: contexto 1h (ADR, tendencia, BTC) ─────────────────────────
def enrich(c: Candidate, df15: pd.DataFrame, df1h: Optional[pd.DataFrame], btc: str, cfg) -> Optional[Candidate]:
    if df1h is None or len(df1h) < 80:
        return None
    close1h = df1h["close"]
    e21 = float(ind.ema(close1h, 21).iloc[-1])
    e50 = float(ind.ema(close1h, 50).iloc[-1])
    p1h = float(close1h.iloc[-1])

    daily = df1h.resample("1D").agg({"high": "max", "low": "min"}).dropna()
    today = datetime.now(timezone.utc).date()
    daily = daily[daily.index.date < today]
    if len(daily) < 5:
        return None
    adr = float(((daily["high"] / daily["low"] - 1.0) * 100.0).tail(14).mean())
    if not np.isfinite(adr) or adr < cfg.min_adr_pct:
        return None

    d15 = df15[df15.index.date == today]
    today_rng = float((d15["high"].max() / d15["low"].min() - 1.0) * 100.0) if len(d15) else 0.0
    room_ratio = today_rng / adr
    if c.kind == "TRIGGERED" and room_ratio > cfg.max_room_ratio:
        return None   # ya explotó hoy: llegamos tarde

    if c.direction == "LONG":
        t1h = (5.0 if e21 > e50 else 0.0) + (3.0 if p1h > e50 else 0.0)
    elif c.direction == "SHORT":
        t1h = (5.0 if e21 < e50 else 0.0) + (3.0 if p1h < e50 else 0.0)
    else:
        t1h = 3.0
    room = 10.0 * _clip((1.5 - room_ratio) / 1.0, 0.0, 1.0)
    btc_pen = 0.0
    if cfg.use_btc_filter:
        if c.direction == "LONG" and btc == "DOWN":
            btc_pen = -8.0
        elif c.direction == "SHORT" and btc == "UP":
            btc_pen = -8.0

    c.score, c.info["parts"] = score_parts(c.info, c.direction, c.kind, {"trend1h": t1h, "room": room, "btc": btc_pen})
    c.info.update({"adr": adr, "today_rng": today_rng, "room_ratio": room_ratio, "btc": btc})
    return c


# ───────────────────────── Escáner ─────────────────────────
class Scanner:
    def __init__(self, cfg, bx):
        self.cfg = cfg
        self.bx = bx

    async def _universe(self) -> List[Tuple[str, float]]:
        cfg = self.cfg
        tickers = await self.bx.tickers()
        rows = []
        for sym, t in tickers.items():
            m = self.bx.swaps.get(sym)
            if not m:
                continue
            if str(m.get("base", "")).upper() in cfg.exclude:
                continue
            qv = t.get("quoteVolume")
            if not qv and t.get("baseVolume") and t.get("last"):
                qv = float(t["baseVolume"]) * float(t["last"])
            if not qv or qv < cfg.min_quote_volume:
                continue
            bid, ask = t.get("bid"), t.get("ask")
            if bid and ask and bid > 0 and (ask - bid) / bid * 100.0 > cfg.max_spread_pct:
                continue
            rows.append((sym, float(qv)))
        rows.sort(key=lambda x: -x[1])
        if cfg.max_symbols > 0:
            rows = rows[: cfg.max_symbols]
        return rows

    async def _btc_regime(self) -> str:
        sym = self.bx.btc_symbol
        if not sym:
            return "MIXED"
        try:
            df = await self.bx.ohlcv(sym, "1h", 120)
            c = df["close"]
            e21, e50, p = ind.ema(c, 21).iloc[-1], ind.ema(c, 50).iloc[-1], c.iloc[-1]
            if p > e50 and e21 > e50:
                return "UP"
            if p < e50 and e21 < e50:
                return "DOWN"
        except Exception as e:
            log.warning("No se pudo evaluar BTC: %s", e)
        return "MIXED"

    async def scan(self) -> Tuple[List[Candidate], dict]:
        cfg = self.cfg
        t0 = time.time()
        universe = await self._universe()
        btc = await self._btc_regime()
        sem = asyncio.Semaphore(cfg.concurrency)
        dfs: Dict[str, pd.DataFrame] = {}
        errors = 0

        async def stage1(sym: str, qv: float):
            nonlocal errors
            async with sem:
                try:
                    df = await self.bx.ohlcv(sym, cfg.scan_timeframe, cfg.candles)
                except Exception as e:
                    errors += 1
                    log.debug("ohlcv %s: %s", sym, e)
                    return None
            try:
                cand = analyze_15m(sym, df, qv, cfg)
            except Exception as e:
                log.debug("analyze %s: %s", sym, e)
                return None
            if cand:
                dfs[sym] = df
            return cand

        res = await asyncio.gather(*[stage1(s, q) for s, q in universe])
        s1 = sorted([r for r in res if r], key=lambda x: -x.score)
        trig = [c for c in s1 if c.kind == "TRIGGERED"]
        rest = [c for c in s1 if c.kind != "TRIGGERED"]
        chosen = trig[:15] + rest[: cfg.stage2_top]

        async def stage2(c: Candidate):
            async with sem:
                try:
                    df1h = await self.bx.ohlcv(c.symbol, "1h", 400)
                except Exception as e:
                    log.debug("1h %s: %s", c.symbol, e)
                    return None
            try:
                out = enrich(c, dfs[c.symbol], df1h, btc, cfg)
            except Exception as e:
                log.debug("enrich %s: %s", c.symbol, e)
                return None
            if out:
                build_levels(out, cfg)
            return out

        res2 = await asyncio.gather(*[stage2(c) for c in chosen])
        final = sorted([r for r in res2 if r], key=lambda x: -x.score)
        stats = {
            "universe": len(universe), "stage1": len(s1), "final": len(final),
            "triggered": len([c for c in final if c.kind == "TRIGGERED"]),
            "loaded": len([c for c in final if c.kind == "LOADED"]),
            "btc": btc, "errors": errors, "seconds": round(time.time() - t0, 1),
        }
        log.info("Escaneo: %s", stats)
        return final, stats
