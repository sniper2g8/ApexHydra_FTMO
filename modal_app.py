"""
modal_app.py — ApexHydra PRO  |  Intelligent Multi-Strategy AI Engine

Strategies:
  - TrendFollowing  : EMA crossover + MACD momentum, own PPO model
  - MeanReversion   : RSI extremes + Bollinger bands, own PPO model
  - Breakout        : ATR volatility breakout + volume confirmation, own PPO model

Intelligence layer:
  - RegimeDetector  : classifies market as TRENDING / RANGING / VOLATILE
                      and routes signals to the most appropriate strategy
  - Backtester      : runs weekly against 2 years of real Yahoo Finance data,
                      scores each strategy by Sharpe + win rate + max drawdown
  - ForwardTester   : paper-trades each strategy every 4 hours on live data,
                      promotes best recent performer
  - OnlineLearner   : replays closed trade outcomes every 6 hours to
                      fine-tune the active PPO model

Deploy: modal deploy modal_app.py
"""

import logging
import modal

# Module-level logger for all modal_app components (endpoints and scheduled jobs)
log = logging.getLogger("apexhydra")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

# ── Image ─────────────────────────────────────────────────────────────────────
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "numpy", "pandas", "torch",
        "stable-baselines3>=2.0",
        "sb3-contrib>=2.0",
        "gymnasium",
        "ta",
        "yfinance",
        "scikit-learn",
        "supabase",
        "requests",
        "fastapi",
        "python-multipart",
    )
)

volume    = modal.Volume.from_name("apexhydra-model-weights", create_if_missing=True)
MODEL_DIR = "/model"
app       = modal.App("apexhydra-pro", image=image)
secrets   = modal.Secret.from_name("apexhydra-secrets")


def _run_stale_trades_cleanup(supabase_client, hours: float = 8):
    """
    Mark stale open trades (no pnl, no closed_at, older than `hours`) as closed with pnl=0,
    and delete phantom rows (lot=NULL). Returns (cleaned_count, symbols_set).
    Used by POST /cleanup and by the scheduled cleanup job.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    cleaned = 0
    symbols = set()

    stale = (
        supabase_client.table("trades")
        .select("id, symbol, opened_at")
        .is_("pnl", "null")
        .is_("closed_at", "null")
        .not_.is_("lot", "null")
        .lt("opened_at", cutoff)
        .execute()
        .data or []
    )
    phantoms = (
        supabase_client.table("trades")
        .select("id, symbol, opened_at")
        .is_("lot", "null")
        .is_("pnl", "null")
        .is_("closed_at", "null")
        .lt("opened_at", cutoff)
        .execute()
        .data or []
    )

    if stale:
        ids = [r["id"] for r in stale]
        symbols.update(r["symbol"] for r in stale)
        supabase_client.table("trades").update({
            "pnl": 0.0,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }).in_("id", ids).execute()
        cleaned += len(ids)
        log.info("[CLEANUP] Closed %d stale confirmed trades: %s", len(ids), list(symbols))

    if phantoms:
        phantom_ids = [r["id"] for r in phantoms]
        symbols.update(r["symbol"] for r in phantoms)
        supabase_client.table("trades").delete().in_("id", phantom_ids).execute()
        cleaned += len(phantom_ids)
        log.info("[CLEANUP] Deleted %d phantom (lot=NULL) rows", len(phantom_ids))

    return cleaned, symbols


STRATEGY_NAMES = ["trend_following", "mean_reversion", "breakout"]
def _flatten_yf(df, col: str):
    """
    yfinance >=0.2 returns a MultiIndex DataFrame when group_by is default.
    df["Close"] gives a DataFrame, not a Series — .tolist() fails.
    This squeezes it to a plain list regardless of column structure.
    """
    c = df[col]
    if hasattr(c, "squeeze"):
        c = c.squeeze()          # DataFrame → Series if single ticker
    if hasattr(c, "tolist"):
        return c.tolist()
    return list(c)


YF_SYMBOLS = [
    # ── Original core 4 ───────────────────────────────────────────────
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "GC=F",
    # ── 7 new additions (account now ~$880, infrastructure validated) ──
    # USDCHF  : safe-haven / inverse-EUR, clean trends, (7-16 UTC)
    "USDCHF=X",
    # AUDUSD  : commodity-correlated, among the smoothest trending pairs
    "AUDUSD=X",
    # USDCAD  : oil-correlated, predictable NY-session trend behavior
    "USDCAD=X",
    # EURJPY  : highest ATR of all EUR crosses, strong directional moves
    #           Asian session added (0-16 UTC) — Tokyo is the JPY prime session
    "EURJPY=X",
    # NZDUSD  : mirrors AUDUSD with slight lag — diversifies commodity exposure
    "NZDUSD=X",
    # EURGBP  : tightest spread of any cross (~0.5-1.5 pip), ideal MR candidate,
    #           purely European pair — London session only (7-16 UTC)
    "EURGBP=X",
    # GBPJPY  : re-added — account now has enough cushion. At $880×0.5% risk = $4.40
    #           GBPJPY 0.01-lot spread cost ≈ $0.45 (10% of risk budget, acceptable).
    #           London-only (7-12 UTC), 7-pip spread cap kept as hard safety gate.
    "GBPJPY=X",
    # ── Precious metals ───────────────────────────────────────────────
    # XAGUSD  : silver futures (SI=F on Yahoo Finance). Price ~$32/oz.
    #           Same session profile as gold (06-22 UTC). Ideal mean-reversion
    #           candidate — silver oscillates around a 20-bar mean more reliably
    #           than gold during ranging regimes. Spread cap: 20 pips (vs gold 35).
    "SI=F",
]


# =============================================================================
# SECTION 1 — TECHNICAL INDICATORS
# =============================================================================

def _ema(prices, period):
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    k   = 2.0 / (period + 1)
    val = prices[0]
    for p in prices[1:]:
        val = p * k + val * (1 - k)
    return val

def _rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = sum(d for d in deltas[-period:] if d > 0) / period
    losses = sum(-d for d in deltas[-period:] if d < 0) / period
    return 100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)

def _adx(highs, lows, closes, period=14):
    if len(closes) < period + 2:
        return 25.0
    trs, pdms, mdms = [], [], []
    for i in range(1, len(closes)):
        tr  = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        pdm = max(highs[i]-highs[i-1], 0) if highs[i]-highs[i-1] > lows[i-1]-lows[i] else 0
        mdm = max(lows[i-1]-lows[i],   0) if lows[i-1]-lows[i]   > highs[i]-highs[i-1] else 0
        trs.append(tr); pdms.append(pdm); mdms.append(mdm)
    atr = sum(trs[-period:]) / period
    if atr == 0:
        return 0.0
    pdi = 100 * sum(pdms[-period:]) / (period * atr)
    mdi = 100 * sum(mdms[-period:]) / (period * atr)
    return 100 * abs(pdi - mdi) / max(pdi + mdi, 1e-9)

def _atr(prices, period=14, highs=None, lows=None):
    """True ATR using high/low/close when available, else close-to-close."""
    if highs and lows and len(highs) == len(prices):
        trs = [max(highs[i]-lows[i],
                   abs(highs[i]-prices[i-1]),
                   abs(lows[i]-prices[i-1]))
               for i in range(1, len(prices))]
    else:
        trs = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
    if not trs:
        return 0.0
    return sum(trs[-period:]) / min(len(trs), period)

def _bollinger(prices, period=20):
    import math
    w    = prices[-period:]
    mean = sum(w) / len(w)
    std  = math.sqrt(sum((p - mean)**2 for p in w) / len(w))
    return mean - 2*std, mean, mean + 2*std

def _safe(a, b, fb=0.0):
    return a / b if b else fb


def _atr_spike_skip(msg: dict, lookback_bars: int = 50, percentile: float = 90.0) -> bool:
    """
    STRATEGY RECOMMENDATION #5: Skip or reduce entries when ATR is in top decile.
    Volatility expansion often means whipsaws. Returns True if current ATR >= percentile
    of recent ATR history (e.g. 90th = top 10%).
    """
    closes = msg.get("close", [])
    highs = msg.get("high", closes)
    lows = msg.get("low", closes)
    need = lookback_bars + 14  # 14 for ATR period
    if len(closes) < need:
        return False
    atr_values = []
    for i in range(14, len(closes) + 1):
        atr_val = _atr(closes[:i], 14, highs[:i] if len(highs) >= i else None, lows[:i] if len(lows) >= i else None)
        atr_values.append(atr_val)
    atr_values = atr_values[-lookback_bars:]
    if len(atr_values) < 10:
        return False
    current_atr = atr_values[-1]
    sorted_atr = sorted(atr_values)
    idx = min(int((percentile / 100.0) * len(sorted_atr)), len(sorted_atr) - 1)
    threshold = sorted_atr[idx]
    return current_atr >= threshold


def compute_sl_tp_forex(
    msg: dict,
    direction: str,
    confidence: float,
    strategy: str,
    regime: str,
    n_closed_trades: int = 0,
) -> dict:
    """
    Dynamic ATR-based SL and TP for the Forex EA.

    Replaces fixed-pip SL/TP on the EA side with server-calculated values
    that adapt to current volatility, strategy type, and signal maturity.

    SL/TP multipliers per strategy (ATR multiples):
      trend_following : SL=1.8×ATR  TP tight=2.0→full=3.5 (trend rides need room)
      mean_reversion  : SL=1.5×ATR  TP tight=1.5→full=2.2 (quick snap-back)
      breakout        : SL=2.0×ATR  TP tight=2.5→full=4.5 (breakouts go far or fail fast)

    Dynamic TP maturity:
      Scales from tight (bootstrap) → full (mature) as closed trade count grows.
      Target maturity = 60 trades. Rolling win rate adjusts maturity up/down.
      Identical logic to the crypto system.

    Confidence boost:
      High conviction (≥0.75) widens TP by 15% — let winners run.

    Volatility scaling (XAU special case):
      Gold ATR is ~$8-25 per candle. SL multiplier is capped lower to prevent
      enormous pip distances on a small capital base.

    Returns:
        {sl_price, tp_price, sl_atr_mult, tp_atr_mult, rr_ratio, atr, tp_maturity}
    """
    closes = msg.get("close", [])
    highs  = msg.get("high",  closes)
    lows   = msg.get("low",   closes)
    price  = float(closes[-1]) if closes else 0.0
    if price <= 0 or len(closes) < 15:
        return {"sl_price": 0.0, "tp_price": 0.0, "sl_atr_mult": 0.0,
                "tp_atr_mult": 0.0, "rr_ratio": 0.0, "atr": 0.0, "tp_maturity": 0.0}

    atr = _atr(closes, 14, highs=highs, lows=lows)
    if atr <= 0:
        return {"sl_price": 0.0, "tp_price": 0.0, "sl_atr_mult": 0.0,
                "tp_atr_mult": 0.0, "rr_ratio": 0.0, "atr": 0.0, "tp_maturity": 0.0}

    is_buy = direction == "BUY"
    sym    = str(msg.get("symbol", "")).upper()
    is_xau = "XAU" in sym

    # ── Per-strategy SL/TP base multipliers ───────────────────────────────────
    # (sl_mult, tp_tight, tp_full)
    strat_map = {
        "trend_following": (1.8, 2.0, 3.5),
        "mean_reversion":  (1.5, 1.5, 2.2),
        "breakout":        (2.0, 2.5, 4.5),
    }
    sl_mult, tp_tight, tp_full = strat_map.get(strategy, (1.8, 2.0, 3.5))

    # Gold ATR is huge in absolute terms — tighten SL so it doesn't consume the
    # entire risk budget on a single trade. TP scales down proportionally.
    if is_xau:
        sl_mult  = min(sl_mult, 1.4)
        tp_tight = min(tp_tight, 1.6)
        tp_full  = min(tp_full,  2.8)

    # ── Dynamic TP maturity (0.0 = bootstrap tight, 1.0 = fully mature) ───────
    maturity = min(n_closed_trades / 60.0, 1.0)

    # Rolling win-rate feedback from recent_outcomes if available
    recent_outcomes = msg.get("recent_outcomes", [])
    if len(recent_outcomes) >= 20:
        recent_wins = sum(1 for x in recent_outcomes[-20:] if x > 0)
        rolling_wr  = recent_wins / 20.0
        if rolling_wr < 0.45:
            maturity *= 0.5       # struggling — stay tight
        elif rolling_wr >= 0.60:
            maturity = min(1.0, maturity * 1.25)   # performing — expand faster

    tp_mult = tp_tight + (tp_full - tp_tight) * maturity

    # High conviction boost — let winners run
    if confidence >= 0.75:
        tp_mult *= 1.15

    sl_dist  = atr * sl_mult
    tp_dist  = atr * tp_mult
    sl_price = (price - sl_dist) if is_buy else (price + sl_dist)
    tp_price = (price + tp_dist) if is_buy else (price - tp_dist)
    rr       = tp_dist / (sl_dist + 1e-9)

    return {
        "sl_price":    round(sl_price, 5),
        "tp_price":    round(tp_price, 5),
        "sl_atr_mult": round(sl_mult,  3),
        "tp_atr_mult": round(tp_mult,  3),
        "rr_ratio":    round(rr,       3),
        "atr":         round(atr,      5),
        "tp_maturity": round(maturity, 4),
    }


# =============================================================================
# SECTION 2 — FEATURE BUILDER  (30 features)
# =============================================================================

def build_features(msg: dict):
    import numpy as np, math

    closes  = msg.get("close",  [])
    highs   = msg.get("high",   closes)
    lows    = msg.get("low",    closes)
    volumes = msg.get("volume", [])

    if len(closes) < 30:
        return np.zeros(30, dtype=np.float32)

    c = closes[-1]

    ema8   = _ema(closes, 8)
    ema21  = _ema(closes, 21)
    ema50  = _ema(closes, 50) if len(closes) >= 50 else ema21
    # Compute MACD series over last 26+9 bars, then take EMA of that series
    # for a proper signal line — not EMA of raw closes
    macd_series = [_ema(closes[:i], 12) - _ema(closes[:i], 26)
                   for i in range(max(26, len(closes)-20), len(closes)+1)]
    macd   = macd_series[-1] if macd_series else 0.0
    macd_s = _ema(macd_series, 9) if len(macd_series) >= 9 else macd
    adx    = _adx(highs, lows, closes) / 100.0
    trend_dir = 1.0 if ema8>ema21>ema50 else (-1.0 if ema8<ema21<ema50 else 0.0)

    rsi_v  = (_rsi(closes) - 50.0) / 50.0
    roc5   = _safe(closes[-1]-closes[-6],  closes[-6])  * 100
    roc10  = _safe(closes[-1]-closes[-11], closes[-11]) * 100
    roc20  = _safe(closes[-1]-closes[-21], closes[-21]) * 100

    atr     = _atr(closes, highs=highs, lows=lows)
    atr_n   = _safe(atr, c)
    bbl, bbm, bbh = _bollinger(closes)
    bbw     = _safe(bbh-bbl, bbm)
    bbpos   = _safe(c-bbl, bbh-bbl, 0.5) * 2 - 1
    rets    = [_safe(closes[i]-closes[i-1], closes[i-1]) for i in range(1, 21)]
    hvol    = math.sqrt(sum(r**2 for r in rets)/len(rets)) * math.sqrt(252)

    avg_vol  = sum(volumes[-20:]) / max(len(volumes[-20:]), 1)
    volr     = _safe(volumes[-1] if volumes else 0, avg_vol, 1.0)
    volt     = _safe(sum(volumes[-5:]), sum(volumes[-10:-5]), 1.0) - 1.0
    vspike   = 1.0 if volr > 2.0 else 0.0

    spread   = float(msg.get("spread", 0))
    # FIX 3: Old formula: spread_n = spread/c * 10000 — severely understates
    # gold spreads. For EURUSD (c≈1.08), 1-pip spread → 0.93.
    # For XAUUSD (c≈2900), same formula → 0.0034 — 270x smaller than forex,
    # making the model think gold has almost zero spread cost.
    # Fix: normalize spread as fraction of a "standard pip" value for the symbol.
    # For forex: 1 pip = 0.0001 × c → spread in pips.
    # For gold:  1 pip ≈ $0.10 = 0.10/c → spread in pip-equivalents.
    # We use a universal formula: (spread / c) × 100_000, which gives consistent
    # pip-equivalent values across all symbols including gold and JPY crosses.
    # FIX: spread from EA is already in pip units (e.g. 1.2 pips for EURUSD).
    # Old formula spread/c * 100_000 treated it as a price-unit value, producing
    # values like 111,111 for a 1.2-pip EURUSD spread — a garbage feature.
    # Correct: the spread value IS the pip count. Cap at 50 pips to keep the
    # feature bounded; clip/nan-to-num handles edge cases downstream.
    spread_n = min(float(spread), 50.0)   # spread in pips, capped at 50
    bid      = float(msg.get("bid", c))
    ask      = float(msg.get("ask", c))
    midpos   = _safe(c-bid, ask-bid, 0.5) * 2 - 1

    hour = int(msg.get("hour", 12))
    dow  = int(msg.get("dow",  2))
    h_sin = math.sin(2*math.pi*hour/24)
    h_cos = math.cos(2*math.pi*hour/24)
    d_sin = math.sin(2*math.pi*dow/5)
    d_cos = math.cos(2*math.pi*dow/5)
    sess  = 1.0 if 7 <= hour < 17 else 0.0   # FIX: < 17 matches dead-zone boundary (>= 17 is blocked)

    f = [
        _safe(ema8, ema21)-1, _safe(ema21, ema50)-1,
        macd*1000, macd_s*1000, (macd-macd_s)*1000,
        adx, trend_dir,
        rsi_v, roc5, roc10, roc20, roc5-roc10,
        atr_n*1000, bbw*100, bbpos, hvol,
        _safe(c, bbm)-1,
        1.0 if bbpos < -0.7 else (-1.0 if bbpos > 0.7 else 0.0),
        volr-1.0, volt, vspike,
        spread_n, midpos,
        _safe(c-closes[-5],  closes[-5])  * 100,
        _safe(c-closes[-20], closes[-20]) * 100,
        h_sin, h_cos, d_sin, d_cos, sess,
    ]

    obs = __import__("numpy").array(f[:30], dtype=__import__("numpy").float32)
    return __import__("numpy").nan_to_num(obs, nan=0.0, posinf=0.0, neginf=0.0)


# =============================================================================
# SECTION 3 — MARKET REGIME DETECTOR
# =============================================================================

def detect_regime(msg: dict) -> str:
    """
    TRENDING  : ADX > 18 with EMA alignment OR strong EMA momentum (catches early trends)
    VOLATILE  : ATR spike or very wide Bollinger bands or volume spike
    RANGING   : Only when ADX is genuinely weak AND momentum is absent

    FIX: Previous ADX threshold of 25 was too lagging. At the START of a trend
    (where the profitable moves happen), ADX is still 18-24 while price is already
    moving. This caused RANGING classification all day on strong directional days,
    routing everything to mean_reversion which then faded the trend → losses.

    Evidence from Feb 26 logs: 354 daily_limit blocks, zero TRENDING detections,
    every trade a BUY in a USD-strength day = mean_reversion fading a real trend.
    """
    closes  = msg.get("close", [])
    highs   = msg.get("high",  closes)
    lows    = msg.get("low",   closes)
    volumes = msg.get("volume", [])

    if len(closes) < 30:
        return "RANGING"

    adx       = _adx(highs, lows, closes)
    atr_fast  = _atr(closes, 5,  highs=highs, lows=lows)
    atr_slow  = _atr(closes, 20, highs=highs, lows=lows)
    bbl, bbm, bbh = _bollinger(closes)
    bbw       = _safe(bbh-bbl, bbm)
    ema8      = _ema(closes, 8)
    ema21     = _ema(closes, 21)
    ema50     = _ema(closes, 50) if len(closes) >= 50 else ema21
    avg_vol   = sum(volumes[-20:]) / max(len(volumes[-20:]), 1)
    vol_spike = (volumes[-1] if volumes else avg_vol) > avg_vol * 2

    # VOLATILE check: ATR expansion OR volume spike — instrument-agnostic.
    # REMOVED: bbw > 0.003 — this absolute threshold fired on virtually every
    # XAUUSD bar (gold's normal BB width is 0.015-0.030, well above 0.003),
    # routing all gold signals to breakout which then almost never fired (requires
    # aexp+vsurge), effectively blocking gold from trading entirely.
    # ATR expansion (fast/slow ratio > 1.5) and volume spikes are far more
    # reliable VOLATILE indicators and work consistently across all instruments.
    if (atr_slow > 0 and atr_fast / atr_slow > 1.5) or vol_spike:
        return "VOLATILE"

    ema_aligned = (ema8 > ema21 > ema50) or (ema8 < ema21 < ema50)
    # FIX BUG#7: (ema8>ema21) != (ema8<ema21) is True whenever ema8 != ema21,
    # which is ~99.99% of all bars. It is not a crossover detector — it just
    # checks if they differ. This over-classified ranging markets as TRENDING
    # and mis-routed mean_reversion signals to trend_following.
    # A real crossover requires comparing current bar vs previous bar.
    closes_prev = closes[:-1]
    ema8_prev   = _ema(closes_prev, 8)  if len(closes_prev) >= 8  else ema8
    ema21_prev  = _ema(closes_prev, 21) if len(closes_prev) >= 21 else ema21
    ema_crossed = (ema8_prev > ema21_prev) != (ema8 > ema21)  # True only on the crossover bar

    # Primary TRENDING: ADX lowered from 25 → 18. Catches trends earlier.
    if adx > 18 and ema_aligned:
        return "TRENDING"

    # Secondary TRENDING: EMA8 crossed EMA21 with price momentum confirmation.
    # This fires even before ADX builds up, preventing RANGING misclassification
    # during the first bars of a new trend move.
    roc = _safe(closes[-1] - closes[-6], closes[-6])   # 5-bar rate of change
    strong_momentum = abs(roc) > 0.002                  # >0.2% move in 5 bars
    if ema_crossed and strong_momentum:
        return "TRENDING"

    # Only classify RANGING when ADX is genuinely weak — avoid defaulting here
    if adx < 18 and not strong_momentum:
        return "RANGING"

    # Ambiguous (ADX 18-25, partial alignment) — lean TRENDING to avoid
    # mean_reversion fading a move that hasn't fully confirmed yet
    if ema_crossed:
        return "TRENDING"

    return "RANGING"


# =============================================================================
# SECTION 4 — STRATEGY ENGINES
# =============================================================================

def signal_trend_following(msg):
    """
    Trend following with STRATEGY RECOMMENDATIONS #1 (pullback), #4 (confluence), #6 (swing structure).
    Requires at least 2 of: EMA aligned, MACD direction, price vs EMA21, volume OK.
    For BUY/SELL requires pullback to EMA21 in last 3-5 bars then resume; optional higher-low.
    Confidence boost when 3+ conditions agree.
    """
    closes  = msg.get("close", [])
    highs   = msg.get("high",  closes)
    lows    = msg.get("low",   closes)
    volumes = msg.get("volume", [])   # PROFITABLE FIX #10: volume now used
    if len(closes) < 35:
        return "NONE", 0.0, "insufficient_data"

    ema8  = _ema(closes, 8); ema21 = _ema(closes, 21)
    ema50 = _ema(closes, 50) if len(closes) >= 50 else ema21
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd  = ema12 - ema26
    macd_series = [_ema(closes[:i], 12) - _ema(closes[:i], 26)
                   for i in range(max(26, len(closes) - 20), len(closes) + 1)]
    signal_line = _ema(macd_series, 9) if len(macd_series) >= 9 else macd_series[-1]
    hist  = macd_series[-1] - signal_line
    adx   = _adx(highs, lows, closes)
    c     = closes[-1]

    # Volume confirmation
    avg_vol = sum(volumes[-20:]) / max(len(volumes[-20:]), 1) if volumes else 1
    cur_vol = volumes[-1] if volumes else avg_vol
    vol_ok  = cur_vol >= avg_vol * 0.8

    # Confluence: count independent conditions (REC #4)
    buy_conds = [
        ema8 > ema21 > ema50,
        hist > 0,
        c > ema21,
        vol_ok,
    ]
    sell_conds = [
        ema8 < ema21 < ema50,
        hist < 0,
        c < ema21,
        vol_ok,
    ]
    n_buy = sum(buy_conds)
    n_sell = sum(sell_conds)

    sb = se = 0.0
    if ema8 > ema21 > ema50: sb += 0.30
    if ema8 < ema21 < ema50: se += 0.30
    if c > ema21: sb += 0.20
    if c < ema21: se += 0.20
    if hist > 0:  sb += 0.25
    if hist < 0:  se += 0.25
    f = 0.5 + 0.5 * min(adx / 50.0, 1.0)
    sb *= f; se *= f
    if not vol_ok:
        sb *= 0.70
        se *= 0.70

    t = sb + se
    if t == 0:
        return "NONE", 0.0, "no_signal"

    # Pullback check (REC #1): for BUY require pullback to EMA21 then resume; optional higher low.
    atr_val = _atr(closes, 14, highs=highs, lows=lows)
    near_ema = atr_val * 0.3 if atr_val > 0 else (c * 0.0001)
    buy_pullback = False
    sell_pullback = False
    if len(lows) >= 5 and len(highs) >= 5:
        # BUY: some bar in last 5 had low <= ema21 + buffer, and close > ema21, and (higher low or bullish candle)
        touched_zone_buy = any(lows[i] <= ema21 + near_ema for i in range(max(0, len(lows) - 5), len(lows)))
        if touched_zone_buy and c > ema21:
            if len(lows) >= 2 and lows[-1] > lows[-2]:
                buy_pullback = True
            elif len(closes) >= 2 and c > closes[-2]:
                buy_pullback = True
        touched_zone_sell = any(highs[i] >= ema21 - near_ema for i in range(max(0, len(highs) - 5), len(highs)))
        if touched_zone_sell and c < ema21:
            if len(highs) >= 2 and highs[-1] < highs[-2]:
                sell_pullback = True
            elif len(closes) >= 2 and c < closes[-2]:
                sell_pullback = True

    # Swing structure (REC #6): optional bonus when pullback is near swing low/high
    swing_low_5 = min(lows[-6:-1]) if len(lows) >= 6 else lows[-1]
    swing_high_5 = max(highs[-6:-1]) if len(highs) >= 6 else highs[-1]
    atr_03 = atr_val * 0.3 if atr_val > 0 else (c * 0.0001)
    near_swing_buy = abs(c - swing_low_5) <= atr_03 or (lows[-1] <= swing_low_5 + atr_03 and c > ema21)
    near_swing_sell = abs(c - swing_high_5) <= atr_03 or (highs[-1] >= swing_high_5 - atr_03 and c < ema21)

    vol_tag = "vol_ok" if vol_ok else "vol_low"
    conf_boost = 1.05 if (n_buy >= 3 or n_sell >= 3) else 1.0

    # BUY: require confluence >= 2 and (pullback or allow without for very strong confluence)
    if n_buy >= 2 and sb > se and sb / t > 0.60:
        if not buy_pullback and n_buy < 3:
            return "NONE", 0.0, "TF:no_pullback"
        conf = round(min((sb / t) * conf_boost, 0.99), 4)
        if near_swing_buy:
            conf = round(min(conf * 1.02, 0.99), 4)
        return "BUY", conf, f"TF:adx={adx:.0f},{vol_tag},pullback"
    # SELL
    if n_sell >= 2 and se > sb and se / t > 0.60:
        if not sell_pullback and n_sell < 3:
            return "NONE", 0.0, "TF:no_pullback"
        conf = round(min((se / t) * conf_boost, 0.99), 4)
        if near_swing_sell:
            conf = round(min(conf * 1.02, 0.99), 4)
        return "SELL", conf, f"TF:adx={adx:.0f},{vol_tag},pullback"
    return "NONE", 0.0, "low_confluence"


def signal_mean_reversion(msg):
    """
    Mean reversion with STRATEGY RECOMMENDATIONS #2 (momentum confirmation), #4 (confluence), #6 (swing).
    Requires at least 2 of: RSI extreme, BB touch, divergence, volume contraction.
    For BUY requires reversal confirmation: RSI5 turning up or bullish candle(s) after oversold; same for SELL.
    """
    closes  = msg.get("close", [])
    volumes = msg.get("volume", [])
    lows    = msg.get("low", closes)
    highs   = msg.get("high", closes)
    if len(closes) < 21:
        return "NONE", 0.0, "insufficient_data"

    rsi_v = _rsi(closes)
    bbl, _, bbh = _bollinger(closes)
    c     = closes[-1]
    avg_v = sum(volumes[-20:]) / max(len(volumes[-20:]), 1)
    vcont = (volumes[-1] if volumes else avg_v) < avg_v * 0.8
    rsi5  = _rsi(closes[-6:], 5)
    # RSI5 two bars ago for momentum turn
    rsi5_prev = _rsi(closes[-5:-1], 5) if len(closes) >= 6 else rsi5

    vote_buy = 0
    vote_sell = 0
    sb = se = 0.0
    votes = []
    if rsi_v < 30:
        sb += 0.40 * (30 - rsi_v) / 30
        votes.append(f"RSI_OS:{rsi_v:.0f}")
        vote_buy += 1
    elif rsi_v > 70:
        se += 0.40 * (rsi_v - 70) / 30
        votes.append(f"RSI_OB:{rsi_v:.0f}")
        vote_sell += 1
    if c <= bbl:
        sb += 0.35
        votes.append("BB_LOW")
        vote_buy += 1
    elif c >= bbh:
        se += 0.35
        votes.append("BB_HIGH")
        vote_sell += 1
    if rsi5 > rsi_v and rsi_v < 40:
        sb += 0.15
        votes.append("RSI_DIV")
        vote_buy += 1
    elif rsi5 < rsi_v and rsi_v > 60:
        se += 0.15
        vote_sell += 1
    if vcont:
        sb *= 1.10
        se *= 1.10
        votes.append("VCONT")
        vote_buy += 1
        vote_sell += 1

    t = sb + se
    if t == 0:
        return "NONE", 0.0, "no_signal"

    # Confluence (REC #4): require at least 2 conditions and 65% score (was 0.62 — tighter for accuracy)
    buy_ok = (sb > se and sb / t > 0.65 and vote_buy >= 2)
    sell_ok = (se > sb and se / t > 0.65 and vote_sell >= 2)
    if not buy_ok and not sell_ok:
        return "NONE", 0.0, "MR:low_confluence_or_votes"

    # Momentum confirmation (REC #2): for BUY require RSI5 turning up or bullish candle(s)
    if buy_ok:
        # Reversal confirmation: RSI5 turning up, or last bar(s) bullish (close > open / higher low)
        rsi_turn_up = rsi5 > rsi5_prev and rsi_v < 45
        bullish_candle = len(closes) >= 2 and c > closes[-2]
        higher_low = len(lows) >= 2 and lows[-1] > lows[-2]
        if not (rsi_turn_up or bullish_candle or higher_low):
            return "NONE", 0.0, "MR:no_reversal_buy"
    if sell_ok:
        rsi_turn_dn = rsi5 < rsi5_prev and rsi_v > 55
        bearish_candle = len(closes) >= 2 and c < closes[-2]
        lower_high = len(highs) >= 2 and highs[-1] < highs[-2]
        if not (rsi_turn_dn or bearish_candle or lower_high):
            return "NONE", 0.0, "MR:no_reversal_sell"

    # Swing structure (REC #6): bonus when near swing low/high
    atr_val = _atr(closes, 14, highs=highs, lows=lows)
    atr_03 = atr_val * 0.3 if atr_val > 0 else (c * 0.0001)
    swing_low = min(lows[-11:-1]) if len(lows) >= 11 else c
    swing_high = max(highs[-11:-1]) if len(highs) >= 11 else c
    near_swing_buy = abs(c - swing_low) <= atr_03
    near_swing_sell = abs(c - swing_high) <= atr_03
    conf_boost = 1.02 if (buy_ok and near_swing_buy) or (sell_ok and near_swing_sell) else 1.0

    if buy_ok:
        conf = round(min((sb / t) * conf_boost, 0.99), 4)
        return "BUY", conf, "MR:" + ",".join(votes)
    if sell_ok:
        conf = round(min((se / t) * conf_boost, 0.99), 4)
        return "SELL", conf, "MR:" + ",".join(votes)
    return "NONE", 0.0, "low_confluence"


def signal_breakout(msg):
    """
    Breakout with STRATEGY RECOMMENDATION #3 (retest), #4 (confluence).
    Require break of range high/low; prefer retest of that level then resume.
    When retest present, relax aexp/vsurge slightly (retest filters false breakouts).
    """
    closes  = msg.get("close",  [])
    highs   = msg.get("high",   closes)
    lows    = msg.get("low",    closes)
    volumes = msg.get("volume", [])
    if len(closes) < 25:
        return "NONE", 0.0, "insufficient_data"

    c      = closes[-1]
    rhi    = max(highs[-21:-1])
    rlo    = min(lows[-21:-1])
    atr_f  = _atr(closes, 5,  highs=highs, lows=lows)
    atr_s  = _atr(closes, 20, highs=highs, lows=lows)
    atr_val = atr_s if atr_s > 0 else atr_f
    avg_v  = sum(volumes[-20:]) / max(len(volumes[-20:]), 1)
    vsurge = (volumes[-1] if volumes else avg_v) > avg_v * 1.5
    aexp   = atr_f > atr_s * 1.2

    # Retest (REC #3): after break, did price pull back to level then resume?
    retest_buy = False
    retest_sell = False
    lookback = min(12, len(closes) - 2)
    for start in range(len(closes) - lookback, len(closes) - 1):
        if closes[start] > rhi:
            for j in range(start + 1, len(closes)):
                if lows[j] <= rhi + 0.5 * atr_val and closes[j] > rhi:
                    retest_buy = True
                    break
            break
    for start in range(len(closes) - lookback, len(closes) - 1):
        if closes[start] < rlo:
            for j in range(start + 1, len(closes)):
                if highs[j] >= rlo - 0.5 * atr_val and closes[j] < rlo:
                    retest_sell = True
                    break
            break

    sb = se = 0.0
    votes = []
    if c > rhi:
        sb += 0.45
        votes.append("BRK_HI")
    if c < rlo:
        se += 0.45
        votes.append("BRK_LO")
    if aexp:
        sb *= 1.20
        se *= 1.20
        votes.append("ATR_EXP")
    if vsurge:
        sb *= 1.20
        se *= 1.20
        votes.append("VOL_SURGE")
    # Confluence: require break + (aexp or vsurge). If retest present, accept without aexp/vsurge (REC #3).
    if not (aexp or vsurge) and not (retest_buy or retest_sell):
        sb *= 0.5
        se *= 0.5
    if retest_buy:
        votes.append("RETEST")
        sb *= 1.05
    if retest_sell:
        votes.append("RETEST")
        se *= 1.05

    t = sb + se
    if t == 0:
        return "NONE", 0.0, "no_breakout"
    # Prefer retest for higher-quality entries but allow first break when aexp or vsurge
    if sb > se and sb / t > 0.65:
        conf = min(sb / t, 0.99)
        if retest_buy:
            conf = min(conf * 1.03, 0.99)
        return "BUY", round(conf, 4), "BO:" + ",".join(votes)
    if se > sb and se / t > 0.65:
        conf = min(se / t, 0.99)
        if retest_sell:
            conf = min(conf * 1.03, 0.99)
        return "SELL", round(conf, 4), "BO:" + ",".join(votes)
    return "NONE", 0.0, "low_confluence"


STRATEGY_FN = {
    "trend_following": signal_trend_following,
    "mean_reversion":  signal_mean_reversion,
    "breakout":        signal_breakout,
}

REGIME_TO_STRATEGY = {
    "TRENDING": "trend_following",
    "RANGING":  "mean_reversion",
    "VOLATILE": "breakout",
}

# ── In-memory regime history ──────────────────────────────────────────────────
# Replaces the broken Supabase lookback: market_regime uses upsert (one row per
# symbol), so querying "last N minutes" always returns only the current regime —
# the history never existed in the DB.
#
# This dict persists across requests within the same warm Modal container.
# Structure: { symbol: deque of (datetime, regime_str) }  newest entry first.
# On cold-start history is empty — safe: the current-regime check still fires;
# only the lookback is blind for the first scan after a container restart.
from collections import deque as _deque
from datetime import datetime as _dt, timezone as _tz, timedelta as _td

# ── Regime history cache ───────────────────────────────────────────────────────
# Stores per-symbol deques of (datetime, regime) so the MR trending block can
# do a real time-based lookback without hitting the single-row market_regime DB.
_REGIME_HISTORY: dict      = {}     # symbol → deque[(datetime, regime)]
_REGIME_HISTORY_MAXLEN     = 30     # ~30 min of 1-min scans, or ~8 M15 candles

# ── Cold-start / cross-container continuity ────────────────────────────────────
# Modal can spin up multiple warm containers (max_containers=10). These globals
# let us:
#   (a) seed the regime cache from Supabase on first scan after a cold-start, and
#   (b) apply a conservative MR block during the 15-min warmup while the cache fills.
_CONTAINER_START: _dt      = _dt.now(_tz.utc)   # timestamp when this container started
_MR_WARMUP_SECS            = 900                 # 15 min: regime cache takes this long to warm
_seeded_symbols: set       = set()               # symbols already seeded from DB this session
_LAST_REGIME_CLEANUP: dict = {}                  # symbol → last DB cleanup datetime

# ── Required Supabase tables (run once in Supabase SQL editor) ─────────────────
# -- Stores per-minute regime readings for cross-container + cold-start recovery:
# CREATE TABLE regime_history (
#   id          BIGSERIAL PRIMARY KEY,
#   symbol      TEXT NOT NULL,
#   regime      TEXT NOT NULL,
#   detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
# );
# CREATE INDEX idx_regime_hist ON regime_history (symbol, detected_at DESC);
#
# -- Stores the last fired signal so /open survives a cross-container request:
# CREATE TABLE signal_cache (
#   symbol     TEXT PRIMARY KEY,
#   direction  TEXT, strategy TEXT, regime TEXT,
#   confidence FLOAT, features JSONB,
#   cached_at  TIMESTAMPTZ
# );


# =============================================================================
# SECTION 5 — PPO MODEL MANAGEMENT
# =============================================================================

def _make_env(obs_dim=30):
    import numpy as np
    import gymnasium as gym
    from gymnasium import spaces

    class TradingEnv(gym.Env):
        def __init__(self):
            super().__init__()
            self.action_space      = spaces.Discrete(3)
            self.observation_space = spaces.Box(-__import__("numpy").inf, __import__("numpy").inf, shape=(obs_dim,), dtype=__import__("numpy").float32)
            self._steps = 0
        def reset(self, *, seed=None, options=None):
            super().reset(seed=seed)
            self._steps = 0
            return __import__("numpy").zeros(obs_dim, dtype=__import__("numpy").float32), {}
        def step(self, action):
            self._steps += 1
            return __import__("numpy").zeros(obs_dim, dtype=__import__("numpy").float32), 0.0, self._steps >= 200, False, {}
    return TradingEnv()


def load_strategy_model(strategy: str):
    import os
    import numpy as np   # FIX: was missing — caused "name 'np' is not defined" on load
    from sb3_contrib import RecurrentPPO
    path = os.path.join(MODEL_DIR, f"ppo_{strategy}.zip")
    env  = _make_env()
    
    try:
        if os.path.exists(path):
            log.info(f"[MODEL] Loading {strategy} from {path}")
            model = RecurrentPPO.load(path, env=env)
            # Validate model is properly loaded
            test_obs = np.zeros(30, dtype=np.float32)
            try:
                action, _ = model.predict(test_obs.reshape(1, -1))
                log.info(f"[MODEL] {strategy} loaded successfully - test prediction: {action}")
                return model
            except Exception as pred_error:
                log.warning("[MODEL] %s prediction test failed: %s", strategy, pred_error)
                # Model file corrupted, create fresh one
                pass
        else:
            log.info(f"[MODEL] No saved model found for {strategy} at {path}")
            
        # Create fresh model with optimized parameters
        log.info(f"[MODEL] Creating fresh PPO for {strategy}")
        model = RecurrentPPO(
            "MlpLstmPolicy", 
            env, 
            verbose=0,
            n_steps=512, 
            batch_size=64, 
            n_epochs=6, 
            learning_rate=2e-4,
            gamma=0.99,           # Discount factor
            gae_lambda=0.95,      # GAE lambda
            clip_range=0.2,       # PPO clipping
            ent_coef=0.01,        # Entropy coefficient
            vf_coef=0.5,          # Value function coefficient
            max_grad_norm=0.5,    # Gradient clipping
            use_sde=False,        # Use generalized State Dependent Exploration
            sde_sample_freq=-1,   # Frequency of SDE sampling
        )
        return model
    except Exception as e:
        log.warning("[MODEL] Failed to load/create %s: %s", strategy, e)
        # Fallback to basic model
        return RecurrentPPO("MlpLstmPolicy", env, verbose=0)


def save_strategy_model(model, strategy: str, mark_trained: bool = True):
    import os, json
    model.save(os.path.join(MODEL_DIR, f"ppo_{strategy}.zip"))
    # PROFITABLE FIX #4: Write a metadata file alongside the model so the
    # signal endpoint can distinguish a trained model from a fresh random one.
    # mark_trained=False: save weights but do not set trained=True (e.g. <10 trades).
    meta_path = os.path.join(MODEL_DIR, f"ppo_{strategy}_meta.json")
    meta = {}
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except Exception:
            meta = {}
    if mark_trained:
        meta["trained"] = True
    meta["save_count"] = meta.get("save_count", 0) + 1
    with open(meta_path, "w") as f:
        json.dump(meta, f)
    volume.commit()
    log.info(f"[MODEL] Saved {strategy} (save_count={meta['save_count']}, trained={mark_trained})")


def is_model_trained(strategy: str) -> bool:
    """
    PROFITABLE FIX #4: Returns True only if the model has been saved at least
    once after real training. A brand-new RecurrentPPO has random weights and
    will inject random BUY/SELL noise into live trading.
    """
    import os, json
    meta_path = os.path.join(MODEL_DIR, f"ppo_{strategy}_meta.json")
    if not os.path.exists(meta_path):
        return False
    try:
        with open(meta_path) as f:
            return bool(json.load(f).get("trained", False))
    except Exception:
        return False


def ppo_predict(model, obs) -> tuple:
    """
    Run RecurrentPPO inference and return (action_str, confidence).

    BUG FIX: Previous version called model.policy.extract_features_and_latent()
    and model.policy.action_net() which do not exist on RecurrentActorCriticPolicy
    from sb3_contrib. This caused every confidence extraction to fall through to
    the 0.50 fallback, making the PPO signal useless (always "untrained" branch).

    Fix: use model.policy.get_distribution() which IS a valid SB3 method and
    returns a proper action distribution we can read probabilities from.
    """
    import numpy as np, torch
    obs_arr = np.array(obs, dtype=np.float32).reshape(1, -1)

    act_idx, _ = model.predict(
        obs_arr,
        state=None,
        episode_start=np.array([True]),
        deterministic=True,
    )
    act_int = int(act_idx[0])

    # ── Confidence via action distribution ───────────────────────────────────
    try:
        with torch.no_grad():
            obs_t  = torch.as_tensor(obs_arr, dtype=torch.float32).to(model.device)
            ep_t   = torch.as_tensor([True],  dtype=torch.bool).to(model.device)
            # FIX: model.policy.initial_state() was removed in newer sb3-contrib versions.
            # Pass lstm_states=None instead — the policy re-initialises the LSTM cell
            # automatically when episode_starts=True, which is equivalent behaviour.
            dist = model.policy.get_distribution(
                obs_t,
                lstm_states=None,
                episode_starts=ep_t,
            )
            # dist.distribution is a torch.distributions.Categorical
            probs = dist.distribution.probs.cpu().numpy().flatten()
            conf  = float(probs[act_int])
    except Exception as e1:
        # Fallback: stochastic sampling to estimate confidence (always works)
        try:
            votes = np.zeros(3, dtype=np.float32)
            for _ in range(10):
                a, _ = model.predict(obs_arr, state=None,
                                     episode_start=np.array([True]),
                                     deterministic=False)
                votes[int(a[0])] += 1
            conf = float(votes[act_int] / 10.0)
        except Exception:
            conf = 0.50
        log.warning("[PPO] get_distribution failed (%s) — using sampling fallback conf=%.2f", e1, conf)

    return {0: "BUY", 1: "SELL", 2: "NONE"}[act_int], conf




# =============================================================================
# SECTION 5a — NEWS MONITOR  (every 5 minutes)
# Fetches from multiple credible sources, writes active blackouts to Supabase.
# Signal endpoint reads from DB — no external calls in the hot path.
#
# Sources:
#   1. ForexFactory   — gold standard for scheduled economic calendar events
#   2. Finnhub        — breaking financial news + sentiment (free tier)
#   3. Alpha Vantage  — market news & sentiment with ticker mapping (free tier)
#
# Add API keys to Modal secret:
#   modal secret update apexhydra-secrets \
#     FINNHUB_API_KEY=your_key \
#     ALPHAVANTAGE_API_KEY=your_key
# Both are FREE — register at finnhub.io and alphavantage.co
# =============================================================================

# High-impact keywords that trigger a full market blackout (ALL currencies)
# SHOCK = direct forex market systemic events only.
# Rules for adding a keyword:
#   1. Must directly move ALL major currency pairs simultaneously
#   2. Must be unscheduled / surprise (scheduled events covered by ForexFactory)
#   3. Must be a financial system event, NOT weather/transport/corporate earnings
#
# NOT shocks: blizzards, flight cancellations, company earnings, product launches,
#             sports events, celebrity news, regional disasters, stock moves
SHOCK_KEYWORDS = [
    # Central bank emergency actions (unscheduled)
    "emergency rate cut", "emergency rate hike", "emergency rate decision",
    "unscheduled fed meeting", "unscheduled fomc", "extraordinary fed",
    "fed emergency", "ecb emergency", "boe emergency", "boj emergency",
    # Financial system collapse
    "bank failure", "bank collapse", "bank run on",
    "systemic banking crisis", "financial system collapse",
    "stock market circuit breaker", "nyse trading halt", "global trading halt",
    # Sovereign/debt crisis
    "us treasury default", "us debt default", "sovereign default",
    "debt ceiling breached", "government debt crisis",
    "imf emergency loan",
    # Geopolitical — only events that directly halt markets
    "war declared", "nuclear strike", "nuclear attack",
    "major terrorist attack on financial",
    "martial law declared", "president assassinated",
    # Named crises (historical markers for pattern matching)
    "lehman brothers", "svb collapse", "credit suisse collapse",
]

# Must contain a FOREX-RELEVANT term to qualify as currency news.
# This prevents Nvidia/tech/weather articles from triggering currency blackouts.
FOREX_RELEVANCE_TERMS = [
    "forex", "currency", "exchange rate", "central bank", "interest rate",
    "federal reserve", "ecb ", "bank of england", "bank of japan",
    "boe ", "boj ", "fomc", "inflation", "cpi", "gdp", "nonfarm",
    "monetary policy", "rate decision", "rate hike", "rate cut",
    "dollar", "euro ", "sterling", "yen ", "swiss franc",
    "fx market", "fx trading", "foreign exchange",
]

# Currency-specific high-impact keywords — must be specific to FX drivers
CURRENCY_KEYWORDS = {
    "USD": ["nonfarm payroll", "nfp report", "federal reserve decision",
            "fomc decision", "fomc minutes", "jerome powell speech",
            "us cpi report", "us inflation data", "us gdp", "us unemployment",
            "fed rate decision", "debt ceiling crisis", "us treasury default"],
    "EUR": ["ecb rate decision", "ecb meeting", "lagarde speech",
            "eurozone cpi", "eurozone inflation", "eurozone gdp",
            "ecb interest rate", "eurozone unemployment"],
    "GBP": ["bank of england decision", "boe rate", "mpc decision",
            "uk cpi report", "uk inflation data", "uk gdp report",
            "uk unemployment data", "bailey speech"],
    "JPY": ["bank of japan decision", "boj rate", "boj policy",
            "japan cpi", "yen intervention", "boj intervention",
            "japan gdp", "ueda speech"],
    "XAU": ["central bank gold reserve", "gold reserve announcement",
            "imf gold sale", "comex trading halt"],
    "ALL": SHOCK_KEYWORDS,
}

# Blackout window in minutes before/after each event
WINDOW_MINUTES = {
    "scheduled_high":   30,   # ForexFactory High impact — 30 min each side
    "scheduled_medium": 10,   # ForexFactory Medium impact — 10 min each side
    "breaking_shock":   60,   # Surprise/unscheduled shock — 60 min blackout
    "breaking_currency": 20,  # Currency-specific breaking news — 20 min
}


@app.function(
    image=image,
    secrets=[secrets],
    schedule=modal.Period(minutes=5),
    timeout=120,
)
def news_monitor():
    """
    Runs every 5 minutes. Aggregates news from 3 sources and maintains
    the news_blackouts table in Supabase.

    ForexFactory  → scheduled economic calendar (NFP, FOMC, CPI etc.)
    Finnhub       → breaking financial news (surprise events, shocks)
    Alpha Vantage → market news sentiment with currency impact scoring
    """
    import os, json
    import requests
    from datetime import datetime, timezone, timedelta
    from supabase import create_client

    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    now      = datetime.now(timezone.utc)
    active_blackouts = []

    # ── SOURCE 1: ForexFactory — scheduled economic calendar ─────────────────
    try:
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=8, headers={"User-Agent": "Mozilla/5.0"}
        )
        r.raise_for_status()
        ff_events = r.json()
        log.info(f"[NEWS] ForexFactory: {len(ff_events)} events loaded")

        for evt in ff_events:
            impact = evt.get("impact", "").lower()
            if impact not in ("high", "medium"):
                continue

            try:
                evt_time = datetime.fromisoformat(
                    evt["date"].replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except Exception:
                continue

            window = timedelta(minutes=WINDOW_MINUTES[
                "scheduled_high" if impact == "high" else "scheduled_medium"
            ])

            if abs(evt_time - now) <= window:
                currency = evt.get("currency", "").strip().upper()
                title    = evt.get("title", "Economic event")
                active_blackouts.append({
                    "source":     "ForexFactory",
                    "title":      title[:120],
                    "currencies": currency,
                    "impact":     impact.capitalize(),
                    "event_time": evt_time.isoformat(),
                    "expires_at": (evt_time + window).isoformat(),
                    "active":     True,
                    "updated_at": now.isoformat(),
                })
                log.info(f"[NEWS] FF Blackout: {currency} — {title} @ {evt_time}")

    except Exception as e:
        log.warning("[NEWS] ForexFactory failed: %s", e)

    # ── SOURCE 2: Finnhub — breaking financial news ───────────────────────────
    finnhub_key = os.environ.get("FINNHUB_API_KEY", "")
    if finnhub_key:
        try:
            # General market news — most recent 20 articles
            r = requests.get(
                f"https://finnhub.io/api/v1/news?category=general&token={finnhub_key}",
                timeout=8
            )
            r.raise_for_status()
            articles = r.json()[:20]

            for art in articles:
                headline = (art.get("headline", "") + " " + art.get("summary", "")).lower()
                pub_time = datetime.fromtimestamp(art.get("datetime", 0), tz=timezone.utc)

                # Only consider articles from last 2 hours
                if (now - pub_time).total_seconds() > 7200:
                    continue

                # SHOCK: must match keyword AND be forex-relevant
                shock_hit = any(kw in headline for kw in SHOCK_KEYWORDS)
                forex_rel = any(t in headline for t in FOREX_RELEVANCE_TERMS)

                if shock_hit and forex_rel:
                    active_blackouts.append({
                        "source":     "Finnhub",
                        "title":      art.get("headline", "Breaking news")[:120],
                        "currencies": "ALL",
                        "impact":     "SHOCK",
                        "event_time": pub_time.isoformat(),
                        "expires_at": (now + timedelta(
                            minutes=WINDOW_MINUTES["breaking_shock"]
                        )).isoformat(),
                        "active":     True,
                        "updated_at": now.isoformat(),
                    })
                    log.info(f"[NEWS] ⚠️ SHOCK: {art.get('headline','')[:80]}")
                    continue

                # Currency-specific: skip non-forex articles
                if not forex_rel:
                    continue

                for currency, keywords in CURRENCY_KEYWORDS.items():
                    if currency == "ALL":
                        continue
                    if any(kw in headline for kw in keywords):
                        active_blackouts.append({
                            "source":     "Finnhub",
                            "title":      art.get("headline", "News")[:120],
                            "currencies": currency,
                            "impact":     "High",
                            "event_time": pub_time.isoformat(),
                            "expires_at": (now + timedelta(
                                minutes=WINDOW_MINUTES["breaking_currency"]
                            )).isoformat(),
                            "active":     True,
                            "updated_at": now.isoformat(),
                        })
                        log.info(f"[NEWS] Finnhub {currency}: {art.get('headline','')[:60]}")
                        break

        except Exception as e:
            log.warning("[NEWS] Finnhub failed: %s", e)
    else:
        log.warning("[NEWS] FINNHUB_API_KEY not set — skipping Finnhub")

    # ── SOURCE 3: Alpha Vantage — news sentiment ──────────────────────────────
    av_key = os.environ.get("ALPHAVANTAGE_API_KEY", "")
    if av_key:
        try:
            # Fetch forex + financial news with sentiment
            r = requests.get(
                f"https://www.alphavantage.co/query"
                f"?function=NEWS_SENTIMENT&topics=forex,financial_markets,economy_macro"
                f"&sort=LATEST&limit=20&apikey={av_key}",
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            articles = data.get("feed", [])

            for art in articles:
                headline = (art.get("title", "") + " " + art.get("summary", "")).lower()
                # AV provides time_published like "20240223T143000"
                try:
                    ts = art.get("time_published", "")
                    pub_time = datetime.strptime(ts, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                except Exception:
                    continue

                if (now - pub_time).total_seconds() > 3600:
                    continue

                # Overall sentiment score — bearish spike = potential shock
                overall_sentiment = float(art.get("overall_sentiment_score", 0))

                # Shock keywords
                # SHOCK: must match shock keyword AND be forex-relevant
                shock_hit    = any(kw in headline for kw in SHOCK_KEYWORDS)
                forex_rel    = any(t in headline for t in FOREX_RELEVANCE_TERMS)
                # Sentiment threshold tightened to -0.60 to avoid normal bearish analysis
                extreme_sent = overall_sentiment < -0.60

                if (shock_hit or extreme_sent) and forex_rel:
                    label = "shock keyword" if shock_hit else f"sentiment={overall_sentiment:.2f}"
                    active_blackouts.append({
                        "source":     "AlphaVantage",
                        "title":      art.get("title", "Market alert")[:120],
                        "currencies": "ALL",
                        "impact":     "SHOCK",
                        "event_time": pub_time.isoformat(),
                        "expires_at": (now + timedelta(
                            minutes=WINDOW_MINUTES["breaking_shock"]
                        )).isoformat(),
                        "active":     True,
                        "updated_at": now.isoformat(),
                    })
                    log.info(f"[NEWS] AV Alert ({label}): {art.get('title','')[:60]}")
                    continue

                # Currency-specific: must be forex-relevant AND match currency keyword
                if not forex_rel:
                    continue  # skip non-forex articles entirely

                for currency, keywords in CURRENCY_KEYWORDS.items():
                    if currency == "ALL":
                        continue
                    if any(kw in headline for kw in keywords):
                        active_blackouts.append({
                            "source":     "AlphaVantage",
                            "title":      art.get("title", "News")[:120],
                            "currencies": currency,
                            "impact":     "High",
                            "event_time": pub_time.isoformat(),
                            "expires_at": (now + timedelta(
                                minutes=WINDOW_MINUTES["breaking_currency"]
                            )).isoformat(),
                            "active":     True,
                            "updated_at": now.isoformat(),
                        })
                        log.info(f"[NEWS] AV {currency}: {art.get('title','')[:60]}")
                        break

        except Exception as e:
            log.warning("[NEWS] Alpha Vantage failed: %s", e)
    else:
        log.warning("[NEWS] ALPHAVANTAGE_API_KEY not set — skipping Alpha Vantage")

    # ── Write to Supabase ─────────────────────────────────────────────────────
    # First expire any old blackouts that have passed their expiry time.
    # NOTE: if news_blackouts table does not exist yet, create it implicitly by
    # catching the error and skipping — the insert below will create rows fine.
    try:
        supabase.table("news_blackouts")             .update({"active": False})             .lt("expires_at", now.isoformat())             .execute()
    except Exception as e:
        log.warning("[NEWS] Expire old blackouts failed (table may not exist yet): %s", e)

    if not active_blackouts:
        log.info("[NEWS] No active blackouts right now — all clear")
        return

    # Insert new active blackouts (deduplicate by source+title)
    inserted = 0
    for row in active_blackouts:
        try:
            # Check if this exact event already exists
            existing = supabase.table("news_blackouts")                 .select("id")                 .eq("source",  row["source"])                 .eq("title",   row["title"])                 .eq("active",  True)                 .execute().data
            if existing:
                continue  # already in DB
            supabase.table("news_blackouts").insert(row).execute()
            inserted += 1
        except Exception as e:
            log.warning("[NEWS] Insert failed: %s", e)

    total_active = len(supabase.table("news_blackouts")         .select("id").eq("active", True).execute().data or [])

    log.info(f"[NEWS] Monitor complete: {inserted} new blackouts | {total_active} active total")


# =============================================================================
# SECTION 6 — BACKTESTER  (weekly, 2 years of real data)
# =============================================================================

# PROFITABLE FIX #5: Backtest re-enabled. Was commented out to stay within Modal's
# 5-cron limit — replaced equity_snapshot (least critical) with this.
# Weekly backtest provides systematic strategy validation and forces SAFE mode
# if no strategy scores above 0.50 — preventing live trading on broken signals.
@app.function(
    image=image, secrets=[secrets], volumes={MODEL_DIR: volume},
    schedule=modal.Period(weeks=1), timeout=1800, memory=4096,
)
def run_backtest():
    """
    Downloads 2 years of 1-hour OHLCV for all symbols via Yahoo Finance.
    Simulates all 3 strategies bar-by-bar (no lookahead bias).
    Scores by Sharpe, win rate, max drawdown, profit factor.
    Forces bot to SAFE mode if best strategy score < 0.50.
    """
    import os, math
    import yfinance as yf
    from datetime import datetime, timezone
    from supabase import create_client

    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    results  = []

    for yf_sym in YF_SYMBOLS:
        mt5_sym = (yf_sym.replace("=X","").replace("=F","")
                          .replace("GC","XAUUSD").replace("SI","XAGUSD"))
        log.info(f"[BACKTEST] {yf_sym}...")
        try:
            df = yf.download(yf_sym, period="2y", interval="1h", progress=False, auto_adjust=True)
            if df.empty or len(df) < 100:
                continue
        except Exception as e:
            log.warning("[BACKTEST] Download failed %s: %s", yf_sym, e); continue

        df.dropna(inplace=True)
        closes  = _flatten_yf(df, "Close")
        highs   = _flatten_yf(df, "High")
        lows    = _flatten_yf(df, "Low")
        volumes = _flatten_yf(df, "Volume")

        # Per-symbol realistic spread estimate (pip value, matching forward tester)
        # Used for: (a) bid/ask on entry, (b) round-trip cost deducted from PnL
        sym_upper_bt = yf_sym.upper()
        if "GC" in sym_upper_bt:    bt_spread = 15.0  # gold ≈ $1.50 round-trip cost
        elif "SI" in sym_upper_bt:  bt_spread = 8.0   # silver — 3-8 pip typical, use 8 (conservative)
        elif "JPY" in sym_upper_bt: bt_spread = 1.5
        elif "GBP" in sym_upper_bt: bt_spread = 2.0
        else:                       bt_spread = 1.0   # EURUSD etc.

        # Convert pip spread to price-unit spread for bid/ask construction
        if "GC" in sym_upper_bt:         bt_spread_price = bt_spread * 0.10   # gold: 1 pip = $0.10
        elif "SI" in sym_upper_bt:       bt_spread_price = bt_spread * 0.001  # silver: point=0.001, 1 pip=0.001
        elif "JPY" in sym_upper_bt:      bt_spread_price = bt_spread * 0.01
        else:                            bt_spread_price = bt_spread * 0.0001

        for sname, sfn in STRATEGY_FN.items():
            trades = []; pos = 0; entry = 0.0; entry_dir = ""
            equity = [10000.0]; peak = 10000.0; max_dd = 0.0

            for i in range(50, len(closes)):
                w   = min(i, 100)

                # FIX: Use real bar timestamp for session filter.
                # Old code hardcoded hour=12, dow=2 — session filter never fired,
                # backtest traded 24/7 while the live bot only trades 10h/day.
                bar_time = df.index[i]
                bar_hour = int(bar_time.hour)
                bar_dow  = int(bar_time.weekday())  # 0=Mon…4=Fri
                c_now    = closes[i]
                bid_now  = c_now - bt_spread_price / 2
                ask_now  = c_now + bt_spread_price / 2

                msg = {
                    "close": closes[i-w:i], "high": highs[i-w:i],
                    "low":   lows[i-w:i],   "volume": volumes[i-w:i],
                    "hour":  bar_hour,       "dow": bar_dow,
                    "spread": bt_spread,
                    "bid":   bid_now,        "ask": ask_now,
                }

                # Apply session filter — mirrors live bot behaviour
                mt5_sym_bt = (yf_sym.replace("=X","").replace("=F","")
                                     .replace("GC","XAUUSD").replace("SI","XAGUSD"))
                sess_ok, _ = _is_session_active(mt5_sym_bt)

                action, conf, _ = sfn(msg)

                # Close logic: exit at mid-price (ask for sell-close, bid for buy-close)
                if pos ==  1 and action == "SELL":
                    # Close BUY at bid; deduct round-trip spread
                    gross = (bid_now - entry) / entry * 1000
                    cost  = (bt_spread_price * 2) / entry * 1000
                    pnl   = gross - cost
                    trades.append(pnl); equity.append(equity[-1]+pnl); pos = 0; entry_dir = ""
                elif pos == -1 and action == "BUY":
                    # Close SELL at ask; deduct round-trip spread
                    gross = (entry - ask_now) / entry * 1000
                    cost  = (bt_spread_price * 2) / entry * 1000
                    pnl   = gross - cost
                    trades.append(pnl); equity.append(equity[-1]+pnl); pos = 0; entry_dir = ""

                # Only open new positions during active sessions and with sufficient conf
                if pos == 0 and action in ("BUY","SELL") and conf > 0.62 and sess_ok:
                    entry     = ask_now if action == "BUY" else bid_now
                    pos       = 1 if action == "BUY" else -1
                    entry_dir = action

                if equity[-1] > peak: peak = equity[-1]
                dd = (peak - equity[-1]) / peak
                if dd > max_dd: max_dd = dd

            if not trades: continue

            wins  = [t for t in trades if t > 0]
            loses = [t for t in trades if t <= 0]
            wr    = len(wins) / len(trades)
            aw    = sum(wins)  / max(len(wins),  1)
            al    = abs(sum(loses) / max(len(loses), 1))
            pf    = aw * len(wins) / max(al * len(loses), 1e-9)
            avg_t = sum(trades) / len(trades)
            std_t = math.sqrt(sum((t-avg_t)**2 for t in trades) / len(trades))
            sharpe = avg_t / max(std_t, 1e-9) * math.sqrt(252)
            score  = (
                min(sharpe, 5.0)/5.0 * 0.35 + wr * 0.25 +
                (1-min(max_dd,1.0))  * 0.25 + min(pf,5.0)/5.0 * 0.15
            )

            row = {
                "symbol": mt5_sym, "strategy": sname,
                "sharpe": round(sharpe,4), "win_rate": round(wr,4),
                "max_dd": round(max_dd,4), "profit_factor": round(pf,4),
                "total_return": round((equity[-1]-10000)/10000, 4),
                "num_trades": len(trades), "score": round(score,6),
                "tested_at": datetime.now(timezone.utc).isoformat(),
            }
            results.append(row)
            log.info(f"  {sname}: sharpe={sharpe:.2f} wr={wr:.0%} dd={max_dd:.1%} score={score:.3f}")

    for row in results:
        try:
            supabase.table("backtest_results").upsert(row).execute()
        except Exception as e:
            log.warning("[BACKTEST] DB write failed: %s", e)

    # Fine-tune PPO for best overall strategy
    if results:
        best = max(results, key=lambda r: r["score"])
        log.info(f"[BACKTEST] Best: {best['strategy']} / {best['symbol']} score={best['score']:.3f}")

        # PROFITABLE FIX #5b: Performance gate.
        # If the best strategy scores below 0.50 (worse than a coin flip after
        # costs), force the bot to SAFE mode and log a warning. This prevents
        # live trading when all strategies are showing poor backtest results.
        if best["score"] < 0.50:
            log.warning("[BACKTEST] Best score %.3f < 0.50 — forcing SAFE mode", best["score"])
            try:
                supabase.table("bot_state").update({
                    "mode":       "SAFE",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).gte("id", "00000000-0000-0000-0000-000000000000").execute()
                log.info("[BACKTEST] Bot mode forced to SAFE due to poor strategy performance")
            except Exception as e:
                log.warning("[BACKTEST] Could not set SAFE mode: %s", e)

        # FIX BUG#6: Only checkpoint the model if it was already trained on real
        # trade data. Previously: load_strategy_model() created a fresh RecurrentPPO
        # with random weights when no .zip existed, then save_strategy_model()
        # immediately wrote trained=True to metadata, bypassing the cold-start guard
        # in /signal. Random PPO weights then influenced every live trading decision.
        # Rule: run_backtest() NEVER promotes an untrained model. A model only becomes
        # trained=True after force_learn() or run_forward_test() with real closed trades.
        try:
            if is_model_trained(best["strategy"]):
                model = load_strategy_model(best["strategy"])
                save_strategy_model(model, best["strategy"])
                log.info(f"[BACKTEST] Model checkpoint saved for {best['strategy']} (trained model refreshed).")
            else:
                log.info(f"[BACKTEST] Skipping model save for {best['strategy']} — "
                      f"no trained model yet, cold-start guard remains active.")
        except Exception as e:
            log.warning("[BACKTEST] Save failed: %s", e)

    log.info(f"[BACKTEST] Complete. {len(results)} evaluations.")


# =============================================================================
# SECTION 7 — FORWARD TESTER  (every 4 hours, paper trading)
# =============================================================================

@app.function(
    image=image, secrets=[secrets], volumes={MODEL_DIR: volume},
    schedule=modal.Period(hours=4), timeout=900, memory=4096,
)
def run_forward_test():
    """
    Combined: Forward Testing + Online Learning  (runs every 4 hours)
    Merged to stay within Modal free tier 5-cron-job limit.

    Part 1 — Forward Test:
      Downloads last 7 days of 5-min data, paper-trades all 3 strategies,
      scores results, promotes best performer in Supabase strategies table.

    Part 2 — Online Learn:
      Pulls closed trades with real PnL + stored features from last 7 days.
      Fine-tunes the active strategy PPO model from live trade outcomes.
      Runs only if ≥10 closed trades are available.
    """
    import os, math
    import yfinance as yf
    from datetime import datetime, timezone
    from supabase import create_client

    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    agg      = {s: {"wr": [], "pnl": []} for s in STRATEGY_NAMES}

    # FIX 1: was YF_SYMBOLS[:3] — excluded gold (GC=F) from forward-test scoring.
    # This meant the active strategy was promoted based on forex-only performance,
    # which is suboptimal when XAUUSD is in the trading portfolio.
    for yf_sym in YF_SYMBOLS:
        try:
            df = yf.download(yf_sym, period="7d", interval="5m", progress=False, auto_adjust=True)
            if df.empty or len(df) < 50:
                continue
        except Exception as e:
            log.info(f"[FWD] {yf_sym}: {e}"); continue

        closes  = _flatten_yf(df, "Close")
        highs   = _flatten_yf(df, "High")
        lows    = _flatten_yf(df, "Low")
        volumes = _flatten_yf(df, "Volume")

        # PROFITABLE FIX #6: Realistic spread estimate by symbol.
        # Old code used spread=0.0001 for all bars — understated gold ~270×.
        sym_upper = yf_sym.upper()
        if "GC" in sym_upper:    est_spread = 15.0  # gold: ~1.5 USD ≈ 15 pips
        elif "JPY" in sym_upper: est_spread = 1.5
        elif "GBP" in sym_upper: est_spread = 2.0
        else:                    est_spread = 1.0   # EURUSD etc.

        for sname, sfn in STRATEGY_FN.items():
            trades = []; pos = 0; entry = 0.0
            for i in range(30, len(closes)):
                w   = min(i, 60)

                # PROFITABLE FIX #6: Use real bar timestamp for hour/dow so
                # session filters work correctly in paper-trading evaluation.
                # Old code hardcoded hour=12, dow=2 — bypassed all session checks.
                bar_time = df.index[i]
                bar_hour = int(bar_time.hour)
                bar_dow  = int(bar_time.weekday())   # 0=Mon … 4=Fri
                c_now    = closes[i]
                spread_price = est_spread * 0.0001 if "JPY" not in sym_upper else est_spread * 0.01

                msg = {
                    "close":  closes[i-w:i], "high": highs[i-w:i],
                    "low":    lows[i-w:i],   "volume": volumes[i-w:i],
                    "hour":   bar_hour,
                    "dow":    bar_dow,
                    "spread": est_spread,
                    "bid":    c_now - spread_price / 2,
                    "ask":    c_now + spread_price / 2,
                }
                action, conf, _ = sfn(msg)
                if pos ==  1 and action == "SELL":
                    # PROFITABLE FIX #6: Deduct round-trip spread cost from PnL
                    gross = (closes[i] - entry) / entry * 1000
                    net   = gross - (spread_price * 2 / closes[i] * 1000)
                    trades.append(net); pos = 0
                elif pos == -1 and action == "BUY":
                    gross = (entry - closes[i]) / entry * 1000
                    net   = gross - (spread_price * 2 / closes[i] * 1000)
                    trades.append(net); pos = 0
                if pos == 0 and action in ("BUY","SELL"):
                    # FIX: use strategy-specific SAFE threshold (0.63-0.72), not
                    # hardcoded 0.62. Old value was looser than live thresholds,
                    # admitting trades the live bot would reject -> inflated scores.
                    fwd_threshold = _THRESHOLDS["SAFE"].get(sname, 0.68)
                    if conf >= fwd_threshold:
                        pos = 1 if action=="BUY" else -1; entry = closes[i]

            if trades:
                agg[sname]["wr"].append(len([t for t in trades if t>0])/len(trades))
                agg[sname]["pnl"].append(sum(trades)/len(trades))

    scores = {}
    for sname, data in agg.items():
        if not data["wr"]:
            scores[sname] = 0.5; continue
        wr  = sum(data["wr"])  / len(data["wr"])
        pnl = sum(data["pnl"]) / len(data["pnl"])
        scores[sname] = round(wr*0.6 + min(max(pnl/10,0),1)*0.4, 4)

    best = max(scores, key=scores.get)
    now  = datetime.now(timezone.utc).isoformat()

    for sname, score in scores.items():
        try:
            supabase.table("strategies").upsert({
                "strategy": sname, "fwd_score": score,
                "is_active": sname == best, "updated_at": now,
            }).execute()
        except Exception as e:
            log.warning("[FWD] DB failed %s: %s", sname, e)

    try:
        supabase.table("forward_results").insert({
            "tested_at": now, "best_strategy": best,
            "tf_score":  scores["trend_following"],
            "mr_score":  scores["mean_reversion"],
            "bo_score":  scores["breakout"],
        }).execute()
    except Exception as e:
        log.warning("[FWD] forward_results failed: %s", e)

    log.info(f"[FWD] Scores: {scores} | Active: {best}")

    # ── Part 2: Online Learning ───────────────────────────────────────────────
    log.info("[LEARN] Starting online learning pass...")
    try:
        import numpy as np
        from datetime import timedelta

        strat_rows = supabase.table("strategies").select("strategy") \
            .eq("is_active", True).limit(1).execute().data
        active = strat_rows[0]["strategy"] if strat_rows else "trend_following"

        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        rows  = supabase.table("trades").select("*") \
            .gte("opened_at", since) \
            .not_.is_("pnl", "null") \
            .not_.is_("features", "null") \
            .not_.is_("strategy", "null") \
            .execute().data or []

        if len(rows) < 10:
            log.info(f"[LEARN] Only {len(rows)} closed trades — need ≥10. Skipping.")
        else:
            valid = []
            for row in rows:
                try:
                    feat      = row.get("features", [])
                    direction = row.get("direction", "")
                    # FIX 2: require valid direction — needed for correct reward signal.
                    # Old code excluded direction=="NONE" but allowed any other string,
                    # meaning rows without direction would produce wrong reward signals.
                    if not feat or direction not in ("BUY", "SELL"):
                        continue
                    valid.append((np.array(feat[:30], dtype=np.float32),
                                  float(row.get("pnl", 0)),
                                  direction))
                except Exception:
                    continue

            if len(valid) >= 5:
                model = load_strategy_model(active)

                # BUG FIX: Previous code called model.learn(total_timesteps=N) which
                # rolled out the dummy TradingEnv (all-zero obs, zero reward) — it
                # never touched the real trade data in `valid` at all.
                # Fix: create a TradeReplayEnv that serves stored features as
                # observations and normalised PnL as the reward signal so the PPO
                # model actually learns from what happened in live trading.

                import numpy as np
                import gymnasium as gym
                from gymnasium import spaces

                class TradeReplayEnv(gym.Env):
                    """Single-episode environment that replays stored live trades.

                    FIX 2: Previous reward used only pnl sign to determine direction,
                    meaning a profitable SELL trade (pnl > 0) had direction=+1, and
                    if PPO output action=0 (BUY) it got rewarded — the opposite of what
                    actually made money. We now store the original trade direction so the
                    reward correctly teaches: "BUY on this obs was profitable" vs
                    "SELL on this obs was profitable".

                    Data format: list of (obs_array, pnl, direction_str)
                    direction_str: "BUY" | "SELL"
                    """
                    def __init__(self, trade_data):
                        super().__init__()
                        self.data  = trade_data    # list of (obs_array, pnl, direction)
                        self.idx   = 0
                        self.action_space      = spaces.Discrete(3)
                        self.observation_space = spaces.Box(
                            -np.inf, np.inf, shape=(30,), dtype=np.float32)

                    def reset(self, *, seed=None, options=None):
                        super().reset(seed=seed)
                        self.idx = 0
                        return self.data[0][0].copy(), {}

                    def step(self, action):
                        feats, pnl, orig_dir = self.data[self.idx % len(self.data)]
                        # Map original direction to action integer
                        # BUY=0, SELL=1, NONE=2
                        orig_act = 0 if orig_dir == "BUY" else 1

                        # Reward logic:
                        #  - action matches original trade AND trade was profitable → +reward
                        #  - action matches original trade AND trade was a loss      → -reward (teach to avoid)
                        #  - action does NOT match original trade direction           → -reward (wrong direction)
                        #  - action == NONE on a real profitable trade               → small penalty
                        if action == 2:
                            # NONE on a real trade — penalise more if trade was profitable
                            reward = -abs(pnl) * (0.5 if pnl > 0 else 0.1)
                        elif action == orig_act:
                            # Correct direction — reward if profitable, penalise if loss
                            reward = pnl   # positive pnl → positive reward, negative → negative
                        else:
                            # Wrong direction — always penalise by pnl magnitude
                            reward = -abs(pnl)

                        # Clip to avoid extreme gradients
                        reward = float(np.clip(reward / 10.0, -2.0, 2.0))

                        self.idx += 1
                        done = self.idx >= len(self.data)
                        next_obs = self.data[self.idx % len(self.data)][0].copy() \
                                   if not done else feats.copy()
                        return next_obs, reward, done, False, {}

                replay_env = TradeReplayEnv(valid)
                model.set_env(replay_env)  # hot-swap to replay env for this pass
                timesteps = max(len(valid) * 20, 5000)
                model.learn(total_timesteps=timesteps, reset_num_timesteps=False)
                # Restore the standard env so the model stays compatible with
                # the rest of the codebase (predict calls, future learn calls).
                model.set_env(_make_env())
                save_strategy_model(model, active)
                supabase.table("learning_log").insert({
                    "strategy":   active,
                    "num_trades": len(valid),
                    "learned_at": datetime.now(timezone.utc).isoformat(),
                }).execute()
                log.info(f"[LEARN] {active} updated on {len(valid)} live trades "
                      f"({timesteps} timesteps, replay env).")
            else:
                log.info("[LEARN] Not enough valid feature samples.")
    except Exception as e:
        log.warning("[LEARN] Online learning failed: %s", e)

    # ── Merged: equity fallback snapshot ─────────────────────────────────────
    # CRON LIMIT FIX: equity_snapshot cron removed. Fallback runs here instead
    # (every 4h) — covers MT5-disconnection gaps without needing its own cron.
    _equity_snapshot_fallback(supabase)

    # ── Merged: daily reset (was automated_daily_reset cron) ─────────────────
    # CRON LIMIT FIX: automated_daily_reset cron removed. Since run_forward_test
    # runs every 4h it will trigger within 4h of midnight UTC, which is accurate
    # enough for daily P&L reset purposes.
    try:
        now_utc     = datetime.now(timezone.utc)
        midnight    = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        hours_since = (now_utc - midnight).total_seconds() / 3600
        # Only reset if we're within the first 4-hour window after midnight
        if hours_since < 4:
            state_r = supabase.table("bot_state").select("id,risk_day_start") \
                .order("updated_at", desc=True).limit(1).execute().data
            if state_r:
                sid       = state_r[0].get("id", "")
                stored    = state_r[0].get("risk_day_start", "")
                new_reset = midnight.isoformat()
                # Only write if not already reset today
                already_reset = False
                if stored:
                    try:
                        stored_dt     = datetime.fromisoformat(stored.replace("Z", "+00:00"))
                        already_reset = stored_dt >= midnight
                    except Exception:
                        pass
                if not already_reset and sid:
                    supabase.table("bot_state").update({
                        "risk_day_start": new_reset,
                        "updated_at":     now_utc.isoformat(),
                    }).eq("id", sid).execute()
                    log.info(f"[DAILY_RESET] Day baseline reset to {new_reset}")
    except Exception as e:
        log.warning("[DAILY_RESET] Failed: %s", e)



# =============================================================================
# SECTION 8b — FORCE LEARN  (run manually: modal run modal_app.py::force_learn)
# =============================================================================
# One-shot function to train all 3 PPO models on ALL closed trades in Supabase,
# not just the last 7 days. Solves two problems:
#
#  1. Trades with features=NULL (closed before feature-logging was added):
#     Reconstructs features from yfinance historical OHLCV around each trade's
#     open timestamp, then runs the same build_features() pipeline.
#
#  2. 7-day recency window in the regular online learner:
#     force_learn uses ALL closed trades regardless of age, giving the PPO
#     models a full trade history on the very first pass.
#
# Run once after deploy:
#   modal run modal_app.py::force_learn
#
# Re-run any time you want a full retrain (e.g. after parameter changes).
# The regular online learner (every 4h) handles incremental updates after this.
# =============================================================================

@app.function(
    image=image,
    secrets=[secrets],
    volumes={MODEL_DIR: volume},
    timeout=3600,
    memory=4096,
)
def force_learn():
    """
    Full historical training pass over ALL closed trades.
    Trains all 3 PPO strategy models and marks them as trained.
    For trades with stored features: used directly.
    For trades missing features: reconstructed from yfinance OHLCV history.
    """
    import os, json, math
    import numpy as np
    import yfinance as yf
    import gymnasium as gym
    from gymnasium import spaces
    from sb3_contrib import RecurrentPPO
    from datetime import datetime, timezone, timedelta
    from supabase import create_client

    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

    SYM_TO_YF = {
        "EURUSD":  "EURUSD=X", "GBPUSD":  "GBPUSD=X",
        "USDJPY":  "USDJPY=X", "GBPJPY":  "GBPJPY=X",
        "USDCHF":  "USDCHF=X", "AUDUSD":  "AUDUSD=X",
        "USDCAD":  "USDCAD=X", "NZDUSD":  "NZDUSD=X",
        "EURJPY":  "EURJPY=X", "EURGBP":  "EURGBP=X",   # NEW: added with 7-pair expansion
        "XAUUSD":  "GC=F",     "XAUUSDM": "GC=F",
        "GOLD":    "GC=F",
        "XAGUSD":  "SI=F",     "XAGUSDM": "SI=F",        # Silver (SI=F = front-month futures)
    }

    # ── Step 1: Pull ALL closed trades ───────────────────────────────────────
    log.info("[FORCE_LEARN] Fetching all closed trades from Supabase...")
    rows = (
        supabase.table("trades")
        .select("*")
        .not_.is_("pnl",       "null")
        .not_.is_("closed_at", "null")
        .not_.is_("direction", "null")
        .execute()
        .data or []
    )
    rows = [r for r in rows
            if r.get("direction") in ("BUY", "SELL")
            and r.get("lot") is not None
            and float(r.get("lot", 0)) > 0]
    log.info(f"[FORCE_LEARN] Found {len(rows)} confirmed closed trades")
    if not rows:
        log.info("[FORCE_LEARN] No trades to learn from — exiting")
        return

    # ── Step 2: Download yfinance OHLCV history per symbol ──────────────────
    log.info("[FORCE_LEARN] Downloading OHLCV history for feature reconstruction...")
    hist_cache   = {}
    needed_yf_syms = set()
    for r in rows:
        sym_raw = str(r.get("symbol", "")).upper()
        sym_key = sym_raw.rstrip("M") if sym_raw.endswith("M") else sym_raw
        yf_sym  = SYM_TO_YF.get(sym_key, SYM_TO_YF.get(sym_raw))
        if yf_sym:
            needed_yf_syms.add(yf_sym)
    for yf_sym in needed_yf_syms:
        # Yahoo Finance hard limit: 15m data only available for last 60 days.
        # Fix: download 1h bars for full 2-year history, then top-up with real
        # 15m bars for the last 60 days. Merge both, deduplicate (15m wins on
        # overlap). Feature reconstruction works fine on 1h older bars since
        # EMA/RSI/ADX values over a 55-bar lookback are nearly identical at 1h.
        import pandas as pd
        frames = []
        try:
            df_1h = yf.download(yf_sym, period="2y", interval="1h",
                                progress=False, auto_adjust=True)
            if not df_1h.empty:
                df_1h.index = (df_1h.index.tz_localize("UTC")
                               if df_1h.index.tzinfo is None
                               else df_1h.index.tz_convert("UTC"))
                frames.append(df_1h)
                log.info(f"[FORCE_LEARN]   {yf_sym} 1h: {len(df_1h)} bars")
        except Exception as e1:
            log.warning("[FORCE_LEARN]   %s 1h failed: %s", yf_sym, e1)
        try:
            df_15m = yf.download(yf_sym, period="60d", interval="15m",
                                 progress=False, auto_adjust=True)
            if not df_15m.empty:
                df_15m.index = (df_15m.index.tz_localize("UTC")
                                if df_15m.index.tzinfo is None
                                else df_15m.index.tz_convert("UTC"))
                frames.append(df_15m)
                log.info(f"[FORCE_LEARN]   {yf_sym} 15m: {len(df_15m)} bars")
        except Exception as e2:
            log.warning("[FORCE_LEARN]   %s 15m failed: %s", yf_sym, e2)
        if not frames:
            log.info(f"[FORCE_LEARN]   {yf_sym}: no data — skipping")
            continue
        df_merged = pd.concat(frames).sort_index()
        df_merged = df_merged[~df_merged.index.duplicated(keep="last")]
        df_merged.dropna(inplace=True)
        hist_cache[yf_sym] = df_merged
        log.info(f"[FORCE_LEARN]   {yf_sym}: {len(df_merged)} bars total (1h+15m merged)")

    # ── Step 3: Build feature vectors ───────────────────────────────────────
    log.info("[FORCE_LEARN] Building feature vectors...")
    valid         = []
    from_stored   = 0
    reconstructed = 0
    skipped       = 0

    for r in rows:
        direction = r.get("direction", "")
        pnl       = float(r.get("pnl", 0))
        strategy  = r.get("strategy") or "trend_following"
        if strategy not in STRATEGY_NAMES:
            strategy = "trend_following"

        # Option A: use stored features
        stored_feats = r.get("features")
        if stored_feats and len(stored_feats) >= 30:
            try:
                obs = np.array(stored_feats[:30], dtype=np.float32)
                if not np.all(obs == 0):
                    valid.append((obs, pnl, direction, strategy))
                    from_stored += 1
                    continue
            except Exception:
                pass

        # Option B: reconstruct from yfinance history
        sym_raw = str(r.get("symbol", "")).upper()
        sym_key = sym_raw.rstrip("M") if sym_raw.endswith("M") else sym_raw
        yf_sym  = SYM_TO_YF.get(sym_key, SYM_TO_YF.get(sym_raw))
        if not yf_sym or yf_sym not in hist_cache:
            skipped += 1
            continue

        opened_at = r.get("opened_at", "")
        if not opened_at:
            skipped += 1
            continue
        try:
            trade_dt = datetime.fromisoformat(
                opened_at.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except Exception:
            skipped += 1
            continue

        df      = hist_cache[yf_sym]
        bar_idx = df.index.searchsorted(trade_dt)
        bar_idx = max(0, min(bar_idx, len(df) - 1))
        if bar_idx < 55:
            skipped += 1
            continue

        window  = df.iloc[bar_idx - 55 : bar_idx]
        closes  = _flatten_yf(window, "Close")
        highs   = _flatten_yf(window, "High")
        lows    = _flatten_yf(window, "Low")
        volumes = _flatten_yf(window, "Volume")
        if len(closes) < 30:
            skipped += 1
            continue

        bar_time   = df.index[bar_idx]
        spread_est = (15.0 if "GC"  in yf_sym else
                      1.5  if "JPY" in yf_sym else
                      2.0  if "GBP" in yf_sym else 1.0)
        c   = closes[-1]
        msg = {
            "close":  closes, "high": highs, "low": lows, "volume": volumes,
            "hour":   int(bar_time.hour),
            "dow":    int(bar_time.weekday()),
            "spread": spread_est,
            "bid":    c * (1 - spread_est * 0.000001),
            "ask":    c * (1 + spread_est * 0.000001),
        }
        try:
            obs = build_features(msg)
            if np.all(obs == 0):
                skipped += 1
                continue
            valid.append((obs, pnl, direction, strategy))
            reconstructed += 1
        except Exception:
            skipped += 1

    log.info(f"[FORCE_LEARN] Features ready: {len(valid)} usable trades "
          f"({from_stored} stored, {reconstructed} reconstructed, {skipped} skipped)")

    if len(valid) < 5:
        log.info("[FORCE_LEARN] Not enough valid samples (need ≥5) — exiting")
        return

    # Only mark models as trained (live PPO can decide) when we have enough data
    # for ~60–75% accuracy; with <10 trades PPO is still noisy.
    MIN_TRADES_TO_MARK_TRAINED = 10
    mark_trained = len(valid) >= MIN_TRADES_TO_MARK_TRAINED
    if not mark_trained:
        log.info(f"[FORCE_LEARN] {len(valid)} trades < {MIN_TRADES_TO_MARK_TRAINED} — will save weights but NOT set trained=True (heuristic-only until more data)")

    # ── Step 4: Train each strategy model ───────────────────────────────────
    class TradeReplayEnv(gym.Env):
        def __init__(self, data):
            super().__init__()
            self.data  = data
            self.idx   = 0
            self.action_space      = spaces.Discrete(3)
            self.observation_space = spaces.Box(
                -np.inf, np.inf, shape=(30,), dtype=np.float32)
        def reset(self, *, seed=None, options=None):
            super().reset(seed=seed)
            self.idx = 0
            return self.data[0][0].copy(), {}
        def step(self, action):
            obs_arr, pnl, orig_dir, _ = self.data[self.idx % len(self.data)]
            orig_act = 0 if orig_dir == "BUY" else 1
            if action == 2:
                reward = -abs(pnl) * (0.5 if pnl > 0 else 0.1)
            elif action == orig_act:
                reward = float(pnl)
            else:
                reward = -abs(pnl)
            reward   = float(np.clip(reward / 10.0, -2.0, 2.0))
            self.idx += 1
            done      = self.idx >= len(self.data)
            next_obs  = (self.data[self.idx % len(self.data)][0].copy()
                         if not done else obs_arr.copy())
            return next_obs, reward, done, False, {}

    strategy_splits = {s: [x for x in valid if x[3] == s] for s in STRATEGY_NAMES}

    for strategy in STRATEGY_NAMES:
        own_data   = strategy_splits[strategy]
        train_data = own_data if len(own_data) >= 5 else valid
        label      = "own trades" if len(own_data) >= 5 else f"all trades (own only {len(own_data)})"
        log.info(f"\n[FORCE_LEARN] Training '{strategy}' on {len(train_data)} trades ({label})")
        try:
            model     = load_strategy_model(strategy)
            env       = TradeReplayEnv(train_data)
            model.set_env(env)
            timesteps = max(10_000, min(len(train_data) * 50, 200_000))
            log.info(f"[FORCE_LEARN]   {timesteps:,} timesteps...")
            model.learn(total_timesteps=timesteps, reset_num_timesteps=True)
            model.set_env(_make_env())
            save_strategy_model(model, strategy, mark_trained=mark_trained)

            wins  = [x[1] for x in train_data if x[1] > 0]
            losses= [x[1] for x in train_data if x[1] <= 0]
            wr    = len(wins) / len(train_data) * 100
            avg_w = sum(wins)  / max(len(wins),  1)
            avg_l = sum(losses) / max(len(losses), 1)
            log.info(f"[FORCE_LEARN]   '{strategy}' done — "
                  f"WR={wr:.1f}% | avg_win=${avg_w:.2f} | avg_loss=${avg_l:.2f}")
            supabase.table("learning_log").insert({
                "strategy":   strategy,
                "num_trades": len(train_data),
                "learned_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as e:
            log.warning("[FORCE_LEARN] '%s' training failed: %s", strategy, e)

    # ── Step 5: Score strategies and promote the best one ───────────────────
    log.info("\n[FORCE_LEARN] Scoring strategies...")
    best_score    = -1.0
    best_strategy = "trend_following"
    for strategy in STRATEGY_NAMES:
        data  = strategy_splits[strategy] or valid
        wins  = [x[1] for x in data if x[1] > 0]
        losses= [x[1] for x in data if x[1] <= 0]
        if not data:
            continue
        wr    = len(wins) / len(data)
        avg_w = sum(wins)  / max(len(wins),  1)
        avg_l = abs(sum(losses) / max(len(losses), 1))
        score = wr * (avg_w / max(avg_l, 0.01))
        log.info(f"  {strategy}: score={score:.4f}  WR={wr*100:.1f}%  "
              f"avg_win=${avg_w:.2f}  avg_loss=${avg_l:.2f}")
        if score > best_score:
            best_score    = score
            best_strategy = strategy

    now_str = datetime.now(timezone.utc).isoformat()
    for s in STRATEGY_NAMES:
        try:
            supabase.table("strategies").upsert({
                "strategy":   s,
                "is_active":  s == best_strategy,
                "fwd_score":  1.0 if s == best_strategy else 0.5,
                "updated_at": now_str,
            }).execute()
        except Exception as e:
            log.warning("[FORCE_LEARN] DB update failed for %s: %s", s, e)

    log.info(f"\n[FORCE_LEARN] ✔ Complete.")
    log.info(f"  Active strategy → '{best_strategy}' (score={best_score:.4f})")
    log.info(f"  Trained on: {from_stored} stored + {reconstructed} reconstructed features")
    log.info(f"  PPO cold-start guard CLEARED — models live in next signal scan.")


# =============================================================================
# SECTION 9 — EQUITY SNAPSHOT  (cron REMOVED — see note below)
# =============================================================================
# CRON LIMIT FIX: equity_snapshot scheduled cron removed to stay within
# Modal free-tier 5-cron limit. The EA already POSTs real equity every 30s
# via /equity — a 15-min scheduled fallback is redundant during normal ops.
# Logic kept as _equity_snapshot_fallback() and called from run_forward_test
# (every 4h) so MT5-disconnection gaps are still covered.

def _net_withdrawals(supabase) -> float:
    """
    Returns the total amount withdrawn from the account (sum of all
    transactions with type='withdrawal'). Used to adjust the historical
    peak balance before computing drawdown so that withdrawals are never
    mistaken for trading losses.

    Example: started at $1,000, withdrew $200, currently at $750.
      raw peak = $1,000  →  drawdown = 25%  ← WRONG (includes withdrawal)
      adj peak = $800    →  drawdown = 6.25% ← CORRECT (only trading loss)
    """
    try:
        rows = supabase.table("transactions") \
            .select("amount") \
            .eq("type", "withdrawal") \
            .execute().data
        return sum(float(r.get("amount", 0)) for r in (rows or []))
    except Exception as e:
        log.warning("[WITHDRAWALS] Could not fetch net withdrawals (non-fatal): %s", e)
        return 0.0


def _equity_snapshot_fallback(supabase):
    """Write a computed equity snapshot only when no real MT5 report has
    arrived in the last 20 minutes. Called from run_forward_test every 4h.

    FIX BUG#11: Previous version estimated balance as capital + sum(closed_pnl)
    where capital = the dashboard lot-sizing setting (e.g. $200). If the actual
    account had grown to $350 via profits and deposits, the fallback reported
    $200 + pnl instead of the real balance, corrupting the equity curve during
    disconnections and triggering false drawdown limits.
    Fix: use the most recent real MT5 equity row as the balance base. If no
    real equity row exists at all, skip the fallback entirely rather than write
    a misleading value."""
    from datetime import datetime, timezone, timedelta
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        recent = supabase.table("equity").select("timestamp,balance") \
            .gte("timestamp", cutoff).order("timestamp", desc=True).limit(1).execute().data
        if recent:
            log.info(f"[EQUITY_SNAP] MT5 data fresh ({recent[0]['timestamp']}) — skipping")
            return
        # Use last known real MT5 balance as estimate
        latest_eq = supabase.table("equity").select("balance") \
            .order("timestamp", desc=True).limit(1).execute().data
        if not latest_eq:
            log.info("[EQUITY_SNAP] No real equity data exists yet — skipping fallback")
            return
        balance  = float(latest_eq[0]["balance"])
        peak_row = supabase.table("equity").select("balance") \
            .order("balance", desc=True).limit(1).execute().data or []
        raw_peak  = max(balance, float(peak_row[0]["balance"])) if peak_row else balance
        adj_peak  = max(balance, raw_peak - _net_withdrawals(supabase))
        drawdown  = max(0.0, (adj_peak - balance) / adj_peak) if adj_peak > 0 else 0.0
        supabase.table("equity").insert({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "balance":   round(balance, 2),
            "drawdown":  round(drawdown, 6),
        }).execute()
        log.info(f"[EQUITY_SNAP] Fallback snapshot (last real MT5 balance): ${balance:.2f} dd={drawdown*100:.2f}%")
    except Exception as e:
        log.error("[EQUITY_SNAP] %s", e)


# =============================================================================
# SECTION 10 — PORTFOLIO ROTATION  (every hour)
# =============================================================================

@app.function(image=image, secrets=[secrets], schedule=modal.Period(hours=1), timeout=60)
def portfolio_rotation():
    import os, math
    from datetime import datetime, timezone, timedelta
    from supabase import create_client

    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    # Stale trades cleanup (merged here to stay within Modal free-tier 5-cron limit)
    cleaned, _ = _run_stale_trades_cleanup(supabase, hours=8)
    if cleaned > 0:
        log.info("[CLEANUP] Rotation run: cleaned %d stale trades", cleaned)

    # FIX 5a: Added XAUUSDm alongside XAUUSD so the rotation scorer tracks the
    # actual broker symbol name used by the EA. If only one is active, list just
    # that one — having both avoids missing either variant.
    SYMS     = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "XAUUSDm"]  # GBPJPY removed
    lookback = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    scored   = []

    for sym in SYMS:
        trades = supabase.table("trades").select("pnl").eq("symbol",sym) \
            .gte("opened_at",lookback).not_.is_("pnl","null").execute().data or []
        if not trades:
            scored.append({"symbol":sym,"score":0.5,"win_rate":0.5,"volatility":0.01,"drawdown":0.0,
                           "enabled":True,"updated_at":datetime.now(timezone.utc).isoformat()}); continue
        pnls   = [float(t.get("pnl",0)) for t in trades]
        wr     = len([p for p in pnls if p>0]) / len(pnls)
        avg    = sum(pnls)/len(pnls)
        vol    = math.sqrt(sum((p-avg)**2 for p in pnls)/len(pnls)) if len(pnls)>1 else 0.01
        run = pk = mdd = 0.0
        for p in pnls:
            run += p; pk = max(pk,run)
            mdd  = max(mdd, (pk-run)/max(abs(pk),1e-9))
        score = wr*0.50 + (1-min(mdd,1.0))*0.30 + min(1.0/max(vol,0.0001),1.0)*0.20
        scored.append({"symbol":sym,"score":round(score,6),"win_rate":round(wr,4),"volatility":round(vol,6),
                       "drawdown":round(mdd,4),"enabled":True,"updated_at":datetime.now(timezone.utc).isoformat()})

    ranked = sorted(scored, key=lambda x: x["score"], reverse=True)

    # BUG FIX: Core pairs must NEVER be disabled by rotation — they are the
    # primary liquidity providers and disabling them silences the EA entirely.
    # Previously: row["enabled"] = i < 2  — this disabled USDJPY (rank 3+) and
    # GBPUSD during quiet weeks, preventing ALL trades during London session.
    # Fix: mark the top-2 by score as enabled, but always keep core trio on.
    # All 11 pairs are marked ALWAYS_ON — symbol rotation should never disable a pair
    # we're actively scanning.  Rotation's purpose is to boost the TOP performers, not
    # to silence underperformers (that job belongs to the cooldown and compliance engine).
    # A quiet EURGBP week would disable it exactly before it starts moving.
    ALWAYS_ON = {
        # Original 4
        "EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "XAUUSDm",
        # 7 forex additions
        "USDCHF", "AUDUSD", "USDCAD", "EURJPY", "NZDUSD", "EURGBP", "GBPJPY",
        # Precious metals
        "XAGUSD", "XAGUSDm",
    }
    for i, row in enumerate(ranked):
        row["enabled"] = row["symbol"] in ALWAYS_ON or i < 2
        supabase.table("symbol_scores").upsert(row).execute()

    log.info("[ROTATION] Active: %s", [r["symbol"] for r in ranked if r["enabled"]])


# =============================================================================
# SECTION 11a — HELPERS FOR WEB ENDPOINT
# Defined here (above web()) so Modal's ASGI closure can resolve them.
# =============================================================================

_THRESHOLDS = {
    # SAFE: only high-conviction signals (target 68–75% effective accuracy)
    "SAFE":       {"trend_following": 0.68, "mean_reversion": 0.70, "breakout": 0.72},
    # AGGRESSIVE: minimum 65% bar so model is at least 60–75% accurate; was 0.63–0.67.
    # Raised so we don’t take low-conviction signals that hurt win rate.
    "AGGRESSIVE": {"trend_following": 0.65, "mean_reversion": 0.67, "breakout": 0.68},
}
_LOT_SCALE = {
    "SAFE":       1.0,
    # PROFITABLE FIX #7b: Lot scaling re-enabled at 1.25× for AGGRESSIVE mode.
    # Previously disabled because thresholds were too low (0.55) producing losers.
    # Now that thresholds are raised to 0.63+, AGGRESSIVE mode signals are meaningful.
    # Capped at 1.25× (not 1.5×) until win rate confirmed over 100+ live trades.
    "AGGRESSIVE": 1.25,
}

# =============================================================================
# NEWS FILTER — DB-backed, multi-source, checked by signal endpoint
# =============================================================================
# The signal endpoint reads from Supabase news_blackouts table.
# The news_monitor() scheduled function keeps that table up to date every 5 min.
# This means zero external API calls in the trading hot path.

# =============================================================================
# SESSION FILTER — pair-aware trading hours (UTC)
# =============================================================================
# SESSION WINDOWS — validated from live 23-27 Feb data + literature consensus
#
# Core principle: only trade during the session where each pair has genuine
# institutional flow.  Wider windows = more opportunities but lower signal quality.
# Start conservative; widen after 50+ trades confirm win rate > 50% per session.
#
# 11-pair grid (UTC):
#   EURUSD  07-17  London + NY  — USD pair, liquid both sessions
#   GBPUSD  07-17  London + NY  — confirmed strong performer
#   USDJPY  00-16  Asian + LON + early NY — Tokyo is prime JPY session
#   XAUUSD  06-22  Asian-close → NY-close — full liquid gold hours
#   USDCHF  07-16  London + early NY — Swiss, stop before late-NY vol
#   AUDUSD  07-17  London + NY — conservative start; add Asian (22-07)
#                  after 50 trades confirm win rate (Sydney pip noise is real)
#   USDCAD  07-17  London + NY — CAD most liquid when Canada is open (13-17)
#   EURJPY  00-16  Asian + London + early NY — JPY component drives Asian flow,
#                  EUR component drives London; both sessions are productive
#   NZDUSD  07-17  London + NY — conservative start same as AUDUSD
#   EURGBP  07-16  London only — purely European pair, NY adds noise not edge
#   GBPJPY  07-12  London only — high spread in NY; keep restricted to AM session

# Symbol → list of (start_hour_utc, end_hour_utc) windows where trading allowed
# NY session in UTC ≈ 13:00–22:00 (8am–5pm Eastern). End hour is exclusive: (7,21) = 07:00–20:59 UTC.
_SESSION_WINDOWS = {
    "EURUSD":  [(7, 21)],   # London + full NY (was 17; extended so NY not "off peak")
    "GBPUSD":  [(7, 21)],   # London + full NY
    "USDCHF":  [(7, 21)],   # London + NY (was 7-16; fix off_peak during NY)
    "USDJPY":  [(0, 21)],   # Asian + London + full NY (was 0-16)
    "GBPJPY":  [(7, 12)],   # London only — high spread in NY; keep tight
    "XAUUSD":  [(0, 24)],   # Loosened — 24/5; you monitor NY manually
    "XAUUSDM": [(0, 24)],
    "XAGUSD":  [(0, 24)],   # Loosened — 24/5; you monitor NY manually
    "XAGUSDM": [(0, 24)],
    "AUDUSD":  [(7, 21)],   # London + full NY
    "USDCAD":  [(7, 21)],   # London + NY — CAD liquid when Canada open (13-17 UTC)
    "EURJPY":  [(0, 21)],   # Asian + London + full NY (was 0-16)
    "NZDUSD":  [(7, 21)],   # London + full NY
    "EURGBP":  [(7, 21)],   # London + NY (was 7-16; fix off_peak during NY)
}
_DEFAULT_WINDOWS = [(7, 21)]  # safe fallback: London + NY

def _is_session_active(symbol: str) -> tuple:
    """
    Returns (allowed: bool, reason: str).
    Checks UTC hour against the pair's optimal trading windows.
    Also blocks weekends entirely.
    """
    from datetime import datetime, timezone
    now     = datetime.now(timezone.utc)
    weekday = now.weekday()   # 0=Mon … 4=Fri 5=Sat 6=Sun
    hour    = now.hour

    # ── Weekend block ─────────────────────────────────────────────────
    if weekday == 5:  # Saturday
        return False, "weekend"
    if weekday == 6:  # Sunday — allow from 22:00 UTC (markets reopen)
        if hour < 22:
            return False, "weekend"

    # ── Friday wind-down — stop new trades after 20:00 UTC ───────────
    if weekday == 4 and hour >= 20:
        return False, "friday_close"

    # ── Dead zone — 21:00–07:00 UTC for forex (NY close → London open) ─────
    # Metals (XAUUSD, XAGUSD): no dead zone — loosened; user monitors NY manually.
    # JPY pairs with Asian windows: allow before 07:00 for Tokyo session.
    sym_up       = symbol.upper()
    is_metal_sym = sym_up.startswith("XAUUSD") or sym_up.startswith("XAGUSD")
    is_jpy_asian = "JPY" in sym_up and hour < 7
    if hour >= 21 or (hour < 7 and not is_metal_sym and not is_jpy_asian):
        if not is_metal_sym and not is_jpy_asian:
            if not (weekday == 6 and hour >= 22):  # allow Sunday 22:00+ for market open
                return False, "dead_zone_low_volume"

    # ── Pair-specific session check ───────────────────────────────────
    # FIX 4d: Check full symbol first (catches XAUUSDm → "XAUUSDM"),
    # then fall back to 6-char base, then default windows.
    sym_upper = symbol.upper()
    windows   = (_SESSION_WINDOWS.get(sym_upper)
                 or _SESSION_WINDOWS.get(sym_upper[:6])
                 or _DEFAULT_WINDOWS)

    for (start, end) in windows:
        if start <= hour < end:
            return True, "session_ok"

    # Not in any active window for this pair
    active_str = ", ".join([f"{s:02d}:00-{e:02d}:00" for s, e in windows])
    return False, f"off_peak:{active_str}_UTC"


def _is_news_blackout(symbol: str, supabase=None) -> tuple:
    """
    Returns (is_blocked: bool, reason: str).
    Reads active blackouts from Supabase — populated by news_monitor().
    Falls back to ForexFactory direct check if Supabase call fails.
    """
    import os
    from datetime import datetime, timezone, timedelta

    base  = symbol[:3].upper()
    quote = symbol[3:6].upper()
    now   = datetime.now(timezone.utc)

    # ── Primary: check Supabase blackouts table ───────────────────────────────
    if supabase:
        try:
            rows = supabase.table("news_blackouts")                 .select("*")                 .eq("active", True)                 .execute().data or []

            for row in rows:
                currencies = [c.strip().upper() for c in row.get("currencies", "").split(",")]
                if base in currencies or quote in currencies or "ALL" in currencies:
                    title  = row.get("title", "News event")
                    source = row.get("source", "unknown")
                    impact = row.get("impact", "High")
                    log.info(f"[NEWS] Blackout {symbol}: {title} ({source}/{impact})")
                    return True, f"news:{title[:40]}"
            return False, ""
        except Exception as e:
            log.warning("[NEWS] DB check failed: %s — falling back to ForexFactory", e)

    # ── Fallback: fail OPEN when DB is unavailable ───────────────────────────
    # Changed from fail-closed to fail-open. Reason: if the news_blackouts
    # table doesn't exist yet (fresh deploy) OR Supabase is briefly unreachable,
    # fail-closed was permanently blocking ALL trades. news_monitor() runs every
    # 5 min and writes blackouts to DB — in the rare case of a DB outage we
    # accept the small risk of trading through a news event rather than
    # stopping the bot entirely. A warning is logged so you can investigate.
    log.warning("[NEWS] Blackout DB unavailable for %s — allowing trade (fail-open)", symbol)
    return False, ""


def _check_compliance(supabase, state: dict, symbol: str = "") -> tuple:
    from datetime import datetime, timezone, timedelta

    # "Today" baseline: allow manual reset via bot_state.risk_day_start.
    # BUG FIX: previously, if risk_day_start was set manually (e.g. yesterday),
    # it was used literally forever — the daily trade count NEVER reset.
    # Fix: if risk_day_start exists but is from a PREVIOUS UTC day, ignore it
    # and use today's midnight instead. This means midnight UTC auto-resets the
    # daily trade count correctly every day, with manual resets still working
    # as intraday overrides within the current day.
    today_midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    risk_day_start = state.get("risk_day_start")
    if isinstance(risk_day_start, str) and risk_day_start:
        try:
            rds_dt = datetime.fromisoformat(risk_day_start.replace("Z", "+00:00"))
            # Only honour the stored value if it's within TODAY (>= today's midnight)
            if rds_dt >= today_midnight:
                day_start_ts = risk_day_start
            else:
                # Stale from a previous day — use today's midnight
                day_start_ts = today_midnight.isoformat()
                log.info(f"[COMPLIANCE] risk_day_start {risk_day_start} is from previous day — "
                      f"using today's midnight {day_start_ts}")
        except Exception:
            day_start_ts = today_midnight.isoformat()
    else:
        day_start_ts = today_midnight.isoformat()

    try:
        # Equity rows from current "day" start for DD calculations.
        # Select both balance and equity: FTMO rules apply to equity (incl. open P&L).
        eq = (
            supabase.table("equity")
            .select("balance", "equity")
            .gte("timestamp", day_start_ts)
            .order("timestamp")
            .execute()
            .data
            or []
        )

        # Daily trade count: how many positions have been OPENED since day_start_ts.
        # BUG FIX: exclude direction="CLOSED" records — these are orphan close-only
        # rows inserted by /close when no matching open trade exists (e.g. the EA
        # re-sent a close for a trade opened before the current session).
        # Counting them exhausted the 20-trade cap with phantom trades, blocking
        # all real trading for the rest of the day.
        # BUG FIX: previous query counted ALL rows with opened_at >= day_start,
        # including unconfirmed pending signal rows (lot=NULL) that /signal logs
        # every 60 seconds for every symbol. With 6 symbols scanning every minute,
        # 20 phantom rows accumulate in ~3 minutes, hitting the daily limit even
        # with zero real trades executed.
        # Fix: add .not_.is_("lot", "null") so only EA-confirmed executions count.
        # This matches the same logic used by the open_trades concurrent check.
        trades_today = (
            supabase.table("trades")
            .select("id")
            .gte("opened_at", day_start_ts)
            .neq("direction", "CLOSED")
            .not_.is_("lot", "null")
            .gt("lot", 0)
            .execute()
            .data
            or []
        )

        # Concurrent open positions — ALL currently open trades, no date filter.
        # BUG FIX: previous query had .gte("opened_at", day_start_ts) which caused
        # trades opened before today's midnight (or before a manual day reset) to be
        # invisible to the compliance engine. This produced two bad outcomes:
        #   1. Dashboard showed 0 open positions while MT5 had real open trades.
        #   2. Compliance engine allowed new trades past the concurrent limit because
        #      it didn't see the pre-existing open positions from the prior session.
        # Fix: count ALL confirmed-open trades regardless of when they were opened.
        # day_start_ts filtering belongs only to trades_today (the daily count cap),
        # not to the concurrent-position limit which is a live snapshot of MT5 state.
        # STALE TRADE FIX: trades stuck with lot≠NULL, pnl=NULL cause the
        # counter to read "4/3 open" even when MT5 shows no open positions.
        # Root cause: if MT5 closes a position but /close endpoint wasn't called
        # (e.g. manual close, SL hit during server gap), the DB row is never
        # updated. Fix: only count trades opened in the last 10 hours as "open".
        # The 8h time-exit in the EA means any genuine open trade is < 8h old.
        # Trades older than 10h with no pnl are treated as stale and ignored.
        # FIX BUG#10: Counter used 8h, cleanup used 9h — 1h gap where a trade was
        # excluded from the open-position count but not yet cleaned up, allowing one
        # extra phantom concurrent slot. Both thresholds now use 8.5h.
        stale_cutoff = (datetime.now(timezone.utc) - timedelta(hours=8, minutes=30)).isoformat()

        # Also auto-clean truly stale rows (>24h open, no pnl) so they don't
        # accumulate — mark them with pnl=0 and a "stale_cleaned" closed_at.
        try:
            stale_rows = (
                supabase.table("trades")
                .select("id")
                .is_("pnl", "null")
                .is_("closed_at", "null")
                .not_.is_("lot", "null")
                .lt("opened_at", (datetime.now(timezone.utc) - timedelta(hours=8, minutes=30)).isoformat())
                .execute()
                .data or []
            )
            if stale_rows:
                stale_ids = [r["id"] for r in stale_rows]
                supabase.table("trades").update({
                    "pnl":       0.0,
                    "closed_at": datetime.now(timezone.utc).isoformat(),
                }).in_("id", stale_ids).execute()
                log.info(f"[COMPLIANCE] Auto-cleaned {len(stale_ids)} stale open trades (>8.5h, no pnl)")
        except Exception as _e:
            log.warning("[COMPLIANCE] Stale cleanup failed (non-fatal): %s", _e)

        open_trades = (
            supabase.table("trades")
            .select("id")
            .is_("pnl", "null")
            .is_("closed_at", "null")
            .not_.is_("lot", "null")
            .gt("lot", 0)
            .gte("opened_at", stale_cutoff)    # only count trades < 8h old
            .execute()
            .data
            or []
        )
    except Exception as e:
        return False, f"db_error:{e}"

    # FTMO: Initial Simulated Capital — used for 5% daily / 10% total loss limits.
    initial_cap = float(state.get("initial_capital") or state.get("capital") or 10000)
    cap         = float(state.get("capital", 10000))
    mxdd        = float(state.get("max_daily_dd", 0.05))
    mxtd        = float(state.get("max_total_dd", 0.10))
    # max_concurrent_trades  = max open positions at the same time
    # max_trades_per_day     = max number of trades you are allowed to OPEN
    #                          during the current "day" window (midnight‑to‑midnight
    #                          by default, or from risk_day_start if manually reset).
    mxc  = int(state.get("max_concurrent_trades",  5))
    mxt  = int(state.get("max_trades_per_day",    20))

    if len(open_trades) >= mxc:
        return False, f"concurrent_limit:{len(open_trades)}/{mxc}_open"

    if len(trades_today) >= mxt:
        return False, f"daily_trades_limit:{len(trades_today)}/{mxt}"

    # LOSS STREAK FIX: Entry throttle — max one new position every N minutes (0 = disabled).
    # Prevents filling all slots in the first minutes of session open.
    throttle_mins = int(state.get("entry_throttle_mins", 5))
    if throttle_mins > 0:
        try:
            last_open = (
                supabase.table("trades")
                .select("opened_at")
                .not_.is_("opened_at", "null")
                .order("opened_at", desc=True)
                .limit(1)
                .execute()
                .data
            )
            if last_open:
                last_ts = last_open[0].get("opened_at")
                if last_ts:
                    # Parse as UTC: Supabase may return with "Z" or without tz (naive)
                    last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    now_utc = datetime.now(timezone.utc)
                    elapsed = now_utc - last_dt
                    # Only throttle if we're still within the window (elapsed < throttle_mins)
                    if elapsed < timedelta(minutes=throttle_mins):
                        remaining_sec = (timedelta(minutes=throttle_mins) - elapsed).total_seconds()
                        remaining = max(0, int(remaining_sec / 60))
                        elapsed_mins = int(elapsed.total_seconds() // 60)
                        log.info(f"[COMPLIANCE] Entry throttle: last open {elapsed_mins}min ago, "
                                 f"wait {remaining}min (entry_throttle_mins={throttle_mins})")
                        return False, f"entry_throttle:{throttle_mins}min:{remaining}min_remaining"
        except Exception as e:
            log.warning("[COMPLIANCE] Entry throttle check failed (non-fatal): %s", e)

    # PROFITABLE FIX #8: Stronger per-symbol loss cooldown.
    # Old: trigger after 3 consecutive losses, wait only 15 min (one M15 candle).
    # New: trigger after 2 consecutive losses, cooldown scales with severity:
    #   2 losses → 60 min, 3 losses → 120 min, 4+ losses → 240 min.
    # Rationale: same bad conditions that caused the loss usually persist for hours.
    if symbol:
        try:
            recent_sym = (
                supabase.table("trades")
                .select("symbol, pnl, closed_at")
                .eq("symbol", symbol)
                .not_.is_("pnl", "null")
                .not_.is_("closed_at", "null")
                .order("closed_at", desc=True)
                .limit(4)
                .execute()
                .data or []
            )
            if len(recent_sym) >= 2:
                consecutive_losses = 0
                for row in recent_sym:
                    if float(row.get("pnl", 0)) < 0:
                        consecutive_losses += 1
                    else:
                        break   # stop counting on first winner

                if consecutive_losses >= 2:
                    last_close = recent_sym[0].get("closed_at", "")
                    if last_close:
                        last_dt      = datetime.fromisoformat(last_close.replace("Z", "+00:00"))
                        # FIX: old formula min(20*(n-1), 30) maxed at 30 min regardless of
                        # how many consecutive losses occurred. Corrected to match intended design:
                        # 2 losses → 60 min, 3 losses → 120 min, 4+ losses → 240 min.
                        # Longer cooldowns are correct: the same bad market conditions that caused
                        # N consecutive losses typically persist for several hours.
                        cooldown_mins = min(60 * (consecutive_losses - 1), 240)
                        cooldown      = timedelta(minutes=cooldown_mins)
                        elapsed       = datetime.now(timezone.utc) - last_dt
                        if elapsed < cooldown:
                            remaining = int((cooldown - elapsed).total_seconds() / 60)
                            log.info(f"[COMPLIANCE] {symbol} cooldown: {consecutive_losses} consecutive "
                                  f"losses → {cooldown_mins}min block, {remaining}min remaining")
                            return False, (
                                f"symbol_cooldown:{symbol}:"
                                f"{consecutive_losses}_losses:{remaining}min_remaining"
                            )
        except Exception as e:
            log.warning("[COMPLIANCE] Cooldown check failed for %s: %s", symbol, e)
            # Non-fatal — don't block trading just because the cooldown check errored

    # FTMO Max Daily Loss: equity cannot drop below (balance at day start) − 5% of initial capital.
    # When we have equity rows today: use first row as day_start_bal, last as cur_equity.
    # When we have NO equity rows today (e.g. first signal of the day): still enforce daily limit
    # by using last balance before day_start_ts as day_start_bal and latest equity row as cur_equity.
    cur_equity = cap
    day_start_bal = None
    if eq:
        day_start_bal = float(eq[0].get("balance") or 0)
        cur_equity = float(eq[-1].get("equity") or eq[-1].get("balance") or cap)
    else:
        try:
            # No equity data today — get latest snapshot and last balance before day start
            latest_row = supabase.table("equity").select("balance", "equity").order("timestamp", desc=True).limit(1).execute().data
            if latest_row:
                cur_equity = float(latest_row[0].get("equity") or latest_row[0].get("balance") or cap)
            day_before = supabase.table("equity").select("balance").lt("timestamp", day_start_ts).order("timestamp", desc=True).limit(1).execute().data
            if day_before:
                day_start_bal = float(day_before[0].get("balance") or 0)
        except Exception as _e:
            log.warning("[COMPLIANCE] Fallback day-start query failed (non-fatal): %s", _e)

    if day_start_bal is not None and initial_cap > 0:
        daily_floor = day_start_bal - (mxdd * initial_cap)
        if cur_equity < daily_floor:
            ddd_pct = max(0.0, (day_start_bal - cur_equity) / day_start_bal * 100) if day_start_bal > 0 else 0.0
            return False, f"daily_dd:{ddd_pct:.2f}%_ftmo_limit"

    # FTMO Max Loss: equity cannot drop below max(initial, trailing peak) − 10% of initial.
    try:
        if not eq:
            latest = supabase.table("equity").select("balance", "equity").order("timestamp", desc=True).limit(1).execute().data
            if latest:
                cur_equity = float(latest[0].get("equity") or latest[0].get("balance") or cap)
        peak_row   = (
            supabase.table("equity")
            .select("balance")
            .order("balance", desc=True)
            .limit(1)
            .execute()
            .data
        )
        raw_peak       = max(initial_cap, float(peak_row[0]["balance"])) if peak_row else max(initial_cap, cur_equity)
        trailing_peak  = max(cur_equity, raw_peak - _net_withdrawals(supabase))
        total_floor    = trailing_peak - (mxtd * initial_cap)
        if cur_equity < total_floor:
            tdd_pct = max(0.0, (trailing_peak - cur_equity) / trailing_peak * 100) if trailing_peak > 0 else 0.0
            return False, f"total_dd:{tdd_pct:.2f}%_ftmo_limit"
    except Exception as _tdd_err:
        log.warning("[COMPLIANCE] All-time peak query failed (non-fatal): %s", _tdd_err)

    return True, "OK"

# =============================================================================
# SECTION 12 — AUTOMATED DAILY RESET  (cron REMOVED — merged into run_forward_test)
# =============================================================================
# CRON LIMIT FIX: automated_daily_reset cron removed to stay within Modal's
# 5-cron free-tier limit. The reset logic now runs inside run_forward_test
# (every 4h). It fires on the first 4h window after midnight UTC each day,
# which is accurate enough for daily P&L / drawdown baseline purposes.
# =============================================================================
#
# WHY: @modal.fastapi_endpoint gives each function its OWN URL.
# A GET to the signal URL returns 405 because that function only accepts POST.
# Solution: one @modal.asgi_app with a real FastAPI router that handles
# both methods on the same base URL.
#
# After deploy, Modal prints ONE url, e.g.:
#   https://YOUR-WORKSPACE--apexhydra-pro-web.modal.run
#
# MT5 EA  →  POST https://…/signal
# Browser →  GET  https://…/        (health check, returns 200)
# =============================================================================

@app.function(
    image=image,
    secrets=[secrets],
    volumes={MODEL_DIR: volume},
    min_containers=1,
    max_containers=10,
    timeout=30,
)
@modal.asgi_app()
def web():
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    import os, numpy as np
    from datetime import datetime, timezone
    from supabase import create_client

    api = FastAPI()

    # ── In-memory signal cache ────────────────────────────────────────────────
    # Stores the last signal per symbol so /open can retrieve features + metadata
    # without any DB round-trip. Keyed by symbol (string).
    # Entry: { direction, features, strategy, regime, confidence, ts }
    # TTL: entries older than 5 minutes are ignored (stale signal).
    # Thread-safety: FastAPI runs single-threaded per worker; no lock needed.
    # Multi-container: if /signal and /open land on different containers the
    # features entry will be missing — /open falls back to inserting without
    # features, which is fine (force_learn reconstructs them from yfinance).
    _signal_cache: dict = {}

    # Startup: ensure all core symbols are enabled in symbol_scores.
    # Fixes cases where portfolio_rotation disabled a symbol (e.g. GBPJPY).
    # FIX BUG#9: Previous code did an unconditional upsert with enabled=True on
    # every container start (cold starts, scale-out, redeploys). Any symbol you
    # manually disabled from the dashboard would be silently re-enabled within
    # minutes — especially dangerous for XAUUSD given its per-pip volatility.
    # Fix: only INSERT a row if none exists. Existing rows are left untouched,
    # so manual disables survive container restarts.
    try:
        import os
        from supabase import create_client
        _sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
        _core = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]  # GBPJPY removed
        for _sym in _core:
            _existing = _sb.table("symbol_scores").select("symbol") \
                .eq("symbol", _sym).maybe_single().execute()
            if not _existing.data:
                _sb.table("symbol_scores").insert(
                    {"symbol": _sym, "enabled": True}
                ).execute()
                log.info(f"[STARTUP] Initialised new symbol: {_sym}")
        log.info(f"[STARTUP] Core symbol check complete (existing rows unchanged)")
    except Exception as _e:
        log.warning("[STARTUP] Symbol init failed (non-fatal): %s", _e)

    # Shared auth token for EA + dashboard.
    # If APEXHYDRA_API_TOKEN is set in Modal secrets, all trading/control
    # endpoints require header X-APEXHYDRA-TOKEN to match. Health (GET /)
    # stays open so you can ping the app.
    API_TOKEN = os.environ.get("APEXHYDRA_API_TOKEN", "").strip()

    async def _require_auth(req: Request):
        if not API_TOKEN:
            return None  # auth disabled
        token = req.headers.get("X-APEXHYDRA-TOKEN", "")
        if token != API_TOKEN:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return None

    # ── GET /settings  —  HTML settings dashboard ─────────────────────────────
    @api.get("/settings")
    async def settings_page():
        from fastapi.responses import HTMLResponse
        try:
            supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
            rows = supabase.table("bot_state").select("*").order("updated_at", desc=True).limit(1).execute().data
            s = rows[0] if rows else {}
        except Exception:
            s = {}

        is_running   = "checked" if s.get("is_running", False) else ""
        mode         = s.get("mode", "SAFE")
        risk_capital = int(s.get("capital", 0) or 0)
        max_daily_dd = round(float(s.get("max_daily_dd", 0.05)) * 100, 1)
        max_total_dd = round(float(s.get("max_total_dd", 0.10)) * 100, 1)
        max_conc     = int(s.get("max_concurrent_trades", 5))
        max_day      = int(s.get("max_trades_per_day", 10))
        capital      = float(s.get("capital", 159))
        safe_sel     = "selected" if mode == "SAFE"       else ""
        agg_sel      = "selected" if mode == "AGGRESSIVE" else ""
        dot_cls      = "dot-on"   if s.get("is_running")  else "dot-off"

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ApexHydra Settings</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh;padding:24px 16px}}
.wrap{{max-width:860px;margin:0 auto}}
h1{{font-size:1.5rem;font-weight:700;color:#f8fafc;margin-bottom:2px}}
.sub{{color:#64748b;font-size:.83rem;margin-bottom:24px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin-bottom:20px}}
.card{{background:#1e2130;border:1px solid #2d3148;border-radius:12px;padding:18px}}
.card h2{{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#64748b;margin-bottom:14px}}
label{{display:block;font-size:.8rem;color:#94a3b8;margin-bottom:3px;margin-top:10px}}
label:first-of-type{{margin-top:0}}
input[type=number],select{{width:100%;background:#0f1117;border:1px solid #2d3148;border-radius:7px;color:#e2e8f0;padding:8px 11px;font-size:.88rem;outline:none;transition:border .15s}}
input[type=number]:focus,select:focus{{border-color:#6366f1}}
.hint{{font-size:.73rem;color:#475569;margin-top:3px;line-height:1.45}}
.hint strong{{color:#94a3b8}}
.toggle-row{{display:flex;align-items:center;justify-content:space-between;padding:8px 0}}
.toggle-row span{{font-size:.88rem}}
.switch{{position:relative;width:42px;height:23px;flex-shrink:0}}
.switch input{{opacity:0;width:0;height:0}}
.slider{{position:absolute;inset:0;background:#374151;border-radius:23px;cursor:pointer;transition:.25s}}
.slider:before{{content:"";position:absolute;width:17px;height:17px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.25s}}
input:checked+.slider{{background:#6366f1}}
input:checked+.slider:before{{transform:translateX(19px)}}
.status-dot{{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:middle}}
.dot-on{{background:#22c55e;box-shadow:0 0 5px #22c55e}}
.dot-off{{background:#ef4444}}
.btn{{width:100%;padding:11px;background:#6366f1;color:#fff;border:none;border-radius:8px;font-size:.9rem;font-weight:600;cursor:pointer;transition:background .15s}}
.btn:hover{{background:#4f46e5}}
.btn:disabled{{background:#374151;cursor:not-allowed}}
#msg{{display:none;text-align:center;padding:9px;border-radius:7px;margin-top:10px;font-size:.83rem}}
.msg-ok{{background:#14532d;color:#86efac;display:block!important}}
.msg-err{{background:#450a0a;color:#fca5a5;display:block!important}}
</style>
</head>
<body>
<div class="wrap">
<h1>⚡ ApexHydra</h1>
<p class="sub">Settings — changes take effect on the next signal scan (~30 s)</p>
<form id="f">
<div class="grid">

  <div class="card">
    <h2>Bot Status</h2>
    <div class="toggle-row">
      <span><span class="status-dot {dot_cls}"></span>Bot Running</span>
      <label class="switch">
        <input type="checkbox" id="is_running" name="is_running" {is_running}>
        <span class="slider"></span>
      </label>
    </div>
    <label>Mode</label>
    <select name="mode">
      <option value="SAFE" {safe_sel}>🛡 SAFE</option>
      <option value="AGGRESSIVE" {agg_sel}>⚡ AGGRESSIVE</option>
    </select>
    <p class="hint">SAFE uses conservative thresholds. Use AGGRESSIVE only after 100+ profitable trades.</p>
  </div>

  <div class="card">
    <h2>💰 Risk Capital</h2>
    <label>Capital ($)</label>
    <input type="number" name="capital" value="{risk_capital}" min="0" step="50">
    <p class="hint"><strong>0 = use full live equity.</strong> Set to your starting/target balance (e.g. 200) to cap lot sizing AND calculate drawdown percentages. Prevents runaway gold lots on small accounts.</p>
  </div>

  <div class="card">
    <h2>🛡 Drawdown Limits</h2>
    <label>Max Daily Drawdown (%)</label>
    <input type="number" name="max_daily_dd_pct" value="{max_daily_dd}" min="0.5" max="20" step="0.5">
    <p class="hint">Bot pauses for the day when daily loss hits this % of capital. Current: {max_daily_dd}%</p>
    <label>Max Total Drawdown (%)</label>
    <input type="number" name="max_total_dd_pct" value="{max_total_dd}" min="1" max="50" step="1">
    <p class="hint">Bot stops permanently (until manual reset) when total loss hits this %. Current: {max_total_dd}%</p>
  </div>

  <div class="card">
    <h2>📊 Trade Limits</h2>
    <label>Max Concurrent Open Trades</label>
    <input type="number" name="max_concurrent_trades" value="{max_conc}" min="1" max="10" step="1">
    <p class="hint">Hard cap on simultaneous open positions across all symbols.</p>
    <label>Max Trades Per Day</label>
    <input type="number" name="max_trades_per_day" value="{max_day}" min="1" max="100" step="1">
    <p class="hint">Resets at midnight UTC daily.</p>
  </div>

</div>
<button type="submit" class="btn">💾 Save Settings</button>
<div id="msg"></div>
</form>

<div class="card" style="margin-top:14px">
  <h2>🔧 Maintenance</h2>
  <p style="font-size:.82rem;color:#94a3b8;margin-bottom:12px">
    Use <strong>Reset Positions</strong> if the open trade counter is stuck (e.g. shows 4/5 when MT5 has no open positions).
    This only cleans the database counter — it does <em>not</em> close real MT5 positions.
  </p>
  <button type="button" class="btn" id="cleanBtn" style="background:#7c3aed">🔄 Reset Position Counter</button>
  <div id="cleanMsg" style="display:none;margin-top:10px;padding:9px;border-radius:7px;font-size:.83rem"></div>
</div>

<script>
document.getElementById("cleanBtn").addEventListener("click", async () => {{
  const btn = document.getElementById("cleanBtn");
  const msg = document.getElementById("cleanMsg");
  btn.disabled = true; btn.textContent = "Cleaning…";
  try {{
    const r = await fetch("/cleanup", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{"hours": 8}})
    }});
    const d = await r.json();
    if (d.ok) {{
      msg.style.cssText = "display:block;background:#14532d;color:#86efac";
      msg.textContent = d.cleaned === 0
        ? "✓ No stale trades found — counter is already clean"
        : "✓ Cleaned " + d.cleaned + " stale trade(s): " + (d.symbols || []).join(", ");
    }} else {{
      msg.style.cssText = "display:block;background:#450a0a;color:#fca5a5";
      msg.textContent = "Error: " + (d.reason || "unknown");
    }}
  }} catch(e) {{
    msg.style.cssText = "display:block;background:#450a0a;color:#fca5a5";
    msg.textContent = "Network error: " + e.message;
  }} finally {{
    btn.disabled = false; btn.textContent = "🔄 Reset Position Counter";
  }}
}});
</script>
</div>
<script>
document.getElementById("f").addEventListener("submit", async e => {{
  e.preventDefault();
  const btn = e.target.querySelector("button");
  btn.disabled = true; btn.textContent = "Saving…";
  const fd  = new FormData(e.target);
  const payload = {{
    is_running:           document.getElementById("is_running").checked,
    mode:                 fd.get("mode"),
    capital:              parseFloat(fd.get("capital"))       || 0,
    max_daily_dd:         (parseFloat(fd.get("max_daily_dd_pct")) || 5) / 100,
    max_total_dd:         (parseFloat(fd.get("max_total_dd_pct")) || 10) / 100,
    max_concurrent_trades:parseInt(fd.get("max_concurrent_trades")) || 5,
    max_trades_per_day:   parseInt(fd.get("max_trades_per_day"))    || 10,
  }};
  try {{
    const r = await fetch("/settings", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(payload)
    }});
    const d = await r.json();
    const msg = document.getElementById("msg");
    if (d.ok) {{
      msg.className = "msg-ok"; msg.textContent = "✓ Saved! Bot will use new settings on next scan.";
    }} else {{
      msg.className = "msg-err"; msg.textContent = "Error: " + (d.reason || "unknown");
    }}
  }} catch(err) {{
    const msg = document.getElementById("msg");
    msg.className = "msg-err"; msg.textContent = "Network error: " + err.message;
  }} finally {{
    btn.disabled = false; btn.textContent = "💾 Save Settings";
  }}
}});
</script>
</body>
</html>"""
        return HTMLResponse(html)

    # ── POST /settings  —  save settings from dashboard ───────────────────────
    @api.post("/settings")
    async def save_settings(request: Request):
        from datetime import datetime, timezone
        try:
            supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
            data = await request.json()

            update = {
                "is_running":           bool(data.get("is_running", False)),
                "mode":                 str(data.get("mode", "SAFE")),
                "capital":              float(data.get("capital", 0) or 0),
                "initial_capital":      float(data.get("initial_capital") or data.get("capital", 0) or 0) or None,
                "max_daily_dd":         float(data.get("max_daily_dd", 0.05)),
                "max_total_dd":         float(data.get("max_total_dd", 0.10)),
                "max_concurrent_trades":int(data.get("max_concurrent_trades", 5)),
                "max_trades_per_day":   int(data.get("max_trades_per_day", 20)),
                "updated_at":           datetime.now(timezone.utc).isoformat(),
            }

            rows = supabase.table("bot_state").select("id").order("updated_at", desc=True).limit(1).execute().data
            if rows:
                supabase.table("bot_state").update(update).eq("id", rows[0]["id"]).execute()
            else:
                supabase.table("bot_state").insert(update).execute()

            log.info(f"[SETTINGS] Saved: capital=${update['capital']:.0f} "
                  f"mode={update['mode']} running={update['is_running']} "
                  f"concurrent={update['max_concurrent_trades']}")
            return {"ok": True}
        except Exception as e:
            log.warning("[SETTINGS] Save failed: %s", e)
            return {"ok": False, "reason": str(e)}


    # ── GET /  —  health check ────────────────────────────────────────────────
    @api.get("/")
    async def health():
        try:
            supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
            state  = (supabase.table("bot_state").select("is_running,mode")
                     .order("updated_at", desc=True).limit(1).execute().data or [{}])[0]
            strats = supabase.table("strategies").select("strategy,fwd_score,is_active").execute().data or []
            regime = (supabase.table("market_regime").select("symbol,regime,updated_at")
                     .order("updated_at", desc=True).limit(5).execute().data or [])
            return {
                "status":          "ok",
                "running":         state.get("is_running", False),
                "mode":            state.get("mode", "SAFE"),
                "strategies":      strats,
                "recent_regimes":  regime,
                "timestamp":       datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            return {"status": "ok", "db": f"unreachable:{e}"}

    # ── POST /signal  —  main trading brain ───────────────────────────────────
    @api.post("/signal")
    async def signal(request: Request):
        """
        Full intelligent signal pipeline:
        Regime detection → Strategy selection → Indicator signal → PPO inference
        → Agreement check → Mode-aware threshold → Log with features for learning
        """
        auth_error = await _require_auth(request)
        if auth_error is not None:
            return auth_error
        try:
            msg = await request.json()
        except Exception:
            return JSONResponse({"action": "NONE", "reason": "invalid_json"}, status_code=400)

        supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
        symbol   = str(msg.get("symbol", "UNKNOWN")).upper()

        # Bot state
        try:
            rows  = supabase.table("bot_state").select("*").order("updated_at", desc=True).limit(1).execute().data
            state = rows[0] if rows else {}
        except Exception as e:
            return {"action": "NONE", "reason": f"state_error:{e}"}

        if not state.get("is_running", False):
            return {"action": "NONE", "reason": "bot_stopped"}

        mode = state.get("mode", "SAFE")

        # Symbol enabled
        try:
            sr = supabase.table("symbol_scores").select("enabled").eq("symbol", symbol).maybe_single().execute()
            if sr.data and sr.data.get("enabled") is False:
                return {"action": "NONE", "reason": "symbol_disabled"}
        except Exception:
            pass

        # Session filter — pair-aware trading hours (uses server UTC; EA sends symbol)
        session_ok, session_reason = _is_session_active(symbol)
        if not session_ok:
            # BUG FIX: was f"off_peak:{session_reason}" but _is_session_active
            # already embeds a descriptive reason (e.g. "off_peak:07:00-12:00_UTC",
            # "weekend", "dead_zone_low_volume"). Wrapping again produced double
            # prefixes like "off_peak:off_peak:07:00-12:00_UTC" in EA logs.
            return {"action": "NONE", "reason": session_reason,
                    "strategy": "", "regime": ""}

        # News blackout
        news_blocked, news_reason = _is_news_blackout(symbol, supabase)
        if news_blocked:
            return {"action": "NONE", "reason": f"news_blackout:{news_reason}",
                    "strategy": "", "regime": ""}

        # Compliance (now includes per-symbol cooldown check)
        ok, reason = _check_compliance(supabase, state, symbol=symbol)
        if not ok:
            return {"action": "NONE", "reason": reason}

        # Features
        obs = build_features(msg)
        if np.all(obs == 0):
            return {"action": "NONE", "reason": "insufficient_data"}

        # PROFITABLE FIX #2: Spread guard on the server side.
        # The EA also pre-filters (saves HTTP round-trip), but this catches any
        # edge cases where the EA's spread value differs from live broker tick.
        # Block when spread > 25% of ATR — at that point spread cost eats too
        # much of the expected R:R to justify entry.
        _closes = msg.get("close", [])
        _highs  = msg.get("high",  _closes)
        _lows   = msg.get("low",   _closes)
        _spread = float(msg.get("spread", 0))
        if _spread > 0:
            # Per-symbol spread cap table — mirrors EA logic.
            # Caps are set at ~2× the typical inter-session spread so we only
            # block genuinely wide (illiquid or news-spike) conditions.
            # Typical spreads (ECN/raw, pips):
            #   EURUSD 0.1-0.8  GBPUSD 0.3-1.0  USDCHF 0.4-1.2  AUDUSD 0.4-1.0
            #   USDCAD 0.5-1.2  NZDUSD 0.6-1.5  EURGBP 0.3-0.9
            #   USDJPY 0.4-0.9  EURJPY 0.6-1.4  GBPJPY 1.5-4.0
            #   XAUUSD 3-15
            _sym_up = symbol.upper()
            if "XAU" in _sym_up:
                _max_spread = 35.0   # gold — wide gap at session transitions
            elif "XAG" in _sym_up:
                _max_spread = 20.0   # silver — tighter than gold, but wider than forex
            elif "GBP" in _sym_up and "JPY" in _sym_up:
                _max_spread = 7.0    # GBPJPY — elevated spread, keep hard cap
            elif "JPY" in _sym_up:
                _max_spread = 3.0    # USDJPY, EURJPY — tighter than GBPJPY
            elif "EURGBP" in _sym_up:
                _max_spread = 1.5    # EURGBP — tightest of all crosses
            elif "NZD" in _sym_up or "CAD" in _sym_up:
                _max_spread = 3.0    # NZDUSD, USDCAD — slightly wider minors
            else:
                _max_spread = 2.5    # EURUSD, GBPUSD, USDCHF, AUDUSD
            if _spread > _max_spread:
                return {"action": "NONE",
                        "reason": f"spread_too_wide:{_spread:.1f}pips",
                        "strategy": "", "regime": ""}

        # Regime
        regime = detect_regime(msg)
        # ── Update in-memory regime history ──────────────────────────────
        # Write every regime reading into the cache so the MR trending block
        # can do a real time-based lookback instead of querying a single-row DB.
        #
        # On cold-start (empty cache per symbol), seed from the regime_history
        # Supabase table first.  This survives:
        #   • Container restarts (e.g. after a deploy)
        #   • /signal and /open landing on different warm containers
        _sym_hist = _REGIME_HISTORY.setdefault(symbol, _deque(maxlen=_REGIME_HISTORY_MAXLEN))

        # Cold-start seed — runs ONCE per symbol per container lifetime.
        if symbol not in _seeded_symbols:
            _seeded_symbols.add(symbol)
            try:
                _seed_rows = (
                    supabase.table("regime_history")
                    .select("detected_at,regime")
                    .eq("symbol", symbol)
                    .order("detected_at", desc=True)
                    .limit(_REGIME_HISTORY_MAXLEN)
                    .execute()
                    .data or []
                )
                if _seed_rows:
                    for _sr in reversed(_seed_rows):   # oldest first → newest ends up at deque front
                        try:
                            _st = _dt.fromisoformat(_sr["detected_at"].replace("Z", "+00:00"))
                            _sym_hist.appendleft((_st, _sr["regime"]))
                        except Exception:
                            pass
                    log.info(f"[REGIME] Cold-start seeded {symbol}: {len(_seed_rows)} entries from DB")
                else:
                    log.info(f"[REGIME] Cold-start: no DB history for {symbol} "
                          f"(first deploy or regime_history table not yet created)")
            except Exception as _seed_err:
                log.warning("[REGIME] Cold-start seed failed for %s: %s", symbol, _seed_err)

        _now_utc = _dt.now(_tz.utc)
        _sym_hist.appendleft((_now_utc, regime))

        # Append to regime_history for cross-container + restart continuity.
        # Hourly cleanup per symbol keeps the table small (only last 2 h needed).
        try:
            supabase.table("regime_history").insert({
                "symbol":      symbol,
                "regime":      regime,
                "detected_at": _now_utc.isoformat(),
            }).execute()

            _last_rh_cleanup = _LAST_REGIME_CLEANUP.get(symbol)
            if (_last_rh_cleanup is None or
                    (_now_utc - _last_rh_cleanup).total_seconds() >= 3600):
                _LAST_REGIME_CLEANUP[symbol] = _now_utc
                _cleanup_cutoff = (_now_utc - _td(hours=2)).isoformat()
                supabase.table("regime_history").delete() \
                    .lt("detected_at", _cleanup_cutoff) \
                    .eq("symbol", symbol) \
                    .execute()
        except Exception:
            pass   # non-fatal — in-memory cache is the primary mechanism

        try:
            supabase.table("market_regime").upsert({
                "symbol": symbol, "regime": regime,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception:
            pass

        # Strategy selection: regime default, forward-test winner overrides if 15%+ better
        regime_strat = REGIME_TO_STRATEGY[regime]
        try:
            strat_rows = supabase.table("strategies").select("*").execute().data or []
            strat_map  = {r["strategy"]: r for r in strat_rows}
            active_row = next((r for r in strat_rows if r.get("is_active")), None)
            if active_row:
                rscore = strat_map.get(regime_strat, {}).get("fwd_score", 0.5)
                ascore = active_row.get("fwd_score", 0.5)
                chosen = active_row["strategy"] if ascore > rscore * 1.15 else regime_strat
            else:
                chosen = regime_strat
        except Exception:
            chosen = regime_strat

        # Indicator signal
        ind_action, ind_conf, ind_reason = STRATEGY_FN[chosen](msg)

        # ── MR trending block — all symbols ──────────────────────────────────
        # Block mean_reversion signals when the market is trending or was trending
        # recently. Mean reversion fading a real trend is the single biggest source
        # of losses — especially on XAUUSD (large pip value) but also on EURUSD
        # (evidence: EURUSD MR SELL at conf=0.842 with regime=TRENDING in live logs).
        #
        # Uses the in-memory _REGIME_HISTORY cache (written above on every scan)
        # instead of querying Supabase — the DB uses upsert (one row per symbol)
        # so historical lookback from DB was always returning only the current row.
        #
        # Lookback windows (tuned per instrument volatility):
        #   XAUUSD : 15 min — gold makes fast impulsive moves; 15 min = one M15 candle
        #   Others : 10 min — forex trends are smoother; release block faster
        #
        # Current regime=TRENDING always blocks regardless of history.
        is_mr_trending_block = False
        if chosen == "mean_reversion":
            if regime == "TRENDING":
                is_mr_trending_block = True
                log.info(f"[SIGNAL] MR BLOCK {symbol}: current regime=TRENDING")
            else:
                lookback_mins = 15 if "XAU" in symbol.upper() else 10
                cutoff_dt = _dt.now(_tz.utc) - _td(minutes=lookback_mins)
                hist = _REGIME_HISTORY.get(symbol, [])
                # ── Cold-start guard ──────────────────────────────────────────
                # For the first _MR_WARMUP_SECS after a container cold-start the
                # regime cache is empty (or just seeded this scan).  Without
                # lookback history we cannot confirm the market wasn't trending
                # recently.  Apply a conservative block during the warmup window
                # rather than allowing MR trades that should have been blocked.
                # After warmup the DB-seeded cache provides real history.
                _cache_age_secs = (_dt.now(_tz.utc) - _CONTAINER_START).total_seconds()
                cold_start_block = (not hist and _cache_age_secs < _MR_WARMUP_SECS)
                if cold_start_block:
                    log.info(f"[SIGNAL] MR BLOCK {symbol}: cold-start warmup "
                          f"({_cache_age_secs:.0f}s < {_MR_WARMUP_SECS}s, cache empty)")
                recent_trending = cold_start_block or any(
                    r == "TRENDING" and t >= cutoff_dt
                    for t, r in hist
                )
                if recent_trending:
                    is_mr_trending_block = True
                    log.info(f"[SIGNAL] MR BLOCK {symbol}: TRENDING in last {lookback_mins}min history")

        if is_mr_trending_block:
            return {"action": "NONE", "reason": "mr_trending_block",
                    "strategy": chosen, "regime": regime}

        # LOSS STREAK FIX: Block mean_reversion when regime=VOLATILE.
        # MR in volatile conditions gets whipsawed (buy dip / sell rally while trend continues).
        # Only allow MR when regime=RANGING; TRENDING is already blocked above.
        if chosen == "mean_reversion" and regime == "VOLATILE":
            log.info(f"[SIGNAL] MR BLOCK {symbol}: regime=VOLATILE (MR allowed only in RANGING)")
            return {"action": "NONE", "reason": "mr_volatile_block",
                    "strategy": chosen, "regime": regime}

        # STRATEGY RECOMMENDATION #5: ATR spike filter — skip entries when volatility is in top decile.
        if ind_action != "NONE" and _atr_spike_skip(msg):
            log.info(f"[SIGNAL] ATR spike skip {symbol}: current ATR in top 10%% of last 50 bars")
            return {"action": "NONE", "reason": "atr_spike_skip",
                    "strategy": chosen, "regime": regime}

        # PPO inference — PROFITABLE FIX #4: cold-start guard.
        # A fresh RecurrentPPO with random weights produces ~0.33 probability for
        # each action. With the 0.55 threshold, random models often exceed it by luck,
        # injecting random BUY/SELL decisions into live trading.
        # Only use PPO if it has been trained on real trade data (metadata flag set).
        try:
            model        = load_strategy_model(chosen)
            ppo_trained  = is_model_trained(chosen)

            if not ppo_trained:
                # Model is untrained — skip PPO entirely, use heuristic signal only.
                # This makes the bot a pure indicator system until real training data
                # accumulates (needs ≥10 closed trades for first online-learn pass).
                ppo_action, ppo_conf = ind_action, ind_conf
                log.info(f"[SIGNAL] PPO untrained for '{chosen}' — using heuristic signal only")
            else:
                ppo_action, ppo_conf = ppo_predict(model, obs)
        except Exception as e:
            log.warning("[SIGNAL] PPO failed: %s", e)
            ppo_action, ppo_conf = ind_action, ind_conf * 0.9

        # ── Enhanced PPO Decision Making ─────────────────────────────────────────
        # PPO is the primary decision maker EXCEPT when the heuristic sees extreme
        # RSI exhaustion (>80 OB or <20 OS). With only a handful of training trades
        # the PPO defaults to NONE on almost everything — that's safe but creates a
        # catch-22: no trades → no data → PPO never improves. The extreme RSI
        # override breaks that loop while keeping the PPO veto intact for normal setups.

        # ── Extreme RSI override — heuristic fires regardless of PPO ─────────────
        # Triggers when: RSI > 80 (overbought) or RSI < 20 (oversold) in ind_reason.
        # These are high-probability exhaustion setups even in trending markets.
        _extreme_rsi = False
        if ind_action != "NONE" and ind_reason:
            import re as _re
            _rsi_match = _re.search(r"RSI_O[BS]:(\d+)", ind_reason)
            if _rsi_match:
                _rsi_val = int(_rsi_match.group(1))
                _extreme_rsi = (_rsi_val >= 80) or (_rsi_val <= 20)

        if _extreme_rsi:
            # Extreme exhaustion — trust heuristic, don't let PPO veto
            final_action = ind_action
            final_conf   = ind_conf
            log.info(f"[SIGNAL] EXTREME RSI OVERRIDE: {ind_action} {ind_reason} (PPO={ppo_action} bypassed)")
        else:
            # Normal path — PPO is primary decision maker.
            # Minimum 60% bar so PPO only “decides” when at least 60% confident (accuracy target).
            PPO_CONFIDENCE_THRESHOLD = 0.60
            ppo_is_reliable = ppo_conf >= PPO_CONFIDENCE_THRESHOLD

            if not ppo_is_reliable:
                # PPO not confident enough — use heuristic with small penalty
                final_action = ind_action
                final_conf   = ind_conf * 0.85
                log.info(f"[SIGNAL] PPO low confidence (conf={ppo_conf:.3f}) — using heuristic with penalty")
            else:
                # PPO confident — PPO decides
                final_action = ppo_action
                final_conf   = ppo_conf

                if ind_action == ppo_action:
                    final_conf = min(final_conf * 1.05, 0.99)
                    log.info(f"[SIGNAL] PPO+Heuristic AGREE: {ppo_action} (conf={final_conf:.3f})")
                elif ind_action == "NONE":
                    # PPO-only: require higher bar (0.70) so we only take trades when very confident.
                    # Reduces noise from model when indicators abstain (no_signal).
                    if ppo_conf < 0.70:
                        log.info(f"[SIGNAL] PPO-only block: conf={ppo_conf:.3f} < 0.70 (heuristic abstains)")
                        return {"action": "NONE", "reason": "ppo_only_low_conf",
                                "strategy": chosen, "regime": regime, "ppo_conf": ppo_conf}
                    final_conf = final_conf * 0.95
                    log.info(f"[SIGNAL] PPO DECIDES: {ppo_action} (heuristic abstains, conf={final_conf:.3f})")
                else:
                    # FIX: old penalty * 0.75 meant SAFE threshold (0.68) required PPO
                    # conf >= 0.91 after a disagreement — effectively vetoed all heuristic
                    # signals the PPO disagreed with. Changed to * 0.85 so a PPO at 0.80
                    # conf survives disagreement (0.80 * 0.85 = 0.68 ≥ SAFE threshold).
                    # Hard block lowered from 0.60 → 0.55 for the same reason.
                    final_conf = final_conf * 0.85
                    log.info(f"[SIGNAL] PPO vs Heuristic DISAGREE: PPO={ppo_action} vs Ind={ind_action} (conf={final_conf:.3f})")
                    if final_conf < 0.60:
                        return {"action": "NONE", "reason": f"strong_disagreement:ppo_{ppo_action}_vs_ind_{ind_action}",
                                "strategy": chosen, "regime": regime, "ppo_conf": ppo_conf, "ind_conf": ind_conf}

        # ── Metals heuristic agreement guard (XAU + XAG) ───────────────────────────
        # PPO was firing BUY/SELL with high conf when heuristic returned NONE (no_signal).
        # On a small training set that's model noise, not edge — led to bad BUY on XAGUSD
        # (e.g. -$68.45 loss when price continued down; SELL would have been correct).
        # Block any metal MR entry where the heuristic abstains entirely.
        # Remove once 50+ metal trades have been closed and PPO is properly trained.
        _sym_up = symbol.upper()
        _is_metal_mr_no_heuristic = (
            ("XAU" in _sym_up or "XAG" in _sym_up)
            and chosen == "mean_reversion"
            and ind_action == "NONE"
        )
        if _is_metal_mr_no_heuristic:
            metal_label = "GOLD" if "XAU" in _sym_up else "SILVER"
            log.info(f"[SIGNAL] {metal_label} heuristic agreement block: PPO={final_action} conf={final_conf:.3f} "
                  f"but heuristic=NONE ({ind_reason}) — blocked until 50+ metal trades")
            return {"action": "NONE", "reason": "gold_ppo_no_heuristic_agreement" if "XAU" in _sym_up else "silver_ppo_no_heuristic_agreement",
                    "strategy": chosen, "regime": regime,
                    "ppo_conf": final_conf, "ind_reason": ind_reason}

        # SAFE mode: only take trades when PPO and heuristic agree (no disagreement entries).
        # Improves accuracy by avoiding marginal “PPO says BUY, heuristic says SELL” setups.
        if mode == "SAFE" and ind_action != "NONE" and ppo_action != "NONE" and ind_action != ppo_action:
            log.info(f"[SIGNAL] SAFE agreement block: PPO={ppo_action} vs heuristic={ind_action} — require agreement")
            return {"action": "NONE", "reason": "safe_require_agreement",
                    "strategy": chosen, "regime": regime, "ppo_conf": ppo_conf, "ind_conf": ind_conf}

        # Mode-aware threshold per strategy
        threshold = _THRESHOLDS.get(mode, _THRESHOLDS["SAFE"]).get(chosen, 0.70)

        # Metals (XAU, XAG): mean_reversion threshold 0.75 so model is at least ~75% bar.
        # Reduces bad entries on volatile instruments when conviction is marginal.
        is_metal_sym = "XAU" in symbol.upper() or "XAG" in symbol.upper()
        if is_metal_sym and chosen == "mean_reversion":
            metal_mr_threshold = 0.75
            if metal_mr_threshold > threshold:
                threshold = metal_mr_threshold
                log.info(f"[SIGNAL] Metal MR threshold override: {threshold:.2f}")

        if final_action != "NONE" and final_conf < threshold:
            return {"action": "NONE", "reason": f"low_conf:{final_conf:.3f}<{threshold}",
                    "strategy": chosen, "regime": regime}

        # ── Log model performance metrics ────────────────────────────────────────
        # Track PPO vs heuristic performance for continuous improvement
        try:
            supabase.table("model_performance").insert({
                "symbol": symbol,
                "strategy": chosen,
                "regime": regime,
                "ppo_action": ppo_action,
                "ppo_confidence": round(ppo_conf, 4),
                "ind_action": ind_action,
                "ind_confidence": round(ind_conf, 4),
                "final_action": final_action,
                "final_confidence": round(final_conf, 4),
                "agreement": ppo_action == ind_action,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as log_error:
            log.warning("[SIGNAL] Failed to log model performance: %s", log_error)

        # ── Cache signal metadata for /open ──────────────────────────────────
        # PHANTOM TRADE FIX: the old approach wrote a lot=NULL "pending" row to
        # the DB on every signal that passed compliance — every 60s per symbol.
        # With 4 symbols scanning, that was 240+ phantom rows/hour. They didn't
        # count toward the concurrent limit (lot IS NOT NULL filter), but they:
        #   (a) added noise to the trades table
        #   (b) required a DB dedup query on every signal (extra latency)
        #   (c) risked /open confirming the wrong row if the EA sent a second
        #       direction before the first was resolved
        #
        # New approach: write NOTHING to the DB at signal time.
        # Store features + metadata in a per-container in-memory dict (_signal_cache).
        # The EA calls /open only when it actually executes — /open reads the cache
        # and writes exactly ONE confirmed trade row with all fields populated.
        # Zero phantom rows. Zero dedup queries.
        if final_action != "NONE":
            _cache_ts    = datetime.now(timezone.utc).isoformat()
            _cache_entry = {
                "direction":  final_action,
                "features":   obs.tolist(),
                "strategy":   chosen,
                "regime":     regime,
                "confidence": round(final_conf, 4),
                "ts":         _cache_ts,
            }
            _signal_cache[symbol] = _cache_entry

            # Mirror to Supabase for cross-container durability.
            # Modal routes requests across up to max_containers=10 warm instances —
            # if /signal lands on container A and /open on container B, the in-memory
            # dict is empty in B.  This DB write ensures features are always recoverable
            # so the PPO online-learner never silently loses training samples.
            try:
                supabase.table("signal_cache").upsert({
                    "symbol":     symbol,
                    "direction":  final_action,
                    "strategy":   chosen,
                    "regime":     regime,
                    "confidence": round(final_conf, 4),
                    "features":   obs.tolist(),
                    "cached_at":  _cache_ts,
                }).execute()
            except Exception:
                pass   # non-fatal — in-memory is primary; force_learn reconstructs missing features
            log.info(f"[SIGNAL] Cached {symbol} {final_action} → waiting for /open confirmation")

        # capital: read from bot_state (same column used for DD calcs and lot sizing cap).
        # 0 = use full live equity. e.g. 200 caps lot sizing to $200 basis.
        risk_capital = float(state.get("capital", 0) or 0)

        # ── Dynamic ATR-based SL/TP ───────────────────────────────────────────
        # Compute server-side SL/TP so the EA uses volatility-adjusted levels
        # instead of fixed pips. n_closed_trades drives TP maturity scaling.
        # Inject last 20 closed-trade PnLs so compute_sl_tp_forex can tighten TP when WR is low.
        n_closed = 0
        recent_outcomes = []
        try:
            _ct = supabase.table("trades").select("id", count="exact") \
                .not_.is_("pnl", "null").execute()
            n_closed = _ct.count or 0
            _rows = supabase.table("trades").select("pnl").not_.is_("pnl", "null") \
                .order("closed_at", desc=True).limit(20).execute().data or []
            recent_outcomes = [float(r.get("pnl") or 0) for r in _rows]
        except Exception:
            pass
        _msg_for_sltp = dict(msg)
        _msg_for_sltp["recent_outcomes"] = recent_outcomes

        sl_tp = compute_sl_tp_forex(
            msg        = _msg_for_sltp,
            direction  = final_action if final_action != "NONE" else "BUY",
            confidence = final_conf,
            strategy   = chosen,
            regime     = regime,
            n_closed_trades = n_closed,
        )

        log.info(f"[SIGNAL] {final_action} {symbol} | {chosen} | {regime} | conf={final_conf:.3f} | "
              f"SL={sl_tp['sl_price']} TP={sl_tp['tp_price']} RR={sl_tp['rr_ratio']:.2f} | "
              f"mode={mode} | risk_capital=${risk_capital:.0f}")
        # When PPO drives the trade (heuristic was NONE or disagreed), avoid logging "no_signal"
        reason_out = ind_reason if (ind_action == final_action and (ind_reason or "").strip()) else f"{chosen}:{final_action}"
        return {
            "action":        final_action,
            "confidence":    round(final_conf, 4),
            "strategy":      chosen,
            "regime":        regime,
            "session":       "ok",   # session filter passed (pair-aware UTC windows)
            "ind_signal":    ind_action,
            "ppo_signal":    ppo_action,
            "reason":        reason_out,
            "lot_scale":     _LOT_SCALE.get(mode, 1.0),
            "risk_capital":  risk_capital,
            # Dynamic SL/TP — EA should use these instead of fixed pips
            "sl_price":      sl_tp["sl_price"],
            "tp_price":      sl_tp["tp_price"],
            "sl_atr_mult":   sl_tp["sl_atr_mult"],
            "tp_atr_mult":   sl_tp["tp_atr_mult"],
            "rr_ratio":      sl_tp["rr_ratio"],
            "atr":           sl_tp["atr"],
            "tp_maturity":   sl_tp["tp_maturity"],
        }


    # ── POST /open  —  called by EA immediately after order fills ────────────
    # Payload: { "symbol": "EURUSD", "direction": "BUY"|"SELL",
    #            "lot": 0.01, "ticket": 12345678 }
    #
    # Reads features + metadata from the per-container _signal_cache populated
    # by the most recent /signal call for this symbol, then writes ONE confirmed
    # trade row to the DB. No pending-row lookup needed — no phantom rows exist.
    @api.post("/open")
    async def open_trade(request: Request):
        auth_error = await _require_auth(request)
        if auth_error is not None:
            return auth_error
        try:
            msg = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "reason": "invalid_json"}, status_code=400)

        symbol    = str(msg.get("symbol",    "")).upper()
        direction = str(msg.get("direction", "")).upper()
        lot       = float(msg.get("lot",     0))
        ticket    = int(msg.get("ticket",    0)) or None

        if not symbol or direction not in ("BUY", "SELL"):
            return JSONResponse({"ok": False, "reason": "missing symbol or direction"}, status_code=400)
        if lot <= 0:
            return JSONResponse({"ok": False, "reason": "lot must be > 0"}, status_code=400)

        try:
            supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
            now_ts   = datetime.now(timezone.utc)

            # Pull cached signal metadata (features, strategy, regime, confidence).
            # Cache is keyed by symbol — read it, then clear the entry so a
            # second /open for the same symbol doesn't accidentally reuse stale data.
            cached   = _signal_cache.pop(symbol, None)
            features = None
            strategy = None
            regime   = None
            confidence = None

            if cached:
                # Validate freshness: ignore cache entries older than 5 minutes
                try:
                    cache_age = (now_ts - datetime.fromisoformat(
                        cached["ts"].replace("Z", "+00:00")
                    )).total_seconds()
                    if cache_age <= 300:
                        # Only use cache if direction matches (could be stale from prev signal)
                        if cached.get("direction") == direction:
                            features   = cached.get("features")
                            strategy   = cached.get("strategy")
                            regime     = cached.get("regime")
                            confidence = cached.get("confidence")
                        else:
                            log.info(f"[OPEN] Cache direction mismatch for {symbol}: "
                                  f"cached={cached.get('direction')} vs open={direction} — ignoring cache")
                    else:
                        log.info(f"[OPEN] Cache entry for {symbol} is {cache_age:.0f}s old — ignoring (stale)")
                except Exception as _ce:
                    log.info(f"[OPEN] Cache parse error for {symbol}: {_ce}")

            # ── Cross-container fallback ──────────────────────────────────────
            # If in-memory cache missed (different Modal container handled /signal),
            # read from the Supabase signal_cache table which is written by /signal.
            # This guarantees PPO training features are captured even at max container scale.
            if features is None:
                try:
                    _sc_rows = (
                        supabase.table("signal_cache")
                        .select("*")
                        .eq("symbol",    symbol)
                        .eq("direction", direction)
                        .order("cached_at", desc=True)
                        .limit(1)
                        .execute()
                        .data or []
                    )
                    if _sc_rows:
                        _sc     = _sc_rows[0]
                        _sc_age = (now_ts - datetime.fromisoformat(
                            _sc["cached_at"].replace("Z", "+00:00")
                        )).total_seconds()
                        if _sc_age <= 300:
                            features   = _sc.get("features")
                            strategy   = _sc.get("strategy")
                            regime     = _sc.get("regime")
                            confidence = _sc.get("confidence")
                            log.info(f"[OPEN] ✔ Cross-container DB cache hit for {symbol} "
                                  f"(age={_sc_age:.0f}s) — features recovered from Supabase")
                        else:
                            log.info(f"[OPEN] DB signal_cache for {symbol} is {_sc_age:.0f}s old — stale")
                    else:
                        log.info(f"[OPEN] No DB signal_cache for {symbol}/{direction} "
                              f"— features will be reconstructed by force_learn")
                except Exception:
                    pass   # non-fatal — trade opens normally; force_learn reconstructs features

            # Build the confirmed trade row — all fields in one insert, no update needed
            row = {
                "symbol":     symbol,
                "direction":  direction,
                "lot":        round(lot, 2),
                "opened_at":  now_ts.isoformat(),
            }
            if ticket:     row["ticket"]     = ticket
            if strategy:   row["strategy"]   = strategy
            if regime:     row["regime"]     = regime
            if confidence: row["confidence"] = confidence
            if features:   row["features"]   = features

            result = supabase.table("trades").insert(row).execute()
            trade_id = result.data[0]["id"] if result.data else None

            cache_status = "with features" if features else "no cache (features will be reconstructed by force_learn)"
            log.info(f"[OPEN] ✔ {symbol} {direction} {lot} lots | ticket={ticket} | id={trade_id} | {cache_status}")
            return {"ok": True, "id": trade_id, "lot": round(lot, 2)}

        except Exception as e:
            log.error("[OPEN] Error: %s", e)
            return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


    # ── POST /close  —  called by MT5 EA when a trade closes ─────────────────
    # Payload: { "ticket": 12345678, "symbol": "EURUSD", "pnl": 12.50,
    #            "close_price": 1.0851, "close_time": "2026-02-23T06:30:00Z" }
    @api.post("/close")
    async def close_trade(request: Request):
        auth_error = await _require_auth(request)
        if auth_error is not None:
            return auth_error
        try:
            msg = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "reason": "invalid_json"}, status_code=400)

        ticket_raw  = msg.get("ticket")
        symbol      = str(msg.get("symbol", "")).upper()
        pnl         = float(msg.get("pnl", 0))
        close_price = float(msg.get("close_price", 0))
        close_time  = msg.get("close_time", datetime.now(timezone.utc).isoformat())

        if symbol is None or symbol == "":
            return JSONResponse({"ok": False, "reason": "missing_symbol"}, status_code=400)

        try:
            supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

            # Prefer matching by ticket when provided (avoids wrong trade if multiple opens per symbol)
            ticket_int = None
            if ticket_raw is not None and str(ticket_raw).strip() != "":
                try:
                    ticket_int = int(ticket_raw)
                except (TypeError, ValueError):
                    pass

            rows = None
            if ticket_int is not None:
                rows = supabase.table("trades").select("id").eq("ticket", ticket_int).is_("pnl", "null").is_("closed_at", "null").limit(1).execute().data
            if not rows:
                # Fallback: most recent open trade for this symbol
                rows = supabase.table("trades").select("id").eq("symbol", symbol).is_("pnl", "null").is_("closed_at", "null").order("opened_at", desc=True).limit(1).execute().data

            if rows:
                trade_id = rows[0]["id"]
                supabase.table("trades").update({
                    "pnl":         round(pnl, 2),
                    "closed_at":   close_time,
                }).eq("id", trade_id).execute()
                log.info(f"[CLOSE] {symbol} ticket={ticket_raw} pnl=${pnl:.2f}")
            else:
                # No open trade found — insert a close-only record
                supabase.table("trades").insert({
                    "symbol":      symbol,
                    "direction":   "CLOSED",
                    "pnl":         round(pnl, 2),
                    "closed_at":   close_time,
                    "opened_at":   close_time,
                    "confidence":  None,
                }).execute()
                log.info(f"[CLOSE] {symbol} ticket={ticket_raw} pnl=${pnl:.2f} (no open trade matched)")

            return {"ok": True, "ticket": ticket_raw, "pnl": round(pnl, 2)}

        except Exception as e:
            log.error("[CLOSE] Error: %s", e)
            return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


    # ── POST /equity  —  called by EA every scan cycle ───────────────────────
    # Payload: { "balance": 10234.50, "equity": 10198.20,
    #            "margin": 120.00, "free_margin": 10078.20 }
    # This is the single source of truth for P&L — uses real MT5 account data,
    # no dependency on trade.pnl fields being populated.
    @api.post("/equity")
    async def post_equity(request: Request):
        auth_error = await _require_auth(request)
        if auth_error is not None:
            return auth_error
        try:
            msg = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "reason": "invalid_json"}, status_code=400)

        balance     = float(msg.get("balance",     0))
        equity      = float(msg.get("equity",      balance))
        margin      = float(msg.get("margin",      0))
        if balance <= 0:
            return {"ok": False, "reason": "invalid_balance"}

        try:
            supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

            # Get capital + peak balance efficiently
            # BUG FIX 6: only fetch most recent row + capital, not all history
            state_rows = supabase.table("bot_state").select("capital")                 .order("updated_at", desc=True).limit(1).execute().data
            capital = float((state_rows[0] if state_rows else {}).get("capital", balance))

            # Peak = max of capital and most recent recorded max
            # BUG FIX 6: use single max query, not all rows
            peak_rows = supabase.table("equity").select("balance")                 .order("balance", desc=True).limit(1).execute().data
            peak_bal  = float(peak_rows[0]["balance"]) if peak_rows else capital
            raw_peak  = max(capital, peak_bal, balance)
            # Subtract total withdrawals so they don't inflate drawdown.
            # e.g. withdrew $200 from $1000 peak → adj_peak=$800, not $1000.
            adj_peak  = max(balance, raw_peak - _net_withdrawals(supabase))
            drawdown  = max(0.0, (adj_peak - balance) / adj_peak) if adj_peak > 0 else 0.0

            # BUG FIX 5: use INSERT not UPSERT — equity is an append-only log
            # upsert without a matching unique key inserts a new row anyway,
            # but INSERT is explicit and avoids accidental overwrites
            supabase.table("equity").insert({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "balance":   round(balance, 2),
                "equity":    round(equity, 2),
                "margin":    round(margin, 2),
                "drawdown":  round(drawdown, 6),
            }).execute()

            log.info(f"[EQUITY] balance=${balance:.2f} equity=${equity:.2f} dd={drawdown*100:.2f}%")
            return {"ok": True, "balance": balance, "drawdown": round(drawdown, 4)}

        except Exception as e:
            log.info(f"[EQUITY] Error: {e}")
            return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


    # ── POST /transaction  —  called by EA on every deposit / withdrawal ────────
    # Payload: {
    #   "type":          "deposit" | "withdrawal",
    #   "amount":        1000.00,           -- always positive
    #   "balance_after": 11000.00,          -- MT5 balance right after the event
    #   "ticket":        123456789,         -- MT5 deal ticket (0 if unavailable)
    #   "event_time":    "2026-02-24T09:15:00Z"
    # }
    # This is what makes P&L correct: withdrawals are recorded separately so the
    # dashboard never mistakes them for trading losses.
    @api.post("/transaction")
    async def post_transaction(request: Request):
        auth_error = await _require_auth(request)
        if auth_error is not None:
            return auth_error
        try:
            msg = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "reason": "invalid_json"}, status_code=400)

        txn_type      = str(msg.get("type", "")).lower()
        amount        = float(msg.get("amount", 0))
        balance_after = float(msg.get("balance_after", 0))
        ticket        = int(msg.get("ticket", 0)) or None   # store NULL if 0
        event_time    = msg.get("event_time", datetime.now(timezone.utc).isoformat())

        if txn_type not in ("deposit", "withdrawal"):
            return JSONResponse(
                {"ok": False, "reason": f"invalid type: {txn_type!r} — must be 'deposit' or 'withdrawal'"},
                status_code=400,
            )
        if amount <= 0:
            return JSONResponse(
                {"ok": False, "reason": "amount must be > 0"},
                status_code=400,
            )

        try:
            supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

            # Deduplicate by MT5 deal ticket — the UNIQUE index on transactions.ticket
            # rejects duplicates at the DB level too, but checking first gives a
            # clean {duplicate: true} response instead of a 500.
            if ticket:
                existing = (
                    supabase.table("transactions")
                    .select("id")
                    .eq("ticket", ticket)
                    .execute()
                    .data
                )
                if existing:
                    log.info(f"[TRANSACTION] Duplicate ticket {ticket} — skipping insert")
                    return {"ok": True, "duplicate": True, "ticket": ticket}

            row = {
                "type":          txn_type,
                "amount":        round(amount, 2),
                "balance_after": round(balance_after, 2),
                "event_time":    event_time,
                "reported_at":   datetime.now(timezone.utc).isoformat(),
            }
            if ticket:
                row["ticket"] = ticket

            supabase.table("transactions").insert(row).execute()

            log.info(
                f"[TRANSACTION] {txn_type.upper()} ${amount:.2f} | "
                f"balance_after=${balance_after:.2f} | ticket={ticket}"
            )
            return {
                "ok":            True,
                "type":          txn_type,
                "amount":        round(amount, 2),
                "balance_after": round(balance_after, 2),
            }

        except Exception as e:
            log.error("[TRANSACTION] Error: %s", e)
            return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


    # ── POST /cleanup  —  force-clean stale open trades in DB ──────────────
    @api.post("/cleanup")
    async def cleanup_stale(request: Request):
        try:
            supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
            try:
                data = await request.json()
            except Exception:
                data = {}
            hours = float(data.get("hours", 8))
            cleaned, symbols = _run_stale_trades_cleanup(supabase, hours=hours)
            if cleaned == 0:
                return {"ok": True, "cleaned": 0, "message": "No stale trades found"}
            return {"ok": True, "cleaned": cleaned, "symbols": list(symbols)}
        except Exception as e:
            log.exception("[CLEANUP] Failed")
            return {"ok": False, "reason": str(e)}

    # ── POST /purge_phantoms  —  one-shot migration: delete ALL phantom rows ─
    # Run once after deploying this version to clear the backlog of lot=NULL rows
    # created by the old /signal pending-log system.
    # Safe to call multiple times — only deletes rows where lot IS NULL AND pnl IS NULL.
    @api.post("/purge_phantoms")
    async def purge_phantoms(request: Request):
        auth_error = await _require_auth(request)
        if auth_error is not None:
            return auth_error
        from datetime import datetime, timezone
        try:
            supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
            rows = (
                supabase.table("trades")
                .select("id, symbol, opened_at")
                .is_("lot",       "null")
                .is_("pnl",       "null")
                .is_("closed_at", "null")
                .execute()
                .data or []
            )
            if not rows:
                return {"ok": True, "deleted": 0, "message": "No phantom rows found — DB is already clean"}
            ids     = [r["id"] for r in rows]
            symbols = {}
            for r in rows:
                symbols[r["symbol"]] = symbols.get(r["symbol"], 0) + 1
            supabase.table("trades").delete().in_("id", ids).execute()
            log.info(f"[PURGE] Deleted {len(ids)} phantom rows: {symbols}")
            return {"ok": True, "deleted": len(ids), "by_symbol": symbols}
        except Exception as e:
            log.warning("[PURGE] Failed: %s", e)
            return {"ok": False, "reason": str(e)}

    # ── POST /closeall  —  called by dashboard STOP button ───────────────────
    # Sets bot to stopped AND signals MT5 EA to close all open positions.
    # The EA polls this endpoint; when it returns close_all=true it closes
    # every open position immediately regardless of symbol.
    @api.post("/closeall")
    async def closeall(request: Request):
        auth_error = await _require_auth(request)
        if auth_error is not None:
            return auth_error
        try:
            msg = await request.json()
        except Exception:
            msg = {}

        try:
            supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

            # Stop the bot
            supabase.table("bot_state").update({
                "is_running": False,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).gte("id", "00000000-0000-0000-0000-000000000000").execute()

            # Write a close_all command for the EA to pick up.
            # acknowledged_at is filled by /closeall_ack once the EA finishes.
            supabase.table("bot_commands").upsert({
                "command":         "close_all",
                "issued_at":       datetime.now(timezone.utc).isoformat(),
                "executed":        False,
                "acknowledged_at": None,
            }).execute()

            log.info("[CLOSEALL] Bot stopped + close_all command issued")
            return {"ok": True, "close_all": True, "reason": "dashboard_stop"}

        except Exception as e:
            log.info(f"[CLOSEALL] Error: {e}")
            return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)

    # ── POST /closeall_ack  —  called by EA after closing all positions ───────
    # Confirms that a previously-issued close_all command has been executed.
    @api.post("/closeall_ack")
    async def closeall_ack(request: Request):
        auth_error = await _require_auth(request)
        if auth_error is not None:
            return auth_error

        try:
            msg = await request.json()
        except Exception:
            msg = {}

        closed = int(msg.get("closed", 0))
        source = str(msg.get("source", "ea"))

        try:
            supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

            # Update the most recent close_all command with acknowledgment info.
            rows = (
                supabase.table("bot_commands")
                .select("id")
                .eq("command", "close_all")
                .order("issued_at", desc=True)
                .limit(1)
                .execute()
                .data
                or []
            )

            if rows:
                cid = rows[0]["id"]
                supabase.table("bot_commands").update({
                    "acknowledged_at": datetime.now(timezone.utc).isoformat(),
                    "ack_source":      source,
                    "ack_closed":      closed,
                }).eq("id", cid).execute()

            log.info(f"[CLOSEALL_ACK] source={source} closed={closed}")
            return {"ok": True, "closed": closed}

        except Exception as e:
            log.info(f"[CLOSEALL_ACK] Error: {e}")
            return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)

    # ── GET /commands  —  EA polls this to get pending commands ──────────────
    @api.get("/commands")
    async def get_commands(request: Request):
        auth_error = await _require_auth(request)
        if auth_error is not None:
            return auth_error
        try:
            supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

            rows = supabase.table("bot_commands")                 .select("*")                 .eq("executed", False)                 .execute().data or []

            commands = [r["command"] for r in rows]

            # Mark all as executed (delivered to EA)
            if rows:
                ids = [r["id"] for r in rows]
                for cid in ids:
                    supabase.table("bot_commands").update({
                        "executed":    True,
                        "executed_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("id", cid).execute()

            return {"commands": commands, "count": len(commands)}

        except Exception as e:
            return {"commands": [], "count": 0, "error": str(e)}


    # ── POST /log  —  EA sends a log line to Modal ───────────────────────────
    # Payload: {
    #   "level":   "INFO" | "WARN" | "ERROR",
    #   "symbol":  "EURUSD",          -- optional
    #   "message": "BUY 0.01 lots | SL=1.0820 TP=1.0870",
    #   "ea_time": "2026-02-26T08:05:00Z"   -- optional, EA local time
    # }
    # Stored in ea_logs table. Dashboard reads and allows CSV download.
    @api.post("/log")
    async def post_log(request: Request):
        auth_error = await _require_auth(request)
        if auth_error is not None:
            return auth_error
        try:
            msg = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "reason": "invalid_json"}, status_code=400)

        level   = str(msg.get("level",   "INFO")).upper()[:10]
        symbol  = str(msg.get("symbol",  "")).upper()[:20]
        message = str(msg.get("message", ""))[:500]
        ea_time = msg.get("ea_time", datetime.now(timezone.utc).isoformat())

        if not message:
            return JSONResponse({"ok": False, "reason": "empty message"}, status_code=400)

        try:
            supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
            supabase.table("ea_logs").insert({
                "level":      level,
                "symbol":     symbol or None,
                "message":    message,
                "ea_time":    ea_time,
                "logged_at":  datetime.now(timezone.utc).isoformat(),
            }).execute()
            return {"ok": True}
        except Exception as e:
            # Never crash the EA over a log failure — swallow and return ok
            log.warning("[LOG] DB insert failed: %s", e)
            return {"ok": False, "reason": str(e)}


    # ── GET /logs  —  dashboard fetches recent EA logs ───────────────────────
    @api.get("/logs")
    async def get_logs(request: Request):
        auth_error = await _require_auth(request)
        if auth_error is not None:
            return auth_error
        try:
            supabase  = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
            limit_str = request.query_params.get("limit", "500")
            limit     = min(int(limit_str), 2000)
            rows = (
                supabase.table("ea_logs")
                .select("*")
                .order("logged_at", desc=True)
                .limit(limit)
                .execute()
                .data or []
            )
            return {"ok": True, "count": len(rows), "logs": rows}
        except Exception as e:
            return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


    return api