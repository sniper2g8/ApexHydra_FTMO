# FTMO account check — compliance checklist

Use this before and during your FTMO challenge to avoid rule breaches.

---

## 1. Rules enforced by the bot

| Rule | Bot behavior | Status |
|------|----------------|--------|
| **Max Daily Loss (5%)** | Server: equity cannot drop below (balance at day start) − 5% of initial. **EA backup**: local 4% daily circuit breaker blocks new trades if server is down. | ✅ Enforced |
| **Max Loss (10%)** | Equity cannot drop below max(initial, trailing peak) − 10% of initial. Checked before every new signal. | ✅ Enforced |
| **Equity used for limits** | FTMO uses equity (balance + open P&L). We use the `equity` column from the equity table when available. | ✅ Correct |
| **News blackout (backup)** | Server has news filter; **EA** has hardcoded backup: NFP (first Fri 12:00–14:00 GMT), FOMC-style (Wed 18:00–20:00 GMT). No new trades in these windows if Modal is down. | ✅ EA backup |
| **Min 4 trading days** | Server counts distinct trading days; dashboard and API show **FTMO days X/4**. You must trade at least 4 days before passing evaluation. | ✅ Tracked |
| **Friday close (standard)** | **EA**: optional close of all EA positions on Friday at 21:00 GMT (configurable). Use for standard FTMO; disable for Swing. | ✅ Optional |

---

## 2. Day boundary: UTC vs CE(S)T

- **FTMO** recalculates the daily loss limit at **midnight CE(S)T** (Prague).
- **This app** uses **midnight UTC** for “trading day” (and optional manual reset via dashboard).
- **Difference**: In winter (CET) UTC = CET, so the day aligns. In summer (CEST) midnight Prague = 22:00 UTC previous day, so our “day” can be **up to 2 hours offset** from FTMO’s.

**Recommendation**: Treat the bot as a **conservative** guard. If in doubt, use the dashboard “Reset trading day” to align with FTMO’s day (e.g. after checking Account MetriX “Today’s permitted loss” reset time). For strict alignment with Prague, you could run a daily job at 00:00 CET that sets `risk_day_start`; the schema supports it.

---

## 3. Pre-flight checklist (before starting the challenge)

- [ ] **Supabase**: `schema_ftmo.sql` has been run; `bot_state` has one row.
- [ ] **Initial capital** = your FTMO account size (e.g. 100,000). Set in dashboard **Risk Parameters** → Capital and **Initial capital (FTMO)**. Same value in both for a fresh challenge.
- [ ] **Max Daily DD** = 5% and **Max Total DD** = 10% (defaults in schema and app).
- [ ] **EA**: Attach **ApexHydraFTMO** (not the old EA). All new positions will have comment `ApexHydraFTMO`; only these are managed and closed by the EA/dashboard.
- [ ] **Modal**: Deployed and secrets include `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, and optionally `APEXHYDRA_API_TOKEN` (same as in EA and dashboard).
- [ ] **Allowed URLs**: In MT5 → Tools → Options → Expert Advisors, add your Modal base URL so WebRequest can reach `/signal`, `/equity`, `/open`, `/close`, `/transaction`, `/log`, `/commands`.

---

## 4. Rules not enforced by the bot (you must comply yourself)

| Rule | What to do |
|------|------------|
| **Min 4 trading days** | Dashboard shows **FTMO days X/4**. Trade on at least 4 different days; if you hit profit target in 3 days, keep trading until day 4 or FTMO will fail the evaluation. |
| **Best Day Rule** | Your best single day’s profit must not exceed 50% of your total “positive days’ profit”. Informational only; we do not enforce it. Monitor in FTMO Account MetriX. |
| **Profit target** | 10% (Challenge) / 5% (Verification). Track progress on the dashboard; close all positions before the target is reviewed. |
| **No copy trading / tick scalping / abusive strategies** | The strategy is your own (regime + PPO); avoid copy trading, tick scalping, or martingale/grid that cannot scale. |

---

## 5. Potential bugs and edge cases (addressed)

| Issue | Fix |
|-------|-----|
| **No equity data today** | Daily limit is still enforced using the last balance before day start and the latest equity row (no “free pass” on first signal of the day). |
| **Close-all closing manual positions** | Dashboard “Stop & close all” now closes only positions with comment `ApexHydraFTMO`. |
| **Old position comment** | Positions with comment `ApexHydra` (old EA) are no longer managed; use the new EA and only new positions get `ApexHydraFTMO`. |
| **Server down during news** | EA has local news blackout (NFP first Fri 12–14 GMT, Wed 18–20 GMT). Disable with `USE_LOCAL_NEWS_BLACKOUT = false` if you prefer server-only. |
| **Server down: daily loss** | EA tracks day-start balance (midnight GMT) and blocks new trades when daily loss ≥ 4% (`LOCAL_DAILY_LOSS_PCT`). Gives 1% buffer before FTMO 5% limit. |
| **Server down: total DD** | Set EA input `INITIAL_CAPITAL_FOR_DD` to your FTMO start capital. EA then blocks new trades when total DD ≥ 9.5% (`LOCAL_TOTAL_DD_PCT`) so 10% is never breached if Modal is down. |
| **Telegram critical DD** | Use `DD_CRITICAL_PCT=4.8` in .env (default in code). FTMO breaches at 5%; critical alert must fire before that. |
| **XAGUSD spread** | Silver spread cap raised to 40 pips (EA and server) so valid silver trades are not blocked by session spikes. |
| **Weekend hold (standard)** | EA can close all its positions on Friday at 21:00 GMT (`FRIDAY_CLOSE_STANDARD = true`). Set to `false` for Swing accounts. |

---

## 6. Quick verification

1. **Dashboard** → Settings → Risk: Initial capital = FTMO size, Max Daily DD = 5%, Max Total DD = 10%.
2. **Telegram** `/config`: Same limits and “Initial (FTMO)” shown.
3. **MT5**: EA name shows “ApexHydraFTMO”; Experts log shows “ApexHydraFTMO MultiScan started”.
4. After a few trades, **Dashboard** → Overview: Daily DD and Total DD stay below the stated limits.

If any of the above fail, fix config or schema before continuing the challenge.
