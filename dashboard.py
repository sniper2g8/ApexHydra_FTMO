"""
dashboard.py — ApexHydra PRO | Streamlit Cloud
Credentials in Streamlit Cloud → App Settings → Secrets:
  SUPABASE_URL = "https://xxxx.supabase.co"
  SUPABASE_SERVICE_KEY = "eyJhbG..."
  MODAL_SIGNAL_URL = "https://YOUR-WORKSPACE--apexhydra-pro-web.modal.run"
"""

import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timezone
import altair as alt
import streamlit.components.v1 as components

from supabase import create_client

st.set_page_config(
    page_title="ApexHydra PRO",
    page_icon="🐍",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Professional theme: custom CSS ───────────────────────────────────────────
st.markdown("""
<style>
  /* Variables */
  :root {
    --bg-main: #0f1117;
    --bg-card: #161b26;
    --bg-sidebar: #0c0e14;
    --border: #2d3748;
    --text: #e2e8f0;
    --text-muted: #94a3b8;
    --accent: #22c55e;
    --accent-dim: #16a34a;
    --danger: #ef4444;
    --warning: #f59e0b;
    --radius: 10px;
    --shadow: 0 4px 6px -1px rgba(0,0,0,0.2), 0 2px 4px -2px rgba(0,0,0,0.15);
  }
  /* Main container */
  .stApp { background: var(--bg-main); }
  [data-testid="stHeader"] { background: var(--bg-main) !important; }
  [data-testid="stToolbar"] { background: var(--bg-main) !important; }
  /* Sidebar */
  [data-testid="stSidebar"] {
    background: linear-gradient(180deg, var(--bg-sidebar) 0%, #0a0c10 100%) !important;
    border-right: 1px solid var(--border);
  }
  [data-testid="stSidebar"] .stMarkdown { color: var(--text-muted); }
  /* Cards */
  .apex-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.25rem;
    margin-bottom: 1rem;
    box-shadow: var(--shadow);
  }
  .apex-card h3 {
    color: var(--text);
    font-size: 0.95rem;
    font-weight: 600;
    margin: 0 0 0.75rem 0;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border);
  }
  /* Status pill */
  .status-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.35rem 0.75rem;
    border-radius: 9999px;
    font-size: 0.8rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .status-pill.running { background: rgba(34, 197, 94, 0.2); color: var(--accent); border: 1px solid var(--accent); }
  .status-pill.stopped { background: rgba(239, 68, 68, 0.2); color: var(--danger); border: 1px solid var(--danger); }
  /* Metric value emphasis */
  [data-testid="stMetricValue"] { font-weight: 700 !important; }
  /* Section headers */
  .apex-section {
    margin-top: 2rem;
    margin-bottom: 1rem;
  }
  .apex-section-title {
    font-size: 1.1rem;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 0.75rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .apex-section-title::before {
    content: "";
    width: 4px;
    height: 1.2em;
    background: var(--accent);
    border-radius: 2px;
  }
  /* Dividers */
  hr { border-color: var(--border) !important; opacity: 0.6; }
  /* DataFrames */
  [data-testid="stDataFrame"] { border-radius: var(--radius); overflow: hidden; border: 1px solid var(--border); }
  /* Buttons in sidebar */
  [data-testid="stSidebar"] [data-testid="stButton"] button {
    width: 100%;
    border-radius: 8px;
    font-weight: 600;
  }
  /* Key metrics strip (dashboard bar) */
  .apex-strip {
    display: flex;
    align-items: center;
    gap: 1.5rem;
    flex-wrap: wrap;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 0.75rem 1.25rem;
    margin-bottom: 1.5rem;
    box-shadow: var(--shadow);
  }
  .apex-strip-item { display: flex; align-items: baseline; gap: 0.5rem; }
  .apex-strip-label { color: var(--text-muted); font-size: 0.8rem; }
  .apex-strip-value { color: var(--text); font-weight: 700; font-size: 1.1rem; }
  .apex-strip-value.positive { color: var(--accent); }
  .apex-strip-value.negative { color: var(--danger); }
  /* Tabs: match dark theme */
  [data-testid="stTabs"] > div > div {
    background: var(--bg-card) !important;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 0.5rem 0;
  }
  [data-testid="stTabs"] [data-baseweb="tab-list"] {
    background: transparent !important;
    gap: 0.25rem;
    padding: 0 1rem;
  }
  [data-testid="stTabs"] [data-baseweb="tab"] {
    color: var(--text-muted) !important;
    border-radius: 8px;
    padding: 0.5rem 1rem;
  }
  [data-testid="stTabs"] [data-baseweb="tab"]:hover { color: var(--text) !important; }
  [data-testid="stTabs"] [aria-selected="true"] {
    color: var(--accent) !important;
    background: rgba(34, 197, 94, 0.1);
    font-weight: 600;
  }
  .apex-tab-content { padding-top: 1rem; }
  /* Metric cards in grid */
  .apex-metric-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 0.75rem; margin-bottom: 1rem; }
  .apex-metric-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.75rem 1rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.15);
  }
  .apex-metric-card .label { font-size: 0.7rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .apex-metric-card .value { font-size: 1.05rem; font-weight: 700; color: var(--text); }
  .apex-metric-card .value.positive { color: var(--accent); }
  .apex-metric-card .value.negative { color: var(--danger); }

  /* Small UI polish */
  .apex-muted { color: var(--text-muted); }
  .apex-hstack { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  .apex-pill {
    display: inline-flex; align-items: center; gap: 0.35rem;
    padding: 0.25rem 0.6rem; border-radius: 9999px;
    border: 1px solid var(--border); background: rgba(148,163,184,0.08);
    font-size: 0.78rem; color: var(--text);
  }
  .apex-pill strong { font-weight: 700; }
</style>
""", unsafe_allow_html=True)

try:
    SUPABASE_URL     = st.secrets["SUPABASE_URL"]
    SUPABASE_KEY     = st.secrets["SUPABASE_SERVICE_KEY"]
    MODAL_SIGNAL_URL = st.secrets.get("MODAL_SIGNAL_URL", "")
    API_TOKEN        = st.secrets.get("APEXHYDRA_API_TOKEN", "")
except KeyError:
    st.error("❌ Missing secrets. Add SUPABASE_URL, SUPABASE_SERVICE_KEY, MODAL_SIGNAL_URL in Streamlit Cloud → Secrets.")
    st.stop()

@st.cache_resource
def get_client():
    return create_client(SUPABASE_URL, SUPABASE_KEY)
db = get_client()

# ── Data helpers ──────────────────────────────────────────────────────────────
def q(table, **kwargs):
    try:
        r = db.table(table).select("*")
        for k, v in kwargs.items():
            if k == "order":   r = r.order(v[0], desc=v[1])
            elif k == "limit": r = r.limit(v)
            elif k == "gte":   r = r.gte(v[0], v[1])
            elif k == "eq":    r = r.eq(v[0], v[1])
            elif k == "is_":   r = r.is_(v[0], v[1])
        return r.execute().data or []
    except Exception as e:
        st.warning(f"DB read error ({table}): {e}")
        return []

def get_state():
    rows = q("bot_state", order=("updated_at", True), limit=1)
    return rows[0] if rows else {}

def update_state(patch, sid):
    try:
        patch["updated_at"] = datetime.now(timezone.utc).isoformat()
        db.table("bot_state").update(patch).eq("id", sid).execute()
        return True
    except Exception as e:
        st.error(f"Update failed: {e}"); return False

# ── Load core state ────────────────────────────────────────────────────────────
state = get_state()
if not state:
    st.error("❌ Could not load bot state. Check credentials and that schema_update.sql has been run.")
    st.stop()

sid            = state.get("id","")
is_running     = bool(state.get("is_running", False))
mode           = state.get("mode", "SAFE")
capital        = float(state.get("capital") or 10000)
risk_day_start = state.get("risk_day_start")

# FTMO min trading days (from Modal GET /)
ftmo_trading_days = None
ftmo_min_days = 4
try:
    base = MODAL_SIGNAL_URL.rstrip("/")
    if base.endswith("/signal"):
        base = base[:-7]
    r = requests.get(base + "/", timeout=5)
    if r.status_code == 200:
        health = r.json()
        ftmo_trading_days = health.get("ftmo_trading_days")
        ftmo_min_days = health.get("ftmo_min_trading_days", 4)
except Exception:
    pass

# ── Sidebar: Bot controls & settings ──────────────────────────────────────────
with st.sidebar:
    st.markdown("### 🐍 ApexHydra PRO")
    st.caption("Trading control center")
    st.markdown("---")
    st.markdown("**Bot control**")
    if st.button("▶ Start bot", key="start_btn", type="primary", disabled=is_running, use_container_width=True):
        if update_state({"is_running": True}, sid):
            st.toast("Started!", icon="✅")
            st.rerun()
    # FTMO: require 4 trading days; confirm before stop when days < 4
    stop_confirm_pending = st.session_state.get("stop_confirm_pending", False)
    ftmo_days_ok = ftmo_trading_days is None or ftmo_trading_days >= 4
    if stop_confirm_pending and not ftmo_days_ok:
        st.warning(f"FTMO requires **4 trading days**. You have **{ftmo_trading_days or 0}/4**. Stopping now may fail the challenge.")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Yes, stop anyway", key="stop_confirm_yes", type="primary", use_container_width=True):
                st.session_state.pop("stop_confirm_pending", None)
                stopped = False
                try:
                    base = MODAL_SIGNAL_URL.rstrip("/")
                    if base.endswith("/signal"): base = base[:-7]
                    r = requests.post(base + "/closeall", json={"source": "dashboard"},
                        headers=({"X-APEXHYDRA-TOKEN": API_TOKEN} if API_TOKEN else {}), timeout=8)
                    stopped = r.status_code == 200
                except Exception:
                    pass
                if not stopped:
                    stopped = update_state({"is_running": False}, sid)
                if stopped:
                    st.toast("Bot stopped + close all sent!", icon="🛑")
                st.rerun()
        with c2:
            if st.button("Cancel", key="stop_confirm_no", use_container_width=True):
                st.session_state.pop("stop_confirm_pending", None)
                st.rerun()
    elif st.button("⛔ Stop & close all", key="stop_btn", disabled=not is_running, type="primary", use_container_width=True):
        if not ftmo_days_ok:
            st.session_state["stop_confirm_pending"] = True
            st.rerun()
        stopped = False
        try:
            base = MODAL_SIGNAL_URL.rstrip("/")
            if base.endswith("/signal"): base = base[:-7]
            r = requests.post(base + "/closeall",
                json={"source": "dashboard"},
                headers=({"X-APEXHYDRA-TOKEN": API_TOKEN} if API_TOKEN else {}),
                timeout=8,
            )
            stopped = r.status_code == 200
        except Exception:
            pass
        if not stopped:
            stopped = update_state({"is_running": False}, sid)
        if stopped:
            st.toast("Bot stopped + close all sent!", icon="🛑")
            st.rerun()
    st.markdown("**Mode**")
    new_mode = st.selectbox("Strategy mode", ["SAFE", "AGGRESSIVE"], index=0 if mode == "SAFE" else 1, key="mode_select", label_visibility="collapsed")
    if st.button("Apply mode", key="apply_mode_btn", use_container_width=True):
        if update_state({"mode": new_mode}, sid):
            st.toast(f"Mode: {new_mode}", icon="⚙️")
            st.rerun()
    st.markdown("---")
    st.markdown("**Day reset**")
    if st.button("🔄 Reset trading day", key="reset_day", type="secondary", use_container_width=True):
        if update_state({"risk_day_start": datetime.now(timezone.utc).isoformat()}, sid):
            st.toast("Day baseline reset.", icon="♻️")
            st.rerun()
    st.markdown("---")
    st.markdown("**Currency pairs**")
    st.caption("Enable/disable pairs for signals")
    CORE_PAIRS = ["EURJPY", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
    sym_scores = q("symbol_scores")
    enabled_by_sym = {}
    for r in (sym_scores or []):
        s = (r.get("symbol") or "").strip().upper()
        if s in CORE_PAIRS:
            enabled_by_sym[s] = bool(r.get("enabled", True))
    for p in CORE_PAIRS:
        if p not in enabled_by_sym:
            enabled_by_sym[p] = True
    pair_toggles = {}
    for p in CORE_PAIRS:
        pair_toggles[p] = st.toggle(p, value=enabled_by_sym[p], key=f"pair_{p}")
    if st.button("Apply pairs", key="apply_pairs_btn", use_container_width=True):
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            for p in CORE_PAIRS:
                if pair_toggles[p] != enabled_by_sym[p]:
                    db.table("symbol_scores").upsert({
                        "symbol": p, "enabled": pair_toggles[p], "updated_at": now_iso
                    }, on_conflict="symbol").execute()
            st.toast("Currency pairs saved.", icon="✅")
            st.rerun()
        except Exception as e:
            st.error(f"Save failed: {e}")
    st.markdown("---")
    st.caption(f"Last load: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")

# ── Main header ────────────────────────────────────────────────────────────────
status_class = "running" if is_running else "stopped"
status_text = "Running" if is_running else "Stopped"
hdr_l, hdr_m, hdr_r = st.columns([3, 1.4, 2.6])
with hdr_l:
    st.markdown(
        """
        <div class="apex-hstack">
          <div style="font-size:1.35rem;font-weight:800;line-height:1.2;color:var(--text);">ApexHydra PRO</div>
          <div class="apex-muted">Trading control center</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
with hdr_m:
    st.markdown(f'<div class="status-pill {status_class}">● {status_text}</div>', unsafe_allow_html=True)
with hdr_r:
    pills = [
        f"Mode <strong>{mode}</strong>",
        f"Capital <strong>${capital:,.0f}</strong>",
        f"Max DD <strong>{float(state.get('max_daily_dd') or 0.05)*100:.1f}%</strong>",
    ]
    if ftmo_trading_days is not None:
        pills.append(f"FTMO days <strong>{ftmo_trading_days}/{ftmo_min_days}</strong>")
    pills.append(datetime.now(timezone.utc).strftime('%H:%M:%S') + " UTC")
    inner = "".join(f'<span class="apex-pill">{p}</span>' for p in pills)
    st.markdown(
        f'<div class="apex-hstack" style="justify-content:flex-end;">{inner}</div>',
        unsafe_allow_html=True,
    )
st.markdown("<br>", unsafe_allow_html=True)

# ── Close-all acknowledgment ───────────────────────────────────────────────────
try:
    cmd_rows = db.table("bot_commands").select("command,issued_at,acknowledged_at,ack_closed") \
        .eq("command", "close_all").order("issued_at", desc=True).limit(1).execute().data or []
except Exception:
    cmd_rows = []
if cmd_rows:
    row = cmd_rows[0]
    issued = (row.get("issued_at") or "")[:16].replace("T", " ")
    ack_ts = (row.get("acknowledged_at") or "")[:16].replace("T", " ")
    closed_ct = row.get("ack_closed")
    if ack_ts:
        msg = f"Last close-all confirmed by EA at {ack_ts} UTC"
        if closed_ct is not None:
            msg += f" ({closed_ct} positions closed)"
        st.info(msg)
    else:
        st.warning(f"Close-all issued at {issued} UTC — pending EA acknowledgment.")

# ── Metrics ───────────────────────────────────────────────────────────────────
trades      = q("trades", order=("opened_at", True), limit=200)

default_day_start = datetime.now(timezone.utc).replace(
    hour=0, minute=0, second=0, microsecond=0
).isoformat()

# BUG FIX: if risk_day_start is from a previous UTC day, treat it as stale
# and fall back to today's midnight — same logic as compliance engine.
if risk_day_start:
    try:
        rds_dt = datetime.fromisoformat(risk_day_start.replace("Z", "+00:00"))
        today_midnight = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).replace(tzinfo=None)
        rds_naive = rds_dt.replace(tzinfo=None)
        day_start_str = risk_day_start if rds_naive >= today_midnight else default_day_start
    except Exception:
        day_start_str = default_day_start
else:
    day_start_str = default_day_start

# BUG FIX: exclude direction="CLOSED" orphan rows so this count matches what
# _check_compliance() counts in modal_app.py. Without this, the dashboard
# showed a higher "Trades Today" number than the actual enforced limit,
# making it look like the limit was hit when it hadn't been.
# BUG FIX: same phantom-row bug as compliance engine.
# Old filter counted every row with opened_at >= day_start including
# unconfirmed pending signals (lot=NULL / lot=0). Fix: only count rows
# where lot is confirmed (> 0), matching what compliance engine now does.
trades_today = [
    t for t in trades
    if (t.get("opened_at") or "") >= day_start_str
    and t.get("direction") != "CLOSED"
    and t.get("lot") not in (None, 0, 0.0)
]

# Open positions: fetch ALL trades still open — no day_start_str filter.
# BUG FIX: the previous query had .gte("opened_at", day_start_str) which
# silently dropped any trade opened before today's midnight or before a
# manual day reset. This was exactly why the dashboard showed "Open Pos 0/5"
# while MT5 had 4 real open positions — they were opened in a prior session.
# An open trade is open regardless of WHEN it was opened.
try:
    _open_raw = db.table("trades").select("*") \
        .is_("pnl", "null") \
        .is_("closed_at", "null") \
        .not_.is_("lot", "null") \
        .execute().data or []
    open_positions = [t for t in _open_raw if t.get("lot") not in (None, 0, 0.0)]
except Exception:
    # Fallback to in-memory list (also without day_start_str filter)
    open_positions = [
        t for t in trades
        if t.get("pnl") is None
        and t.get("closed_at") is None
        and t.get("lot") not in (None, 0, 0.0)
    ]

# Closed trades in the current trading-day window (by close time)
closed_trades_today = [
    t for t in trades
    if (t.get("closed_at") or "") >= day_start_str
]

# Fetch equity rows since current "day" start, sorted ASC so [0] = opening balance.
# BUG FIX: the previous "fix" set order=("timestamp", True) thinking True=ASC but
# the q() helper maps v[1] directly to the `desc=` parameter, so True=DESC.
# DESC means [0] is the LATEST row — day_start_bal == cur_bal — today_pnl always ~0
# and daily DD was always 0.  Correct value is False (desc=False = ASC).
equity_rows = q("equity", gte=("timestamp", day_start_str), order=("timestamp", False))

# Single most-recent equity row for current balance
all_eq_latest = q("equity", order=("timestamp", True), limit=1)

if all_eq_latest:
    latest_eq  = all_eq_latest[0]
    # DB may return null for balance/equity; .get(k, default) only applies when key is missing
    cur_bal    = float(latest_eq.get("balance") or capital)
    cur_equity = float(latest_eq.get("equity") or cur_bal)
    open_pnl   = cur_equity - cur_bal
else:
    cur_bal    = capital
    cur_equity = capital
    open_pnl   = 0.0

# ── Fetch transaction history (deposits & withdrawals) ────────────────────────
# This is the core fix for withdrawals showing as losses.
# We read from the transactions table written by the EA via POST /transaction.
def _txn_amount(r: dict, key: str = "amount") -> float:
    v = r.get(key)
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0

def _is_deposit(r: dict) -> bool:
    return (r.get("type") or "").strip().lower() == "deposit"

def _is_withdrawal(r: dict) -> bool:
    return (r.get("type") or "").strip().lower() == "withdrawal"

try:
    txn_data        = db.table("transactions").select("type, amount").execute().data or []
    total_deposited = sum(_txn_amount(r) for r in txn_data if _is_deposit(r))
    total_withdrawn = sum(_txn_amount(r) for r in txn_data if _is_withdrawal(r))
except Exception:
    # Graceful fallback if transactions are temporarily unavailable.
    st.caption("Transaction history unavailable — using configured capital as baseline.")
    total_deposited = capital   # fallback: treat configured capital as sole deposit
    total_withdrawn = 0.0

# Withdrawals that happened since current "day" start (needed for today_pnl and daily DD)
try:
    txn_today       = db.table("transactions").select("type, amount") \
                        .gte("event_time", day_start_str).execute().data or []
    withdrawn_today = sum(_txn_amount(r) for r in txn_today if _is_withdrawal(r))
except Exception:
    withdrawn_today = 0.0

# ── P&L baseline ──────────────────────────────────────────────────────────────
# Baseline = total deposited capital ONLY.  Withdrawals are intentional removals
# of profit — they must never reduce the baseline or inflate the loss figure.
#
# Correct mental model:
#   Deposited $10 000 → traded up to $15 000 → withdrew $5 000 → balance $11 000
#   Overall P&L = ($11 000 balance + $5 000 withdrawn) − $10 000 deposited = +$6 000  ✔
#   Old (broken) calc: $11 000 − $15 000 peak = −$4 000  ✘
pnl_baseline = total_deposited if total_deposited > 0 else capital
overall_pnl  = (cur_bal + total_withdrawn) - pnl_baseline

# ── Today P&L ─────────────────────────────────────────────────────────────────
# Add back any withdrawals made today so they don't appear as trading losses.
if equity_rows:
    day_start_bal = float(equity_rows[0].get("balance") or cur_bal)
    today_pnl     = (cur_bal + withdrawn_today) - day_start_bal
else:
    today_pnl = 0.0

# ── Daily drawdown (FTMO: measured on equity = balance + floating P&L) ─────────
# Subtract today's withdrawals from the day-open balance before computing DD
# so a withdrawal at 10:00 doesn't make the rest of the day look like a loss.
if equity_rows:
    bals               = [float(r.get("balance") or capital) for r in equity_rows]
    adj_day_start      = bals[0] - withdrawn_today
    daily_dd_pct       = max(0.0, (adj_day_start - cur_equity) / adj_day_start) * 100 \
                         if adj_day_start > 0 else 0.0
else:
    daily_dd_pct = 0.0

# ── Total (all-time) drawdown ─────────────────────────────────────────────────
# Subtract total_withdrawn from the raw balance peak before computing DD.
# Without this, every withdrawal permanently "inflates" the peak and makes the
# account look like it is in drawdown even after a series of winning trades.
if all_eq_latest:
    try:
        peak_row     = db.table("equity").select("balance") \
                         .order("balance", desc=True).limit(1).execute().data
        raw_peak     = float(peak_row[0].get("balance") or capital) if peak_row else capital
        # Trading peak = highest recorded balance minus capital already withdrawn
        trading_peak = max(raw_peak - total_withdrawn, pnl_baseline)
        total_dd_pct = max(0.0, (trading_peak - cur_equity) / trading_peak) * 100 \
                       if trading_peak > 0 else 0.0
    except Exception:
        total_dd_pct = 0.0
else:
    total_dd_pct = 0.0

# Key metrics (always visible at top of content)
kpi_today_class   = "positive" if today_pnl >= 0 else "negative"
kpi_overall_class = "positive" if overall_pnl >= 0 else "negative"
kpi_dd_class      = "negative" if daily_dd_pct > 0 else ""
st.markdown(
    f"""
    <div class="apex-metric-grid">
      <div class="apex-metric-card"><div class="label">Balance</div><div class="value">${cur_bal:,.2f}</div></div>
      <div class="apex-metric-card"><div class="label">Today P&amp;L</div><div class="value {kpi_today_class}">${today_pnl:+,.2f}</div></div>
      <div class="apex-metric-card"><div class="label">Open</div><div class="value">{len(open_positions)}/{int(state.get('max_concurrent_trades', 5))}</div></div>
      <div class="apex-metric-card"><div class="label">Daily DD</div><div class="value {kpi_dd_class}">{daily_dd_pct:.2f}%</div></div>
      <div class="apex-metric-card"><div class="label">Overall P&amp;L</div><div class="value {kpi_overall_class}">${overall_pnl:+,.2f}</div></div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.expander("Key metrics (details)", expanded=False):
    m1, m2, m3, m4, m5 = st.columns(5)
    m6, m7, m8, m9, m10 = st.columns(5)
    m1.metric("Deposited", f"${pnl_baseline:,.0f}", help="Total capital deposited — P&L baseline")
    m2.metric("Withdrawn", f"${total_withdrawn:,.0f}", help="Total withdrawn (not counted as losses)")
    m3.metric("Balance", f"${cur_bal:,.2f}")
    m4.metric("Equity", f"${cur_equity:,.2f}", delta=f"{open_pnl:+.2f} open", delta_color="normal")
    m5.metric("Overall P&L", f"${overall_pnl:+,.2f}")
    m6.metric("Today P&L", f"${today_pnl:+,.2f}")
    m7.metric("Open pos", f"{len(open_positions)}/{int(state.get('max_concurrent_trades', 5))}",
              help="Open positions vs max concurrent limit.")
    m8.metric("Daily DD", f"{daily_dd_pct:.2f}%",
              delta=f"FTMO limit {float(state.get('max_daily_dd') or 0.05)*100:.1f}%", delta_color="inverse")
    m9.metric("Total DD", f"{total_dd_pct:.2f}%",
              delta=f"FTMO limit {float(state.get('max_total_dd') or 0.10)*100:.1f}%", delta_color="inverse")
    m10.metric("Trades today", f"{len(trades_today)}/{int(state.get('max_trades_per_day', 10))}",
               help="Confirmed trades since day baseline.")
st.markdown("<br>", unsafe_allow_html=True)

# Preload all tab data once
strat_rows  = q("strategies")
regime_rows = q("market_regime")
fwd_rows    = q("forward_results", order=("tested_at", True), limit=20)
bt_rows     = q("backtest_results")
learn_rows  = q("learning_log", order=("learned_at", True), limit=10)
try:
    news_rows = db.table("news_blackouts").select("*").eq("active", True).order("updated_at", desc=True).execute().data or []
except Exception:
    news_rows = []

tab_overview, tab_intel, tab_trades, tab_settings = st.tabs(
    ["📊 Overview", "🧠 Intelligence", "📋 Trades & Logs", "⚙️ Settings"]
)

# ── Tab: Intelligence ────────────────────────────────────────────────────────
with tab_intel:
    st.markdown('<p class="apex-section-title">AI Strategy Intelligence</p>', unsafe_allow_html=True)
    si1, si2, si3 = st.columns(3)
    with si1:
        st.markdown("**📊 Forward Test Scores** *(updated every 4h)*")
        if strat_rows:
            NAMES = {"trend_following":"Trend Following","mean_reversion":"Mean Reversion","breakout":"Breakout"}
            ICONS = {"trend_following":"📈","mean_reversion":"↔️","breakout":"💥"}
            for row in sorted(strat_rows, key=lambda r: r.get("fwd_score",0), reverse=True):
                s  = row["strategy"]
                sc = float(row.get("fwd_score") or 0)
                active_badge = " 🟢 **ACTIVE**" if row.get("is_active") else ""
                st.markdown(f"{ICONS.get(s,'•')} **{NAMES.get(s,s)}**{active_badge}")
                st.progress(sc, text=f"{sc:.3f}")
        else:
            st.caption("Scores appear after first forward test (runs 4h after deploy).")
    with si2:
        st.markdown("**🌍 Live Market Regimes**")
        if regime_rows:
            REGIME_COLOR = {"TRENDING":"🟢","RANGING":"🟡","VOLATILE":"🔴"}
            REGIME_STRAT = {"TRENDING":"→ Trend Following","RANGING":"→ Mean Reversion","VOLATILE":"→ Breakout"}
            for row in regime_rows:
                r  = row.get("regime","—")
                ts = row.get("updated_at","")[:16].replace("T"," ")
                st.markdown(f"{REGIME_COLOR.get(r,'⚪')} **{row['symbol']}**: {r} {REGIME_STRAT.get(r,'')}")
                st.caption(f"Last seen: {ts} UTC")
        else:
            st.caption("Regimes appear after first live signal.")
    with si3:
        st.markdown("**🎓 Online Learning Log**")
        if learn_rows:
            for row in reversed(learn_rows):
                ts = row.get("learned_at","")[:16].replace("T"," ")
                st.markdown(f"✅ **{row.get('strategy','')}** — {row.get('num_trades',0)} trades → {ts}")
        else:
            st.caption("Learning runs every 6h once 10+ closed trades exist.")
    st.divider()
    st.markdown('<p class="apex-section-title">Active News Blackouts</p>', unsafe_allow_html=True)
    if news_rows:
        st.markdown('<p class="apex-section-title">Active News Blackouts — Trading Paused</p>', unsafe_allow_html=True)
        IMPACT_COLOR = {"SHOCK": "🔴", "High": "🟠", "Medium": "🟡"}
        SOURCE_ICON  = {"ForexFactory": "📅", "Finnhub": "📡", "AlphaVantage": "🤖"}
        for row in news_rows:
            impact  = row.get("impact", "High")
            source  = row.get("source", "")
            title   = row.get("title",  "")
            currs   = row.get("currencies", "")
            expires = row.get("expires_at", "")[:16].replace("T", " ")
            st.error(
                f"{IMPACT_COLOR.get(impact,'🔴')} **{currs}** — {title}  \n"
                f"{SOURCE_ICON.get(source,'📰')} *{source}* | Impact: {impact} | "
                f"Expires: {expires} UTC"
            )
    else:
        st.success("✅ No active news blackouts — trading is clear")
    st.divider()
    if bt_rows:
        st.markdown('<p class="apex-section-title">Backtest Results (2 years, weekly refresh)</p>', unsafe_allow_html=True)
        df_bt = pd.DataFrame(bt_rows)
        STRAT_LABEL = {"trend_following":"Trend","mean_reversion":"MeanRev","breakout":"Breakout"}
        df_bt["strategy"] = df_bt["strategy"].map(STRAT_LABEL).fillna(df_bt["strategy"])
        df_bt = df_bt.sort_values("score", ascending=False)
        show  = [c for c in ["symbol","strategy","score","sharpe","win_rate","max_dd","profit_factor","num_trades"] if c in df_bt.columns]
        st.dataframe(
            df_bt[show],
            use_container_width=True, hide_index=True,
            column_config={
                "score":         st.column_config.ProgressColumn("Score",  min_value=0, max_value=1),
                "win_rate":      st.column_config.ProgressColumn("Win %",  min_value=0, max_value=1),
                "max_dd":        st.column_config.NumberColumn("Max DD",  format="%.1%"),
                "sharpe":        st.column_config.NumberColumn("Sharpe",  format="%.2f"),
                "profit_factor": st.column_config.NumberColumn("PF",      format="%.2f"),
            },
        )
    if fwd_rows:
        with st.expander("📈 Forward Test Score History"):
            df_fwd = pd.DataFrame(fwd_rows)
            df_fwd["tested_at"] = pd.to_datetime(df_fwd["tested_at"])
            df_fwd = df_fwd.set_index("tested_at")
            score_cols = [c for c in ["tf_score","mr_score","bo_score"] if c in df_fwd.columns]
            df_fwd = df_fwd[score_cols].rename(columns={
                "tf_score":"Trend Following","mr_score":"Mean Reversion","bo_score":"Breakout"
            })
            st.line_chart(df_fwd)
            st.caption("Dashed line shows which strategy was promoted each round.")

# ── Tab: Overview ──────────────────────────────────────────────────────────────
with tab_overview:
    st.markdown('<p class="apex-section-title">Live Equity & Symbol Rotation</p>', unsafe_allow_html=True)
    o1, o2, o3 = st.columns([2, 1, 1])
    with o1:
        eq_range = st.selectbox(
            "Equity range",
            ["Today", "7 days", "30 days", "All (sampled)"],
            index=0,
            label_visibility="collapsed",
        )
    with o2:
        show_open_panel = st.toggle("Open positions", value=True)
    with o3:
        show_dd_chart = st.toggle("Drawdown chart", value=True)

    # Chart equity range is independent from "today" metrics baseline.
    if eq_range == "Today":
        equity_rows_chart = equity_rows
    else:
        now_utc = datetime.now(timezone.utc)
        if eq_range == "7 days":
            start_dt = now_utc - pd.Timedelta(days=7)
            start_str = start_dt.isoformat()
            equity_rows_chart = q("equity", gte=("timestamp", start_str), order=("timestamp", False), limit=2000)
        elif eq_range == "30 days":
            start_dt = now_utc - pd.Timedelta(days=30)
            start_str = start_dt.isoformat()
            equity_rows_chart = q("equity", gte=("timestamp", start_str), order=("timestamp", False), limit=2000)
        else:
            # All-time: sample most recent N points
            equity_rows_chart = q("equity", order=("timestamp", True), limit=2000)

    left, right = st.columns([5, 2])
    with left:
        if equity_rows_chart:
            df_eq = pd.DataFrame(equity_rows_chart)
            df_eq["timestamp"] = pd.to_datetime(df_eq["timestamp"])
            df_eq = df_eq.sort_values("timestamp")
            df_eq["balance"] = pd.to_numeric(df_eq.get("balance"), errors="coerce")
            df_eq = df_eq.dropna(subset=["timestamp", "balance"])
            df_eq["peak"] = df_eq["balance"].expanding().max()
            df_eq["drawdown_pct"] = (df_eq["balance"] - df_eq["peak"]) / df_eq["peak"] * 100

            base = alt.Chart(df_eq).encode(
                x=alt.X("timestamp:T", title=None),
            )
            bal_chart = (
                base.mark_line(color="#22c55e", strokeWidth=2)
                .encode(
                    y=alt.Y("balance:Q", title="Balance ($)"),
                    tooltip=[
                        alt.Tooltip("timestamp:T", title="Time"),
                        alt.Tooltip("balance:Q", title="Balance", format=",.2f"),
                        alt.Tooltip("drawdown_pct:Q", title="DD %", format=".2f"),
                    ],
                )
                .properties(height=260)
                .interactive()
            )
            st.altair_chart(bal_chart, use_container_width=True)

            if show_dd_chart:
                dd_chart = (
                    base.mark_area(color="#ef4444", opacity=0.12)
                    .encode(
                        y=alt.Y("drawdown_pct:Q", title="Drawdown (%)"),
                        tooltip=[
                            alt.Tooltip("timestamp:T", title="Time"),
                            alt.Tooltip("drawdown_pct:Q", title="Drawdown %", format=".2f"),
                        ],
                    )
                    .properties(height=140)
                    .interactive()
                )
                st.altair_chart(dd_chart, use_container_width=True)

            current_balance  = float(df_eq["balance"].iloc[-1])
            peak_balance     = float(df_eq["peak"].iloc[-1])
            current_drawdown = float(df_eq["drawdown_pct"].iloc[-1])
            stat_col1, stat_col2, stat_col3 = st.columns(3)
            stat_col1.metric("Current Balance", f"${current_balance:,.2f}")
            stat_col2.metric("Peak Balance", f"${peak_balance:,.2f}")
            stat_col3.metric("Current Drawdown", f"{current_drawdown:.2f}%", delta=f"{current_drawdown:.2f}%", delta_color="inverse")
            if len(df_eq) > 1:
                daily_returns = df_eq["balance"].pct_change().dropna()
                avg_daily_return = daily_returns.mean() * 100
                volatility = daily_returns.std() * 100
                perf_col1, perf_col2 = st.columns(2)
                with perf_col1:
                    st.metric("Avg Daily Return", f"{avg_daily_return:.3f}%")
                with perf_col2:
                    st.metric("Daily Volatility", f"{volatility:.3f}%")
        else:
            st.caption("📊 Waiting for equity data... Snapshots run every 15 min via Modal scheduler.")
            st.info("💡 The equity curve will appear automatically once trading begins and data is collected.")
    with right:
        sym_rows = q("symbol_scores")
        if sym_rows:
            st.markdown("**🧭 Symbol rotation**")
            st.caption("Enable/disable symbols used by portfolio rotation.")
            df_ss = pd.DataFrame(sym_rows)
            if "symbol" in df_ss.columns:
                df_ss["enabled"]  = df_ss.get("enabled", True).fillna(True).astype(bool)
                df_ss["score"]    = pd.to_numeric(df_ss.get("score"),    errors="coerce").fillna(0.0)
                df_ss["win_rate"] = pd.to_numeric(df_ss.get("win_rate"), errors="coerce").fillna(0.0)
                df_ss = df_ss.sort_values(["score", "win_rate"], ascending=False)
                show_df = df_ss[["enabled", "symbol", "score", "win_rate"]].rename(
                    columns={"enabled": "On", "symbol": "Symbol", "score": "Score", "win_rate": "Win %"}
                )
                edited = st.data_editor(
                    show_df,
                    use_container_width=True,
                    hide_index=True,
                    disabled=["Symbol", "Score", "Win %"],
                    column_config={
                        "On": st.column_config.CheckboxColumn("On"),
                        "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=1),
                        "Win %": st.column_config.ProgressColumn("Win %", min_value=0, max_value=1),
                    },
                    key="symbol_rotation_editor",
                )
                if st.button("💾 Save symbol changes", use_container_width=True, type="primary", key="save_sym_scores"):
                    try:
                        merged = edited.merge(
                            show_df.rename(columns={"On": "On_old"})[["Symbol", "On_old"]],
                            on="Symbol",
                            how="left",
                        )
                        changed = merged[merged["On"] != merged["On_old"]]
                        if not changed.empty:
                            now_iso = datetime.now(timezone.utc).isoformat()
                            payload = [
                                {"symbol": r["Symbol"], "enabled": bool(r["On"]), "updated_at": now_iso}
                                for _, r in changed.iterrows()
                            ]
                            db.table("symbol_scores").upsert(payload).execute()
                            st.toast("Symbol rotation saved.", icon="✅")
                            st.rerun()
                        else:
                            st.info("No changes to save.")
                    except Exception as e:
                        st.error(f"Failed to save symbols: {e}")
        else:
            st.caption("Symbol scores appear after first rotation (1h after deploy).")
    st.divider()
    if show_open_panel:
        st.markdown('<p class="apex-section-title">Open Positions</p>', unsafe_allow_html=True)
        if open_positions:
            df_open = pd.DataFrame(open_positions)
            # keep columns that exist across schemas
            show_open = [c for c in ["opened_at", "symbol", "direction", "lot", "sl", "tp", "strategy", "regime", "confidence"] if c in df_open.columns]
            # normalize types for display
            if "lot" in df_open.columns:
                df_open["lot"] = pd.to_numeric(df_open["lot"], errors="coerce")
            if "opened_at" in df_open.columns:
                df_open["opened_at"] = pd.to_datetime(df_open["opened_at"], errors="coerce")
            if "confidence" in df_open.columns:
                df_open["confidence"] = pd.to_numeric(df_open["confidence"], errors="coerce")
            if "direction" in df_open.columns:
                df_open["direction"] = df_open["direction"].map(
                    {"BUY": "🟢 BUY", "SELL": "🔴 SELL"}
                ).fillna(df_open["direction"])

            sym_opts = sorted(df_open.get("symbol", pd.Series(dtype=str)).dropna().unique().tolist())
            sym_pick = st.multiselect("Filter symbols", options=sym_opts, default=[], key="open_sym_filter")
            if sym_pick and "symbol" in df_open.columns:
                df_open = df_open[df_open["symbol"].isin(sym_pick)]
            if "opened_at" in df_open.columns:
                df_open = df_open.sort_values("opened_at", ascending=False)

            st.dataframe(
                df_open[show_open].head(50),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "opened_at": st.column_config.DatetimeColumn("Opened (UTC)"),
                    "lot": st.column_config.NumberColumn("Lot", format="%.2f"),
                    "confidence": st.column_config.ProgressColumn("Conf", min_value=0, max_value=1),
                },
            )
        else:
            st.caption("No open positions right now.")
        st.divider()
    st.markdown('<p class="apex-section-title">Per-Symbol Performance</p>', unsafe_allow_html=True)
    ASSET_GROUPS = {
        "⚡ Majors":    ["EURUSD", "GBPUSD", "USDJPY", "USDCHF"],
        "🌏 Commodity": ["AUDUSD", "NZDUSD", "USDCAD"],
        "✝️ Crosses":   ["EURJPY", "GBPJPY", "EURGBP"],
        "🥇 Metals":    ["XAUUSD", "XAUUSDM", "XAGUSD", "XAGUSDM"],
    }
    SYM_GROUP = {}
    for grp, syms in ASSET_GROUPS.items():
        for s in syms:
            SYM_GROUP[s] = grp
    all_closed = [t for t in trades if t.get("pnl") is not None and t.get("lot") not in (None, 0, 0.0)]
    if all_closed:
        sym_stats = {}
        for t in all_closed:
            sym = t.get("symbol", "UNKNOWN")
            pnl = float(t.get("pnl") or 0)
            if sym not in sym_stats:
                sym_stats[sym] = {"trades": 0, "wins": 0, "pnl": 0.0, "group": SYM_GROUP.get(sym, "🔷 Other")}
            sym_stats[sym]["trades"] += 1
            sym_stats[sym]["pnl"]    += pnl
            if pnl > 0:
                sym_stats[sym]["wins"] += 1
        df_sym = pd.DataFrame([
            {
                "Symbol":   sym,
                "Group":    v["group"],
                "Trades":   v["trades"],
                "Win %":    round(v["wins"] / v["trades"] * 100, 1) if v["trades"] > 0 else 0,
                "Total P&L":round(v["pnl"], 2),
                "Avg P&L":  round(v["pnl"] / v["trades"], 2) if v["trades"] > 0 else 0,
            }
            for sym, v in sym_stats.items()
        ]).sort_values("Total P&L", ascending=False)
        grp_exposure = {}
        for t in open_positions:
            sym = t.get("symbol", "")
            grp = SYM_GROUP.get(sym, "🔷 Other")
            grp_exposure[grp] = grp_exposure.get(grp, 0) + 1
        if grp_exposure:
            exp_cols = st.columns(len(grp_exposure))
            for i, (grp, count) in enumerate(sorted(grp_exposure.items())):
                exp_cols[i].metric(grp, f"{count} open")
        else:
            st.caption("No open positions — exposure summary will appear when trades are live.")

        grp_opts = ["All"] + sorted(df_sym["Group"].dropna().unique().tolist())
        grp_sel = st.selectbox("Asset class", options=grp_opts, index=0, key="sym_group_filter")
        df_sym_show = df_sym if grp_sel == "All" else df_sym[df_sym["Group"] == grp_sel]
        st.dataframe(
            df_sym_show,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Total P&L": st.column_config.NumberColumn("Total P&L ($)", format="$%.2f"),
                "Avg P&L":   st.column_config.NumberColumn("Avg P&L ($)",   format="$%.2f"),
                "Win %":     st.column_config.ProgressColumn("Win %", min_value=0, max_value=100, format="%.1f%%"),
            },
        )
        grp_pnl = {}
        for t in all_closed:
            grp = SYM_GROUP.get(t.get("symbol",""), "🔷 Other")
            grp_pnl[grp] = grp_pnl.get(grp, 0.0) + float(t.get("pnl") or 0)
        if grp_pnl:
            df_grp = pd.DataFrame(
                [{"Asset Class": g, "P&L ($)": round(v, 2)} for g, v in grp_pnl.items()]
            ).sort_values("P&L ($)", ascending=False)
            bar = (
                alt.Chart(df_grp)
                .mark_bar(color="#22c55e")
                .encode(
                    x=alt.X("P&L ($):Q", title="P&L ($)"),
                    y=alt.Y("Asset Class:N", sort="-x", title=None),
                    tooltip=[alt.Tooltip("Asset Class:N"), alt.Tooltip("P&L ($):Q", format=",.2f")],
                )
                .properties(height=min(240, 40 * max(1, len(df_grp))))
            )
            st.altair_chart(bar, use_container_width=True)
    else:
        st.caption("Per-symbol breakdown appears after first closed trades.")

# ── Tab: Trades & Logs ─────────────────────────────────────────────────────────
with tab_trades:
    t_trades, t_logs, t_model, t_system = st.tabs(["📈 Trades", "🧾 EA Logs", "🤖 Model", "🧰 System"])

    with t_trades:
        st.markdown('<p class="apex-section-title">Recent Trades</p>', unsafe_allow_html=True)
        if trades:
            df_t  = pd.DataFrame(trades)
            if "features" in df_t.columns:
                df_t.drop(columns=["features"], inplace=True)
            # Filters
            f1, f2, f3, f4 = st.columns([2, 2, 1, 1])
            with f1:
                sym_opts = sorted([s for s in df_t.get("symbol", pd.Series(dtype=str)).dropna().unique().tolist()])
                sym_sel = st.multiselect("Symbols", options=sym_opts, default=[], key="trade_sym_filter")
            with f2:
                status_sel = st.selectbox("Status", ["All", "Open", "Closed"], index=0, key="trade_status_filter")
            with f3:
                max_rows = st.selectbox("Rows", [50, 100, 250, 500], index=0, key="trade_rows")
            with f4:
                newest_first = st.toggle("Newest first", value=True, key="trade_newest_first")

            if sym_sel and "symbol" in df_t.columns:
                df_t = df_t[df_t["symbol"].isin(sym_sel)]
            if status_sel != "All":
                if status_sel == "Open":
                    if "closed_at" in df_t.columns:
                        df_t = df_t[df_t["closed_at"].isna()]
                    if "pnl" in df_t.columns:
                        df_t = df_t[df_t["pnl"].isna()]
                else:
                    if "closed_at" in df_t.columns:
                        df_t = df_t[df_t["closed_at"].notna()]
                    if "pnl" in df_t.columns:
                        df_t = df_t[df_t["pnl"].notna()]
            if "opened_at" in df_t.columns:
                df_t = df_t.sort_values("opened_at", ascending=not newest_first)

            show  = [c for c in ["opened_at","symbol","direction","strategy","regime","lot","pnl","confidence"] if c in df_t.columns]
            df_t["pnl"]        = pd.to_numeric(df_t.get("pnl"),        errors="coerce")
            df_t["confidence"] = pd.to_numeric(df_t.get("confidence"), errors="coerce")
            REGIME_EMOJI = {"TRENDING":"📈","RANGING":"↔️","VOLATILE":"💥"}
            if "regime" in df_t.columns:
                df_t["regime"] = df_t["regime"].map(lambda r: f"{REGIME_EMOJI.get(r,'')} {r}" if r else "")
            st.dataframe(
                df_t[show].head(max_rows), use_container_width=True, hide_index=True,
                column_config={
                    "pnl":        st.column_config.NumberColumn("P&L ($)",    format="$%.2f"),
                    "confidence": st.column_config.ProgressColumn("Conf",     min_value=0, max_value=1),
                    "strategy":   st.column_config.TextColumn("Strategy"),
                    "regime":     st.column_config.TextColumn("Regime"),
                },
            )
            st.download_button(
                label="⬇️ Download trades as CSV",
                data=df_t[show].to_csv(index=False).encode("utf-8"),
                file_name=f"apexhydra_trades_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
                key="trades_download",
            )
        else:
            st.caption("Trades appear as MT5 sends signals.")

    with t_logs:
        st.markdown('<p class="apex-section-title">EA Logs</p>', unsafe_allow_html=True)
        log_col1, log_col2, log_col3 = st.columns([2, 2, 1])
        with log_col1:
            log_level_filter = st.selectbox("Level", ["ALL", "INFO", "WARN", "ERROR"],
                                             key="log_level", label_visibility="collapsed")
        with log_col2:
            log_search = st.text_input("Search logs", placeholder="symbol or keyword…",
                                        key="log_search", label_visibility="collapsed")
        with log_col3:
            log_limit = st.selectbox("Show", [100, 250, 500], key="log_limit",
                                      label_visibility="collapsed")
        try:
            log_rows = db.table("ea_logs").select("*") \
                .order("logged_at", desc=True).limit(log_limit).execute().data or []
        except Exception:
            log_rows = []
        if log_level_filter != "ALL":
            log_rows = [r for r in log_rows if r.get("level") == log_level_filter]
        if log_search:
            kw = log_search.lower()
            log_rows = [r for r in log_rows
                        if kw in (r.get("message") or "").lower()
                        or kw in (r.get("symbol") or "").lower()]
        if log_rows:
            df_logs = pd.DataFrame(log_rows)
            LEVEL_COLOR = {"ERROR": "🔴", "WARN": "🟠", "INFO": "🟢"}
            df_logs["lvl"] = df_logs["level"].map(lambda l: LEVEL_COLOR.get(l, "⚪") + " " + l)
            show_cols = [c for c in ["ea_time", "symbol", "lvl", "message"] if c in df_logs.columns]
            st.dataframe(df_logs[show_cols].rename(columns={"lvl": "Level", "ea_time": "EA Time",
                                                              "symbol": "Symbol", "message": "Message"}),
                         use_container_width=True, hide_index=True)
            csv_data = df_logs[show_cols].to_csv(index=False).encode("utf-8")
            st.download_button(
                label="⬇️ Download logs as CSV",
                data=csv_data,
                file_name=f"apexhydra_ea_logs_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
                key="log_download",
            )
        else:
            st.caption("No EA logs yet. Logs appear after the EA connects and starts sending signals.")

    with t_model:
        st.markdown('<p class="apex-section-title">Model Performance</p>', unsafe_allow_html=True)
        try:
            mp_rows = db.table("model_performance").select("*") \
                .order("timestamp", desc=True).limit(200).execute().data or []
        except Exception:
            mp_rows = []
        if mp_rows:
            df_mp = pd.DataFrame(mp_rows)
            agree_rate = df_mp["agreement"].mean() * 100 if "agreement" in df_mp.columns else 0
            mp_c1, mp_c2, mp_c3 = st.columns(3)
            mp_c1.metric("PPO/Heuristic Agreement", f"{agree_rate:.1f}%",
                         help="% of signals where PPO and heuristic agreed on direction")
            if "ppo_confidence" in df_mp.columns:
                mp_c2.metric("Avg PPO Confidence", f"{df_mp['ppo_confidence'].mean():.3f}")
            if "final_confidence" in df_mp.columns:
                mp_c3.metric("Avg Final Confidence", f"{df_mp['final_confidence'].mean():.3f}")
            if "strategy" in df_mp.columns and "agreement" in df_mp.columns:
                strat_agree = df_mp.groupby("strategy")["agreement"].mean().reset_index()
                strat_agree.columns = ["Strategy", "Agreement Rate"]
                strat_agree["Agreement Rate"] = (strat_agree["Agreement Rate"] * 100).round(1)
                st.dataframe(strat_agree, use_container_width=True, hide_index=True)
        else:
            st.caption("Model performance data appears after trading begins.")

    with t_system:
        st.markdown('<p class="apex-section-title">System Logs</p>', unsafe_allow_html=True)
        try:
            syslog_rows = db.table("system_logs").select("*") \
                .order("timestamp", desc=True).limit(50).execute().data or []
        except Exception:
            syslog_rows = []
        if syslog_rows:
            df_sl = pd.DataFrame(syslog_rows)
            show_sl = [c for c in ["timestamp", "severity", "event_type", "message"] if c in df_sl.columns]
            st.dataframe(df_sl[show_sl], use_container_width=True, hide_index=True)
        else:
            st.caption("No system logs yet.")

# ── Tab: Settings ─────────────────────────────────────────────────────────────
with tab_settings:
    st.markdown('<p class="apex-section-title">Risk & System Settings</p>', unsafe_allow_html=True)

    with st.expander("⚙️ Risk Parameters", expanded=True):
        with st.form("risk"):
            fc1, fc2 = st.columns(2)
            with fc1:
                nc  = st.number_input("Capital ($)",      value=capital,                                    step=500.0)
                init_cap = float(state.get("initial_capital") or state.get("capital") or capital)
                ninit = st.number_input("Initial capital / FTMO ($)", value=init_cap, step=500.0, min_value=0.0, help="FTMO: used for 5%% daily / 10%% total loss limits. Leave same as Capital for challenge.")
                ndd = st.number_input("Max Daily DD (%)", value=float(state.get("max_daily_dd") or 0.05)*100, step=0.5, min_value=0.1, max_value=20.0, help="FTMO: 5%%")
                nct = st.number_input(
                    "Max Concurrent Positions",
                    value=int(state.get("max_concurrent_trades", 5)),
                    step=1,
                    min_value=1,
                    help="Max positions open at the same time. Closed trades free their slot.",
                )
            with fc2:
                ntd = st.number_input("Max Total DD (%)",  value=float(state.get("max_total_dd") or 0.10)*100, step=0.5, min_value=0.1, max_value=50.0, help="FTMO: 10%%")
                ntdp = st.number_input(
                    "Max Trades per Day",
                    value=int(state.get("max_trades_per_day", 10)),
                    step=1,
                    min_value=1,
                    help="Max trades to open in a day across all 12 pairs.",
                )
                eth = st.number_input(
                    "Entry throttle (min)",
                    value=int(state.get("entry_throttle_mins", 5)),
                    step=1,
                    min_value=0,
                    max_value=60,
                    help="Min minutes between opening new positions (0=off).",
                )
            if st.form_submit_button("💾 Save", use_container_width=True):
                patch = {
                    "capital":                nc,
                    "max_daily_dd":           ndd / 100,
                    "max_total_dd":           ntd / 100,
                    "max_concurrent_trades":  int(nct),
                    "max_trades_per_day":     int(ntdp),
                    "entry_throttle_mins":    int(eth),
                }
                if ninit > 0:
                    patch["initial_capital"] = ninit
                if update_state(patch, sid):
                    st.success("Saved."); st.rerun()

    with st.expander("📥 Record deposit / withdrawal", expanded=False):
        st.caption("Use this if you withdrew or deposited from the broker and the EA didn’t record it (e.g. withdrawal from broker website). Recording it here fixes P&L so withdrawals are not shown as losses.")
        with st.form("record_txn"):
            txn_type = st.selectbox("Type", ["withdrawal", "deposit"], key="manual_txn_type")
            txn_amt = st.number_input("Amount ($)", value=1.0, step=10.0, min_value=0.01, format="%.2f", key="manual_txn_amt")
            if st.form_submit_button("Record"):
                if txn_amt <= 0:
                    st.warning("Enter an amount greater than 0.")
                else:
                    try:
                        from datetime import datetime, timezone
                        # Estimate balance_after so DB constraints are satisfied; P&L uses sum(amount) by type
                        bal_after = (cur_bal - txn_amt) if txn_type == "withdrawal" else (cur_bal + txn_amt)
                        db.table("transactions").insert({
                            "type": txn_type,
                            "amount": round(txn_amt, 2),
                            "balance_after": round(bal_after, 2),
                            "event_time": datetime.now(timezone.utc).isoformat(),
                            "reported_at": datetime.now(timezone.utc).isoformat(),
                        }).execute()
                        st.success(f"Recorded {txn_type} of ${txn_amt:,.2f}. Refresh to see updated P&L.")
                    except Exception as e:
                        st.error(f"Failed to record: {e}")

    with st.expander("🔧 System Status", expanded=False):
        st.markdown("""
| Component | What it does | Schedule |
|-----------|-------------|----------|
| `signal` endpoint | Regime → Strategy → PPO → Trade | On every MT5 tick |
| `news_monitor` | Multi-source news blackouts | Every 5 min |
| `equity_snapshot` | Records balance + drawdown | Every 15 min |
| `portfolio_rotation` | Ranks symbols by recent perf | Every hour |
| `run_forward_test` | Paper trades + online PPO learning | Every 4 hours |
| `run_backtest` | 2yr real data, retrains PPO | Every week |
""")

        st.markdown("**🗄️ Required Supabase Tables** *(run once if not already created)*")
        st.code("""
-- Regime history: cross-container cold-start recovery
CREATE TABLE IF NOT EXISTS regime_history (
  id          BIGSERIAL PRIMARY KEY,
  symbol      TEXT NOT NULL,
  regime      TEXT NOT NULL,
  detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_regime_hist ON regime_history (symbol, detected_at DESC);

-- Signal cache: /signal → /open cross-container handoff
CREATE TABLE IF NOT EXISTS signal_cache (
  symbol     TEXT PRIMARY KEY,
  direction  TEXT, strategy TEXT, regime TEXT,
  confidence FLOAT, features JSONB,
  cached_at  TIMESTAMPTZ
);
""", language="sql")

# ── Auto-refresh (sidebar drives interval) ──────────────────────────────────────
# Avoid blocking sleeps; refresh via small client-side timer.
st.session_state.setdefault("auto_refresh", True)
st.session_state.setdefault("refresh_interval", 15)
with st.sidebar:
    st.markdown("---")
    st.markdown("**Refresh**")
    if st.button("⟳ Refresh now", key="refresh_now", type="secondary", use_container_width=True):
        st.rerun()
    auto_refresh = st.checkbox("Auto-refresh", value=st.session_state.auto_refresh, key="auto_refresh")
    interval = st.selectbox(
        "Interval (sec)",
        options=[15, 30, 60],
        index=[15, 30, 60].index(st.session_state.refresh_interval) if st.session_state.refresh_interval in (15, 30, 60) else 0,
        key="refresh_interval",
        label_visibility="collapsed",
    )
if auto_refresh:
    components.html(
        f"""
        <script>
          setTimeout(() => {{
            window.location.reload();
          }}, {int(interval) * 1000});
        </script>
        """,
        height=0,
    )
