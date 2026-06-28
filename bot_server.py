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
try:
    import psycopg2
    import psycopg2.extras
    _PG_URL = os.environ.get("DATABASE_URL")
    _db_available = bool(_PG_URL)
except ImportError:
    _db_available = False

def _db_conn():
    return psycopg2.connect(_PG_URL, sslmode="require")

def _db_init():
    if not _db_available: return
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS state (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    );
                    CREATE TABLE IF NOT EXISTS trades (
                        id SERIAL PRIMARY KEY,
                        ts TIMESTAMP DEFAULT NOW(),
                        coin TEXT,
                        side TEXT,
                        entry REAL,
                        exit_price REAL,
                        pnl REAL,
                        reason TEXT,
                        balance_after REAL
                    );
                    CREATE TABLE IF NOT EXISTS lessons (
                        id SERIAL PRIMARY KEY,
                        ts TIMESTAMP DEFAULT NOW(),
                        lesson TEXT,
                        win_rate REAL,
                        total_trades INT
                    );
                """)
        print("[DB] PostgreSQL connected and tables ready")
    except Exception as e:
        print(f"[DB] Init error: {e}")

# ── Config ────────────────────────────────────────────────────────────────────
TG_TOKEN   = "8382017140:AAH-Zs0dBTAk47vwnvnKOuXlo6ee8693wfs"
TG_CHAT_ID = "6689707091"
TG_URL     = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
BASE_URL   = "https://api.kraken.com/0/public"

EMA_PERIOD    = 14
RSI_PERIOD    = 14
CANDLE_LIMIT  = 60
INTERVAL      = 5
REFRESH_SEC   = 30
CONFIRM_TICKS = 1

PAPER_START    = 100.0
PAPER_TARGET   = 50000.0
PAPER_FLOOR    = 50.0
LEVERAGE       = 3
RISK_PER_TRADE = 0.10
MAX_TRADE_GAIN = 0.06   # close when up 6%
MAX_TRADE_LOSS = 0.03   # close when down 3%
MAX_TRADE_MINS = 30     # force close after 30 min

SAVE_FILE = "paper_state.json"

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

# ── Shared state ──────────────────────────────────────────────────────────────
news_sentiment  = {p: {"sentiment": "NEUTRAL", "headline": "", "score": 0} for p in COIN_KEYWORDS}
market_mood     = {"nasdaq": "NEUTRAL", "change_pct": 0.0}
_paused         = False
_current_coin   = SCAN_UNIVERSE[0]
_last_update_id = 0
_seen_headlines = set()

# ── Telegram helpers ──────────────────────────────────────────────────────────
def tg(msg):
    try:
        requests.post(TG_URL, data={"chat_id": TG_CHAT_ID, "text": msg,
                                    "parse_mode": "Markdown"}, timeout=5)
    except Exception:
        pass

def tg_buttons(msg, buttons):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": msg,
                            "parse_mode": "Markdown",
                            "reply_markup": {"inline_keyboard": buttons}}, timeout=5)
    except Exception:
        pass

def tg_answer(cb_id, text=""):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id": cb_id, "text": text}, timeout=5)
    except Exception:
        pass

# ── Data ──────────────────────────────────────────────────────────────────────
def get_klines(pair):
    r = requests.get(f"{BASE_URL}/OHLC", params={"pair": pair, "interval": INTERVAL}, timeout=10)
    result = r.json()
    rkey = list(result["result"].keys())[0]
    data = result["result"][rkey][-CANDLE_LIMIT:]
    closes  = [float(c[4]) for c in data]
    highs   = [float(c[2]) for c in data]
    lows    = [float(c[3]) for c in data]
    return closes, highs, lows

def get_price(pair):
    r = requests.get(f"{BASE_URL}/Ticker", params={"pair": pair}, timeout=10)
    key = list(r.json()["result"].keys())[0]
    return float(r.json()["result"][key]["c"][0])

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
    return round(100 - 100/(1 + ag/al), 1) if al > 0 else 100.0

# ── Rank coins ────────────────────────────────────────────────────────────────
def rank_coins():
    scores = []
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
            reason = []
            if volatility > 2:  reason.append(f"Volatility {volatility:.1f}%")
            if above_ema:       reason.append("Above EMA")
            if 40 <= rsi <= 60: reason.append(f"RSI {rsi} ideal")
            if news.get("sentiment") == "BULLISH": reason.append("Bullish news")
            scores.append({"name": coin["name"], "pair": pair,
                           "score": round(score,1), "rsi": rsi,
                           "volatility": round(volatility,2),
                           "news": news.get("sentiment","NEUTRAL"),
                           "reason": ", ".join(reason) or "No signal",
                           "alert_buffer": coin["alert_buffer"]})
        except Exception:
            pass
    return sorted(scores, key=lambda x: x["score"], reverse=True)

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
        if _db_available:
            try:
                with _db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT key, value FROM state WHERE key IN ('balance','peak','position','current_rank','trades')")
                        rows = {r[0]: r[1] for r in cur.fetchall()}
                if rows:
                    self.balance      = float(rows.get("balance", PAPER_START))
                    self.peak         = float(rows.get("peak", self.balance))
                    self.position     = json.loads(rows["position"]) if rows.get("position") and rows["position"] != "null" else None
                    self.current_rank = rows.get("current_rank", RANKS[0]["name"])
                    self.trades       = json.loads(rows.get("trades", "[]"))
                    print(f"[DB] Loaded — Balance: ${self.balance:.2f} | Trades: {len(self.trades)}")
                    return
            except Exception as e:
                print(f"[DB] Load error: {e}")
        # fallback to file
        if os.path.exists(SAVE_FILE):
            try:
                with open(SAVE_FILE) as f:
                    d = json.load(f)
                self.balance      = d.get("balance", PAPER_START)
                self.peak         = d.get("peak", self.balance)
                self.trades       = d.get("trades", [])
                self.position     = d.get("position", None)
                self.current_rank = d.get("current_rank", RANKS[0]["name"])
                print(f"[Paper] Loaded from file — Balance: ${self.balance:.2f}")
            except Exception as e:
                print(f"[Paper] Load error: {e}")

    def _save(self):
        if _db_available:
            try:
                with _db_conn() as conn:
                    with conn.cursor() as cur:
                        for key, val in [
                            ("balance",      str(self.balance)),
                            ("peak",         str(self.peak)),
                            ("position",     json.dumps(self.position)),
                            ("current_rank", self.current_rank),
                            ("trades",       json.dumps(self.trades[-200:])),
                        ]:
                            cur.execute("INSERT INTO state(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (key, val))
                return
            except Exception as e:
                print(f"[DB] Save error: {e}")
        # fallback to file
        try:
            with open(SAVE_FILE, "w") as f:
                json.dump({"balance": self.balance, "peak": self.peak,
                           "trades": self.trades, "position": self.position,
                           "current_rank": self.current_rank}, f, indent=2)
        except Exception as e:
            print(f"[Paper] Save error: {e}")

    def _save_trade_db(self, coin, side, entry, exit_price, pnl, reason):
        if not _db_available: return
        try:
            with _db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO trades(coin,side,entry,exit_price,pnl,reason,balance_after) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                                (coin, side, entry, exit_price, pnl, reason, self.balance))
        except Exception as e:
            print(f"[DB] Trade save error: {e}")

    def _save_lesson_db(self, lesson, win_rate, total_trades):
        if not _db_available: return
        try:
            with _db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO lessons(lesson,win_rate,total_trades) VALUES(%s,%s,%s)",
                                (lesson, win_rate, total_trades))
        except Exception as e:
            print(f"[DB] Lesson save error: {e}")

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

    def on_signal(self, sig, price, stop, target, name):
        global _paused
        if _paused or self.balance < PAPER_FLOOR: return
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
                self._open("LONG",  price, name, target)
            elif sig == "SELL":
                self._open("SHORT", price, name, target)

    def _open(self, side, price, name, target):
        margin    = round(self.balance * RISK_PER_TRADE, 4)
        contracts = round((margin * LEVERAGE) / price, 6)
        self.position = {"side": side, "entry": price,
                         "contracts": contracts, "margin": margin,
                         "target": target, "opened_at": time.time()}
        self._save()
        tg(f"📂 *Trade OPENED — {name}*\n"
           f"{'🟢 LONG' if side=='LONG' else '🔴 SHORT'} @ `${price:.4f}`\n"
           f"Margin: `${margin:.2f}` | {LEVERAGE}x leverage\n"
           f"Balance: `${self.balance:.2f}`")

    def _close(self, price, name, reason):
        if not self.position: return
        pnl          = self.unrealized_pnl(price)
        self.balance = round(self.balance + pnl, 4)
        self.peak    = max(self.peak, self.balance)
        trade = {"side": self.position["side"], "entry": self.position["entry"],
                 "exit": price, "pnl": pnl}
        self.trades.append(trade)
        self._save_trade_db(name, trade["side"], trade["entry"], price, pnl, reason)
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
        for l in lessons:
            self._save_lesson_db(l, self.win_rate, len(self.trades))
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

# ── Signal engine ─────────────────────────────────────────────────────────────
class SignalEngine:
    def __init__(self):
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
        news = news_sentiment.get(_current_coin["pair"], {})
        n_score = news.get("score", 0)
        if n_score >= 3:
            if sig == "BUY"  and news.get("sentiment") == "BEARISH": sig = "HOLD"
            if sig == "SELL" and news.get("sentiment") == "BULLISH": sig = "HOLD"

        # NASDAQ gate
        if market_mood["nasdaq"] == "BEARISH" and sig == "BUY": sig = "HOLD"

        return sig, plan, ema, rsi

# ── News scanner ──────────────────────────────────────────────────────────────
def _news_loop():
    global _seen_headlines
    while True:
        try:
            new_scores = {p: {"sentiment":"NEUTRAL","headline":"","score":0} for p in COIN_KEYWORDS}
            for url in NEWS_FEEDS:
                try:
                    r    = requests.get(url, timeout=8, headers={"User-Agent":"Mozilla/5.0"})
                    root = ET.fromstring(r.content)
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
                market_mood["nasdaq"] = "BULLISH" if chg > 1 else "BEARISH" if chg < -2 else "NEUTRAL"
                print(f"[NASDAQ] QQQ {chg:+.2f}% → {market_mood['nasdaq']}")
        except Exception as e:
            print(f"[NASDAQ] {e}")
        time.sleep(900)

# ── Auto coin switcher ────────────────────────────────────────────────────────
_hold_since = {}  # pair -> timestamp when HOLD started

def _switcher_loop():
    global _current_coin
    time.sleep(20)
    while True:
        try:
            scores = rank_coins()
            if not scores:
                time.sleep(600)
                continue

            pair = _current_coin["pair"]
            # track how long current coin has been HOLDing
            try:
                closes, highs, lows = get_klines(pair)
                price = get_price(pair)
                engine_tmp = SignalEngine()
                sig, _, _, _ = engine_tmp.evaluate(closes, highs, lows, price, _current_coin["alert_buffer"])
                if sig == "HOLD":
                    if pair not in _hold_since:
                        _hold_since[pair] = time.time()
                else:
                    _hold_since.pop(pair, None)
            except Exception:
                sig = "HOLD"

            hold_mins = (time.time() - _hold_since.get(pair, time.time())) / 60
            force_switch = hold_mins >= 20  # switch if HOLD for 20+ minutes

            # If stuck on HOLD, pick best coin that isn't the current one
            if force_switch:
                best = next((s for s in scores if s["pair"] != pair), scores[0])
            else:
                best = scores[0]

            if best["pair"] != pair:
                new_coin = next((c for c in SCAN_UNIVERSE if c["pair"] == best["pair"]), _current_coin)
                _current_coin = new_coin
                _hold_since.pop(pair, None)
                reason = f"HOLD {hold_mins:.0f}min — forced switch" if force_switch else "better score"
                tg(f"🔀 *Switched to {best['name']}* ({reason})\n"
                   f"Score: `{best['score']}` | RSI: `{best['rsi']}` | News: `{best['news']}`\n"
                   f"_{best['reason']}_")
                print(f"[Switch] → {best['name']} ({reason})")
        except Exception as e:
            print(f"[Switcher] {e}")
        time.sleep(600)  # check every 10 min instead of 30

# ── Telegram buttons ──────────────────────────────────────────────────────────
def send_menu(trader=None):
    status = "🟢 LIVE" if not _paused else "⏸ PAUSED"
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
        [{"text": "⏸ Pause" if not _paused else "▶ Resume",
          "callback_data": "pause" if not _paused else "resume"},
         {"text": "❌ Close Trade", "callback_data": "close_trade"}],
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
            pos_line = f"{'🟢 LONG' if p['side']=='LONG' else '🔴 SHORT'} @ `${p['entry']:.4f}` {upnl_str}"

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

    elif data == "close_trade":
        if trader.position:
            try:
                price = get_price(_current_coin["pair"])
                trader._close(price, _current_coin["name"], "manual close")
                tg_buttons(
                    f"❌ *Trade Closed Manually*\nBalance: `${trader.balance:,.2f}`",
                    [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]]
                )
            except Exception as e:
                tg(f"Error closing trade: {e}")
        else:
            tg_buttons(
                "No open trade to close.",
                [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]]
            )

    elif data == "pause":
        _paused = True
        tg_buttons(
            "⏸ *Trading PAUSED*\nThe bot will not open new trades.",
            [[{"text": "▶ Resume Trading", "callback_data": "resume"},
              {"text": "🔙 Back",           "callback_data": "menu"}]]
        )

    elif data == "resume":
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
            _current_coin = coin
            engine.__init__()
            tg_buttons(
                f"🔄 *Switched to {coin['name']}*\nBot will now trade this pair.",
                [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]]
            )

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
                    _handle_callback(u["callback_query"], trader, engine)
                elif "message" in u:
                    txt = u["message"].get("text","").strip().lower()
                    if txt in ("/start","/menu","menu"):
                        send_menu(trader)
        except Exception as e:
            print(f"[Poll] {e}")
        time.sleep(1)

# ── Main trading loop ─────────────────────────────────────────────────────────
def trading_loop(trader, engine):
    last_sig       = None
    last_update_tg = 0

    while True:
        try:
            pair   = _current_coin["pair"]
            name   = _current_coin["name"]
            buf    = _current_coin["alert_buffer"]
            closes, highs, lows = get_klines(pair)
            price  = get_price(pair)
            sig, plan, ema, rsi = engine.evaluate(closes, highs, lows, price, buf)

            now = datetime.now().strftime("%H:%M:%S")
            print(f"[{now}] {name} ${price:.4f} EMA:{ema:.2f} RSI:{rsi} → {sig}")

            had_position = bool(trader.position)
            if sig in ("BUY","SELL"):
                stop   = plan.get("stop",  price*0.985 if sig=="BUY" else price*1.015)
                target = plan.get("exit",  price*1.015 if sig=="BUY" else price*0.985)
                trader.on_signal(sig, price, stop, target, name)
                # alert on signal change OR when we just re-entered after a close
                if sig != last_sig or (not had_position and trader.position):
                    emoji = "🟢" if sig=="BUY" else "🔴"
                    tg(f"{emoji} *{sig} Signal — {name}*\n"
                       f"Enter: `${plan['enter']:.4f}`\nExit: `${target:.4f}`\nStop: `${stop:.4f}`\n"
                       f"EMA: `{ema:.2f}` | RSI: `{rsi}`")

            last_sig = sig

            # Send status update every 30 minutes
            if time.time() - last_update_tg >= 1800:
                rank   = get_rank(trader.balance)
                upnl   = trader.unrealized_pnl(price)
                pos    = trader.position
                if pos:
                    pos_txt = f"{'🟢 LONG' if pos['side']=='LONG' else '🔴 SHORT'} @ `${pos['entry']:.4f}` | uPnL: `{'+'if upnl>=0 else ''}{upnl:.2f}$`"
                else:
                    pos_txt = "None"
                tg(f"📊 *30-Min Update*\n"
                   f"{rank['emoji']} `${trader.balance:,.2f}` | Coin: *{name}*\n"
                   f"Price: `${price:.4f}` | EMA: `{ema:.2f}` | RSI: `{rsi}` | Signal: `{sig}`\n"
                   f"Position: {pos_txt}\n"
                   f"Trades: `{len(trader.trades)}` | Win Rate: `{trader.win_rate:.0f}%`")
                last_update_tg = time.time()

        except Exception as e:
            print(f"[Trade] {e}")
        time.sleep(REFRESH_SEC)

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    print("=" * 40)
    print("  CRYPTOBOT SERVER STARTING")
    print("=" * 40)
    _db_init()

    trader = PaperTrader()
    engine = SignalEngine()

    # Start background threads
    threading.Thread(target=_news_loop,                         daemon=True).start()
    threading.Thread(target=_nasdaq_loop,                       daemon=True).start()
    threading.Thread(target=_switcher_loop,                     daemon=True).start()
    threading.Thread(target=_poll_loop, args=(trader, engine),  daemon=True).start()

    print("[Bot] All systems online.")

    # Find best coin on startup
    try:
        print("[Bot] Scanning for best coin...")
        scores = rank_coins()
        if scores:
            global _current_coin
            _current_coin = next((c for c in SCAN_UNIVERSE if c["pair"]==scores[0]["pair"]), SCAN_UNIVERSE[0])
            print(f"[Bot] Best coin: {_current_coin['name']}")
    except Exception as e:
        print(f"[Bot] Scan error: {e}")

    rank     = get_rank(trader.balance)
    next_rnk = get_next_rank(trader.balance)
    progress = max(0,(trader.balance-PAPER_START)/(PAPER_TARGET-PAPER_START)*100)

    send_menu(trader)
    tg(f"{rank['emoji']} *CryptoBot Online*\n"
       f"─────────────────────\n"
       f"Rank: *{rank['name']}* | Balance: `${trader.balance:.2f}`\n"
       f"Progress: `{progress:.1f}%` → Goal: `${PAPER_TARGET:,.0f}`\n"
       f"Trading: *{_current_coin['name']}*\n"
       f"Next rank: {next_rnk['name']} @ `${next_rnk['min']:,.0f}`\n"
       f"─────────────────────\n"
       f"_{rank['unlock']}_")

    # Main trading loop (runs forever)
    trading_loop(trader, engine)

if __name__ == "__main__":
    main()
