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
import math
import io
import hashlib
import hmac
import base64
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from flask import Flask as _Flask, Response as _Response, request as _flask_request

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
def _clean_env(val):
    return val.strip().strip('"').strip("'").strip()

TG_TOKEN   = _clean_env(os.environ.get("TG_TOKEN",   ""))
TG_CHAT_ID = _clean_env(os.environ.get("TG_CHAT_ID", ""))
TG_URL     = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
BASE_URL         = "https://api.kraken.com/0/public"
BINANCE_BASE_URL = "https://api.binance.com"

# ── Live trading mode ─────────────────────────────────────────────────────────
# Set EXCHANGE=binance (or EXCHANGE=kraken, the default) to choose the exchange.
# Provide matching API keys — the bot auto-detects live mode from their presence.
KRAKEN_API_KEY    = _clean_env(os.environ.get("KRAKEN_API_KEY",    ""))
KRAKEN_API_SECRET = _clean_env(os.environ.get("KRAKEN_API_SECRET", ""))
BINANCE_API_KEY   = _clean_env(os.environ.get("BINANCE_API_KEY",   ""))
BINANCE_API_SECRET= _clean_env(os.environ.get("BINANCE_API_SECRET",""))
EXCHANGE          = _clean_env(os.environ.get("EXCHANGE", "kraken")).lower()
USE_BINANCE       = EXCHANGE == "binance"
LIVE_MODE         = bool(
    (USE_BINANCE and BINANCE_API_KEY and BINANCE_API_SECRET) or
    (not USE_BINANCE and KRAKEN_API_KEY and KRAKEN_API_SECRET)
)
LIVE_EXCHANGE     = "binance" if (USE_BINANCE and BINANCE_API_KEY) else "kraken"
BINANCE_FEE       = 0.001   # 0.10% per trade (vs Kraken's 0.26%)

# Binance kline interval strings
_BINANCE_IV = {1:"1m",3:"3m",5:"5m",15:"15m",30:"30m",60:"1h",120:"2h",240:"4h",1440:"1d"}

# Minimum order volumes for Kraken spot (in base-currency units)
_KRAKEN_MIN_VOL = {
    "XBTUSD": 0.0001, "XXBTZUSD": 0.0001,
    "ETHUSD": 0.002,  "XETHZUSD": 0.002,
    "SOLUSD": 0.5,    "XRPUSD":   5.0,
    "XDGUSD": 50.0,   "ADAUSD":   5.0,
    "AVAXUSD": 0.1,   "LINKUSD":  0.1,
    "DOTUSD": 0.1,    "LTCUSD":   0.01,
    "ATOMUSD": 0.1,   "UNIUSD":   0.1,
    "AAVEUSD": 0.01,  "INJUSD":   0.1,
    "SUIUSD": 5.0,    "APTUSD":   0.1,
    "ARBUSD": 1.0,    "NEARUSD":  0.5,
    "ALGOUSD": 5.0,   "FILUSD":   0.1,
    "BCHUSD": 0.01,   "PEPEUSD":  5000000.0,
    "BONKUSD": 100000.0, "SHIBUSD": 100000.0,
    "WIFUSD": 0.5,    "FLOKIUSD": 100000.0,
}

EMA_PERIOD    = 14
RSI_PERIOD    = 14
CANDLE_LIMIT  = 80             # more history for 15-min chart
INTERVAL      = 15             # 15-min candles: far less noise than 5-min
REFRESH_SEC   = 60             # poll every 60s on 15-min chart
CONFIRM_TICKS = 2              # 2 × 15-min bars = 30-min confirmation before signal

PAPER_START    = 100.0
PAPER_TARGET   = 50000.0
PAPER_FLOOR    = 50.0
LEVERAGE_MIN   = 1             # 1x (spot-like) at low confidence
LEVERAGE_MAX   = 3             # 3x max — was 5x, high leverage amplifies stop-outs
RISK_MIN       = 0.04          # 4% margin at low confidence
RISK_MAX       = 0.12          # 12% max — was 20%, limits per-trade blowup size
MAX_TRADE_GAIN   = 0.12        # full-close at 12% (was 8%) — let winners run longer
PARTIAL_TAKE_PCT = 0.08        # take 50% profit at 8% (was 5%) — more room before harvest
TRAIL_PCT        = 0.04        # 4% fallback trail on 15-min chart (was 3%)
ATR_PERIOD       = 14
ATR_MULTIPLIER   = 2.0         # 2× ATR trail (was 1.5) — gives trade room to breathe
MAX_TRADE_MINS   = 120         # 2-hour time limit (was 30 min) — trends need time
KRAKEN_FEE       = 0.0026
SLIPPAGE         = 0.001
MAX_TRADES_DAY   = 10
DAILY_LOSS_LIMIT = 0.10
ACTIVE_HOURS_UTC  = (5, 23)
FUNDING_THRESHOLD = 0.0005
MAX_POSITIONS     = 2          # max 2 simultaneous positions (was 3) — focus on quality
MAX_TOTAL_RISK    = 0.25       # total margin ≤ 25% (was 40%) — reduce correlated exposure
BREAKEVEN_PCT     = 0.020      # lock breakeven at +2% (was 1.2%) — 15-min moves are bigger
MIN_RR_RATIO      = 2.0        # require 2:1 R:R minimum (was 1.5) — only asymmetric trades
DAILY_GAIN_SOFT   = 0.03
DAILY_GAIN_HARD   = 0.06
LIVE_CHART_MINS   = 10
MAX_SESSION_DD    = 0.12
VOLUME_FILTER_MULT= 1.5
EXTREME_FUNDING   = 0.001
ECON_BLACKOUT_MINS= 15
MIN_CONFIDENCE    = 0.62       # 62% confidence floor (was 50%) — far fewer but higher-quality signals
ADX_PERIOD        = 14
ADX_MIN           = 25         # Wilder's "real trend" threshold (was 20) — no ranging markets
ER_PERIOD         = 10
ER_MIN            = 0.30       # stricter efficiency ratio (was 0.25) — cleaner directional moves

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

# Pairs that move together — don't hold two in the same direction simultaneously
CORRELATED_GROUPS = [
    frozenset({"XBTUSD", "ETHUSD"}),
    frozenset({"PEPEUSD", "BONKUSD", "WIFUSD", "FLOKIUSD", "XDGUSD"}),
    frozenset({"SOLUSD", "AVAXUSD", "APTUSD", "SUIUSD", "NEARUSD"}),
    frozenset({"XRPUSD", "ADAUSD", "ALGOUSD"}),
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pillar_outcomes (
                    id     SERIAL PRIMARY KEY,
                    pillar TEXT,
                    active BOOLEAN,
                    won    BOOLEAN,
                    ts     FLOAT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS pi_pillar ON pillar_outcomes(pillar, active)")

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
        """Per-pair recency-weighted win rate (14-day half-life, min trades threshold)."""
        if not self.conn: return {}
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    SELECT pair,
                           COUNT(*) AS n,
                           ROUND(
                               SUM(CASE WHEN pnl > 0
                                   THEN EXP(-(EXTRACT(EPOCH FROM NOW()) - ts) / 1209600.0)
                                   ELSE 0 END) /
                               NULLIF(SUM(EXP(-(EXTRACT(EPOCH FROM NOW()) - ts) / 1209600.0)), 0)
                               * 100
                           , 1) AS wr
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

    def hourly_win_rates(self):
        """Per-UTC-hour win rate (min 5 trades). Reveals which hours trade best."""
        if not self.conn: return {}
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    SELECT hour,
                           COUNT(*) AS n,
                           SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)::float/COUNT(*)*100 AS wr
                    FROM trades
                    WHERE hour IS NOT NULL
                    GROUP BY hour HAVING COUNT(*) >= 5
                """)
                return {int(r[0]): {"n": r[1], "wr": float(r[2])} for r in cur.fetchall()}
        except Exception as e:
            log("DB", f"hourly_win_rates error: {e}", "ERR")
            return {}

    def log_pillars(self, pillars: dict, won: bool):
        """Store per-pillar outcome for adaptive weight learning."""
        if not self.conn or not pillars: return
        try:
            with self.conn.cursor() as cur:
                for pillar, active in pillars.items():
                    cur.execute(
                        "INSERT INTO pillar_outcomes (pillar,active,won,ts) VALUES (%s,%s,%s,%s)",
                        (pillar, bool(active), won, time.time()))
        except Exception as e:
            log("DB", f"log_pillars error: {e}", "ERR")

    def pillar_win_rates(self):
        """Per-pillar win rate when that pillar was ACTIVE. Min 10 samples."""
        if not self.conn: return {}
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    SELECT pillar,
                           COUNT(*) AS n,
                           SUM(CASE WHEN won THEN 1 ELSE 0 END)::float/COUNT(*)*100 AS wr
                    FROM pillar_outcomes
                    WHERE active = true
                    GROUP BY pillar HAVING COUNT(*) >= 10
                """)
                return {r[0]: {"n": r[1], "wr": float(r[2])} for r in cur.fetchall()}
        except Exception as e:
            log("DB", f"pillar_win_rates error: {e}", "ERR")
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
# Long/Short ratio from Binance Futures (liquidation pressure proxy)
lsr_data        = {}   # pair → {"lsr": float, "bias": "LONG_HEAVY"|"SHORT_HEAVY"|"NEUTRAL"}
# Open interest from Binance Futures (confirms real money vs short-covering)
open_interest_data = {}  # pair → {"oi": float, "prev_oi": float, "trend": "RISING"|"FALLING"|"NEUTRAL"}
# High-impact economic calendar events (Forex Factory feed)
_econ_events    = []   # list of {"title": str, "date": str, "impact": str}
# User-set price alerts  {pair: [{"target": float, "above": bool, "label": str}]}
_price_alerts   = {}

# Market breadth: how many scanned coins are above their EMA (updated each rank cycle)
_market_breadth = {"above": 0, "total": 0}
# HTF trend cache — keyed (pair, interval): (trend_str, expires_ts)
# Avoids a Kraken API call on every 60-second tick for each coin.
_htf_cache: dict = {}
_HTF_CACHE_TTL = 900  # refresh 4h/1h trend every 15 minutes

# Chart-pattern cache — keyed by pair, refreshed every 15 minutes per coin
# {"name": str, "signal": "BULL"|"BEAR"|"NONE", "strength": float, "ts": float}
_pattern_cache: dict = {}
_PATTERN_TTL   = 900  # 15 minutes between rescans

_activity_log: list = []   # last 30 scan events for dashboard activity feed
_ACTIVITY_MAX  = 30

_btc_price_hist: list = []  # (timestamp, price) — rolling 35-min BTC tick history for momentum gate
_BTC_HIST_SECS  = 2100      # 35 minutes kept

# Live chart mode — when True, auto-send a fresh chart every LIVE_CHART_MINS
_live_charts_on = False

# Gate telemetry — count how many BUY/SELL signals each gate blocked per day
_gate_counters = {
    "choppy": 0, "volume": 0, "active_hours": 0, "econ": 0,
    "funding": 0, "divergence": 0, "nasdaq": 0, "fear_greed": 0,
    "btc_dom": 0, "btc_momentum": 0, "news": 0, "macd": 0, "1h_trend": 0, "4h_trend": 0,
    "pullback": 0, "momentum": 0, "rr_ratio": 0, "vwap": 0, "spike": 0, "daily_gain": 0,
    "adx": 0, "efficiency": 0, "min_conf": 0, "bb_squeeze": 0, "ob_imbalance": 0,
}
_gate_counter_lock = threading.Lock()

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

def tg_photo(buf, caption=""):
    """Send a PNG image buffer to the Telegram chat."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            data={"chat_id": TG_CHAT_ID, "caption": caption, "parse_mode": "Markdown"},
            files={"photo": ("chart.png", buf, "image/png")},
            timeout=20,
        )
        if not r.ok:
            log("TG", f"sendPhoto {r.status_code}: {r.text[:200]}", "ERR")
    except Exception as e:
        log("TG", f"sendPhoto error: {e}", "ERR")

def tg_buttons(msg, buttons):
    if not TG_TOKEN:
        return
    chat = TG_CHAT_ID or "0"
    attempts = [
        {"chat_id": chat, "text": msg, "parse_mode": "Markdown",
         "reply_markup": {"inline_keyboard": buttons}},
        {"chat_id": chat, "text": msg,
         "reply_markup": {"inline_keyboard": buttons}},
        {"chat_id": chat, "text": msg[:200].split("\n")[0],
         "reply_markup": {"inline_keyboard": [[{"text": "Menu", "callback_data": "menu"}]]}},
    ]
    for i, payload in enumerate(attempts):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              json=payload, timeout=10)
            if r.ok:
                return
            log("TG", f"Buttons attempt {i+1} failed {r.status_code}: {r.text[:200]}", "ERR")
        except Exception as e:
            log("TG", f"Buttons attempt {i+1} error: {e}", "ERR")

def tg_answer(cb_id, text=""):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery",
                          json={"callback_query_id": cb_id, "text": text}, timeout=5)
        if not r.ok:
            log("TG", f"answerCallbackQuery {r.status_code}: {r.text[:100]}", "WARN")
    except Exception as e:
        log("TG", f"answerCallbackQuery error: {e}", "WARN")

# ── Data ──────────────────────────────────────────────────────────────────────
def get_klines(pair, interval=None, limit=None):
    if USE_BINANCE:
        return _bn_get_klines(pair, interval, limit)
    iv = interval or INTERVAL
    lm = limit or CANDLE_LIMIT
    r = requests.get(f"{BASE_URL}/OHLC", params={"pair": pair, "interval": iv}, timeout=10)
    payload = r.json()
    if payload.get("error"):
        raise ValueError(f"Kraken OHLC error: {payload['error']}")
    rkey = next(k for k in payload["result"] if k != "last")
    data = payload["result"][rkey][-lm:]
    opens   = [float(c[1]) for c in data]
    closes  = [float(c[4]) for c in data]
    highs   = [float(c[2]) for c in data]
    lows    = [float(c[3]) for c in data]
    volumes = [float(c[6]) for c in data]
    return closes, highs, lows, volumes, opens

def get_price(pair):
    if USE_BINANCE:
        return _bn_get_price(pair)
    r = requests.get(f"{BASE_URL}/Ticker", params={"pair": pair}, timeout=10)
    payload = r.json()
    if payload.get("error"):
        raise ValueError(f"Kraken Ticker error: {payload['error']}")
    key = list(payload["result"].keys())[0]
    return float(payload["result"][key]["c"][0])

# ── Kraken private API (live trading mode) ───────────────────────────────────

def _kraken_private(method, params=None):
    """Sign and send a Kraken private REST request. Returns result dict or raises."""
    params = dict(params or {})
    nonce  = str(int(time.time() * 1000))
    params["nonce"] = nonce
    post_data = urllib.parse.urlencode(params)
    url_path  = f"/0/private/{method}"
    msg       = url_path.encode() + hashlib.sha256((nonce + post_data).encode()).digest()
    secret    = base64.b64decode(KRAKEN_API_SECRET)
    signature = base64.b64encode(hmac.new(secret, msg, hashlib.sha512).digest()).decode()
    headers   = {"API-Key": KRAKEN_API_KEY, "API-Sign": signature,
                 "Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(f"https://api.kraken.com{url_path}",
                      headers=headers, data=post_data, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")
    return data["result"]

def _kraken_get_usd_balance():
    """Return available USD balance from Kraken. Returns 0.0 on failure."""
    try:
        result = _kraken_private("Balance")
        return float(result.get("ZUSD", result.get("USD", 0)))
    except Exception as e:
        log("LIVE", f"balance fetch error: {e}", "ERR")
        return 0.0

def _kraken_place_order(pair, side, volume, validate=False):
    """
    Place a Kraken spot market order.
    side: 'buy' or 'sell'
    volume: base-currency units (e.g. BTC amount, not USD)
    Returns txid string or raises on failure.
    """
    min_vol = _KRAKEN_MIN_VOL.get(pair, 0.0)
    if volume < min_vol:
        raise ValueError(f"Order volume {volume:.8f} below Kraken minimum {min_vol} for {pair}")
    params = {
        "pair":       pair,
        "type":       side,
        "ordertype":  "market",
        "volume":     f"{volume:.8f}",
    }
    if validate:
        params["validate"] = "true"
    result = _kraken_private("AddOrder", params)
    txids  = result.get("txid", [])
    txid   = txids[0] if txids else "unknown"
    log("LIVE", f"Order placed: {side.upper()} {volume:.6f} {pair}  txid={txid}")
    return txid

def _kraken_cancel_order(txid):
    """Cancel an open Kraken order by txid. Silently ignores errors (order may already be filled)."""
    try:
        _kraken_private("CancelOrder", {"txid": txid})
    except Exception as e:
        log("LIVE", f"cancel {txid}: {e}", "WARN")

def _kraken_validate_keys():
    """Returns True if the API keys can authenticate successfully."""
    try:
        _kraken_private("Balance")
        return True
    except Exception as e:
        log("LIVE", f"key validation failed: {e}", "ERR")
        return False


# ── Binance public helpers ───────────────────────────────────────────────────

def _bn_pair(kraken_pair):
    """Translate Kraken pair name → Binance symbol (e.g. XBTUSD → BTCUSDT)."""
    return KRAKEN_TO_BINANCE.get(kraken_pair, kraken_pair)

def _bn_get_klines(pair, interval=None, limit=None):
    iv  = _BINANCE_IV.get(interval or INTERVAL, "15m")
    lm  = limit or CANDLE_LIMIT
    sym = _bn_pair(pair)
    r   = requests.get(f"{BINANCE_BASE_URL}/api/v3/klines",
                       params={"symbol": sym, "interval": iv, "limit": lm}, timeout=10)
    r.raise_for_status()
    data    = r.json()
    opens   = [float(c[1]) for c in data]
    highs   = [float(c[2]) for c in data]
    lows    = [float(c[3]) for c in data]
    closes  = [float(c[4]) for c in data]
    volumes = [float(c[5]) for c in data]
    return closes, highs, lows, volumes, opens

def _bn_get_price(pair):
    r = requests.get(f"{BINANCE_BASE_URL}/api/v3/ticker/price",
                     params={"symbol": _bn_pair(pair)}, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])

def _bn_get_ob_imbalance(pair):
    """Binance order book imbalance — bid_vol / ask_vol at top 10 levels."""
    try:
        r = requests.get(f"{BINANCE_BASE_URL}/api/v3/depth",
                         params={"symbol": _bn_pair(pair), "limit": 10}, timeout=5)
        if not r.ok: return None
        d = r.json()
        bid_vol = sum(float(b[1]) for b in d.get("bids", []))
        ask_vol = sum(float(a[1]) for a in d.get("asks", []))
        return bid_vol / ask_vol if ask_vol > 0 else None
    except Exception:
        return None

# ── Binance private API ──────────────────────────────────────────────────────

def _binance_private(path, http_method="GET", params=None):
    """Sign and send a Binance private REST request. Returns parsed JSON."""
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    query  = urllib.parse.urlencode(params)
    sig    = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    url = f"{BINANCE_BASE_URL}{path}"
    r   = (requests.get(url, params=params, headers=headers, timeout=20) if http_method == "GET"
           else requests.post(url, params=params, headers=headers, timeout=20))
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "code" in data and data["code"] != 200:
        raise RuntimeError(f"Binance API error: {data}")
    return data

def _binance_get_usdt_balance():
    """Return free USDT balance from Binance. Returns 0.0 on failure."""
    try:
        result = _binance_private("/api/v3/account")
        for b in result.get("balances", []):
            if b["asset"] == "USDT":
                return float(b["free"])
        return 0.0
    except Exception as e:
        log("LIVE", f"Binance balance error: {e}", "ERR")
        return 0.0

def _binance_place_order(pair, side, usdt_amount=None, base_qty=None):
    """
    Place a Binance spot market order.
    BUY:  pass usdt_amount  — spends that many USDT (quoteOrderQty)
    SELL: pass base_qty     — sells that many base-currency units
    Returns (order_id_str, filled_base_qty, avg_fill_price).
    """
    sym    = _bn_pair(pair)
    params = {"symbol": sym, "side": side, "type": "MARKET"}
    if side == "BUY" and usdt_amount is not None:
        params["quoteOrderQty"] = f"{usdt_amount:.2f}"
    elif base_qty is not None:
        # Round to Binance step size (8 dp is safe for most pairs)
        params["quantity"] = f"{base_qty:.8f}"
    else:
        raise ValueError("_binance_place_order: need usdt_amount (BUY) or base_qty (SELL)")
    result     = _binance_private("/api/v3/order", http_method="POST", params=params)
    order_id   = str(result["orderId"])
    filled_qty = float(result.get("executedQty", 0))
    fills      = result.get("fills", [])
    if fills:
        total_cost = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        total_qty  = sum(float(f["qty"]) for f in fills)
        avg_price  = total_cost / total_qty if total_qty > 0 else 0.0
    else:
        quote_qty  = float(result.get("cummulativeQuoteQty", 0))
        avg_price  = quote_qty / filled_qty if filled_qty > 0 else 0.0
    log("LIVE", f"Binance {side} {sym}  qty={filled_qty:.6f}  avg=${avg_price:.4f}  id={order_id}")
    return order_id, filled_qty, avg_price

def _binance_validate_keys():
    """Returns True if Binance API keys authenticate successfully."""
    try:
        _binance_private("/api/v3/account")
        return True
    except Exception as e:
        log("LIVE", f"Binance key validation failed: {e}", "ERR")
        return False

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

def calc_adx(highs, lows, closes, period=ADX_PERIOD):
    """Wilder's Average Directional Index — trend strength 0-100.
    ADX > 25 = strong trend; < 20 = weak/ranging (avoid entries)."""
    if len(closes) < period * 2 + 1:
        return 0.0
    try:
        plus_dm, minus_dm, tr_vals = [], [], []
        for i in range(1, len(closes)):
            up   = highs[i]  - highs[i-1]
            down = lows[i-1] - lows[i]
            plus_dm.append(up   if up > down and up > 0 else 0.0)
            minus_dm.append(down if down > up and down > 0 else 0.0)
            tr_vals.append(max(highs[i]-lows[i],
                               abs(highs[i]-closes[i-1]),
                               abs(lows[i]-closes[i-1])))
        def _wilder_smooth(vals, p):
            s = sum(vals[:p])
            out = [s]
            for v in vals[p:]:
                s = s - s/p + v
                out.append(s)
            return out
        atr14 = _wilder_smooth(tr_vals,  period)
        pdm14 = _wilder_smooth(plus_dm,  period)
        mdm14 = _wilder_smooth(minus_dm, period)
        pdi   = [100*p/max(a,1e-9) for p, a in zip(pdm14, atr14)]
        mdi   = [100*m/max(a,1e-9) for m, a in zip(mdm14, atr14)]
        dx    = [100*abs(p-m)/max(p+m,1e-9) for p, m in zip(pdi, mdi)]
        adx   = sum(dx[:period]) / period
        for v in dx[period:]:
            adx = (adx*(period-1) + v) / period
        return round(adx, 1)
    except Exception:
        return 0.0

def calc_efficiency_ratio(closes, period=ER_PERIOD):
    """Kaufman Efficiency Ratio: |net price move| / sum(|bar moves|).
    0.0 = random walk, 1.0 = perfectly directional. Only trade above ER_MIN."""
    if len(closes) < period + 1:
        return 0.0
    try:
        window     = closes[-(period+1):]
        net_move   = abs(window[-1] - window[0])
        total_move = sum(abs(window[i]-window[i-1]) for i in range(1, len(window)))
        return round(net_move / max(total_move, 1e-9), 3)
    except Exception:
        return 0.0

def calc_obv_trend(closes, volumes, period=10):
    """On-Balance Volume trend over last `period` candles.
    Returns 'RISING', 'FALLING', or 'FLAT'."""
    if len(closes) < period + 2 or len(volumes) < period + 2:
        return "FLAT"
    try:
        obv_vals = []
        running  = 0.0
        for i in range(1, len(closes)):
            if   closes[i] > closes[i-1]: running += volumes[i]
            elif closes[i] < closes[i-1]: running -= volumes[i]
            obv_vals.append(running)
        n = max(period // 2, 2)
        recent_avg = sum(obv_vals[-n:])   / n
        older_avg  = sum(obv_vals[-2*n:-n]) / n
        threshold = abs(older_avg) * 0.03
        if recent_avg > older_avg + threshold: return "RISING"
        if recent_avg < older_avg - threshold: return "FALLING"
        return "FLAT"
    except Exception:
        return "FLAT"

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


def calc_bb_squeeze(closes, period=20, std_dev=2.0):
    """True when Bollinger Band width is at a local low — direction unknown, breakout pending."""
    if len(closes) < period + 10:
        return False
    widths = []
    for i in range(len(closes) - period + 1):
        window = closes[i:i + period]
        mid    = sum(window) / period
        std    = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
        widths.append(2 * std_dev * std)
    if len(widths) < 10:
        return False
    current  = widths[-1]
    lookback = widths[-20:] if len(widths) >= 20 else widths
    avg      = sum(lookback) / len(lookback)
    return avg > 0 and current < avg * 0.55  # current < 55% of recent average = squeeze

def _get_ob_imbalance(pair):
    """Fetch top-10 order book levels and return bid_vol / ask_vol ratio.
    Routes to Binance or Kraken depending on EXCHANGE setting.
    Returns None on error. Ratio > 1 = more bid pressure; < 1 = more ask pressure."""
    if USE_BINANCE:
        return _bn_get_ob_imbalance(pair)
    try:
        r = requests.get(f"{BASE_URL}/Depth", params={"pair": pair, "count": 10}, timeout=5)
        if not r.ok:
            return None
        result = r.json().get("result", {})
        key    = next(iter(result), None)
        if not key:
            return None
        data   = result[key]
        bid_vol = sum(float(b[1]) for b in data.get("bids", []))
        ask_vol = sum(float(a[1]) for a in data.get("asks", []))
        if ask_vol <= 0:
            return None
        return bid_vol / ask_vol
    except Exception:
        return None

def detect_candle_pattern(opens, closes, highs, lows):
    """Detect 35+ candlestick patterns (single, two-candle, three-candle).
    Returns {"signal": "BULL"|"BEAR"|"NONE", "name": str}.
    Patterns: Doji family, Marubozu, Hammer/Star family, Engulfing, Kicker,
    Piercing/Dark Cloud, Harami, Tweezer, Morning/Evening Star, Abandoned Baby,
    Three Soldiers/Crows, Three Inside/Outside Up/Down, Three Line Strike,
    Rising/Falling Window, Spinning Top.
    """
    _N = {"signal": "NONE", "name": ""}
    if opens is None or len(opens) < 3 or len(closes) < 3:
        return _N
    try:
        # ── Candle geometry ───────────────────────────────────────────
        o,  c,  h,  l  = opens[-1], closes[-1], highs[-1], lows[-1]
        po, pc, ph, pl = opens[-2], closes[-2], highs[-2], lows[-2]
        o3, c3, h3, l3 = opens[-3], closes[-3], highs[-3], lows[-3]

        body   = c - o;     ab   = abs(body)
        pbody  = pc - po;   pab  = abs(pbody)
        body3  = c3 - o3;   ab3  = abs(body3)

        fr   = h  - l  or 1e-9    # full range current
        pfr  = ph - pl or 1e-9    # full range previous
        sfr  = h3 - l3 or 1e-9

        uw  = h  - max(o,  c)     # upper wick current
        lw  = min(o,  c)  - l
        puw = ph - max(po, pc)
        plw = min(po, pc) - pl

        br  = ab  / fr             # body ratio 0=doji 1=marubozu
        pbr = pab / pfr

        ref = (c + pc + c3) / 3 or 1
        gap = ref * 0.001          # 0.1% minimum gap

        # ── SINGLE-CANDLE ─────────────────────────────────────────────

        # Dragonfly Doji: body tiny, long lower wick → BULL
        if br < 0.10 and lw >= fr * 0.60 and uw <= fr * 0.10:
            return {"signal": "BULL", "name": "Dragonfly Doji"}

        # Gravestone Doji: body tiny, long upper wick → BEAR
        if br < 0.10 and uw >= fr * 0.60 and lw <= fr * 0.10:
            return {"signal": "BEAR", "name": "Gravestone Doji"}

        # Neutral Doji
        if br < 0.10:
            return _N

        # Bullish Marubozu: large green, wicks ≤5% of range
        if c > o and br >= 0.90 and uw <= fr * 0.05 and lw <= fr * 0.05:
            return {"signal": "BULL", "name": "Bullish Marubozu"}

        # Bearish Marubozu
        if c < o and br >= 0.90 and uw <= fr * 0.05 and lw <= fr * 0.05:
            return {"signal": "BEAR", "name": "Bearish Marubozu"}

        # Spinning Top: small body, notable wicks both sides
        if br < 0.35 and uw >= fr * 0.20 and lw >= fr * 0.20:
            return {"signal": "BULL" if c > o else "BEAR",
                    "name":   "Bullish Spinning Top" if c > o else "Bearish Spinning Top"}

        # Hammer / Hanging Man: long lower wick (≥2× body), tiny upper wick
        if lw >= 2 * ab and uw <= ab * 0.5 and ab > 0:
            # After bearish prev → Hammer (bullish reversal)
            # After bullish prev → Hanging Man (bearish reversal)
            return ({"signal": "BULL", "name": "Hammer"}
                    if pbody < 0 else {"signal": "BEAR", "name": "Hanging Man"})

        # Inverted Hammer / Shooting Star: long upper wick (≥2× body), tiny lower wick
        if uw >= 2 * ab and lw <= ab * 0.5 and ab > 0:
            return ({"signal": "BULL", "name": "Inverted Hammer"}
                    if pbody < 0 else {"signal": "BEAR", "name": "Shooting Star"})

        # ── TWO-CANDLE ────────────────────────────────────────────────

        # Bullish Engulfing
        if c > o and pbody < 0 and o <= pc and c >= po:
            return {"signal": "BULL", "name": "Bullish Engulfing"}

        # Bearish Engulfing
        if c < o and pbody > 0 and o >= pc and c <= po:
            return {"signal": "BEAR", "name": "Bearish Engulfing"}

        # Bullish Kicker: gap-up open after bearish candle, current bullish
        if pbody < 0 and c > o and o > po + gap:
            return {"signal": "BULL", "name": "Bullish Kicker"}

        # Bearish Kicker: gap-down open after bullish candle, current bearish
        if pbody > 0 and c < o and o < po - gap:
            return {"signal": "BEAR", "name": "Bearish Kicker"}

        # Piercing Line: bearish prev → bullish current opens below prev low, closes > prev midpoint
        if pbody < 0 and c > o and o < pl and c > (po + pc) / 2 and c < po:
            return {"signal": "BULL", "name": "Piercing Line"}

        # Dark Cloud Cover: bullish prev → bearish current opens above prev high, closes < prev midpoint
        if pbody > 0 and c < o and o > ph and c < (po + pc) / 2 and c > po:
            return {"signal": "BEAR", "name": "Dark Cloud Cover"}

        # Tweezer Bottom: equal lows, previous bearish
        if abs(l - pl) / ref < 0.002 and pbody < 0:
            return {"signal": "BULL", "name": "Tweezer Bottom"}

        # Tweezer Top: equal highs, previous bullish
        if abs(h - ph) / ref < 0.002 and pbody > 0:
            return {"signal": "BEAR", "name": "Tweezer Top"}

        # Bullish Harami: small green inside large bearish prev
        if pbody < 0 and c > o and o > pc and c < po and ab < pab * 0.5:
            return {"signal": "BULL", "name": "Bullish Harami"}

        # Bearish Harami: small red inside large bullish prev
        if pbody > 0 and c < o and o < pc and c > po and ab < pab * 0.5:
            return {"signal": "BEAR", "name": "Bearish Harami"}

        # Rising Window: gap up (current low > previous high)
        if l > ph + gap:
            return {"signal": "BULL", "name": "Rising Window"}

        # Falling Window: gap down (current high < previous low)
        if h < pl - gap:
            return {"signal": "BEAR", "name": "Falling Window"}

        # ── THREE-CANDLE ──────────────────────────────────────────────

        star_br = pab / pfr   # previous bar body ratio (star = small body)

        # Morning Star: bearish → small/doji → bullish, current closes > bar-3 midpoint
        if body3 < 0 and c > o and star_br < 0.35 and c > (o3 + c3) / 2:
            return {"signal": "BULL", "name": "Morning Star"}

        # Evening Star: bullish → small/doji → bearish, current closes < bar-3 midpoint
        if body3 > 0 and c < o and star_br < 0.35 and c < (o3 + c3) / 2:
            return {"signal": "BEAR", "name": "Evening Star"}

        # Three White Soldiers: three rising green candles
        if c > o and pc > po and c3 > o3 and c > pc > c3 and o > po > o3:
            return {"signal": "BULL", "name": "Three White Soldiers"}

        # Three Black Crows: three falling red candles
        if c < o and pc < po and c3 < o3 and c < pc < c3 and o < po < o3:
            return {"signal": "BEAR", "name": "Three Black Crows"}

        # Three Inside Up: bearish → bullish harami → confirmation
        if (body3 < 0 and pbody > 0 and body > 0 and
                po > c3 and pc < o3 and c > pc):
            return {"signal": "BULL", "name": "Three Inside Up"}

        # Three Inside Down: bullish → bearish harami → confirmation
        if (body3 > 0 and pbody < 0 and body < 0 and
                po < c3 and pc > o3 and c < pc):
            return {"signal": "BEAR", "name": "Three Inside Down"}

        # Three Outside Up: bearish → bullish engulfing → confirmation
        if (body3 < 0 and pbody > 0 and body > 0 and
                po <= c3 and pc >= o3 and c > pc):
            return {"signal": "BULL", "name": "Three Outside Up"}

        # Three Outside Down: bullish → bearish engulfing → confirmation
        if (body3 > 0 and pbody < 0 and body < 0 and
                po >= c3 and pc <= o3 and c < pc):
            return {"signal": "BEAR", "name": "Three Outside Down"}

        # Bullish Abandoned Baby: bearish → doji gapped below → bullish gapped up
        if (body3 < 0 and star_br < 0.10 and
                ph < l3 - gap and o > ph + gap and c > o):
            return {"signal": "BULL", "name": "Bullish Abandoned Baby"}

        # Bearish Abandoned Baby: bullish → doji gapped above → bearish gapped down
        if (body3 > 0 and star_br < 0.10 and
                pl > h3 + gap and o < pl - gap and c < o):
            return {"signal": "BEAR", "name": "Bearish Abandoned Baby"}

        # Three Line Strike — needs 4 bars
        if len(opens) >= 4:
            o4, c4 = opens[-4], closes[-4]
            if c4 < o4 and c3 < o3 and pc < po and c > o and c > o4:
                return {"signal": "BULL", "name": "Three Line Strike"}
            if c4 > o4 and c3 > o3 and pc > po and c < o and c < o4:
                return {"signal": "BEAR", "name": "Three Line Strike"}

    except Exception:
        pass
    return _N


def _find_swings(highs, lows, window=3):
    """Return (peaks, troughs) as (index, value) pairs from interior candles."""
    peaks, troughs = [], []
    n = len(highs)
    for i in range(window, n - window):
        if highs[i] >= max(highs[i - window: i + window + 1]):
            peaks.append((i, highs[i]))
        if lows[i] <= min(lows[i - window: i + window + 1]):
            troughs.append((i, lows[i]))
    return peaks, troughs


def _linreg_slope(vals):
    """Least-squares slope (rise per bar) of a value series."""
    n = len(vals)
    if n < 3:
        return 0.0
    x_bar = (n - 1) / 2.0
    y_bar = sum(vals) / n
    num = sum((i - x_bar) * (vals[i] - y_bar) for i in range(n))
    den = sum((i - x_bar) ** 2 for i in range(n))
    return num / max(den, 1e-9)


def detect_chart_pattern(closes, highs, lows, lookback=40):
    """Detect classical chart patterns (reversal, continuation, bilateral).
    Returns {"signal": "BULL"|"BEAR"|"NONE", "name": str, "strength": float}.
    Covers: Double Top/Bottom, Head & Shoulders, Inv. H&S, Rising/Falling Wedge,
    Ascending/Descending/Sym Triangle, Bull/Bear Rectangle, Bull/Bear Pennant.
    """
    _NONE = {"signal": "NONE", "name": "", "strength": 0.0}
    if len(closes) < 20:
        return _NONE
    n = min(lookback, len(closes))
    c = list(closes[-n:])
    h = list(highs[-n:])
    l = list(lows[-n:])
    price = c[-1]
    if price <= 0:
        return _NONE

    tol = 0.025  # 2.5% "same level" tolerance

    try:
        peaks, troughs = _find_swings(h, l, window=3)
    except Exception:
        return _NONE

    # ── Double Top (bearish reversal) ────────────────────────────
    # Requires: prior uptrend → two peaks at same level → price at/below neckline
    if len(peaks) >= 2:
        p1, p2 = peaks[-2], peaks[-1]
        if p1[1] > 0 and abs(p1[1] - p2[1]) / p1[1] <= tol:
            mid = [t for t in troughs if p1[0] < t[0] < p2[0]]
            if mid and c[p1[0]] > c[0] * (1 + tol):  # prior uptrend required
                neckline = max(t[1] for t in mid)
                if price <= neckline * (1 + tol):
                    return {"signal": "BEAR", "name": "Double Top", "strength": 0.85}

    # ── Double Bottom (bullish reversal) ─────────────────────────
    # Requires: prior downtrend → two troughs at same level → price at/above neckline
    if len(troughs) >= 2:
        t1, t2 = troughs[-2], troughs[-1]
        if t1[1] > 0 and abs(t1[1] - t2[1]) / t1[1] <= tol:
            mid = [p for p in peaks if t1[0] < p[0] < t2[0]]
            if mid and c[t1[0]] < c[0] * (1 - tol):  # prior downtrend required
                neckline = min(p[1] for p in mid)
                if price >= neckline * (1 - tol):
                    return {"signal": "BULL", "name": "Double Bottom", "strength": 0.85}

    # ── Head & Shoulders (bearish reversal) ──────────────────────
    if len(peaks) >= 3:
        ls_, hd_, rs_ = peaks[-3], peaks[-2], peaks[-1]
        if (hd_[1] > ls_[1] and hd_[1] > rs_[1]
                and ls_[1] > 0 and abs(ls_[1] - rs_[1]) / ls_[1] <= tol * 2):
            nt = [t for t in troughs if ls_[0] < t[0] < rs_[0]]
            if len(nt) >= 2:
                neckline = (nt[0][1] + nt[-1][1]) / 2
                if price <= neckline * (1 + tol):
                    return {"signal": "BEAR", "name": "Head & Shoulders", "strength": 0.90}

    # ── Inverse Head & Shoulders (bullish reversal) ───────────────
    if len(troughs) >= 3:
        ls_, hd_, rs_ = troughs[-3], troughs[-2], troughs[-1]
        if (hd_[1] < ls_[1] and hd_[1] < rs_[1]
                and ls_[1] > 0 and abs(ls_[1] - rs_[1]) / ls_[1] <= tol * 2):
            np_ = [p for p in peaks if ls_[0] < p[0] < rs_[0]]
            if len(np_) >= 2:
                neckline = (np_[0][1] + np_[-1][1]) / 2
                if price >= neckline * (1 - tol):
                    return {"signal": "BULL", "name": "Inv. Head & Shoulders", "strength": 0.90}

    # ── Wedge / Triangle via linear regression on all highs & lows ─
    # Checked before Pennant: a wedge has significant slope throughout,
    # whereas a pennant consolidates flat after a pole.
    flat = 5e-5
    hs_all = _linreg_slope(h) / price   # slope of highs normalised by price
    ls_all = _linreg_slope(l) / price   # slope of lows  normalised by price

    # Rising Wedge (bearish): both upward, lows rising faster → narrowing top
    if hs_all > flat and ls_all > flat and ls_all > hs_all * 1.1:
        return {"signal": "BEAR", "name": "Rising Wedge", "strength": 0.75}

    # Falling Wedge (bullish): both downward, lows falling faster → narrowing bottom
    if hs_all < -flat and ls_all < -flat and ls_all < hs_all * 1.1:
        return {"signal": "BULL", "name": "Falling Wedge", "strength": 0.75}

    # Ascending Triangle: highs flat, lows rising → bullish breakout
    if abs(hs_all) < flat and ls_all > flat * 2:
        return {"signal": "BULL", "name": "Ascending Triangle", "strength": 0.75}

    # Descending Triangle: highs falling, lows flat → bearish breakdown
    if hs_all < -flat * 2 and abs(ls_all) < flat:
        return {"signal": "BEAR", "name": "Descending Triangle", "strength": 0.75}

    # Symmetrical Triangle: highs falling AND lows rising → trade with momentum
    if hs_all < -flat and ls_all > flat:
        sym_sig = "BULL" if c[-1] > c[max(0, len(c) - 10)] else "BEAR"
        return {"signal": sym_sig, "name": "Sym. Triangle", "strength": 0.60}

    # ── Pennant (strong pole → tight horizontal consolidation) ───────────────
    # Only fires when wedge/triangle slopes were not significant above.
    if n > 18:
        pole_st  = c[0]
        pole_en  = c[-8]
        pole_pct = abs(pole_en - pole_st) / max(abs(pole_st), 1e-9)
        if pole_pct >= 0.03:
            rec_h    = max(h[-8:])
            rec_l    = min(l[-8:])
            cons_pct = (rec_h - rec_l) / max(rec_h, 1e-9)
            if cons_pct < pole_pct * 0.40:
                if pole_en > pole_st:
                    return {"signal": "BULL", "name": "Bullish Pennant", "strength": 0.72}
                else:
                    return {"signal": "BEAR", "name": "Bearish Pennant", "strength": 0.72}

    # ── Rectangle / channel (flat highs AND flat lows, no clear wedge) ───────
    if peaks and troughs:
        hp = [p[1] for p in peaks[-3:]]
        ht = [t[1] for t in troughs[-3:]]
        if len(hp) >= 2 and len(ht) >= 2:
            peak_spread   = (max(hp) - min(hp)) / max(hp)
            trough_spread = (max(ht) - min(ht)) / max(ht)
            if peak_spread < tol and trough_spread < tol:
                mid_ch = (sum(hp) / len(hp) + sum(ht) / len(ht)) / 2
                if price > mid_ch:
                    return {"signal": "BULL", "name": "Bullish Rectangle", "strength": 0.65}
                else:
                    return {"signal": "BEAR", "name": "Bearish Rectangle", "strength": 0.65}

    return _NONE


# ── Rank coins ────────────────────────────────────────────────────────────────
def rank_coins():
    scores    = []
    if db.connected:
        db_rates = db.coin_win_rates()
    else:
        # Fall back to in-memory rates computed from saved trades
        _t = _web_trader_ref[0] if _web_trader_ref else None
        db_rates = _t.memory_coin_rates() if _t else {}
        if db_rates:
            log("RANK", f"Using memory learning ({len(db_rates)} coins)")
    for coin in SCAN_UNIVERSE:
        pair = coin["pair"]
        try:
            closes, highs, lows, _, _ = get_klines(pair)
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
                           "alert_buffer": coin["alert_buffer"],
                           "above_ema": above_ema})
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
        # Per-pair win-rate cache for stake sizing
        self._wr_cache     = {}   # pair → {"n": n, "wr": wr}
        self._wr_ts        = 0.0
        # Loss streak global entry cooldown (30-min pause after ≥3 consecutive losses)
        self._streak_cool_until = 0.0
        # Volatility-adaptive sizing: EMA of ATR per pair to detect elevated volatility
        self._base_atr  = {}   # pair → long-run EMA of ATR
        # Kelly Criterion cache (refreshed every 5 min when ≥20 trades available)
        self._kelly_sz  = 0.0
        self._kelly_ts  = 0.0
        self._live_orders  = {}   # pair → kraken txid (live mode only)
        self._load()
        if LIVE_MODE:
            if LIVE_EXCHANGE == "binance":
                real_bal = _binance_get_usdt_balance()
                exch_label = "Binance"
            else:
                real_bal = _kraken_get_usd_balance()
                exch_label = "Kraken"
            if real_bal > 0:
                self.balance       = real_bal
                self.peak          = max(self.peak, real_bal)
                self.day_start_bal = real_bal
                self.session_start = real_bal
                log("LIVE", f"Real USD balance from {exch_label}: ${real_bal:.2f}")

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
            if t["pnl"] <= 0: count += 1
            else: break
        return count

    @property
    def consecutive_wins(self):
        count = 0
        for t in reversed(self.trades):
            if t["pnl"] > 0: count += 1
            else: break
        return count

    def _win_streak_multiplier(self, confidence):
        """Scale up size modestly on win streaks — symmetric with loss-streak reduction.
        Only fires when not already in a loss-streak penalty, and needs conf ≥ 0.70
        so the bot doesn't bet big on weak high-streak signals."""
        if self._risk_multiplier() < 1.0:
            return 1.0   # never layer win bonus on top of loss-streak cut
        wins = self.consecutive_wins
        if wins >= 5 and confidence >= 0.70:
            return 1.15
        if wins >= 3 and confidence >= 0.70:
            return 1.08
        return 1.0

    def _session_multiplier(self):
        """Size by trading session: US hours have the most volume and follow-through;
        Asian session is choppier with more false breakouts."""
        h = datetime.utcnow().hour
        if 13 <= h < 20:   # US session (NY open → close)
            return 1.10
        if 0 <= h < 6:     # Asian session (low liquidity, choppy)
            return 0.75
        return 1.0          # London / EU / other

    def _daily_gain_multiplier(self):
        """Protect a good day: at DAILY_GAIN_SOFT reduce size; caller blocks at HARD."""
        daily_pct = (self.balance - self.day_start_bal) / max(self.day_start_bal, 0.01)
        if daily_pct >= DAILY_GAIN_SOFT:
            return 0.50   # half size — lock in the gains
        return 1.0

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

    def _dynamic_conf_gate(self):
        """Raise confidence gate when recent performance is poor.
        Looks at the last 10 trades; needs ≥5 to activate."""
        recent = self.trades[-10:]
        if len(recent) < 5:
            return 0.0
        wr = sum(1 for t in recent if t["pnl"] > 0) / len(recent)
        losses = self.consecutive_losses
        if wr < 0.30:
            return 0.65   # <30% WR → only very high-confidence entries
        if wr < 0.40:
            return 0.55   # <40% WR → raise bar meaningfully
        if wr < 0.50 and losses >= 3:
            return 0.50   # bad stretch + active streak → moderate caution
        return 0.0

    def _hourly_conf_gate(self):
        """Raise confidence floor during hours of the day that historically lose.
        Uses the last 60 closed trades; needs ≥4 trades in the current hour to activate."""
        cur_hour = datetime.utcnow().hour
        hour_trades = [t for t in self.trades[-60:] if t.get("hour") == cur_hour]
        if len(hour_trades) < 4:
            return 0.0
        wr = sum(1 for t in hour_trades if t["pnl"] > 0) / len(hour_trades)
        if wr < 0.30:
            return 0.70  # this hour historically loses badly → require high confidence
        if wr < 0.40:
            return 0.65  # this hour is below-average → raise the bar moderately
        return 0.0

    # ── Learning method 1: signal-condition fingerprint ──────────────────────
    def _feature_multiplier(self, fkey):
        """Scale stake up/down based on realized win rate of this signal fingerprint.
        Needs ≥5 samples; uses DB when available, in-memory trades otherwise."""
        if self._no_persist or not fkey:
            return 1.0
        now = time.time()
        if now - self._feat_ts > 600:
            try:
                if db.connected:
                    self._feat_cache = db.feature_win_rates()
                else:
                    self._feat_cache = self.memory_feature_rates()
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

    def memory_coin_rates(self, min_trades=3):
        """Per-coin win rates computed from in-memory trades — no DB required.
        Used as fallback when db.connected is False."""
        rates: dict = {}
        for t in self.trades:
            cn = t.get("coin", "")
            if not cn:
                continue
            if cn not in rates:
                rates[cn] = {"wins": 0, "n": 0, "pnl": 0.0}
            rates[cn]["n"] += 1
            rates[cn]["pnl"] += t["pnl"]
            if t["pnl"] > 0:
                rates[cn]["wins"] += 1
        return {
            cn: {"wr": v["wins"] / v["n"] * 100, "n": v["n"], "pnl": round(v["pnl"], 2)}
            for cn, v in rates.items()
            if v["n"] >= min_trades
        }

    def memory_feature_rates(self, min_trades=5):
        """Signal-fingerprint win rates from in-memory trades — no DB required."""
        rates: dict = {}
        for t in self.trades:
            fk = t.get("fkey", "")
            if not fk:
                continue
            if fk not in rates:
                rates[fk] = {"wins": 0, "n": 0}
            rates[fk]["n"] += 1
            if t["pnl"] > 0:
                rates[fk]["wins"] += 1
        return {
            fk: {"wr": v["wins"] / v["n"] * 100, "n": v["n"]}
            for fk, v in rates.items()
            if v["n"] >= min_trades
        }

    @property
    def win_rate(self):
        return (self.wins / len(self.trades) * 100) if self.trades else 0.0

    @property
    def recency_win_rate(self):
        """Win rate weighted by recency — 14-day half-life so recent trades count more."""
        now = time.time()
        half_life = 14 * 86400
        w_wins = sum(math.exp(-(now - t["ts"]) / half_life)
                     for t in self.trades if t["pnl"] > 0)
        w_total = sum(math.exp(-(now - t["ts"]) / half_life) for t in self.trades)
        return (w_wins / w_total * 100) if w_total > 1e-9 else 0.0

    def _kelly_size(self):
        """Half-Kelly optimal risk fraction from realized win/loss stats.
        Requires ≥20 trades; returns 0 when insufficient data."""
        now = time.time()
        if now - self._kelly_ts < 300:
            return self._kelly_sz
        self._kelly_ts = now
        trades = self.trades
        if len(trades) < 20:
            self._kelly_sz = 0.0
            return 0.0
        wins   = [t["pnl"] for t in trades if t["pnl"] > 0]
        losses = [abs(t["pnl"]) for t in trades if t["pnl"] < 0]
        if not wins or not losses:
            self._kelly_sz = 0.0
            return 0.0
        p      = len(wins) / len(trades)
        q      = 1.0 - p
        b      = (sum(wins) / len(wins)) / max(sum(losses) / len(losses), 1e-9)
        kelly  = (p * b - q) / max(b, 1e-9)
        self._kelly_sz = max(0.0, min(kelly * 0.5, RISK_MAX))  # half-Kelly, capped
        return self._kelly_sz

    @property
    def total_pnl(self):
        return round(sum(t["pnl"] for t in self.trades), 2)

    def unrealized_pnl(self, price, pair=None):
        p = self.positions.get(pair) if pair else self.position
        if not p: return 0.0
        move = (price - p["entry"]) / p["entry"]
        if p["side"] == "SHORT": move = -move
        return round(move * p["margin"] * p.get("leverage", LEVERAGE_MIN), 4)

    def on_signal(self, sig, price, stop, target, name, confidence, pair, atr=None, fkey="", pillars=None):
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

            # Breakeven stop: once up BREAKEVEN_PCT, push stop to entry so we never lose
            if not p.get("breakeven_set") and move >= BREAKEVEN_PCT:
                if side == "LONG":
                    if p["trail_stop"] < p["entry"]:
                        p["trail_stop"] = round(p["entry"], 6)
                        tg(f"🔒 *Breakeven — {name}*\nStop moved to entry `${p['entry']:.4f}` (up {move*100:.1f}%)")
                else:
                    if p["trail_stop"] > p["entry"]:
                        p["trail_stop"] = round(p["entry"], 6)
                        tg(f"🔒 *Breakeven — {name}*\nStop moved to entry `${p['entry']:.4f}` (up {move*100:.1f}%)")
                p["breakeven_set"] = True

            # Tier-2 trailing stop: at +10% move lock in ≥7% profit (3% trail from peak)
            if not p.get("tier2_trail_set") and move >= 0.10:
                lock_stop = round(p["entry"] * 1.07 if side == "LONG" else p["entry"] * 0.93, 6)
                tight_atr = round(price * 0.03, 8)  # 3% trail going forward
                if side == "LONG" and lock_stop > p.get("trail_stop", 0):
                    p["trail_stop"]      = lock_stop
                    p["atr_dist"]        = tight_atr
                    p["tier2_trail_set"] = True
                    tg(f"🔐 *+7% Locked — {name}*\nUp `{move*100:.1f}%` → stop `${lock_stop:.4f}` (trail tightened to 3%)")
                elif side == "SHORT" and lock_stop < p.get("trail_stop", float("inf")):
                    p["trail_stop"]      = lock_stop
                    p["atr_dist"]        = tight_atr
                    p["tier2_trail_set"] = True
                    tg(f"🔐 *+7% Locked — {name}*\nUp `{move*100:.1f}%` → stop `${lock_stop:.4f}` (trail tightened to 3%)")

            # Tier-3 trailing stop: at +20% lock in ≥17% profit (1.5% trail from peak)
            if not p.get("tier3_trail_set") and move >= 0.20:
                lock_stop = round(p["entry"] * 1.17 if side == "LONG" else p["entry"] * 0.83, 6)
                tight_atr = round(price * 0.015, 8)  # 1.5% trail — preserve big winners
                if side == "LONG" and lock_stop > p.get("trail_stop", 0):
                    p["trail_stop"]      = lock_stop
                    p["atr_dist"]        = tight_atr
                    p["tier3_trail_set"] = True
                    tg(f"🏆 *+17% Locked — {name}*\nUp `{move*100:.1f}%` → stop `${lock_stop:.4f}` (trail tightened to 1.5%)")
                elif side == "SHORT" and lock_stop < p.get("trail_stop", float("inf")):
                    p["trail_stop"]      = lock_stop
                    p["atr_dist"]        = tight_atr
                    p["tier3_trail_set"] = True
                    tg(f"🏆 *+17% Locked — {name}*\nUp `{move*100:.1f}%` → stop `${lock_stop:.4f}` (trail tightened to 1.5%)")

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
            if time.time() < self._streak_cool_until:
                return   # global pause: ≥3 consecutive losses; wait for market to settle
            # Daily gain hard ceiling — protect the day's profits
            _dgain = (self.balance - self.day_start_bal) / max(self.day_start_bal, 0.01)
            if _dgain >= DAILY_GAIN_HARD:
                with _gate_counter_lock: _gate_counters["daily_gain"] += 1
                return
            min_conf = max(MIN_CONFIDENCE,
                           self._min_conf_threshold(pair),
                           self._dynamic_conf_gate(),
                           self._hourly_conf_gate())
            if sig in ("BUY", "SELL") and confidence < min_conf:
                with _gate_counter_lock: _gate_counters["min_conf"] += 1
                return
            if sig == "BUY"  and self.can_open_new():
                self._open("LONG",  price, name, target, confidence, pair, atr, fkey=fkey, stop=stop, pillars=pillars)
            elif sig == "SELL" and self.can_open_new():
                if LIVE_MODE:
                    log("LIVE", f"SHORT skipped ({name}) — live mode uses spot only")
                else:
                    self._open("SHORT", price, name, target, confidence, pair, atr, fkey=fkey, stop=stop, pillars=pillars)

    def _open(self, side, price, name, target, confidence, pair, atr=None, fkey="", stop=None, pillars=None):
        # Correlation filter: don't pile into the same direction on correlated coins
        for grp in CORRELATED_GROUPS:
            if pair not in grp: continue
            for ep, ep_data in self.positions.items():
                if ep in grp and ep != pair and ep_data["side"] == side:
                    log("PAPER", f"Blocked {name} {side} — correlated with {ep_data['name']}")
                    return

        # Win-rate-based stake multiplier (refreshed every 10 min from DB)
        now_ts = time.time()
        if now_ts - self._wr_ts > 600:
            try:
                self._wr_cache = db.coin_win_rates(min_trades=5)
            except Exception:
                pass
            self._wr_ts = now_ts
        wr_data = self._wr_cache.get(pair)
        wr_mult = 1.0
        if wr_data:
            wr = wr_data["wr"]
            if   wr >= 65: wr_mult = 1.20; log("PAPER", f"{pair} WR {wr:.0f}% → +20% stake")
            elif wr <= 35: wr_mult = 0.60; log("PAPER", f"{pair} WR {wr:.0f}% → -40% stake")
            elif wr <= 45: wr_mult = 0.80; log("PAPER", f"{pair} WR {wr:.0f}% → -20% stake")

        # Kelly Criterion base risk when ≥20 trades available (half-Kelly)
        # Floor at 50% of the linear formula to prevent a jarring drop when Kelly
        # first activates on a marginal strategy (e.g., 55% WR → kelly ≈ 5%).
        linear = RISK_MIN + (RISK_MAX - RISK_MIN) * confidence
        kelly  = self._kelly_size()
        if kelly > 0:
            kelly_base = kelly * (0.5 + 0.5 * confidence)
            base_risk  = max(kelly_base, linear * 0.5)
        else:
            base_risk = linear

        # Volatility-adaptive multiplier: shrink size when current ATR is elevated
        vol_mult = 1.0
        if atr is not None and atr > 0:
            prev_base = self._base_atr.get(pair)
            new_base  = prev_base * 0.85 + atr * 0.15 if prev_base else atr
            self._base_atr[pair] = new_base
            if prev_base and prev_base > 1e-9:
                vol_ratio = atr / new_base
                vol_mult  = max(0.5, min(1.0, 1.0 / max(vol_ratio, 1.0)))

        streak_mult  = self._win_streak_multiplier(confidence)
        session_mult = self._session_multiplier()
        gain_mult    = self._daily_gain_multiplier()
        risk       = min(
                        base_risk
                        * self._risk_multiplier()
                        * self._calibration_multiplier(confidence)
                        * self._feature_multiplier(fkey)
                        * wr_mult
                        * vol_mult
                        * streak_mult
                        * session_mult
                        * gain_mult,
                        RISK_MAX)
        if LIVE_MODE:
            leverage = 1    # spot only — no leverage on live exchanges
            contract_tier = "Spot"
            margin    = round(self.balance * risk, 4)
            fill      = price
            try:
                if LIVE_EXCHANGE == "binance":
                    _oid, contracts, fill = _binance_place_order(pair, "BUY", usdt_amount=margin)
                    self._live_orders[pair] = _oid
                    real_usd = _binance_get_usdt_balance()
                else:
                    contracts = round(margin / fill, 8)
                    txid = _kraken_place_order(pair, "buy", contracts)
                    self._live_orders[pair] = txid
                    real_usd = _kraken_get_usd_balance()
            except Exception as exc:
                log("LIVE", f"Order FAILED for {name}: {exc}", "ERR")
                tg(f"❌ *Live order FAILED — {name}*\n`{exc}`\n_Trade skipped._")
                return
            if real_usd > 0:
                self.balance = real_usd
            fee = round(margin * (BINANCE_FEE if LIVE_EXCHANGE == "binance" else KRAKEN_FEE), 4)
        else:
            # Tiered contracts — leverage and label scale with bot's conviction
            if confidence >= 0.84:
                leverage      = 5
                contract_tier = "Max Bet"
            elif confidence >= 0.76:
                leverage      = 3
                contract_tier = "Confident"
            elif confidence >= 0.68:
                leverage      = 2
                contract_tier = "Moderate"
            else:
                leverage      = 1
                contract_tier = "Cautious"
            fill      = price * (1 + SLIPPAGE) if side == "LONG" else price * (1 - SLIPPAGE)
            margin    = round(self.balance * risk, 4)
            contracts = round((margin * leverage) / fill, 6)
            _sim_fee  = BINANCE_FEE if USE_BINANCE else KRAKEN_FEE
            fee       = round(margin * _sim_fee, 4)
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
                                "contract_tier": contract_tier,
                                "pair": pair, "name": name,
                                "trail_stop": trail_stop, "trail_peak": fill,
                                "atr_dist": atr_dist, "fkey": fkey,
                                "entry_nasdaq": market_mood["nasdaq"],
                                "entry_news": news_sentiment.get(pair, {}).get("sentiment", "NEUTRAL"),
                                "pillars": pillars or {}}
        self._save()
        _push_sse("trade_open", {"name": name, "side": side,
                                  "entry": fill, "pair": pair,
                                  "confidence": int(confidence * 100)})
        mul = self._risk_multiplier()
        dd_note      = f" ⚠️ `{int(mul*100)}%` size (losing streak)" if mul < 1.0 else ""
        vol_note     = f" 🌊 vol×`{vol_mult:.2f}`" if vol_mult < 0.95 else ""
        kelly_note   = " 📐 Kelly" if kelly > 0 else ""
        streak_note  = f" 🔥 streak×`{streak_mult:.2f}`" if streak_mult > 1.0 else ""
        session_note = f" 🇺🇸 US×`{session_mult:.2f}`" if session_mult > 1.0 else \
                       f" 🌙 Asia×`{session_mult:.2f}`" if session_mult < 1.0 else ""
        gain_note    = f" 🔒 gain-protect×`{gain_mult:.2f}`" if gain_mult < 1.0 else ""
        live_note    = f" 🔴 *LIVE ({LIVE_EXCHANGE.upper()})*" if LIVE_MODE else ""
        _tier_emoji  = {"Cautious": "🔵", "Moderate": "🟡", "Confident": "🟠", "Max Bet": "🔴"}.get(contract_tier, "⚪")
        lev_str      = "1x spot" if LIVE_MODE else f"*{leverage}× {contract_tier}* {_tier_emoji}"
        tg(f"📂 *Trade OPENED — {name}*{live_note}\n"
           f"{'🟢 LONG' if side=='LONG' else '🔴 SHORT'} @ `${fill:.4f}` (slip+fee: `${fee:.3f}`)\n"
           f"Confidence: `{conf_pct}%` | Contract: {lev_str}\n"
           f"Margin: `${margin:.2f}` | Size: `{risk*100:.1f}%` of balance{dd_note}{vol_note}{kelly_note}{streak_note}{session_note}{gain_note}\n"
           f"Trail stop: `${trail_stop:.4f}` ({stop_label}) | Balance: `${self.balance:.2f}`")
        _print_trade_box("OPEN", name, side, fill,
                         stop=trail_stop, target=target,
                         confidence=confidence, leverage=leverage,
                         margin=margin, fee=fee,
                         balance=self.balance,
                         stop_type=stop_label)
        _p = self.positions[pair]
        threading.Thread(target=_send_position_chart,
                         args=(pair, _p["entry"], _p["side"],
                               _p.get("trail_stop")),
                         daemon=True).start()

    def _close(self, price, name, reason, pair):
        global _paused
        p = self.positions.get(pair)
        if not p: return

        if LIVE_MODE:
            contracts = p.get("contracts", 0.0)
            try:
                if LIVE_EXCHANGE == "binance":
                    _oid, _qty, fill = _binance_place_order(pair, "SELL", base_qty=contracts)
                    self._live_orders.pop(pair, None)
                    real_usd_after = _binance_get_usdt_balance()
                else:
                    _kraken_place_order(pair, "sell", contracts)
                    self._live_orders.pop(pair, None)
                    fill = price
                    real_usd_after = _kraken_get_usd_balance()
            except Exception as exc:
                exch = LIVE_EXCHANGE.capitalize()
                log("LIVE", f"Close order FAILED for {name}: {exc}", "ERR")
                tg(f"⚠️ *Live close FAILED — {name}*\n`{exc}`\n_Position still open on {exch} — check manually!_")
                return
            pnl  = round(real_usd_after - self.balance, 4) if real_usd_after > 0 else 0.0
            if real_usd_after > 0:
                self.balance = real_usd_after
            fee = round(p.get("margin", 0) * (BINANCE_FEE if LIVE_EXCHANGE == "binance" else KRAKEN_FEE), 4)
        else:
            fill = price * (1 - SLIPPAGE) if p["side"] == "LONG" else price * (1 + SLIPPAGE)
            move = (fill - p["entry"]) / p["entry"]
            if p["side"] == "SHORT": move = -move
            _sim_fee = BINANCE_FEE if USE_BINANCE else KRAKEN_FEE
            fee  = round(p["margin"] * _sim_fee, 4)
            pnl  = round(move * p["margin"] * p.get("leverage", LEVERAGE_MIN) - fee, 4)
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
                     "held_mins": held_mins, "reason": reason, "ts": time.time(),
                     "fkey": p.get("fkey", ""), "hour": datetime.utcnow().hour}
        db.log_feature(fkey, pair, pnl > 0)
        db.log_pillars(p.get("pillars", {}), pnl > 0)
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
        _push_sse("trade_close", {"name": name, "side": p["side"],
                                   "pnl": pnl, "reason": reason,
                                   "balance": self.balance, "win": pnl >= 0})
        # Max session drawdown: auto-pause if down MAX_SESSION_DD from peak
        if self.peak > 0 and (self.peak - self.balance) / self.peak >= MAX_SESSION_DD:
            with _state_lock:
                _paused = True
            dd_pct = (self.peak - self.balance) / self.peak * 100
            tg(f"⚠️ *Max drawdown hit — trading PAUSED*\n"
               f"Balance dropped `{dd_pct:.1f}%` from peak `${self.peak:.2f}`\n"
               f"Current: `${self.balance:.2f}` | Tap ▶ Resume to continue")

        # Cooldown: block re-entry on this pair after a loss
        if pnl < 0:
            cooldown = 1800 if reason in ("trailing stop", "signal flip") else 900
            self._cooldown[pair] = time.time() + cooldown
            log("PAPER", f"{name} cooldown {cooldown//60}m after {reason} loss")
            # Loss streak global cooldown: ≥3 in a row → 30-min pause on all new entries
            if self.consecutive_losses >= 3:
                self._streak_cool_until = time.time() + 1800
                log("PAPER", f"Loss streak {self.consecutive_losses} → 30-min entry pause", "WARN")
            _post_loss_analysis(p, pnl, reason, held_mins)
        else:
            self._streak_cool_until = 0.0   # win resets the streak cooldown
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

        threading.Thread(target=_send_position_chart,
                         args=(pair, p["entry"], p["side"],
                               p.get("trail_stop")),
                         kwargs={"exit_price": fill, "exit_pnl": pnl},
                         daemon=True).start()
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
        self.above_ticks      = 0
        self.below_ticks      = 0
        self._pillar_weights  = {}    # pillar → float multiplier (1.0 = neutral)
        self._pillar_w_ts     = 0.0
        self._hour_win_rates  = {}    # hour (int) → {"n": n, "wr": wr}
        self._hour_w_ts       = 0.0

    def reset(self):
        self.above_ticks = 0
        self.below_ticks = 0

    def evaluate(self, closes, highs, lows, volumes, price, alert_buffer, pair=None, opens=None):
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

        # ADX — Wilder's trend strength (Wilder 1978; Chan "Algorithmic Trading")
        adx = calc_adx(highs, lows, closes)

        # Kaufman Efficiency Ratio — directional quality of recent price movement
        er  = calc_efficiency_ratio(closes)

        # OBV trend — Granville's volume precedes price rule
        obv_trend = calc_obv_trend(closes, volumes)

        # Volume confirmation — require 1.5× average; low-volume breakouts fail often
        avg_vol = sum(volumes) / len(volumes) if volumes else 1
        high_volume = volumes[-1] > avg_vol * VOLUME_FILTER_MULT if avg_vol > 0 else False

        # Candlestick pattern (7th confidence pillar)
        candle_pat = detect_candle_pattern(opens, closes, highs, lows)
        # Classical chart pattern (10th confidence pillar)
        chart_pat  = detect_chart_pattern(closes, highs, lows)
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
                    "exit":  nearest_r if nearest_r else price*1.030,
                    "stop":  nearest_s if nearest_s else price*0.985}
        elif self.below_ticks >= CONFIRM_TICKS and rsi > 30:
            sig  = "SELL"
            plan = {"enter": price,
                    "exit":  nearest_s if nearest_s else price*0.970,
                    "stop":  nearest_r if nearest_r else price*1.015}

        # news gate + news-triggered entry
        current_pair = pair or _current_coin["pair"]
        news    = news_sentiment.get(current_pair, {})
        n_score = news.get("score", 0)
        n_sent  = news.get("sentiment", "NEUTRAL")

        # Block trades that go strongly against news
        if n_score >= 2:
            if sig == "BUY"  and n_sent == "BEARISH":
                with _gate_counter_lock: _gate_counters["news"] += 1
                sig = "HOLD"
            if sig == "SELL" and n_sent == "BULLISH":
                with _gate_counter_lock: _gate_counters["news"] += 1
                sig = "HOLD"

        # MACD gate — block signals that fight the momentum
        if sig == "BUY"  and macd_bear and macd_hist < -0.005 * price / 1000:
            with _gate_counter_lock: _gate_counters["macd"] += 1
            sig = "HOLD"
        if sig == "SELL" and macd_bull and macd_hist >  0.005 * price / 1000:
            with _gate_counter_lock: _gate_counters["macd"] += 1
            sig = "HOLD"

        # News-triggered entry: only fire on exceptional news (score ≥ 5) + MACD alignment.
        # Lower threshold (3) caused losses by bypassing confirm_ticks on borderline signals.
        if sig == "HOLD" and n_score >= 5:
            if n_sent == "BULLISH" and above_ema and rsi < 60 and macd_bull:
                sig  = "BUY"
                plan = {"enter": price,
                        "exit":  nearest_r if nearest_r else price * 1.020,
                        "stop":  nearest_s if nearest_s else price * 0.980}
            elif n_sent == "BEARISH" and not above_ema and rsi > 40 and macd_bear:
                sig  = "SELL"
                plan = {"enter": price,
                        "exit":  nearest_s if nearest_s else price * 0.980,
                        "stop":  nearest_r if nearest_r else price * 1.020}

        # NASDAQ gate
        nasdaq_mood = market_mood["nasdaq"]
        if nasdaq_mood == "BEARISH" and sig == "BUY":
            with _gate_counter_lock: _gate_counters["nasdaq"] += 1
            sig = "HOLD"

        # Fear & Greed gate
        fg_val = fear_greed["value"]
        if fg_val > 80 and sig == "BUY":
            with _gate_counter_lock: _gate_counters["fear_greed"] += 1
            sig = "HOLD"
        if fg_val < 20 and sig == "SELL":
            with _gate_counter_lock: _gate_counters["fear_greed"] += 1
            sig = "HOLD"

        # Bitcoin dominance gate — rising BTC dominance → altcoins bleed
        if btc_dominance["rising"] and sig == "BUY" and current_pair != "XBTUSD":
            with _gate_counter_lock: _gate_counters["btc_dom"] += 1
            sig = "HOLD"

        # BTC price momentum gate — if BTC drops ≥2% in the last 30 min, pause altcoin longs
        if sig == "BUY" and current_pair != "XBTUSD":
            _now_m = time.time()
            _hist_30 = [p for ts, p in _btc_price_hist if ts >= _now_m - 1800]
            if len(_hist_30) >= 3:
                _btc_move = (_hist_30[-1] - _hist_30[0]) / _hist_30[0]
                if _btc_move <= -0.02:
                    with _gate_counter_lock: _gate_counters["btc_momentum"] += 1
                    sig = "HOLD"

        # Funding rate gate — extreme funding = overcrowded side, flush risk
        fr = funding_rates.get(current_pair, 0.0)
        if fr >  FUNDING_THRESHOLD and sig == "BUY":
            with _gate_counter_lock: _gate_counters["funding"] += 1
            sig = "HOLD"
        if fr < -FUNDING_THRESHOLD and sig == "SELL":
            with _gate_counter_lock: _gate_counters["funding"] += 1
            sig = "HOLD"

        # RSI divergence gate — price and RSI disagreeing = unreliable signal
        divergence = detect_divergence(closes, rsi)
        if divergence == "BEARISH_DIV" and sig == "BUY":
            with _gate_counter_lock: _gate_counters["divergence"] += 1
            sig = "HOLD"
        if divergence == "BULLISH_DIV" and sig == "SELL":
            with _gate_counter_lock: _gate_counters["divergence"] += 1
            sig = "HOLD"

        # Market regime — CHOPPY softened: penalise confidence instead of hard-block
        # Hard-blocking CHOPPY eliminated most signals; let the bot trade but smaller
        regime   = detect_regime(closes, highs, lows)
        choppy   = (regime == "CHOPPY" and sig in ("BUY", "SELL"))

        # Time-of-day filter — avoid extreme low-liquidity overnight hours
        if not _in_active_hours() and sig in ("BUY", "SELL"):
            with _gate_counter_lock: _gate_counters["active_hours"] += 1
            sig = "HOLD"

        # Volume gate — low-volume moves fail too often to act on
        if sig in ("BUY", "SELL") and not high_volume:
            with _gate_counter_lock: _gate_counters["volume"] += 1
            sig = "HOLD"

        # VWAP gate — institutions watch this; trading against it means fighting big money
        # Typical price = (H+L+C)/3; VWAP = cumulative(TP×Vol) / cumulative(Vol)
        try:
            _vols = volumes if volumes else []
            _tvol = sum(_vols)
            if _tvol > 0:
                _tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(closes))]
                vwap = sum(_tp[i] * _vols[i] for i in range(len(_tp))) / _tvol
            else:
                vwap = None
        except Exception:
            vwap = None
        above_vwap = price > vwap if vwap else True
        if sig == "BUY"  and vwap and not above_vwap:
            with _gate_counter_lock: _gate_counters["vwap"] += 1
            sig = "HOLD"
        if sig == "SELL" and vwap and above_vwap:
            with _gate_counter_lock: _gate_counters["vwap"] += 1
            sig = "HOLD"

        # Bollinger Band squeeze — bands are narrowest in 20 candles = breakout forming
        # but direction is unknown; wait until price picks a side and bands expand
        if sig in ("BUY", "SELL"):
            try:
                if calc_bb_squeeze(closes):
                    with _gate_counter_lock: _gate_counters["bb_squeeze"] += 1
                    sig = "HOLD"
            except Exception:
                pass

        # ADX gate — Wilder: only enter when a real trend exists (ADX > ADX_MIN)
        # ADX measures trend *strength* without regard to direction
        if sig in ("BUY", "SELL") and adx < ADX_MIN:
            with _gate_counter_lock: _gate_counters["adx"] += 1
            sig = "HOLD"

        # Kaufman Efficiency Ratio gate — skip entries when price is moving randomly
        if sig in ("BUY", "SELL") and er < ER_MIN:
            with _gate_counter_lock: _gate_counters["efficiency"] += 1
            sig = "HOLD"

        # Economic calendar blackout — skip entries around high-impact events
        if sig in ("BUY", "SELL") and _near_econ_event():
            with _gate_counter_lock: _gate_counters["econ"] += 1
            sig = "HOLD"

        # ── Refresh adaptive pillar weights (from DB, cached 10 min) ──────────
        now_ts = time.time()
        if now_ts - self._pillar_w_ts > 600:
            if db.connected:
                try:
                    pw_raw = db.pillar_win_rates()
                    self._pillar_weights = {
                        p: max(0.4, min(1.5, s["wr"] / 50.0))
                        for p, s in pw_raw.items()
                    }
                except Exception:
                    pass
            self._pillar_w_ts = now_ts
        pw = self._pillar_weights

        # ── Confidence score (0.0 → 1.0) ─────────────────────────────────────
        # 6 pillars, each weighted by its historical effectiveness (1.0 = neutral)
        ticks = self.above_ticks if sig == "BUY" else self.below_ticks

        rsi_pts    = 1.0 if 40 <= rsi <= 60 else 0.5 if 30 < rsi < 70 else 0.0
        news_pts   = (1.0 if ((sig == "BUY"  and n_sent == "BULLISH") or
                              (sig == "SELL" and n_sent == "BEARISH"))
                      else 0.5 if n_sent == "NEUTRAL" else 0.0)
        nasdaq_pts = (1.0 if ((sig == "BUY"  and nasdaq_mood == "BULLISH") or
                              (sig == "SELL" and nasdaq_mood == "BEARISH"))
                      else 0.5 if nasdaq_mood == "NEUTRAL" else 0.0)
        tick_pts   = (min(ticks / 5.0, 1.0) if ticks > 0
                      else (min(n_score / 5.0, 1.0) if sig in ("BUY","SELL") and n_score >= 3 else 0.0))
        macd_pts   = 1.0 if (sig == "BUY" and macd_bull) or (sig == "SELL" and macd_bear) else 0.0
        vol_pts    = 1.0 if high_volume else 0.5
        _cp_sig    = candle_pat["signal"]
        candle_pts = (1.0 if (sig == "BUY"  and _cp_sig == "BULL") or
                             (sig == "SELL" and _cp_sig == "BEAR")
                      else 0.0 if (sig == "BUY"  and _cp_sig == "BEAR") or
                                  (sig == "SELL" and _cp_sig == "BULL")
                      else 0.5)   # NONE = neutral
        # VWAP pillar: being on the right side of VWAP boosts confidence;
        # unknown (no volume data) scores neutral
        vwap_pts   = (0.5 if vwap is None else
                      1.0 if ((sig == "BUY"  and above_vwap) or
                              (sig == "SELL" and not above_vwap))
                      else 0.0)

        # OBV pillar (Granville): volume must lead/confirm price direction
        obv_pts = (1.0 if (sig == "BUY"  and obv_trend == "RISING")  or
                          (sig == "SELL" and obv_trend == "FALLING")
                   else 0.5 if obv_trend == "FLAT"
                   else 0.0)

        # Chart-pattern pillar: classical structure detection (10th pillar)
        # Strength scales the point value (0.60–0.90) so stronger patterns weigh more
        _cp_sig = chart_pat["signal"]
        _cp_str = chart_pat.get("strength", 1.0)
        if (sig == "BUY"  and _cp_sig == "BULL") or (sig == "SELL" and _cp_sig == "BEAR"):
            chart_pts = 1.0 * _cp_str          # pattern confirms signal
        elif (sig == "BUY" and _cp_sig == "BEAR") or (sig == "SELL" and _cp_sig == "BULL"):
            chart_pts = 0.0                     # pattern contradicts signal
        else:
            chart_pts = 0.5                     # no pattern = neutral

        pts = (rsi_pts    * pw.get("rsi_zone",       1.0) +
               news_pts   * pw.get("news_align",     1.0) +
               nasdaq_pts * pw.get("nasdaq_align",   1.0) +
               tick_pts   * pw.get("tick_strength",  1.0) +
               macd_pts   * pw.get("macd_align",     1.0) +
               vol_pts    * pw.get("high_volume",    1.0) +
               candle_pts * pw.get("candle_pattern", 1.0) +
               vwap_pts   * pw.get("vwap_align",     1.0) +
               obv_pts    * pw.get("obv_trend",      1.0) +
               chart_pts  * pw.get("chart_struct",   1.0))
        max_pts = (1.0 * pw.get("rsi_zone",       1.0) +
                   1.0 * pw.get("news_align",     1.0) +
                   1.0 * pw.get("nasdaq_align",   1.0) +
                   1.0 * pw.get("tick_strength",  1.0) +
                   1.0 * pw.get("macd_align",     1.0) +
                   1.0 * pw.get("high_volume",    1.0) +
                   1.0 * pw.get("candle_pattern", 1.0) +
                   1.0 * pw.get("vwap_align",     1.0) +
                   1.0 * pw.get("obv_trend",      1.0) +
                   1.0 * pw.get("chart_struct",   1.0))
        confidence = round(pts / max(max_pts, 0.1), 2) if sig in ("BUY", "SELL") else 0.0

        # Choppy-regime confidence penalty (soft gate — trade allowed but smaller)
        # Counter here so it only fires when the signal survived all hard gates
        if choppy:
            with _gate_counter_lock: _gate_counters["choppy"] += 1
            confidence = max(0.0, round(confidence - 0.15, 2))

        # Bonus: Long/Short Ratio squeeze potential (contrarian liquidation boost)
        lsr = lsr_data.get(current_pair, {})
        if sig == "BUY"  and lsr.get("bias") == "SHORT_HEAVY": confidence = min(round(confidence + 0.08, 2), 1.0)
        if sig == "SELL" and lsr.get("bias") == "LONG_HEAVY":  confidence = min(round(confidence + 0.08, 2), 1.0)

        # Bonus: extreme funding = contrarian squeeze setup
        if fr < -EXTREME_FUNDING and sig == "BUY":  confidence = min(round(confidence + 0.06, 2), 1.0)
        if fr >  EXTREME_FUNDING and sig == "SELL": confidence = min(round(confidence + 0.06, 2), 1.0)

        # Open interest conviction check: rising OI confirms real new money;
        # falling OI means moves are just liquidations/unwinding (weaker follow-through)
        oi_info = open_interest_data.get(current_pair, {})
        oi_trend = oi_info.get("trend", "NEUTRAL")
        if sig in ("BUY", "SELL"):
            if oi_trend == "RISING":
                confidence = min(round(confidence + 0.06, 2), 1.0)
            elif oi_trend == "FALLING":
                confidence = max(round(confidence - 0.05, 2), 0.0)

        # Trending regime boosts confidence slightly (cleaner signal)
        if regime == "TRENDING" and sig in ("BUY", "SELL"):
            confidence = min(round(confidence + 0.05, 2), 1.0)

        # Hour-of-day win rate adjustment (refresh hourly from DB)
        now_ts = time.time()
        if now_ts - self._hour_w_ts > 3600:
            if db.connected:
                try:
                    self._hour_win_rates = db.hourly_win_rates()
                except Exception: pass
                self._hour_w_ts = now_ts   # only advance if DB was reachable
        hour_data = self._hour_win_rates.get(datetime.utcnow().hour)
        if hour_data and sig in ("BUY", "SELL"):
            if hour_data["wr"] >= 60:
                confidence = min(round(confidence + 0.05, 2), 1.0)
            elif hour_data["wr"] <= 35:
                confidence = max(round(confidence - 0.10, 2), 0.0)

        # Market breadth: how many scanned coins are trending in the signal direction?
        # Strong agreement = macro move (boost); isolated signal = noise (penalty)
        if sig in ("BUY", "SELL"):
            _mb  = _market_breadth  # single reference load; avoids torn read across two keys
            _ab  = _mb.get("above", 0)
            _tot = max(_mb.get("total", 0), 1)
            if _tot >= 5:
                _bp = _ab / _tot
                if sig == "BUY":
                    if _bp >= 0.70:   # 7+/10 coins above EMA → broad uptrend
                        confidence = min(round(confidence + 0.08, 2), 1.0)
                    elif _bp <= 0.30:  # isolated bid against falling market
                        confidence = max(round(confidence - 0.05, 2), 0.0)
                else:  # SELL
                    if _bp <= 0.30:   # 7+/10 coins below EMA → broad downtrend
                        confidence = min(round(confidence + 0.08, 2), 1.0)
                    elif _bp >= 0.70:  # isolated short into rising market
                        confidence = max(round(confidence - 0.05, 2), 0.0)

        # ── Pillar states for DB adaptive weight logging ─────────────────────
        plan["pillars"] = {
            "rsi_zone":      40 <= rsi <= 60,
            "news_align":    news_pts >= 1.0,
            "nasdaq_align":  nasdaq_pts >= 1.0,
            "tick_strength": ticks > 0,
            "macd_align":    macd_pts >= 1.0,
            "high_volume":   high_volume,
            "candle_pattern":candle_pts >= 1.0,
            "vwap_align":    vwap_pts >= 1.0,
            "obv_trend":     obv_pts >= 1.0,
            "chart_struct":  chart_pts >= 0.7,
        }
        if chart_pat["name"]:
            plan["chart_name"]     = chart_pat["name"]
            plan["chart_signal"]   = chart_pat["signal"]
            plan["chart_strength"] = chart_pat.get("strength", 0.0)
        if candle_pat["name"]:
            plan["candle_name"]   = candle_pat["name"]
            plan["candle_signal"] = candle_pat["signal"]

        # ── Feature fingerprint key ──────────────────────────────────────────
        rsi_bin  = min(int(rsi / 20), 4)
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

# ── Long/Short Ratio monitor (liquidation pressure proxy) ────────────────────
def _lsr_loop():
    while True:
        for pair, symbol in KRAKEN_TO_BINANCE.items():
            try:
                r = requests.get(
                    "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                    params={"symbol": symbol, "period": "5m", "limit": 1}, timeout=6)
                if r.ok:
                    data = r.json()
                    if data:
                        lsr = float(data[0]["longShortRatio"])
                        bias = ("LONG_HEAVY" if lsr > 1.5
                                else "SHORT_HEAVY" if lsr < 0.67
                                else "NEUTRAL")
                        lsr_data[pair] = {"lsr": round(lsr, 3), "bias": bias}
            except Exception:
                pass
            time.sleep(0.3)
        time.sleep(300)  # refresh every 5 minutes

# ── Open Interest monitor (real-money conviction vs. short-covering) ─────────
def _oi_loop():
    global open_interest_data
    while True:
        for pair, symbol in KRAKEN_TO_BINANCE.items():
            try:
                r = requests.get("https://fapi.binance.com/fapi/v1/openInterest",
                                 params={"symbol": symbol}, timeout=5)
                if r.ok:
                    oi = float(r.json()["openInterest"])
                    prev = open_interest_data.get(pair, {}).get("oi", oi)
                    change_pct = (oi - prev) / max(prev, 1e-9) * 100
                    trend = ("RISING"  if change_pct >  0.5
                             else "FALLING" if change_pct < -0.5
                             else "NEUTRAL")
                    open_interest_data[pair] = {"oi": oi, "prev_oi": prev, "trend": trend}
            except Exception:
                pass
            time.sleep(0.2)
        time.sleep(300)   # refresh every 5 minutes

# ── Economic calendar (Forex Factory JSON feed) ───────────────────────────────
def _econ_calendar_loop():
    global _econ_events
    while True:
        try:
            r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                             timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok:
                events = [e for e in r.json()
                          if e.get("impact") == "High"
                          and e.get("country") == "USD"]
                _econ_events = events
                log("ECON", f"Loaded {len(events)} high-impact USD events this week")
        except Exception as e:
            log("ECON", str(e), "ERR")
        time.sleep(3600)  # refresh hourly

def _near_econ_event():
    """Return True if within ECON_BLACKOUT_MINS of a high-impact USD event."""
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)
    for ev in _econ_events:
        try:
            ev_dt = datetime.fromisoformat(ev["date"])
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.replace(tzinfo=_tz.utc)
            diff = abs((ev_dt.astimezone(_tz.utc) - now).total_seconds() / 60)
            if diff <= ECON_BLACKOUT_MINS:
                log("ECON", f"Blackout: {ev.get('title','event')} in {diff:.0f} min")
                return True
        except Exception:
            pass
    return False

# ── Price alert checker ───────────────────────────────────────────────────────
def _alert_loop(trader):
    while True:
        for pair, alerts in list(_price_alerts.items()):
            if not alerts: continue
            try:
                price = get_price(pair)
            except Exception:
                continue
            kept = []
            for a in alerts:
                triggered = (price >= a["target"] if a["above"] else price <= a["target"])
                if triggered:
                    name  = next((c["name"] for c in SCAN_UNIVERSE if c["pair"] == pair), pair)
                    dirn  = "above" if a["above"] else "below"
                    label = f" — {a['label']}" if a.get("label") else ""
                    tg(f"🔔 *Price Alert — {name}*\nNow `${price:.4f}` ({dirn} `${a['target']:.4f}`){label}",
                       plain=True)
                else:
                    kept.append(a)
            _price_alerts[pair] = kept
        time.sleep(30)

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
# Persistent reply keyboard shown after every menu call.
# Tapping a reply-keyboard button sends a plain text message — handled by the
# message branch of _poll_loop — so buttons work even when callback_query
# delivery is broken.
_REPLY_KB = {
    "keyboard": [
        [{"text": "💰 Balance"},  {"text": "📊 Rankings"}],
        [{"text": "📜 History"},  {"text": "📰 News"}],
        [{"text": "🧠 Intel"},    {"text": "🏆 Ranks"},  {"text": "🎓 Learn"}],
        [{"text": "🔄 Switch"},   {"text": "⏸ Pause"},  {"text": "▶ Resume"}],
        [{"text": "🔍 Why"},      {"text": "📈 Chart"}, {"text": "📡 Live"}, {"text": "📋 Menu"}],
        [{"text": "🔬 Backtest"}],
    ],
    "resize_keyboard": True,
}

# Maps incoming text (lower-cased) to the same action strings _dispatch_callback uses.
_TEXT_ACTION: dict = {
    "💰 balance":  "balance",
    "📊 rankings": "rankings",
    "📜 history":  "history",
    "📰 news":     "news",
    "🧠 intel":    "intelligence",
    "🏆 ranks":    "ranks",
    "🎓 learn":    "learn",
    "🔄 switch":   "switch_menu",
    "⏸ pause":     "pause",
    "▶ resume":    "resume",
    "🔍 why":      "why",
    "📈 chart":    "chart",
    "📡 live":     "live",
    "🔬 backtest": "backtest",
    "📋 menu":     "menu",
    # text commands
    "/start":      "menu",
    "/menu":       "menu",
    "/balance":    "balance",
    "/bal":        "balance",
    "/rankings":   "rankings",
    "/history":    "history",
    "/news":       "news",
    "/intel":      "intelligence",
    "/learn":      "learn",
    "/why":        "why",
    "/chart":      "chart",
    "/live":       "live",
    "/backtest":   "backtest",
    "/equity":     "equity",
    "/pause":      "pause",
    "/resume":     "resume",
    "/close":      "force_close",
    "/status":     "balance",
    "/alerts":     "alert_list",
}

def _parse_alert_command(txt):
    """Parse /alert BTC 65000 [above|below] and register it."""
    # Supported: /alert BTC 65000  or  /alert BTC above 65000  or  /alert BTC below 65000
    parts = txt.strip().split()
    # parts[0] = /alert, parts[1] = coin symbol, rest = price / direction
    if len(parts) < 3:
        tg("Usage: /alert BTC 65000  or  /alert BTC above 65000", plain=True)
        return
    sym = parts[1].upper().replace("/", "")
    # Find matching pair
    pair = next((c["pair"] for c in SCAN_UNIVERSE
                 if c["name"].upper().startswith(sym) or c["pair"].startswith(sym)), None)
    if not pair:
        tg(f"Unknown coin: {parts[1]}", plain=True)
        return
    try:
        if parts[2].lower() in ("above", "below"):
            above  = parts[2].lower() == "above"
            target = float(parts[3])
        else:
            target = float(parts[2])
            try:
                current = get_price(pair)
                above = target > current
            except Exception:
                above = True
    except (ValueError, IndexError):
        tg("Usage: /alert BTC 65000  or  /alert BTC above 65000", plain=True)
        return
    _price_alerts.setdefault(pair, []).append({"target": target, "above": above, "label": ""})
    name = next((c["name"] for c in SCAN_UNIVERSE if c["pair"] == pair), pair)
    dirn = "above" if above else "below"
    tg(f"🔔 Alert set: {name} {dirn} ${target:,.4f}", plain=True)

def _decode_fkey(fkey):
    """Translate a signal fingerprint key into human-readable entry reasons."""
    if not fkey: return ""
    parts = []
    if "e1" in fkey: parts.append("above EMA")
    elif "e0" in fkey: parts.append("below EMA")
    if "m1" in fkey: parts.append("MACD bull")
    elif "m0" in fkey: parts.append("MACD bear")
    if "v1" in fkey: parts.append("high vol")
    if "nB" in fkey: parts.append("bullish news")
    elif "nR" in fkey: parts.append("bearish news")
    return ", ".join(parts)

def _post_loss_analysis(position, pnl, reason, held_mins):
    """Brief post-loss note to Telegram — what conditions were at entry."""
    lines = []
    conf  = position.get("confidence", 0.0)
    fkey  = position.get("fkey", "")
    side  = position.get("side", "")
    name  = position.get("name", "?")
    if conf < 0.4:
        lines.append(f"⚠️ Low-confidence entry ({conf:.0%}) — pattern will be de-weighted")
    if held_mins < 3:
        lines.append("⚠️ Very short hold (<3 min) — likely a false breakout")
    if reason == "trailing stop":
        lines.append("📌 Trailing stop fired — stop may be too tight for this pair")
    if fkey:
        lines.append(f"🔑 Pattern `{fkey}` ({_decode_fkey(fkey)}) — added to loss memory")
    if not lines:
        return
    tg(f"📚 *Loss note — {name}*\n"
       f"`{side}` | PnL `{pnl:+.2f}$` | Held `{held_mins:.0f}` min\n"
       + "\n".join(lines))

def _compute_sharpe_sortino(trades):
    """Annualised Sharpe and Sortino ratios from trade PnL as % of PAPER_START.
    Returns (sharpe, sortino) floats, or (None, None) if not enough data."""
    if len(trades) < 10:
        return None, None
    returns = [t["pnl"] / max(PAPER_START, 1) for t in trades]
    mean_r  = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
    std_r   = math.sqrt(variance)
    if std_r < 1e-9:
        return None, None
    annual  = math.sqrt(252)  # approximate trading-day annualisation
    sharpe  = round((mean_r / std_r) * annual, 2)
    # Sortino: denominator uses only downside deviations (but divided by all n)
    down_sq = sum((r ** 2) for r in returns if r < 0)
    sortino_std = math.sqrt(down_sq / max(len(returns), 1))
    sortino = round((mean_r / max(sortino_std, 1e-9)) * annual, 2) if sortino_std > 1e-9 else None
    return sharpe, sortino


def _readiness_score(trader):
    """Compute 0-110 paper-to-live readiness score with breakdown."""
    trades  = trader.trades
    total   = len(trades)
    if total == 0:
        return 0, ["No trades yet — start accumulating history"]
    wins    = sum(1 for t in trades if t["pnl"] > 0)
    wr      = wins / total * 100
    rwr     = trader.recency_win_rate   # recency-weighted win rate

    # Trade volume — max 25 pts (100 trades = full score)
    trade_pts = min(total / 4.0, 25.0)

    # Win rate (recency-weighted) — max 30 pts
    # Cap using simple WR so recent hot streak can't mask a lifetime losing record
    wr_pts = 30 if rwr >= 60 else 20 if rwr >= 55 else 10 if rwr >= 50 else 0
    if wr < 40:
        wr_pts = min(wr_pts, 10)

    # Profit factor — max 20 pts
    gross_win  = sum(t["pnl"] for t in trades if t["pnl"] > 0) or 0
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0)) or 1
    pf         = gross_win / gross_loss
    pf_pts     = 20 if pf >= 2.0 else 15 if pf >= 1.5 else 8 if pf >= 1.2 else 0

    # Sharpe ratio — max 15 pts (replaces plain balance growth in weighting)
    sharpe, sortino = _compute_sharpe_sortino(trades)
    sh_pts = 15 if (sharpe or 0) >= 2.0 else 10 if (sharpe or 0) >= 1.0 else 5 if (sharpe or 0) >= 0.5 else 0

    # Balance growth — max 10 pts (reduced; Sharpe captures quality better)
    growth  = (trader.balance - PAPER_START) / PAPER_START * 100
    g_pts   = 10 if growth >= 50 else 7 if growth >= 20 else 3 if growth >= 5 else 0

    # Learning active — max 10 pts (unchanged)
    l_pts = 0
    feat_count = 0
    if db.connected:
        try:
            feat_count = len(db.feature_win_rates())
            l_pts = min(feat_count * 2, 10)
        except Exception:
            pass

    score     = round(trade_pts + wr_pts + pf_pts + sh_pts + g_pts + l_pts)
    sharpe_str   = f"{sharpe:.2f}" if sharpe is not None else "n/a (<10 trades)"
    sortino_str  = f"{sortino:.2f}" if sortino is not None else "n/a"
    breakdown = [
        f"Trade count: {total}/100  ({trade_pts:.0f}/25 pts)",
        f"Win rate (recent): {rwr:.0f}%  simple: {wr:.0f}%  ({wr_pts}/30 pts — need ≥55%)",
        f"Profit factor: {pf:.1f}x  ({pf_pts}/20 pts — need ≥1.5)",
        f"Sharpe: {sharpe_str}  Sortino: {sortino_str}  ({sh_pts}/15 pts — need ≥1.0)",
        f"Balance growth: {growth:+.0f}%  ({g_pts}/10 pts — need ≥20%)",
        f"Learning patterns: {feat_count}  ({l_pts}/10 pts — need DB + 5 patterns)",
    ]
    return score, breakdown

_CHART_COLORS = {
    "BG_DARK":  "#0d1117",
    "BG_PANEL": "#161b22",
    "GREEN":    "#3fb950",
    "RED":      "#f85149",
    "ACCENT":   "#58a6ff",   # EMA line and RSI line
    "GRID":     "#21262d",
    "TEXT":     "#c9d1d9",
    "MUTED":    "#6e7681",
}

def _make_price_chart(pair, entry=None, entry_side=None,
                      trail_stop=None, exit_price=None, exit_pnl=None):
    """Generate a dark-theme candlestick + RSI chart for a pair.
    Optionally marks an open/closed trade position.
    Returns a BytesIO PNG buffer, or None on failure."""
    try:
        closes, highs, lows, volumes, opens = get_klines(pair, limit=60)
    except Exception as e:
        log("CHART", f"klines failed {pair}: {e}", "ERR")
        return None
    try:
        n   = len(closes)
        x   = list(range(n))
        col = _CHART_COLORS
        price = closes[-1]
        pair_name = next((c["name"] for c in SCAN_UNIVERSE if c["pair"] == pair), pair)

        # ── EMA series ────────────────────────────────────────────────────────
        ema_series = []
        if n >= EMA_PERIOD:
            k   = 2.0 / (EMA_PERIOD + 1)
            ema = sum(closes[:EMA_PERIOD]) / EMA_PERIOD
            ema_series = [None] * EMA_PERIOD
            for c2 in closes[EMA_PERIOD:]:
                ema = c2 * k + ema * (1 - k)
                ema_series.append(ema)

        # ── Rolling RSI series ────────────────────────────────────────────────
        rsi_series = []
        if n >= RSI_PERIOD + 1:
            g_list, l_list = [], []
            for i in range(1, RSI_PERIOD + 1):
                d = closes[i] - closes[i-1]
                g_list.append(max(d, 0)); l_list.append(max(-d, 0))
            ag = sum(g_list) / RSI_PERIOD
            al = sum(l_list) / RSI_PERIOD
            for i in range(RSI_PERIOD, n):
                d  = closes[i] - closes[i-1]
                ag = (ag*(RSI_PERIOD-1) + max(d, 0)) / RSI_PERIOD
                al = (al*(RSI_PERIOD-1) + max(-d, 0)) / RSI_PERIOD
                if al == 0 and ag == 0: rsi_series.append(50.0)
                elif al == 0: rsi_series.append(100.0)
                else: rsi_series.append(round(100 - 100 / (1 + ag / al), 1))

        # ── Figure layout ─────────────────────────────────────────────────────
        has_rsi = len(rsi_series) > 0
        if has_rsi:
            fig, (ax1, ax2) = plt.subplots(
                2, 1, figsize=(10, 6.5),
                gridspec_kw={"height_ratios": [3, 1], "hspace": 0.04},
                sharex=True)
        else:
            fig, ax1 = plt.subplots(figsize=(10, 5.5))
            ax2 = None
        fig.patch.set_facecolor(col["BG_DARK"])

        for ax in ([ax1, ax2] if ax2 else [ax1]):
            ax.set_facecolor(col["BG_PANEL"])
            ax.tick_params(colors=col["MUTED"], labelsize=7, length=0)
            for spine in ax.spines.values():
                spine.set_visible(False)
            ax.grid(color=col["GRID"], linewidth=0.5, linestyle="solid", zorder=0)

        # ── Candlesticks ──────────────────────────────────────────────────────
        body_w = 0.55
        for i in x:
            o, c2, h, l = opens[i], closes[i], highs[i], lows[i]
            is_bull = c2 >= o
            color   = col["GREEN"] if is_bull else col["RED"]
            ax1.plot([i, i], [l, h], color=color, linewidth=0.7, zorder=2,
                     solid_capstyle="round")
            height = max(abs(c2 - o), price * 0.00005)
            ax1.bar(i, height, bottom=min(o, c2), color=color,
                    width=body_w, zorder=3, alpha=0.9, linewidth=0)

        # ── EMA overlay ───────────────────────────────────────────────────────
        if ema_series:
            ex = [i for i, v in enumerate(ema_series) if v is not None]
            ey = [v for v in ema_series if v is not None]
            ax1.plot(ex, ey, color=col["ACCENT"], linewidth=1.5,
                     alpha=0.85, zorder=4)

        # ── Position markers ──────────────────────────────────────────────────
        # Right-side label x coord; ax1 xlim set below to leave this room
        right = n
        ann_kw = dict(annotation_clip=False, fontsize=8, va="center")
        if entry is not None and entry_side:
            ec = col["GREEN"] if entry_side == "LONG" else col["RED"]
            ax1.axhline(entry, color=ec, linewidth=1.0, alpha=0.85, zorder=5)
            ax1.annotate(f"  Entry  ${entry:.4f}", xy=(right, entry),
                         color=ec, fontweight="bold", **ann_kw)
        if trail_stop is not None:
            ax1.axhline(trail_stop, color=col["RED"], linewidth=0.8,
                        linestyle=(0, (3, 4)), alpha=0.70, zorder=5)
            ax1.annotate(f"  Stop  ${trail_stop:.4f}", xy=(right, trail_stop),
                         color=col["RED"], fontsize=7.5, va="center",
                         annotation_clip=False)
        if exit_price is not None:
            ec2 = col["GREEN"] if (exit_pnl or 0) >= 0 else col["RED"]
            ax1.axhline(exit_price, color=ec2, linewidth=1.0, alpha=0.9, zorder=5)
            pnl_str = f"  {exit_pnl:+.2f}$" if exit_pnl is not None else ""
            ax1.annotate(f"  Exit  ${exit_price:.4f}{pnl_str}", xy=(right, exit_price),
                         color=ec2, fontweight="bold", **ann_kw)

        # Shade P&L band between entry and current/exit price
        if entry is not None and entry_side:
            ref = exit_price if exit_price is not None else price
            profitable = (ref > entry and entry_side == "LONG") or \
                         (ref < entry and entry_side == "SHORT")
            ax1.fill_between(x, min(entry, ref), max(entry, ref),
                             alpha=0.08,
                             color=col["GREEN"] if profitable else col["RED"],
                             zorder=1, linewidth=0)

        # Current price label
        ax1.annotate(f"  ${price:.4f}", xy=(right, price),
                     color=col["TEXT"], fontsize=8.5, va="center",
                     annotation_clip=False, fontweight="semibold")
        ax1.set_ylabel(pair_name, color=col["MUTED"], fontsize=8.5,
                       labelpad=6)
        ax1.set_xlim(-0.5, n + 9)

        # ── RSI subplot ───────────────────────────────────────────────────────
        if ax2 and rsi_series:
            rsi_x = list(range(RSI_PERIOD, n))
            ax2.plot(rsi_x, rsi_series, color=col["ACCENT"],
                     linewidth=1.2, zorder=3)
            ax2.fill_between(rsi_x, rsi_series, 50,
                             where=[r >= 50 for r in rsi_series],
                             alpha=0.12, color=col["GREEN"], zorder=2, linewidth=0)
            ax2.fill_between(rsi_x, rsi_series, 50,
                             where=[r < 50 for r in rsi_series],
                             alpha=0.12, color=col["RED"], zorder=2, linewidth=0)
            # Solid threshold lines (not dashed — per mark specs)
            ax2.axhline(70, color=col["RED"],   linewidth=0.6, alpha=0.45, zorder=1)
            ax2.axhline(30, color=col["GREEN"], linewidth=0.6, alpha=0.45, zorder=1)
            ax2.axhline(50, color=col["MUTED"], linewidth=0.4, alpha=0.30, zorder=1)
            cur_rsi = rsi_series[-1]
            rc = col["RED"]   if cur_rsi >= 70 else \
                 col["GREEN"] if cur_rsi <= 30 else col["TEXT"]
            ax2.annotate(f"  {cur_rsi:.0f}", xy=(n, cur_rsi),
                         color=rc, fontsize=7.5, va="center",
                         annotation_clip=False)
            ax2.set_ylim(5, 95)
            ax2.set_yticks([30, 70])
            ax2.set_ylabel("RSI", color=col["MUTED"], fontsize=7.5, labelpad=4)
            ax2.yaxis.set_tick_params(labelsize=6.5, colors=col["MUTED"])
        if ax2:
            ax2.set_xticks([])
        ax1.set_xticks([])

        # ── Title ─────────────────────────────────────────────────────────────
        itvl = f"{INTERVAL}m"
        if entry is not None and entry_side:
            side_icon = "▲ LONG" if entry_side == "LONG" else "▼ SHORT"
            if exit_price is None:
                move = (price - entry) / entry * 100
                if entry_side == "SHORT": move = -move
                sign = "+" if move >= 0 else ""
                title = f"{pair_name} · {itvl}  |  {side_icon} @ ${entry:.4f}  |  {sign}{move:.2f}%"
            else:
                pnl_str = f"{exit_pnl:+.2f}$" if exit_pnl is not None else ""
                ok = "PROFIT" if (exit_pnl or 0) >= 0 else "LOSS"
                title = f"{pair_name} · {itvl}  |  CLOSED · {ok}  {pnl_str}"
        else:
            title = f"{pair_name} · {itvl}  |  ${price:.4f}"

        fig.text(0.012, 0.992, title, color=col["TEXT"], fontsize=9.5,
                 va="top", ha="left", fontweight="semibold")
        fig.subplots_adjust(left=0.07, right=0.78, top=0.93, bottom=0.03)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=140, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        log("CHART", f"_make_price_chart {pair}: {e}", "ERR")
        try: plt.close("all")
        except Exception: pass
        return None

def _send_position_chart(pair, entry, entry_side, trail_stop,
                         exit_price=None, exit_pnl=None):
    """Fire-and-forget: generate and send a price chart for a pair/position.
    All position data passed explicitly to avoid race with position deletion."""
    try:
        buf = _make_price_chart(pair,
                                entry=entry, entry_side=entry_side,
                                trail_stop=trail_stop,
                                exit_price=exit_price, exit_pnl=exit_pnl)
        if buf:
            pair_name = next((c["name"] for c in SCAN_UNIVERSE
                              if c["pair"] == pair), pair)
            if exit_price is None:
                caption = f"📂 *{pair_name}* — position open"
            else:
                ok = "✅" if (exit_pnl or 0) >= 0 else "❌"
                pnl_str = f"{exit_pnl:+.2f}$" if exit_pnl is not None else ""
                caption = f"{ok} *{pair_name}* closed  {pnl_str}"
            tg_photo(buf, caption)
    except Exception as e:
        log("CHART", f"_send_position_chart {pair}: {e}", "ERR")

def _cmd_chart(trader):
    """Handler for /chart — live candlestick chart of current coin or open position."""
    # Prefer the first open position; fall back to the current top coin
    pair = None
    if trader.positions:
        pair = next(iter(trader.positions))
    if not pair:
        pair = _current_coin.get("pair")
    if not pair:
        tg("📊 No coin selected yet — bot hasn't scanned yet. Try again in a moment.")
        return

    pos = trader.positions.get(pair)
    entry      = pos["entry"]           if pos else None
    entry_side = pos["side"]            if pos else None
    trail_stop = pos.get("trail_stop")  if pos else None
    buf = _make_price_chart(pair, entry=entry, entry_side=entry_side,
                            trail_stop=trail_stop)
    if not buf:
        tg("⚠️ Couldn't fetch chart data right now — try again in a moment.")
        return
    pair_name = next((c["name"] for c in SCAN_UNIVERSE if c["pair"] == pair), pair)
    status = "📂 position open" if pos else "📡 scanning"
    tg_photo(buf, f"📊 *{pair_name}* — {status}")

def _cmd_live(trader):
    """Handler for /live — toggle auto chart updates while a position is open."""
    global _live_charts_on
    _live_charts_on = not _live_charts_on
    if _live_charts_on:
        tg(f"📡 *Live charts ON* — sending a fresh chart every `{LIVE_CHART_MINS}` min while a position is open.\nSend again to turn off.")
        _cmd_chart(trader)
    else:
        tg("📡 *Live charts OFF* — auto updates stopped.")

def _backtest_coin(pair, bt_limit=720, bt_interval=5):
    """
    Walk-forward backtest for one coin using the live signal engine.
    Fetches bt_limit candles, slides a CANDLE_LIMIT window, fires on every signal.
    Returns list of trade records {side, entry, exit, pnl_pct, bars, reason}.
    """
    closes, highs, lows, volumes, opens = get_klines(pair, interval=bt_interval, limit=bt_limit)
    if len(closes) < CANDLE_LIMIT + 5:
        return []

    engine   = SignalEngine()
    trades   = []
    position = None
    win      = CANDLE_LIMIT

    for i in range(win, len(closes) - 1):
        c = closes[:i][-win:]
        h = highs[:i][-win:]
        l = lows[:i][-win:]
        v = volumes[:i][-win:]
        o = opens[:i][-win:]
        sig_price = closes[i - 1]     # last close of the evaluation window
        entry_price = closes[i]       # next candle's close — realistic fill

        if position is None:
            try:
                sig, plan, ema, rsi, conf = engine.evaluate(
                    c, h, l, v, sig_price, alert_buffer=0.10, pair=pair, opens=o)
            except Exception:
                continue
            if sig in ("BUY", "SELL") and conf >= MIN_CONFIDENCE:
                price = entry_price
                try:    atr = calc_atr(h, l, c)
                except Exception: atr = None
                stop   = plan.get("stop",   price * (0.985 if sig == "BUY" else 1.015))
                target = plan.get("exit",   price * (1.030 if sig == "BUY" else 0.970))
                atr_d  = (atr * ATR_MULTIPLIER) if atr else abs(price - stop)
                trail  = price - atr_d if sig == "BUY" else price + atr_d
                position = {"side": sig, "entry": price, "target": target,
                            "trail_stop": trail, "atr_dist": atr_d, "bars": 0}
        else:
            p     = position
            side  = p["side"]
            price = closes[i]
            p["bars"] += 1

            # Trail stop ratchet
            if side == "BUY":
                new_t = price - p["atr_dist"]
                if new_t > p["trail_stop"]: p["trail_stop"] = new_t
            else:
                new_t = price + p["atr_dist"]
                if new_t < p["trail_stop"]: p["trail_stop"] = new_t

            reason = None
            if side == "BUY":
                if price <= p["trail_stop"]: reason = "stop"
                elif price >= p["target"]:   reason = "target"
            else:
                if price >= p["trail_stop"]: reason = "stop"
                elif price <= p["target"]:   reason = "target"
            if p["bars"] >= MAX_TRADE_MINS // bt_interval:
                reason = "timeout"

            if reason:
                pnl = ((price - p["entry"]) / p["entry"] if side == "BUY"
                       else (p["entry"] - price) / p["entry"])
                trades.append({"side": side, "entry": p["entry"], "exit": price,
                               "pnl_pct": round(pnl, 4), "bars": p["bars"], "reason": reason})
                position = None

    return trades


def _cmd_backtest(trader):
    """Trigger a walk-forward backtest over recent Kraken OHLCV data (runs in background)."""
    tg("🔬 *Backtesting...* Replaying 60 hours of 5-min candles through the signal engine.\n_Results in ~30–60 seconds._")

    def _run():
        all_trades = []
        per_coin   = {}
        errors     = []

        with _gate_counter_lock:
            _saved_gates = dict(_gate_counters)

        for coin in SCAN_UNIVERSE[:10]:
            try:
                trades = _backtest_coin(coin["pair"])
                per_coin[coin["name"]] = trades
                all_trades.extend(trades)
                log("BT", f"{coin['name']}: {len(trades)} sim trades")
            except Exception as e:
                errors.append(coin["name"])
                log("BT", f"{coin['name']}: {e}", "ERR")

        with _gate_counter_lock:
            _gate_counters.update(_saved_gates)

        if not all_trades:
            tg("⚠️ *Backtest: zero trades generated.*\n"
               "All signals were filtered out by the gates (ADX, ER, confidence floor). "
               "This is correct behaviour — the gates are doing their job. "
               "Run again after more market movement, or send `/backtest` at a more active session.")
            return

        wins   = [t for t in all_trades if t["pnl_pct"] > 0]
        losses = [t for t in all_trades if t["pnl_pct"] <= 0]
        wr     = len(wins) / len(all_trades) * 100
        avg_w  = sum(t["pnl_pct"] for t in wins)  / max(len(wins),  1) * 100
        avg_l  = sum(t["pnl_pct"] for t in losses) / max(len(losses), 1) * 100
        total  = sum(t["pnl_pct"] for t in all_trades) * 100
        best   = max(all_trades, key=lambda t: t["pnl_pct"])
        worst  = min(all_trades, key=lambda t: t["pnl_pct"])
        by_reason = {}
        for t in all_trades:
            by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1

        verdict = "✅ Looks profitable" if total > 0 and wr >= 45 else \
                  "🟡 Marginal — review gate settings" if total > 0 else \
                  "🔴 Losing — signals need more filtering"

        lines = [
            f"🔬 *Backtest Results* _(60h · 5-min candles · {len(SCAN_UNIVERSE[:10])} coins)_",
            "━━━━━━━━━━━━━━━━━━━━",
            f"{verdict}",
            f"Trades:    `{len(all_trades)}` · Win Rate: `{wr:.0f}%`",
            f"Total P&L: `{'+'if total>=0 else ''}{total:.2f}%`",
            f"Avg win:   `+{avg_w:.2f}%` · Avg loss: `{avg_l:.2f}%`",
            f"Best:  `+{best['pnl_pct']*100:.2f}%` ({best['side']})",
            f"Worst: `{worst['pnl_pct']*100:.2f}%` ({worst['side']})",
            f"Exits: " + "  ".join(f"{k} `{v}`" for k, v in sorted(by_reason.items())),
        ]
        if errors:
            lines.append(f"_Skipped: {', '.join(errors)}_")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("*By coin (sorted by P&L):*")

        for name, ct in sorted(per_coin.items(),
                               key=lambda x: sum(t["pnl_pct"] for t in x[1]),
                               reverse=True):
            if not ct: continue
            cwr  = sum(1 for t in ct if t["pnl_pct"] > 0) / len(ct) * 100
            cpnl = sum(t["pnl_pct"] for t in ct) * 100
            icon = "✅" if cpnl >= 0 else "❌"
            lines.append(f"  {icon} *{name}* `{len(ct)}` trades · "
                         f"`{cwr:.0f}%` WR · `{'+'if cpnl>=0 else ''}{cpnl:.1f}%`")

        lines += [
            "━━━━━━━━━━━━━━━━━━━━",
            "_Gates active: ADX, ER, CONFIRM\\_TICKS=3, OBV, VWAP, MACD, RSI_",
            "_4h/1h trend filter not applied — live bot is more conservative_",
        ]
        tg_buttons("\n".join(lines), [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])

    threading.Thread(target=_run, daemon=True).start()


def _cmd_equity(trader):
    """Handler for /equity — account balance equity curve."""
    trades = trader.trades
    if not trades:
        tg("📊 No trade history yet — make some trades first and then check back!")
        return

    balances   = [PAPER_START]
    timestamps = [trades[0]["ts"] - 60]
    for t in trades:
        balances.append(round(balances[-1] + t["pnl"], 2))
        timestamps.append(t["ts"])
    dates = [datetime.utcfromtimestamp(ts) for ts in timestamps]

    wins      = sum(1 for t in trades if t["pnl"] > 0)
    wr        = wins / len(trades) * 100
    total_pnl = trader.balance - PAPER_START
    pnl_sign  = "+" if total_pnl >= 0 else ""
    col = _CHART_COLORS

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor(col["BG_DARK"])
    ax.set_facecolor(col["BG_PANEL"])

    ax.plot(dates, balances, color=col["ACCENT"], linewidth=2, zorder=2)
    ax.fill_between(dates, PAPER_START, balances,
                    where=[b >= PAPER_START for b in balances],
                    alpha=0.20, color=col["GREEN"], interpolate=True)
    ax.fill_between(dates, PAPER_START, balances,
                    where=[b < PAPER_START for b in balances],
                    alpha=0.20, color=col["RED"], interpolate=True)
    for i, t in enumerate(trades):
        ax.scatter(datetime.utcfromtimestamp(t["ts"]), balances[i + 1],
                   color=col["GREEN"] if t["pnl"] > 0 else col["RED"],
                   s=22, zorder=3, edgecolors="none")
    ax.axhline(PAPER_START, color=col["MUTED"], linestyle="--",
               linewidth=0.8, alpha=0.6)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
    fig.autofmt_xdate(rotation=30, ha="right")
    ax.tick_params(colors=col["MUTED"], labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor(col["GRID"])
    ax.grid(color=col["GRID"], linewidth=0.5, zorder=0)
    ax.set_ylabel("Balance ($)", color=col["MUTED"], fontsize=9)
    ax.set_title(f"Balance: ${trader.balance:,.2f}  ({pnl_sign}{total_pnl:.2f}$)"
                 f"   WR {wr:.0f}%   {len(trades)} trades",
                 color=col["TEXT"], fontsize=10, pad=10)

    fig.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    buf.seek(0)
    plt.close(fig)
    tg_photo(buf, f"📈 *Equity Curve* — `{len(trades)}` trades | WR `{wr:.0f}%` | `{pnl_sign}{total_pnl:.2f}$`")

def _cmd_learn(trader):
    """Handler for /learn — learning status + real-money readiness."""
    score, breakdown = _readiness_score(trader)
    dots   = min(int(score / 20), 5)
    bar    = "🟢" * dots + "⚫" * (5 - dots)
    if score >= 80:   verdict = "✅ _Ready — consider risking small real amounts_"
    elif score >= 60: verdict = "🟡 _Getting close — keep paper trading_"
    elif score >= 40: verdict = "🟠 _Progress visible — more history needed_"
    else:             verdict = "🔴 _Not ready — need more trade history_"

    lines = [
        "*🎓 Learning & Real-Money Readiness*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Score: *{score}/110*  {bar}",
        verdict,
        "━━━━━━━━━━━━━━━━━━━━",
        "*Score breakdown:*",
    ]
    for b in breakdown:
        lines.append(f"  • {b}")

    if db.connected:
        feat = {}
        try: feat = db.feature_win_rates()
        except Exception: pass
        if feat:
            lines.append("━━━━━━━━━━━━━━━━━━━━")
            lines.append("*🔑 Signal pattern win rates (min 5 trades):*")
            best = sorted(feat.items(), key=lambda x: -x[1]["wr"])[:4]
            best_keys = {fk for fk, _ in best}
            worst = [x for x in sorted(feat.items(), key=lambda x: x[1]["wr"])
                     if x[0] not in best_keys][:3]
            lines.append("_Best:_")
            for fk, s in best:
                lines.append(f"  ✅ `{fk}` {_decode_fkey(fk)}: `{s['wr']:.0f}%`  n={s['n']}")
            if worst:
                lines.append("_Avoid:_")
                for fk, s in worst:
                    lines.append(f"  ❌ `{fk}` {_decode_fkey(fk)}: `{s['wr']:.0f}%`  n={s['n']}")

        pwr = {}
        try: pwr = db.pillar_win_rates()
        except Exception: pass
        if pwr:
            lines.append("━━━━━━━━━━━━━━━━━━━━")
            lines.append("*📊 Confidence pillar effectiveness:*")
            for pname, s in sorted(pwr.items(), key=lambda x: -x[1]["wr"]):
                icon = "✅" if s["wr"] >= 55 else "⚠️" if s["wr"] >= 45 else "❌"
                lines.append(f"  {icon} {pname}: `{s['wr']:.0f}%` WR  n={s['n']}")
    else:
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("⚫ _Add PostgreSQL on Railway to unlock learning_")

    msg = "\n".join(lines)
    if len(msg) > 3800:
        msg = msg[:3800].rsplit("\n", 1)[0] + "\n_...truncated_"
    tg_buttons(msg, [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])

def _cmd_why():
    """Handler for /why — show which gates are currently blocking + today's counts."""
    with _gate_counter_lock:
        counts = dict(_gate_counters)
    total_blocked = sum(counts.values())
    lines = [
        "*🔍 Gate Telemetry — Today*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Total signals intercepted: `{total_blocked}`",
        "",
    ]
    gate_labels = {
        "volume":       "📉 Volume too low",
        "choppy":       "〰️ Choppy regime (softened)",
        "4h_trend":     "📊 4-hour trend filter",
        "1h_trend":     "📊 1-hour trend filter",
        "pullback":     "⏳ Waiting for pullback",
        "momentum":     "🚀 Counter-momentum entry",
        "nasdaq":       "🏦 Bearish NASDAQ",
        "funding":      "💸 Extreme funding rate",
        "divergence":   "⚡ RSI divergence",
        "active_hours": "🌙 Outside trading hours",
        "econ":         "📅 Economic event blackout",
        "fear_greed":   "😱 Extreme fear/greed",
        "btc_dom":      "₿ BTC dominance rising",
        "news":         "📰 News opposing signal",
        "macd":         "📈 MACD fighting signal",
        "adx":          "📉 Weak trend (ADX < 20)",
        "efficiency":   "〰️ Random walk (Kaufman ER)",
        "min_conf":     "🔒 Below confidence floor",
    }
    rows = [(gate_labels.get(k, k), v) for k, v in sorted(counts.items(), key=lambda x: -x[1]) if v > 0]
    if rows:
        for label, n in rows:
            bar = "█" * min(n, 10) + "░" * max(0, 10 - n)
            lines.append(f"`{bar}` {n:>3}  {label}")
    else:
        lines.append("No gates have fired today yet.")
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "_Counts reset at midnight UTC_",
        "_Choppy is softened — lowers confidence, not blocked_",
    ]
    tg_buttons("\n".join(lines), [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])

def _tg_with_kb(msg, kb):
    """Send a message with a ReplyKeyboardMarkup."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    for parse_mode in ("Markdown", None):
        payload = {"chat_id": TG_CHAT_ID, "text": msg, "reply_markup": kb}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              json=payload, timeout=10)
            if r.ok:
                return
            log("TG", f"tg_with_kb {r.status_code}: {r.text[:200]}", "ERR")
        except Exception as e:
            log("TG", f"tg_with_kb error: {e}", "ERR")

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
    pos_close = ""
    if trader and trader.positions:
        n = len(trader.positions)
        pos_close = f"\n🚨 *{n} open position{'s' if n > 1 else ''}* — tap 💰 Balance to close"
    _tg_with_kb(header + pos_close, _REPLY_KB)

def _handle_callback(query, trader):
    global _paused, _current_coin
    cb_id = query.get("id")
    if cb_id:
        tg_answer(cb_id, "")
    data = query.get("data", "")
    try:
        _dispatch_callback(data, query, trader)
    except Exception as e:
        log("CALLBACK", f"{data!r} error: {e}", "ERR")
        tg(f"Error: {e}", plain=True)

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
            icon   = "✅" if t["pnl"] >= 0 else "❌"
            side   = "🟢 L" if t["side"] == "LONG" else "🔴 S"
            reason = _decode_fkey(t.get("fkey", ""))
            reason_str = f"\n   _{reason}_" if reason else ""
            lines.append(
                f"{icon} {side} *{t.get('coin','?')}*  "
                f"`{'+'if t['pnl']>=0 else ''}{t['pnl']:.2f}$`  "
                f"_{t.get('reason','?')}_"
                f"{reason_str}"
            )
        total_pnl = trader.total_pnl
        lines.append("━━━━━━━━━━━━━━━━━━━━")
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
            # Append best-hours analysis
            if trader.trades:
                hour_stats: dict = {}
                for t in trader.trades:
                    h = t.get("hour", datetime.utcfromtimestamp(t.get("ts", 0)).hour)
                    hs = hour_stats.setdefault(h, {"w": 0, "l": 0, "pnl": 0.0})
                    if t["pnl"] > 0: hs["w"] += 1
                    else:            hs["l"] += 1
                    hs["pnl"] += t["pnl"]
                ranked = sorted(hour_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)
                best3  = [(h, s) for h, s in ranked if s["w"] + s["l"] >= 2][:3]
                worst3 = [(h, s) for h, s in reversed(ranked) if s["w"] + s["l"] >= 2][:3]
                if best3 or worst3:
                    report += "\n\n*⏰ Best Trading Hours (UTC)*\n"
                    for h, s in best3:
                        tot = s["w"] + s["l"] or 1
                        report += f"  ✅ {h:02d}:00  W{s['w']}/L{s['l']}  `{s['pnl']:+.2f}$`  `{s['w']/tot*100:.0f}%`\n"
                    if worst3:
                        report += "*⏰ Worst Hours*\n"
                        for h, s in worst3:
                            tot = s["w"] + s["l"] or 1
                            report += f"  ❌ {h:02d}:00  W{s['w']}/L{s['l']}  `{s['pnl']:+.2f}$`  `{s['w']/tot*100:.0f}%`\n"
            # Append top LSR readings
            hot_lsr = [(p, d) for p, d in lsr_data.items() if d["bias"] != "NEUTRAL"]
            if hot_lsr:
                report += "\n*📊 L/S Ratio Pressure*\n"
                for p, d in sorted(hot_lsr, key=lambda x: abs(x[1]["lsr"] - 1), reverse=True)[:4]:
                    name = next((c["name"] for c in SCAN_UNIVERSE if c["pair"] == p), p)
                    icon = "🐂" if d["bias"] == "SHORT_HEAVY" else "🐻"
                    report += f"  {icon} {name}: `{d['lsr']:.2f}` ({d['bias']})\n"
            if len(report) > 3800:
                report = report[:3800].rsplit("\n", 1)[0] + "\n_...truncated_"
        except Exception as e:
            report = f"⚠️ Intelligence error: {e}"
        tg_buttons(report, [[{"text": "🔙 Back to Menu", "callback_data": "menu"}]])

    elif data.startswith("alert_"):
        # alert_set_XBTUSD_65000_above  or  alert_del_XBTUSD
        parts = data.split("_")
        if parts[1] == "del" and len(parts) >= 3:
            _price_alerts.pop(parts[2], None)
            tg_buttons("🔕 Alert cleared.", [[{"text": "🔙 Back", "callback_data": "menu"}]])
        elif parts[1] == "list":
            if not any(_price_alerts.values()):
                tg_buttons("🔕 No active alerts.", [[{"text": "🔙 Back", "callback_data": "menu"}]])
            else:
                lines = ["*🔔 Active Price Alerts*"]
                for pair, alerts in _price_alerts.items():
                    name = next((c["name"] for c in SCAN_UNIVERSE if c["pair"] == pair), pair)
                    for a in alerts:
                        dirn = "above" if a["above"] else "below"
                        lines.append(f"  {name} {dirn} `${a['target']:.4f}`")
                tg_buttons("\n".join(lines), [[{"text": "🔙 Back", "callback_data": "menu"}]])

    elif data == "learn":
        _cmd_learn(trader)

    elif data == "why":
        _cmd_why()

    elif data == "chart":
        _cmd_chart(trader)

    elif data == "live":
        _cmd_live(trader)

    elif data == "backtest":
        _cmd_backtest(trader)

    elif data == "equity":
        _cmd_equity(trader)

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

def _weekly_summary(trader):
    cutoff = time.time() - 7 * 86400
    week   = [t for t in trader.trades if t.get("ts", 0) >= cutoff]
    rank   = get_rank(trader.balance)
    if not week:
        tg(f"📆 *Weekly Summary*\nNo trades this week.\nBalance: `${trader.balance:.2f}`")
        return
    wins      = [t for t in week if t["pnl"] > 0]
    losses    = [t for t in week if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in week)
    best      = max(week, key=lambda t: t["pnl"])
    worst     = min(week, key=lambda t: t["pnl"])
    wr        = len(wins) / len(week) * 100
    # Best coin this week
    coin_pnl: dict = {}
    for t in week:
        coin_pnl[t.get("coin", "?")] = coin_pnl.get(t.get("coin", "?"), 0) + t["pnl"]
    top_coin  = max(coin_pnl, key=lambda k: coin_pnl[k])
    bot_coin  = min(coin_pnl, key=lambda k: coin_pnl[k])
    tg(f"📆 *Weekly Summary*\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"Trades: `{len(week)}` | W: `{len(wins)}` L: `{len(losses)}`\n"
       f"Win Rate: `{wr:.0f}%` | PnL: `{'+'if total_pnl>=0 else ''}{total_pnl:.2f}$`\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"Best trade:  `+{best['pnl']:.2f}$` ({best.get('coin','?')})\n"
       f"Worst trade: `{worst['pnl']:.2f}$` ({worst.get('coin','?')})\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"Top coin: *{top_coin}* `{coin_pnl[top_coin]:+.2f}$`\n"
       f"Worst coin: *{bot_coin}* `{coin_pnl[bot_coin]:+.2f}$`\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"Balance: `${trader.balance:.2f}` | {rank['emoji']} {rank['name']}")

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
            else:
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
            # Gate telemetry daily report + counter reset
            with _gate_counter_lock:
                gate_snap = dict(_gate_counters)
                for k in _gate_counters:
                    _gate_counters[k] = 0
            total_blocked = sum(gate_snap.values())
            if total_blocked > 0:
                gate_labels = {
                    "volume": "Volume", "choppy": "Choppy(soft)",
                    "4h_trend": "4H trend", "1h_trend": "1H trend",
                    "pullback": "Pullback wait", "momentum": "Counter-mom",
                    "nasdaq": "NASDAQ", "funding": "Funding",
                    "divergence": "RSI div", "active_hours": "Off-hours",
                    "econ": "Econ event", "fear_greed": "Fear/Greed",
                    "btc_dom": "BTC dom", "news": "News", "macd": "MACD",
                    "rr_ratio": "Bad R:R", "vwap": "VWAP side",
                    "spike": "Candle spike", "daily_gain": "Day profit cap",
                    "adx": "ADX weak", "efficiency": "ER random", "min_conf": "Low conf",
                }
                top_gates = sorted(gate_snap.items(), key=lambda x: -x[1])[:5]
                gate_lines = "  ".join(f"{gate_labels.get(k, k)}: `{v}`" for k, v in top_gates if v > 0)
                tg(f"🔒 *Gate Report (last 24h)*\nTotal intercepted: `{total_blocked}`\n{gate_lines}")

            # Weekly summary every Sunday (weekday 6)
            if now.weekday() == 6:
                _weekly_summary(trader)
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
    log("POLL", f"Starting Telegram poll loop  token_len={len(TG_TOKEN)}  chat_id={TG_CHAT_ID!r}")
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                params={"offset": _last_update_id + 1, "timeout": 10},
                timeout=15)
            if not r.ok:
                log("POLL", f"getUpdates error {r.status_code}: {r.text[:200]}", "ERR")
                time.sleep(5)
                continue
            updates = r.json().get("result", [])
            for u in updates:
                _last_update_id = u["update_id"]
                if "callback_query" in u:
                    cb      = u["callback_query"]
                    user_id = str(cb.get("from", {}).get("id", ""))
                    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                    btn     = cb.get("data", "?")
                    log("POLL", f"Callback: data={btn!r} user={user_id} chat={chat_id}")
                    # No ID filter — single-user personal bot; process all callbacks.
                    threading.Thread(target=_handle_callback,
                                     args=(cb, trader), daemon=True).start()
                elif "message" in u:
                    chat_id = str(u["message"].get("chat", {}).get("id", ""))
                    user_id = str(u["message"].get("from", {}).get("id", ""))
                    txt     = u["message"].get("text", "").strip()
                    # Route text/commands to the same dispatch as inline buttons.
                    action = _TEXT_ACTION.get(txt.lower())
                    if action:
                        log("POLL", f"Text → {action!r} ({txt!r})")
                        threading.Thread(target=_handle_callback,
                                         args=({"data": action}, trader),
                                         daemon=True).start()
                    elif txt.lower().startswith("/alert "):
                        threading.Thread(target=_parse_alert_command,
                                         args=(txt,), daemon=True).start()
                    elif txt.lower() in ("menu", "/start", "/menu"):
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
                    # Update market breadth for confidence scoring
                    _market_breadth = {
                        "above": sum(1 for s in ranked_list if s.get("above_ema")),
                        "total": len(ranked_list),
                    }
                except Exception as e:
                    log("TRADE", f"rank_coins: {e}", "ERR")

            # Track BTC price for the momentum gate (one call per tick, ~60 s)
            try:
                _btc_tick = get_price("XBTUSD")
                _btc_price_hist.append((now_ts, _btc_tick))
                _btc_cutoff = now_ts - _BTC_HIST_SECS
                while _btc_price_hist and _btc_price_hist[0][0] < _btc_cutoff:
                    _btc_price_hist.pop(0)
            except Exception:
                pass

            # ── Manage every open position every tick ──────────────────────
            for pair in list(trader.positions.keys()):
                p = trader.positions.get(pair)
                if not p: continue
                try:
                    closes, highs, lows, volumes, opens_m = get_klines(pair)
                    price = get_price(pair)
                    try: atr = calc_atr(highs, lows, closes)
                    except Exception: atr = None

                    # ── Parabolic move: tighten trail before the reversal ──
                    if not p.get("parabolic_tightened"):
                        try:
                            _rsi_live = calc_rsi(closes)
                            _avg_vol  = sum(volumes) / len(volumes) if volumes else 1
                            _vol_spike = volumes[-1] > _avg_vol * 3
                            _bull3 = all(closes[-i] > closes[-i-1] for i in range(1, 4))
                            _bear3 = all(closes[-i] < closes[-i-1] for i in range(1, 4))
                            _move = (price - p["entry"]) / p["entry"]
                            if p["side"] == "SHORT": _move = -_move
                            if _move > 0.02:  # only tighten if already in profit
                                if ((p["side"] == "LONG"  and _rsi_live > 82 and _vol_spike and _bull3) or
                                    (p["side"] == "SHORT" and _rsi_live < 18 and _vol_spike and _bear3)):
                                    _tight = round(price * 0.010, 8)
                                    if _tight < p.get("atr_dist", float("inf")):
                                        p["atr_dist"] = _tight
                                        p["parabolic_tightened"] = True
                                        tg(f"🌋 *Parabolic — {p['name']}*\n"
                                           f"RSI `{_rsi_live:.0f}` + vol spike + 3-candle run\n"
                                           f"Trail tightened to `1%` before reversal")
                        except Exception: pass

                    # ── Divergence exit: RSI/price disagreement while in trade ──
                    if not p.get("div_tightened"):
                        try:
                            _rsi_live = calc_rsi(closes)
                            _div = detect_divergence(closes, _rsi_live)
                            _move = (price - p["entry"]) / p["entry"]
                            if p["side"] == "SHORT": _move = -_move
                            if _move > 0.01:  # only tighten if in profit
                                if _div == "BEARISH_DIV" and p["side"] == "LONG":
                                    _tight = round(atr * 1.0, 8) if atr else round(price * 0.015, 8)
                                    if _tight < p.get("atr_dist", float("inf")):
                                        p["atr_dist"] = _tight
                                        p["div_tightened"] = True
                                        tg(f"⚠️ *Bearish Divergence — {p['name']}*\n"
                                           f"Price making highs, RSI declining\nTrail tightened to `1×ATR`")
                                elif _div == "BULLISH_DIV" and p["side"] == "SHORT":
                                    _tight = round(atr * 1.0, 8) if atr else round(price * 0.015, 8)
                                    if _tight < p.get("atr_dist", float("inf")):
                                        p["atr_dist"] = _tight
                                        p["div_tightened"] = True
                                        tg(f"⚠️ *Bullish Divergence — {p['name']}*\n"
                                           f"Price making lows, RSI rising\nTrail tightened to `1×ATR`")
                        except Exception: pass

                    # ── Live chart auto-update ──────────────────────────────
                    if _live_charts_on:
                        try:
                            _now = time.time()
                            if _now - p.get("_last_chart_ts", 0) >= LIVE_CHART_MINS * 60:
                                p["_last_chart_ts"] = _now
                                _buf = _make_price_chart(
                                    pair,
                                    entry=p["entry"],
                                    entry_side=p["side"],
                                    trail_stop=p.get("trail_stop"),
                                )
                                if _buf:
                                    _upnl = trader.unrealized_pnl(price, pair)
                                    _sign = "+" if _upnl >= 0 else ""
                                    tg_photo(_buf,
                                        f"📡 *Live — {p['name']}*  "
                                        f"`{_sign}{_upnl:.2f}$`  "
                                        f"{'🟢' if _upnl >= 0 else '🔴'}")
                        except Exception: pass

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
                    closes, highs, lows, volumes, opens = get_klines(pair)
                    price = get_price(pair)
                    if pair not in engine_map:
                        engine_map[pair] = SignalEngine()
                    eng = engine_map[pair]
                    sig, plan, ema, rsi, conf = eng.evaluate(
                        closes, highs, lows, volumes, price, coin["alert_buffer"],
                        pair=pair, opens=opens)
                    sig_from_eval = sig   # capture before gate filters may change it
                    try: atr = calc_atr(highs, lows, closes)
                    except Exception: atr = None

                    # ── Pattern cache refresh ─────────────────────────────────
                    _now_p  = time.time()
                    _prev_p = _pattern_cache.get(pair, {})
                    # Chart structure: re-detect every 15 min (changes with candle close)
                    if _now_p - _prev_p.get("ts", 0) >= _PATTERN_TTL:
                        try:
                            _cp = detect_chart_pattern(closes, highs, lows)
                            _pattern_cache[pair] = {
                                "name":     _cp["name"],
                                "signal":   _cp["signal"],
                                "strength": round(_cp.get("strength", 0.0), 2),
                                "coin":     coin["name"],
                                "ts":       _now_p,
                                "candle_name":   "",
                                "candle_signal": "NONE",
                            }
                        except Exception:
                            pass
                    # Candle pattern: update every scan (cheap, uses last 4 bars)
                    try:
                        _cdp = detect_candle_pattern(opens, closes, highs, lows)
                        if pair in _pattern_cache:
                            _pattern_cache[pair]["candle_name"]   = _cdp["name"]
                            _pattern_cache[pair]["candle_signal"] = _cdp["signal"]
                        elif _cdp["name"]:
                            _pattern_cache[pair] = {
                                "name": "", "signal": "NONE", "strength": 0.0,
                                "coin": coin["name"], "ts": 0,
                                "candle_name":   _cdp["name"],
                                "candle_signal": _cdp["signal"],
                            }
                    except Exception:
                        pass

                    # 4-hour trend confirmation (higher-timeframe confluence)
                    # Cached for _HTF_CACHE_TTL seconds to avoid extra API calls every tick.
                    if sig in ("BUY", "SELL"):
                        _now_ts = time.time()
                        _k4 = (pair, 240)
                        _cached4 = _htf_cache.get(_k4)
                        if _cached4 and _now_ts < _cached4[1]:
                            trend_4h = _cached4[0]
                        else:
                            try:
                                closes_4h, _, _, _, _ = get_klines(pair, interval=240, limit=16)
                                ema_4h   = calc_ema(closes_4h)
                                trend_4h = "UP" if closes_4h[-1] > ema_4h else "DOWN"
                                _htf_cache[_k4] = (trend_4h, _now_ts + _HTF_CACHE_TTL)
                            except Exception:
                                trend_4h = _cached4[0] if _cached4 else None
                        if trend_4h:
                            if sig == "BUY"  and trend_4h != "UP":
                                with _gate_counter_lock: _gate_counters["4h_trend"] += 1
                                sig = "HOLD"
                            elif sig == "SELL" and trend_4h != "DOWN":
                                with _gate_counter_lock: _gate_counters["4h_trend"] += 1
                                sig = "HOLD"

                    # 1-hour trend confirmation
                    if sig in ("BUY", "SELL"):
                        _now_ts = time.time()
                        _k1 = (pair, 60)
                        _cached1 = _htf_cache.get(_k1)
                        if _cached1 and _now_ts < _cached1[1]:
                            trend_1h = _cached1[0]
                        else:
                            try:
                                closes_1h, _, _, _, _ = get_klines(pair, interval=60, limit=24)
                                ema_1h   = calc_ema(closes_1h)
                                trend_1h = "UP" if closes_1h[-1] > ema_1h else "DOWN"
                                _htf_cache[_k1] = (trend_1h, _now_ts + _HTF_CACHE_TTL)
                            except Exception:
                                trend_1h = _cached1[0] if _cached1 else None
                        if trend_1h:
                            if sig == "BUY"  and trend_1h != "UP":
                                with _gate_counter_lock: _gate_counters["1h_trend"] += 1
                                sig = "HOLD"
                            elif sig == "SELL" and trend_1h != "DOWN":
                                with _gate_counter_lock: _gate_counters["1h_trend"] += 1
                                sig = "HOLD"

                    # Regime-adaptive pullback filter: in a TRENDING market, wait for
                    # a 1-candle pullback to get a better entry price
                    if sig in ("BUY", "SELL") and len(closes) >= 2:
                        try:
                            local_regime = detect_regime(closes, highs, lows)
                            if local_regime == "TRENDING":
                                if sig == "BUY"  and closes[-1] >= closes[-2]:
                                    with _gate_counter_lock: _gate_counters["pullback"] += 1
                                    sig = "HOLD"
                                elif sig == "SELL" and closes[-1] <= closes[-2]:
                                    with _gate_counter_lock: _gate_counters["pullback"] += 1
                                    sig = "HOLD"
                        except Exception: pass

                    # Coin momentum filter: both 5-candle and 15-candle momentum must
                    # not both oppose the signal (prevents fighting sustained counter-moves)
                    if sig in ("BUY", "SELL") and len(closes) >= 16:
                        try:
                            mom5  = closes[-1] - closes[-6]
                            mom15 = closes[-1] - closes[-16]
                            if sig == "BUY"  and mom5 <= 0 and mom15 <= 0:
                                with _gate_counter_lock: _gate_counters["momentum"] += 1
                                sig = "HOLD"
                            elif sig == "SELL" and mom5 >= 0 and mom15 >= 0:
                                with _gate_counter_lock: _gate_counters["momentum"] += 1
                                sig = "HOLD"
                        except Exception: pass

                    # Spike/chase filter: if current candle body > 2× ATR the move is
                    # already exhausted — don't enter at the top of a spike
                    if sig in ("BUY", "SELL") and atr and atr > 0 and opens:
                        try:
                            body = abs(closes[-1] - opens[-1])
                            if body > 2 * atr:
                                bull_spike = closes[-1] > opens[-1] and sig == "BUY"
                                bear_spike = closes[-1] < opens[-1] and sig == "SELL"
                                if bull_spike or bear_spike:
                                    with _gate_counter_lock: _gate_counters["spike"] += 1
                                    sig = "HOLD"
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
                        stop     = plan.get("stop",    price*0.985 if sig=="BUY" else price*1.015)
                        target   = plan.get("exit",    price*1.030 if sig=="BUY" else price*0.970)
                        fkey     = plan.get("fkey",    "")
                        pillars  = plan.get("pillars", {})

                        # R:R gate: skip entry when reward doesn't justify the risk
                        _rr_reward = (target - price) if sig == "BUY" else (price - target)
                        _rr_risk   = (price - stop)   if sig == "BUY" else (stop - price)
                        if _rr_risk <= 0 or _rr_reward / _rr_risk < MIN_RR_RATIO:
                            with _gate_counter_lock: _gate_counters["rr_ratio"] += 1
                            last_sigs[pair] = sig
                            continue

                        # Order book imbalance gate — only enter when real money lines up
                        # with the signal; skips false breakouts where big sellers wait above
                        try:
                            _ob = _get_ob_imbalance(pair)
                            if _ob is not None:
                                _ob_block = (sig == "BUY"  and _ob < 0.80) or \
                                            (sig == "SELL" and _ob > 1.25)
                                if _ob_block:
                                    with _gate_counter_lock: _gate_counters["ob_imbalance"] += 1
                                    last_sigs[pair] = sig
                                    log("GATE", f"{cname} OB imbalance {_ob:.2f} → blocked {sig}")
                                    continue
                        except Exception:
                            pass

                        risk     = RISK_MIN + (RISK_MAX - RISK_MIN) * conf
                        leverage = round(LEVERAGE_MIN + (LEVERAGE_MAX - LEVERAGE_MIN) * conf)
                        arrow    = "↑" if sig == "BUY" else "↓"
                        conf_pct = f"{int(conf*100)}%"
                        _rr_str  = f"{_rr_reward / _rr_risk:.1f}"
                        log("SIGNAL", (f"{_c(sig_col, f'{arrow} {sig} {cname}')}  "
                                       f"${price:.4f}  target ${target:.4f}  stop ${stop:.4f}  "
                                       f"R:R {_rr_str}  "
                                       f"conf {_c(conf_col, conf_pct)}  {leverage}x"))
                        emoji    = "🟢" if sig == "BUY" else "🔴"
                        _cpn  = plan.get("chart_name", "")
                        _cdn  = plan.get("candle_name", "")
                        _pat_note = ""
                        if _cpn: _pat_note += f"\n📐 Chart: `{_cpn}`"
                        if _cdn: _pat_note += f"\n🕯️ Candle: `{_cdn}`"
                        tg(f"{emoji} *{sig} Signal — {coin['name']}*\n"
                           f"Enter: `${plan['enter']:.4f}` | Exit: `${target:.4f}` | Stop: `${stop:.4f}`\n"
                           f"R:R: `{_rr_str}:1` | EMA: `{ema:.2f}` | RSI: `{rsi}` | Conf: `{int(conf*100)}%`\n"
                           f"Size: `{risk*100:.1f}%` | Leverage: *{leverage}x*{_pat_note}")
                        trader.on_signal(sig, price, stop, target, coin["name"], conf, pair,
                                         atr=atr, fkey=fkey, pillars=pillars)

                    last_sigs[pair] = sig

                    # ── Activity log entry ────────────────────────────────
                    _act_pillars = plan.get("pillars", {})
                    _act_entry = {
                        "ts":       int(time.time() * 1000),
                        "coin":     coin["name"],
                        "price":    round(price, 4),
                        "eval_sig": sig_from_eval,
                        "final_sig": sig,
                        "conf":     round(conf * 100),
                        "pillars_ok":    sum(1 for v in _act_pillars.values() if v),
                        "pillars_total": len(_act_pillars),
                        "chart":    plan.get("chart_name", ""),
                        "candle":   plan.get("candle_name", ""),
                        "in_pos":   pair in trader.positions,
                    }
                    _activity_log.append(_act_entry)
                    if len(_activity_log) > _ACTIVITY_MAX:
                        _activity_log.pop(0)

                except Exception as e:
                    log("TRADE", f"scan {pair}: {e}", "ERR")

        except Exception as e:
            log("TRADE", str(e), "ERR")
        time.sleep(REFRESH_SEC)

# ── Web Dashboard ─────────────────────────────────────────────────────────────
_flask_app      = _Flask("cryptobot")
_web_trader_ref: list = []   # filled in main(); list so route closures can mutate it
import queue as _queue

_sse_clients: list = []      # list of queue.Queue, one per connected SSE client
_sse_lock = threading.Lock()

def _push_sse(event_type: str, data: dict):
    """Broadcast a server-sent event to every connected dashboard tab."""
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        dead = [q for q in _sse_clients if q.full()]
        for q in dead:
            try: _sse_clients.remove(q)
            except ValueError: pass
        for q in _sse_clients:
            try: q.put_nowait(msg)
            except _queue.Full: pass

_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="CryptoBot">
<meta name="application-name" content="CryptoBot">
<meta name="theme-color" content="#060e1c" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="#060e1c">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" sizes="180x180" href="/icon/180">
<link rel="apple-touch-icon" sizes="167x167" href="/icon/167">
<link rel="apple-touch-icon" sizes="152x152" href="/icon/152">
<link rel="apple-touch-icon" href="/icon/180">
<link rel="icon" type="image/svg+xml" href="/icon/svg">
<title>CryptoBot</title>
<style>
:root{
  --bg:#060e1c;--s0:#0c1929;--s1:#122135;
  --bd:#162540;--bd2:#1e3555;--bd3:#2a4565;
  --tx:#c8daf0;--mu:#4d6f94;--mu2:#1a3050;
  --g:#00cc74;--r:#ff3352;--b:#4a8fff;--y:#f5a11c;
  --fn:'SF Mono','Fira Mono','Cascadia Code',ui-monospace,monospace;
  --fu:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  --tab-h:62px;--hdr-h:54px;
  --st:env(safe-area-inset-top,0px);
  --sb:env(safe-area-inset-bottom,0px);
  --sl:env(safe-area-inset-left,0px);
  --sr:env(safe-area-inset-right,0px);
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--tx);font-family:var(--fu);
     -webkit-font-smoothing:antialiased;display:flex;flex-direction:column;
     overscroll-behavior:none;user-select:none}

/* ── HEADER — grows to clear status bar / Dynamic Island ── */
.hdr{height:calc(var(--hdr-h) + var(--st));display:flex;align-items:flex-end;gap:8px;
     padding:0 calc(14px + var(--sr)) var(--hdr-pad,10px) calc(14px + var(--sl));
     padding-top:var(--st);
     background:var(--s0);border-bottom:1px solid var(--bd);
     flex-shrink:0;z-index:10}
.hdr-logo{font-family:var(--fn);font-size:.62rem;font-weight:700;
           letter-spacing:.22em;text-transform:uppercase;color:var(--b);flex-shrink:0}
.badge{display:inline-flex;align-items:center;gap:4px;padding:3px 8px;
       border-radius:99px;font-size:.52rem;font-weight:800;
       letter-spacing:.08em;text-transform:uppercase;flex-shrink:0}
.badge-live{background:rgba(0,204,116,.1);border:1px solid rgba(0,204,116,.25);color:var(--g)}
.badge-paper{background:rgba(77,111,148,.12);border:1px solid rgba(77,111,148,.28);color:var(--mu)}
.dot{width:5px;height:5px;border-radius:50%;flex-shrink:0}
.dot-live{background:var(--g);animation:blink 2s ease-in-out infinite}
.dot-paper{background:var(--mu)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
.hdr-mid{flex:1;display:flex;flex-direction:column;align-items:center;min-width:0}
.hdr-coin{font-size:.58rem;color:var(--mu);letter-spacing:.06em;line-height:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:120px}
.hdr-price{font-family:var(--fn);font-size:.98rem;font-weight:700;
            font-variant-numeric:tabular-nums;line-height:1.3}
.hdr-actions{display:flex;align-items:center;gap:6px;flex-shrink:0}
.icon-btn{width:38px;height:38px;display:flex;align-items:center;justify-content:center;
           border-radius:10px;border:1px solid var(--bd);background:transparent;
           color:var(--mu);font-size:1rem;cursor:pointer;touch-action:manipulation;
           transition:background .1s,color .1s,border-color .1s;flex-shrink:0}
.icon-btn:active{background:var(--s1)}
.icon-btn.paused{background:rgba(255,51,82,.1);border-color:rgba(255,51,82,.3);color:var(--r)}
.icon-btn.notif-on{border-color:rgba(245,161,28,.35);color:var(--y)}

/* ── PAGES ── */
.pages{flex:1;overflow:hidden;position:relative}
.page{position:absolute;inset:0;overflow-y:auto;overflow-x:hidden;
      -webkit-overflow-scrolling:touch;overscroll-behavior-y:contain;
      padding-bottom:calc(var(--tab-h) + var(--sb) + 8px)}
.page:not(.active){display:none}

/* ── BOTTOM TAB BAR ── */
.tabbar{height:calc(var(--tab-h) + var(--sb));padding-bottom:var(--sb);
        background:var(--s0);border-top:1px solid var(--bd);
        display:flex;flex-shrink:0;z-index:10}
.tab{flex:1;display:flex;flex-direction:column;align-items:center;
     justify-content:center;gap:3px;cursor:pointer;touch-action:manipulation;
     border:none;background:none;color:var(--mu);padding:0;min-height:44px;
     transition:color .12s}
.tab.active{color:var(--b)}
.tab-ico{font-size:1.3rem;line-height:1}
.tab-lbl{font-size:.5rem;letter-spacing:.07em;font-weight:700;
          text-transform:uppercase;line-height:1}

/* ── PULL TO REFRESH ── */
.ptr{display:flex;align-items:center;justify-content:center;height:0;overflow:hidden;
     transition:height .2s;color:var(--mu);font-size:.68rem;gap:6px;flex-shrink:0}
.ptr.show{height:38px}

/* ── SECTION HEADER ── */
.sh{display:flex;align-items:center;gap:8px;padding:18px 16px 10px}
.sh span{font-size:.57rem;letter-spacing:.13em;text-transform:uppercase;
          color:var(--mu);white-space:nowrap}
.sh::after{content:'';flex:1;height:1px;background:var(--bd)}

/* ── HOME TAB ── */
.hero{padding:20px 16px 18px;
      background:linear-gradient(155deg,var(--s1) 0%,var(--bg) 60%)}
.hero-lbl{font-size:.57rem;letter-spacing:.13em;text-transform:uppercase;
           color:var(--mu);margin-bottom:7px}
.hero-num{display:flex;align-items:baseline;gap:1px;font-family:var(--fn);
           font-variant-numeric:tabular-nums;line-height:1}
.hero-int{font-size:2.8rem;font-weight:700;letter-spacing:-.02em}
.hero-dec{font-size:1.4rem;font-weight:500;opacity:.5}
.hero-pill{display:inline-flex;align-items:center;gap:5px;margin-top:9px;
            padding:4px 11px;border-radius:99px;font-family:var(--fn);
            font-size:.73rem;font-weight:700;font-variant-numeric:tabular-nums}
.pill-up{background:rgba(0,204,116,.12);color:var(--g)}
.pill-dn{background:rgba(255,51,82,.1);color:var(--r)}
.pill-fl{background:rgba(77,111,148,.1);color:var(--mu)}
.hero-sub{display:flex;margin-top:14px;border-top:1px solid var(--bd2);padding-top:14px}
.hs-item{flex:1;padding-right:10px}
.hs-item+.hs-item{padding-left:10px;border-left:1px solid var(--bd2)}
.hs-lbl{font-size:.53rem;letter-spacing:.1em;text-transform:uppercase;color:var(--mu);margin-bottom:4px}
.hs-val{font-family:var(--fn);font-size:.82rem;font-weight:600;font-variant-numeric:tabular-nums}

/* stat cards 2-col grid */
.qrow{display:flex;gap:8px;padding:0 16px 10px}
.qcard{flex:1;background:var(--s0);border:1px solid var(--bd);border-radius:12px;
        padding:13px 12px;position:relative;overflow:hidden}
.qcard::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;
               border-radius:12px 0 0 12px;background:var(--ac,var(--bd2))}
.ac-b{--ac:var(--b)}.ac-g{--ac:var(--g)}.ac-r{--ac:var(--r)}.ac-y{--ac:var(--y)}.ac-m{--ac:var(--mu)}
.qcard-lbl{font-size:.52rem;letter-spacing:.1em;text-transform:uppercase;
            color:var(--mu);margin-bottom:6px}
.qcard-val{font-family:var(--fn);font-size:1.28rem;font-weight:700;
            line-height:1;font-variant-numeric:tabular-nums}
.qcard-sub{font-size:.6rem;color:var(--mu);margin-top:5px;font-family:var(--fn)}
.wr-bar{margin-top:8px;height:3px;background:var(--bd2);border-radius:2px;overflow:hidden}
.wr-fill{height:100%;border-radius:2px;transition:width .8s ease,background .4s}
.sparks{display:flex;gap:3px;margin-top:9px;flex-wrap:wrap}
.spark{width:8px;height:8px;border-radius:2px}
.spark-w{background:var(--g)}.spark-l{background:var(--r)}.spark-e{background:var(--mu2)}
.eq-wrap{margin:6px 16px 16px;background:var(--s0);border:1px solid var(--bd);
          border-radius:12px;overflow:hidden}

/* ── CHART TAB ── */
.chart-info-row{display:flex;align-items:center;gap:10px;padding:12px 16px 8px;flex-wrap:wrap}
.ci-name{font-weight:700;font-size:1.05rem}
.ci-interval{font-size:.58rem;color:var(--mu);margin-top:3px}
.ci-price{font-family:var(--fn);font-size:1.05rem;font-variant-numeric:tabular-nums;font-weight:600}
.ci-chg{font-size:.73rem;font-family:var(--fn);padding:3px 9px;border-radius:99px;font-weight:700;margin-left:auto}
.ci-chg.up{background:rgba(0,204,116,.12);color:var(--g)}
.ci-chg.dn{background:rgba(255,51,82,.1);color:var(--r)}
.ci-chg.fl{background:rgba(77,111,148,.1);color:var(--mu)}
.chart-wrap{margin:0 16px 16px;background:var(--s0);border:1px solid var(--bd);
             border-radius:12px;overflow:hidden;position:relative}
.cv-tip{position:absolute;pointer-events:none;display:none;
         background:rgba(6,14,28,.96);border:1px solid var(--bd2);border-radius:8px;
         padding:8px 11px;font-size:.62rem;font-family:var(--fn);
         color:var(--tx);white-space:nowrap;z-index:5;top:8px;left:8px;
         box-shadow:0 4px 16px rgba(0,0,0,.5)}
.cv-tip b{color:var(--b)}

/* ── POSITIONS TAB ── */
.pos-empty{display:flex;flex-direction:column;align-items:center;
            padding:40px 24px;gap:10px;color:var(--mu)}
.pos-empty-ico{font-size:2.2rem;opacity:.25}
.pos-empty-txt{font-size:.78rem;text-align:center;line-height:1.5}
.pc{background:var(--s0);border:1px solid var(--bd);border-radius:12px;
     margin:0 16px 10px;padding:15px;overflow:hidden}
.pc-r1{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}
.pc-name{font-weight:700;font-size:.95rem;display:flex;align-items:center;gap:7px}
.pc-meta{font-size:.61rem;color:var(--mu);margin-top:4px;font-family:var(--fn)}
.pc-right{text-align:right}
.pc-pnl{font-family:var(--fn);font-size:1.15rem;font-weight:700;
          font-variant-numeric:tabular-nums;line-height:1.15}
.pc-move{font-size:.66rem;text-align:right;margin-top:3px;font-family:var(--fn)}
.chip{display:inline-flex;align-items:center;font-size:.5rem;font-weight:800;letter-spacing:.07em;
       text-transform:uppercase;padding:3px 7px;border-radius:4px}
.chip-l{background:rgba(0,204,116,.12);color:var(--g);border:1px solid rgba(0,204,116,.25)}
.chip-s{background:rgba(255,51,82,.1);color:var(--r);border:1px solid rgba(255,51,82,.22)}
.mb-track{height:4px;background:var(--bd2);border-radius:2px;overflow:hidden;margin-top:8px}
.mb-fill{height:100%;border-radius:2px;transition:width .5s ease}
.mb-labels{display:flex;justify-content:space-between;margin-top:5px;
             font-size:.57rem;color:var(--mu);font-family:var(--fn)}
.trade-box{margin:0 16px 16px;background:var(--s0);border:1px solid var(--bd);border-radius:12px;overflow:hidden}
.tr{display:flex;align-items:center;padding:12px 14px;gap:10px;
     border-bottom:1px solid var(--bd)}
.tr:last-child{border-bottom:none}
.tr-icon{width:32px;height:32px;border-radius:9px;display:flex;align-items:center;
          justify-content:center;font-size:.85rem;flex-shrink:0;font-weight:700}
.tr-icon.w{background:rgba(0,204,116,.12);color:var(--g)}
.tr-icon.l{background:rgba(255,51,82,.1);color:var(--r)}
.tr-body{flex:1;min-width:0}
.tr-coin{font-weight:600;font-size:.8rem}
.tr-sub{font-size:.6rem;color:var(--mu);margin-top:2px;
         white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tr-right{text-align:right;flex-shrink:0}
.tr-pnl{font-family:var(--fn);font-size:.85rem;font-weight:700;font-variant-numeric:tabular-nums}
.tr-time{font-size:.58rem;color:var(--mu);margin-top:2px;font-family:var(--fn)}

/* ── STATS TAB ── */
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:0 16px 10px}
.scard{background:var(--s0);border:1px solid var(--bd);border-radius:12px;padding:14px 13px}
.scard-lbl{font-size:.52rem;letter-spacing:.1em;text-transform:uppercase;color:var(--mu);margin-bottom:7px}
.scard-val{font-family:var(--fn);font-size:1.38rem;font-weight:700;
            line-height:1;font-variant-numeric:tabular-nums}
.scard-sub{font-size:.62rem;color:var(--mu);margin-top:5px;font-family:var(--fn)}
.coin-box{background:var(--s0);border:1px solid var(--bd);border-radius:12px;
           overflow:hidden;margin:0 16px 16px}
.ct{display:grid;grid-template-columns:1fr 44px 34px 64px;align-items:center;
     padding:10px 14px;gap:8px;border-bottom:1px solid var(--bd)}
.ct:last-child{border-bottom:none}
.ct-hdr{font-size:.51rem;letter-spacing:.1em;text-transform:uppercase;color:var(--mu)}
.ct-coin{font-weight:600;font-size:.76rem}
.ct-wr-wrap{text-align:right}
.ct-wr{font-family:var(--fn);font-size:.73rem;font-variant-numeric:tabular-nums;font-weight:600}
.ct-cnt{font-size:.63rem;color:var(--mu);font-family:var(--fn);text-align:center}
.ct-pnl{font-family:var(--fn);font-size:.76rem;font-weight:600;font-variant-numeric:tabular-nums;text-align:right}
.wr-mini{height:2px;background:var(--bd2);border-radius:2px;overflow:hidden;margin-top:4px}
.wr-mini-f{height:100%;border-radius:2px}
.no-data{padding:22px 16px;font-size:.73rem;color:var(--mu);text-align:center}

/* color helpers */
.c-g{color:var(--g)}.c-r{color:var(--r)}.c-b{color:var(--b)}.c-y{color:var(--y)}.c-tx{color:var(--tx)}.c-mu{color:var(--mu)}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
</style>
</head>
<body>

<header class="hdr">
  <div class="hdr-logo">CB</div>
  <div id="mode_badge" class="badge badge-paper">
    <div id="mode_dot" class="dot dot-paper"></div>
    <span id="mode_txt">PAPER</span>
  </div>
  <div class="hdr-mid">
    <div class="hdr-coin" id="hdr_coin">—</div>
    <div class="hdr-price c-tx" id="hdr_price">—</div>
  </div>
  <div class="hdr-actions">
    <button class="icon-btn" id="pause_btn" onclick="togglePause()" title="Pause bot">&#9208;</button>
    <button class="icon-btn" id="notif_btn" onclick="requestNotifs()" title="Notifications">&#128276;</button>
  </div>
</header>

<div class="pages" id="pages">

  <!-- HOME -->
  <div class="page active" id="pg-home">
    <div class="ptr" id="ptr-home">&#8635; Refreshing…</div>
    <div class="hero">
      <div class="hero-lbl">Portfolio Balance</div>
      <div class="hero-num c-tx" id="hero_num">
        <span class="hero-int" id="bal_int">—</span><span class="hero-dec" id="bal_dec"></span>
      </div>
      <div class="hero-pill pill-fl" id="hero_pill">+$0.00 today</div>
      <div class="hero-sub">
        <div class="hs-item">
          <div class="hs-lbl">Today</div>
          <div class="hs-val c-mu" id="h_day">—</div>
        </div>
        <div class="hs-item">
          <div class="hs-lbl">Session</div>
          <div class="hs-val c-mu" id="h_sess">—</div>
        </div>
        <div class="hs-item">
          <div class="hs-lbl">Peak</div>
          <div class="hs-val c-mu" id="h_peak">—</div>
        </div>
      </div>
    </div>

    <div class="sh"><span>Rank Progress</span></div>
    <div style="margin:0 16px 16px;background:var(--s0);border:1px solid var(--bd);border-radius:12px;padding:15px 16px" id="rank_card">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <div style="font-size:1.6rem;line-height:1" id="rank_emoji">🟤</div>
        <div style="flex:1">
          <div style="font-weight:700;font-size:.95rem;line-height:1.2" id="rank_name">Rookie</div>
          <div style="font-size:.6rem;color:var(--mu);margin-top:2px" id="rank_unlock">You're just getting started. Every trade is a lesson.</div>
        </div>
        <div style="text-align:right;flex-shrink:0">
          <div style="font-family:var(--fn);font-size:.72rem;font-weight:700;color:var(--b)" id="rank_pct">0%</div>
          <div style="font-size:.55rem;color:var(--mu);margin-top:1px" id="rank_next_lbl">to next</div>
        </div>
      </div>
      <div style="height:6px;background:var(--bd2);border-radius:3px;overflow:hidden">
        <div id="rank_bar" style="height:100%;border-radius:3px;background:linear-gradient(90deg,var(--b),#7ab4ff);width:0%;transition:width 1s ease"></div>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:6px;font-size:.57rem;color:var(--mu);font-family:var(--fn)">
        <span id="rank_cur_bal">$100</span>
        <span id="rank_next_info">Next: ⚪ Trader @ $250</span>
      </div>
    </div>

    <div class="sh"><span>Quick Stats</span></div>
    <div class="qrow">
      <div class="qcard ac-b">
        <div class="qcard-lbl">Win Rate</div>
        <div class="qcard-val c-b" id="wr_val">—</div>
        <div class="wr-bar"><div class="wr-fill" id="wr_fill" style="width:0%"></div></div>
      </div>
      <div class="qcard ac-m" id="streak_card">
        <div class="qcard-lbl">Streak</div>
        <div class="qcard-val c-mu" id="streak_val">—</div>
        <div class="qcard-sub" id="streak_sub"></div>
      </div>
    </div>
    <div class="qrow" style="padding-top:0">
      <div class="qcard ac-m">
        <div class="qcard-lbl">Total Trades</div>
        <div class="qcard-val c-b" id="trades_val">—</div>
        <div class="sparks" id="sparks"></div>
      </div>
      <div class="qcard ac-m">
        <div class="qcard-lbl">Watching</div>
        <div class="qcard-val c-b" id="coin_val" style="font-size:.98rem">—</div>
        <div class="qcard-sub" id="watching_sub">—</div>
      </div>
    </div>
    <div class="qrow" style="padding-top:0">
      <div class="qcard ac-m" id="pf_card">
        <div class="qcard-lbl">Profit Factor</div>
        <div class="qcard-val" id="pf_val">—</div>
        <div class="qcard-sub" id="pf_sub">wins ÷ losses</div>
      </div>
      <div class="qcard ac-m">
        <div class="qcard-lbl">Avg Win / Loss</div>
        <div class="qcard-val" id="avgwl_val" style="font-size:.88rem">—</div>
        <div class="qcard-sub" id="avgwl_sub"></div>
      </div>
    </div>

    <div class="sh"><span>Bot Activity</span><span style="font-size:.58rem;color:var(--mu);font-weight:400">live scan feed</span></div>
    <div id="activity_feed" style="padding:0 12px 4px">
      <div class="no-data" style="padding:16px 0">Waiting for first scan…</div>
    </div>

    <div class="sh"><span>Equity Curve</span></div>
    <div class="eq-wrap"><canvas id="eq_cv" class="cvc" style="display:block;width:100%" height="110"></canvas></div>
  </div>

  <!-- CHART -->
  <div class="page" id="pg-chart">
    <div class="ptr" id="ptr-chart">&#8635; Refreshing…</div>
    <div class="chart-info-row">
      <div>
        <div class="ci-name" id="ci_name">—</div>
        <div class="ci-interval">15-min candles · 80 bars</div>
      </div>
      <div class="ci-price c-tx" id="ci_price">—</div>
      <div class="ci-chg fl" id="ci_chg">—</div>
    </div>
    <div id="pat_badge_wrap" style="padding:0 12px 8px;display:none">
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <div id="pat_badge" style="display:none;align-items:center;gap:6px;
             padding:5px 12px;border-radius:8px;font-size:.7rem;font-weight:700;
             letter-spacing:.04em;border:1px solid;transition:all .3s">
          <span id="pat_ico" style="font-size:.85rem"></span>
          <span id="pat_name"></span>
          <span id="pat_str" style="opacity:.6;font-weight:500;font-size:.62rem"></span>
        </div>
        <div id="candle_badge" style="display:none;align-items:center;gap:6px;
             padding:5px 12px;border-radius:8px;font-size:.7rem;font-weight:700;
             letter-spacing:.04em;border:1px solid;transition:all .3s">
          <span id="candle_ico" style="font-size:.85rem"></span>
          <span id="candle_name_el"></span>
        </div>
      </div>
      <div style="font-size:.54rem;color:var(--mu);margin-top:5px;padding-left:2px">
        📐 Chart structure · 15 min &nbsp;|&nbsp; 🕯️ Candle · live
      </div>
    </div>
    <div class="chart-wrap">
      <canvas id="cd_cv" style="display:block;width:100%" height="290"></canvas>
      <div class="cv-tip" id="cv_tip"></div>
    </div>
  </div>

  <!-- TRADES -->
  <div class="page" id="pg-pos">
    <div class="ptr" id="ptr-pos">&#8635; Refreshing…</div>
    <div class="sh"><span>Open Positions</span></div>
    <div id="pos_list"></div>
    <div class="sh"><span>Recent Trades</span></div>
    <div class="trade-box" id="trades_box"><div class="no-data">No trades yet</div></div>
  </div>

  <!-- STATS -->
  <div class="page" id="pg-stats">
    <div class="ptr" id="ptr-stats">&#8635; Refreshing…</div>
    <div class="sh"><span>Performance</span></div>
    <div class="stat-grid">
      <div class="scard">
        <div class="scard-lbl">Win Rate</div>
        <div class="scard-val c-b" id="s_wr">—</div>
        <div class="wr-bar" style="margin-top:8px"><div class="wr-fill" id="s_wr_fill" style="width:0%"></div></div>
      </div>
      <div class="scard" id="s_streak_card">
        <div class="scard-lbl">Streak</div>
        <div class="scard-val c-mu" id="s_streak">—</div>
        <div class="scard-sub" id="s_streak_sub"></div>
      </div>
      <div class="scard">
        <div class="scard-lbl">Total Trades</div>
        <div class="scard-val c-b" id="s_trades">—</div>
        <div class="scard-sub" id="s_trades_sub"></div>
      </div>
      <div class="scard">
        <div class="scard-lbl">Session P&amp;L</div>
        <div class="scard-val" id="s_pnl">—</div>
        <div class="scard-sub" id="s_pnl_sub"></div>
      </div>
      <div class="scard" id="s_pf_card">
        <div class="scard-lbl">Profit Factor</div>
        <div class="scard-val" id="s_pf">—</div>
        <div class="scard-sub" id="s_pf_sub">need &gt;1.5 to scale up</div>
      </div>
      <div class="scard">
        <div class="scard-lbl">Avg Win</div>
        <div class="scard-val c-g" id="s_avg_win">—</div>
        <div class="scard-sub" id="s_avg_win_sub"></div>
      </div>
      <div class="scard">
        <div class="scard-lbl">Avg Loss</div>
        <div class="scard-val c-r" id="s_avg_loss">—</div>
        <div class="scard-sub" id="s_avg_loss_sub"></div>
      </div>
    </div>
    <div class="sh"><span>Rank Progress</span></div>
    <div style="margin:0 16px 14px;background:var(--s0);border:1px solid var(--bd);border-radius:12px;padding:14px 15px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <div style="font-size:1.45rem;line-height:1" id="s_rank_emoji">🟤</div>
        <div style="flex:1">
          <div style="font-weight:700;font-size:.9rem" id="s_rank_name">Rookie</div>
          <div style="font-size:.58rem;color:var(--mu);margin-top:1px" id="s_rank_next">Next: ⚪ Trader @ $250</div>
        </div>
        <div style="font-family:var(--fn);font-size:.82rem;font-weight:700;color:var(--b)" id="s_rank_pct">0%</div>
      </div>
      <div style="height:5px;background:var(--bd2);border-radius:3px;overflow:hidden">
        <div id="s_rank_bar" style="height:100%;border-radius:3px;background:linear-gradient(90deg,var(--b),#7ab4ff);width:0%;transition:width 1s ease"></div>
      </div>
      <div style="font-size:.57rem;color:var(--mu);margin-top:5px;font-family:var(--fn)" id="s_learn_status"></div>
    </div>
    <div class="sh"><span>Pattern Scan</span><span style="font-size:.55rem;color:var(--mu);font-weight:400">updates every 15 min</span></div>
    <div id="pattern_scan_list" style="padding:0 12px 16px">
      <div class="no-data">Scanning…</div>
    </div>
    <div class="sh"><span>Coin Breakdown</span></div>
    <div class="coin-box" id="coin_table"><div class="no-data">No trades yet</div></div>
  </div>

</div><!-- /pages -->

<nav class="tabbar">
  <button class="tab active" id="tab-home" onclick="goTab('home')">
    <div class="tab-ico">&#127968;</div>
    <div class="tab-lbl">Home</div>
  </button>
  <button class="tab" id="tab-chart" onclick="goTab('chart')">
    <div class="tab-ico">&#128200;</div>
    <div class="tab-lbl">Chart</div>
  </button>
  <button class="tab" id="tab-pos" onclick="goTab('pos')">
    <div class="tab-ico">&#128260;</div>
    <div class="tab-lbl">Trades</div>
  </button>
  <button class="tab" id="tab-stats" onclick="goTab('stats')">
    <div class="tab-ico">&#128202;</div>
    <div class="tab-lbl">Stats</div>
  </button>
</nav>

<script>
const $=id=>document.getElementById(id);
const TAB_ORDER=['home','chart','pos','stats'];
let _tab='home',_paused=false,_notif=false;
let _eqData=[],_cdData=[],_cdHover=-1,_tick=30;

const fmt=(n,d=2)=>Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const msign=n=>(n>=0?'+$':'\u2212$')+fmt(Math.abs(n));
const pc=n=>n>0?'c-g':n<0?'c-r':'c-mu';

/* ── TAB NAVIGATION ── */
function goTab(t){
  if(_tab===t)return;
  _tab=t;
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  $('pg-'+t).classList.add('active');
  $('tab-'+t).classList.add('active');
  if(t==='chart'){drawCandles();}
  if(t==='home'){drawEquity();}
}

/* ── SWIPE ── */
(function(){
  const pages=$('pages');
  let sx=0,sy=0,ok=false;
  pages.addEventListener('touchstart',e=>{sx=e.touches[0].clientX;sy=e.touches[0].clientY;ok=true;},{passive:true});
  pages.addEventListener('touchend',e=>{
    if(!ok)return;ok=false;
    const dx=e.changedTouches[0].clientX-sx,dy=e.changedTouches[0].clientY-sy;
    if(Math.abs(dx)>55&&Math.abs(dx)>Math.abs(dy)*1.6){
      const ci=TAB_ORDER.indexOf(_tab);
      const ni=dx<0?Math.min(ci+1,TAB_ORDER.length-1):Math.max(ci-1,0);
      if(ni!==ci)goTab(TAB_ORDER[ni]);
    }
  },{passive:true});
})();

/* ── PULL TO REFRESH ── */
(function(){
  TAB_ORDER.forEach(t=>{
    const pg=$('pg-'+t),ptr=$('ptr-'+t);
    if(!pg||!ptr)return;
    let sy=0,arm=false;
    pg.addEventListener('touchstart',e=>{if(pg.scrollTop===0){sy=e.touches[0].clientY;arm=true;}},{passive:true});
    pg.addEventListener('touchmove',e=>{if(arm&&e.touches[0].clientY-sy>65)ptr.classList.add('show');},{passive:true});
    pg.addEventListener('touchend',()=>{
      if(ptr.classList.contains('show')){fetchStatus();fetchCandles();fetchHistory();}
      ptr.classList.remove('show');arm=false;
    },{passive:true});
  });
})();

/* ── BALANCE ── */
function setBal(n,cls){
  const p=fmt(Math.abs(n)).split('.');
  $('bal_int').textContent=(n<0?'\u2212':'')+'$'+p[0];
  $('bal_dec').textContent='.'+( p[1]||'00');
  $('hero_num').className='hero-num '+cls;
}
function hsVal(id,n){const e=$(id);e.textContent=msign(n);e.className='hs-val '+pc(n);}

/* ── FETCH STATUS ── */
async function fetchStatus(){
  try{
    const d=await(await fetch('/status')).json();
    const live=d.mode==='LIVE';
    $('mode_badge').className='badge '+(live?'badge-live':'badge-paper');
    $('mode_dot').className='dot '+(live?'dot-live':'dot-paper');
    $('mode_txt').textContent=(d.mode||'PAPER')+(live&&d.exchange?' · '+d.exchange.toUpperCase():'');
    setBal(d.balance,pc(d.day_pnl));
    const dp=d.day_pnl||0;
    const pill=$('hero_pill');
    pill.textContent=(dp>=0?'+':'')+msign(dp)+' today';
    pill.className='hero-pill '+(dp>0?'pill-up':dp<0?'pill-dn':'pill-fl');
    hsVal('h_day',dp);
    hsVal('h_sess',d.session_pnl||0);
    $('h_peak').textContent='$'+fmt(d.peak||d.balance);
    const wr=d.win_rate||0;
    $('wr_val').textContent=wr.toFixed(0)+'%';
    $('s_wr').textContent=wr.toFixed(0)+'%';
    [$('wr_fill'),$('s_wr_fill')].forEach(f=>{
      f.style.width=Math.min(wr,100)+'%';
      f.style.background=wr>=50?'var(--g)':wr>=35?'var(--y)':'var(--r)';
    });
    const s=d.streak||0;
    const [sv,sc,ssb]=[$('streak_val'),$('streak_card'),$('streak_sub')];
    const [ss,ssc,ssbs]=[$('s_streak'),$('s_streak_card'),$('s_streak_sub')];
    if(s>0){
      sv.textContent='+'+s+' \u2191';sv.className='qcard-val c-g';sc.className='qcard ac-g';ssb.textContent=s+' wins in a row';
      ss.textContent='+'+s+' wins';ss.className='scard-val c-g';ssc.className='scard';ssbs.textContent='Keep it up!';
    }else if(s<0){
      sv.textContent=s+' \u2193';sv.className='qcard-val c-r';sc.className='qcard ac-r';ssb.textContent=Math.abs(s)+' losses in a row';
      ss.textContent=s+' losses';ss.className='scard-val c-r';ssc.className='scard';ssbs.textContent='Be careful';
    }else{
      sv.textContent='—';sv.className='qcard-val c-mu';sc.className='qcard ac-m';ssb.textContent='No streak yet';
      ss.textContent='—';ss.className='scard-val c-mu';ssc.className='scard';ssbs.textContent='';
    }
    const tot=d.trades||0,w=d.wins||0,l=d.losses||0;
    $('trades_val').textContent=tot.toLocaleString();
    $('s_trades').textContent=tot.toLocaleString();
    $('s_trades_sub').textContent=w+' wins · '+l+' losses';
    const sp=d.session_pnl||0;
    $('s_pnl').textContent=(sp>=0?'+':'')+msign(sp);
    $('s_pnl').className='scard-val '+(sp>0?'c-g':sp<0?'c-r':'c-mu');
    $('s_pnl_sub').textContent='this session';
    // Profit factor
    const pf=d.profit_factor;
    const pfStr=pf!=null?pf.toFixed(2):'—';
    const pfCls=pf==null?'c-mu':pf>=1.5?'c-g':pf>=1.0?'c-y':'c-r';
    const pfNote=pf==null?'need trades':pf>=1.5?'edge confirmed ✓':pf>=1.0?'marginal edge':'losing edge ✗';
    [$('pf_val'),$('s_pf')].forEach(e=>{if(e){e.textContent=pfStr;e.className=(e.id==='pf_val'?'qcard-val ':'scard-val ')+pfCls;}});
    [$('pf_card')].forEach(e=>{if(e)e.className='qcard '+(pf>=1.5?'ac-g':pf>=1.0?'ac-m':'ac-r');});
    [$('s_pf_card')].forEach(e=>{if(e)e.className='scard';});
    [$('pf_sub'),$('s_pf_sub')].forEach(e=>{if(e)e.textContent=pfNote;});
    // Avg win / loss
    const aw=d.avg_win,al=d.avg_loss;
    if($('avgwl_val'))$('avgwl_val').textContent=(aw!=null?'+$'+aw.toFixed(2):'—')+' / '+(al!=null?'$'+al.toFixed(2):'—');
    if($('avgwl_sub'))$('avgwl_sub').textContent=aw&&al?'ratio '+(aw/Math.max(al,0.01)).toFixed(2)+'×':'';
    if($('s_avg_win')){$('s_avg_win').textContent=aw!=null?'+$'+aw.toFixed(2):'—';$('s_avg_win_sub').textContent=aw&&al?'vs $'+al.toFixed(2)+' loss':'';}
    if($('s_avg_loss')){$('s_avg_loss').textContent=al!=null?'$'+al.toFixed(2):'—';$('s_avg_loss_sub').textContent=aw&&al?'ratio '+(aw/Math.max(al,0.01)).toFixed(2)+'×':'';}

    const recent=(d.recent_trades||[]).slice(0,10);
    $('sparks').innerHTML=recent.map(t=>'<div class="spark '+(t.pnl>0?'spark-w':'spark-l')+'" title="'+(t.pnl>0?'+':'')+t.pnl.toFixed(2)+'"></div>').join('');
    const coin=d.coin||'—',pair=d.pair||coin;
    $('coin_val').textContent=coin;
    $('watching_sub').textContent=pair;
    $('hdr_coin').textContent=pair;
    $('ci_name').textContent=pair;
    _paused=!!d.paused;
    const pb=$('pause_btn');
    pb.innerHTML=_paused?'&#9654;':'&#9208;';
    pb.className='icon-btn'+(_paused?' paused':'');
    pb.title=_paused?'Resume bot':'Pause bot';
    renderPositions(d.open_positions||[]);
    renderActivity(d.activity_log||[]);
    renderTrades(recent);
    renderCoinTable(d.coin_stats||{});
    renderRank(d);
    renderPattern(d);
  }catch(e){console.warn('status',e);}
}

/* ── POSITIONS ── */
function renderPositions(ps){
  const el=$('pos_list');
  if(!ps.length){
    el.innerHTML='<div class="pos-empty"><div class="pos-empty-ico">&#128219;</div><div class="pos-empty-txt">No open positions<br><small style="opacity:.5">Bot is watching markets</small></div></div>';
    return;
  }
  el.innerHTML=ps.map(p=>{
    const isL=p.side==='LONG';
    const chip='<span class="chip '+(isL?'chip-l':'chip-s')+'">' +(isL?'LONG':'SHORT')+'</span>';
    const mc=(p.move_pct||0)>=0?'c-g':'c-r';
    const mbg=(p.move_pct||0)>=0?'var(--g)':'var(--r)';
    const bw=Math.min(Math.abs(p.move_pct||0)*8,100).toFixed(0);
    const stop=p.trail_stop?'Stop $'+p.trail_stop.toFixed(4):'';
    // Contract tier badge
    const tierEmoji={'Cautious':'🔵','Moderate':'🟡','Confident':'🟠','Max Bet':'🔴'};
    const tierCol={'Cautious':'#4fc3f7','Moderate':'#ffd54f','Confident':'#ff9800','Max Bet':'#ef5350'};
    const tier=p.contract_tier||'';
    const tierBadge=tier?'<span style="font-size:.62rem;font-weight:700;padding:1px 6px;border-radius:5px;background:'+
      (tierCol[tier]||'var(--mu)')+'22;color:'+(tierCol[tier]||'var(--mu)')+';border:1px solid '+
      (tierCol[tier]||'var(--mu)')+'44;margin-left:5px">'+(tierEmoji[tier]||'')+'&nbsp;'+tier+'&nbsp;'+p.leverage+'×</span>':'';
    return '<div class="pc">'+
      '<div class="pc-r1"><div>'+
        '<div class="pc-name">'+chip+' '+p.name+tierBadge+'</div>'+
        '<div class="pc-meta">Entry $'+p.entry.toFixed(4)+' · '+p.confidence+'% conf · '+(p.held_mins||0)+'m</div>'+
      '</div><div class="pc-right">'+
        '<div class="pc-pnl '+pc(p.unrealized_pnl)+'">'+msign(p.unrealized_pnl)+'</div>'+
        '<div class="pc-move '+mc+'">' +(p.move_pct>=0?'+':'')+p.move_pct.toFixed(2)+'%</div>'+
      '</div></div>'+
      '<div class="mb-track"><div class="mb-fill" style="width:'+bw+'%;background:'+mbg+'"></div></div>'+
      '<div class="mb-labels"><span>Entry $'+p.entry.toFixed(4)+'</span><span>'+stop+'</span></div></div>';
  }).join('');
}

/* ── RECENT TRADES ── */
function renderTrades(recent){
  const box=$('trades_box');
  if(!recent.length){box.innerHTML='<div class="no-data">No trades yet</div>';return;}
  box.innerHTML=recent.map(t=>{
    const w=t.pnl>0;
    const ts=t.ts?new Date(t.ts).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'';
    return '<div class="tr">'+
      '<div class="tr-icon '+(w?'w':'l')+'">' +(w?'\u2713':'\u2717')+'</div>'+
      '<div class="tr-body">'+
        '<div class="tr-coin">'+(t.coin||'')+'</div>'+
        '<div class="tr-sub">'+(t.reason||'exit').replace(/_/g,' ')+' · '+(t.held_mins||0)+'m</div>'+
      '</div>'+
      '<div class="tr-right">'+
        '<div class="tr-pnl '+(w?'c-g':'c-r')+'">'+msign(t.pnl)+'</div>'+
        '<div class="tr-time">'+ts+'</div>'+
      '</div></div>';
  }).join('');
}

/* ── ACTIVITY FEED ── */
function renderActivity(log){
  const el=$('activity_feed');
  if(!el)return;
  if(!log||!log.length){el.innerHTML='<div class="no-data" style="padding:16px 0">Waiting for first scan…</div>';return;}
  const fmtTime=ts=>{
    const d=new Date(ts);
    return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  };
  el.innerHTML=log.map(e=>{
    const isBuy=e.eval_sig==='BUY', isSell=e.eval_sig==='SELL';
    const blocked=isBuy||isSell?e.final_sig==='HOLD':false;
    const inPos=e.in_pos;

    // Determine action label + color
    let ico, label, acol, abg;
    if(inPos && (isBuy||isSell) && !blocked){
      ico='📂'; label='IN TRADE'; acol='var(--b)'; abg='rgba(70,130,255,.1)';
    } else if(blocked){
      ico='⛔'; label='BLOCKED'; acol='var(--mu)'; abg='rgba(128,128,128,.07)';
    } else if(isBuy){
      ico='📊'; label='BUY SIG'; acol='var(--g)'; abg='rgba(0,204,116,.07)';
    } else if(isSell){
      ico='📊'; label='SELL SIG'; acol='var(--r)'; abg='rgba(255,51,82,.07)';
    } else {
      ico='🔍'; label='WATCH'; acol='var(--mu)'; abg='transparent';
    }

    const confCol=e.conf>=70?'var(--g)':e.conf>=55?'var(--b)':'var(--mu)';
    const pillStr=e.pillars_total?e.pillars_ok+'/'+e.pillars_total+' ✓':'';
    const patStr=[e.chart?'📐'+e.chart:'',e.candle?'🕯️'+e.candle:''].filter(Boolean).join('  ');

    return '<div style="display:flex;align-items:flex-start;gap:9px;padding:7px 10px;margin-bottom:4px;'+
      'border-radius:9px;background:'+abg+'">'+
      '<div style="font-size:1rem;line-height:1.4;flex-shrink:0">'+ico+'</div>'+
      '<div style="flex:1;min-width:0">'+
        '<div style="display:flex;align-items:baseline;gap:7px;flex-wrap:wrap">'+
          '<span style="font-weight:700;font-size:.8rem;color:var(--tx)">'+e.coin+'</span>'+
          '<span style="font-size:.65rem;font-weight:700;color:'+acol+'">'+label+'</span>'+
          '<span style="font-family:var(--fn);font-size:.65rem;color:'+confCol+'">'+e.conf+'%</span>'+
          (pillStr?'<span style="font-size:.58rem;color:var(--mu)">'+pillStr+'</span>':'')+
        '</div>'+
        (patStr?'<div style="font-size:.58rem;color:var(--mu);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+patStr+'</div>':'')+
      '</div>'+
      '<div style="font-size:.55rem;color:var(--mu);flex-shrink:0;margin-top:2px">'+fmtTime(e.ts)+'</div>'+
    '</div>';
  }).join('');
}

/* ── COIN TABLE ── */
function renderCoinTable(cs){
  const coins=Object.entries(cs).sort((a,b)=>(b[1].wins+b[1].losses)-(a[1].wins+a[1].losses));
  const ct=$('coin_table');
  if(!coins.length){ct.innerHTML='<div class="no-data">No trades yet</div>';return;}
  ct.innerHTML='<div class="ct">'+
    '<span class="ct-hdr">Coin</span>'+
    '<span class="ct-hdr ct-wr-wrap">Win%</span>'+
    '<span class="ct-hdr ct-cnt">#</span>'+
    '<span class="ct-hdr ct-pnl">P&amp;L</span></div>'+
    coins.map(([coin,s])=>{
      const tot=s.wins+s.losses;
      const wr=tot?Math.round(s.wins/tot*100):0;
      const fc=wr>=50?'var(--g)':wr>=35?'var(--y)':'var(--r)';
      const wrc=wr>=50?'c-g':wr>=35?'c-y':'c-r';
      return '<div class="ct">'+
        '<div class="ct-coin">'+coin+'<div class="wr-mini"><div class="wr-mini-f" style="width:'+wr+'%;background:'+fc+'"></div></div></div>'+
        '<div class="ct-wr-wrap"><span class="ct-wr '+wrc+'">'+wr+'%</span></div>'+
        '<div class="ct-cnt">'+tot+'</div>'+
        '<div class="ct-pnl '+(s.pnl>=0?'c-g':'c-r')+'">'+msign(s.pnl)+'</div></div>';
    }).join('');
}

/* ── RANK PROGRESS ── */
function renderRank(d){
  const pct=d.rank_progress||0;
  const bar=pct.toFixed(1)+'%';
  const nextInfo='Next: '+(d.next_rank_emoji||'')+' '+(d.next_rank||'—')+' @ $'+(d.next_rank_min||0).toLocaleString();
  const unlock={
    'Rookie':'Every trade is a lesson.',
    'Trader':'You proved you can grow.',
    'Pro':'Skill, not luck.',
    'Expert':'Rare territory.',
    'Elite':'Top 1%.',
    'Legend':'Built different.',
    'GOAT':'$10k hit.',
    'Diamond':'$15k strong.',
    'Immortal':'$25k mastered.',
    'Mythic':'$35k and rising.',
    'OVERLORD':'OVERLORD status.'
  }[d.rank]||'';
  // Home tab rank card
  const re=$('rank_emoji'),rn=$('rank_name'),ru=$('rank_unlock');
  const rp=$('rank_pct'),rb=$('rank_bar'),rcb=$('rank_cur_bal'),rni=$('rank_next_info');
  if(re){re.textContent=d.rank_emoji||'🟤';}
  if(rn){rn.textContent=d.rank||'Rookie';}
  if(ru){ru.textContent=unlock;}
  if(rp){rp.textContent=bar;}
  if(rb){rb.style.width=Math.min(pct,100)+'%';}
  if(rcb){rcb.textContent='$'+(d.balance||0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});}
  if(rni){rni.textContent=nextInfo;}
  // Stats tab rank card
  const se=$('s_rank_emoji'),sn=$('s_rank_name'),snx=$('s_rank_next');
  const sp=$('s_rank_pct'),sb=$('s_rank_bar'),sl=$('s_learn_status');
  if(se){se.textContent=d.rank_emoji||'🟤';}
  if(sn){sn.textContent=d.rank||'Rookie';}
  if(snx){snx.textContent=nextInfo;}
  if(sp){sp.textContent=bar;}
  if(sb){sb.style.width=Math.min(pct,100)+'%';}
  if(sl){sl.textContent=d.learning?'Learning from trades: ON ✓':'Need more trades to activate learning';}
}

/* ── PATTERN BADGE HELPER ── */
function _applyBadge(el,sig,label,str){
  if(!el)return;
  if(!label||sig==='NONE'){el.style.display='none';return;}
  const bull=sig==='BULL';
  const col=bull?'var(--g)':'var(--r)';
  el.style.display='inline-flex';
  el.style.background=bull?'rgba(0,204,116,.08)':'rgba(255,51,82,.08)';
  el.style.borderColor=bull?'rgba(0,204,116,.25)':'rgba(255,51,82,.25)';
  el.style.color=col;
  return col;
}

/* ── CHART PATTERN BADGE + SCAN LIST ── */
function renderPattern(d){
  const wrap=$('pat_badge_wrap');
  const hasChart=d.chart_pattern&&d.chart_pattern_signal!=='NONE';
  const hasCandle=d.candle_pattern&&d.candle_pattern_signal!=='NONE';
  if(wrap){wrap.style.display=(hasChart||hasCandle)?'block':'none';}

  // Chart structure badge
  const pb=$('pat_badge');
  if(hasChart){
    _applyBadge(pb,d.chart_pattern_signal);
    const bull=d.chart_pattern_signal==='BULL';
    $('pat_ico').textContent=bull?'▲':'▼';
    $('pat_name').textContent=d.chart_pattern;
    const ap=d.all_patterns||{};
    const cp=Object.values(ap).find(p=>p.name===d.chart_pattern);
    $('pat_str').textContent=cp?Math.round(cp.strength*100)+'% conf':'';
    pb.style.display='inline-flex';
  } else if(pb){pb.style.display='none';}

  // Candle pattern badge
  const cb=$('candle_badge');
  if(hasCandle){
    _applyBadge(cb,d.candle_pattern_signal);
    const bull=d.candle_pattern_signal==='BULL';
    $('candle_ico').textContent=bull?'🕯️▲':'🕯️▼';
    $('candle_name_el').textContent=d.candle_pattern;
    cb.style.display='inline-flex';
  } else if(cb){cb.style.display='none';}

  // ── Stats tab pattern scan list ──────────────────────────────
  const el=$('pattern_scan_list');
  if(!el)return;
  const ap=d.all_patterns||{};
  const entries=Object.entries(ap).filter(([,v])=>v.name||v.candle_name);
  if(!entries.length){
    el.innerHTML='<div class="no-data">Scanning — first results appear after 15 min</div>';
    return;
  }
  // Priority: chart signal first (BULL before BEAR), then candle, then strength
  const sigOrd=s=>s==='BULL'?0:s==='BEAR'?1:2;
  entries.sort((a,b)=>{
    const as=sigOrd(a[1].signal||'NONE'), bs=sigOrd(b[1].signal||'NONE');
    if(as!==bs)return as-bs;
    return (b[1].strength||0)-(a[1].strength||0);
  });
  el.innerHTML=entries.map(([pair,v])=>{
    const cs=v.signal||'NONE', cn=v.candle_signal||'NONE';
    const sig=cs!=='NONE'?cs:cn;
    const bull=sig==='BULL';
    const col=bull?'var(--g)':'var(--r)';
    const bg=bull?'rgba(0,204,116,.07)':'rgba(255,51,82,.07)';
    const bdr=bull?'rgba(0,204,116,.2)':'rgba(255,51,82,.2)';
    const ico=bull?'▲':'▼';
    const str=v.strength?Math.round(v.strength*100)+'%':'—';
    const chartLine=v.name?
      '<div style="font-weight:700;font-size:.78rem;color:var(--tx)">📐 '+v.name+'</div>':'';
    const candleLine=v.candle_name?
      '<div style="font-size:.72rem;color:var(--mu);margin-top:2px">🕯️ '+v.candle_name+'</div>':'';
    return '<div style="display:flex;align-items:center;gap:10px;'+
      'padding:9px 12px;margin-bottom:6px;border-radius:10px;'+
      'background:'+bg+';border:1px solid '+bdr+'">'+
      '<div style="font-size:.95rem;line-height:1;color:'+col+'">'+ico+'</div>'+
      '<div style="flex:1;min-width:0">'+
        chartLine+candleLine+
        '<div style="font-size:.58rem;color:var(--mu);margin-top:3px">'+v.coin+' · '+pair+'</div>'+
      '</div>'+
      (v.name?'<div style="text-align:right;flex-shrink:0">'+
        '<div style="font-family:var(--fn);font-size:.75rem;font-weight:700;color:'+col+'">'+str+'</div>'+
        '<div style="font-size:.53rem;color:var(--mu)">conf</div></div>':'')
      +'</div>';
  }).join('');
}

/* ── HISTORY / EQUITY CURVE ── */
async function fetchHistory(){
  try{
    const pts=await(await fetch('/history')).json();
    if(pts&&pts.length){_eqData=pts;if(_tab==='home')drawEquity();}
  }catch(e){console.warn('history',e);}
}
function drawEquity(){
  const cv=$('eq_cv');
  const W=cv.parentElement.clientWidth||320;
  const H=110,PR=devicePixelRatio||1;
  cv.width=W*PR;cv.height=H*PR;cv.style.width=W+'px';cv.style.height=H+'px';
  const ctx=cv.getContext('2d');ctx.scale(PR,PR);
  const pts=_eqData;if(!pts.length)return;
  const P={t:8,r:10,b:18,l:52};
  const cw=W-P.l-P.r,ch=H-P.t-P.b;
  const vals=pts.map(p=>p.balance);
  let lo=Math.min(...vals),hi=Math.max(...vals);
  if(hi===lo){hi+=1;lo-=1;}
  const xS=i=>P.l+(i/(pts.length-1||1))*cw;
  const yS=v=>P.t+ch-(v-lo)/(hi-lo)*ch;
  ctx.strokeStyle='#162540';ctx.lineWidth=.7;
  [0,.5,1].forEach(f=>{
    const y=yS(lo+(hi-lo)*f);
    ctx.beginPath();ctx.moveTo(P.l,y);ctx.lineTo(W-P.r,y);ctx.stroke();
    ctx.fillStyle='#4d6f94';ctx.font='9px monospace';ctx.textAlign='right';
    ctx.fillText('$'+Math.round(lo+(hi-lo)*f).toLocaleString(),P.l-3,y+3);
  });
  const trend=pts[pts.length-1].balance>=pts[0].balance;
  const lc=trend?'#4a8fff':'#ff3352';
  const grad=ctx.createLinearGradient(0,P.t,0,P.t+ch);
  grad.addColorStop(0,trend?'rgba(74,143,255,.22)':'rgba(255,51,82,.14)');
  grad.addColorStop(1,'rgba(0,0,0,0)');
  ctx.beginPath();
  pts.forEach((p,i)=>i===0?ctx.moveTo(xS(i),yS(p.balance)):ctx.lineTo(xS(i),yS(p.balance)));
  ctx.lineTo(xS(pts.length-1),P.t+ch);ctx.lineTo(P.l,P.t+ch);
  ctx.closePath();ctx.fillStyle=grad;ctx.fill();
  ctx.beginPath();
  pts.forEach((p,i)=>i===0?ctx.moveTo(xS(i),yS(p.balance)):ctx.lineTo(xS(i),yS(p.balance)));
  ctx.strokeStyle=lc;ctx.lineWidth=1.5;ctx.lineJoin='round';ctx.stroke();
  const lx=xS(pts.length-1),ly=yS(pts[pts.length-1].balance);
  ctx.beginPath();ctx.arc(lx,ly,3.5,0,Math.PI*2);ctx.fillStyle=lc;ctx.fill();
}

/* ── CANDLES ── */
async function fetchCandles(){
  try{
    const data=await(await fetch('/candles')).json();
    if(Array.isArray(data)&&data.length){
      _cdData=data;
      const last=data[data.length-1],first=data[0];
      const pct=(last.c-first.c)/first.c*100;
      const pf=last.c>=100?last.c.toFixed(2):last.c.toPrecision(6);
      [$('hdr_price'),$('ci_price')].forEach(el=>el.textContent='$'+pf);
      const chg=$('ci_chg');
      chg.textContent=(pct>=0?'+':'')+pct.toFixed(2)+'%';
      chg.className='ci-chg '+(pct>0.05?'up':pct<-0.05?'dn':'fl');
      if(_tab==='chart')drawCandles();
    }
  }catch(e){console.warn('candles',e);}
}
function drawCandles(){
  const cv=$('cd_cv');
  const W=cv.parentElement.clientWidth||320;
  const H=290,PR=devicePixelRatio||1;
  cv.width=W*PR;cv.height=H*PR;cv.style.width=W+'px';cv.style.height=H+'px';
  const ctx=cv.getContext('2d');ctx.scale(PR,PR);
  const data=_cdData;if(!data.length)return;
  const P={t:8,r:10,b:22,l:54};
  const VH=44,cw=W-P.l-P.r,ch=H-P.t-P.b-VH-4;
  const n=data.length;
  const slotW=cw/n,barW=Math.max(2,Math.floor(slotW*.72));
  const prices=data.flatMap(c=>[c.h,c.l]);
  let lo=Math.min(...prices),hi=Math.max(...prices);
  if(hi===lo){hi*=1.001;lo*=0.999;}
  const xC=i=>P.l+(i+.5)*slotW;
  const yP=v=>P.t+ch-(v-lo)/(hi-lo)*ch;
  const vMax=Math.max(...data.map(c=>c.v))||1;
  const yV=v=>H-P.b-(v/vMax)*VH;
  ctx.strokeStyle='#162540';ctx.lineWidth=.7;
  for(let i=0;i<=4;i++){
    const v=lo+(hi-lo)*(i/4),y=yP(v);
    ctx.beginPath();ctx.moveTo(P.l,y);ctx.lineTo(W-P.r,y);ctx.stroke();
    const lbl=v>=100?v.toFixed(0):v>=1?v.toFixed(2):v.toPrecision(4);
    ctx.fillStyle='#4d6f94';ctx.font='8.5px monospace';ctx.textAlign='right';
    ctx.fillText('$'+lbl,P.l-3,y+3);
  }
  data.forEach((c,i)=>{
    const bull=c.c>=c.o,gc=bull?'#00cc74':'#ff3352';
    const x=xC(i),h=barW/2;
    const yO=yP(c.o),yC=yP(c.c),yHi=yP(c.h),yLo=yP(c.l);
    const bT=Math.min(yO,yC),bH=Math.max(1.5,Math.abs(yC-yO));
    const alpha=i===_cdHover?.95:.72;
    ctx.strokeStyle=gc;ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(x,yHi);ctx.lineTo(x,bT);ctx.stroke();
    ctx.beginPath();ctx.moveTo(x,bT+bH);ctx.lineTo(x,yLo);ctx.stroke();
    ctx.fillStyle=bull?`rgba(0,204,116,${alpha})`:`rgba(255,51,82,${alpha})`;
    ctx.fillRect(x-h,bT,barW,bH);
    ctx.fillStyle=bull?'rgba(0,204,116,.25)':'rgba(255,51,82,.18)';
    ctx.fillRect(x-h,yV(c.v),barW,H-P.b-yV(c.v));
  });
  ctx.fillStyle='#4d6f94';ctx.font='8px monospace';ctx.textAlign='center';
  const stride=Math.max(1,Math.ceil(n/7));
  data.forEach((c,i)=>{
    if(i%stride===0){
      const dd=new Date(c.t*1000);
      ctx.fillText(dd.getHours().toString().padStart(2,'0')+':'+dd.getMinutes().toString().padStart(2,'0'),xC(i),H-P.b+10);
    }
  });
  if(_cdHover>=0){
    const x=xC(_cdHover);
    ctx.save();ctx.strokeStyle='rgba(200,218,240,.2)';ctx.lineWidth=1;
    ctx.setLineDash([3,3]);
    ctx.beginPath();ctx.moveTo(x,P.t);ctx.lineTo(x,H-P.b);ctx.stroke();
    ctx.restore();
  }
}
function initCandleHover(){
  const cv=$('cd_cv'),tip=$('cv_tip');
  const idx=cx=>{
    const b=cv.getBoundingClientRect(),n=_cdData.length;
    return n?Math.min(n-1,Math.max(0,Math.floor((cx-b.left)/b.width*n))):-1;
  };
  const show=i=>{
    if(i<0||!_cdData[i]){tip.style.display='none';_cdHover=-1;drawCandles();return;}
    _cdHover=i;drawCandles();
    const c=_cdData[i],bull=c.c>=c.o;
    const ts=new Date(c.t*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
    const f6=v=>v>=100?v.toFixed(2):v.toPrecision(6);
    tip.innerHTML='<b>'+ts+'</b>&nbsp; O&nbsp;'+f6(c.o)+
      '&nbsp; H&nbsp;<span style="color:var(--g)">'+f6(c.h)+'</span>'+
      '&nbsp; L&nbsp;<span style="color:var(--r)">'+f6(c.l)+'</span>'+
      '&nbsp; C&nbsp;<span style="color:'+(bull?'var(--g)':'var(--r)')+'">'+f6(c.c)+'</span>';
    tip.style.display='block';
  };
  cv.addEventListener('mousemove',e=>show(idx(e.clientX)));
  cv.addEventListener('mouseleave',()=>{_cdHover=-1;tip.style.display='none';drawCandles();});
  cv.addEventListener('touchstart',e=>{e.preventDefault();show(idx(e.touches[0].clientX));},{passive:false});
  cv.addEventListener('touchmove',e=>{e.preventDefault();show(idx(e.touches[0].clientX));},{passive:false});
  cv.addEventListener('touchend',()=>{_cdHover=-1;tip.style.display='none';drawCandles();});
}

/* ── SSE ── */
function initSSE(){
  const es=new EventSource('/events');
  es.addEventListener('trade_open',e=>{
    const d=JSON.parse(e.data);
    notify('Trade Opened',d.side+' '+d.name+' @ $'+d.entry+' ('+d.confidence+'% conf)');
    fetchStatus();
  });
  es.addEventListener('trade_close',e=>{
    const d=JSON.parse(e.data);
    notify('Trade Closed',d.name+': '+(d.pnl>=0?'+':'')+d.pnl.toFixed(2)+' '+(d.win?'\u2713':'\u2717'),d.win);
    fetchStatus();fetchHistory();
  });
  es.addEventListener('control',e=>{
    const d=JSON.parse(e.data);_paused=d.paused;
    const pb=$('pause_btn');
    pb.innerHTML=_paused?'&#9654;':'&#9208;';
    pb.className='icon-btn'+(_paused?' paused':'');
  });
  es.onerror=()=>{};
}

/* ── NOTIFICATIONS ── */
function notify(title,body){
  if(!_notif)return;
  try{new Notification(title,{body,tag:'cryptobot',silent:true});}catch(e){}
}
async function requestNotifs(){
  if(!('Notification' in window))return;
  if(Notification.permission==='granted'){_notif=true;updateNotifBtn();return;}
  _notif=(await Notification.requestPermission())==='granted';
  updateNotifBtn();
}
function updateNotifBtn(){
  const b=$('notif_btn');
  b.innerHTML=_notif?'&#128276;':'&#128277;';
  b.className='icon-btn'+(_notif?' notif-on':'');
  b.title=_notif?'Notifications on':'Enable notifications';
}

/* ── PAUSE / RESUME ── */
async function togglePause(){
  try{
    const d=await(await fetch('/control',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'toggle'})})).json();
    _paused=d.paused;
    const pb=$('pause_btn');
    pb.innerHTML=_paused?'&#9654;':'&#9208;';
    pb.className='icon-btn'+(_paused?' paused':'');
  }catch(e){console.warn('control',e);}
}

/* ── TICK ── */
function tick(){if(--_tick<=0){fetchStatus();fetchCandles();fetchHistory();_tick=30;}}

/* ── INIT ── */
initCandleHover();
fetchStatus();fetchCandles();fetchHistory();
initSSE();
setInterval(tick,1000);
window.addEventListener('resize',()=>{drawEquity();drawCandles();});
if('Notification' in window&&Notification.permission==='granted'){_notif=true;updateNotifBtn();}
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});
</script>
</body>
</html>"""

@_flask_app.route("/")
def _web_index():
    return _DASHBOARD_HTML

@_flask_app.route("/chart.png")
def _web_chart_png():
    trader = _web_trader_ref[0] if _web_trader_ref else None
    pair = entry = entry_side = trail_stop = None
    if trader and trader.positions:
        snap = dict(trader.positions)
        if snap:
            pair, pos  = next(iter(snap.items()))
            entry      = pos["entry"]
            entry_side = pos["side"]
            trail_stop = pos.get("trail_stop")
    if not pair:
        pair = _current_coin.get("pair")
    if not pair:
        return _Response("No data yet — bot is still starting up.", status=503, mimetype="text/plain")
    buf = _make_price_chart(pair, entry=entry, entry_side=entry_side, trail_stop=trail_stop)
    if not buf:
        return _Response("Chart unavailable.", status=503, mimetype="text/plain")
    return _Response(buf.read(), mimetype="image/png",
                     headers={"Cache-Control": "no-store, no-cache"})

@_flask_app.route("/status")
def _web_status():
    trader = _web_trader_ref[0] if _web_trader_ref else None
    if not trader:
        return _Response('{"error":"not ready"}', status=503, mimetype="application/json")
    try:
        day_pnl     = round(trader.balance - trader.day_start_bal, 2)
        session_pnl = round(trader.balance - trader.session_start, 2)
    except Exception:
        day_pnl = session_pnl = 0.0

    open_pos = []
    for pr, p in dict(trader.positions).items():
        try:
            cur_price = get_price(pr)
            upnl = round(trader.unrealized_pnl(cur_price, pr), 2)
            move_pct = round((cur_price - p["entry"]) / p["entry"] * 100
                             * (1 if p["side"] == "LONG" else -1), 2)
        except Exception:
            upnl = 0.0; move_pct = 0.0; cur_price = p["entry"]
        held = round((time.time() - p.get("opened_at", time.time())) / 60, 0)
        open_pos.append({
            "name":           p["name"],
            "side":           p["side"],
            "entry":          p["entry"],
            "leverage":       p.get("leverage", 1),
            "contract_tier":  p.get("contract_tier", ""),
            "unrealized_pnl": upnl,
            "move_pct":       move_pct,
            "trail_stop":     round(p.get("trail_stop", 0), 4),
            "confidence":     round(p.get("confidence", 0) * 100),
            "held_mins":      int(held),
        })

    wins_streak  = trader.consecutive_wins
    losses_streak = trader.consecutive_losses
    streak = wins_streak if wins_streak > 0 else -losses_streak

    recent = []
    for t in reversed(trader.trades[-10:]):
        recent.append({
            "coin":      t.get("coin", ""),
            "side":      t.get("side", ""),
            "pnl":       round(t.get("pnl", 0), 2),
            "held_mins": round(t.get("held_mins", 0)),
            "reason":    t.get("reason", ""),
            "ts":        int(t.get("closed_at", t.get("ts", 0)) * 1000),
        })

    coin_stats: dict = {}
    for t in list(trader.trades):
        cn = t.get("coin", "")
        if not cn:
            continue
        if cn not in coin_stats:
            coin_stats[cn] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if t["pnl"] > 0:
            coin_stats[cn]["wins"] += 1
        else:
            coin_stats[cn]["losses"] += 1
        coin_stats[cn]["pnl"] = round(coin_stats[cn]["pnl"] + t["pnl"], 2)

    with _state_lock:
        paused_now = _paused

    rank     = get_rank(trader.balance)
    next_rnk = get_next_rank(trader.balance)
    rank_range = next_rnk["min"] - rank["min"]
    rank_pct   = round(min(100.0, (trader.balance - rank["min"]) / max(rank_range, 0.01) * 100), 1)

    payload = {
        "balance":        round(trader.balance, 2),
        "peak":           round(trader.peak, 2),
        "day_pnl":        day_pnl,
        "session_pnl":    session_pnl,
        "win_rate":       round(trader.win_rate, 1),
        "trades":         len(trader.trades),
        "wins":           trader.wins,
        "losses":         len(trader.trades) - trader.wins,
        "profit_factor":  round(
            sum(t["pnl"] for t in trader.trades if t["pnl"] > 0) /
            max(abs(sum(t["pnl"] for t in trader.trades if t["pnl"] < 0)), 0.01), 2
        ) if trader.trades else None,
        "avg_win":        round(
            sum(t["pnl"] for t in trader.trades if t["pnl"] > 0) /
            max(sum(1 for t in trader.trades if t["pnl"] > 0), 1), 2
        ) if trader.trades else None,
        "avg_loss":       round(
            abs(sum(t["pnl"] for t in trader.trades if t["pnl"] < 0)) /
            max(sum(1 for t in trader.trades if t["pnl"] < 0), 1), 2
        ) if trader.trades else None,
        "positions":      len(trader.positions),
        "coin":           _current_coin.get("name", "—"),
        "pair":           _current_coin.get("pair", ""),
        "open_positions": open_pos,
        "streak":         streak,
        "recent_trades":  recent,
        "mode":           "LIVE" if LIVE_MODE else "PAPER",
        "exchange":       LIVE_EXCHANGE if LIVE_MODE else EXCHANGE,
        "paused":         paused_now,
        "coin_stats":     coin_stats,
        "rank":           rank["name"],
        "rank_emoji":     rank["emoji"],
        "next_rank":      next_rnk["name"],
        "next_rank_emoji": next_rnk["emoji"],
        "next_rank_min":  next_rnk["min"],
        "rank_progress":  rank_pct,
        "learning":       db.connected or len(trader.trades) >= 3,
        "chart_pattern":        _pattern_cache.get(_current_coin.get("pair",""), {}).get("name", ""),
        "chart_pattern_signal": _pattern_cache.get(_current_coin.get("pair",""), {}).get("signal", "NONE"),
        "candle_pattern":       _pattern_cache.get(_current_coin.get("pair",""), {}).get("candle_name", ""),
        "candle_pattern_signal":_pattern_cache.get(_current_coin.get("pair",""), {}).get("candle_signal", "NONE"),
        "all_patterns": {
            p: {
                "name":          v.get("name",""),
                "signal":        v.get("signal","NONE"),
                "strength":      v.get("strength", 0.0),
                "coin":          v.get("coin",""),
                "candle_name":   v.get("candle_name",""),
                "candle_signal": v.get("candle_signal","NONE"),
            }
            for p, v in _pattern_cache.items()
            if v.get("name") or v.get("candle_name")
        },
        "activity_log": list(reversed(_activity_log[-20:])),
    }
    return _Response(json.dumps(payload), mimetype="application/json")

@_flask_app.route("/events")
def _web_events():
    q: _queue.Queue = _queue.Queue(maxsize=50)
    with _sse_lock:
        _sse_clients.append(q)

    def _stream():
        try:
            yield "retry: 5000\n\n"
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield msg
                except _queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _sse_lock:
                try:
                    _sse_clients.remove(q)
                except ValueError:
                    pass

    return _Response(_stream(), mimetype="text/event-stream",
                     headers={"Cache-Control": "no-cache",
                               "X-Accel-Buffering": "no",
                               "Connection": "keep-alive"})

@_flask_app.route("/candles")
def _web_candles():
    pair = _flask_request.args.get("pair") or _current_coin.get("pair") or ""
    if not pair:
        return _Response('{"error":"no pair"}', status=503, mimetype="application/json")
    try:
        r = requests.get(f"{BASE_URL}/OHLC",
                         params={"pair": pair, "interval": INTERVAL}, timeout=10)
        payload = r.json()
        if payload.get("error"):
            raise ValueError(str(payload["error"]))
        rkey = next(k for k in payload["result"] if k != "last")
        raw = payload["result"][rkey][-80:]
        candles = [{"t": int(c[0]), "o": float(c[1]), "h": float(c[2]),
                    "l": float(c[3]), "c": float(c[4]), "v": float(c[6])}
                   for c in raw]
        return _Response(json.dumps(candles), mimetype="application/json",
                         headers={"Cache-Control": "no-store"})
    except Exception as e:
        return _Response(json.dumps({"error": str(e)}), status=503, mimetype="application/json")

@_flask_app.route("/history")
def _web_history():
    trader = _web_trader_ref[0] if _web_trader_ref else None
    if not trader:
        return _Response("[]", mimetype="application/json")
    trades = list(trader.trades)
    if not trades:
        return _Response("[]", mimetype="application/json")
    base = round(trader.balance - sum(t["pnl"] for t in trades), 2)
    bal = base
    pts = []
    for t in trades:
        bal = round(bal + t["pnl"], 2)
        pts.append({"ts": int(t["ts"] * 1000), "balance": bal})
    return _Response(json.dumps(pts), mimetype="application/json",
                     headers={"Cache-Control": "no-store"})

@_flask_app.route("/control", methods=["POST"])
def _web_control():
    global _paused
    try:
        body = _flask_request.get_json(silent=True) or {}
        action = body.get("action", "toggle")
    except Exception:
        action = "toggle"
    with _state_lock:
        if action == "pause":
            _paused = True
        elif action == "resume":
            _paused = False
        else:
            _paused = not _paused
        now_paused = _paused
    _push_sse("control", {"paused": now_paused})
    return _Response(json.dumps({"paused": now_paused}), mimetype="application/json")

_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" fill="#060e1c"/>
<defs><linearGradient id="gb" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#0d2040"/><stop offset="1" stop-color="#060e1c"/>
</linearGradient></defs>
<rect width="512" height="512" fill="url(#gb)"/>
<!-- trend line -->
<polyline points="60,400 140,340 220,310 300,240 380,190 460,130"
  fill="none" stroke="#4a8fff" stroke-width="4" stroke-linecap="round"
  stroke-linejoin="round" opacity="0.45"/>
<!-- candle 1 bearish -->
<line x1="100" y1="290" x2="100" y2="320" stroke="#ff3352" stroke-width="6" stroke-linecap="round"/>
<rect x="78" y="320" width="44" height="80" rx="5" fill="#ff3352"/>
<line x1="100" y1="400" x2="100" y2="420" stroke="#ff3352" stroke-width="6" stroke-linecap="round"/>
<!-- candle 2 bullish -->
<line x1="188" y1="260" x2="188" y2="300" stroke="#00cc74" stroke-width="6" stroke-linecap="round"/>
<rect x="166" y="300" width="44" height="70" rx="5" fill="#00cc74"/>
<line x1="188" y1="370" x2="188" y2="395" stroke="#00cc74" stroke-width="6" stroke-linecap="round"/>
<!-- candle 3 bullish small -->
<line x1="276" y1="230" x2="276" y2="265" stroke="#00cc74" stroke-width="6" stroke-linecap="round"/>
<rect x="254" y="265" width="44" height="55" rx="5" fill="#00cc74"/>
<line x1="276" y1="320" x2="276" y2="345" stroke="#00cc74" stroke-width="6" stroke-linecap="round"/>
<!-- candle 4 big bullish -->
<line x1="364" y1="175" x2="364" y2="210" stroke="#00cc74" stroke-width="6" stroke-linecap="round"/>
<rect x="342" y="210" width="44" height="100" rx="5" fill="#00cc74"/>
<line x1="364" y1="310" x2="364" y2="335" stroke="#00cc74" stroke-width="6" stroke-linecap="round"/>
<!-- candle 5 tallest bullish -->
<line x1="452" y1="130" x2="452" y2="165" stroke="#00cc74" stroke-width="6" stroke-linecap="round"/>
<rect x="430" y="165" width="44" height="115" rx="5" fill="#00cc74"/>
<line x1="452" y1="280" x2="452" y2="310" stroke="#00cc74" stroke-width="6" stroke-linecap="round"/>
</svg>"""

def _render_icon_png(size: int) -> bytes:
    """Generate a minimal PNG of the icon at the requested size using stdlib only."""
    import struct, zlib as _zlib
    # Rasterise via simple pixel mapping from the SVG design
    bg = (6, 14, 28)          # --bg
    green = (0, 204, 116)     # --g
    red   = (255, 51, 82)     # --r
    blue  = (74, 143, 255)    # --b

    def lerp(a, b, t): return int(a + (b - a) * t)
    def gradient_bg(y, h):
        t = y / max(h - 1, 1)
        return (lerp(13, 6, t), lerp(32, 14, t), lerp(64, 28, t))

    s = size
    pixels = []
    for py in range(s):
        row = []
        for px in range(s):
            fx = px / s  # 0..1
            fy = py / s

            # Normalise to 512-space
            x512 = px * 512 / s
            y512 = py * 512 / s

            # Background gradient
            col = gradient_bg(py, s)

            # Trend line (approximate as a thick diagonal band)
            # Line from (60,400)→(460,130) in 512 space
            tx = 60 + (460 - 60) * fx
            ty = 400 + (130 - 400) * fx
            if abs(y512 - ty) < 512 / s * 3:
                col = (lerp(col[0], blue[0], 0.35), lerp(col[1], blue[1], 0.35), lerp(col[2], blue[2], 0.35))

            # Candles: (cx, body_top, body_bot, wick_top, wick_bot, color)
            candles = [
                (100, 320, 400, 290, 420, red),
                (188, 300, 370, 260, 395, green),
                (276, 265, 320, 230, 345, green),
                (364, 210, 310, 175, 335, green),
                (452, 165, 280, 130, 310, green),
            ]
            hw = 22  # half-body width in 512 space
            hw2 = 3  # half-wick width

            for (cx, bt, bb, wt, wb, cc) in candles:
                # Body
                if abs(x512 - cx) <= hw and bt <= y512 <= bb:
                    col = cc
                # Wick
                elif abs(x512 - cx) <= hw2 and wt <= y512 <= wb:
                    col = cc

            row.append(col)
        pixels.append(row)

    # Encode as PNG
    def make_chunk(tag, data):
        crc = _zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw_rows = b"".join(
        b"\x00" + bytes(c for px in row for c in px)
        for row in pixels
    )
    idat = _zlib.compress(raw_rows, 6)

    png = (b"\x89PNG\r\n\x1a\n"
           + make_chunk(b"IHDR", struct.pack(">IIBBBBB", s, s, 8, 2, 0, 0, 0))
           + make_chunk(b"IDAT", idat)
           + make_chunk(b"IEND", b""))
    return png

@_flask_app.route("/icon/svg")
def _web_icon_svg():
    return _Response(_ICON_SVG, mimetype="image/svg+xml",
                     headers={"Cache-Control": "public,max-age=86400"})

@_flask_app.route("/icon/<int:size>")
def _web_icon_png(size):
    size = min(max(size, 16), 512)
    png = _render_icon_png(size)
    return _Response(png, mimetype="image/png",
                     headers={"Cache-Control": "public,max-age=86400"})

@_flask_app.route("/manifest.json")
def _web_manifest():
    manifest = {
        "name": "CryptoBot",
        "short_name": "CryptoBot",
        "description": "Live crypto trading bot dashboard",
        "start_url": "/",
        "id": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#060e1c",
        "theme_color": "#060e1c",
        "icons": [
            {"src": "/icon/192", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon/512", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon/svg", "sizes": "any",     "type": "image/svg+xml"},
        ],
    }
    return _Response(json.dumps(manifest), mimetype="application/manifest+json")

@_flask_app.route("/sw.js")
def _web_sw():
    sw = r"""
const CACHE='cryptobot-v1';
self.addEventListener('install',e=>e.waitUntil(
  caches.open(CACHE).then(c=>c.addAll(['/'])).then(()=>self.skipWaiting())
));
self.addEventListener('activate',e=>e.waitUntil(
  caches.keys().then(keys=>Promise.all(
    keys.filter(k=>k!==CACHE).map(k=>caches.delete(k))
  )).then(()=>self.clients.claim())
));
self.addEventListener('fetch',e=>{
  const url=e.request.url;
  if(url.includes('/status')||url.includes('/events')||
     url.includes('/candles')||url.includes('/history')||
     url.includes('/control'))return;
  e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request)));
});
"""
    return _Response(sw.strip(), mimetype="application/javascript",
                     headers={"Cache-Control": "no-store"})


def _start_web_server(trader):
    import logging as _logging
    _logging.getLogger("werkzeug").setLevel(_logging.ERROR)
    _web_trader_ref.append(trader)
    port = int(os.environ.get("PORT", 8080))
    log("WEB", f"Dashboard → http://0.0.0.0:{port}")
    _flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    global _current_coin

    # ── Startup banner ────────────────────────────────────────────────────────
    BW = 56
    def _bcenter(text, col=_C.WHITE):
        pad_l = (BW - len(text)) // 2
        pad_r  = BW - len(text) - pad_l
        print(_c(_C.CYAN, "║") + " " * pad_l + _c(col, text) + " " * pad_r + _c(_C.CYAN, "║"))
    mode_label = "LIVE TRADING · spot" if LIVE_MODE else "paper trading  ·  simulation"
    print(_c(_C.CYAN, "╔" + "═" * BW + "╗"))
    _bcenter("")
    _bcenter("◈  C R Y P T O B O T  ◈", _C.CYAN + _C.BOLD)
    _bcenter("─" * 30, _C.GREY)
    _bcenter(f"{mode_label}  ·  26 pairs", _C.GREY)
    _bcenter("ATR trailing stops  ·  9-pillar confidence", _C.GREY)
    _bcenter("")
    print(_c(_C.CYAN, "╠" + "═" * BW + "╣"))
    _bcenter(datetime.now().strftime("Started  %Y-%m-%d  %H:%M  UTC"), _C.GREY)
    print(_c(_C.CYAN, "╚" + "═" * BW + "╝"))
    print()

    log("BOOT", f"Chat ID: {_c(_C.CYAN, TG_CHAT_ID)}")
    log("BOOT", f"Mode: {_c(_C.RED + _C.BOLD, 'LIVE TRADING') if LIVE_MODE else _c(_C.GREY, 'paper')}")

    # Remove any webhook so long-polling (getUpdates) works
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/deleteWebhook",
                          json={"drop_pending_updates": False}, timeout=10)
        log("BOOT", f"deleteWebhook: {r.json().get('description', 'ok')}")
    except Exception as e:
        log("BOOT", f"deleteWebhook error: {e}", "ERR")

    # Validate Kraken live keys before anything else touches the exchange
    if LIVE_MODE:
        if LIVE_EXCHANGE == "binance":
            log("BOOT", "Validating Binance API keys...")
            if not _binance_validate_keys():
                log("BOOT", "BINANCE API KEY VALIDATION FAILED — check BINANCE_API_KEY / BINANCE_API_SECRET", "ERR")
                tg("🚨 *Binance API key validation FAILED*\nCheck `BINANCE_API_KEY` and `BINANCE_API_SECRET` env vars.\n_Exiting._", plain=True)
                sys.exit(1)
            log("BOOT", _c(_C.GREEN + _C.BOLD, "Binance API keys valid — LIVE MODE active"))
        else:
            log("BOOT", "Validating Kraken API keys...")
            if not _kraken_validate_keys():
                log("BOOT", "KRAKEN API KEY VALIDATION FAILED — check KRAKEN_API_KEY / KRAKEN_API_SECRET", "ERR")
                tg("🚨 *Kraken API key validation FAILED*\nCheck `KRAKEN_API_KEY` and `KRAKEN_API_SECRET` env vars.\n_Exiting._", plain=True)
                sys.exit(1)
            log("BOOT", _c(_C.GREEN + _C.BOLD, "Kraken API keys valid — LIVE MODE active"))

    # Plain-text ping — proves connectivity before any markdown
    ok = tg(f"CryptoBot booting... (chat_id={TG_CHAT_ID})", plain=True)
    log("BOOT", f"Telegram ping: {_c(_C.GREEN, 'OK') if ok else _c(_C.RED, 'FAILED')}")

    trader = PaperTrader()

    if LIVE_MODE:
        _exch_disp = "Binance" if LIVE_EXCHANGE == "binance" else "Kraken"
        _fee_disp  = "0.10%" if LIVE_EXCHANGE == "binance" else "0.26%"
        tg(f"🔴 *LIVE TRADING MODE — real money on {_exch_disp}*\n"
           f"Balance: `${trader.balance:.2f}` USD\n"
           f"Exchange fee: `{_fee_disp}` per trade\n"
           f"Strategy: spot BUY only · 11-pillar confidence · 15+ entry gates\n"
           f"⚠️ _This bot will place real orders. Ensure balance is intentional._")

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
        ("LSR monitor",       _lsr_loop,            ()),
        ("Open interest",     _oi_loop,             ()),
        ("Econ calendar",     _econ_calendar_loop,  ()),
        ("Price alerts",      _alert_loop,          (trader,)),
        ("Web dashboard",     _start_web_server,    (trader,)),
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
