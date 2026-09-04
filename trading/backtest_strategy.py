#!/usr/bin/env python3
"""
Complete Trading Strategy Backtesting Script

Strategy Rules:
- Indicators: EMA 9, EMA 21, EMA 50
- Entry: BUY when EMA 9 crosses above BOTH EMA 21 and EMA 50 (after candle close)
- Entry: SELL when EMA 9 crosses below BOTH EMA 21 and EMA 50 (after candle close)
- Exit: Fixed Stop Loss: 400 points, Fixed Target: 800 points (Risk:Reward = 1:2)
- Only one trade at a time (no overlapping trades)

Backtesting Periods:
- Last 3 months
- Last 6 months
- Last 1 year
- Last 2 years
"""

import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from fetch_trading_data import CryptoAPIClient
import json

# Configuration (Delta Exchange only) — supplied via environment.
API_KEY = os.environ.get("DELTA_API_KEY", "")
SECRET_KEY = os.environ.get("DELTA_SECRET_KEY", "")

# Default configuration (easily configurable)
DEFAULT_CONFIG = {
    'symbol': 'BTCUSDT',
    'timeframe': '5m',
    'exchange': 'delta',
    'ema_periods': {
        'ema9': 9,
        'ema21': 21,
        'ema50': 50
    },
    'stop_loss_points': 400,
    'target_points': 800
}


class EMACrossoverStrategy:
    """
    EMA Crossover Trading Strategy
    
    Entry Rules:
    - BUY: EMA 9 crosses above BOTH EMA 21 and EMA 50
    - SELL: EMA 9 crosses below BOTH EMA 21 and EMA 50
    - Entry confirmed only after candle close
    
    Exit Rules:
    - Fixed Stop Loss: 400 points
    - Fixed Target: 800 points
    - Risk:Reward = 1:2
    - Only one trade at a time
    """
    
    def __init__(self, ema9: int = 9, ema21: int = 21, ema50: int = 50,
                 stop_loss_points: float = 400, target_points: float = 800):
        """
        Initialize strategy parameters
        
        Args:
            ema9: Period for EMA 9
            ema21: Period for EMA 21
            ema50: Period for EMA 50
            stop_loss_points: Stop loss in points
            target_points: Target in points
        """
        self.ema9_period = ema9
        self.ema21_period = ema21
        self.ema50_period = ema50
        self.stop_loss_points = stop_loss_points
        self.target_points = target_points
        
        # Validate risk:reward ratio
        if abs(target_points / stop_loss_points) != 2.0:
            print(f"⚠️ Warning: Risk:Reward ratio is {target_points/stop_loss_points:.2f}, expected 1:2")
    
    def calculate_ema(self, data: pd.Series, period: int) -> pd.Series:
        """
        Calculate Exponential Moving Average (EMA)
        
        Args:
            data: Price series (typically close prices)
            period: EMA period
            
        Returns:
            EMA series
        """
        return data.ewm(span=period, adjust=False).mean()
    
    def prepare_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Prepare dataframe with EMA indicators
        
        Args:
            df: DataFrame with OHLCV data (must have 'Close' column)
            
        Returns:
            DataFrame with EMA columns added
        """
        # Ensure we have Close column
        if 'Close' not in df.columns and 'close' in df.columns:
            df['Close'] = df['close']
        elif 'Close' not in df.columns:
            raise ValueError("DataFrame must have 'Close' or 'close' column")
        
        # Calculate EMAs
        df['EMA_9'] = self.calculate_ema(df['Close'], self.ema9_period)
        df['EMA_21'] = self.calculate_ema(df['Close'], self.ema21_period)
        df['EMA_50'] = self.calculate_ema(df['Close'], self.ema50_period)
        
        return df
    
    def detect_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Detect buy and sell signals based on EMA crossover
        
        Entry Rules:
        - BUY: EMA 9 crosses above BOTH EMA 21 and EMA 50
        - SELL: EMA 9 crosses below BOTH EMA 21 and EMA 50
        - Signal confirmed only after candle close (using previous candle's state)
        
        Args:
            df: DataFrame with EMA columns
            
        Returns:
            DataFrame with signal columns added
        """
        df = df.copy()
        
        # Initialize signal columns
        df['BUY_SIGNAL'] = False
        df['SELL_SIGNAL'] = False
        
        # Check for valid EMA values (need at least 50 candles for EMA 50)
        for i in range(1, len(df)):
            # Get current and previous candle values
            prev_ema9 = df.iloc[i-1]['EMA_9']
            prev_ema21 = df.iloc[i-1]['EMA_21']
            prev_ema50 = df.iloc[i-1]['EMA_50']
            
            curr_ema9 = df.iloc[i]['EMA_9']
            curr_ema21 = df.iloc[i]['EMA_21']
            curr_ema50 = df.iloc[i]['EMA_50']
            
            # Skip if any EMA is NaN
            if pd.isna(prev_ema9) or pd.isna(prev_ema21) or pd.isna(prev_ema50) or \
               pd.isna(curr_ema9) or pd.isna(curr_ema21) or pd.isna(curr_ema50):
                continue
            
            # BUY Signal: EMA 9 crosses above BOTH EMA 21 and EMA 50
            # This means:
            # - EMA 9 crosses above EMA 21 (prev_ema9 <= prev_ema21 AND curr_ema9 > curr_ema21)
            # - EMA 9 crosses above EMA 50 (prev_ema9 <= prev_ema50 AND curr_ema9 > curr_ema50)
            # Both conditions must be true
            ema9_crosses_above_21 = (prev_ema9 <= prev_ema21) and (curr_ema9 > curr_ema21)
            ema9_crosses_above_50 = (prev_ema9 <= prev_ema50) and (curr_ema9 > curr_ema50)
            
            if ema9_crosses_above_21 and ema9_crosses_above_50:
                df.at[df.index[i], 'BUY_SIGNAL'] = True
            
            # SELL Signal: EMA 9 crosses below BOTH EMA 21 and EMA 50
            # This means:
            # - EMA 9 crosses below EMA 21 (prev_ema9 >= prev_ema21 AND curr_ema9 < curr_ema21)
            # - EMA 9 crosses below EMA 50 (prev_ema9 >= prev_ema50 AND curr_ema9 < curr_ema50)
            # Both conditions must be true
            ema9_crosses_below_21 = (prev_ema9 >= prev_ema21) and (curr_ema9 < curr_ema21)
            ema9_crosses_below_50 = (prev_ema9 >= prev_ema50) and (curr_ema9 < curr_ema50)
            
            if ema9_crosses_below_21 and ema9_crosses_below_50:
                df.at[df.index[i], 'SELL_SIGNAL'] = True
        
        return df


class Trade:
    """
    Represents a single trade
    """
    
    def __init__(self, side: str, entry_price: float, entry_time: pd.Timestamp,
                 stop_loss: float, target: float):
        """
        Initialize trade
        
        Args:
            side: 'buy' or 'sell'
            entry_price: Entry price
            entry_time: Entry timestamp
            stop_loss: Stop loss price
            target: Target price
        """
        self.side = side
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.stop_loss = stop_loss
        self.target = target
        self.exit_price = None
        self.exit_time = None
        self.status = 'OPEN'  # OPEN, SL_HIT, TARGET_HIT, CLOSED_AT_END
        self.pnl = 0.0
        self.pnl_points = 0.0
    
    def check_exit(self, high: float, low: float, close: float, timestamp: pd.Timestamp) -> bool:
        """
        Check if trade should exit based on SL/Target
        
        Args:
            high: Candle high price
            low: Candle low price
            close: Candle close price
            timestamp: Candle timestamp
            
        Returns:
            True if trade exited, False otherwise
        """
        if self.side == 'buy':
            # Check stop loss first (lower priority in case both hit)
            if low <= self.stop_loss:
                self.exit_price = self.stop_loss
                self.exit_time = timestamp
                self.status = 'SL_HIT'
                self.pnl = self.exit_price - self.entry_price
                self.pnl_points = self.pnl
                return True
            # Check target
            elif high >= self.target:
                self.exit_price = self.target
                self.exit_time = timestamp
                self.status = 'TARGET_HIT'
                self.pnl = self.exit_price - self.entry_price
                self.pnl_points = self.pnl
                return True
        else:  # sell
            # Check stop loss first
            if high >= self.stop_loss:
                self.exit_price = self.stop_loss
                self.exit_time = timestamp
                self.status = 'SL_HIT'
                self.pnl = self.entry_price - self.exit_price
                self.pnl_points = self.pnl
                return True
            # Check target
            elif low <= self.target:
                self.exit_price = self.target
                self.exit_time = timestamp
                self.status = 'TARGET_HIT'
                self.pnl = self.entry_price - self.exit_price
                self.pnl_points = self.pnl
                return True
        
        return False
    
    def close_at_end(self, close_price: float, timestamp: pd.Timestamp):
        """
        Close trade at end of backtest period
        
        Args:
            close_price: Final close price
            timestamp: Final timestamp
        """
        self.exit_price = close_price
        self.exit_time = timestamp
        self.status = 'CLOSED_AT_END'
        
        if self.side == 'buy':
            self.pnl = self.exit_price - self.entry_price
        else:
            self.pnl = self.entry_price - self.exit_price
        
        self.pnl_points = self.pnl
    
    def to_dict(self) -> Dict:
        """Convert trade to dictionary"""
        return {
            'side': self.side,
            'entry_price': float(self.entry_price),
            'entry_time': self.entry_time.isoformat() if isinstance(self.entry_time, pd.Timestamp) else str(self.entry_time),
            'exit_price': float(self.exit_price) if self.exit_price else None,
            'exit_time': self.exit_time.isoformat() if self.exit_time and isinstance(self.exit_time, pd.Timestamp) else (str(self.exit_time) if self.exit_time else None),
            'stop_loss': float(self.stop_loss),
            'target': float(self.target),
            'status': self.status,
            'pnl': float(self.pnl),
            'pnl_points': float(self.pnl_points)
        }


class BacktestEngine:
    """
    Backtesting engine for the EMA crossover strategy
    """
    
    def __init__(self, strategy: EMACrossoverStrategy):
        """
        Initialize backtest engine
        
        Args:
            strategy: EMACrossoverStrategy instance
        """
        self.strategy = strategy
        self.trades: List[Trade] = []
        self.current_trade: Optional[Trade] = None
    
    def run_backtest(self, df: pd.DataFrame) -> Dict:
        """
        Run backtest on historical data
        
        Args:
            df: DataFrame with OHLCV data and signals
            
        Returns:
            Dictionary with backtest results
        """
        # Reset state
        self.trades = []
        self.current_trade = None
        
        # Prepare data with EMAs
        df = self.strategy.prepare_data(df)
        
        # Detect signals
        df = self.strategy.detect_signals(df)
        
        # Process each candle
        for i in range(len(df)):
            row = df.iloc[i]
            timestamp = row.get('Open Time', df.index[i])
            
            # Check if we have a current trade
            if self.current_trade:
                # Check for exit
                high = row['High']
                low = row['Low']
                close = row['Close']
                
                if self.current_trade.check_exit(high, low, close, timestamp):
                    self.trades.append(self.current_trade)
                    self.current_trade = None
            
            # Check for new entry signals (only if no current trade)
            if self.current_trade is None:
                if row['BUY_SIGNAL']:
                    entry_price = row['Close']  # Entry after candle close
                    stop_loss = entry_price - self.strategy.stop_loss_points
                    target = entry_price + self.strategy.target_points
                    
                    self.current_trade = Trade(
                        side='buy',
                        entry_price=entry_price,
                        entry_time=timestamp,
                        stop_loss=stop_loss,
                        target=target
                    )
                
                elif row['SELL_SIGNAL']:
                    entry_price = row['Close']  # Entry after candle close
                    stop_loss = entry_price + self.strategy.stop_loss_points
                    target = entry_price - self.strategy.target_points
                    
                    self.current_trade = Trade(
                        side='sell',
                        entry_price=entry_price,
                        entry_time=timestamp,
                        stop_loss=stop_loss,
                        target=target
                    )
        
        # Close any open trade at the end
        if self.current_trade:
            last_row = df.iloc[-1]
            last_close = last_row['Close']
            last_timestamp = last_row.get('Open Time', df.index[-1])
            self.current_trade.close_at_end(last_close, last_timestamp)
            self.trades.append(self.current_trade)
            self.current_trade = None
        
        # Calculate metrics
        return self.calculate_metrics()
    
    def calculate_metrics(self) -> Dict:
        """
        Calculate backtest performance metrics
        
        Returns:
            Dictionary with all metrics
        """
        if not self.trades:
            return {
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': 0.0,
                'net_points': 0.0,
                'max_drawdown': 0.0,
                'profit_factor': 0.0,
                'total_profit': 0.0,
                'total_loss': 0.0,
                'avg_win': 0.0,
                'avg_loss': 0.0
            }
        
        # Basic metrics
        total_trades = len(self.trades)
        winning_trades = sum(1 for t in self.trades if t.pnl > 0)
        losing_trades = sum(1 for t in self.trades if t.pnl <= 0)
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
        
        # PnL metrics
        total_profit = sum(t.pnl for t in self.trades if t.pnl > 0)
        total_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        net_points = sum(t.pnl_points for t in self.trades)
        
        # Average win/loss
        wins = [t.pnl for t in self.trades if t.pnl > 0]
        losses = [abs(t.pnl) for t in self.trades if t.pnl < 0]
        avg_win = np.mean(wins) if wins else 0.0
        avg_loss = np.mean(losses) if losses else 0.0
        
        # Profit factor
        profit_factor = (total_profit / total_loss) if total_loss > 0 else (float('inf') if total_profit > 0 else 0.0)
        
        # Maximum drawdown
        cumulative_pnl = np.cumsum([t.pnl_points for t in self.trades])
        running_max = np.maximum.accumulate(cumulative_pnl)
        drawdown = running_max - cumulative_pnl
        max_drawdown = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0
        
        return {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': round(win_rate, 2),
            'net_points': round(net_points, 2),
            'max_drawdown': round(max_drawdown, 2),
            'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else 'Infinity',
            'total_profit': round(total_profit, 2),
            'total_loss': round(total_loss, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'trades': [t.to_dict() for t in self.trades]
        }


def fetch_historical_data(symbol: str, timeframe: str, days: int, exchange: str = 'delta') -> Optional[pd.DataFrame]:
    """
    Fetch historical data for backtesting
    
    Args:
        symbol: Trading symbol (e.g., 'BTCUSDT')
        timeframe: Timeframe (e.g., '5m', '1h', '1d')
        days: Number of days to fetch
        exchange: Exchange name ('binance' or 'delta')
        
    Returns:
        DataFrame with OHLCV data or None if failed
    """
    print(f"\n📈 Fetching {days} days of data for {symbol} ({timeframe}) from {exchange}...")
    
    client = CryptoAPIClient(API_KEY, SECRET_KEY)
    result = client.get_historical_data_batch(
        symbol=symbol,
        interval=timeframe,
        days=days,
        exchange_name=exchange
    )
    
    if not result or 'dataframe' not in result:
        print(f"❌ Failed to fetch data")
        return None
    
    df = result['dataframe']
    print(f"✅ Fetched {len(df)} candles")
    
    return df


def run_backtest_period(symbol: str, timeframe: str, days: int, exchange: str,
                        ema9: int, ema21: int, ema50: int,
                        stop_loss_points: float, target_points: float) -> Dict:
    """
    Run backtest for a specific period
    
    Args:
        symbol: Trading symbol
        timeframe: Timeframe
        days: Number of days
        exchange: Exchange name
        ema9: EMA 9 period
        ema21: EMA 21 period
        ema50: EMA 50 period
        stop_loss_points: Stop loss in points
        target_points: Target in points
        
    Returns:
        Dictionary with backtest results
    """
    # Fetch data
    df = fetch_historical_data(symbol, timeframe, days, exchange)
    
    if df is None or len(df) == 0:
        return {
            'period': f"{days} days",
            'error': 'Failed to fetch data'
        }
    
    # Initialize strategy and engine
    strategy = EMACrossoverStrategy(
        ema9=ema9,
        ema21=ema21,
        ema50=ema50,
        stop_loss_points=stop_loss_points,
        target_points=target_points
    )
    
    engine = BacktestEngine(strategy)
    
    # Run backtest
    results = engine.run_backtest(df)
    
    # Add period info
    results['period'] = f"{days} days"
    results['symbol'] = symbol
    results['timeframe'] = timeframe
    results['exchange'] = exchange
    results['start_date'] = df['Open Time'].min().isoformat() if 'Open Time' in df.columns else 'N/A'
    results['end_date'] = df['Open Time'].max().isoformat() if 'Open Time' in df.columns else 'N/A'
    results['total_candles'] = len(df)
    
    return results


def print_backtest_results(results: Dict, period_name: str):
    """
    Print formatted backtest results
    
    Args:
        results: Backtest results dictionary
        period_name: Name of the period (e.g., "3 Months")
    """
    if 'error' in results:
        print(f"\n❌ {period_name}: {results['error']}")
        return
    
    print(f"\n{'='*70}")
    print(f"📊 BACKTEST RESULTS: {period_name}")
    print(f"{'='*70}")
    print(f"Symbol: {results.get('symbol', 'N/A')}")
    print(f"Timeframe: {results.get('timeframe', 'N/A')}")
    print(f"Exchange: {results.get('exchange', 'N/A')}")
    print(f"Period: {results.get('start_date', 'N/A')} to {results.get('end_date', 'N/A')}")
    print(f"Total Candles: {results.get('total_candles', 0)}")
    print(f"\n📈 TRADE STATISTICS:")
    print(f"  Total Trades: {results['total_trades']}")
    print(f"  Winning Trades: {results['winning_trades']}")
    print(f"  Losing Trades: {results['losing_trades']}")
    print(f"  Win Rate: {results['win_rate']}%")
    print(f"\n💰 PERFORMANCE METRICS:")
    print(f"  Net Points: {results['net_points']:+.2f}")
    print(f"  Total Profit: {results['total_profit']:+.2f}")
    print(f"  Total Loss: {results['total_loss']:+.2f}")
    print(f"  Average Win: {results['avg_win']:+.2f}")
    print(f"  Average Loss: {results['avg_loss']:+.2f}")
    print(f"  Maximum Drawdown: {results['max_drawdown']:.2f}")
    print(f"  Profit Factor: {results['profit_factor']}")
    print(f"{'='*70}")


def main():
    """
    Main function to run backtests for multiple periods
    """
    # Configuration (easily configurable)
    config = DEFAULT_CONFIG.copy()
    
    # You can modify these values
    symbol = config['symbol']
    timeframe = config['timeframe']
    exchange = config['exchange']
    ema9 = config['ema_periods']['ema9']
    ema21 = config['ema_periods']['ema21']
    ema50 = config['ema_periods']['ema50']
    stop_loss_points = config['stop_loss_points']
    target_points = config['target_points']
    
    print("="*70)
    print("🚀 EMA CROSSOVER STRATEGY BACKTEST")
    print("="*70)
    print(f"\n📋 Configuration:")
    print(f"  Symbol: {symbol}")
    print(f"  Timeframe: {timeframe}")
    print(f"  Exchange: {exchange}")
    print(f"  EMA Periods: {ema9}, {ema21}, {ema50}")
    print(f"  Stop Loss: {stop_loss_points} points")
    print(f"  Target: {target_points} points")
    print(f"  Risk:Reward: 1:2")
    print("\n" + "="*70)
    
    # Define periods to test
    periods = [
        (90, "3 Months"),
        (180, "6 Months"),
        (365, "1 Year"),
        (730, "2 Years")
    ]
    
    all_results = {}
    
    # Run backtest for each period
    for days, period_name in periods:
        try:
            results = run_backtest_period(
                symbol=symbol,
                timeframe=timeframe,
                days=days,
                exchange=exchange,
                ema9=ema9,
                ema21=ema21,
                ema50=ema50,
                stop_loss_points=stop_loss_points,
                target_points=target_points
            )
            
            all_results[period_name] = results
            print_backtest_results(results, period_name)
            
        except Exception as e:
            print(f"\n❌ Error running backtest for {period_name}: {e}")
            import traceback
            traceback.print_exc()
            all_results[period_name] = {'error': str(e)}
    
    # Summary table
    print(f"\n\n{'='*70}")
    print("📊 SUMMARY TABLE")
    print(f"{'='*70}")
    print(f"{'Period':<15} {'Trades':<8} {'Win Rate':<10} {'Net Points':<12} {'Max DD':<10} {'Profit Factor':<15}")
    print("-"*70)
    
    for period_name, results in all_results.items():
        if 'error' not in results:
            print(f"{period_name:<15} {results['total_trades']:<8} {results['win_rate']:<10}% "
                  f"{results['net_points']:>+10.2f} {results['max_drawdown']:>9.2f} {str(results['profit_factor']):<15}")
        else:
            print(f"{period_name:<15} {'ERROR':<8}")
    
    print("="*70)
    
    # Save results to JSON file
    output_file = 'backtest_results.json'
    with open(output_file, 'w') as f:
        json.dump(all_results, f, indent=2, default=str)
    
    print(f"\n✅ Results saved to {output_file}")


if __name__ == '__main__':
    main()
