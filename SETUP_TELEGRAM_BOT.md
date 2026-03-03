# ApexHydra FTMO — Telegram bot setup

The Telegram bot lets you check status, open/closed positions, performance, and control the EA (start/pause/stop) from your phone. It reads from the same Supabase database as the dashboard and EA.

---

## 1. Prerequisites

- **Supabase** project with the FTMO schema applied (`schema_ftmo.sql`). The bot needs `bot_state`, `equity`, `trades`, etc.
- **Python 3.10+** (for running the bot locally).

---

## 2. Create a Telegram bot and get your token

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts (name and username, e.g. `ApexHydra FTMO` and `ApexHydraFTMO_bot`).
3. BotFather replies with a **token** like `123456789:ABCdefGHIjklMNOpqrSTUvwxYZ`. Copy it — this is `TELEGRAM_BOT_TOKEN`.

---

## 3. Get your Telegram user ID (allowed user)

The bot only responds to users listed in `TELEGRAM_ALLOWED_IDS`. You need your numeric chat ID.

**Option A — use @userinfobot**

1. In Telegram, search for **@userinfobot**.
2. Start the bot; it will reply with your **Id** (e.g. `123456789`). That value is your allowed ID.

**Option B — from your bot**

1. Start your bot (send `/start`).
2. Open: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
3. Send another message to the bot, refresh the page. In the JSON, look for `"from":{"id": 123456789}` — that number is your ID.

Use one or more IDs, comma-separated, for `TELEGRAM_ALLOWED_IDS` (e.g. `123456789` or `123456789,987654321`).

---

## 4. Get Supabase keys

In Supabase: **Project Settings → API**

- **Project URL** → `SUPABASE_URL`
- **service_role** key (under Project API keys) → `SUPABASE_KEY` (or use anon key if you prefer; service_role has full access)

---

## 5. Configure environment (.env)

In the project root, create or edit `.env`:

```env
# Required
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_ALLOWED_IDS=123456789
SUPABASE_URL=https://YOUR-PROJECT.supabase.co
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

Replace with your real bot token, your Telegram ID(s), and your Supabase URL/key.

**Optional (defaults are fine):**

```env
DD_ALERT_PCT=4.0
DD_CRITICAL_PCT=8.0
MONITOR_INTERVAL_S=60
```

- `DD_ALERT_PCT` — drawdown % that triggers a warning (default 4).
- `DD_CRITICAL_PCT` — drawdown % that triggers a critical alert (default 8).
- `MONITOR_INTERVAL_S` — how often the bot checks for alerts (default 60 seconds).

---

## 6. Install and run

```bash
cd "d:\Web Apps\Trading files\Apexhydra\ApexHydra_FTMO"
pip install -r requirements.txt
python apex_hydra_FTMO_bot.py
```

You should see something like:

```
ApexHydra FTMO Telegram Bot started.
```

In Telegram, send `/start` to your bot. You should get the welcome message and command list. Try `/status` to see account summary from Supabase.

---

## 7. Commands (in Telegram)

| Command | Description |
|--------|-------------|
| `/start` | Welcome and command list |
| `/status` | Account summary, balance, today P&L, open positions, inline Start/Pause/Stop |
| `/perf` | Performance, win rate, profit factor, per-symbol P&L |
| `/trades` | Last 10 trades |
| `/open` | Current open positions |
| `/regimes` | Live market regimes per symbol |
| `/news` | Active news blackouts |
| `/logs` | Recent EA logs |
| `/config` | Current settings (capital, limits, mode) |
| `/start_bot` | Resume EA (set `is_running` true) |
| `/pause` | Pause EA (no new trades) |
| `/stop` | Emergency stop (with confirmation) |
| `/setcapital <amount>` | Set capital (e.g. `/setcapital 100000`) |
| `/setmaxdd <pct>` | Set max daily DD % (e.g. `/setmaxdd 5`) |
| `/setmaxpos <n>` | Set max concurrent positions (e.g. `/setmaxpos 5`) |
| `/geo_pause [hours]` | Manual geopolitical blackout (blocks new trades) |
| `/geo_clear` | Clear manual geo blackout |
| `/help` | Detailed help |

---

## 8. Run in background (optional)

**Windows (PowerShell):**

```powershell
Start-Process python -ArgumentList "apex_hydra_FTMO_bot.py" -WindowStyle Hidden
```

Or use a process manager (e.g. NSSM, PM2 with python) or a VPS.

**Linux/macOS:**

```bash
nohup python apex_hydra_FTMO_bot.py > bot.log 2>&1 &
```

---

## 9. Troubleshooting

| Issue | What to do |
|-------|------------|
| **"Unauthorized"** | Add your Telegram user ID to `TELEGRAM_ALLOWED_IDS` in `.env`. Restart the bot. |
| **No response to /start** | Check `TELEGRAM_BOT_TOKEN`. Make sure you’re messaging the correct bot. |
| **"State push failed" / DB errors | Check `SUPABASE_URL` and `SUPABASE_KEY`. Ensure `schema_ftmo.sql` has been run and `bot_state` has at least one row. |
| **ModuleNotFoundError: telegram** | Run `pip install -r requirements.txt` (includes `python-telegram-bot`). |
| **ModuleNotFoundError: dotenv** | Run `pip install python-dotenv`. |

---

## 10. Summary

1. Create a bot with @BotFather and copy **TELEGRAM_BOT_TOKEN**.
2. Get your Telegram **user ID** (e.g. via @userinfobot) and set **TELEGRAM_ALLOWED_IDS**.
3. Set **SUPABASE_URL** and **SUPABASE_KEY** in `.env`.
4. Run: `pip install -r requirements.txt` then `python apex_hydra_FTMO_bot.py`.
5. In Telegram, send `/start` and use `/status`, `/config`, etc.

The bot uses the same Supabase data as the dashboard and EA; no extra deployment is required as long as the bot process is running and `.env` is correct.
