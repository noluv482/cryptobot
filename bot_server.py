"""
CryptoBot Server — Cloud Version
Runs 24/7 with no screen needed.
Control everything from Telegram buttons.
"""

import requests
import threading
import time
import os
import json
import xml.etree.ElementTree as ET
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
TG_TOKEN   = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
TG_URL     = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
BASE_URL   = "https://api.kraken.com/0/public"

EMA_PERIOD    = 14
RSI_PERIOD    = 14
CANDLE_LIMIT  = 60
INTERVAL      = 5
REFRESH_SEC   = 30
CONFIRM_TICKS = 2

PAPER_START    = 100.0
PAPER_TARGET   = 50000.0
PAPER_FLOOR    = 50.0
LEVERAGE       = 3
RISK_MIN       = 0.05   # 5%  margin at low confidence
RISK_MAX       = 0.20   # 20% margin at high confidence
MAX_TRADE_GAIN = 0.08   # close when up 8%
MAX_TRADE_LOSS = 0.04   # close when down 4%
MAX_TRADE_MINS = 60     # force close after 60 min

SAVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_state.json")

SCAN_UNIVERSE = [
    {"name": "SOL/USD",   "pair": "SOLUSD",   "alert_buffer": 0.10},
    {"name": "BTC/USD",   "pair": "XBTUSD",   "alert_buffer": 50.0},
    {"name": "ETH/USD",   "pair": "ETHUSD",   "alert_buffer": 2.0},
    {"name": "XRP/USD",   "pair": "XRPUSD",   "alert_buffer": 0.01},
    {"name": "DOGE/USD",  "pair": "XDGUSD",   "alert_buffer": 0.002},
    {"name": "ADA/USD",   "pair": "ADAUSD",   "alert_buffer": 0.002},
    {"name": "AVAX/USD",  "pair": "AVAXUSD",  "alert_buffer": 0.05},
    {"name": "LINK/USD",  "pair": "LINKUSD",  "alert_buffer": 0.02},
    {"name": "DOT/USD",   "pair": "DOTUSD",   "alert_buffer": 0.01},
    {"name": "LTC/USD",   "pair": "LTCUSD",   "alert_buffer": 0.5},
    {"name": "ATOM/USD",  "pair": "ATOMUSD",  "alert_buffer": 0.02},
    {"name": "UNI/USD",   "pair": "UNIUSD",   "alert_buffer": 0.02},
    {"name": "AAVE/USD",  "pair": "AAVEUSD",  "alert_buffer": 0.5},
    {"name": "INJ/USD",   "pair": "INJUSD",   "alert_buffer": 0.05},
    {"name": "SUI/USD",   "pair": "SUIUSD",   "alert_buffer": 0.005},
    {"name": "APT/USD",   "pair": "APTUSD",   "alert_buffer": 0.02},
    {"name": "ARB/USD",   "pair": "ARBUSD",   "alert_buffer": 0.005},
    {"name": "NEAR/USD",  "pair": "NEARUSD",  "alert_buffer": 0.01},
    {"name": "ALGO/USD",  "pair": "ALGOUSD",  "alert_buffer": 0.002},
    {"name": "FIL/USD",   "pair": "FILUSD",   "alert_buffer": 0.02},
    {"name": "BCH/USD",   "pair": "BCHUSD",   "alert_buffer": 1.0},
    {"name": "PEPE/USD",  "pair": "PEPEUSD",  "alert_buffer": 0.0000001},
    {"name": "BONK/USD",  "pair": "BONKUSD",  "alert_buffer": 0.0000001},
]

RANKS = [
    {"name": "Rookie",    "min": 0,     "emoji": "🟤", "unlock": "You're just getting started. Every trade is a lesson."},
    {"name": "Trader",    "min": 250,   "emoji": "⚪", "unlock": "You proved you can grow. Now stay consistent."},
    {"name": "Pro",       "min": 500,   "emoji": "🟡", "unlock": "You're not lucky — you're skilled. Keep pushing."},
    {"name": "Expert",    "min": 1000,  "emoji": "🟠", "unlock": "Most bots never get here. You're in rare territory."},
    {"name": "Elite",     "min": 2500,  "emoji": "🔵", "unlock": "You're in the top 1%. Keep that momentum going."},
    {"name": "Legend",    "min": 5000,  "emoji": "🔴", "unlock": "Half way to $10k. You're built different."},
    {"name": "GOAT",      "min": 10000, "emoji": "👑", "unlock": "Hit $10,000. Most never get here. Keep stacking."},
    {"name": "Diamond",   "min": 15000, "emoji": "💎", "unlock": "$15k. You're not stopping, are you."},
    {"name": "Immortal",  "min": 25000, "emoji": "⚡", "unlock": "$25,000. A quarter of the way to greatness."},
    {"name": "Mythic",    "min": 35000, "emoji": "🌙", "unlock": "$35k and rising. The goal is in reach."},
    {"name": "OVERLORD",  "min": 50000, "emoji": "🏆", "unlock": "You turned $100 into $50,000. OVERLORD status."},
]

NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
]

COIN_KEYWORDS = {
    "SOLUSD":  ["solana","sol ","$sol"],
    "XBTUSD":  ["bitcoin","btc","$btc"],
    "ETHUSD":  ["ethereum","eth ","$eth"],
    "XRPUSD":  ["xrp","ripple","$xrp"],
    "XDGUSD":  ["dogecoin","doge","$doge"],
    "ADAUSD":  ["cardano","ada","$ada"],
    "AVAXUSD": ["avalanche","avax","$avax"],
    "LINKUSD": ["chainlink","link","$link"],
    "DOTUSD":  ["polkadot","dot","$dot"],
    "LTCUSD":  ["litecoin","ltc","$ltc"],
    "ATOMUSD": ["cosmos","atom","$atom"],
    "UNIUSD":  ["uniswap","uni","$uni"],
    "AAVEUSD": ["aave","$aave"],
    "INJUSD":  ["injective","inj","$inj"],
    "SUIUSD":  ["sui","$sui"],
    "APTUSD":  ["aptos","apt","$apt"],
    "ARBUSD":  ["arbitrum","arb","$arb"],
    "NEARUSD": ["near","$near"],
    "ALGOUSD": ["algorand","algo","$algo"],
    "FILUSD":  ["filecoin","fil","$fil"],
    "BCHUSD":  ["bitcoin cash","bch","$bch"],
    "PEPEUSD": ["pepe","$pepe"],
    "BONKUSD": ["bonk","$bonk"],
}

BULLISH_WORDS = ["surge","rally","pump","moon","breakout","all-time high","ath",
                 "bullish","approved","etf","partnership","upgrade","record",
                 "institutional","inflow","rises","jumps","soars","adoption"]
BEARISH_WORDS = ["crash","drop","dump","ban","hack","exploit","lawsuit","sec",
                 "bearish","fear","panic","liquidation","outflow","falls",
                 "plunges","tumbles","decline","warning","investigation"]

# ── Database ──────────────────────────────────────────────────────────────────
class Database:
    def __init__(self):
        self.conn = None
        url = os.environ.get("DATABASE_URL")
        if not url:
            print("[DB] No DATABASE_URL — learning disabled, running on JSON only")
            return
        try:
            try:
                import psycopg2
            except ImportError:
                print("[DB] psycopg2 not installed — learning disabled")
                return
            self.conn = psycopg2.connect(url, connect_timeout=5)
            self.conn.autocommit = True
            self._init_schema()
            print("[DB] Connected — learning enabled")
        except Exception as e:
            print(f"[DB] Connect error: {e}")
            self.conn = None

    def _init_schema(self):
        with self.conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id          SERIAL PRIMARY KEY,
                    ts          FLOAT,
                    coin        TEXT,
                    pair        TEXT,
                    side        TEXT,
                    entry       FLOAT,
                    exit_price  FLOAT,
                    pnl         FLOAT,
                    held_mins   FLOAT,
                    reason      TEXT,
                    confidence  FLOAT,
                    nasdaq_mood TEXT,
                    news_sent   TEXT,
                    balance_after FLOAT
                )
            """)

    def save_trade(self, t):
        if not self.conn: return
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO trades
                      (ts, coin, pair, side, entry, exit_price, pnl, held_mins,
                       reason, confidence, nasdaq_mood, news_sent, balance_after)
                    VALUES
                      (%(ts)s,%(coin)s,%(pair)s,%(side)s,%(entry)s,%(exit_price)s,
                       %(pnl)s,%(held_mins)s,%(reason)s,%(confidence)s,
                       %(nasdaq_mood)s,%(news_sent)s,%(balance_after)s)
                """, t)
        except Exception as e:
            print(f"[DB] Save error: {e}")

    def coin_win_rates(self, min_trades=3):
        """Per-pair win rate for coins with enough history."""
        if not self.conn: return {}
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    SELECT pair,
                           COUNT(*) AS n,
                           ROUND(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)::numeric
                                 / COUNT(*) * 100, 1) AS wr
                    FROM trades
                    GROUP BY pair
                    HAVING COUNT(*) >= %s
                """, (min_trades,))
                return {row[0]: {"n": row[1], "wr": float(row[2])} for row in cur.fetchall()}
        except Exception as e:
            print(f"[DB] coin_win_rates error: {e}")
            return {}

    def confidence_calibration(self):
        """Win rates split by confidence tier."""
        if not self.conn: return {}
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    SELECT CASE WHEN confidence >= 0.6 THEN 'high' ELSE 'low' END AS tier,
                           COUNT(*) AS n,
                           ROUND(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)::numeric
                                 / COUNT(*) * 100, 1) AS wr
                    FROM trades
                    GROUP BY tier
                """)
                return {row[0]: {"n": row[1], "wr": float(row[2])} for row in cur.fetchall()}
        except Exception as e:
            print(f"[DB] confidence_calibration error: {e}")
            return {}

    def best_exit_reason(self):
        """Which exit reason has the best avg PnL."""
        if not self.conn: return {}
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    SELECT reason,
                           COUNT(*) AS n,
                           ROUND(AVG(pnl)::numeric, 3) AS avg_pnl
                    FROM trades
                    GROUP BY reason
                    ORDER BY avg_pnl DESC
                """)
                return [(row[0], row[1], float(row[2])) for row in cur.fetchall()]
        except Exception as e:
            print(f"[DB] best_exit_reason error: {e}")
            return []

    @property
    def connected(self):
        return self.conn is not None

db = Database()

# ── Shared state ──────────────────────────────────────────────────────────────
news_sentiment  = {p: {"sentiment": "NEUTRAL", "headline": "", "score": 0} for p in COIN_KEYWORDS}
market_mood     = {"nasdaq": "NEUTRAL", "change_pct": 0.0}
_paused         = False
_current_coin   = SCAN_UNIVERSE[0]
_last_update_id = 0
_seen_headlines = set()
_state_lock     = threading.Lock()   # guards _paused and _current_coin

# ── Telegram helpers ──────────────────────────────────────────────────────────
def tg(msg):
    try:
        r = requests.post(TG_URL, data={"chat_id": TG_CHAT_ID, "text": msg,
                                        "parse_mode": "Markdown"}, timeout=8)
        if not r.ok:
            print(f"[TG] Error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[TG] Send error: {e}")

def tg_buttons(msg, buttons):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT_ID, "text": msg,
                                "parse_mode": "Markdown",
                                "reply_markup": {"inline_keyboard": buttons}}, timeout=8)
        if not r.ok:
            print(f"[TG] Buttons error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[TG] Buttons send error: {e}")

def tg_answer(cb_id, text=""):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id": cb_id, "text": text}, timeout=5)
    except Exception:
        pass

# ── Data ──────────────────────────────────────────────────────────────────────
def get_klines(pair):
    r = requests.get(f"{BASE_URL}/OHLC", params={"pair": pair, "interval": INTERVAL}, timeout=10)
    payload = r.json()
    if payload.get("error"):
        raise ValueError(f"Kraken OHLC error: {payload['error']}")
    rkey = list(payload["result"].keys())[0]
    data = payload["result"][rkey][-CANDLE_LIMIT:]
    closes  = [float(c[4]) for c in data]
    highs   = [float(c[2]) for c in data]
    lows    = [float(c[3]) for c in data]
    return closes, highs, lows

def get_price(pair):
    r = requests.get(f"{BASE_URL}/Ticker", params={"pair": pair}, timeout=10)
    payload = r.json()
    if payload.get("error"):
        raise ValueError(f"Kraken Ticker error: {payload['error']}")
    key = list(payload["result"].keys())[0]
    return float(payload["result"][key]["c"][0])

def calc_ema(closes):
    k   = 2.0 / (EMA_PERIOD + 1)
    ema = sum(closes[:EMA_PERIOD]) / EMA_PERIOD
    for p in closes[EMA_PERIOD:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_rsi(closes):
    gains, losses = [], []
    for i in range(1, RSI_PERIOD + 1):
        d = closes[i] - closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag = sum(gains) / RSI_PERIOD
    al = sum(losses) / RSI_PERIOD
    for i in range(RSI_PERIOD, len(closes)):
        d  = closes[i] - closes[i-1]
        ag = (ag*(RSI_PERIOD-1) + max(d,0))  / RSI_PERIOD
        al = (al*(RSI_PERIOD-1) + max(-d,0)) / RSI_PERIOD
    if al == 0 and ag == 0: return 50.0
    return round(100 - 100/(1 + ag/al), 1) if al > 0 else 100.0

# ── Rank coins ────────────────────────────────────────────────────────────────
def rank_coins():
    scores    = []
    db_rates  = db.coin_win_rates()   # {} if DB not connected
    for coin in SCAN_UNIVERSE:
        pair = coin["pair"]
        try:
            closes, highs, lows = get_klines(pair)
            volatility = (max(highs) - min(lows)) / closes[-1] * 100
            ema        = calc_ema(closes)
            rsi        = calc_rsi(closes)
            above_ema  = closes[-1] > ema
            score = 0
            score += min(volatility * 10, 40)
            score += 20 if above_ema else 0
            score += 20 if 40 <= rsi <= 60 else 0
            score += 10 if 30 < rsi < 70 else -10
            news = news_sentiment.get(pair, {})
            if news.get("sentiment") == "BULLISH": score += 15
            if news.get("sentiment") == "BEARISH": score -= 15

            # ── Adaptive learning boost ───────────────────────────────────
            learned = ""
            if pair in db_rates:
                wr = db_rates[pair]["wr"]
                n  = db_rates[pair]["n"]
                if   wr >= 65: score += 20; learned = f"Learned {wr:.0f}%WR✅"
                elif wr >= 55: score += 10; learned = f"Learned {wr:.0f}%WR"
                elif wr <= 35: score -= 20; learned = f"Learned {wr:.0f}%WR❌"
                elif wr <= 45: score -= 10; learned = f"Learned {wr:.0f}%WR"

            reason = []
            if volatility > 2:  reason.append(f"Volatility {volatility:.1f}%")
            if above_ema:       reason.append("Above EMA")
            if 40 <= rsi <= 60: reason.append(f"RSI {rsi} ideal")
            if news.get("sentiment") == "BULLISH": reason.append("Bullish news")
            if learned:         reason.append(learned)

            scores.append({"name": coin["name"], "pair": pair,
                           "score": round(score,1), "rsi": rsi,
                           "volatility": round(volatility,2),
                           "news": news.get("sentiment","NEUTRAL"),
                           "reason": ", ".join(reason) or "No signal",
                           "alert_buffer": coin["alert_buffer"]})
        except Exception:
            pass
    return sorted(scores, key=lambda x: x["score"], reverse=True)

def analyse_intelligence(trades):
    if not trades:
        return "📊 No trades yet — intelligence report available after first trade."

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = len(trades)

    # High vs low confidence split (threshold 60%)
    hi = [t for t in trades if t.get("confidence", 0) >= 0.60]
    lo = [t for t in trades if t.get("confidence", 0) <  0.60]
    hi_wr = (sum(1 for t in hi if t["pnl"] > 0) / len(hi) * 100) if hi else 0
    lo_wr = (sum(1 for t in lo if t["pnl"] > 0) / len(lo) * 100) if lo else 0

    # Profit factor
    gross_win  = sum(t["pnl"] for t in wins)  or 0
    gross_loss = abs(sum(t["pnl"] for t in losses)) or 1
    pf = round(gross_win / gross_loss, 2)

    # Best coin by win rate (min 2 trades)
    coins = {}
    for t in trades:
        c = t.get("coin", "?")
        coins.setdefault(c, {"w": 0, "n": 0})
        coins[c]["n"] += 1
        if t["pnl"] > 0: coins[c]["w"] += 1
    best_coin = max(
        ((c, d) for c, d in coins.items() if d["n"] >= 2),
        key=lambda x: x[1]["w"] / x[1]["n"],
        default=(None, None)
    )

    # Current streak
    streak, streak_type = 0, ""
    for t in reversed(trades):
        kind = "W" if t["pnl"] > 0 else "L"
        if streak == 0:
            streak_type = kind
        if kind == streak_type:
            streak += 1
        else:
            break

    # Avg hold time
    held = [t.get("held_mins", 0) for t in trades]
    avg_hold = round(sum(held) / len(held), 1) if held else 0

    # Best / worst trade
    best  = max(trades, key=lambda t: t["pnl"])
    worst = min(trades, key=lambda t: t["pnl"])

    conf_arrow = "🟢" if hi_wr > lo_wr else "🔴" if hi_wr < lo_wr else "⚫"

    lines = [
        "*🧠 Intelligence Report*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 Trades: `{total}` | Win Rate: `{len(wins)/total*100:.0f}%`",
        f"💰 Profit Factor: `{pf}x` ({'good' if pf >= 1.5 else 'needs work'})",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"*Confidence accuracy* {conf_arrow}",
        f"  High (≥60%): `{len(hi)}` trades → `{hi_wr:.0f}%` win rate",
        f"  Low  (<60%): `{len(lo)}` trades → `{lo_wr:.0f}%` win rate",
    ]
    if hi_wr > lo_wr + 5:
        lines.append("  ✅ Confidence scoring is working")
    elif lo_wr > hi_wr + 5:
        lines.append("  ⚠️ Low-confidence trades outperforming — needs tuning")
    else:
        lines.append("  📌 Not enough data to judge yet")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        f"🔥 Current streak: `{streak} {'win' if streak_type=='W' else 'loss'}{'s' if streak>1 else ''}`",
        f"⏱ Avg hold time: `{avg_hold} min`",
        f"🏅 Best trade:  `+{best['pnl']:.2f}$` ({best.get('coin','?')})",
        f"💀 Worst trade: `{worst['pnl']:.2f}$` ({worst.get('coin','?')})",
    ]
    if best_coin[0]:
        wr_pct = best_coin[1]['w'] / best_coin[1]['n'] * 100
        lines.append(f"🪙 Best coin: *{best_coin[0]}* `{wr_pct:.0f}%` WR ({best_coin[1]['n']} trades)")

    # Exit reason breakdown
    reasons = {}
    for t in trades:
        r = t.get("reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("*Exit reasons:*")
    for r, n in sorted(reasons.items(), key=lambda x: -x[1]):
        lines.append(f"  `{r}`: {n}x")

    # DB stats (lifetime, survives redeploys)
    if db.connected:
        db_rates = db.coin_win_rates()
        exit_stats = db.best_exit_reason()
        cal = db.confidence_calibration()
        lifetime_total = sum(v["n"] for v in db_rates.values())
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"*📦 Lifetime DB:* `{lifetime_total}` trades stored")
        if cal:
            for tier, d in cal.items():
                lines.append(f"  {tier.capitalize()} conf: `{d['wr']:.0f}%` WR ({d['n']} trades)")
        if exit_stats:
            best_exit = exit_stats[0]
            lines.append(f"  Best exit: `{best_exit[0]}` avg `{best_exit[2]:+.2f}$`")
    else:
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("⚫ _Add PostgreSQL on Railway to enable lifetime learning_")

    return "\n".join(lines)

def get_rank(balance):
    for r in reversed(RANKS):
        if balance >= r["min"]: return r
    return RANKS[0]

def get_next_rank(balance):
    for r in RANKS:
        if balance < r["min"]: return r
    return RANKS[-1]

# ── Paper trader ──────────────────────────────────────────────────────────────
class PaperTrader:
    def __init__(self):
        self.balance       = PAPER_START
        self.position      = None
        self.trades        = []
        self.peak          = PAPER_START
        self.current_rank  = RANKS[0]["name"]
        self.session_start = PAPER_START
        self._load()

    def _load(self):
        if os.path.exists(SAVE_FILE):
            try:
                with open(SAVE_FILE) as f:
                    d = json.load(f)
                self.balance      = d.get("balance", PAPER_START)
                self.peak         = d.get("peak", self.balance)
                self.trades       = d.get("trades", [])
                self.position     = d.get("position", None)
                self.current_rank = d.get("current_rank", RANKS[0]["name"])
                print(f"[Paper] Loaded — Balance: ${self.balance:.2f}")
            except Exception as e:
                print(f"[Paper] Load error: {e}")

    def _save(self):
        try:
            with open(SAVE_FILE, "w") as f:
                json.dump({"balance": self.balance, "peak": self.peak,
                           "trades": self.trades, "position": self.position,
                           "current_rank": self.current_rank}, f, indent=2)
        except Exception as e:
            print(f"[Paper] Save error: {e}")

    @property
    def wins(self):
        return sum(1 for t in self.trades if t["pnl"] > 0)

    @property
    def win_rate(self):
        return (self.wins / len(self.trades) * 100) if self.trades else 0.0

    @property
    def total_pnl(self):
        return round(sum(t["pnl"] for t in self.trades), 2)

    def unrealized_pnl(self, price):
        if not self.position: return 0.0
        p    = self.position
        move = (price - p["entry"]) / p["entry"]
        if p["side"] == "SHORT": move = -move
        return round(move * p["margin"] * LEVERAGE, 4)

    def on_signal(self, sig, price, stop, target, name, confidence):
        with _state_lock:
            paused = _paused
        if paused or self.balance < PAPER_FLOOR: return
        if self.balance >= PAPER_TARGET: return

        if self.position:
            p    = self.position
            side = p["side"]
            move = (price - p["entry"]) / p["entry"]
            if side == "SHORT": move = -move
            mins_open = (time.time() - p.get("opened_at", time.time())) / 60

            if move >= MAX_TRADE_GAIN:
                self._close(price, name, "profit cap")
            elif move <= -MAX_TRADE_LOSS:
                self._close(price, name, "stop loss")
            elif mins_open >= MAX_TRADE_MINS:
                self._close(price, name, "time limit")
            elif side == "LONG"  and price >= p.get("target", float("inf")):
                self._close(price, name, "take profit")
            elif side == "SHORT" and price <= p.get("target", 0):
                self._close(price, name, "take profit")
            elif (side == "LONG" and sig == "SELL") or (side == "SHORT" and sig == "BUY"):
                self._close(price, name, "signal flip")

        if not self.position:
            if sig == "BUY":
                self._open("LONG",  price, name, target, confidence, _current_coin["pair"])
            elif sig == "SELL":
                self._open("SHORT", price, name, target, confidence, _current_coin["pair"])

    def _open(self, side, price, name, target, confidence, pair):
        risk      = RISK_MIN + (RISK_MAX - RISK_MIN) * confidence
        margin    = round(self.balance * risk, 4)
        contracts = round((margin * LEVERAGE) / price, 6)
        conf_pct  = int(confidence * 100)
        self.position = {"side": side, "entry": price,
                         "contracts": contracts, "margin": margin,
                         "target": target, "opened_at": time.time(),
                         "confidence": confidence,
                         "pair": pair, "name": name}
        self._save()
        tg(f"📂 *Trade OPENED — {name}*\n"
           f"{'🟢 LONG' if side=='LONG' else '🔴 SHORT'} @ `${price:.4f}`\n"
           f"Confidence: `{conf_pct}%` | Size: `{risk*100:.1f}%` of balance\n"
           f"Margin: `${margin:.2f}` | {LEVERAGE}x leverage\n"
           f"Balance: `${self.balance:.2f}`")

    def _close(self, price, name, reason):
        if not self.position: return
        pnl          = self.unrealized_pnl(price)
        self.balance = round(self.balance + pnl, 4)
        self.peak    = max(self.peak, self.balance)
        held_mins    = round((time.time() - self.position.get("opened_at", time.time())) / 60, 1)
        trade_rec = {"side":       self.position["side"],
                     "entry":      self.position["entry"],
                     "exit":       price,
                     "pnl":        pnl,
                     "coin":       self.position.get("name", name),
                     "confidence": self.position.get("confidence", 0.0),
                     "held_mins":  held_mins,
                     "reason":     reason,
                     "ts":         time.time()}
        self.trades.append(trade_rec)
        db.save_trade({
            "ts":           trade_rec["ts"],
            "coin":         trade_rec["coin"],
            "pair":         self.position.get("pair", ""),
            "side":         trade_rec["side"],
            "entry":        trade_rec["entry"],
            "exit_price":   price,
            "pnl":          pnl,
            "held_mins":    held_mins,
            "reason":       reason,
            "confidence":   trade_rec["confidence"],
            "nasdaq_mood":  market_mood["nasdaq"],
            "news_sent":    news_sentiment.get(self.position.get("pair",""), {}).get("sentiment","NEUTRAL"),
            "balance_after": self.balance,
        })
        emoji = "✅" if pnl >= 0 else "❌"
        tg(f"{emoji} *Trade CLOSED — {name}*\n"
           f"Reason: `{reason}`\n"
           f"PnL: `{'+'if pnl>=0 else ''}{pnl:.2f}$`\n"
           f"Balance: `${self.balance:.2f}` | Win rate: `{self.win_rate:.0f}%`")

        # rank-up check
        new_rank = get_rank(self.balance)
        if new_rank["name"] != self.current_rank:
            old = next((r for r in RANKS if r["name"] == self.current_rank), RANKS[0])
            nxt = get_next_rank(self.balance)
            tg(f"⬆️ *RANK UP!*\n{old['emoji']} {old['name']} → {new_rank['emoji']} *{new_rank['name']}*\n"
               f"_{new_rank['unlock']}_\nNext: {nxt['emoji']} {nxt['name']} @ `${nxt['min']:,.0f}`")
            self.current_rank = new_rank["name"]

        self.position = None
        self._save()

        if self.balance < PAPER_FLOOR:
            self._eliminate()
        if self.balance >= PAPER_TARGET:
            tg(f"🏆 *GOAL REACHED — ${PAPER_TARGET:,.0f}!*\nStarted `${PAPER_START:.0f}` → `${self.balance:.2f}`\n"
               f"Trades: `{len(self.trades)}` | Win rate: `{self.win_rate:.0f}%`\n🎉 OVERLORD. Mission complete.")

    def _eliminate(self):
        losses   = [t for t in self.trades if t["pnl"] < 0]
        wins     = [t for t in self.trades if t["pnl"] > 0]
        lessons  = []
        if len(losses) > len(wins):
            lessons.append(f"Too many losses ({len(losses)}/{len(self.trades)})")
        if losses and wins:
            al = abs(sum(t["pnl"] for t in losses)/len(losses))
            aw = sum(t["pnl"] for t in wins)/len(wins)
            if al > aw * 1.5:
                lessons.append(f"Avg loss (${al:.2f}) > avg win (${aw:.2f})")
        if self.win_rate < 40 and len(self.trades) >= 5:
            lessons.append(f"Win rate only {self.win_rate:.0f}%")
        lesson_txt = "\n".join(f"• {l}" for l in lessons) if lessons else "• Not enough data"
        tg(f"💀 *BOT ELIMINATED — Restarting*\n"
           f"Balance: `${self.balance:.2f}` | Trades: `{len(self.trades)}`\n"
           f"📚 *Lessons:*\n{lesson_txt}\n🔄 Restarting at `${PAPER_START:.0f}`...")
        self.balance       = PAPER_START
        self.position      = None
        self.trades        = []
        self.peak          = PAPER_START
        self.current_rank  = RANKS[0]["name"]
        self.session_start = PAPER_START
        self._save()
        send_menu(self)

# ── Signal engine ─────────────────────────────────────────────────────────────
class SignalEngine:
    def __init__(self):
        self.above_ticks = 0
        self.below_ticks = 0

    def reset(self):
        self.above_ticks = 0
        self.below_ticks = 0

    def evaluate(self, closes, highs, lows, price, alert_buffer):
        ema = calc_ema(closes)
        rsi = calc_rsi(closes)
        price_range = max(highs) - min(lows) or 1
        tolerance   = price_range * 0.005
        levels = []
        for i in range(2, len(highs)-2):
            if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
                levels.append(highs[i])
            if lows[i]  < lows[i-1]  and lows[i]  < lows[i+1]:
                levels.append(lows[i])
        levels.sort()
        clustered = []
        for lvl in levels:
            if clustered and abs(lvl-clustered[-1]) <= tolerance:
                clustered[-1] = (clustered[-1]+lvl)/2
            else:
                clustered.append(lvl)

        nearest_r = next((l for l in clustered if l > price), None)
        nearest_s = next((l for l in reversed(clustered) if l < price), None)
        above_ema = price > ema

        if above_ema: self.above_ticks += 1; self.below_ticks = 0
        else:         self.below_ticks += 1; self.above_ticks = 0

        sig  = "HOLD"
        plan = {}
        if self.above_ticks >= CONFIRM_TICKS and rsi < 70:
            sig  = "BUY"
            plan = {"enter": price,
                    "exit":  nearest_r if nearest_r else price*1.015,
                    "stop":  nearest_s if nearest_s else price*0.985}
        elif self.below_ticks >= CONFIRM_TICKS and rsi > 30:
            sig  = "SELL"
            plan = {"enter": price,
                    "exit":  nearest_s if nearest_s else price*0.985,
                    "stop":  nearest_r if nearest_r else price*1.015}

        # news gate
        news    = news_sentiment.get(_current_coin["pair"], {})
        n_score = news.get("score", 0)
        n_sent  = news.get("sentiment", "NEUTRAL")
        if n_score >= 3:
            if sig == "BUY"  and n_sent == "BEARISH": sig = "HOLD"
            if sig == "SELL" and n_sent == "BULLISH": sig = "HOLD"

        # NASDAQ gate
        nasdaq_mood = market_mood["nasdaq"]
        if nasdaq_mood == "BEARISH" and sig == "BUY": sig = "HOLD"

        # ── Confidence score (0.0 → 1.0) ─────────────────────────────────────
        # Scored across 4 equal pillars, each worth 1 point
        ticks = self.above_ticks if sig == "BUY" else self.below_ticks
        pts   = 0.0
        # 1. RSI zone
        if 40 <= rsi <= 60:   pts += 1.0
        elif 30 < rsi < 70:   pts += 0.5
        # 2. News alignment
        if   (sig == "BUY"  and n_sent == "BULLISH") or \
             (sig == "SELL" and n_sent == "BEARISH"):  pts += 1.0
        elif n_sent == "NEUTRAL":                       pts += 0.5
        # 3. NASDAQ alignment
        if   (sig == "BUY"  and nasdaq_mood == "BULLISH") or \
             (sig == "SELL" and nasdaq_mood == "BEARISH"): pts += 1.0
        elif nasdaq_mood == "NEUTRAL":                     pts += 0.5
        # 4. Tick strength (1 point at 5+ consecutive ticks)
        pts += min(ticks / 5.0, 1.0)

        confidence = round(pts / 4.0, 2) if sig in ("BUY", "SELL") else 0.0

        return sig, plan, ema, rsi, confidence

# ── News scanner ──────────────────────────────────────────────────────────────
def _news_loop():
    global _seen_headlines
    while True:
        try:
            new_scores = {p: {"sentiment":"NEUTRAL","headline":"","score":0} for p in COIN_KEYWORDS}
            for url in NEWS_FEEDS:
                try:
                    r    = requests.get(url, timeout=8, headers={"User-Agent":"Mozilla/5.0"})
                    root = ET.fromstring(r.content[:512_000])
                    for item in root.findall(".//item")[:10]:
                        title = item.findtext("title","").strip()
                        if not title: continue
                        t     = title.lower()
                        bull  = sum(1 for w in BULLISH_WORDS if w in t)
                        bear  = sum(1 for w in BEARISH_WORDS if w in t)
                        if bull == bear: continue
                        sent  = "BULLISH" if bull > bear else "BEARISH"
                        score = abs(bull - bear)
                        for pair, kws in COIN_KEYWORDS.items():
                            if any(k in t for k in kws) and score > new_scores[pair]["score"]:
                                new_scores[pair] = {"sentiment":sent,"headline":title[:80],"score":score}
                                key = title[:60]
                                if key not in _seen_headlines:
                                    _seen_headlines.add(key)
                                    coin_name = next((c["name"] for c in SCAN_UNIVERSE if c["pair"]==pair), pair)
                                    emoji = "🟢" if sent=="BULLISH" else "🔴"
                                    tg(f"{emoji} *{sent} NEWS — {coin_name}*\n📰 {title[:120]}\n"
                                       f"Impact: {'🔥 HIGH' if score>=3 else '⚡ MEDIUM' if score==2 else '📌 LOW'}")
                except Exception:
                    pass
            for pair in COIN_KEYWORDS:
                news_sentiment[pair] = new_scores[pair]
        except Exception as e:
            print(f"[News] {e}")
        time.sleep(120)

# ── NASDAQ monitor ────────────────────────────────────────────────────────────
def _nasdaq_loop():
    while True:
        try:
            import yfinance as yf
            hist = yf.Ticker("QQQ").history(period="2d", interval="1d")
            if len(hist) >= 2:
                chg = ((hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2]) * 100
                market_mood["change_pct"] = round(chg, 2)
                market_mood["nasdaq"] = "BULLISH" if chg > 1 else "BEARISH" if chg < -1 else "NEUTRAL"
                print(f"[NASDAQ] QQQ {chg:+.2f}% → {market_mood['nasdaq']}")
        except Exception as e:
            print(f"[NASDAQ] {e}")
        time.sleep(900)

# ── Auto coin switcher ────────────────────────────────────────────────────────
def _switcher_loop(trader):
    global _current_coin
    time.sleep(20)
    while True:
        try:
            scores = rank_coins()
            switched_to = None
            with _state_lock:
                # Atomic guard + write — no gap between the position check and the coin update
                if not trader.position and scores and scores[0]["pair"] != _current_coin["pair"]:
                    new_coin = next((c for c in SCAN_UNIVERSE if c["pair"] == scores[0]["pair"]), None)
                    if new_coin:
                        _current_coin = new_coin
                        switched_to   = scores[0]
            if switched_to:
                tg(f"🔀 *Switched to {switched_to['name']}*\n"
                   f"Score: `{switched_to['score']}` | RSI: `{switched_to['rsi']}` | News: `{switched_to['news']}`\n"
                   f"_{switched_to['reason']}_")
                print(f"[Switch] → {switched_to['name']}")
        except Exception as e:
            print(f"[Switcher] {e}")
        time.sleep(1800)

# ── Telegram buttons ──────────────────────────────────────────────────────────
def send_menu(trader=None):
    with _state_lock:
        paused = _paused
    status = "🟢 LIVE" if not paused else "⏸ PAUSED"
    bal    = f"${trader.balance:,.2f}" if trader else "—"
    coin   = _current_coin["name"]
    nasdaq = market_mood["nasdaq"]
    nasdaq_icon = "🟢" if nasdaq == "BULLISH" else "🔴" if nasdaq == "BEARISH" else "⚫"
    header = (
        f"🤖 *CryptoBot* | {status}\n"
        f"💵 Balance: `{bal}` | 🎯 Goal: `${PAPER_TARGET:,.0f}`\n"
        f"📈 Trading: *{coin}* | {nasdaq_icon} NASDAQ: `{nasdaq}`"
    )
    tg_buttons(header, [
        [{"text": "💰 Balance & Stats",  "callback_data": "balance"},
         {"text": "📊 Rankings",         "callback_data": "rankings"}],
        [{"text": "📜 Trade History",    "callback_data": "history"},
         {"text": "📰 News Feed",        "callback_data": "news"}],
        [{"text": "🔄 Switch Coin",      "callback_data": "switch_menu"},
         {"text": "🏆 Rank Ladder",      "callback_data": "ranks"}],
        [{"text": "🧠 Intelligence",     "callback_data": "intelligence"}],
        [{"text": "⏸ Pause" if not paused else "▶ Resume",
          "callback_data": "pause" if not paused else "resume"}],
    ])

def _handle_callback(query, trader, engine):
    global _paused, _current_coin
    tg_answer(query["id"])
    data = query.get("data", "")

    if data == "menu":
        send_menu(trader)

    elif data == "balance":
        rank     = get_rank(trader.balance)
        next_rnk = get_next_rank(trader.balance)
        progress = min(100, max(0, (trader.balance - PAPER_START) / (PAPER_TARGET - PAPER_START) * 100))
        bar_filled = int(progress / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        losses = len(trader.trades) - trader.wins

        pos_line = "None"
        unreal   = ""
        if trader.position:
            p = trader.position
            try:
                live = get_price(_current_coin["pair"])
                upnl = trader.unrealized_pnl(live)
                upnl_str = f"({'+'if upnl>=0 else ''}{upnl:.2f}$)"
            except Exception:
                upnl_str = ""
            conf_str = f" | {int(p.get('confidence',0)*100)}% conf" if p.get('confidence') else ""
            pos_line = f"{'🟢 LONG' if p['side']=='LONG' else '🔴 SHORT'} @ `${p['entry']:.4f}` {upnl_str}{conf_str}"

        tg_buttons(
            f"{rank['emoji']} *{rank['name']}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 Balance:  `${trader.balance:,.2f}`\n"
            f"📈 Peak:     `${trader.peak:,.2f}`\n"
            f"🎯 Goal:     `${PAPER_TARGET:,.0f}`\n"
            f"⬜ Progress: `[{bar}] {progress:.1f}%`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Wins:     `{trader.wins}`  ❌ Losses: `{losses}`\n"
            f"🎯 Win Rate: `{trader.win_rate:.0f}%`  📊 Trades: `{len(trader.trades)}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 Position: {pos_line}\n"
            f"🪙 Coin:     *{_current_coin['name']}*\n"
            f"⬆️ Next Rank: {next_rnk['emoji']} {next_rnk['name']} @ `${next_rnk['min']:,.0f}`",
            [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]]
        )

    elif data == "rankings":
        scores = rank_coins()[:5]
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        lines  = ["*📊 Top 5 Coins Right Now*", "━━━━━━━━━━━━━━━━━━━━"]
        for i, s in enumerate(scores):
            news_icon = "🟢" if s["news"] == "BULLISH" else "🔴" if s["news"] == "BEARISH" else "⚫"
            lines.append(
                f"{medals[i]} *{s['name']}*\n"
                f"   Score: `{s['score']}` | RSI: `{s['rsi']}` | Vol: `{s['volatility']}%` {news_icon}\n"
                f"   _{s['reason']}_"
            )
        tg_buttons("\n".join(lines), [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])

    elif data == "history":
        if not trader.trades:
            tg_buttons("📜 *No trades yet.*", [[{"text": "🔙 Back", "callback_data": "menu"}]])
            return
        recent = trader.trades[-8:]
        lines  = [f"📜 *Last {len(recent)} Trades*", "━━━━━━━━━━━━━━━━━━━━"]
        for t in reversed(recent):
            icon  = "✅" if t["pnl"] >= 0 else "❌"
            side  = "🟢 L" if t["side"] == "LONG" else "🔴 S"
            lines.append(
                f"{icon} {side} `${t['entry']:.3f}` → `${t['exit']:.3f}`  "
                f"`{'+'if t['pnl']>=0 else ''}{t['pnl']:.2f}$`"
            )
        total_pnl = trader.total_pnl
        lines.append(f"━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"Total PnL: `{'+'if total_pnl>=0 else ''}{total_pnl:.2f}$`")
        tg_buttons("\n".join(lines), [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])

    elif data == "ranks":
        current = get_rank(trader.balance)
        lines   = ["*🏆 Rank Ladder*", "━━━━━━━━━━━━━━━━━━━━"]
        for r in RANKS:
            if r["name"] == current["name"]:
                lines.append(f"▶ {r['emoji']} *{r['name']}* ← YOU  `${r['min']:,.0f}`")
            elif trader.balance >= r["min"]:
                lines.append(f"  {r['emoji']} ~~{r['name']}~~  `${r['min']:,.0f}` ✓")
            else:
                lines.append(f"  {r['emoji']} {r['name']}  `${r['min']:,.0f}`")
        tg_buttons("\n".join(lines), [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])

    elif data == "pause":
        with _state_lock:
            _paused = True
        tg_buttons(
            "⏸ *Trading PAUSED*\nThe bot will not open new trades.",
            [[{"text": "▶ Resume Trading", "callback_data": "resume"},
              {"text": "🔙 Back",           "callback_data": "menu"}]]
        )

    elif data == "resume":
        with _state_lock:
            _paused = False
        tg_buttons(
            "▶ *Trading RESUMED*\nThe bot is back to scanning for signals.",
            [[{"text": "⏸ Pause Trading", "callback_data": "pause"},
              {"text": "🔙 Back",          "callback_data": "menu"}]]
        )

    elif data == "switch_menu":
        scores = rank_coins()[:10]
        rows   = []
        for i in range(0, len(scores), 2):
            row = [{"text": f"{s['name']} ({s['score']})", "callback_data": f"sw_{s['pair']}"} for s in scores[i:i+2]]
            rows.append(row)
        rows.append([{"text": "🔙 Back to Menu", "callback_data": "menu"}])
        tg_buttons("🔄 *Pick a coin — sorted by score:*", rows)

    elif data.startswith("sw_"):
        pair = data[3:]
        coin = next((c for c in SCAN_UNIVERSE if c["pair"] == pair), None)
        if coin:
            if trader.position:
                tg_buttons(
                    "⚠️ *Cannot switch — position is open.*\nClose the current trade first.",
                    [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]]
                )
            else:
                with _state_lock:
                    _current_coin = coin
                engine.reset()
                tg_buttons(
                    f"🔄 *Switched to {coin['name']}*\nBot will now trade this pair.",
                    [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]]
                )

    elif data == "intelligence":
        report = analyse_intelligence(trader.trades)
        tg_buttons(report, [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])

    elif data == "news":
        active = [(p, i) for p, i in news_sentiment.items() if i["sentiment"] != "NEUTRAL"]
        neutral = [(p, i) for p, i in news_sentiment.items() if i["sentiment"] == "NEUTRAL"]
        lines = ["*📰 News Sentiment*", "━━━━━━━━━━━━━━━━━━━━"]
        for pair, info in active:
            name = next((c["name"] for c in SCAN_UNIVERSE if c["pair"] == pair), pair)
            icon = "🟢" if info["sentiment"] == "BULLISH" else "🔴"
            hl   = f"\n   _{info['headline']}_" if info["headline"] else ""
            lines.append(f"{icon} *{name}*: `{info['sentiment']}`{hl}")
        if neutral:
            lines.append(f"⚫ {len(neutral)} coins neutral")
        tg_buttons("\n".join(lines), [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])

def _poll_loop(trader, engine):
    global _last_update_id
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                params={"offset":_last_update_id+1,"timeout":10,
                        "allowed_updates":["callback_query","message"]},
                timeout=15)
            for u in r.json().get("result",[]):
                _last_update_id = u["update_id"]
                if "callback_query" in u:
                    cb = u["callback_query"]
                    if str(cb.get("from", {}).get("id", "")) != TG_CHAT_ID:
                        continue
                    _handle_callback(cb, trader, engine)
                elif "message" in u:
                    if str(u["message"].get("chat", {}).get("id", "")) != TG_CHAT_ID:
                        continue
                    txt = u["message"].get("text","").strip().lower()
                    if txt in ("/start","/menu","menu"):
                        send_menu(trader)
        except Exception as e:
            print(f"[Poll] {e}")
        time.sleep(1)

# ── Main trading loop ─────────────────────────────────────────────────────────
def trading_loop(trader, engine):
    last_sig = None
    while True:
        try:
            with _state_lock:
                coin = _current_coin

            # If a position is open, always manage it using the coin it was opened on.
            # This prevents the price-mismatch glitch when the coin switcher fires mid-trade.
            if trader.position and trader.position.get("pair") != coin["pair"]:
                pos_pair = trader.position["pair"]
                pos_name = trader.position.get("name", pos_pair)
                pos_coin = next((c for c in SCAN_UNIVERSE if c["pair"] == pos_pair), None)
                if pos_coin:
                    pos_price = get_price(pos_pair)
                    trader.on_signal("HOLD", pos_price, 0, 0, pos_name, 0.0)
                time.sleep(REFRESH_SEC)
                continue

            pair = coin["pair"]
            name = coin["name"]
            buf  = coin["alert_buffer"]
            closes, highs, lows = get_klines(pair)
            price  = get_price(pair)
            sig, plan, ema, rsi, confidence = engine.evaluate(closes, highs, lows, price, buf)

            now = datetime.now().strftime("%H:%M:%S")
            print(f"[{now}] {name} ${price:.4f} EMA:{ema:.2f} RSI:{rsi} → {sig} conf:{confidence:.0%}")

            if sig != last_sig and sig in ("BUY","SELL"):
                stop   = plan.get("stop",  price*0.985 if sig=="BUY" else price*1.015)
                target = plan.get("exit",  price*1.015 if sig=="BUY" else price*0.985)
                emoji  = "🟢" if sig=="BUY" else "🔴"
                risk   = RISK_MIN + (RISK_MAX - RISK_MIN) * confidence
                tg(f"{emoji} *{sig} Signal — {name}*\n"
                   f"Enter: `${plan['enter']:.4f}`\nExit: `${target:.4f}`\nStop: `${stop:.4f}`\n"
                   f"EMA: `{ema:.2f}` | RSI: `{rsi}` | Confidence: `{int(confidence*100)}%` → Size: `{risk*100:.1f}%`")
                trader.on_signal(sig, price, stop, target, name, confidence)

            last_sig = sig

        except Exception as e:
            print(f"[Trade] {e}")
        time.sleep(REFRESH_SEC)

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    print("=" * 40)
    print("  CRYPTOBOT SERVER STARTING")
    print("=" * 40)

    trader = PaperTrader()
    engine = SignalEngine()

    # Start background threads
    threading.Thread(target=_news_loop,                         daemon=True).start()
    threading.Thread(target=_nasdaq_loop,                       daemon=True).start()
    threading.Thread(target=_switcher_loop, args=(trader,),     daemon=True).start()
    threading.Thread(target=_poll_loop, args=(trader, engine),  daemon=True).start()

    print("[Bot] All systems online.")

    # Send menu immediately so Telegram responds without waiting for the coin scan
    rank     = get_rank(trader.balance)
    next_rnk = get_next_rank(trader.balance)
    progress = max(0,(trader.balance-PAPER_START)/(PAPER_TARGET-PAPER_START)*100)
    print(f"[Bot] Sending startup menu to chat_id={TG_CHAT_ID}...")
    send_menu(trader)
    tg(f"{rank['emoji']} *CryptoBot Online*\n"
       f"─────────────────────\n"
       f"Rank: *{rank['name']}* | Balance: `${trader.balance:.2f}`\n"
       f"Progress: `{progress:.1f}%` → Goal: `${PAPER_TARGET:,.0f}`\n"
       f"Trading: *{_current_coin['name']}*\n"
       f"Next rank: {next_rnk['name']} @ `${next_rnk['min']:,.0f}`\n"
       f"─────────────────────\n"
       f"_{rank['unlock']}_")
    print("[Bot] Startup messages sent.")

    # Find best coin on startup (runs after menu is already shown)
    try:
        print("[Bot] Scanning for best coin...")
        scores = rank_coins()
        if scores:
            global _current_coin
            _current_coin = next((c for c in SCAN_UNIVERSE if c["pair"]==scores[0]["pair"]), SCAN_UNIVERSE[0])
            print(f"[Bot] Best coin: {_current_coin['name']}")
    except Exception as e:
        print(f"[Bot] Scan error: {e}")

    # Main trading loop (runs forever)
    trading_loop(trader, engine)

if __name__ == "__main__":
    main()
