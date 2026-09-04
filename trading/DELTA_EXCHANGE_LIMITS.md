# Delta Exchange (India) API – Server-side limits

Yeh limits **exchange ki taraf se** hain; inhe code se change nahi kiya ja sakta.

---

## 1. Rate limits (REST API)

| Item | Limit |
|------|--------|
| **Quota** | 10,000 units per **5 minute** window |
| **Reset** | Har 5 minute baad quota full reset |
| **429** | Limit exceed → `HTTP 429 Too Many Requests`; header `X-RATE-LIMIT-RESET` (ms) bataata hai kab next request kar sakte ho |

**Endpoint weight (5 min window me deduct hota hai):**

| Weight | Endpoints |
|--------|-----------|
| **3** | Get Products, Orderbook, Tickers, Open Orders, Open Positions, Balances, **OHLC Candles** |
| 5 | Place/Edit/Delete Order, Add Position Margin |
| 10 | Order History, Fills, Txn Logs |
| 25 | Batch Order APIs |

Example: 1000 OHLC requests = 1000 × 3 = 3000 quota. Baaki 7000 weight 5 min me aur calls ke liye.

---

## 2. Product-level rate limit (matching engine)

| Item | Limit |
|------|--------|
| **Per product** | **500 operations per second** (e.g. orders) |
| REST quota theek ho to bhi is limit ke upar jane par **429** mil sakta hai |

---

## 3. Authentication / request limits

| Item | Limit |
|------|--------|
| **Signature validity** | Request Delta tak **signature create hone ke 5 second** ke andar pahunchna zaroori; 5 sec purana = reject |
| **API key creation block** | Galat OTP/MFA 5+ baar → API key creation **30 min** ke liye block |

---

## 4. Historical OHLC Candles – observed limits

Docs me **“max candles per request”** ya **“max history depth”** number nahi diya. Humne jo **observe** kiya (India API):

| Item | Observed / inferred |
|------|----------------------|
| **Per-request cap (e.g. 5m)** | Lagbhag **~4000 candles** tak ek response (timeframe/resolution par depend kar sakta hai) |
| **History depth (e.g. 5m)** | ~14–15 din tak data milta hai; 30 din ka 5m data ek request me nahi milta |
| **Bada timeframe (e.g. 1h, 15m)** | Kam candles same period ke liye → zyada din ka data mil sakta hai |

Ye numbers **documented** nahi, sirf behaviour se; future me Delta change kar sakta hai.

---

## 5. Pagination (candles ke liye nahi)

Cursor-based pagination **Products, Orders, Order history, Fills, Wallet transactions** ke liye hai.  
**OHLC Candles** endpoint ke liye docs me pagination/cursor **nahi** diya — isliye zyada history ke liye alag-alag time windows me multiple requests (batch) karte hain.

---

## 6. Environment / URLs

| Environment | Base URL |
|-------------|----------|
| **Production (India)** | `https://api.india.delta.exchange` |
| **Testnet / Demo** | `https://cdn-ind.testnet.deltaex.org` |

India keys sirf India URL ke saath; Global (`api.delta.exchange`) alag product hai.

---

## 7. Zyada limit chahiye ho to

Docs: agar legit need ho to **[[email protected]](mailto:api@delta.exchange)** par email karke higher rate limits discuss kar sakte ho.

---

*Source: [Delta Exchange API docs](https://docs.delta.exchange) + project me candles fetch behaviour.*
