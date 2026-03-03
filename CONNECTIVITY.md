# EA ↔ Modal ↔ Supabase ↔ Telegram bot — connectivity guide

All three components stay in sync through **one chain**: the **EA** sends data to **Modal**, Modal reads/writes **Supabase**, and the **Telegram bot** (and dashboard) only read **Supabase**. If any link in the chain fails, the bot and dashboard will show stale or no data.

---

## 1. Data flow

```
┌─────────────┐     POST /signal, /equity,     ┌─────────────┐      read/write       ┌─────────────┐      read only       ┌─────────────────┐
│  MT5 EA     │ ─── /open, /close, /log,       │   Modal     │ ────────────────────► │  Supabase   │ ◄──────────────────── │ Telegram bot    │
│ ApexHydra   │     /transaction, /commands    │ apexhydra-  │   (bot_state, equity,  │  (database) │   (bot_state,        │ & Dashboard     │
│ FTMO        │ ◄── response (action, lot…)    │ pro-web     │    trades, etc.)      │             │    equity, trades)   │                 │
└─────────────┘     + GET /commands            └─────────────┘                       └─────────────┘                       └─────────────────┘
```

- **EA → Modal**: WebRequest from MT5 to your Modal app URL. If this fails, nothing reaches the DB.
- **Modal → Supabase**: Modal needs `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` in its **secrets**. If wrong, Modal returns 500 or doesn’t persist data.
- **Telegram bot / Dashboard**: Only read Supabase. They never call Modal. So if EA↔Modal or Modal↔Supabase is broken, they show old or empty data.

---

## 2. One URL, one token (required)

Use the **same** base URL and (if you use auth) the **same** token everywhere.

### Base URL

After deploy, Modal shows a URL like:

```text
https://YOUR-WORKSPACE--apexhydra-pro-web.modal.run
```

- **No trailing slash**
- **No path** (no `/signal` in the URL itself)

Use this **exact** value in:

| Place | Variable | Example |
|-------|----------|---------|
| **EA inputs** (MT5) | `MODAL_BASE_URL` | `https://YOUR-WORKSPACE--apexhydra-pro-web.modal.run` |
| **Dashboard** (Streamlit secrets) | `MODAL_SIGNAL_URL` | Same URL |
| **.env** (if you have scripts that call Modal) | Same URL | Same URL |

If the EA still has the placeholder `https://YOUR-WORKSPACE--apexhydra-pro-web.modal.run`, replace `YOUR-WORKSPACE` with your real Modal workspace name (e.g. `https://acme--apexhydra-pro-web.modal.run`).

### API token (optional but recommended)

If you set `APEXHYDRA_API_TOKEN` in **Modal secrets**, then:

- **EA**: In MT5 EA inputs, set `API_TOKEN` to the **same** value.
- **Dashboard**: In Streamlit secrets, set `APEXHYDRA_API_TOKEN` to the **same** value.

If any of these sends a different token (or no token), Modal returns **401 Unauthorized** and the EA/dashboard will see failed requests.

---

## 3. Checklist

### EA (MetaTrader 5)

- [ ] **MODAL_BASE_URL** = your real Modal URL (no slash at the end), e.g. `https://acme--apexhydra-pro-web.modal.run`
- [ ] **API_TOKEN** = same as `APEXHYDRA_API_TOKEN` in Modal (or leave empty if you don’t use auth in Modal)
- [ ] **Allowed URLs**: In MT5 go to **Tools → Options → Expert Advisors** and add the **exact** base URL (e.g. `https://acme--apexhydra-pro-web.modal.run`). Without this, WebRequest returns error **-1** in the EA.

### Modal

- [ ] App deployed: `modal deploy modal_app.py` (or your deploy command).
- [ ] **Secrets** (Modal dashboard or CLI): create/use a secret (e.g. `apexhydra-secrets`) with:
  - `SUPABASE_URL` = your Supabase project URL
  - `SUPABASE_SERVICE_KEY` = your Supabase **service_role** key
  - `APEXHYDRA_API_TOKEN` = optional; if set, EA and dashboard must send this in `X-APEXHYDRA-TOKEN`

### Dashboard (Streamlit)

- [ ] **MODAL_SIGNAL_URL** = same base URL as the EA (e.g. `https://acme--apexhydra-pro-web.modal.run`)
- [ ] **APEXHYDRA_API_TOKEN** = same as in Modal and EA (if you use auth)
- [ ] **SUPABASE_URL** and **SUPABASE_SERVICE_KEY** = same Supabase project as Modal

### Telegram bot

- [ ] **SUPABASE_URL** and **SUPABASE_KEY** = same Supabase project. The bot does **not** call Modal; it only reads Supabase. So it will “see” data only when the EA → Modal → Supabase chain is working.

---

## 4. Verify step by step

### Step 1: Modal is reachable

In a browser or with curl:

```bash
curl -s -o /dev/null -w "%{http_code}" "https://YOUR-MODAL-URL.modal.run/"
```

You should get **200**. If you get 4xx/5xx or connection error, the URL or deploy is wrong.

### Step 2: EA can reach Modal (MT5 Experts tab)

1. Attach the EA and leave it running.
2. In the **Experts** tab, look for:
   - `[SYMBOL] ✘ WebRequest FATAL error 4010` (or similar) → URL not in **Allowed URLs** (add the base URL in Tools → Options → Expert Advisors).
   - `[SYMBOL] ✘ HTTP 401` → API token mismatch: set the same `APEXHYDRA_API_TOKEN` in Modal and `API_TOKEN` in the EA.
   - `[SYMBOL] ✔` and `Equity reported: balance=…` → EA → Modal is working.

### Step 3: Data reaches Supabase

1. In **Supabase** → Table Editor, open **equity** and **bot_state**.
2. After the EA has been running for a minute, you should see new **equity** rows (balance/equity from MT5).
3. If **equity** stays empty, Modal is not writing. Check Modal logs (Modal dashboard → your app → Logs) for Supabase or runtime errors. Confirm **SUPABASE_URL** and **SUPABASE_SERVICE_KEY** in Modal secrets.

### Step 4: Telegram bot and dashboard see fresh data

- Open the **dashboard** or send **/status** in Telegram. You should see the same balance and state as in Supabase.
- If they still show zero or old data, the problem is upstream (EA → Modal or Modal → Supabase); fix those first using the steps above.

---

## 5. Common errors

| Symptom | Likely cause | Fix |
|--------|----------------|-----|
| EA: WebRequest error **-1** or **4010** | URL not allowed in MT5 | Add the **exact** Modal base URL in Tools → Options → Expert Advisors → Allow WebRequest for listed URL. |
| EA: **HTTP 401** from Modal | Token mismatch or missing | Set same `APEXHYDRA_API_TOKEN` in Modal secrets and `API_TOKEN` in EA (or remove token in Modal to disable auth). |
| EA: **HTTP 5xx** from Modal | Error inside Modal or Supabase | Check Modal app logs; fix Supabase URL/key in Modal secrets and schema. |
| No new rows in **equity** | Modal not receiving or not writing | Confirm EA logs show 200 for /equity; then check Modal logs and Supabase credentials. |
| Bot/Dashboard always 0 or old | Data not in Supabase | Fix EA → Modal and Modal → Supabase (steps 2–3). Bot and dashboard only reflect what’s in Supabase. |

---

## 6. Quick reference: where each thing is configured

| What | Where |
|------|--------|
| Modal base URL | EA input `MODAL_BASE_URL`; Dashboard secret `MODAL_SIGNAL_URL` |
| API token | EA input `API_TOKEN`; Modal secret `APEXHYDRA_API_TOKEN`; Dashboard secret `APEXHYDRA_API_TOKEN` |
| Supabase | Modal secrets `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`; Dashboard secrets; Telegram bot `.env` `SUPABASE_URL`, `SUPABASE_KEY` |
| MT5 Allowed URL | Tools → Options → Expert Advisors → add the Modal base URL |

Once the **same** URL and token are set everywhere and the EA can reach Modal and Modal can write to Supabase, the Telegram bot and dashboard will show up-to-date data from the same database.
