# CryptoBot verify skill

## bot_server.py
Drive via Python inline scripts — no TG/Kraken env needed:
```bash
python3 -c "import bot_server as bs; print(bs.TG_TOKEN)"
```
- TG_TOKEN/TG_CHAT_ID use `.get()` defaults — import succeeds with no env vars
- `PaperTrader(no_persist=True)` is always safe to instantiate
- DB warns "learning disabled" when DATABASE_URL not set — expected

### Testing learning methods that need db.connected
Set `bs.db.conn = object()` to make `db.connected` truthy (sentinel); then preload
caches and set their timestamps to prevent DB refresh calls:
```python
bs.db.conn = object()
pt = bs.PaperTrader()   # no_persist=False
pt._exit_cache['XBTUSD'] = {'trailing stop': {'n':6,'avg_pnl':-0.5,'early':4}, ...}
pt._exit_ts['XBTUSD'] = time.time() + 9999   # prevent refresh
```
Restore: `bs.db.conn = None`

Note: `_feature_multiplier`, `_atr_mult`, `_trail_only`, `_min_conf_threshold` all
return safe defaults when `no_persist=True` — test them via the above mock pattern.

## backtest.py
Kraken is proxy-blocked in this environment. Test via CSV:
```bash
python3 -c "
import csv, math
rows=[[1700000000+i*3600, 40000+math.sin(i*.3)*500, 40200, 39800, 40100, 100] for i in range(250)]
with open('/tmp/t.csv','w') as f:
    csv.writer(f).writerows([['time','open','high','low','close','volume']]+rows)
"
python3 backtest.py --csv /tmp/t.csv --pair XBTUSD
```

## dashboard.py
Flask not pre-installed — `pip install flask==3.0.3 -q` first.
```bash
PORT=18080 python3 dashboard.py &>/tmp/dashboard.log &
sleep 2
curl -s http://127.0.0.1:18080/healthz    # → ok
curl -s http://127.0.0.1:18080/api/stats  # → {"configured":false,...} without DB
```
compute_stats() requires full DB row shape — include: pnl, held_mins, reason,
confidence, balance_after, ts, coin, side.

## Calibration tiers
- wr≥55 → 1.0, wr≥50 → 0.85, wr≥45 → 0.65, wr≥40 → 0.50, else → 0.35
- Needs n≥15 samples or returns 1.0
- Pre-load `pt._calib` and set `pt._calib_ts = 1e18` to test without DB

## detect_regime
- Strong uptrend (close rising ~50/candle over 200 candles) → TRENDING
- Tiny oscillations (sin*30 amplitude, large ATR) → CHOPPY
- CHOPPY gate in evaluate() sets sig=HOLD; TRENDING adds +0.05 to confidence
