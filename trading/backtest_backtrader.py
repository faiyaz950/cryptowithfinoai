#!/usr/bin/env python3
"""
Backtest using Backtrader on Delta Exchange India historical data.
Usage: pip install backtrader
       python backtest_backtrader.py
"""

import sys
from datetime import datetime

try:
    import backtrader as bt
except ImportError:
    print("Install backtrader: pip install backtrader")
    sys.exit(1)

from fetch_trading_data import BASE_URL


def fetch_historical_data(symbol, resolution, from_timestamp_ms, to_timestamp_ms=None):
    """Fetch OHLC from Delta India API (public). Returns DataFrame with time, open, high, low, close, volume."""
    import requests
    import pandas as pd
    import time as _t
    if to_timestamp_ms is None:
        to_timestamp_ms = int(_t.time() * 1000)
    path = "/v2/history/candles"
    # Delta India API expects start/end (not from/to); try seconds first
    from_sec = from_timestamp_ms // 1000
    to_sec = to_timestamp_ms // 1000
    params = {
        "symbol": symbol,
        "resolution": resolution,
        "start": from_sec,
        "end": to_sec
    }
    response = requests.get(BASE_URL + path, params=params)
    if response.status_code != 200:
        print(f"Error: {response.status_code} - {response.text[:200]}")
        return None
    data = response.json().get("result")
    if not data:
        return None
    candles = data.get("candles", data) if isinstance(data, dict) else data
    if not candles:
        return None
    df = __result_to_df(candles)
    return df


def __result_to_df(candles):
    import pandas as pd
    rows = []
    for c in candles:
        t = c.get("time", c.get("t"))
        if isinstance(t, (int, float)):
            t = pd.to_datetime(t, unit="s" if t < 1e12 else "ms")
        else:
            t = pd.to_datetime(t)
        rows.append({
            "time": t,
            "open": float(c.get("open", c.get("o", 0))),
            "high": float(c.get("high", c.get("h", 0))),
            "low": float(c.get("low", c.get("l", 0))),
            "close": float(c.get("close", c.get("c", 0))),
            "volume": float(c.get("volume", c.get("v", 0)))
        })
    return pd.DataFrame(rows)


class SimpleStrategy(bt.Strategy):
    """SMA crossover: buy when close > SMA, sell when close < SMA."""
    params = (("sma_period", 15),)

    def __init__(self):
        self.sma = bt.indicators.SMA(self.data.close, period=self.p.sma_period)

    def next(self):
        if self.data.close[0] > self.sma[0]:
            self.buy()
        elif self.data.close[0] < self.sma[0]:
            self.sell()


def run_backtest(df, strategy_class=SimpleStrategy, sma_period=15, initial_cash=100000):
    """
    Backtest on historical data.
    df: Pandas DF with 'time', 'open', 'high', 'low', 'close', 'volume'
    """
    cerebro = bt.Cerebro()
    cerebro.addstrategy(strategy_class, sma_period=sma_period)
    df_bt = df.rename(columns={"time": "datetime"})
    data = bt.feeds.PandasData(dataname=df_bt)
    cerebro.adddata(data)
    cerebro.broker.setcash(initial_cash)
    cerebro.run()
    print("Final Portfolio Value:", cerebro.broker.getvalue())
    return cerebro


if __name__ == "__main__":
    symbol = "BTCUSDT"
    resolution = "1h"
    from_ts = int(datetime(2024, 1, 1).timestamp() * 1000)
    
    print("Fetching historical data from Delta Exchange India...")
    df = fetch_historical_data(symbol, resolution, from_ts)
    if df is None or len(df) == 0:
        print("No data received. Check symbol and API.")
        sys.exit(1)
    
    print(f"Got {len(df)} candles from {df['time'].min()} to {df['time'].max()}")
    sma_period = 20
    print(f"Running backtest with SMA period = {sma_period}...")
    cerebro = run_backtest(df, sma_period=sma_period)
    try:
        cerebro.plot()
    except Exception as e:
        print("Plot skipped:", e)
