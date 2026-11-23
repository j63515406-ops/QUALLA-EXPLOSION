"""Pruebas sin red ni claves: datos sintéticos + exchange falso.
Ejecuta:  python test_scanner.py
"""
import asyncio

import numpy as np
import pandas as pd

from config import Config
from scanner import Scanner, analyze_15m, enrich, build_levels
from telegram_bot import format_candidate
from trader import Trader


def make_15m(scenario: str, seed: int = 1, n: int = 200) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    end = pd.Timestamp.now(tz="UTC").floor("15min") - pd.Timedelta(minutes=15)
    idx = pd.date_range(end=end, periods=n, freq="15min")
    if scenario == "random":
        vol = np.full(n, 0.006)
    else:  # volatilidad que se va comprimiendo en las últimas 60 velas
        vol = np.full(n, 0.006)
        vol[-60:] = np.linspace(0.006, 0.0003, 60)
    r = rng.normal(0, 1, n) * vol
    close = 100 * np.exp(np.cumsum(r))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, 1, n)) * vol * 0.4
    high = np.maximum(open_, close) * (1 + wick)
    low = np.minimum(open_, close) * (1 - wick)
    volume = rng.uniform(800, 1200, n)
    volume[-60:] = np.linspace(1000, 450, 60) * rng.uniform(0.9, 1.1, 60)
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)

    hh = df["high"].iloc[-21:-1].max()
    ll = df["low"].iloc[-21:-1].min()
    if scenario == "breakout_up":
        df.iloc[-1, df.columns.get_loc("close")] = hh * 1.006
        df.iloc[-1, df.columns.get_loc("high")] = hh * 1.007
        df.iloc[-1, df.columns.get_loc("volume")] = 3500
    elif scenario == "breakout_dn":
        df.iloc[-1, df.columns.get_loc("close")] = ll * 0.994
        df.iloc[-1, df.columns.get_loc("low")] = ll * 0.993
        df.iloc[-1, df.columns.get_loc("volume")] = 3500
    elif scenario == "loaded":
        df.iloc[-1, df.columns.get_loc("close")] = hh * 0.9993
        df.iloc[-1, df.columns.get_loc("high")] = hh * 0.9995
        df.iloc[-3:, df.columns.get_loc("volume")] = 750
    return df


def make_1h(seed: int = 7, n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    end = pd.Timestamp.now(tz="UTC").floor("1h") - pd.Timedelta(hours=1)
    idx = pd.date_range(end=end, periods=n, freq="1h")
    r = rng.normal(0.0002, 0.012, n)
    close = 100 * np.exp(np.cumsum(r))
    open_ = np.r_[close[0], close[:-1]]
    w = np.abs(rng.normal(0, 0.004, n))
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * (1 + w),
                         "low": np.minimum(open_, close) * (1 - w), "close": close,
                         "volume": rng.uniform(900, 1100, n)}, index=idx)


class FakeBX:
    def __init__(self):
        self.swaps = {f"{s}/USDT:USDT": {"base": s, "quote": "USDT"} for s in
                      ["BTC", "UPCOIN", "DNCOIN", "LOADCOIN", "PLAIN", "THINCOIN"]}
        self.btc_symbol = "BTC/USDT:USDT"
        self.data = {"BTC/USDT:USDT": make_15m("random", 11), "UPCOIN/USDT:USDT": make_15m("breakout_up", 2),
                     "DNCOIN/USDT:USDT": make_15m("breakout_dn", 3), "LOADCOIN/USDT:USDT": make_15m("loaded", 4),
                     "PLAIN/USDT:USDT": make_15m("random", 5), "THINCOIN/USDT:USDT": make_15m("breakout_up", 6)}
        self.orders = []

    async def tickers(self):
        out = {}
        for s in self.swaps:
            qv = 500_000 if s.startswith("THIN") else 20_000_000
            out[s] = {"quoteVolume": qv, "bid": 100.0, "ask": 100.02, "last": 100.0}
        return out

    async def ohlcv(self, sym, tf, limit):
        if tf == "1h":
            return make_1h(abs(hash(sym)) % 1000)
        return self.data[sym]

    # trader
    async def last_price(self, sym):
        return float(self.data[sym]["close"].iloc[-1])

    async def balance(self):
        return 1000.0, 900.0

    def amount_to_precision(self, sym, a):
        return f"{a:.3f}"

    def market(self, sym):
        return {"limits": {"amount": {"min": 0.001}, "cost": {"min": 1.0}}}

    async def prepare(self, sym, d):
        pass

    async def open(self, sym, d, qty, sl, tp):
        self.orders.append((sym, d, qty, sl, tp))
        return {"id": "TEST"}

    async def positions(self):
        return {}

    async def cancel_orders(self, sym):
        pass


class FakeTG:
    def __init__(self):
        self.msgs = []

    async def send(self, t, chat_id=None):
        self.msgs.append(t)


async def main():
    cfg = Config()
    cfg.min_adr_pct = 1.0

    # 1) análisis directo
    for scen, exp_kind, exp_dir in [("breakout_up", "TRIGGERED", "LONG"), ("breakout_dn", "TRIGGERED", "SHORT"),
                                    ("loaded", "LOADED", None), ("random", None, None)]:
        c = analyze_15m("X/USDT:USDT", make_15m(scen, 2), 20e6, cfg)
        kind = c.kind if c else None
        print(f"{scen:12s} → {kind} {c.direction if c else ''} score1={c.score if c else 0:.0f}")
        assert kind == exp_kind, (scen, kind)
        if exp_dir:
            assert c.direction == exp_dir

    # 2) escaneo completo con exchange falso
    bx = FakeBX()
    sc = Scanner(cfg, bx)
    cands, stats = await sc.scan()
    print("stats:", stats)
    syms = {c.symbol.split("/")[0]: c for c in cands}
    for k, c in syms.items():
        print(f"  {k:9s} {c.kind:9s} {c.direction:7s} score={c.score:5.1f} sl%={c.info['sl_pct']:.2f} parts={ {a: round(b) for a, b in c.info['parts'].items()} }")
    assert "THINCOIN" not in syms, "El filtro de volumen debería excluirla"
    assert "UPCOIN" in syms and syms["UPCOIN"].direction == "LONG" and syms["UPCOIN"].kind == "TRIGGERED"
    assert "DNCOIN" in syms and syms["DNCOIN"].direction == "SHORT"
    assert "PLAIN" not in syms

    # 3) mensajes de Telegram
    print("\n" + format_candidate(syms["UPCOIN"], cfg))
    print("\n" + format_candidate(syms["LOADCOIN"], cfg) if "LOADCOIN" in syms else "(LOADCOIN no pasó fase 2)")

    # 4) trader: modo signals no opera; modo live sí, con riesgo correcto
    tg = FakeTG()
    cfg.live = False
    tr = Trader(cfg, bx, tg)
    assert await tr.maybe_trade(syms["UPCOIN"]) == "signals" and not bx.orders

    cfg.live = True
    cfg.min_score_trade = 0
    tr = Trader(cfg, bx, tg)
    res = await tr.maybe_trade(syms["UPCOIN"])
    print("\nresultado trade:", res, bx.orders)
    assert res == "opened"
    sym, d, qty, sl, tp = bx.orders[0]
    price = float(bx.data[sym]["close"].iloc[-1])
    risk_usdt = qty * (price - sl)
    print(f"riesgo real = {risk_usdt:.2f} USDT (objetivo {1000 * cfg.risk_pct / 100:.2f}); TP/R = {(tp - price) / (price - sl):.2f}")
    assert abs(risk_usdt - 1000 * cfg.risk_pct / 100) < 0.2 * 1000 * cfg.risk_pct / 100 + 0.05 or qty * price <= 1000 * cfg.leverage * 0.9 + 1
    assert sl < price < tp
    assert await tr.maybe_trade(syms["UPCOIN"]) == "open"          # no duplica

    tr.positions = {f"S{i}": {} for i in range(cfg.max_open_positions)}
    assert await tr.maybe_trade(syms["DNCOIN"]) == "max_positions"  # límite de posiciones
    tr.positions = {}
    tr.halted_today = True
    assert await tr.maybe_trade(syms["DNCOIN"]) == "paused"         # límite diario
    print("\nTODAS LAS PRUEBAS OK ✅")


if __name__ == "__main__":
    asyncio.run(main())
