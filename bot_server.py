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
import sys
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# ── Terminal colours ──────────────────────────────────────────────────────────
_USE_COLOUR = sys.stdout.isatty() or os.environ.get("FORCE_COLOR", "0") == "1"

class _C:  # ANSI codes
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    # foreground
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    MAGENTA= "\033[95m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"
    GREY   = "\033[90m"

def _c(code, text):
    return f"{code}{text}{_C.RESET}" if _USE_COLOUR else text

# Tag colours per module
_TAG_COLOUR = {
    "BOOT":    _C.CYAN  + _C.BOLD,
    "BOT":     _C.CYAN  + _C.BOLD,
    "TRADE":   _C.MAGENTA,
    "SIGNAL":  _C.MAGENTA + _C.BOLD,
    "OPEN":    _C.GREEN + _C.BOLD,
    "CLOSE":   _C.YELLOW + _C.BOLD,
    "PnL":     _C.YELLOW,
    "NEWS":    _C.BLUE,
    "NASDAQ":  _C.BLUE,
    "F&G":     _C.BLUE,
    "BTC DOM": _C.BLUE,
    "FUNDING": _C.BLUE,
    "TRENDING":_C.BLUE,
    "SWITCH":  _C.CYAN,
    "POLL":    _C.GREY,
    "DB":      _C.GREY,
    "TG":      _C.GREY,
    "PAPER":   _C.GREY,
    "CALLBACK":_C.GREY,
    "ERROR":   _C.RED + _C.BOLD,
    "WARN":    _C.YELLOW,
}

def log(tag: str, msg: str, level: str = "INFO"):
    ts   = datetime.now().strftime("%H:%M:%S")
    tcol = _TAG_COLOUR.get(tag, _C.WHITE)
    if level == "ERR":
        lvl_str = _c(_C.RED + _C.BOLD, "ERR")
    elif level == "WARN":
        lvl_str = _c(_C.YELLOW, "WRN")
    else:
        lvl_str = _c(_C.GREY, "INF")
    tag_str = _c(tcol, f"{tag:<8}")
    ts_str  = _c(_C.GREY, ts)
    print(f"{ts_str}  {lvl_str}  {tag_str}  {msg}", flush=True)

# ── ANSI-aware display helpers ────────────────────────────────────────────────
_ANSI_RE = re.compile(r'\033\[[0-9;]*m')

def _vlen(s: str) -> int:
    return len(_ANSI_RE.sub('', s))

def _vpad(s: str, w: int) -> str:
    return s + " " * max(0, w - _vlen(s))


def _print_rank_table(scores):
    """Formatted top-coin ranking table — printed on each rankings refresh."""
    top = scores[:8]
    if not top:
        return
    W = 63  # inner width; visual content = W-2 (1-space margin each side)

    def brow(text=""):
        return _c(_C.CYAN, "│") + " " + _vpad(text, W - 2) + " " + _c(_C.CYAN, "│")

    ts  = datetime.now().strftime("%H:%M:%S")
    hdr = (_c(_C.CYAN + _C.BOLD, "◈  MARKET SCAN") +
           "  " + _c(_C.GREY, ts + "  ·  " + str(len(top)) + " coins"))

    print(_c(_C.CYAN, "┌" + "─" * W + "┐"))
    print(brow(hdr))
    print(_c(_C.CYAN, "├" + "─" * W + "┤"))

    col_hdr = ("  " +
               _vpad(_c(_C.GREY, "COIN"),  10) + "  " +
               _vpad(_c(_C.GREY, "RSI"),    5) + "  " +
               _vpad(_c(_C.GREY, "SCORE"),  6) + "  " +
               _vpad(_c(_C.GREY, "NEWS"),   8) + "  " +
               _c(_C.GREY, "REASON"))
    print(brow(col_hdr))
    print(_c(_C.CYAN, "├" + "─" * W + "┤"))

    for i, s in enumerate(top):
        marker   = _c(_C.CYAN + _C.BOLD, "►") if i == 0 else " "
        name_col = (_C.WHITE + _C.BOLD) if i == 0 else _C.WHITE
        name_str = _c(name_col, s["name"][:10])

        rsi = s.get("rsi", 0)
        rsi_col = (_C.RED    if rsi > 70 else
                   _C.YELLOW if rsi > 60 else
                   _C.GREEN  if rsi < 40 else _C.WHITE)
        rsi_str = _c(rsi_col, str(rsi))

        sc = round(s.get("score", 0))
        sc_col = (_C.GREEN + _C.BOLD if sc >= 80 else
                  _C.GREEN           if sc >= 60 else
                  _C.YELLOW          if sc >= 40 else _C.RED)
        score_str = _c(sc_col, str(sc))

        news = s.get("news", "NEUTRAL")
        news_col = (_C.GREEN if news == "BULLISH" else
                    _C.RED   if news == "BEARISH" else _C.GREY)
        news_str = _c(news_col, news[:7])

        reason = s.get("reason", "")[:22]

        row_text = (marker + " " +
                    _vpad(name_str,  10) + "  " +
                    _vpad(rsi_str,    5) + "  " +
                    _vpad(score_str,  6) + "  " +
                    _vpad(news_str,   8) + "  " +
                    _c(_C.GREY, reason))
        print(brow(row_text))

    print(_c(_C.CYAN, "└" + "─" * W + "┘"))
    print()


def _print_trade_box(action, name, side, price, **kw):
    """Print a colored trade notification box to terminal."""
    W        = 52
    is_open  = action == "OPEN"
    is_long  = side == "LONG"
    side_col = _C.GREEN if is_long else _C.RED
    pnl      = kw.get("pnl", 0)
    if is_open:
        box_col = _C.GREEN + _C.BOLD
    elif pnl >= 0:
        box_col = _C.GREEN + _C.BOLD
    else:
        box_col = _C.RED + _C.BOLD
    arrow   = "▲" if is_open else "▼"
    act_lbl = "TRADE OPENED" if is_open else "TRADE CLOSED"

    def brow(text=""):
        return _c(box_col, "║") + " " + _vpad(text, W - 2) + " " + _c(box_col, "║")

    title = (_c(box_col, arrow + "  " + act_lbl + "  —  ") +
             _c(_C.WHITE + _C.BOLD, name))
    print(_c(box_col, "╔" + "═" * W + "╗"))
    print(brow(title))
    print(_c(box_col, "╠" + "═" * W + "╣"))

    if is_open:
        stop      = kw.get("stop", 0)
        target    = kw.get("target", 0)
        conf      = kw.get("confidence", 0)
        lev       = kw.get("leverage", 2)
        margin    = kw.get("margin", 0)
        fee       = kw.get("fee", 0)
        bal       = kw.get("balance", 0)
        stop_type = kw.get("stop_type", "ATR")
        conf_col  = (_C.GREEN if conf >= 0.6 else
                     _C.YELLOW if conf >= 0.4 else _C.RED)
        conf_pct  = str(int(conf * 100)) + "%"
        lev_str   = str(lev) + "x"
        print(brow(_c(side_col + _C.BOLD, side) +
                   _c(_C.GREY, "  @  ") + _c(_C.WHITE, f"${price:.4f}")))
        print(brow(_c(_C.GREY, "Stop    ") + _c(_C.RED, f"${stop:.4f}") +
                   _c(_C.GREY, "  ·  Target  ") + _c(_C.GREEN, f"${target:.4f}") +
                   _c(_C.GREY, "  [" + stop_type + "]")))
        print(brow(_c(_C.GREY, "Conf    ") + _c(conf_col, conf_pct) +
                   _c(_C.GREY, "  ·  Leverage  ") + _c(_C.WHITE + _C.BOLD, lev_str)))
        print(brow(_c(_C.GREY, "Margin  ") + _c(_C.WHITE, f"${margin:.2f}") +
                   _c(_C.GREY, "  ·  Fee  ") + _c(_C.GREY, f"${fee:.3f}")))
        print(brow(_c(_C.GREY, "Balance  ") + _c(_C.WHITE + _C.BOLD, f"${bal:.2f}")))
    else:
        reason  = kw.get("reason", "")
        held    = kw.get("held_mins", 0)
        bal     = kw.get("balance", 0)
        wr      = kw.get("win_rate", 0.0)
        pnl_col = (_C.GREEN + _C.BOLD if pnl >= 0 else _C.RED + _C.BOLD)
        pnl_sgn = "+" if pnl >= 0 else ""
        icon    = "✓" if pnl >= 0 else "✗"
        pnl_str = pnl_sgn + f"{pnl:.2f}$"
        print(brow(_c(side_col + _C.BOLD, side) +
                   _c(_C.GREY, "  @  ") + _c(_C.WHITE, f"${price:.4f}") +
                   _c(_C.GREY, "  (" + reason + ")")))
        print(brow(_c(_C.GREY, "PnL     ") + _c(pnl_col, icon + "  " + pnl_str)))
        print(brow(_c(_C.GREY, "Held    ") + _c(_C.WHITE, str(round(held)) + " min") +
                   _c(_C.GREY, "  ·  Win rate  ") + _c(_C.WHITE, f"{wr:.0f}%")))
        print(brow(_c(_C.GREY, "Balance  ") + _c(_C.WHITE + _C.BOLD, f"${bal:.2f}")))

    print(_c(box_col, "╚" + "═" * W + "╝"))
    print()


# ── Config ────────────────────────────────────────────────────────────────────
TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
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
LEVERAGE_MIN   = 2      # 2x at low confidence
LEVERAGE_MAX   = 5      # 5x at high confidence
RISK_MIN       = 0.05   # 5%  margin at low confidence
RISK_MAX       = 0.20   # 20% margin at high confidence
MAX_TRADE_GAIN   = 0.08  # full-close when remaining half is up 8%
PARTIAL_TAKE_PCT = 0.05  # take 50% profit at 5% move
TRAIL_PCT        = 0.03  # fallback trailing stop (used when ATR unavailable)
ATR_PERIOD       = 14    # candles for ATR calculation
ATR_MULTIPLIER   = 1.5   # trail distance = 1.5 × ATR
MAX_TRADE_MINS   = 30    # force close after 30 min
KRAKEN_FEE       = 0.0026   # 0.26% taker fee per side (realistic Kraken fees)
SLIPPAGE         = 0.001    # 0.1% slippage on fills
MAX_TRADES_DAY   = 10       # daily trade cap (when limits enabled)
DAILY_LOSS_LIMIT = 0.10     # stop trading if down 10% today (when limits enabled)
ACTIVE_HOURS_UTC  = (7, 21)  # only trade 07:00–21:00 UTC; skip low-liquidity overnight
FUNDING_THRESHOLD = 0.0005   # 0.05% per 8h → overcrowded side, block new entries
MAX_POSITIONS     = 3        # max simultaneous open positions
MAX_TOTAL_RISK    = 0.40     # total margin across all positions ≤ 40% of balance

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
    {"name": "SHIB/USD",  "pair": "SHIBUSD",  "alert_buffer": 0.0000001},
    {"name": "WIF/USD",   "pair": "WIFUSD",   "alert_buffer": 0.01},
    {"name": "FLOKI/USD", "pair": "FLOKIUSD", "alert_buffer": 0.000001},
]

# CoinGecko coin ID → Kraken pair (used by the trending scanner)
COINGECKO_TO_KRAKEN = {
    "shiba-inu":    "SHIBUSD",
    "dogwifcoin":   "WIFUSD",
    "floki":        "FLOKIUSD",
    "pepe":         "PEPEUSD",
    "bonk":         "BONKUSD",
    "dogecoin":     "XDGUSD",
    "solana":       "SOLUSD",
    "bitcoin":      "XBTUSD",
    "ethereum":     "ETHUSD",
    "ripple":       "XRPUSD",
    "cardano":      "ADAUSD",
    "avalanche-2":  "AVAXUSD",
    "chainlink":    "LINKUSD",
    "polkadot":     "DOTUSD",
    "litecoin":     "LTCUSD",
    "cosmos":       "ATOMUSD",
    "uniswap":      "UNIUSD",
    "aave":         "AAVEUSD",
    "injective-protocol": "INJUSD",
    "sui":          "SUIUSD",
    "aptos":        "APTUSD",
    "arbitrum":     "ARBUSD",
    "near":         "NEARUSD",
    "algorand":     "ALGOUSD",
    "filecoin":     "FILUSD",
    "bitcoin-cash": "BCHUSD",
}

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
    "SHIBUSD": ["shiba","shib","$shib"],
    "WIFUSD":  ["dogwifhat","wif","$wif"],
    "FLOKIUSD":["floki","$floki"],
}

# Kraken pair → Binance USDT perpetual symbol (for funding rate fetches)
KRAKEN_TO_BINANCE = {
    "SOLUSD":   "SOLUSDT",  "XBTUSD":   "BTCUSDT",  "ETHUSD":  "ETHUSDT",
    "XRPUSD":   "XRPUSDT",  "XDGUSD":   "DOGEUSDT", "ADAUSD":  "ADAUSDT",
    "AVAXUSD":  "AVAXUSDT", "LINKUSD":  "LINKUSDT", "DOTUSD":  "DOTUSDT",
    "LTCUSD":   "LTCUSDT",  "ATOMUSD":  "ATOMUSDT", "UNIUSD":  "UNIUSDT",
    "AAVEUSD":  "AAVEUSDT", "INJUSD":   "INJUSDT",  "SUIUSD":  "SUIUSDT",
    "APTUSD":   "APTUSDT",  "ARBUSD":   "ARBUSDT",  "NEARUSD": "NEARUSDT",
    "ALGOUSD":  "ALGOUSDT", "FILUSD":   "FILUSDT",  "BCHUSD":  "BCHUSDT",
    "PEPEUSD":  "PEPEUSDT", "BONKUSD":  "BONKUSDT", "SHIBUSD": "SHIBUSDT",
    "WIFUSD":   "WIFUSDT",  "FLOKIUSD": "FLOKIUSDT",
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
            log("DB", "No DATABASE_URL — learning disabled, running on JSON only", "WARN")
            return
        try:
            try:
                import psycopg2
            except ImportError:
                log("DB", "psycopg2 not installed — learning disabled", "WARN")
                return
            self.conn = psycopg2.connect(url, connect_timeout=5)
            self.conn.autocommit = True
            self._init_schema()
            log("DB", "Connected — learning enabled")
        except Exception as e:
            log("DB", f"Connect error: {e}", "ERR")
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    id         INT PRIMARY KEY DEFAULT 1,
                    data       JSONB,
                    updated_at FLOAT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS feature_outcomes (
                    id   SERIAL PRIMARY KEY,
                    fkey TEXT,
                    pair TEXT,
                    won  BOOLEAN,
                    ts   FLOAT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS fi_fkey ON feature_outcomes(fkey)")

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
            log("DB", f"Save error: {e}", "ERR")

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
            log("DB", f"coin_win_rates error: {e}", "ERR")
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
            log("DB", f"confidence_calibration error: {e}", "ERR")
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
            log("DB", f"best_exit_reason error: {e}", "ERR")
            return []

    def log_feature(self, fkey, pair, won):
        if not self.conn or not fkey: return
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO feature_outcomes (fkey,pair,won,ts) VALUES (%s,%s,%s,%s)",
                    (fkey, pair, won, time.time()))
        except Exception as e:
            log("DB", f"log_feature error: {e}", "ERR")

    def feature_win_rates(self):
        if not self.conn: return {}
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    SELECT fkey, COUNT(*) AS n,
                           SUM(CASE WHEN won THEN 1 ELSE 0 END)::float/COUNT(*)*100 AS wr
                    FROM feature_outcomes
                    GROUP BY fkey HAVING COUNT(*) >= 5
                """)
                return {r[0]: {"n": r[1], "wr": float(r[2])} for r in cur.fetchall()}
        except Exception as e:
            log("DB", f"feature_win_rates error: {e}", "ERR")
            return {}

    def exit_pattern(self, pair):
        """Per-exit-reason stats for a pair (n, avg_pnl, early-stop count)."""
        if not self.conn: return {}
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    SELECT reason, COUNT(*) AS n, AVG(pnl) AS avg_pnl,
                           SUM(CASE WHEN held_mins < 5 THEN 1 ELSE 0 END) AS early
                    FROM trades WHERE pair=%s
                    GROUP BY reason
                """, (pair,))
                return {r[0]: {"n": r[1], "avg_pnl": float(r[2]), "early": r[3]}
                        for r in cur.fetchall()}
        except Exception as e:
            log("DB", f"exit_pattern error: {e}", "ERR")
            return {}

    def save_state(self, state: dict):
        if not self.conn: return False
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bot_state (id, data, updated_at)
                    VALUES (1, %s, %s)
                    ON CONFLICT (id) DO UPDATE
                      SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at
                """, (json.dumps(state), time.time()))
            return True
        except Exception as e:
            log("DB", f"save_state error: {e}", "ERR")
            return False

    def load_state(self):
        if not self.conn: return None
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT data FROM bot_state WHERE id = 1")
                row = cur.fetchone()
                if row and row[0]:
                    return row[0] if isinstance(row[0], dict) else json.loads(row[0])
        except Exception as e:
            log("DB", f"load_state error: {e}", "ERR")
        return None

    @property
    def connected(self):
        return self.conn is not None

db = Database()

# ── Shared state ──────────────────────────────────────────────────────────────
news_sentiment  = {p: {"sentiment": "NEUTRAL", "headline": "", "score": 0} for p in COIN_KEYWORDS}
market_mood     = {"nasdaq": "NEUTRAL", "change_pct": 0.0}
fear_greed      = {"value": 50, "label": "Neutral"}
btc_dominance   = {"pct": 50.0, "prev_pct": 50.0, "rising": False}
funding_rates   = {}   # pair → last funding rate float (from Binance futures)
trending_boost  = {}   # pair → bonus score from social/trending sources
_paused         = False
_daily_limits   = False   # off by default — enable for real-money discipline
_current_coin   = SCAN_UNIVERSE[0]
_last_update_id = 0
_seen_headlines = set()
_state_lock     = threading.Lock()   # guards _paused and _current_coin

# ── Telegram helpers ──────────────────────────────────────────────────────────
def tg(msg, plain=False):
    if not TG_TOKEN or not TG_CHAT_ID:
        return False
    try:
        payload = {"chat_id": TG_CHAT_ID, "text": msg}
        if not plain:
            payload["parse_mode"] = "Markdown"
        r = requests.post(TG_URL, json=payload, timeout=10)
        if not r.ok:
            log("TG", f"Error {r.status_code}: {r.text[:200]}", "ERR")
        return r.ok
    except Exception as e:
        log("TG", f"Send error: {e}", "ERR")
        return False

def tg_buttons(msg, buttons):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT_ID, "text": msg,
                                "parse_mode": "Markdown",
                                "reply_markup": {"inline_keyboard": buttons}}, timeout=10)
        if not r.ok:
            log("TG", f"Buttons error {r.status_code}: {r.text[:200]}", "ERR")
            if r.status_code == 400:
                r2 = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                                   json={"chat_id": TG_CHAT_ID, "text": msg,
                                         "reply_markup": {"inline_keyboard": buttons}}, timeout=10)
                if not r2.ok:
                    log("TG", f"Buttons retry error {r2.status_code}: {r2.text[:200]}", "ERR")
    except Exception as e:
        log("TG", f"Buttons send error: {e}", "ERR")

def tg_answer(cb_id, text=""):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id": cb_id, "text": text}, timeout=5)
    except Exception:
        pass

# ── Data ──────────────────────────────────────────────────────────────────────
def get_klines(pair, interval=None, limit=None):
    iv = interval or INTERVAL
    lm = limit or CANDLE_LIMIT
    r = requests.get(f"{BASE_URL}/OHLC", params={"pair": pair, "interval": iv}, timeout=10)
    payload = r.json()
    if payload.get("error"):
        raise ValueError(f"Kraken OHLC error: {payload['error']}")
    rkey = next(k for k in payload["result"] if k != "last")
    data = payload["result"][rkey][-lm:]
    closes  = [float(c[4]) for c in data]
    highs   = [float(c[2]) for c in data]
    lows    = [float(c[3]) for c in data]
    volumes = [float(c[6]) for c in data]
    return closes, highs, lows, volumes

def get_price(pair):
    r = requests.get(f"{BASE_URL}/Ticker", params={"pair": pair}, timeout=10)
    payload = r.json()
    if payload.get("error"):
        raise ValueError(f"Kraken Ticker error: {payload['error']}")
    key = list(payload["result"].keys())[0]
    return float(payload["result"][key]["c"][0])

def calc_ema(closes):
    if len(closes) < EMA_PERIOD:
        raise ValueError(f"calc_ema needs {EMA_PERIOD} candles, got {len(closes)}")
    k   = 2.0 / (EMA_PERIOD + 1)
    ema = sum(closes[:EMA_PERIOD]) / EMA_PERIOD
    for p in closes[EMA_PERIOD:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_rsi(closes):
    if len(closes) < RSI_PERIOD + 1:
        raise ValueError(f"calc_rsi needs {RSI_PERIOD+1} candles, got {len(closes)}")
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

def calc_macd(closes):
    """Returns (macd, signal, histogram). Needs 35+ candles."""
    def _ema_series(vals, period):
        k = 2.0 / (period + 1)
        e = sum(vals[:period]) / period
        out = [e]
        for v in vals[period:]:
            e = v * k + e * (1 - k)
            out.append(e)
        return out
    if len(closes) < 35:
        raise ValueError(f"calc_macd needs 35 candles, got {len(closes)}")
    fast = _ema_series(closes, 12)   # starts at candle 11
    slow = _ema_series(closes, 26)   # starts at candle 25
    macd_vals = [f - s for f, s in zip(fast[14:], slow)]  # aligned
    k9 = 2.0 / (9 + 1)
    sig = sum(macd_vals[:9]) / 9
    for v in macd_vals[9:]:
        sig = v * k9 + sig * (1 - k9)
    macd = macd_vals[-1]
    return macd, sig, macd - sig  # (macd_line, signal_line, histogram)

def calc_atr(highs, lows, closes, period=ATR_PERIOD):
    """Average True Range — measures typical candle-to-candle volatility."""
    if len(closes) < period + 1:
        raise ValueError(f"calc_atr needs {period+1} candles, got {len(closes)}")
    tr_vals = [max(highs[i] - lows[i],
                   abs(highs[i] - closes[i-1]),
                   abs(lows[i]  - closes[i-1]))
               for i in range(1, len(closes))]
    return sum(tr_vals[-period:]) / period

def _in_active_hours():
    """Return True during the 07:00–21:00 UTC trading window."""
    return ACTIVE_HOURS_UTC[0] <= datetime.utcnow().hour < ACTIVE_HOURS_UTC[1]

def detect_divergence(closes, rsi_now, lookback=15):
    """
    Compare price direction vs RSI direction over the last `lookback` candles.
    Returns 'BEARISH_DIV' (price up, RSI down) or 'BULLISH_DIV' (price down, RSI up).
    """
    if len(closes) < lookback + RSI_PERIOD + 1:
        return None
    try:
        rsi_then     = calc_rsi(closes[-(lookback + RSI_PERIOD + 1):-lookback])
        price_change = (closes[-1] - closes[-lookback]) / closes[-lookback]
        rsi_change   = rsi_now - rsi_then
        if price_change >  0.01 and rsi_change < -5: return "BEARISH_DIV"
        if price_change < -0.01 and rsi_change >  5: return "BULLISH_DIV"
    except Exception:
        pass
    return None

def detect_regime(closes, highs, lows, lookback=20):
    """Classify recent market as TRENDING / CHOPPY / NEUTRAL.
    TRENDING: strong EMA slope + directional bias.
    CHOPPY:   price oscillates without net direction (ATR-normalised range wide, no slope)."""
    if len(closes) < lookback + 2:
        return "NEUTRAL"
    try:
        ema_now  = calc_ema(closes)
        ema_prev = calc_ema(closes[:-lookback//2])
        slope    = (ema_now - ema_prev) / max(ema_prev, 1e-9)

        window   = closes[-lookback:]
        net_move = abs(window[-1] - window[0]) / max(window[0], 1e-9)
        atr_vals  = [highs[i] - lows[i] for i in range(len(highs) - lookback, len(highs))]
        avg_atr   = sum(atr_vals) / len(atr_vals) if atr_vals else 0
        atr_ratio = avg_atr / max(closes[-1], 1e-9)

        if abs(slope) > 0.005 and net_move > atr_ratio * 1.5:
            return "TRENDING"
        if net_move < atr_ratio * 0.5:
            return "CHOPPY"
    except Exception:
        pass
    return "NEUTRAL"


# ── Rank coins ────────────────────────────────────────────────────────────────
def rank_coins():
    scores    = []
    db_rates  = db.coin_win_rates()   # {} if DB not connected
    for coin in SCAN_UNIVERSE:
        pair = coin["pair"]
        try:
            closes, highs, lows, _ = get_klines(pair)
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

            # ── Social / trending boost ───────────────────────────────────
            tb = trending_boost.get(pair, 0)
            if tb > 0: score += tb

            reason = []
            if volatility > 2:  reason.append(f"Volatility {volatility:.1f}%")
            if above_ema:       reason.append("Above EMA")
            if 40 <= rsi <= 60: reason.append(f"RSI {rsi} ideal")
            if news.get("sentiment") == "BULLISH": reason.append("Bullish news")
            if learned:         reason.append(learned)
            if tb > 0:          reason.append(f"🔥 Trending +{tb}")

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
    def __init__(self, no_persist=False):
        self._no_persist   = no_persist
        self.balance       = PAPER_START
        self.positions     = {}   # pair → position dict (multi-coin)
        self.trades        = []
        self.peak          = PAPER_START
        self.coin_stats    = {}   # name → {"wins": n, "losses": n}
        self.day_start_bal = PAPER_START
        self.day_trades    = 0
        self.day_date      = datetime.utcnow().strftime("%Y-%m-%d")
        self.current_rank  = RANKS[0]["name"]
        self.session_start = PAPER_START
        self._calib        = {}
        self._calib_ts     = 0.0
        # Feature fingerprint cache
        self._feat_cache   = {}
        self._feat_ts      = 0.0
        # Per-pair ATR multiplier cache
        self._atr_cache    = {}   # pair → float
        self._atr_ts       = {}   # pair → float
        # Per-pair exit-quality cache
        self._exit_cache   = {}   # pair → dict
        self._exit_ts      = {}   # pair → float
        # Per-pair re-entry cooldown after a loss
        self._cooldown     = {}   # pair → timestamp when cooldown expires
        self._load()

    @property
    def position(self):
        """First open position — backward compat for 'if trader.position' checks."""
        return next(iter(self.positions.values()), None)

    def can_open_new(self):
        if len(self.positions) >= MAX_POSITIONS: return False
        used = sum(p["margin"] for p in self.positions.values())
        return (used / max(self.balance, 0.01)) < MAX_TOTAL_RISK

    def _apply_state(self, d):
        self.balance      = d.get("balance", PAPER_START)
        self.peak         = d.get("peak", self.balance)
        self.trades       = d.get("trades", [])
        self.current_rank = d.get("current_rank", RANKS[0]["name"])
        pos_data = d.get("positions")
        if pos_data is None:
            old = d.get("position")
            if old and isinstance(old, dict) and "side" in old:
                self.positions = {old.get("pair", "XBTUSD"): old}
            else:
                self.positions = {}
        else:
            self.positions = pos_data

    def _load(self):
        if self._no_persist:
            return
        if db.connected:
            state = db.load_state()
            if state:
                try:
                    self._apply_state(state)
                    log("PAPER", f"Loaded from DB  balance=${self.balance:.2f}  "
                                 f"positions={len(self.positions)}  trades={len(self.trades)}")
                    self._save_file()
                    return
                except Exception as e:
                    log("PAPER", f"DB load error: {e}", "ERR")
        if os.path.exists(SAVE_FILE):
            try:
                with open(SAVE_FILE) as f:
                    d = json.load(f)
                self._apply_state(d)
                log("PAPER", f"Loaded from file  balance=${self.balance:.2f}  "
                             f"positions={len(self.positions)}")
                if db.connected:
                    self._save_db()
                    log("PAPER", "Migrated file state → Postgres")
            except Exception as e:
                log("PAPER", f"Load error: {e}", "ERR")

    def _state_dict(self):
        return {"balance": self.balance, "peak": self.peak,
                "trades": self.trades[-1000:], "positions": self.positions,
                "current_rank": self.current_rank}

    def _save_file(self):
        try:
            with open(SAVE_FILE, "w") as f:
                json.dump(self._state_dict(), f, indent=2)
        except Exception as e:
            log("PAPER", f"File save error: {e}", "ERR")

    def _save_db(self):
        if db.connected:
            db.save_state(self._state_dict())

    def _save(self):
        if self._no_persist:
            return
        self._save_db()
        self._save_file()

    def _calibration_multiplier(self, confidence):
        if self._no_persist or not db.connected:
            return 1.0
        now = time.time()
        if now - self._calib_ts > 300:
            try:
                self._calib = db.confidence_calibration()
            except Exception as e:
                log("PAPER", f"calibration fetch error: {e}", "ERR")
            self._calib_ts = now
        tier  = "high" if confidence >= 0.6 else "low"
        stats = self._calib.get(tier)
        if not stats or stats.get("n", 0) < 15:
            return 1.0
        wr = stats.get("wr", 50.0)
        if wr >= 55: return 1.0
        if wr >= 50: return 0.85
        if wr >= 45: return 0.65
        if wr >= 40: return 0.50
        return 0.35

    @property
    def consecutive_losses(self):
        count = 0
        for t in reversed(self.trades):
            if t["pnl"] < 0: count += 1
            else: break
        return count

    def _reset_day_if_needed(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if today != self.day_date:
            self.day_date      = today
            self.day_start_bal = self.balance
            self.day_trades    = 0

    def _risk_multiplier(self):
        """Shrink position size after losing streaks to survive choppy markets."""
        losses = self.consecutive_losses
        if losses >= 5: return 0.25
        if losses >= 3: return 0.50
        return 1.0

    # ── Learning method 1: signal-condition fingerprint ──────────────────────
    def _feature_multiplier(self, fkey):
        """Scale stake up/down based on realized win rate of this signal fingerprint.
        Needs ≥5 samples; safe to boost proven patterns up to 1.15×."""
        if self._no_persist or not db.connected or not fkey:
            return 1.0
        now = time.time()
        if now - self._feat_ts > 600:
            try:
                self._feat_cache = db.feature_win_rates()
            except Exception as e:
                log("PAPER", f"feature cache: {e}", "ERR")
            self._feat_ts = now
        stats = self._feat_cache.get(fkey)
        if not stats or stats["n"] < 5:
            return 1.0
        wr = stats["wr"]
        if wr >= 65: return 1.15
        if wr >= 55: return 1.0
        if wr >= 45: return 0.70
        if wr >= 35: return 0.45
        return 0.25

    # ── Learning method 2: per-pair ATR multiplier self-tuning ───────────────
    def _refresh_exit_cache(self, pair):
        """Shared 30-min cache refresh for exit-pattern data."""
        now = time.time()
        if now - self._exit_ts.get(pair, 0) > 1800:
            self._exit_ts[pair] = now
            try:
                self._exit_cache[pair] = db.exit_pattern(pair)
            except Exception as e:
                log("PAPER", f"exit cache {pair}: {e}", "ERR")

    def _atr_mult(self, pair):
        """Per-pair ATR trailing-stop multiplier. Starts at ATR_MULTIPLIER
        and self-tunes every 30 min using the exit-reason breakdown."""
        if self._no_persist or not db.connected:
            return ATR_MULTIPLIER
        self._refresh_exit_cache(pair)
        exits = self._exit_cache.get(pair, {})
        if not exits:
            return self._atr_cache.get(pair, ATR_MULTIPLIER)
        try:
            trail = exits.get("trailing stop", {})
            tp    = exits.get("take profit",   {})
            if trail.get("n", 0) >= 10:
                current = self._atr_cache.get(pair, ATR_MULTIPLIER)
                if trail.get("avg_pnl", 0) < 0:
                    current = min(current + 0.2, 3.0)  # widen — stops firing at loss
                elif tp.get("n", 0) >= 10 and tp.get("avg_pnl", 0) > trail.get("avg_pnl", 0) * 1.5:
                    current = max(current - 0.1, 0.8)  # tighten — take-profit beats trail
                self._atr_cache[pair] = current
        except Exception as e:
            log("PAPER", f"_atr_mult {pair}: {e}", "ERR")
        return self._atr_cache.get(pair, ATR_MULTIPLIER)

    # ── Learning method 3: exit quality → skip fixed target when trail wins ──
    def _trail_only(self, pair):
        """True when trailing stop consistently earns more per trade than
        the fixed target. Lets winning trades run further."""
        if self._no_persist or not db.connected:
            return False
        self._refresh_exit_cache(pair)
        exits = self._exit_cache.get(pair, {})
        trail = exits.get("trailing stop", {})
        tp    = exits.get("take profit",   {})
        if trail.get("n", 0) >= 5 and tp.get("n", 0) >= 5:
            return trail.get("avg_pnl", 0) > tp.get("avg_pnl", 0) * 1.2
        return False

    # ── Learning method 4: require higher confidence on bad-entry pairs ───────
    def _min_conf_threshold(self, pair):
        """Returns 0.60 confidence floor when early-stop rate ≥40% of total exits."""
        if self._no_persist or not db.connected:
            return 0.0
        self._refresh_exit_cache(pair)
        exits = self._exit_cache.get(pair, {})
        total = sum(v.get("n", 0) for v in exits.values())
        if total < 8:
            return 0.0
        trail = exits.get("trailing stop", {})
        early = trail.get("early", 0)
        if total > 0 and early / total >= 0.40:
            log("PAPER", f"{pair} early-stop rate {early/total:.0%} → raising confidence floor")
            return 0.60
        return 0.0

    def _partial_close(self, price, name, pair):
        """Close 50% of position at current price and let the rest ride."""
        p = self.positions.get(pair)
        if not p or p.get("partial_taken"): return
        move = (price - p["entry"]) / p["entry"]
        if p["side"] == "SHORT": move = -move
        pnl = round(move * (p["margin"] / 2) * p.get("leverage", LEVERAGE_MIN), 4)
        self.balance = round(self.balance + pnl, 4)
        p["contracts"]     = round(p["contracts"] / 2, 6)
        p["margin"]        = round(p["margin"] / 2, 4)
        p["partial_taken"] = True
        self.peak = max(self.peak, self.balance)
        self._save()
        tg(f"🎯 *Partial Take-Profit — {name}*\n"
           f"Closed 50% @ `${price:.4f}` | PnL: `+{pnl:.2f}$`\n"
           f"Remaining half still running | Balance: `${self.balance:.2f}`")

    @property
    def wins(self):
        return sum(1 for t in self.trades if t["pnl"] > 0)

    @property
    def win_rate(self):
        return (self.wins / len(self.trades) * 100) if self.trades else 0.0

    @property
    def total_pnl(self):
        return round(sum(t["pnl"] for t in self.trades), 2)

    def unrealized_pnl(self, price, pair=None):
        p = self.positions.get(pair) if pair else self.position
        if not p: return 0.0
        move = (price - p["entry"]) / p["entry"]
        if p["side"] == "SHORT": move = -move
        return round(move * p["margin"] * p.get("leverage", LEVERAGE_MIN), 4)

    def on_signal(self, sig, price, stop, target, name, confidence, pair, atr=None, fkey=""):
        with _state_lock:
            paused = _paused
        if paused or self.balance < PAPER_FLOOR: return
        if self.balance >= PAPER_TARGET: return
        self._reset_day_if_needed()
        if _daily_limits:
            if self.day_trades >= MAX_TRADES_DAY: return
            if (self.balance - self.day_start_bal) / max(self.day_start_bal, 0.01) <= -DAILY_LOSS_LIMIT: return

        closed_this_tick = False
        p = self.positions.get(pair)
        if p:
            side = p["side"]
            move = (price - p["entry"]) / p["entry"]
            if side == "SHORT": move = -move
            mins_open = (time.time() - p.get("opened_at", time.time())) / 60

            # Partial take-profit: book 50% at PARTIAL_TAKE_PCT, let rest ride
            if not p.get("partial_taken") and move >= PARTIAL_TAKE_PCT:
                self._partial_close(price, name, pair)

            # Update trailing stop using stored ATR distance (or fixed % fallback)
            atr_dist = p.get("atr_dist", price * TRAIL_PCT)
            if side == "LONG":
                if price > p.get("trail_peak", p["entry"]):
                    p["trail_peak"] = price
                    p["trail_stop"] = round(price - atr_dist, 6)
            else:
                if price < p.get("trail_peak", p["entry"]):
                    p["trail_peak"] = price
                    p["trail_stop"] = round(price + atr_dist, 6)

            if side == "LONG":
                trail_stop = p.get("trail_stop", 0)
                hit_trail  = price <= trail_stop
            else:
                trail_stop = p.get("trail_stop", float("inf"))
                hit_trail  = price >= trail_stop

            if move >= MAX_TRADE_GAIN:
                self._close(price, name, "profit cap",   pair); closed_this_tick = True
            elif hit_trail:
                self._close(price, name, "trailing stop", pair); closed_this_tick = True
            elif mins_open >= MAX_TRADE_MINS:
                self._close(price, name, "time limit",   pair); closed_this_tick = True
            elif side == "LONG"  and price >= p.get("target", float("inf")):
                self._close(price, name, "take profit",  pair); closed_this_tick = True
            elif side == "SHORT" and price <= p.get("target", 0):
                self._close(price, name, "take profit",  pair); closed_this_tick = True
            elif (side == "LONG" and sig == "SELL") or (side == "SHORT" and sig == "BUY"):
                self._close(price, name, "signal flip",  pair); closed_this_tick = True

        if pair not in self.positions and not closed_this_tick:
            if time.time() < self._cooldown.get(pair, 0):
                return
            min_conf = self._min_conf_threshold(pair)
            if sig == "BUY"  and self.can_open_new() and confidence >= min_conf:
                self._open("LONG",  price, name, target, confidence, pair, atr, fkey=fkey, stop=stop)
            elif sig == "SELL" and self.can_open_new() and confidence >= min_conf:
                self._open("SHORT", price, name, target, confidence, pair, atr, fkey=fkey, stop=stop)

    def _open(self, side, price, name, target, confidence, pair, atr=None, fkey="", stop=None):
        risk       = min(
                        (RISK_MIN + (RISK_MAX - RISK_MIN) * confidence)
                        * self._risk_multiplier()
                        * self._calibration_multiplier(confidence)
                        * self._feature_multiplier(fkey),
                        RISK_MAX)
        leverage   = round(LEVERAGE_MIN + (LEVERAGE_MAX - LEVERAGE_MIN) * confidence)
        fill       = price * (1 + SLIPPAGE) if side == "LONG" else price * (1 - SLIPPAGE)
        margin     = round(self.balance * risk, 4)
        contracts  = round((margin * leverage) / fill, 6)
        fee        = round(margin * KRAKEN_FEE, 4)
        self.balance = round(self.balance - fee, 4)
        self.day_trades += 1
        conf_pct   = int(confidence * 100)
        if atr is not None:
            atr_dist = round(atr * self._atr_mult(pair), 8)
        else:
            atr_dist = round(fill * TRAIL_PCT, 8)
        # Better stop placement: use nearest support/resistance when tighter than ATR
        stop_label = "ATR"
        if stop is not None and stop > 0:
            if side == "LONG":
                struct_dist = fill - stop
                if 0 < struct_dist < atr_dist * 1.5:
                    atr_dist   = round(struct_dist, 8)
                    stop_label = "structure"
            else:
                struct_dist = stop - fill
                if 0 < struct_dist < atr_dist * 1.5:
                    atr_dist   = round(struct_dist, 8)
                    stop_label = "structure"
        if atr is None:
            stop_label = "fixed"
        trail_stop = round(fill - atr_dist if side == "LONG" else fill + atr_dist, 6)
        effective_target = float("inf") if (side == "LONG"  and self._trail_only(pair)) else \
                           0.0          if (side == "SHORT" and self._trail_only(pair)) else target
        self.positions[pair] = {"side": side, "entry": fill,
                                "contracts": contracts, "margin": margin,
                                "target": effective_target, "opened_at": time.time(),
                                "confidence": confidence, "leverage": leverage,
                                "pair": pair, "name": name,
                                "trail_stop": trail_stop, "trail_peak": fill,
                                "atr_dist": atr_dist, "fkey": fkey,
                                "entry_nasdaq": market_mood["nasdaq"],
                                "entry_news": news_sentiment.get(pair, {}).get("sentiment", "NEUTRAL")}
        self._save()
        mul = self._risk_multiplier()
        dd_note = f" ⚠️ `{int(mul*100)}%` size (losing streak)" if mul < 1.0 else ""
        tg(f"📂 *Trade OPENED — {name}*\n"
           f"{'🟢 LONG' if side=='LONG' else '🔴 SHORT'} @ `${fill:.4f}` (slip+fee: `${fee:.3f}`)\n"
           f"Confidence: `{conf_pct}%` | Size: `{risk*100:.1f}%` of balance{dd_note}\n"
           f"Margin: `${margin:.2f}` | *{leverage}x leverage*\n"
           f"Trail stop: `${trail_stop:.4f}` ({stop_label}) | Balance: `${self.balance:.2f}`")
        _print_trade_box("OPEN", name, side, fill,
                         stop=trail_stop, target=target,
                         confidence=confidence, leverage=leverage,
                         margin=margin, fee=fee,
                         balance=self.balance,
                         stop_type=stop_label)

    def _close(self, price, name, reason, pair):
        p = self.positions.get(pair)
        if not p: return
        fill = price * (1 - SLIPPAGE) if p["side"] == "LONG" else price * (1 + SLIPPAGE)
        move = (fill - p["entry"]) / p["entry"]
        if p["side"] == "SHORT": move = -move
        fee          = round(p["margin"] * KRAKEN_FEE, 4)
        pnl          = round(move * p["margin"] * p.get("leverage", LEVERAGE_MIN) - fee, 4)
        self.balance = round(self.balance + pnl, 4)
        self.peak    = max(self.peak, self.balance)
        held_mins    = round((time.time() - p.get("opened_at", time.time())) / 60, 1)
        cs = self.coin_stats.setdefault(name, {"wins": 0, "losses": 0})
        if pnl >= 0: cs["wins"] += 1
        else:        cs["losses"] += 1
        fkey       = p.get("fkey", "")
        entry_nasdaq = p.get("entry_nasdaq", market_mood["nasdaq"])
        entry_news   = p.get("entry_news",   news_sentiment.get(pair, {}).get("sentiment", "NEUTRAL"))
        trade_rec = {"side": p["side"], "entry": p["entry"], "exit": fill,
                     "pnl": pnl, "coin": p.get("name", name),
                     "confidence": p.get("confidence", 0.0),
                     "held_mins": held_mins, "reason": reason, "ts": time.time()}
        db.log_feature(fkey, pair, pnl > 0)
        self.trades.append(trade_rec)
        db.save_trade({
            "ts": trade_rec["ts"], "coin": trade_rec["coin"], "pair": pair,
            "side": trade_rec["side"], "entry": trade_rec["entry"],
            "exit_price": fill, "pnl": pnl, "held_mins": held_mins,
            "reason": reason, "confidence": trade_rec["confidence"],
            "nasdaq_mood": entry_nasdaq,
            "news_sent": entry_news,
            "balance_after": self.balance,
        })
        # Cooldown: block re-entry on this pair after a loss
        if pnl < 0:
            cooldown = 1800 if reason in ("trailing stop", "signal flip") else 900
            self._cooldown[pair] = time.time() + cooldown
            log("PAPER", f"{name} cooldown {cooldown//60}m after {reason} loss")
        emoji = "✅" if pnl >= 0 else "❌"
        # Trade journal — rich close message
        nasdaq_icon = "📈" if entry_nasdaq == "BULLISH" else "📉" if entry_nasdaq == "BEARISH" else "➖"
        news_icon   = "🟢" if entry_news  == "BULLISH" else "🔴" if entry_news  == "BEARISH" else "⚫"
        move_pct    = round((fill - p["entry"]) / p["entry"] * 100 * (1 if p["side"]=="LONG" else -1), 2)
        fkey_note   = f"\n📊 Signal: `{fkey}`" if fkey else ""
        tg(f"{emoji} *Trade CLOSED — {name}*\n"
           f"{'🟢 L' if p['side']=='LONG' else '🔴 S'} "
           f"`${p['entry']:.4f}` → `${fill:.4f}` ({move_pct:+.2f}%)\n"
           f"Reason: `{reason}` | Held: `{held_mins:.0f} min`\n"
           f"Conf: `{int(p.get('confidence',0)*100)}%` | Fee: `${fee:.3f}`\n"
           f"PnL: `{'+'if pnl>=0 else ''}{pnl:.2f}$` | Balance: `${self.balance:.2f}`\n"
           f"Win rate: `{self.win_rate:.0f}%`{fkey_note}\n"
           f"{nasdaq_icon} NASDAQ: `{entry_nasdaq}` {news_icon} News: `{entry_news}`")
        _print_trade_box("CLOSE", name, p["side"], fill,
                         pnl=pnl, reason=reason,
                         held_mins=held_mins,
                         balance=self.balance,
                         win_rate=self.win_rate)

        new_rank = get_rank(self.balance)
        if new_rank["name"] != self.current_rank:
            old = next((r for r in RANKS if r["name"] == self.current_rank), RANKS[0])
            nxt = get_next_rank(self.balance)
            tg(f"⬆️ *RANK UP!*\n{old['emoji']} {old['name']} → {new_rank['emoji']} *{new_rank['name']}*\n"
               f"_{new_rank['unlock']}_\nNext: {nxt['emoji']} {nxt['name']} @ `${nxt['min']:,.0f}`")
            self.current_rank = new_rank["name"]

        del self.positions[pair]
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
        self.positions     = {}
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

    def evaluate(self, closes, highs, lows, volumes, price, alert_buffer, pair=None):
        ema = calc_ema(closes)
        rsi = calc_rsi(closes)

        # MACD momentum
        try:
            macd, macd_sig, macd_hist = calc_macd(closes)
            macd_bull = macd_hist > 0   # histogram above zero = bullish momentum
            macd_bear = macd_hist < 0
        except Exception:
            macd_bull = macd_bear = False
            macd_hist = 0.0

        # Volume confirmation — is this candle backed by above-average volume?
        avg_vol = sum(volumes) / len(volumes) if volumes else 1
        high_volume = volumes[-1] > avg_vol * 1.2 if avg_vol > 0 else False
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

        # news gate + news-triggered entry
        current_pair = pair or _current_coin["pair"]
        news    = news_sentiment.get(current_pair, {})
        n_score = news.get("score", 0)
        n_sent  = news.get("sentiment", "NEUTRAL")

        # Block trades that go strongly against news
        if n_score >= 2:
            if sig == "BUY"  and n_sent == "BEARISH": sig = "HOLD"
            if sig == "SELL" and n_sent == "BULLISH": sig = "HOLD"

        # MACD gate — block signals that fight the momentum
        if sig == "BUY"  and macd_bear and macd_hist < -0.005 * price / 1000: sig = "HOLD"
        if sig == "SELL" and macd_bull and macd_hist >  0.005 * price / 1000: sig = "HOLD"

        # News-triggered entry: strong news + EMA alignment → trade even without confirm ticks
        if sig == "HOLD" and n_score >= 3:
            if n_sent == "BULLISH" and above_ema and rsi < 65 and not macd_bear:
                sig  = "BUY"
                plan = {"enter": price,
                        "exit":  nearest_r if nearest_r else price * 1.015,
                        "stop":  nearest_s if nearest_s else price * 0.985}
            elif n_sent == "BEARISH" and not above_ema and rsi > 35 and not macd_bull:
                sig  = "SELL"
                plan = {"enter": price,
                        "exit":  nearest_s if nearest_s else price * 0.985,
                        "stop":  nearest_r if nearest_r else price * 1.015}

        # NASDAQ gate
        nasdaq_mood = market_mood["nasdaq"]
        if nasdaq_mood == "BEARISH" and sig == "BUY": sig = "HOLD"

        # Fear & Greed gate
        fg_val = fear_greed["value"]
        if fg_val > 80 and sig == "BUY":  sig = "HOLD"  # extreme greed = dangerous to buy
        if fg_val < 20 and sig == "SELL": sig = "HOLD"  # extreme fear = bounce likely, skip shorts

        # Bitcoin dominance gate — rising BTC dominance means capital rotating into BTC,
        # altcoins bleed; block new altcoin longs until dominance stabilises
        if btc_dominance["rising"] and sig == "BUY" and current_pair != "XBTUSD":
            sig = "HOLD"

        # Funding rate gate — extreme funding = overcrowded side, flush risk
        fr = funding_rates.get(current_pair, 0.0)
        if fr >  FUNDING_THRESHOLD and sig == "BUY":  sig = "HOLD"
        if fr < -FUNDING_THRESHOLD and sig == "SELL": sig = "HOLD"

        # RSI divergence gate — price and RSI disagreeing = unreliable signal
        divergence = detect_divergence(closes, rsi)
        if divergence == "BEARISH_DIV" and sig == "BUY":  sig = "HOLD"
        if divergence == "BULLISH_DIV" and sig == "SELL": sig = "HOLD"

        # Market regime gate — suppress entries in choppy, directionless markets
        regime = detect_regime(closes, highs, lows)
        if regime == "CHOPPY" and sig in ("BUY", "SELL"):
            sig = "HOLD"

        # Time-of-day filter — avoid low-liquidity overnight hours
        if not _in_active_hours() and sig in ("BUY", "SELL"):
            sig = "HOLD"

        # ── Confidence score (0.0 → 1.0) ─────────────────────────────────────
        # Scored across 6 pillars, each worth 1 point → normalised to 0–1
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
        # 4. Tick strength (or news score as proxy for news-triggered entries)
        if ticks > 0:
            pts += min(ticks / 5.0, 1.0)
        elif sig in ("BUY", "SELL") and n_score >= 3:
            pts += min(n_score / 5.0, 1.0)
        # 5. MACD momentum alignment
        if (sig == "BUY" and macd_bull) or (sig == "SELL" and macd_bear): pts += 1.0
        # 6. Volume — high-volume moves are more reliable
        if high_volume: pts += 1.0
        else:           pts += 0.5

        confidence = round(pts / 6.0, 2) if sig in ("BUY", "SELL") else 0.0

        # Trending regime boosts confidence slightly (signal quality is higher)
        if regime == "TRENDING" and sig in ("BUY", "SELL"):
            confidence = min(round(confidence + 0.05, 2), 1.0)

        # ── Feature fingerprint key ──────────────────────────────────────────
        rsi_bin  = min(int(rsi / 20), 4)          # 0-4 (20-point buckets)
        ema_side = 1 if above_ema else 0
        macd_bit = 1 if macd_bull else 0
        vol_bit  = 1 if high_volume else 0
        news_bit = "B" if n_sent == "BULLISH" else "R" if n_sent == "BEARISH" else "N"
        fkey     = f"r{rsi_bin}e{ema_side}m{macd_bit}v{vol_bit}n{news_bit}"
        if sig in ("BUY", "SELL"):
            plan["fkey"] = fkey

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
            log("NEWS", str(e), "ERR")
        time.sleep(120)

# ── NASDAQ monitor ────────────────────────────────────────────────────────────
def _nasdaq_loop():
    while True:
        try:
            import yfinance as yf
            # Use intraday 5-min bars for same-session change vs today's open
            hist = yf.Ticker("QQQ").history(period="1d", interval="5m")
            if len(hist) >= 2:
                open_price  = float(hist["Open"].iloc[0])
                last_price  = float(hist["Close"].iloc[-1])
                chg = (last_price - open_price) / open_price * 100
                market_mood["change_pct"] = round(chg, 2)
                market_mood["nasdaq"] = "BULLISH" if chg > 0.5 else "BEARISH" if chg < -1.0 else "NEUTRAL"
                mood_col = _C.GREEN if market_mood["nasdaq"]=="BULLISH" else _C.RED if market_mood["nasdaq"]=="BEARISH" else _C.GREY
                log("NASDAQ", f"QQQ intraday {chg:+.2f}%  →  {_c(mood_col + _C.BOLD, market_mood['nasdaq'])}")
            else:
                # Market closed / pre-market: fall back to prior-day comparison
                hist2 = yf.Ticker("QQQ").history(period="2d", interval="1d")
                if len(hist2) >= 2:
                    chg = ((hist2["Close"].iloc[-1] - hist2["Close"].iloc[-2]) / hist2["Close"].iloc[-2]) * 100
                    market_mood["change_pct"] = round(chg, 2)
                    market_mood["nasdaq"] = "BULLISH" if chg > 1 else "BEARISH" if chg < -2 else "NEUTRAL"
                    mood_col = _C.GREEN if market_mood["nasdaq"]=="BULLISH" else _C.RED if market_mood["nasdaq"]=="BEARISH" else _C.GREY
                    log("NASDAQ", f"QQQ prior-day {chg:+.2f}%  →  {_c(mood_col + _C.BOLD, market_mood['nasdaq'])}")
        except Exception as e:
            log("NASDAQ", str(e), "ERR")
        time.sleep(300)

# ── Social / trending scanner ─────────────────────────────────────────────────
REDDIT_SUBS = [
    "https://www.reddit.com/r/CryptoCurrency/hot.json?limit=25",
    "https://www.reddit.com/r/memecoins/hot.json?limit=25",
    "https://www.reddit.com/r/dogecoin/hot.json?limit=15",
    "https://www.reddit.com/r/SHIBArmy/hot.json?limit=15",
]

def _trending_loop():
    global trending_boost
    known_trending = set()
    while True:
        new_boost = {}
        # ── CoinGecko trending (top 7 globally) ──────────────────────────
        try:
            r = requests.get("https://api.coingecko.com/api/v3/search/trending",
                             timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok:
                for item in r.json().get("coins", []):
                    cg_id = item["item"]["id"]
                    pair  = COINGECKO_TO_KRAKEN.get(cg_id)
                    if pair and pair in {c["pair"] for c in SCAN_UNIVERSE}:
                        new_boost[pair] = new_boost.get(pair, 0) + 25
                        if pair not in known_trending:
                            known_trending.add(pair)
                            name = next((c["name"] for c in SCAN_UNIVERSE if c["pair"]==pair), pair)
                            tg(f"🔥 *Trending on CoinGecko — {name}*\n"
                               f"Top 7 globally — score boosted for next scan")
        except Exception as e:
            log("TRENDING", f"CoinGecko error: {e}", "ERR")
        # ── Reddit hot posts ──────────────────────────────────────────────
        for sub_url in REDDIT_SUBS:
            try:
                r = requests.get(sub_url, timeout=10,
                                 headers={"User-Agent": "CryptoBot/1.0"})
                if not r.ok: continue
                posts = r.json()["data"]["children"]
                for post in posts:
                    title = post["data"]["title"].lower()
                    for coin in SCAN_UNIVERSE:
                        kws = COIN_KEYWORDS.get(coin["pair"], [])
                        if any(kw in title for kw in kws):
                            new_boost[coin["pair"]] = new_boost.get(coin["pair"], 0) + 8
            except Exception as e:
                log("TRENDING", f"Reddit error: {e}", "ERR")
        # Alert on new entries from Reddit too
        for pair, pts in new_boost.items():
            if pts >= 16 and pair not in known_trending:
                known_trending.add(pair)
                name = next((c["name"] for c in SCAN_UNIVERSE if c["pair"]==pair), pair)
                tg(f"📢 *Viral on Reddit — {name}*\n"
                   f"Multiple posts detected — added to scan priority")
        # Reset known_trending each cycle so coins can re-alert if they stop/restart trending
        known_trending = set(new_boost.keys())
        trending_boost = new_boost
        hits = [(k, v) for k, v in new_boost.items() if v > 0]
        log("TRENDING", f"Updated  {len(hits)} boosted coins: {hits if hits else 'none'}")
        time.sleep(1800)  # refresh every 30 min

# ── Fear & Greed Index ────────────────────────────────────────────────────────
def _fear_greed_loop():
    while True:
        try:
            r = requests.get("https://api.alternative.me/fng/", timeout=10)
            data = r.json()["data"][0]
            fear_greed["value"] = int(data["value"])
            fear_greed["label"] = data["value_classification"]
            val = fear_greed["value"]
            emoji = "😱" if val < 25 else "😨" if val < 45 else "😐" if val < 55 else "😄" if val < 75 else "🤑"
            fg_col = _C.RED if val < 25 else _C.YELLOW if val < 45 else _C.GREY if val < 55 else _C.GREEN if val < 75 else _C.MAGENTA
            log("F&G", f"{_c(fg_col + _C.BOLD, str(val))} — {fear_greed['label']}")
            if val <= 20 or val >= 80:
                tg(f"{emoji} *Fear & Greed: {val} — {fear_greed['label']}*\n"
                   f"{'Extreme fear — market may be oversold' if val <= 20 else 'Extreme greed — new longs are blocked'}")
        except Exception as e:
            log("F&G", str(e), "ERR")
        time.sleep(3600)  # refresh hourly

# ── Bitcoin dominance monitor ────────────────────────────────────────────────
def _btc_dominance_loop():
    while True:
        try:
            r = requests.get("https://api.coingecko.com/api/v3/global",
                             timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok:
                pct  = r.json()["data"]["market_cap_percentage"].get("btc", 50.0)
                prev = btc_dominance["pct"]
                # Only flag as "rising" when dominance climbs meaningfully (>0.3pp)
                rising = pct > prev + 0.3
                btc_dominance["prev_pct"] = prev
                btc_dominance["pct"]      = round(pct, 1)
                btc_dominance["rising"]   = rising
                dom_note = _c(_C.YELLOW + _C.BOLD, "↑ rising — altcoin longs blocked") if rising else _c(_C.GREY, "stable/falling")
                log("BTC DOM", f"{pct:.1f}%  {dom_note}")
                if rising:
                    tg(f"⚠️ *BTC Dominance Rising — {pct:.1f}%*\n"
                       f"Capital rotating into BTC — altcoin longs blocked until it stabilises")
        except Exception as e:
            log("BTC DOM", str(e), "ERR")
        time.sleep(3600)  # hourly

# ── Binance funding rate monitor ─────────────────────────────────────────────
def _funding_loop():
    while True:
        updated = 0
        for pair, symbol in KRAKEN_TO_BINANCE.items():
            try:
                r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                                 params={"symbol": symbol}, timeout=5)
                if r.ok:
                    fr = float(r.json()["lastFundingRate"])
                    funding_rates[pair] = fr
                    updated += 1
                    if abs(fr) >= FUNDING_THRESHOLD:
                        name = next((c["name"] for c in SCAN_UNIVERSE if c["pair"] == pair), pair)
                        side = "LONG" if fr > 0 else "SHORT"
                        log("FUNDING", f"{name}  {fr:.4%}  {_c(_C.YELLOW, f'extreme -> {side}s blocked')}")
            except Exception:
                pass
        log("FUNDING", f"Updated {updated}/{len(KRAKEN_TO_BINANCE)} pairs")
        time.sleep(3600)  # funding resets every 8h; refresh hourly is sufficient

# ── Top-coin display updater ─────────────────────────────────────────────────
def _switcher_loop(trader):
    global _current_coin
    time.sleep(20)
    while True:
        try:
            scores = rank_coins()
            if scores:
                top_pair = scores[0]["pair"]
                with _state_lock:
                    if top_pair != _current_coin["pair"]:
                        new_coin = next((c for c in SCAN_UNIVERSE if c["pair"] == top_pair), None)
                        if new_coin:
                            _current_coin = new_coin
                            log("SWITCH", f"Top coin → {_c(_C.CYAN + _C.BOLD, new_coin['name'])}")
        except Exception as e:
            log("SWITCH", str(e), "ERR")
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
    fg_val  = fear_greed["value"]
    fg_icon = "😱" if fg_val < 25 else "😨" if fg_val < 45 else "😐" if fg_val < 55 else "😄" if fg_val < 75 else "🤑"
    n_pos  = len(trader.positions) if trader else 0
    pos_str = f" | 📂 `{n_pos}/{MAX_POSITIONS}` open" if n_pos else ""
    header = (
        f"🤖 *CryptoBot* | {status}\n"
        f"💵 Balance: `{bal}` | 🎯 Goal: `${PAPER_TARGET:,.0f}`\n"
        f"📈 Top: *{coin}*{pos_str} | {nasdaq_icon} NASDAQ: `{nasdaq}`\n"
        f"{fg_icon} Fear & Greed: `{fg_val}` — _{fear_greed['label']}_"
    )
    rows = [
        [{"text": "💰 Balance & Stats",  "callback_data": "balance"},
         {"text": "📊 Rankings",         "callback_data": "rankings"}],
        [{"text": "📜 Trade History",    "callback_data": "history"},
         {"text": "📰 News Feed",        "callback_data": "news"}],
        [{"text": "🔄 Switch Coin",      "callback_data": "switch_menu"},
         {"text": "🏆 Rank Ladder",      "callback_data": "ranks"}],
        [{"text": "🧠 Intelligence",     "callback_data": "intelligence"}],
        [{"text": "⏸ Pause" if not paused else "▶ Resume",
          "callback_data": "pause" if not paused else "resume"},
         {"text": "🔒 Limits: ON" if _daily_limits else "🔓 Limits: OFF",
          "callback_data": "toggle_limits"}],
    ]
    if trader and trader.positions:
        n = len(trader.positions)
        rows.insert(-1, [{"text": f"🚨 Close {n} Position{'s' if n > 1 else ''}", "callback_data": "force_close"}])
    tg_buttons(header, rows)

def _handle_callback(query, trader):
    global _paused, _current_coin
    tg_answer(query["id"])
    data = query.get("data", "")
    try:
        _dispatch_callback(data, query, trader)
    except Exception as e:
        log("CALLBACK", f"{data!r} error: {e}", "ERR")
        tg_buttons(f"⚠️ *Error:* `{e}`",
                   [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])

def _dispatch_callback(data, query, trader):
    global _paused, _current_coin, _daily_limits

    if data == "menu":
        send_menu(trader)

    elif data == "balance":
        rank     = get_rank(trader.balance)
        next_rnk = get_next_rank(trader.balance)
        progress = min(100, max(0, (trader.balance - PAPER_START) / (PAPER_TARGET - PAPER_START) * 100))
        bar_filled = int(progress / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        losses = len(trader.trades) - trader.wins

        # Build positions section with live PnL per position
        pos_snap   = dict(trader.positions)
        pos_lines  = []
        close_btns = []
        for pr, p in pos_snap.items():
            try:
                live = get_price(pr)
                upnl = trader.unrealized_pnl(live, pr)
                upnl_str = f" `({'+'if upnl>=0 else ''}{upnl:.2f}$)`"
            except Exception:
                upnl_str = ""
            side_icon = "🟢 L" if p["side"] == "LONG" else "🔴 S"
            conf_str  = f" {int(p.get('confidence',0)*100)}% {p.get('leverage',LEVERAGE_MIN)}x"
            pos_lines.append(f"  {side_icon} *{p['name']}* @ `${p['entry']:.4f}`{upnl_str}{conf_str}")
            close_btns.append({"text": f"🚨 Close {p['name']}", "callback_data": f"close_{pr}"})
        pos_text = "\n".join(pos_lines) if pos_lines else "  None"

        fg_val  = fear_greed["value"]
        fg_icon = "😱" if fg_val < 25 else "😨" if fg_val < 45 else "😐" if fg_val < 55 else "😄" if fg_val < 75 else "🤑"
        cons_loss = trader.consecutive_losses
        dd_line = f"⚠️ Streak: `{cons_loss} losses` → size at `{int(trader._risk_multiplier()*100)}%`" if cons_loss >= 3 else f"✅ Streak: `{cons_loss} losses` (full size)"

        buttons = []
        for i in range(0, len(close_btns), 2):
            buttons.append(close_btns[i:i+2])
        buttons.append([{"text": "🔙 Back to Menu", "callback_data": "menu"}])
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
            f"📅 Today:   `{trader.day_trades}/{MAX_TRADES_DAY}` trades | P&L: `{'+'if (trader.balance-trader.day_start_bal)>=0 else ''}{trader.balance-trader.day_start_bal:.2f}$`\n"
            f"{dd_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 Positions ({len(pos_snap)}/{MAX_POSITIONS}):\n{pos_text}\n"
            f"{fg_icon} Fear & Greed: `{fg_val}` — _{fear_greed['label']}_\n"
            f"⬆️ Next Rank: {next_rnk['emoji']} {next_rnk['name']} @ `${next_rnk['min']:,.0f}`",
            buttons
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
                lines.append(f"  ✅ {r['emoji']} {r['name']}  `${r['min']:,.0f}`")
            else:
                lines.append(f"  ⬜ {r['emoji']} {r['name']}  `${r['min']:,.0f}`")
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

    elif data == "toggle_limits":
        _daily_limits = not _daily_limits
        status = "ON — stops after 10 trades or -10% day loss" if _daily_limits else "OFF — trades unlimited (paper mode)"
        tg_buttons(
            f"{'🔒' if _daily_limits else '🔓'} *Daily Limits {('ON' if _daily_limits else 'OFF')}*\n_{status}_",
            [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]]
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
        if not coin:
            tg_buttons("⚠️ *Unknown coin.*", [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])
        else:
            with _state_lock:
                _current_coin = coin
            tg_buttons(
                f"🔄 *Displaying {coin['name']}*\nBot scans all top coins automatically.",
                [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]]
            )

    elif data == "force_close":
        pos_snap = dict(trader.positions)
        if not pos_snap:
            tg_buttons("ℹ️ *No open positions.*",
                       [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])
        else:
            closed, failed = [], []
            for pr, p in pos_snap.items():
                try:
                    price = get_price(pr)
                    trader._close(price, p["name"], "manual close", pr)
                    closed.append(p["name"])
                except Exception as e:
                    failed.append(f"{p['name']}: {e}")
            summary = f"🚨 *Closed {len(closed)} position{'s' if len(closed)!=1 else ''}:* {', '.join(closed)}"
            if failed: summary += f"\n⚠️ Failed: {', '.join(failed)}"
            tg_buttons(summary, [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])

    elif data.startswith("close_"):
        pr = data[6:]
        p  = trader.positions.get(pr)
        if not p:
            tg_buttons("ℹ️ *Position already closed.*",
                       [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])
        else:
            try:
                price = get_price(pr)
            except Exception as e:
                tg_buttons(f"⚠️ *Could not fetch price for {p['name']}*\n`{e}`\nTry again.",
                           [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])
                return
            trader._close(price, p["name"], "manual close", pr)
            tg_buttons(f"🚨 *Closed {p['name']}* @ `${price:.4f}`",
                       [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])

    elif data == "intelligence":
        try:
            report = analyse_intelligence(trader.trades)
            # Telegram hard limit is 4096 chars; trim with a note if needed
            if len(report) > 3800:
                report = report[:3800].rsplit("\n", 1)[0] + "\n_...truncated_"
        except Exception as e:
            report = f"⚠️ Intelligence error: {e}"
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

def _daily_summary_loop(trader):
    while True:
        now      = datetime.now()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        time.sleep((midnight - now).total_seconds())
        try:
            cutoff = time.time() - 86400
            trades_today = [t for t in trader.trades if t.get("ts", 0) >= cutoff]
            rank = get_rank(trader.balance)
            if not trades_today:
                tg(f"📅 *Daily Summary*\nNo trades in the last 24 h.\n"
                   f"Balance: `${trader.balance:.2f}` | Trading: *{_current_coin['name']}*")
                continue
            wins      = [t for t in trades_today if t["pnl"] > 0]
            losses    = [t for t in trades_today if t["pnl"] <= 0]
            total_pnl = sum(t["pnl"] for t in trades_today)
            best      = max(trades_today, key=lambda t: t["pnl"])
            worst     = min(trades_today, key=lambda t: t["pnl"])
            wr        = len(wins) / len(trades_today) * 100
            tg(f"📅 *Daily Summary*\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"Trades: `{len(trades_today)}` | W: `{len(wins)}` L: `{len(losses)}`\n"
               f"Win Rate: `{wr:.0f}%` | PnL: `{'+'if total_pnl>=0 else ''}{total_pnl:.2f}$`\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"Best:  `+{best['pnl']:.2f}$` ({best.get('coin','?')})\n"
               f"Worst: `{worst['pnl']:.2f}$` ({worst.get('coin','?')})\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"Balance: `${trader.balance:.2f}` | {rank['emoji']} {rank['name']}\n"
               f"Trading: *{_current_coin['name']}*")
        except Exception as e:
            log("BOT", f"DailySummary error: {e}", "ERR")

def _pnl_update_loop(trader):
    while True:
        time.sleep(600)  # every 10 minutes
        for pr, p in list(trader.positions.items()):
            try:
                price = get_price(pr)
                upnl  = trader.unrealized_pnl(price, pr)
                mins  = round((time.time() - p.get("opened_at", time.time())) / 60, 1)
                pct   = upnl / p["margin"] * 100 if p.get("margin") else 0
                trail = p.get("trail_stop", 0)
                emoji = "📈" if upnl >= 0 else "📉"
                tg(f"{emoji} *Live Update — {p['name']}*\n"
                   f"{'🟢 LONG' if p['side']=='LONG' else '🔴 SHORT'} entry `${p['entry']:.4f}` → now `${price:.4f}`\n"
                   f"P&L: `{'+'if upnl>=0 else ''}{upnl:.2f}$` (`{pct:+.1f}%`)\n"
                   f"Trail stop: `${trail:.4f}` | Open: `{mins:.0f} min`")
            except Exception as e:
                log("PnL", f"{pr}: {e}", "ERR")

def _poll_loop(trader):
    global _last_update_id
    log("POLL", "Starting Telegram poll loop")
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                params={"offset":_last_update_id+1,"timeout":10,
                        "allowed_updates":["callback_query","message"]},
                timeout=15)
            if not r.ok:
                log("POLL", f"getUpdates error {r.status_code}: {r.text[:200]}", "ERR")
                time.sleep(5)
                continue
            updates = r.json().get("result", [])
            for u in updates:
                _last_update_id = u["update_id"]
                if "callback_query" in u:
                    cb = u["callback_query"]
                    user_id  = str(cb.get("from", {}).get("id", ""))
                    chat_id  = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                    if user_id != TG_CHAT_ID and chat_id != TG_CHAT_ID:
                        continue
                    btn = cb.get("data", "?")
                    log("POLL", f"Button → {_c(_C.CYAN, btn)}")
                    # Dispatch in a thread so the poll loop stays free to ack
                    # the next update — some callbacks (rankings, balance) make
                    # Kraken HTTP calls that can block for several seconds.
                    threading.Thread(target=_handle_callback,
                                     args=(cb, trader), daemon=True).start()
                elif "message" in u:
                    chat_id = str(u["message"].get("chat", {}).get("id", ""))
                    user_id = str(u["message"].get("from", {}).get("id", ""))
                    txt = u["message"].get("text", "").strip()
                    if chat_id != TG_CHAT_ID and user_id != TG_CHAT_ID:
                        continue
                    if txt.lower() in ("/start", "/menu", "menu"):
                        log("POLL", "Sending menu")
                        send_menu(trader)
        except Exception as e:
            log("POLL", str(e), "ERR")
        time.sleep(1)

# ── Main trading loop ─────────────────────────────────────────────────────────
def trading_loop(trader):
    engine_map   = {}   # pair → SignalEngine
    last_sigs    = {}   # pair → last signal string
    ranked_list  = []   # cached top coins from rank_coins()
    ranked_ts    = 0    # timestamp of last rankings refresh

    while True:
        try:
            now_ts = time.time()

            # Refresh coin rankings every 5 minutes
            if now_ts - ranked_ts > 300:
                try:
                    ranked_list = rank_coins()
                    ranked_ts   = now_ts
                    _print_rank_table(ranked_list)
                except Exception as e:
                    log("TRADE", f"rank_coins: {e}", "ERR")

            # ── Manage every open position every tick ──────────────────────
            for pair in list(trader.positions.keys()):
                p = trader.positions.get(pair)
                if not p: continue
                try:
                    closes, highs, lows, volumes = get_klines(pair)
                    price = get_price(pair)
                    try: atr = calc_atr(highs, lows, closes)
                    except Exception: atr = None
                    trader.on_signal("HOLD", price, 0, 0, p["name"], 0.0, pair, atr=atr)
                except Exception as e:
                    log("TRADE", f"manage {pair}: {e}", "ERR")

            # ── Scan top-ranked coins for new entry signals ────────────────
            for coin_score in ranked_list[:8]:
                pair = coin_score["pair"]
                if pair in trader.positions: continue   # already in this coin
                if not trader.can_open_new(): break     # position slots full

                coin = next((c for c in SCAN_UNIVERSE if c["pair"] == pair), None)
                if not coin: continue
                try:
                    closes, highs, lows, volumes = get_klines(pair)
                    price = get_price(pair)
                    if pair not in engine_map:
                        engine_map[pair] = SignalEngine()
                    eng = engine_map[pair]
                    sig, plan, ema, rsi, conf = eng.evaluate(
                        closes, highs, lows, volumes, price, coin["alert_buffer"], pair=pair)
                    try: atr = calc_atr(highs, lows, closes)
                    except Exception: atr = None

                    # 1-hour trend confirmation
                    if sig in ("BUY", "SELL"):
                        try:
                            closes_1h, _, _, _ = get_klines(pair, interval=60, limit=24)
                            ema_1h   = calc_ema(closes_1h)
                            trend_1h = "UP" if closes_1h[-1] > ema_1h else "DOWN"
                            if sig == "BUY"  and trend_1h != "UP":   sig = "HOLD"
                            if sig == "SELL" and trend_1h != "DOWN": sig = "HOLD"
                        except Exception: pass

                    sig_col  = _C.GREEN + _C.BOLD if sig == "BUY" else _C.RED + _C.BOLD if sig == "SELL" else _C.GREY
                    conf_col = _C.GREEN if conf >= 0.6 else _C.YELLOW if conf >= 0.4 else _C.RED
                    cname    = coin["name"]
                    sig_str  = _c(sig_col, f"{sig:<4}")
                    conf_str = _c(conf_col, f"{conf:.0%}")
                    log("TRADE", (f"{_c(_C.WHITE + _C.BOLD, cname):<12}"
                                  f"  ${price:<10.4f}"
                                  f"  RSI {rsi:<5}"
                                  f"  EMA {ema:<10.2f}"
                                  f"  {sig_str}"
                                  f"  conf {conf_str}"))

                    last_sig = last_sigs.get(pair)
                    if sig != last_sig and sig in ("BUY", "SELL"):
                        stop     = plan.get("stop",  price*0.985 if sig=="BUY" else price*1.015)
                        target   = plan.get("exit",  price*1.015 if sig=="BUY" else price*0.985)
                        fkey     = plan.get("fkey",  "")
                        risk     = RISK_MIN + (RISK_MAX - RISK_MIN) * conf
                        leverage = round(LEVERAGE_MIN + (LEVERAGE_MAX - LEVERAGE_MIN) * conf)
                        arrow    = "↑" if sig == "BUY" else "↓"
                        conf_pct = f"{int(conf*100)}%"
                        log("SIGNAL", (f"{_c(sig_col, f'{arrow} {sig} {cname}')}  "
                                       f"${price:.4f}  target ${target:.4f}  stop ${stop:.4f}  "
                                       f"conf {_c(conf_col, conf_pct)}  {leverage}x"))
                        emoji    = "🟢" if sig == "BUY" else "🔴"
                        tg(f"{emoji} *{sig} Signal — {coin['name']}*\n"
                           f"Enter: `${plan['enter']:.4f}` | Exit: `${target:.4f}` | Stop: `${stop:.4f}`\n"
                           f"EMA: `{ema:.2f}` | RSI: `{rsi}` | Conf: `{int(conf*100)}%`\n"
                           f"Size: `{risk*100:.1f}%` | Leverage: *{leverage}x*")
                        trader.on_signal(sig, price, stop, target, coin["name"], conf, pair,
                                         atr=atr, fkey=fkey)

                    last_sigs[pair] = sig
                except Exception as e:
                    log("TRADE", f"scan {pair}: {e}", "ERR")

        except Exception as e:
            log("TRADE", str(e), "ERR")
        time.sleep(REFRESH_SEC)

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    global _current_coin

    # ── Startup banner ────────────────────────────────────────────────────────
    BW = 56
    def _bcenter(text, col=_C.WHITE):
        pad_l = (BW - len(text)) // 2
        pad_r  = BW - len(text) - pad_l
        print(_c(_C.CYAN, "║") + " " * pad_l + _c(col, text) + " " * pad_r + _c(_C.CYAN, "║"))
    print(_c(_C.CYAN, "╔" + "═" * BW + "╗"))
    _bcenter("")
    _bcenter("◈  C R Y P T O B O T  ◈", _C.CYAN + _C.BOLD)
    _bcenter("─" * 30, _C.GREY)
    _bcenter("paper trading  ·  multi-coin  ·  26 pairs", _C.GREY)
    _bcenter("ATR trailing stops  ·  6-pillar confidence", _C.GREY)
    _bcenter("")
    print(_c(_C.CYAN, "╠" + "═" * BW + "╣"))
    _bcenter(datetime.now().strftime("Started  %Y-%m-%d  %H:%M  UTC"), _C.GREY)
    print(_c(_C.CYAN, "╚" + "═" * BW + "╝"))
    print()

    log("BOOT", f"Chat ID: {_c(_C.CYAN, TG_CHAT_ID)}")

    # Remove any webhook so long-polling (getUpdates) works
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/deleteWebhook",
                          json={"drop_pending_updates": False}, timeout=10)
        log("BOOT", f"deleteWebhook: {r.json().get('description', 'ok')}")
    except Exception as e:
        log("BOOT", f"deleteWebhook error: {e}", "ERR")

    # Plain-text ping — proves connectivity before any markdown
    ok = tg(f"CryptoBot booting... (chat_id={TG_CHAT_ID})", plain=True)
    log("BOOT", f"Telegram ping: {_c(_C.GREEN, 'OK') if ok else _c(_C.RED, 'FAILED')}")

    trader = PaperTrader()

    # Start background threads
    threads = [
        ("News scanner",      _news_loop,          ()),
        ("NASDAQ monitor",    _nasdaq_loop,         ()),
        ("Fear & Greed",      _fear_greed_loop,     ()),
        ("BTC Dominance",     _btc_dominance_loop,  ()),
        ("Funding rates",     _funding_loop,        ()),
        ("Trending scanner",  _trending_loop,       ()),
        ("Coin switcher",     _switcher_loop,       (trader,)),
        ("Telegram poll",     _poll_loop,           (trader,)),
        ("Daily summary",     _daily_summary_loop,  (trader,)),
        ("PnL updater",       _pnl_update_loop,     (trader,)),
    ]
    for name, fn, args in threads:
        threading.Thread(target=fn, args=args, daemon=True).start()
        log("BOOT", f"Started  {_c(_C.GREY, name)}")

    log("BOT", _c(_C.GREEN + _C.BOLD, "All systems online"))

    # Send menu immediately so Telegram responds without waiting for the coin scan
    rank     = get_rank(trader.balance)
    next_rnk = get_next_rank(trader.balance)
    progress = max(0,(trader.balance-PAPER_START)/(PAPER_TARGET-PAPER_START)*100)
    log("BOT", f"Balance: {_c(_C.GREEN + _C.BOLD, f'${trader.balance:.2f}')}  "
               f"rank: {rank['name']}  progress: {progress:.1f}%")
    send_menu(trader)
    tg(f"{rank['emoji']} *CryptoBot Online*\n"
       f"─────────────────────\n"
       f"Rank: *{rank['name']}* | Balance: `${trader.balance:.2f}`\n"
       f"Progress: `{progress:.1f}%` → Goal: `${PAPER_TARGET:,.0f}`\n"
       f"Trading: *{_current_coin['name']}*\n"
       f"Next rank: {next_rnk['name']} @ `${next_rnk['min']:,.0f}`\n"
       f"─────────────────────\n"
       f"_{rank['unlock']}_")

    # Find best coin on startup (runs after menu is already shown)
    try:
        log("BOT", "Scanning for best coin...")
        scores = rank_coins()
        if scores:
            _current_coin = next((c for c in SCAN_UNIVERSE if c["pair"]==scores[0]["pair"]), SCAN_UNIVERSE[0])
            log("BOT", f"Best coin: {_c(_C.CYAN + _C.BOLD, _current_coin['name'])}")
            _print_rank_table(scores)
    except Exception as e:
        log("BOT", f"Scan error: {e}", "ERR")

    # Main trading loop (runs forever)
    trading_loop(trader)

if __name__ == "__main__":
    main()
