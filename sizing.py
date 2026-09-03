#!/usr/bin/env python3
"""SURVIVAL SIZING — pure functions, no DB, no network, no bot_server import.

The order of priorities is fixed: survive first, compound second, optimize
never (optimization is where accounts die). Every function here returns its
reasoning; size_position returns a full audit dict with EVERY term named, so
a trade ticket can print exactly why the size is what it is — or why it is
zero.

Design rules:
  - degenerate inputs (n=0, zero vol, negative equity, hwm=0) never raise —
    they size to ZERO and say why. Unknown risk is not an invitation.
  - Kelly is estimated from a SAMPLE, so it is shrunk toward zero by
    1/sqrt(n) before use and hard-capped (default 25% of full Kelly logic's
    output, i.e. cap=0.25 of equity at risk ceiling).
  - drawdown circuit: at 15% below the high-water mark, sizing goes to zero
    (dd_circuit_open=True). Getting smaller as you lose is the only sizing
    idea with an unconditional pedigree.

WIRING CONTRACT for bot_server's _open (the next bot_server agent codes
against this; nothing here imports or patches bot_server):

    import sizing
    audit = sizing.size_position(
        equity=paper_balance_usd,      # current account equity, USD
        hwm=high_water_mark_usd,       # peak equity ever seen (>= equity)
        wins=n_winning_trades,         # closed-trade counts from the ledger
        losses=n_losing_trades,
        avg_win=avg_win_frac,          # mean winner as +fraction (0.02 = +2%)
        avg_loss=avg_loss_frac,        # mean loser magnitude as +fraction
        realized_vol_20d=vol20,        # 20d realized vol of the PAIR, frac
        target_vol=0.02,               # daily vol the position should carry
        price=entry_price,
        stop_pct=stop_distance_frac)   # entry-to-stop distance as fraction
    if audit["size_units"] > 0: place the order with audit["size_units"];
    always log the whole audit dict next to the trade (it is JSON-safe).
    audit["reasons"] lists every zeroing/clamping that fired, in order.

All fractions are fractions (0.02 = 2%), never percent points.
"""
import math

KELLY_CAP          = 0.25   # never risk more than this fraction of equity
DD_CIRCUIT         = 0.15   # 15% under HWM -> no new positions
BASE_RISK_PCT      = 0.01   # per-trade risk ceiling at zero drawdown
MAX_VOL_SCALAR     = 2.0    # vol targeting may at most double a size


# ── kelly ────────────────────────────────────────────────────────────────────
def kelly_fraction(wins, losses, avg_win, avg_loss, cap=KELLY_CAP):
    """Estimation-aware Kelly fraction of equity to risk, in [0, cap].

    kelly_raw = p - (1-p)/b  with p = wins/n, b = avg_win/avg_loss.
    Shrunk toward 0 by (1 - 1/sqrt(n)) because p and b are estimates: at
    n=1 the shrink is total (0), at n=100 it keeps 90%. Degenerate inputs
    (n=0, non-positive avg_win/avg_loss, negative counts) return 0.0.
    """
    try:
        wins, losses = int(wins), int(losses)
    except (TypeError, ValueError):
        return 0.0
    n = wins + losses
    if n <= 0 or wins < 0 or losses < 0:
        return 0.0
    if avg_win is None or avg_loss is None or avg_win <= 0 or avg_loss <= 0:
        return 0.0
    p = wins / n
    b = avg_win / avg_loss
    raw = p - (1 - p) / b
    if raw <= 0:
        return 0.0
    shrink = 1.0 - 1.0 / math.sqrt(n)
    return max(0.0, min(cap, raw * shrink))


# ── vol targeting ────────────────────────────────────────────────────────────
def vol_target_scalar(realized_vol_20d, target_vol, max_scalar=MAX_VOL_SCALAR):
    """target/realized, clamped to [0, max_scalar].

    Unmeasured or zero vol returns 0.0 — sizing UP because risk could not be
    measured is exactly backwards, so missing data refuses instead.
    """
    if realized_vol_20d is None or target_vol is None:
        return 0.0
    if realized_vol_20d <= 0 or target_vol <= 0:
        return 0.0
    return max(0.0, min(max_scalar, target_vol / realized_vol_20d))


# ── drawdown circuit + per-trade cap ─────────────────────────────────────────
def risk_caps(equity, hwm, base_risk_pct=BASE_RISK_PCT, circuit=DD_CIRCUIT):
    """-> {per_trade_risk_pct, dd_circuit_open, drawdown}.

    Drawdown = 1 - equity/hwm. The per-trade ceiling tapers LINEARLY from
    base_risk_pct at 0 drawdown to zero at the circuit (15%); at or past the
    circuit dd_circuit_open=True and the ceiling is 0. Non-positive equity or
    hwm opens the circuit outright (drawdown reported as None: unmeasurable).
    """
    if equity is None or hwm is None or equity <= 0 or hwm <= 0:
        return {"per_trade_risk_pct": 0.0, "dd_circuit_open": True,
                "drawdown": None}
    dd = max(0.0, 1.0 - equity / hwm)
    if dd >= circuit:
        return {"per_trade_risk_pct": 0.0, "dd_circuit_open": True,
                "drawdown": dd}
    return {"per_trade_risk_pct": base_risk_pct * (1.0 - dd / circuit),
            "dd_circuit_open": False, "drawdown": dd}


# ── composition ──────────────────────────────────────────────────────────────
def size_position(equity, hwm, wins, losses, avg_win, avg_loss,
                  realized_vol_20d, target_vol, price, stop_pct,
                  base_risk_pct=BASE_RISK_PCT, kelly_cap=KELLY_CAP,
                  circuit=DD_CIRCUIT, max_vol_scalar=MAX_VOL_SCALAR):
    """Compose caps -> kelly -> vol target into one audited size.

    risk_fraction = min(kelly_shrunk_capped, per_trade_ceiling) * vol_scalar,
    re-clamped to the ceiling (vol targeting may shrink risk freely but may
    never push it back over the drawdown-tapered cap). risk_dollars =
    equity * risk_fraction; notional = risk_dollars / stop_pct;
    size_units = notional / price.

    Returns a dict naming EVERY term. reasons[] records each zeroing/clamp.
    Never raises on degenerate input — it sizes 0 and says why.
    """
    reasons = []
    caps = risk_caps(equity, hwm, base_risk_pct, circuit)
    if caps["dd_circuit_open"]:
        reasons.append("dd_circuit_open: "
                       + ("equity/hwm not positive" if caps["drawdown"] is None
                          else f"drawdown {caps['drawdown']*100:.1f}% >= "
                               f"circuit {circuit*100:.0f}%"))
    kelly = kelly_fraction(wins, losses, avg_win, avg_loss, cap=kelly_cap)
    n = (wins or 0) + (losses or 0)
    if kelly == 0.0:
        reasons.append("kelly=0: no positive-expectancy estimate "
                       f"(n={n}, avg_win={avg_win}, avg_loss={avg_loss})")
    vol_scalar = vol_target_scalar(realized_vol_20d, target_vol,
                                   max_scalar=max_vol_scalar)
    if vol_scalar == 0.0:
        reasons.append(f"vol_scalar=0: unmeasured/zero vol "
                       f"(realized={realized_vol_20d}, target={target_vol})")

    pre_vol = min(kelly, caps["per_trade_risk_pct"])
    if 0 < caps["per_trade_risk_pct"] < kelly:
        reasons.append(f"kelly {kelly:.4f} clamped to per-trade cap "
                       f"{caps['per_trade_risk_pct']:.4f}")
    risk_fraction = min(pre_vol * vol_scalar, caps["per_trade_risk_pct"])
    if pre_vol * vol_scalar > caps["per_trade_risk_pct"] > 0:
        reasons.append("vol scaling capped at per-trade ceiling")

    risk_dollars = (equity or 0.0) * risk_fraction if equity and equity > 0 else 0.0
    if stop_pct is None or stop_pct <= 0:
        notional = 0.0
        if risk_fraction > 0:
            reasons.append(f"stop_pct={stop_pct}: no stop distance, no size")
    else:
        notional = risk_dollars / stop_pct
    if price is None or price <= 0:
        size_units = 0.0
        if notional > 0:
            reasons.append(f"price={price}: cannot convert notional to units")
    else:
        size_units = notional / price

    return {
        # inputs, echoed so the audit stands alone
        "equity": equity, "hwm": hwm, "wins": wins, "losses": losses,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "realized_vol_20d": realized_vol_20d, "target_vol": target_vol,
        "price": price, "stop_pct": stop_pct,
        # every derived term, named
        "drawdown": caps["drawdown"],
        "dd_circuit_open": caps["dd_circuit_open"],
        "per_trade_risk_cap": caps["per_trade_risk_pct"],
        "kelly_shrunk": kelly,
        "vol_scalar": vol_scalar,
        "risk_fraction": risk_fraction,
        "risk_dollars": risk_dollars,
        "notional": notional,
        "size_units": size_units,
        "reasons": reasons,
    }
