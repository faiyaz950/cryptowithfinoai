# Crypto Trading Website - EMA & Candlestick Chart

## 🚀 Quick Start

### 1. Install Dependencies
```bash
pip3 install flask flask-cors pandas numpy requests
```

### 2. Database (Django ORM) — default SQLite

**Local dev ke liye kuch set karne ki zaroorat nahi:** backend start par `crypto_trading.sqlite3` project root me ban jata hai aur tables migrate ho jati hain.

Optional:

```bash
# Custom SQLite file path
export SQLITE_PATH=/absolute/path/to/my_crypto.db
```

**MySQL (optional):** agar aap MySQL use karna chahein:

```bash
export USE_MYSQL=true
export MYSQL_HOST=127.0.0.1
export MYSQL_PORT=3306
export MYSQL_USER=root
export MYSQL_PASSWORD=your_mysql_password
export MYSQL_DATABASE=crypto_trading
```

**BYOK / encryption (recommended for any environment):**

```bash
export BYOK_ENCRYPTION_KEY=replace_with_fernet_or_passphrase
```

**SMTP (OTP email delivery):**

```bash
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USERNAME=you@example.com
export SMTP_PASSWORD=your_app_password
export SMTP_FROM_EMAIL=you@example.com
export SMTP_USE_TLS=true
export EMAIL_OTP_DEBUG=false
```

Backend startup par Django ORM migrate karta hai; tables include (among others):

- `app_logins`, `broker_logins`, `demo_orders`
- `user_accounts`, `user_sessions`, `exchange_accounts`, `byok_orders`, `email_change_otps`

### 3. Start Server
```bash
python3 backend_api.py
```

### 4. Open Browser
```
http://localhost:2000
```

## 📁 Files

- `backend_api.py` - Flask backend API
- `fetch_trading_data.py` - Crypto API client
- `static/index.html` - Frontend website
- `backtest_strategy.py` - Complete trading strategy backtesting script

## 🎯 Features

- ✅ Candlestick Charts (Open, High, Low, Close)
- ✅ EMA Lines (9, 21, 50 periods)
- ✅ Multiple Symbols (BTC, ETH, BNB, etc.)
- ✅ Multiple Intervals (1m, 5m, 1h, 4h, 1d)
- ✅ Market Info (Price, 24h change, high/low)

## 📡 API Endpoints

- `GET /api/candles` - Candle data with EMA
- `GET /api/market-info` - Market information
- `GET /api/health` - Health check

### 🔐 BYOK (Bring Your Own Key) APIs

- `POST /api/auth/register` - Create platform user
- `POST /api/auth/login` - Login and receive session token
- `POST /api/auth/key-login` - Login/signup via exchange API key + secret
- `GET /api/auth/me` - Current user/session
- `POST /api/auth/logout` - Logout
- `POST /api/byok/exchange-accounts` - Connect exchange key/secret (encrypted at rest)
- `GET /api/byok/exchange-accounts` - List linked exchange accounts
- `POST /api/byok/exchange-accounts/<id>/verify` - Re-verify linked account
- `POST /api/byok/exchange-accounts/<id>/revoke` - Revoke account
- `POST /api/byok/orders` - Place authenticated BYOK order
- `POST /api/byok/orders/cancel` - Cancel BYOK order
- `GET /api/byok/orders` - Fetch user BYOK order history
- `GET /api/byok/positions?exchange_account_id=<id>` - Fetch positions for linked account

### 👤 Profile & Security APIs

- `GET /api/profile` - User profile + Delta API connection status
- `PATCH /api/profile/name` - Update full name (2-50 chars)
- `POST /api/profile/email/request-otp` - Send OTP for email change
- `POST /api/profile/email/verify-otp` - Verify OTP and update email
- `POST /api/profile/password/change` - Change password (current password required)
- `GET /api/profile/delta-api/status` - Delta API status (`connected`/`invalid`/`not_added`)
- `POST /api/profile/delta-api` - Add Delta API key (Trade+Read only, no withdrawal)
- `DELETE /api/profile/delta-api` - Delete Delta API key (requires `confirm=true`)

## 📊 Backtesting Strategy

### Running the Backtest

The `backtest_strategy.py` script provides a complete backtesting solution for the EMA crossover strategy:

```bash
python3 backtest_strategy.py
```

### Strategy Rules

- **Indicators**: EMA 9, EMA 21, EMA 50
- **Entry Rules**:
  - BUY: EMA 9 crosses above BOTH EMA 21 and EMA 50 (after candle close)
  - SELL: EMA 9 crosses below BOTH EMA 21 and EMA 50 (after candle close)
- **Exit Rules**:
  - Fixed Stop Loss: 400 points
  - Fixed Target: 800 points
  - Risk:Reward = 1:2
  - Only one trade at a time (no overlapping trades)

### Backtesting Periods

The script automatically tests the strategy over:
- Last 3 months
- Last 6 months
- Last 1 year
- Last 2 years

### Output Metrics

For each period, the script provides:
- Total number of trades
- Number of winning trades
- Number of losing trades
- Win rate (%)
- Net points gained or lost
- Maximum drawdown
- Profit factor
- Average win/loss
- Total profit/loss

### Configuration

You can easily configure the strategy by modifying the `DEFAULT_CONFIG` dictionary in `backtest_strategy.py`:

```python
DEFAULT_CONFIG = {
    'symbol': 'BTCUSDT',      # Trading symbol
    'timeframe': '5m',         # Timeframe (1m, 5m, 15m, 1h, 4h, 1d, etc.)
    'exchange': 'binance',      # Exchange (binance or delta)
    'ema_periods': {
        'ema9': 9,
        'ema21': 21,
        'ema50': 50
    },
    'stop_loss_points': 400,   # Stop loss in points
    'target_points': 800       # Target in points
}
```

### Results

Results are displayed in the console and saved to `backtest_results.json` for further analysis.

## ⚠️ Important

API credentials `backend_api.py` and `backtest_strategy.py` mein update karein:
```python
API_KEY = "your_api_key"
SECRET_KEY = "your_secret_key"
```
