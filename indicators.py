"""Indicadores técnicos con pandas/numpy (sin TA-Lib para que despliegue fácil en Railway)."""
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = -d.clip(upper=0.0)
    ru = up.ewm(alpha=1.0 / n, adjust=False).mean()
    rd = dn.ewm(alpha=1.0 / n, adjust=False).mean()
    rs = ru / rd.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(rd != 0.0, 100.0)
    out = out.where(~((rd == 0.0) & (ru == 0.0)), 50.0)
    return out.fillna(50.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def bb_width(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    return (2.0 * k * sd) / mid


def rank_at(series: pd.Series, idx: int, window: int = 96) -> float:
    """Percentil (0-1) del valor en `idx` frente a los `window` valores previos.
    Cerca de 0 = anchura de Bollinger mínima (compresión máxima)."""
    start = max(0, idx - window + 1)
    vals = series.iloc[start: idx + 1].dropna()
    if len(vals) < 30:
        return float("nan")
    x = vals.iloc[-1]
    return float((vals.iloc[:-1] < x).mean())


def daily_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP que se reinicia cada día UTC."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    day = df.index.floor("D")
    pv = (tp * df["volume"]).groupby(day).cumsum()
    vv = df["volume"].groupby(day).cumsum()
    return pv / vv.replace(0.0, np.nan)
