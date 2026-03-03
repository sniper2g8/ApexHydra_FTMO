"""
╔══════════════════════════════════════════════════════════════════════╗
║         ApexHydra Forex — Telegram Management Bot                   ║
║  Full remote control + live alerts via Telegram                      ║
║                                                                      ║
║  .env:                                                               ║
║    TELEGRAM_BOT_TOKEN=123456:ABC...                                  ║
║    TELEGRAM_ALLOWED_IDS=123456789                                    ║
║    SUPABASE_URL=https://xxx.supabase.co  (FOREX Supabase project)   ║
║    SUPABASE_KEY=your_service_role_key                                ║
║    DD_ALERT_PCT=4.0                                                  ║
║    DD_CRITICAL_PCT=8.0                                               ║
║    MONITOR_INTERVAL_S=60                                             ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os, logging
from datetime import datetime, timezone, timedelta
from functools import wraps

from dotenv import load_dotenv
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, JobQueue,
)
from telegram.constants import ParseMode

load_dotenv()
logging.basicConfig(
    format="%(asctime)s │ %(name)s │ %(levelname)s │ %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ApexHydra-Forex-Bot")

# ── Config ────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_IDS  = set(int(x) for x in os.environ.get("TELEGRAM_ALLOWED_IDS","").split(",") if x.strip())
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

DD_ALERT_PCT       = float(os.getenv("DD_ALERT_PCT",      "4.0"))
DD_CRITICAL_PCT    = float(os.getenv("DD_CRITICAL_PCT",   "8.0"))
MONITOR_INTERVAL_S = int(os.getenv("MONITOR_INTERVAL_S",  "60"))

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

_alert_state: dict = {
    "last_dd_alert":    None,
    "dd_alerted_pct":   0.0,
    "last_trade_alert": None,
    "stopped_alerted":  False,
}

# ── Display helpers ───────────────────────────────────────────────────
REGIME_EMOJI    = {"TRENDING": "📈", "RANGING": "↔️", "VOLATILE": "💥"}
DIRECTION_EMOJI = {"BUY": "🟢", "SELL": "🔴"}
ASSET_GROUPS    = {
    "EURUSD":"Major","GBPUSD":"Major","USDJPY":"Major","USDCHF":"Major",
    "AUDUSD":"Commodity","NZDUSD":"Commodity","USDCAD":"Commodity",
    "EURJPY":"Cross","GBPJPY":"Cross","EURGBP":"Cross",
    "XAUUSD":"Metal","XAUUSDM":"Metal","XAGUSD":"Metal","XAGUSDM":"Metal",
}

# ── DB helpers (forex schema) ─────────────────────────────────────────

def db_get_state() -> dict:
    """bot_state table — equivalent of ea_config in crypto bot."""
    r = sb.table("bot_state").select("*").order("updated_at", desc=True).limit(1).execute()
    return r.data[0] if r.data else {}


def db_push_state(updates: dict) -> bool:
    try:
        r = sb.table("bot_state").select("id").order("updated_at", desc=True).limit(1).execute()
        if r.data:
            updates["updated_at"] = datetime.now(timezone.utc).isoformat()
            sb.table("bot_state").update(updates).eq("id", r.data[0]["id"]).execute()
        return True
    except Exception as e:
        logger.error(f"State push failed: {e}"); return False


def db_get_equity() -> dict:
    """Latest row from equity table (balance/equity snapshots)."""
    r = sb.table("equity").select("*").order("timestamp", desc=True).limit(1).execute()
    return r.data[0] if r.data else {}


def db_get_equity_today() -> list:
    today = datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
    r = (sb.table("equity").select("balance,timestamp")
           .gte("timestamp", today).order("timestamp", desc=False).execute())
    return r.data or []


def db_get_recent_trades(limit: int = 10) -> list:
    """
    Forex trades columns:
      symbol, direction (BUY/SELL), strategy, regime, lot, pnl,
      confidence, opened_at, closed_at
    Open = pnl IS NULL. Closed = pnl IS NOT NULL.
    """
    r = (sb.table("trades").select("*")
           .order("opened_at", desc=True).limit(limit).execute())
    return r.data or []


def db_get_open_positions() -> list:
    try:
        r = (sb.table("trades").select("*")
               .is_("pnl","null").is_("closed_at","null").execute())
        return [t for t in (r.data or []) if t.get("lot") not in (None,0,0.0)]
    except Exception as e:
        logger.error(f"Open positions error: {e}"); return []


def db_get_trades_today() -> list:
    today = datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
    r = (sb.table("trades").select("*").gte("opened_at", today).execute())
    return [t for t in (r.data or [])
            if t.get("direction") != "CLOSED"
            and t.get("lot") not in (None,0,0.0)]


def db_get_closed_trades(limit: int = 500) -> list:
    r = (sb.table("trades").select("*")
           .not_.is_("pnl","null")
           .order("closed_at", desc=True).limit(limit).execute())
    return r.data or []


def db_get_current_regimes() -> list:
    try:
        r = sb.table("market_regime").select("*").execute()
        return r.data or []
    except Exception as e:
        logger.error(f"Regime error: {e}"); return []


def db_get_recent_logs(limit: int = 15) -> list:
    try:
        r = (sb.table("ea_logs").select("*")
               .order("logged_at", desc=True).limit(limit).execute())
        return r.data or []
    except Exception as e:
        logger.error(f"Log error: {e}"); return []


def db_get_news_blackouts() -> list:
    try:
        r = (sb.table("news_blackouts").select("*")
               .eq("active", True).order("updated_at", desc=True).execute())
        return r.data or []
    except Exception: return []


def db_recent_withdrawal(within_minutes: int = 10) -> dict | None:
    """
    Returns the most recent withdrawal from the transactions table if one
    occurred within the last `within_minutes` minutes, otherwise None.
    Used to suppress false DD alerts caused by withdrawals, not trading losses.
    """
    try:
        since = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
        r = (sb.table("transactions")
               .select("type,amount,balance_after,event_time")
               .eq("type", "withdrawal")
               .gte("event_time", since)
               .order("event_time", desc=True)
               .limit(1)
               .execute())
        return r.data[0] if r.data else None
    except Exception:
        return None


def _compute_daily_pnl(state: dict) -> tuple[float, float]:
    """Returns (today_pnl, daily_dd_pct) from equity table."""
    eq_today  = db_get_equity_today()
    eq_latest = db_get_equity()
    cur_bal   = float(eq_latest.get("balance", float(state.get("capital", 0))))
    if eq_today:
        day_start = float(eq_today[0].get("balance", cur_bal))
        today_pnl = cur_bal - day_start
        daily_dd  = max(0.0, (day_start - cur_bal) / day_start * 100) if day_start > 0 else 0.0
    else:
        today_pnl = 0.0
        daily_dd  = 0.0
    return today_pnl, daily_dd


# ── Auth ──────────────────────────────────────────────────────────────

def restricted(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        uid = update.effective_user.id
        if ALLOWED_IDS and uid not in ALLOWED_IDS:
            await update.message.reply_text("⛔ Unauthorized. Your ID: " + str(uid))
            return
        return await func(update, ctx, *args, **kwargs)
    return wrapper


def restricted_callback(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        uid = update.effective_user.id
        if ALLOWED_IDS and uid not in ALLOWED_IDS:
            await update.callback_query.answer("⛔ Unauthorized")
            return
        return await func(update, ctx, *args, **kwargs)
    return wrapper


# ── Commands ──────────────────────────────────────────────────────────

@restricted
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🐍 <b>ApexHydra Forex Bot</b> (FTMO compliant)\n\n"
        "📊 <b>Monitoring:</b>\n"
        "/status — Status + account summary\n"
        "/perf — Performance + per-symbol P&amp;L\n"
        "/trades — Last 10 trades\n"
        "/open — Current open positions\n"
        "/regimes — Live market regimes\n"
        "/news — Active news blackouts\n"
        "/logs — Recent EA logs\n\n"
        "🎛 <b>Control:</b>\n"
        "/start_bot — Resume trading\n"
        "/pause — Pause (no new trades)\n"
        "/stop — ⚠️ Emergency stop\n"
        "/config — View current settings\n\n"
        "⚙️ <b>Risk Settings:</b>\n"
        "/setcapital &lt;amount&gt;\n"
        "/setmaxdd &lt;pct&gt; — Max daily DD %\n"
        "/setmaxpos &lt;n&gt; — Max concurrent positions\n\n"
        "🔔 <b>Alerts:</b>\n"
        f"DD warning &gt;{DD_ALERT_PCT}%  |  critical &gt;{DD_CRITICAL_PCT}%\n"
        f"Every trade open/close  |  Daily 00:00 UTC summary"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state     = db_get_state()
    eq        = db_get_equity()
    open_pos  = db_get_open_positions()
    trades_td = db_get_trades_today()

    is_running  = bool(state.get("is_running", False))
    status_icon = "✅ RUNNING" if is_running else "🔴 STOPPED"
    cur_bal     = float(eq.get("balance", float(state.get("capital", 0))))
    cur_equity  = float(eq.get("equity",  cur_bal))
    open_pnl    = cur_equity - cur_bal
    today_pnl, daily_dd = _compute_daily_pnl(state)
    max_dd      = float(state.get("max_daily_dd", 0.05)) * 100
    max_pos     = int(state.get("max_concurrent_trades", 5))
    max_td      = int(state.get("max_trades_per_day", 20))
    updated_at  = str(state.get("updated_at","N/A"))[:16]

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶ Start",  callback_data="ctrl_start"),
            InlineKeyboardButton("⏸ Pause",  callback_data="ctrl_pause"),
        ],
        [InlineKeyboardButton("⛔ Emergency Stop", callback_data="ctrl_stop")],
        [InlineKeyboardButton("🔄 Refresh",         callback_data="status_refresh")],
    ])

    text = (
        f"<b>🐍 ApexHydra Forex</b>\n{'─'*28}\n"
        f"<b>Status:</b> {status_icon}\n"
        f"<b>Last sync:</b> {updated_at} UTC\n\n"
        f"<b>💰 Account</b>\n"
        f"Balance:    <code>${cur_bal:,.2f}</code>\n"
        f"Equity:     <code>${cur_equity:,.2f}</code>  ({open_pnl:+.2f} open)\n"
        f"Today P&amp;L:  <code>{today_pnl:+.2f}</code>\n"
        f"Daily DD:   <code>{daily_dd:.2f}%</code> / {max_dd:.1f}% limit"
        f"{'  ⚠️' if daily_dd > DD_ALERT_PCT else ''}\n\n"
        f"<b>📊 Today</b>\n"
        f"Open:   <code>{len(open_pos)}</code> / {max_pos} positions\n"
        f"Trades: <code>{len(trades_td)}</code> / {max_td} today (FTMO)\n\n"
        f"<b>⚙️ Settings</b>\n"
        f"Capital:  <code>${float(state.get('capital',0)):,.0f}</code>\n"
        f"Max DD:   <code>{max_dd:.1f}%</code>\n"
        f"Max Pos:  <code>{max_pos}</code>\n"
        f"Mode:     <code>{state.get('mode','SAFE')}</code>\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


@restricted
async def cmd_perf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    closed  = db_get_closed_trades()
    eq      = db_get_equity()
    state   = db_get_state()
    cur_bal = float(eq.get("balance", float(state.get("capital", 0))))
    capital = float(state.get("capital", cur_bal))

    if not closed:
        await update.message.reply_text(
            f"📭 No closed trades yet.\nBalance: <code>${cur_bal:,.2f}</code>",
            parse_mode=ParseMode.HTML)
        return

    total_pnl  = sum(float(t.get("pnl",0) or 0) for t in closed)
    wins       = sum(1 for t in closed if float(t.get("pnl",0) or 0) > 0)
    win_rate   = wins / len(closed) * 100 if closed else 0
    gross_win  = sum(float(t.get("pnl",0) or 0) for t in closed if float(t.get("pnl",0) or 0) > 0)
    gross_loss = abs(sum(float(t.get("pnl",0) or 0) for t in closed if float(t.get("pnl",0) or 0) < 0))
    pf         = gross_win / gross_loss if gross_loss > 0 else 0.0
    overall_pnl = cur_bal - capital
    today_pnl, daily_dd = _compute_daily_pnl(state)
    dd_color   = "🟥" if daily_dd > DD_CRITICAL_PCT else "🟧" if daily_dd > DD_ALERT_PCT else "🟩"

    # Per-symbol
    sym_stats: dict = {}
    for t in closed:
        sym = t.get("symbol","?")
        pnl = float(t.get("pnl",0) or 0)
        if sym not in sym_stats:
            sym_stats[sym] = {"pnl":0.0,"trades":0,"wins":0}
        sym_stats[sym]["pnl"]    += pnl
        sym_stats[sym]["trades"] += 1
        if pnl > 0: sym_stats[sym]["wins"] += 1

    text = (
        f"<b>📊 Performance — Forex</b>\n{'─'*28}\n"
        f"Balance:       <code>${cur_bal:,.2f}</code>\n"
        f"Overall P&amp;L:   <code>{overall_pnl:+,.2f}</code>\n"
        f"Today P&amp;L:     <code>{today_pnl:+,.2f}</code>\n"
        f"Daily DD:      {dd_color} <code>{daily_dd:.2f}%</code>\n\n"
        f"<b>Trades ({len(closed)} closed)</b>\n"
        f"Wins / Losses: <code>{wins} / {len(closed)-wins}</code>\n"
        f"Win Rate:      <code>{win_rate:.1f}%</code>\n"
        f"Profit Factor: <code>{pf:.2f}</code>\n"
        f"Total P&amp;L:     <code>{total_pnl:+,.2f}</code>\n"
    )
    if sym_stats:
        text += "\n<b>Per Symbol</b>\n"
        for sym, v in sorted(sym_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)[:12]:
            wr   = v["wins"] / v["trades"] * 100 if v["trades"] > 0 else 0
            icon = "✅" if v["pnl"] >= 0 else "❌"
            text += f"{icon} <code>{sym:<8}</code> <code>{v['pnl']:+.2f}</code> WR:<code>{wr:.0f}%</code> ({v['trades']}t)\n"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted
async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    trades = db_get_recent_trades(10)
    if not trades:
        await update.message.reply_text("📭 No trades yet.")
        return
    text = "<b>📋 Last 10 Trades</b>\n" + "─"*28 + "\n"
    for t in trades:
        ts  = str(t.get("opened_at",""))[:16]
        sym = t.get("symbol","")
        d   = t.get("direction","")
        reg = t.get("regime","?")
        pnl = t.get("pnl")
        lot = t.get("lot",0)
        conf= float(t.get("confidence",0) or 0)*100
        strat = str(t.get("strategy","?"))[:10]
        dir_e = DIRECTION_EMOJI.get(d,"⚪")
        reg_e = REGIME_EMOJI.get(str(reg),"⚪")
        if pnl is not None:
            pnl_v = float(pnl)
            pnl_s = f"  {'✅' if pnl_v>=0 else '❌'}<code>{pnl_v:+.2f}</code>"
        else:
            pnl_s = "  🟡 open"
        text += (
            f"<code>{ts}</code> {dir_e} <b>{sym}</b> {reg_e}\n"
            f"  lot:<code>{lot}</code> conf:<code>{conf:.0f}%</code> {strat}{pnl_s}\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted
async def cmd_open(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    positions = db_get_open_positions()
    if not positions:
        await update.message.reply_text("📭 No open positions.")
        return
    text = f"<b>📂 Open Positions ({len(positions)})</b>\n" + "─"*28 + "\n"
    for t in positions:
        sym  = t.get("symbol","")
        d    = t.get("direction","")
        lot  = t.get("lot",0)
        reg  = t.get("regime","?")
        strat= str(t.get("strategy","?"))[:12]
        conf = float(t.get("confidence",0) or 0)*100
        opnd = str(t.get("opened_at",""))[:16]
        dir_e= DIRECTION_EMOJI.get(d,"⚪")
        reg_e= REGIME_EMOJI.get(str(reg),"⚪")
        text += (
            f"{dir_e} <b>{sym}</b> {reg_e} <code>{d}</code>\n"
            f"  lot:<code>{lot}</code> conf:<code>{conf:.0f}%</code> {strat}\n"
            f"  opened: <code>{opnd}</code>\n\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted
async def cmd_regimes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    regimes = db_get_current_regimes()
    if not regimes:
        await update.message.reply_text("📭 No regime data yet.")
        return
    text  = "<b>🌍 Live Market Regimes</b>\n" + "─"*28 + "\n"
    STRAT = {"TRENDING":"→ Trend","RANGING":"→ MeanRev","VOLATILE":"→ Breakout"}
    for r in regimes:
        sym  = r.get("symbol","")
        reg  = r.get("regime","?")
        ts   = str(r.get("updated_at",""))[:16]
        icon = REGIME_EMOJI.get(str(reg),"⚪")
        text += f"{icon} <b>{sym}</b>: <i>{reg}</i> {STRAT.get(str(reg),'')}\n  <code>{ts} UTC</code>\n\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted
async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    blackouts = db_get_news_blackouts()
    if not blackouts:
        await update.message.reply_text("✅ No active news blackouts — trading is clear.")
        return
    text = "<b>🚨 Active News Blackouts</b>\n" + "─"*28 + "\n"
    IMPACT_COLOR = {"SHOCK":"🔴","High":"🟠","Medium":"🟡","FOMC":"🏦"}
    for row in blackouts:
        impact  = row.get("impact","High")
        title   = row.get("title","")[:60]
        currs   = row.get("currencies","")
        expires = str(row.get("expires_at",""))[:16].replace("T"," ")
        source  = row.get("source","")
        icon    = IMPACT_COLOR.get(impact,"🔴")
        text += f"{icon} <b>{currs}</b> — {title}\n  {source} | {impact} | expires {expires} UTC\n\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted
async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logs = db_get_recent_logs(15)
    if not logs:
        await update.message.reply_text("📭 No EA logs yet.")
        return
    text = "<b>📋 Recent EA Logs</b>\n" + "─"*28 + "\n"
    LEVEL_ICON = {"ERROR":"🔴","WARN":"🟠","INFO":"🟢"}
    for row in logs:
        ts  = str(row.get("ea_time", row.get("logged_at","")))[:16]
        sym = row.get("symbol","")
        lvl = row.get("level","INFO")
        msg = str(row.get("message",""))[:80]
        msg = msg.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        sym_s = f"<code>{sym}</code> " if sym else ""
        text += f"<code>{ts}</code> {LEVEL_ICON.get(lvl,'⚪')} {sym_s}{msg}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted
async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = db_get_state()
    init_cap = state.get("initial_capital") or state.get("capital") or 0
    text = (
        f"<b>⚙️ Forex EA Config (FTMO)</b>\n{'─'*28}\n"
        f"Running:       <code>{state.get('is_running',False)}</code>\n"
        f"Mode:          <code>{state.get('mode','SAFE')}</code>\n"
        f"Capital:       <code>${float(state.get('capital',0)):,.0f}</code>\n"
        f"Initial (FTMO):<code>${float(init_cap):,.0f}</code>\n"
        f"Max Daily DD:  <code>{float(state.get('max_daily_dd',0.05))*100:.1f}%</code> (FTMO 5%)\n"
        f"Max Total DD:  <code>{float(state.get('max_total_dd',0.10))*100:.1f}%</code> (FTMO 10%)\n"
        f"Max Positions: <code>{state.get('max_concurrent_trades',5)}</code>\n"
        f"Max Trades/day:<code>{state.get('max_trades_per_day',20)}</code>\n"
        f"Updated:       <code>{str(state.get('updated_at','N/A'))[:16]}</code>\n\n"
        f"<code>/setcapital 1000</code>\n"
        f"<code>/setmaxdd 5</code>\n"
        f"<code>/setmaxpos 5</code>\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── Control commands ──────────────────────────────────────────────────

@restricted
async def cmd_start_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if db_push_state({"is_running": True}):
        await update.message.reply_text("▶️ EA <b>Started</b>.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Failed.")


@restricted
async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if db_push_state({"is_running": False}):
        await update.message.reply_text("⏸ EA <b>Paused</b>.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Failed.")

@restricted
async def cmd_geo_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manually inject a geopolitical blackout — blocks new trades for N hours (default 4)."""
    from datetime import datetime, timezone, timedelta
    args = ctx.args
    hours = 4
    reason = "Manual geopolitical override"
    if args:
        try:
            hours = float(args[0])
        except (ValueError, TypeError, IndexError):
            pass
        if len(args) > 1:
            reason = " ".join(args[1:])

    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=hours)
    try:
        sb.table("news_blackouts").insert({
            "source":     "Manual",
            "title":      f"🚨 {reason}"[:120],
            "currencies": "ALL",
            "impact":     "GEOPOLITICAL",
            "event_time": now.isoformat(),
            "expires_at": expires.isoformat(),
            "active":     True,
            "updated_at": now.isoformat(),
        }).execute()
        await update.message.reply_text(
            f"🚨 <b>Geopolitical blackout set</b>\n"
            f"Reason: <i>{reason}</i>\n"
            f"Duration: <code>{hours:.0f}h</code> (until <code>{expires.strftime('%H:%M UTC')}</code>)\n"
            f"New trades blocked. Existing positions unaffected.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to set blackout: {e}")


@restricted
async def cmd_geo_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Clear any active manual geopolitical blackout."""
    try:
        sb.table("news_blackouts").delete().eq("source", "Manual").execute()
        await update.message.reply_text("✅ Manual geo blackout cleared. Trading resumed.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")


@restricted
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ CONFIRM STOP", callback_data="confirm_stop"),
        InlineKeyboardButton("❌ Cancel",        callback_data="cancel_stop"),
    ]])
    await update.message.reply_text(
        "⚠️ <b>Confirm Emergency Stop?</b>",
        parse_mode=ParseMode.HTML, reply_markup=keyboard)


@restricted
async def cmd_setcapital(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(ctx.args[0]); assert amount >= 0
    except (ValueError, TypeError, IndexError, AssertionError):
        await update.message.reply_text("Usage: <code>/setcapital 1000</code>", parse_mode=ParseMode.HTML)
        return
    if db_push_state({"capital": amount}):
        await update.message.reply_text(f"✅ Capital → <code>${amount:,.2f}</code>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Failed.")


@restricted
async def cmd_setmaxdd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(ctx.args[0]); assert 1.0 <= val <= 30.0
    except (ValueError, TypeError, IndexError, AssertionError):
        await update.message.reply_text("Usage: <code>/setmaxdd 5</code> (1–30)", parse_mode=ParseMode.HTML)
        return
    if db_push_state({"max_daily_dd": val/100}):
        await update.message.reply_text(f"✅ Max daily DD → <code>{val}%</code>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Failed.")


@restricted
async def cmd_setmaxpos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = int(ctx.args[0]); assert 1 <= val <= 12
    except (ValueError, TypeError, IndexError, AssertionError):
        await update.message.reply_text("Usage: <code>/setmaxpos 5</code> (1–12)", parse_mode=ParseMode.HTML)
        return
    if db_push_state({"max_concurrent_trades": val}):
        await update.message.reply_text(f"✅ Max positions → <code>{val}</code>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Failed.")


@restricted
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>📖 ApexHydra Forex Bot — Help</b>\n\n"
        "<b>Monitoring:</b>\n"
        "<code>/status</code> — Full status + inline controls\n"
        "<code>/perf</code> — P&amp;L + per-symbol breakdown\n"
        "<code>/trades</code> — Last 10 trades\n"
        "<code>/open</code> — Current open positions\n"
        "<code>/regimes</code> — Live regime per symbol\n"
        "<code>/news</code> — Active news blackouts\n"
        "<code>/logs</code> — Recent EA logs\n"
        "<code>/config</code> — All settings\n\n"
        "<b>Control:</b>\n"
        "<code>/start_bot</code> — Resume EA\n"
        "<code>/pause</code> — Pause EA\n"
        "<code>/geo_pause 4 Iran strikes</code> — Manual geo blackout\n"
        "<code>/geo_clear</code> — Clear geo blackout\n"
        "<code>/stop</code> — ⚠️ Emergency stop\n\n"
        "<b>Risk:</b>\n"
        "<code>/setcapital &lt;$&gt;</code>\n"
        "<code>/setmaxdd &lt;pct&gt;</code> — Daily DD limit (1–30)\n"
        "<code>/setmaxpos &lt;n&gt;</code> — Max positions (1–12)\n\n"
        "<b>Auto-Alerts:</b>\n"
        f"• DD &gt; <code>{DD_ALERT_PCT}%</code> warning\n"
        f"• DD &gt; <code>{DD_CRITICAL_PCT}%</code> critical\n"
        "• Every trade open/close\n"
        "• Daily summary at 00:00 UTC\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── Inline callbacks ──────────────────────────────────────────────────

@restricted_callback
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "ctrl_start":
        db_push_state({"is_running": True})
        await query.edit_message_text("▶️ EA <b>Started</b>.", parse_mode=ParseMode.HTML)

    elif data == "ctrl_pause":
        db_push_state({"is_running": False})
        await query.edit_message_text("⏸ EA <b>Paused</b>.", parse_mode=ParseMode.HTML)

    elif data == "ctrl_stop":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ CONFIRM STOP", callback_data="confirm_stop"),
            InlineKeyboardButton("❌ Cancel",        callback_data="cancel_stop"),
        ]])
        await query.edit_message_text("⚠️ <b>Confirm Stop?</b>", parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data == "confirm_stop":
        db_push_state({"is_running": False})
        await query.edit_message_text("⛔ EA <b>STOPPED</b>. Use /start_bot to resume.", parse_mode=ParseMode.HTML)

    elif data == "cancel_stop":
        await query.edit_message_text("✅ Stop cancelled.", parse_mode=ParseMode.HTML)

    elif data == "status_refresh":
        state     = db_get_state()
        eq        = db_get_equity()
        open_pos  = db_get_open_positions()
        trades_td = db_get_trades_today()
        is_running  = bool(state.get("is_running", False))
        status_icon = "✅ RUNNING" if is_running else "🔴 STOPPED"
        cur_bal     = float(eq.get("balance", float(state.get("capital",0))))
        cur_equity  = float(eq.get("equity",  cur_bal))
        open_pnl    = cur_equity - cur_bal
        today_pnl, daily_dd = _compute_daily_pnl(state)
        max_pos     = int(state.get("max_concurrent_trades",5))
        max_td      = int(state.get("max_trades_per_day",40))
        text = (
            f"<b>🐍 ApexHydra Forex</b>  <i>(refreshed)</i>\n"
            f"<b>Status:</b> {status_icon}\n"
            f"Balance: <code>${cur_bal:,.2f}</code>  Equity: <code>${cur_equity:,.2f}</code>\n"
            f"Open P&amp;L: <code>{open_pnl:+.2f}</code>  Today: <code>{today_pnl:+.2f}</code>\n"
            f"Daily DD: <code>{daily_dd:.2f}%</code>\n"
            f"Positions: <code>{len(open_pos)}</code>/{max_pos}  "
            f"Trades today: <code>{len(trades_td)}</code>/{max_td}\n"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶ Start",  callback_data="ctrl_start"),
             InlineKeyboardButton("⏸ Pause",  callback_data="ctrl_pause")],
            [InlineKeyboardButton("⛔ Emergency Stop", callback_data="ctrl_stop")],
            [InlineKeyboardButton("🔄 Refresh",         callback_data="status_refresh")],
        ])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ── Background monitor ────────────────────────────────────────────────

async def monitor_job(ctx: ContextTypes.DEFAULT_TYPE):
    if not ALLOWED_IDS: return
    chat_ids = list(ALLOWED_IDS)
    try:
        state      = db_get_state()
        eq         = db_get_equity()
        if not state: return
        is_running = bool(state.get("is_running", True))
        cur_bal    = float(eq.get("balance", float(state.get("capital",0))))
        today_pnl, daily_dd = _compute_daily_pnl(state)
        now        = datetime.now(timezone.utc)
        cooldown   = timedelta(minutes=30)

        # DD alerts
        last_dd   = _alert_state.get("last_dd_alert")
        dd_alerted= _alert_state.get("dd_alerted_pct", 0.0)

        if daily_dd >= DD_CRITICAL_PCT:
            if dd_alerted < DD_CRITICAL_PCT or (last_dd and now - last_dd > cooldown):
                recent_w = db_recent_withdrawal(within_minutes=10)
                if recent_w:
                    amount = float(recent_w.get("amount", 0))
                    if not _alert_state.get("withdrawal_notified") == recent_w.get("event_time"):
                        msg = (
                            f"💸 <b>Withdrawal Detected — Forex</b>\n"
                            f"Balance drop caused by a withdrawal of <code>${amount:,.2f}</code>, "
                            f"not trading losses.\n"
                            f"Balance: <code>${cur_bal:,.2f}</code>"
                        )
                        for cid in chat_ids: await ctx.bot.send_message(cid, msg, parse_mode=ParseMode.HTML)
                        _alert_state["withdrawal_notified"] = recent_w.get("event_time")
                    _alert_state["dd_alerted_pct"] = 0.0
                else:
                    msg = (
                        f"🔴 <b>CRITICAL DD — Forex</b>\n"
                        f"Daily DD: <code>{daily_dd:.2f}%</code> "
                        f"(limit: <code>{float(state.get('max_daily_dd',0.05))*100:.1f}%</code>)\n"
                        f"Balance: <code>${cur_bal:,.2f}</code>\nConsider: /stop"
                    )
                    for cid in chat_ids: await ctx.bot.send_message(cid, msg, parse_mode=ParseMode.HTML)
                    _alert_state["last_dd_alert"] = now
                    _alert_state["dd_alerted_pct"] = daily_dd

        elif daily_dd >= DD_ALERT_PCT:
            if dd_alerted < DD_ALERT_PCT or (last_dd and now - last_dd > cooldown):
                recent_w = db_recent_withdrawal(within_minutes=10)
                if recent_w:
                    _alert_state["dd_alerted_pct"] = 0.0
                else:
                    msg = (
                        f"🟠 <b>DD Warning — Forex</b>\n"
                        f"Daily DD: <code>{daily_dd:.2f}%</code> (alert: <code>{DD_ALERT_PCT}%</code>)\n"
                        f"Balance: <code>${cur_bal:,.2f}</code>"
                    )
                    for cid in chat_ids: await ctx.bot.send_message(cid, msg, parse_mode=ParseMode.HTML)
                    _alert_state["last_dd_alert"] = now
                    _alert_state["dd_alerted_pct"] = daily_dd
        else:
            _alert_state["dd_alerted_pct"] = 0.0

        # EA stopped alert
        if not is_running and not _alert_state.get("stopped_alerted"):
            msg = f"⛔ <b>Forex EA STOPPED</b>\nDaily DD: <code>{daily_dd:.2f}%</code>\nUse /start_bot to resume."
            for cid in chat_ids: await ctx.bot.send_message(cid, msg, parse_mode=ParseMode.HTML)
            _alert_state["stopped_alerted"] = True
        elif is_running:
            _alert_state["stopped_alerted"] = False

        # New trade alerts
        last_trade_ts = _alert_state.get("last_trade_alert")
        trades = db_get_recent_trades(5)
        if trades:
            newest    = trades[0]
            newest_ts = newest.get("opened_at","") or newest.get("closed_at","")
            if last_trade_ts != newest_ts:
                _alert_state["last_trade_alert"] = newest_ts
                sym   = newest.get("symbol","")
                d     = newest.get("direction","")
                lot   = newest.get("lot",0)
                conf  = float(newest.get("confidence",0) or 0)*100
                reg   = newest.get("regime","?")
                pnl   = newest.get("pnl")
                strat = newest.get("strategy","?")
                dir_e = DIRECTION_EMOJI.get(d,"⚪")
                reg_e = REGIME_EMOJI.get(str(reg),"⚪")

                if pnl is None:
                    opened = str(newest.get("opened_at",""))[:16]
                    msg = (
                        f"📂 <b>Trade OPENED</b>\n"
                        f"{dir_e} <b>{sym}</b> | Lots: <code>{lot}</code> | Conf: <code>{conf:.0f}%</code>\n"
                        f"Regime: {reg_e} <i>{reg}</i> | <i>{strat}</i>\n"
                        f"<code>{opened}</code>"
                    )
                else:
                    pnl_v  = float(pnl)
                    icon   = "✅" if pnl_v >= 0 else "❌"
                    closed = str(newest.get("closed_at",""))[:16]
                    msg = (
                        f"📁 <b>Trade CLOSED</b> {icon}\n"
                        f"{dir_e} <b>{sym}</b> P&amp;L: <code>{pnl_v:+.2f}</code>\n"
                        f"Lots: <code>{lot}</code> | {reg_e} <i>{reg}</i>\n"
                        f"<code>{closed}</code>"
                    )
                for cid in chat_ids: await ctx.bot.send_message(cid, msg, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Monitor error: {e}")


async def daily_summary_job(ctx: ContextTypes.DEFAULT_TYPE):
    if not ALLOWED_IDS: return
    try:
        state   = db_get_state()
        eq      = db_get_equity()
        closed  = db_get_closed_trades()
        cur_bal = float(eq.get("balance", float(state.get("capital",0))))
        today_pnl, daily_dd = _compute_daily_pnl(state)
        total_pnl = sum(float(t.get("pnl",0) or 0) for t in closed)
        wins      = sum(1 for t in closed if float(t.get("pnl",0) or 0) > 0)
        win_rate  = wins / len(closed) * 100 if closed else 0
        date_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pnl_icon  = "✅" if today_pnl >= 0 else "❌"

        # Today's closed trades by symbol
        today_str = datetime.now(timezone.utc).replace(
            hour=0,minute=0,second=0,microsecond=0).isoformat()
        today_closed = [t for t in closed if (t.get("closed_at") or "") >= today_str]
        sym_today: dict = {}
        for t in today_closed:
            sym = t.get("symbol","?")
            pnl = float(t.get("pnl",0) or 0)
            sym_today[sym] = sym_today.get(sym, 0.0) + pnl

        text = (
            f"📅 <b>Daily Summary — {date_str}</b> (Forex)\n{'─'*28}\n"
            f"Balance:    <code>${cur_bal:,.2f}</code>\n"
            f"Today P&amp;L:  {pnl_icon} <code>{today_pnl:+,.2f}</code>\n"
            f"Daily DD:   <code>{daily_dd:.2f}%</code>\n"
            f"All-time:   <code>{total_pnl:+,.2f}</code> | WR: <code>{win_rate:.1f}%</code> ({len(closed)} trades)\n"
        )
        if sym_today:
            text += "\n<b>Today by symbol:</b>\n"
            for sym, pnl in sorted(sym_today.items(), key=lambda x: x[1], reverse=True):
                icon = "✅" if pnl >= 0 else "❌"
                text += f"{icon} <code>{sym:<8}</code> <code>{pnl:+.2f}</code>\n"

        for cid in ALLOWED_IDS:
            await ctx.bot.send_message(cid, text, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Daily summary error: {e}")


# ── Main ──────────────────────────────────────────────────────────────

async def post_init(application: Application):
    commands = [
        BotCommand("start",      "Show welcome message"),
        BotCommand("status",     "EA status + account summary"),
        BotCommand("perf",       "Performance metrics"),
        BotCommand("trades",     "Last 10 trades"),
        BotCommand("open",       "Current open positions"),
        BotCommand("regimes",    "Live market regimes"),
        BotCommand("news",       "Active news blackouts"),
        BotCommand("logs",       "Recent EA logs"),
        BotCommand("config",     "View current settings"),
        BotCommand("start_bot",  "Resume EA trading"),
        BotCommand("pause",      "Pause EA"),
        BotCommand("stop",       "Emergency stop"),
        BotCommand("setcapital", "Set capital baseline"),
        BotCommand("setmaxdd",   "Set max daily DD %"),
        BotCommand("setmaxpos",  "Set max concurrent positions"),
        BotCommand("help",       "Detailed help"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("ApexHydra Forex Telegram Bot started.")


def main():
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")
    if not ALLOWED_IDS:
        logger.warning("TELEGRAM_ALLOWED_IDS not set — bot is open to everyone!")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    for cmd, handler in [
        ("start",      cmd_start),
        ("help",       cmd_help),
        ("status",     cmd_status),
        ("perf",       cmd_perf),
        ("trades",     cmd_trades),
        ("open",       cmd_open),
        ("regimes",    cmd_regimes),
        ("news",       cmd_news),
        ("logs",       cmd_logs),
        ("config",     cmd_config),
        ("start_bot",  cmd_start_bot),
        ("pause",      cmd_pause),
        ("geo_pause",  cmd_geo_pause),
        ("geo_clear",  cmd_geo_clear),
        ("stop",       cmd_stop),
        ("setcapital", cmd_setcapital),
        ("setmaxdd",   cmd_setmaxdd),
        ("setmaxpos",  cmd_setmaxpos),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(CallbackQueryHandler(button_handler))

    jq: JobQueue = app.job_queue
    jq.run_repeating(monitor_job, interval=MONITOR_INTERVAL_S, first=10)
    jq.run_daily(
        daily_summary_job,
        time=datetime.strptime("00:00","%H:%M").time().replace(tzinfo=timezone.utc)
    )

    logger.info(f"Starting Forex bot — {MONITOR_INTERVAL_S}s monitor — {len(ALLOWED_IDS)} users")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
