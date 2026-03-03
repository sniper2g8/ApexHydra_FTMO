//+------------------------------------------------------------------+
//| ApexHydraFTMO.mq5                                                |
//| Multi-symbol scanner — one chart trades all pairs                |
//| Reports real MT5 account balance to Modal on every scan          |
//| so the dashboard P&L updates in real time.                       |
//+------------------------------------------------------------------+
#property copyright "ApexHydra FTMO"
#property strict

#include <Trade\Trade.mqh>

//── Inputs ───────────────────────────────────────────────────────────
// Set MODAL_BASE_URL to your deployed Modal app (e.g. https://acme--apexhydra-pro-web.modal.run)
// No trailing slash. Add this URL to MT5 Tools→Options→Expert Advisors (Allowed URLs).
// See CONNECTIVITY.md if EA/Modal/Dashboard are not communicating.
input string MODAL_BASE_URL    = "https://YOUR-WORKSPACE--apexhydra-pro-web.modal.run";
input string API_TOKEN         = "";        // same as Modal secret APEXHYDRA_API_TOKEN (or leave "" if auth disabled)
// SYMBOL FIX: Updated to standard XAUUSD (not XAUUSDm).
// If your broker uses a different name, change GOLD_SYMBOL below.
// Reduced universe: TOTAL 5 symbols including metals (less noise).
// 3 forex + XAUUSD + XAGUSD. Edit if your broker uses suffixes (e.g. EURUSDm).
input string SYMBOLS_CSV       = "EURUSD,GBPUSD,USDJPY,XAUUSD,XAGUSD";
input double LOT_SIZE          = 0.01;   // Fallback / forex default (when dynamic not used)
input double XAUUSD_LOT        = 0.10;   // Max lot for gold (XAUUSD) — used as cap for metals
input double XAGUSD_LOT        = 0.02;   // Max lot for silver (XAGUSD)
input string GOLD_SYMBOL       = "XAUUSD"; // Set to your broker's exact gold symbol name
// Risk capital is set via the dashboard (Supabase bot_state.capital column).
// It arrives in every /signal response as "risk_capital" and is stored in g_riskCapital.
// Change it via the settings page — no EA recompile needed.
input int    BARS_TO_SEND      = 60;        // must be >= 50 (EMA50 + RSI14 + ADX14 need 50+ bars)
input int    SIGNAL_INTERVAL_S = 60;        // seconds between symbol scans
input int    EQUITY_INTERVAL_S = 30;        // seconds between balance reports
// TF must match the timeframe your model was trained on (backend + PPO use M15).
// Using H1 here while the model was built on M15 will mismatch features and hurt performance.
input ENUM_TIMEFRAMES TF       = PERIOD_M15;
input int    MAX_POSITIONS_SYM = 1;
input bool   CLOSE_ON_REVERSE  = true;

//── Stop Loss / Take Profit (ATR-based) ───────────────────────────────
// 1:1.5 risk:reward — TP = 1.5 × SL (hit TP earlier, still positive expectancy).
input double ATR_SL_MULT       = 1.5;  // SL = 1.5 × ATR
input double ATR_TP_MULT       = 2.25; // TP = 2.25 × ATR (1.5 × SL → 1:1.5 R:R)
input int    ATR_PERIOD        = 14;   // ATR lookback period
input int    SL_PIPS_FALLBACK  = 15;   // fallback SL if ATR unavailable
input int    TP_PIPS_FALLBACK  = 23;   // fallback TP (1.5 × SL)
input int    SL_PIPS_JPY_CROSS = 25;   // fallback SL for JPY crosses
input int    TP_PIPS_JPY_CROSS = 38;   // fallback TP for JPY (1.5 × SL)
input int    SL_PIPS_GOLD      = 120;  // fallback SL gold
input int    TP_PIPS_GOLD      = 180;  // fallback TP gold (1.5 × SL)
input bool   USE_TRAILING_STOP = true; // trail SL once price moves 1 ATR in profit
input double TRAIL_ATR_MULT    = 1.0;  // trail distance = 1.0 × ATR
// PROFITABLE FIX #3: Partial close DISABLED.
// Closing 50% at TP then stopping remainder at breakeven reduces average winner
// to ~0.75×TP while full SL stays intact — destroys the 1:2 R:R math.
// Let full position run to TP. Trailing stop protects profits if price extends.
input bool   USE_PARTIAL_CLOSE = false; // DISABLED — kills R:R ratio
input double PARTIAL_CLOSE_PCT = 0.5;  // (unused while disabled)
// Half-profit lock: once price reaches this fraction of the way to TP, move SL there
// so you lock in that much profit (e.g. 0.5 = lock 50% of potential TP gain).
input bool   USE_HALF_PROFIT_LOCK = true;  // lock partial profit so you don't give it back
input double HALF_PROFIT_RATIO    = 0.5;   // lock at 50% of (TP - entry)
// Only move SL (breakeven / half-profit / trail) after price has passed this fraction
// of the way to TP. 0.5 = 50% (move earlier), 0.6 = 60% (more room for trade to recover).
input double SL_MOVE_PAST_PCT     = 0.5;   // move SL after this % toward TP (default 50% = earlier lock)

// Metals quick-exit: for XAU/XAG only, optionally close the full position
// once price has reached N×R (R = initial SL distance).  This lets gold/silver
// bank profits earlier (e.g. 1.0R) instead of always waiting for the full 1.5R TP.
// 0.0 = disabled (no quick-exit; use normal TP/half-lock/trail only).
input double METAL_QUICK_R        = 1.0;   // close metals at ~1.0R if > 0

//── Local news blackout (backup when Modal is down) ─────────────────────
input bool   USE_LOCAL_NEWS_BLACKOUT = true;  // backup when server is unreachable
//── Daily loss circuit breaker (FTMO 5% limit; EA stops at 4% as buffer) ─
input double LOCAL_DAILY_LOSS_PCT    = 4.0;   // no new trades if daily loss >= this %
//── Total DD circuit breaker (FTMO 10% limit; backup when Modal is down) ─
input double INITIAL_CAPITAL_FOR_DD  = 0;     // 0 = disable; set to FTMO start capital for 10% local guard
input double LOCAL_TOTAL_DD_PCT      = 9.5;   // no new trades if total DD >= this % (buffer before 10%)
//── Friday close (standard FTMO: no weekend hold; Swing account = false) ─
input bool   FRIDAY_CLOSE_STANDARD   = true;  // close all before weekend
input int    FRIDAY_CLOSE_HOUR_GMT   = 21;    // close by this hour GMT on Friday

//── Globals ──────────────────────────────────────────────────────────
CTrade   trade;
string   g_symbols[];
datetime g_lastScan[];
datetime g_lastEquity = 0;
int      g_symbolCount = 0;
datetime g_lastDepositScan = 0;  // tracks how far back we've already scanned for deposits/withdrawals
// WebRequest error handling
int      g_consecutiveFailures = 0;
datetime g_lastSuccessTime = 0;
bool     g_connectionErrorReported = false;
// FIX 3: Order failure cooldown — prevents spam loop after repeated rejections
// (e.g. the 84-lot "not enough money" loop that fired every 60s)
int      g_orderFailCount[];  // consecutive order failures per symbol
datetime g_orderCooldownUntil[];  // don't retry this symbol until this time
// Risk capital cap from dashboard (Supabase bot_state.capital column).
// 0 = use full live equity. Updated on every signal response.
double   g_riskCapital = 0;
const int    ORDER_FAIL_THRESHOLD = 3;    // pause after this many consecutive failures
const int    ORDER_COOLDOWN_SECS  = 300;  // 5-minute cooldown
// Daily loss circuit breaker: track day start balance (reset at midnight GMT)
int      g_dayStartDate = 0;       // YYYYMMDD
double   g_dayStartBalance = 0;    // balance at start of current day
// Friday close: avoid closing multiple times
int      g_fridayCloseDoneDate = 0; // YYYYMMDD when we last ran Friday close
// Total DD circuit breaker: peak balance (updated in OnTimer); ref = max(INITIAL_CAPITAL_FOR_DD, g_peakBalance)
double   g_peakBalance = 0;

//+------------------------------------------------------------------+
//| ReportTransaction                                                |
//| Sends a deposit or withdrawal event to Modal /transaction        |
//|                                                                  |
//| Parameters:                                                      |
//|   type         — "deposit" or "withdrawal"                       |
//|   amount       — absolute value (always > 0)                     |
//|   balanceAfter — MT5 balance immediately after the deal          |
//|   ticket       — MT5 deal ticket (0 if unavailable)             |
//|   eventTime    — deal timestamp from MT5 history                 |
//+------------------------------------------------------------------+
void ReportTransaction(string   txnType,
                       double   amount,
                       double   balanceAfter,
                       ulong    ticket,
                       datetime eventTime)
{
   MqlDateTime mdt;
   TimeToStruct(eventTime, mdt);
   string eventTimeStr = StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ",
      mdt.year, mdt.mon, mdt.day, mdt.hour, mdt.min, mdt.sec);

   string json = StringFormat(
      "{\"type\":\"%s\","
       "\"amount\":%.2f,"
       "\"balance_after\":%.2f,"
       "\"ticket\":%I64u,"
       "\"event_time\":\"%s\"}",
      txnType,
      MathAbs(amount),      // always positive; type carries the direction
      balanceAfter,
      ticket,
      eventTimeStr
   );

   char   postData[], res[];
   string headers = "Content-Type: application/json\r\n";
   if (StringLen(API_TOKEN) > 0)
      headers += "X-APEXHYDRA-TOKEN: " + API_TOKEN + "\r\n";
   string resHeaders;
   StringToCharArray(json, postData, 0, StringLen(json));

   int code = WebRequest(
      "POST", MODAL_BASE_URL + "/transaction",
      headers, 5000, postData, res, resHeaders
   );

   if (code == 200)
      PrintFormat("[TRANSACTION] ✔ %s $%.2f | ticket=%I64u | balance_after=$%.2f",
                  txnType, MathAbs(amount), ticket, balanceAfter);
   else if (code == -1)
      PrintFormat("[TRANSACTION] ✘ WebRequest error %d — ensure %s is in Allowed URLs",
                  GetLastError(), MODAL_BASE_URL);
   else
      PrintFormat("[TRANSACTION] ✘ HTTP %d: %s", code, CharArrayToString(res));
}

//+------------------------------------------------------------------+
//| ScanDealHistoryForTransactions                                   |
//| Scans MT5 deal history for balance-type deals (deposits and      |
//| withdrawals). Called once in OnInit to catch any events that     |
//| occurred while the EA was offline or after a weekend restart.    |
//|                                                                  |
//| MT5 marks capital movements as:                                  |
//|   DEAL_TYPE == DEAL_TYPE_BALANCE  (integer value 2)             |
//|   positive profit → deposit                                      |
//|   negative profit → withdrawal                                   |
//+------------------------------------------------------------------+
void ScanDealHistoryForTransactions(datetime fromTime, datetime toTime)
{
   if (!HistorySelect(fromTime, toTime))
   {
      Print("[TRANSACTION] HistorySelect failed — skipping scan");
      return;
   }

   int total = HistoryDealsTotal();
   if (total == 0)
      return;

   Print("[TRANSACTION] Scan: checking ", total, " deals from ",
         TimeToString(fromTime, TIME_DATE | TIME_MINUTES),
         " to ", TimeToString(toTime, TIME_DATE | TIME_MINUTES));

   int found = 0;
   for (int i = 0; i < total; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if (ticket == 0) continue;

      // DEAL_TYPE_BALANCE covers deposits, withdrawals, and broker credit/corrections
      long dealType = HistoryDealGetInteger(ticket, DEAL_TYPE);
      if (dealType != DEAL_TYPE_BALANCE) continue;

      double   profit   = HistoryDealGetDouble(ticket,  DEAL_PROFIT);
      // DEAL_BALANCE is not a valid MQL5 property. The running balance after a
      // historical deal is not directly available in the deal record.
      // Best approximation for historical scans: use current live balance.
      // For the /transaction endpoint, balance_after is informational only —
      // the dashboard derives P&L from sum(deposits) not from balance_after.
      double   balAfter = AccountInfoDouble(ACCOUNT_BALANCE);
      datetime dealTime = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);

      if (profit == 0) continue;  // zero-amount broker correction — skip

      string txnType = (profit > 0) ? "deposit" : "withdrawal";

      PrintFormat("[TRANSACTION] Historical %s: $%.2f | ticket=%I64u | %s",
                  txnType, MathAbs(profit), ticket, TimeToString(dealTime));

      ReportTransaction(txnType, MathAbs(profit), balAfter, ticket, dealTime);
      found++;
   }

   if (found > 0)
      PrintFormat("[TRANSACTION] Scan complete: %d balance event(s) reported", found);
}

//+------------------------------------------------------------------+
int OnInit()
{
   g_symbolCount = StringSplit(SYMBOLS_CSV, ',', g_symbols);
   ArrayResize(g_lastScan, g_symbolCount);
   ArrayInitialize(g_lastScan, 0);
   // FIX 3: init order cooldown arrays
   ArrayResize(g_orderFailCount,     g_symbolCount);
   ArrayResize(g_orderCooldownUntil, g_symbolCount);
   ArrayInitialize(g_orderFailCount,     0);
   ArrayInitialize(g_orderCooldownUntil, 0);

   for (int i = 0; i < g_symbolCount; i++)
   {
      StringTrimLeft(g_symbols[i]);
      StringTrimRight(g_symbols[i]);
      if (!SymbolSelect(g_symbols[i], true))
         Print("Warning: cannot select ", g_symbols[i]);
      else
         Print("Symbol loaded: ", g_symbols[i]);
   }

   // BUG FIX: EventSetTimer() was called without checking its return value.
   // On rare VPS conditions (e.g. MT5 restart after a crash, or a resource-starved
   // server), EventSetTimer can fail silently — the EA loads and shows "running"
   // but OnTimer() never fires. No scans, no equity reports, no position management.
   // Without this check the bot would appear healthy while doing absolutely nothing.
   //
   // Fix: if EventSetTimer fails, return INIT_FAILED so MT5 shows an error banner
   // and the user is alerted rather than left believing the bot is working.
   if (!EventSetTimer(1))
   {
      Print("=== ApexHydraFTMO FATAL: EventSetTimer(1) failed — EA cannot start ===");
      Print("    Retry: remove and re-attach the EA, or restart MT5.");
      return INIT_FAILED;
   }

   // Scan last 30 days of deal history for any deposits/withdrawals that
   // occurred while the EA was offline (weekends, restarts, broker reconnects).
   datetime nowInit = TimeCurrent();
   g_lastDepositScan = nowInit - 30 * 86400;
   ScanDealHistoryForTransactions(g_lastDepositScan, nowInit);
   g_lastDepositScan = nowInit;

   g_peakBalance = AccountInfoDouble(ACCOUNT_BALANCE);

   Print("=== ApexHydraFTMO MultiScan started ===");
   Print("Symbols : ", SYMBOLS_CSV);
   Print("Endpoint: ", MODAL_BASE_URL);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); }

// Intentional: multi-symbol EA runs from OnTimer() (configurable interval). OnTick() would fire
// every tick on the chart symbol only; OnTimer() runs the same logic for all symbols in SYMBOLS_CSV.
void OnTick()
{
}

//+------------------------------------------------------------------+
void OnTimer()
{
   datetime now = TimeCurrent();

   //── Daily loss circuit breaker: reset day-start balance at midnight GMT ─
   {
      MqlDateTime dt;
      TimeToStruct(TimeGMT(), dt);
      int today = dt.year * 10000 + dt.mon * 100 + dt.day;
      if (today != g_dayStartDate)
      {
         g_dayStartDate = today;
         g_dayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
      }
   }

   //── Total DD circuit breaker: track peak balance (for local 10% guard when Modal is down)
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   if (bal > g_peakBalance)
      g_peakBalance = bal;

   //── Friday close (standard FTMO: no weekend hold) ────────────────────
   if (FRIDAY_CLOSE_STANDARD)
   {
      MqlDateTime dt;
      TimeToStruct(TimeGMT(), dt);
      int today = dt.year * 10000 + dt.mon * 100 + dt.day;
      if (dt.day_of_week == 5 && dt.hour >= FRIDAY_CLOSE_HOUR_GMT && g_fridayCloseDoneDate != today)
      {
         g_fridayCloseDoneDate = today;
         CloseAllPositionsFriday();
      }
   }

   //── Report real account balance to Modal ─────────────────────────
   if (now - g_lastEquity >= EQUITY_INTERVAL_S)
   {
      g_lastEquity = now;
      ReportEquity();
   }

   //── Poll for dashboard commands (close_all etc.) ────────────────
   static datetime lastCommandCheck = 0;
   if (now - lastCommandCheck >= 10)   // check every 10 seconds
   {
      lastCommandCheck = now;
      CheckCommands();
   }

   //── ATR trailing stop + partial close (every 10 seconds for responsive SL moves) ──
   static datetime lastManage = 0;
   if (now - lastManage >= 10)
   {
      lastManage = now;
      ManagePositions();
   }
   
   //── Connection health monitoring (every 60 seconds) ───────────────
   static datetime lastHealthCheck = 0;
   if (now - lastHealthCheck >= 60)
   {
      lastHealthCheck = now;
      CheckConnectionHealth();
   }

   //── Periodic transaction scan (every 5 min) — catch withdrawals that
   //   didn't fire OnTradeTransaction (e.g. broker delay, EA was restarted).
   //   Ensures dashboard never shows withdrawals as trading losses.
   static datetime lastTxnScan = 0;
   if (now - lastTxnScan >= 300)
   {
      datetime fromT = (lastTxnScan == 0) ? (now - 7 * 86400) : lastTxnScan;
      if (fromT < now)
         ScanDealHistoryForTransactions(fromT, now);
      lastTxnScan = now;
   }

   //── Scan each symbol ─────────────────────────────────────────────
   for (int i = 0; i < g_symbolCount; i++)
   {
      if (now - g_lastScan[i] >= SIGNAL_INTERVAL_S)
      {
         g_lastScan[i] = now;
         // FIX 3: skip symbol if it's in order-failure cooldown
         if (g_orderCooldownUntil[i] > now)
         {
            static datetime lastCooldownPrint = 0;
            if (now - lastCooldownPrint >= 60)
            {
               lastCooldownPrint = now;
               Print("[", g_symbols[i], "] Order cooldown active for ",
                     (int)(g_orderCooldownUntil[i] - now), "s — skipping scan");
            }
            continue;
         }
         ScanSymbol(g_symbols[i], i);
      }
   }
}

//+------------------------------------------------------------------+
//| CalculateDynamicLotSize                                          |
//| Calculates lot size based on 1% account equity risk              |
//| Parameters:                                                      |
//|   symbol    - trading symbol                                     |
//|   slDist    - stop loss distance in price units                  |
//|   riskPct   - risk percentage (default 1.0 = 1%)                 |
//| Returns:                                                         |
//|   Calculated lot size                                            |
//+------------------------------------------------------------------+
double CalculateDynamicLotSize(string symbol, double slDist, double riskPct = 1.0)
{
   // Get account information
   double accountEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   double accountBalance = AccountInfoDouble(ACCOUNT_BALANCE);
   
   // CAPITAL CAP: uses bot_state.capital from dashboard as sizing base.
   // g_riskCapital is updated from every /signal response.
   // 0 = use full live equity.  e.g. 200 = always size lots as if equity = $200.
   double usableEquity = accountEquity > 0 ? accountEquity : accountBalance;
   if (g_riskCapital > 0)
      usableEquity = MathMin(usableEquity, g_riskCapital);
   if (usableEquity <= 0)
   {
      Print("[", symbol, "] ERROR: Invalid account equity (", usableEquity, ") - using minimum lot");
      return SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   }
   
   // Calculate risk amount in currency
   double riskAmount = usableEquity * (riskPct / 100.0);
   
   // Get symbol information
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   // ── Pip value per lot in account currency ─────────────────────────────────
   // CORRECT METHOD: use MT5's native tick value, which MT5 has already
   // converted to account currency (including live cross-rate for JPY/XAU etc).
   //   tickValue = account-currency P&L for 1 tick move on 1 lot
   //   tickSize  = size of 1 tick in price units
   //   => value of 1 point = tickValue * (point / tickSize)
   //
   // WHY NOT contractSize*point: that gives a price-units number (e.g. 0.00001
   // for GBPUSD), not a money value. Dividing riskAmount by it produces absurd
   // lot sizes (84 lots on a $224 account).
   double tickValue = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   double pipValuePerLot = 0;
   if (tickSize > 0 && tickValue > 0)
      pipValuePerLot = tickValue * (point / tickSize);

   // Fallback guard — should never happen on a properly configured broker
   if (pipValuePerLot <= 0)
   {
      Print("[", symbol, "] WARNING: pipValuePerLot could not be determined — using minimum lot");
      return SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   }

   // FIX BUG#5: Previous formula divided riskAmount by (slDist × pipValuePerLot)
   // where slDist was in PRICE UNITS (e.g. 0.60 yen) and pipValuePerLot is in
   // $/point/lot. The unit mismatch produced lots 100–1000× too large.
   // Example USDJPY: $1 / (0.60 × $0.641) = 2.6 lots → capped at 1.0 lot.
   // Fix: convert slDist to POINTS first (÷ point), then the units are consistent.
   // Correct: $1 / (600 points × $0.641/point/lot) = 0.0026 lots → 0.01 lot ✓
   double slPoints = (point > 0) ? (slDist / point) : 0;
   double lotSize = 0;
   if (slPoints > 0)
   {
      lotSize = riskAmount / (slPoints * pipValuePerLot);
   }
   
   // Get symbol constraints
   double lotStep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   double lotMin = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double lotMax = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   if (lotStep <= 0)
      lotStep = MathMax(lotMin, 0.01);   // guard: avoid division by zero on misconfigured symbol

   // Validate and adjust lot size
   if (lotSize <= 0)
   {
      lotSize = lotMin;
      Print("[", symbol, "] WARNING: Calculated lot size invalid - using minimum: ", lotMin);
   }
   
   // Round to nearest lot step
   lotSize = MathRound(lotSize / lotStep) * lotStep;
   
   // ── Hard safety cap — FIXED: dollar-risk-based, not instrument-blind ────────
   //
   // THE BUG THIS FIXES:
   //   Old formula: maxLot = (equity x 0.05) / 100
   //   The "/ 100" assumed ~$10/pip (forex). For gold at $100/point it produced
   //   lots 10x too large. Example: equity=$323 -> maxLot=0.162 lots
   //   -> 0.162 x 15pts x $100/pt = $243 risk = 75% of account. Catastrophic.
   //
   // CORRECT APPROACH: cap is derived from actual dollar risk, not a fixed divisor.
   //   hardCapRisk = equity x HARD_RISK_PCT   (absolute ceiling, 2% per trade)
   //   hardCapLot  = hardCapRisk / (slDist x pipValuePerLot)
   //
   // This automatically scales correctly for every instrument: forex, gold, indices.
   // 2% hard cap = $6.46 max risk on a $323 account.
   // At gold $100/pt with 15pt SL: max = $6.46 / ($100 x 15) = 0.0043 lots -> min lot
   // At EURUSD $10/pt with 15pt SL: max = $6.46 / ($10 x 15) = 0.043 lots -> fine

   const double HARD_RISK_PCT = 2.0; // hard ceiling: never risk more than 2% per trade
   double hardCapRisk = usableEquity * (HARD_RISK_PCT / 100.0);
   double hardCap     = lotMin; // fallback
   if (slPoints > 0 && pipValuePerLot > 0)
   {
      hardCap = hardCapRisk / (slPoints * pipValuePerLot);
      hardCap = MathMax(0, MathMin(hardCap, 1.0)); // do NOT force to lotMin — allows skip when min lot would exceed 2%
   }

   if (lotSize > hardCap)
   {
      double capRiskPct = (hardCap * slPoints * pipValuePerLot / usableEquity) * 100;
      Print("[", symbol, "] WARNING LOT CAP: calculated=", DoubleToString(lotSize, 4),
            " -> capped to ", DoubleToString(hardCap, 4),
            " (", DoubleToString(capRiskPct, 2), "% risk, equity=$",
            DoubleToString(usableEquity, 2), ")");
      lotSize = hardCap;
   }

   // ── PRECIOUS METALS ABSOLUTE LOT CAP ────────────────────────────────────────
   // Independent safety net that doesn't rely on pipValuePerLot (can be wrong on
   // some brokers, especially for exotic contract sizes).
   //
   // GOLD (XAUUSD): contract = 100 oz.  Pip value ≈ $1/lot/pip.
   //   Rule: 0.01 lots per $100 equity.  $880 → max 0.08 lots.
   //   Ceiling: 0.10 lots (until account > $1,000).
   //
   // SILVER (XAGUSD): contract = 5,000 oz on most MT5 brokers (vs gold's 100 oz).
   //   Pip value ≈ $5/lot/pip — 5× larger than gold.
   //   Rule: 0.002 lots per $100 equity (5× more conservative than gold).
   //   $880 → max 0.01 lots.  Ceiling: 0.02 lots (until account > $1,000).
   //   Even at 0.01 lots with a 400-pip SL: 0.01 × $5 × 400 = $20 = 2.3% risk.
   //   The dynamic sizer (0.5% target) will further reduce this — cap is backstop.
   //
   // Why separate caps? Silver's 5,000-oz contract size means the same lot count
   // puts 5× more dollars at risk than gold.  Treating them identically would
   // allow 5× the intended exposure on silver.
   bool isGoldSymbol   = (StringFind(symbol, "XAU") >= 0 ||
                          StringFind(symbol, "GOLD") >= 0 ||
                          symbol == GOLD_SYMBOL);
   bool isSilverSymbol = (StringFind(symbol, "XAG") >= 0);

   if (isGoldSymbol)
   {
      double lotStep2  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
      double goldCap   = MathMax(lotMin,
                            MathFloor(usableEquity / 100.0) *
                            MathMax(lotStep2, 0.01));
      goldCap = MathRound(goldCap / lotStep2) * lotStep2;
      goldCap = MathMin(goldCap, XAUUSD_LOT); // ceiling from input (e.g. 0.10 for metals)
      if (lotSize > goldCap)
      {
         Print("[", symbol, "] GOLD LOT CAP: ", DoubleToString(lotSize, 4),
               " -> ", DoubleToString(goldCap, 4),
               " (0.01/lot per $100 rule, equity=$", DoubleToString(usableEquity, 2), ")");
         lotSize = goldCap;
      }
   }
   else if (isSilverSymbol)
   {
      // Silver: 0.002 lots per $100 equity (5,000-oz contract = 5× gold pip value)
      double lotStep2   = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
      double silverCap  = MathMax(lotMin,
                             MathFloor(usableEquity / 100.0) *
                             MathMax(lotStep2 * 0.2, 0.002));   // 0.002 per $100
      silverCap = MathRound(silverCap / lotStep2) * lotStep2;
      silverCap = MathMin(silverCap, XAGUSD_LOT);  // ceiling from input (e.g. 0.02)
      if (lotSize > silverCap)
      {
         Print("[", symbol, "] SILVER LOT CAP: ", DoubleToString(lotSize, 4),
               " -> ", DoubleToString(silverCap, 4),
               " (0.002/lot per $100 rule, equity=$", DoubleToString(usableEquity, 2), ")");
         lotSize = silverCap;
      }
   }

   // Ensure within broker limits (no skip: allow min lot even if it exceeds 2% risk)
   lotSize = MathMax(lotMin, MathMin(lotMax, lotSize));
   
   // Log the calculation for transparency
   double actualRisk = lotSize * slPoints * pipValuePerLot;
   double riskPercentage = (actualRisk / usableEquity) * 100;
   
   Print("[", symbol, "] Dynamic Lot Calc: Equity=$", DoubleToString(usableEquity, 2),
         " | Risk=$", DoubleToString(actualRisk, 2), 
         " (", DoubleToString(riskPercentage, 2), "%)",
         " | SL=", DoubleToString(slDist / point, 1), " pts",
         " | pipVal=$", DoubleToString(pipValuePerLot, 4),
         " | Lot=", DoubleToString(lotSize, 4));
   
   return lotSize;
}

//+------------------------------------------------------------------+
//| CheckConnectionHealth                                            |
//| Monitors connection status and triggers fail-safe actions        |
//+------------------------------------------------------------------+
void CheckConnectionHealth()
{
   // Check if we've had connection failures for more than 15 minutes
   if (g_consecutiveFailures >= 3)
   {
      datetime now = TimeCurrent();
      if (g_lastSuccessTime == 0)
         g_lastSuccessTime = now - 1800; // Initialize to 30 minutes ago if never set
      
      int secondsSinceSuccess = (int)(now - g_lastSuccessTime);
      int minutesSinceSuccess = secondsSinceSuccess / 60;
      
      // Alert every 15 minutes of continued failure
      if (minutesSinceSuccess >= 15 && minutesSinceSuccess % 15 == 0)
      {
         SendLog("ERROR", "SYSTEM", 
                 StringFormat("AI SERVER UNREACHABLE for %d minutes - emergency protocols activated", 
                             minutesSinceSuccess));
         
         // Implement fail-safe actions
         ExecuteConnectionFailSafe(minutesSinceSuccess);
      }
   }
}

//+------------------------------------------------------------------+
//| ExecuteConnectionFailSafe                                        |
//| Executes emergency actions when AI server is unreachable         |
//+------------------------------------------------------------------+
void ExecuteConnectionFailSafe(int minutesUnreachable)
{
   // After 15 minutes: Move all stop losses to breakeven
   if (minutesUnreachable >= 15 && minutesUnreachable < 30)
   {
      MoveAllStopsToBreakeven();
      SendLog("WARN", "SYSTEM", "Fail-safe: All stop losses moved to breakeven");
   }
   // After 30 minutes: Close all positions
   else if (minutesUnreachable >= 30)
   {
      CloseAllPositionsEmergency();
      SendLog("ERROR", "SYSTEM", "Fail-safe: Emergency close all positions executed");
   }
}

//+------------------------------------------------------------------+
//| MoveAllStopsToBreakeven                                          |
//| Moves all stop losses to breakeven level                         |
//+------------------------------------------------------------------+
void MoveAllStopsToBreakeven()
{
   int movedCount = 0;
   
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (ticket == 0) continue;
      if (PositionGetString(POSITION_COMMENT) != "ApexHydraFTMO") continue;
      
      string sym = PositionGetString(POSITION_SYMBOL);
      double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      double currentSL = PositionGetDouble(POSITION_SL);
      double point = SymbolInfoDouble(sym, SYMBOL_POINT);
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      
      ENUM_POSITION_TYPE posType = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      
      double newSL = 0;
      if (posType == POSITION_TYPE_BUY)
      {
         // For BUY positions, breakeven is entry price + 1 pip
         newSL = NormalizeDouble(openPrice + point, digits);
         if (currentSL < newSL) // Only move if current SL is worse than breakeven
         {
            if (trade.PositionModify(ticket, newSL, 0))
            {
               Print("[", sym, "] SL moved to breakeven: ", DoubleToString(newSL, digits));
               movedCount++;
            }
         }
      }
      else // SELL position
      {
         // For SELL positions, breakeven is entry price - 1 pip
         newSL = NormalizeDouble(openPrice - point, digits);
         if (currentSL > newSL) // Only move if current SL is worse than breakeven
         {
            if (trade.PositionModify(ticket, newSL, 0))
            {
               Print("[", sym, "] SL moved to breakeven: ", DoubleToString(newSL, digits));
               movedCount++;
            }
         }
      }
   }
   
   if (movedCount > 0)
   {
      SendLog("INFO", "SYSTEM", StringFormat("Moved %d stop losses to breakeven", movedCount));
   }
}

//+------------------------------------------------------------------+
//| CloseAllPositionsEmergency                                       |
//| Emergency close of all positions when server is unreachable      |
//+------------------------------------------------------------------+
void CloseAllPositionsEmergency()
{
   int closedCount = 0;
   
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (ticket == 0) continue;
      if (PositionGetString(POSITION_COMMENT) != "ApexHydraFTMO") continue;
      
      string sym = PositionGetString(POSITION_SYMBOL);
      
      if (trade.PositionClose(ticket))
      {
         Print("[EMERGENCY] Closed position ", ticket, " (", sym, ")");
         closedCount++;
      }
      else
      {
         Print("[EMERGENCY] Failed to close ", ticket, ": ", trade.ResultRetcodeDescription());
      }
   }
   
   if (closedCount > 0)
   {
      SendLog("ERROR", "SYSTEM", StringFormat("Emergency close: %d positions closed", closedCount));
      ReportEquity(); // Update dashboard with new balance
   }
}

//+------------------------------------------------------------------+
//| CloseAllPositionsFriday — standard FTMO: close before weekend     |
//| Only ApexHydraFTMO positions; run once per Friday.                |
//+------------------------------------------------------------------+
void CloseAllPositionsFriday()
{
   int closed = 0;
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (ticket == 0) continue;
      if (PositionGetString(POSITION_COMMENT) != "ApexHydraFTMO") continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      if (trade.PositionClose(ticket))
      {
         Print("[FRIDAY CLOSE] Closed ", ticket, " (", sym, ")");
         closed++;
      }
      else
         Print("[FRIDAY CLOSE] Failed to close ", ticket, ": ", trade.ResultRetcodeDescription());
   }
   if (closed > 0)
   {
      Print("=== Friday close: ", closed, " positions closed (no weekend hold) ===");
      ReportEquity();
   }
}

//+------------------------------------------------------------------+
//| SendLog — sends a log line to Modal /log endpoint.              |
// Called after key events: trade open, close, errors, skips.      |
// Dashboard reads these via GET /logs — downloadable as CSV.      |
//+------------------------------------------------------------------+
void SendLog(string level, string symbol, string message)
{
   MqlDateTime mdt;
   TimeToStruct(TimeGMT(), mdt);
   string eaTimeStr = StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ",
      mdt.year, mdt.mon, mdt.day, mdt.hour, mdt.min, mdt.sec);

   // Escape double quotes in message to keep JSON valid
   StringReplace(message, "\"", "'");

   string json = StringFormat(
      "{\"level\":\"%s\",\"symbol\":\"%s\","
       "\"message\":\"%s\","
       "\"ea_time\":\"%s\"}",
      level, symbol, message, eaTimeStr
   );

   char   postData[], res[];
   string headers = "Content-Type: application/json\r\n";
   if (StringLen(API_TOKEN) > 0)
      headers += "X-APEXHYDRA-TOKEN: " + API_TOKEN + "\r\n";
   string resHeaders;
   StringToCharArray(json, postData, 0, StringLen(json));

   // Fire-and-forget: don't retry, don't block main loop on failure
   WebRequest("POST", MODAL_BASE_URL + "/log",
              headers, 3000, postData, res, resHeaders);
}

//+------------------------------------------------------------------+
// ReportOpen — called after a successful Buy/Sell to confirm the   |
// trade to Modal /open.  This is Phase 2 of the open/confirm       |
// handshake:  /signal logs a "pending" row (lot=NULL);             |
//             /open sets the real lot, making it a confirmed trade. |
// Without this call the compliance engine can't count the position  |
// toward the concurrent limit and the server would keep resending   |
// the same signal every 60 seconds, spamming phantom rows.         |
//+------------------------------------------------------------------+
void ReportOpen(string sym, string direction, double lot, ulong ticket)
{
   string json = StringFormat(
      "{\"symbol\":\"%s\",\"direction\":\"%s\","
       "\"lot\":%.2f,\"ticket\":%I64u}",
      sym, direction, lot, ticket
   );

   char   postData[], res[];
   string headers = "Content-Type: application/json\r\n";
   if (StringLen(API_TOKEN) > 0)
      headers += "X-APEXHYDRA-TOKEN: " + API_TOKEN + "\r\n";
   string resHeaders;
   StringToCharArray(json, postData, 0, StringLen(json));

   int code = WebRequest(
      "POST", MODAL_BASE_URL + "/open",
      headers, 5000, postData, res, resHeaders
   );

   if (code == 200)
      PrintFormat("[%s] ✔ /open confirmed | %s %.2f lots | ticket=%I64u",
                  sym, direction, lot, ticket);
   else if (code == -1)
      PrintFormat("[%s] ✘ /open WebRequest error %d", sym, GetLastError());
   else
      PrintFormat("[%s] ✘ /open HTTP %d: %s", sym, code, CharArrayToString(res));
}

//+------------------------------------------------------------------+
// Sends real MT5 account balance/equity to Modal /equity endpoint.
// This is what makes the dashboard P&L update in real time.
//+------------------------------------------------------------------+
void ReportEquity()
{
   double balance    = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity     = AccountInfoDouble(ACCOUNT_EQUITY);
   double margin     = AccountInfoDouble(ACCOUNT_MARGIN);


   string json = StringFormat(
      "{\"balance\":%.2f,\"equity\":%.2f,\"margin\":%.2f}",
      balance, equity, margin
   );

   char   postData[], res[];
   string headers = "Content-Type: application/json\r\n";
   if (StringLen(API_TOKEN) > 0)
      headers += "X-APEXHYDRA-TOKEN: " + API_TOKEN + "\r\n";
   string resHeaders;
   StringToCharArray(json, postData, 0, StringLen(json));

   int code = WebRequest(
      "POST", MODAL_BASE_URL + "/equity",
      headers, 5000, postData, res, resHeaders
   );

   if (code == 200)
      PrintFormat("Equity reported: balance=%.2f equity=%.2f", balance, equity);
   else if (code == -1)
      Print("Equity report WebRequest error: ", GetLastError());
   else
      Print("Equity report failed (", code, "): ", CharArrayToString(res));
}

//+------------------------------------------------------------------+
//| IsInLocalNewsBlackout — backup when Modal is down                  |
//| NFP: first Friday of month 12:00–14:00 GMT                         |
//| CPI: 2nd Tuesday of month (day 8–14) 12:00–14:00 GMT only        |
//| FOMC: 1st & 3rd Wednesday of month 18:00–20:00 GMT only (~8/yr)   |
//+------------------------------------------------------------------+
bool IsInLocalNewsBlackout()
{
   MqlDateTime dt;
   TimeToStruct(TimeGMT(), dt);
   // NFP: first Friday of month (day 1–7), 12:00–13:59 GMT
   if (dt.day_of_week == 5 && dt.day <= 7 && dt.hour >= 12 && dt.hour < 14)
      return true;
   // CPI: 2nd Tuesday of month only (day 8–14), 12:00–13:59 GMT (US CPI ~13:30 GMT)
   if (dt.day_of_week == 2 && dt.day >= 8 && dt.day <= 14 && dt.hour >= 12 && dt.hour < 14)
      return true;
   // FOMC: only 1st week (day 1–7) and 3rd week (day 15–21) Wednesday 18:00–19:59 GMT
   if (dt.day_of_week == 3 && (dt.day <= 7 || (dt.day >= 15 && dt.day <= 21))
       && dt.hour >= 18 && dt.hour < 20)
      return true;
   return false;
}

//+------------------------------------------------------------------+
void ScanSymbol(string sym, int symIdx = -1)
{
   double closes[], highs[], lows[];
   long   vols[];
   ArraySetAsSeries(closes, true);
   ArraySetAsSeries(highs,  true);
   ArraySetAsSeries(lows,   true);
   ArraySetAsSeries(vols,   true);

   int copied = CopyClose(sym, TF, 0, BARS_TO_SEND, closes);
   if (copied < 26) { Print("[", sym, "] Insufficient bars (", copied, ")"); return; }
   CopyHigh(sym,       TF, 0, copied, highs);
   CopyLow(sym,        TF, 0, copied, lows);
   CopyTickVolume(sym, TF, 0, copied, vols);

   string cArr="[", hArr="[", lArr="[", vArr="[";
   for (int i = copied - 1; i >= 0; i--)
   {
      cArr += DoubleToString(closes[i], 5);
      hArr += DoubleToString(highs[i],  5);
      lArr += DoubleToString(lows[i],   5);
      vArr += IntegerToString(vols[i]);
      if (i > 0) { cArr+=","; hArr+=","; lArr+=","; vArr+=","; }
   }
   cArr+="]"; hArr+="]"; lArr+="]"; vArr+="]";

   MqlTick tick;
   if (!SymbolInfoTick(sym, tick)) { Print("[", sym, "] No tick"); return; }

   // Local news blackout (backup when server is unreachable)
   if (USE_LOCAL_NEWS_BLACKOUT && IsInLocalNewsBlackout())
   {
      static datetime lastNewsPrint = 0;
      if (TimeCurrent() - lastNewsPrint >= 300)
      {
         lastNewsPrint = TimeCurrent();
         Print("[", sym, "] Local news blackout (NFP/FOMC window) — no new trades");
      }
      return;
   }

   // Daily loss circuit breaker: no new trades if daily loss >= 4% (1% buffer before FTMO 5%)
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if (g_dayStartBalance > 0 && LOCAL_DAILY_LOSS_PCT > 0)
   {
      double dailyLossPct = (g_dayStartBalance - equity) / g_dayStartBalance * 100.0;
      if (dailyLossPct >= LOCAL_DAILY_LOSS_PCT)
      {
         static datetime lastDdPrint = 0;
         if (TimeCurrent() - lastDdPrint >= 60)
         {
            lastDdPrint = TimeCurrent();
            Print("[", sym, "] Local daily loss circuit breaker: ", DoubleToString(dailyLossPct, 2),
                  "% >= ", DoubleToString(LOCAL_DAILY_LOSS_PCT, 1), "% — no new trades");
         }
         return;
      }
   }

   // Total DD circuit breaker: no new trades if total DD >= 9.5% (buffer before FTMO 10%) when Modal is down
   if (INITIAL_CAPITAL_FOR_DD > 0 && LOCAL_TOTAL_DD_PCT > 0 && g_peakBalance > 0)
   {
      double refPeak = MathMax(INITIAL_CAPITAL_FOR_DD, g_peakBalance);
      if (refPeak > 0 && equity < refPeak)
      {
         double totalDdPct = (refPeak - equity) / refPeak * 100.0;
         if (totalDdPct >= LOCAL_TOTAL_DD_PCT)
         {
            static datetime lastTotalDdPrint = 0;
            if (TimeCurrent() - lastTotalDdPrint >= 60)
            {
               lastTotalDdPrint = TimeCurrent();
               Print("[", sym, "] Local total DD circuit breaker: ", DoubleToString(totalDdPct, 2),
                     "% >= ", DoubleToString(LOCAL_TOTAL_DD_PCT, 1), "% — no new trades");
            }
            return;
         }
      }
   }

   // PROFITABLE FIX #2: Spread pre-filter — blocks signal request when spread
   // is too wide relative to volatility. A 3-pip spread on an 8-pip ATR move
   // means you start 37% of your SL in the hole. No signal survives that cost.
   // Check happens before the HTTP call to save latency and server load.
   //
   // Spread caps (pips) — set at ~2× typical ECN spread for each pair:
   //   EURUSD 2.5  GBPUSD 2.5  USDCHF 2.5  AUDUSD 2.5
   //   USDCAD 3.0  NZDUSD 3.0  EURGBP 1.5  (tightest cross)
   //   USDJPY 3.0  EURJPY 3.0  GBPJPY 7.0  (elevated spread pair)
   //   XAUUSD 35.0 (gold — wide at session transitions, $1 = 10 pips)
   {
      // SPREAD FIX: divide by (point * 10) for all symbols.
      // Correct: pip = point * 10 for forex AND gold (XAUUSD point=0.01, pip=0.10).
      double spreadPips = (tick.ask - tick.bid) /
                          (SymbolInfoDouble(sym, SYMBOL_POINT) * 10.0);
      // Per-symbol max spread caps:
      double maxSpread = 2.5;   // default: EURUSD, GBPUSD, USDCHF, AUDUSD
      if (StringFind(sym, "GBP") >= 0 && StringFind(sym, "JPY") >= 0) maxSpread = 7.0;
      else if (StringFind(sym, "JPY") >= 0)                            maxSpread = 3.0;   // USDJPY, EURJPY
      else if (StringFind(sym, "EURGBP") >= 0)                         maxSpread = 1.5;   // tightest cross
      else if (StringFind(sym, "NZD") >= 0 || StringFind(sym, "CAD") >= 0) maxSpread = 3.0; // NZDUSD, USDCAD
      if (StringFind(sym, "XAG") >= 0)                                 maxSpread = 40.0;  // silver (before XAU check)
      if (StringFind(sym, "XAU") >= 0 || sym == GOLD_SYMBOL)           maxSpread = 35.0;  // gold (overrides all)
      if (spreadPips >= maxSpread)
      {
         Print("[", sym, "] Spread ", DoubleToString(spreadPips, 1),
               " pips >= max ", DoubleToString(maxSpread, 1),
               " — skipping (spread too wide)");
         return;
      }
   }

   MqlDateTime dt;
   TimeToStruct(TimeGMT(), dt);
   int dow = dt.day_of_week == 0 ? 4 : dt.day_of_week - 1;

   double pt     = SymbolInfoDouble(sym, SYMBOL_POINT);
   double spread = pt > 0 ? (tick.ask - tick.bid) / pt / 10.0 : 0;

   string json = StringFormat(
      "{\"symbol\":\"%s\","
      "\"bid\":%.5f,\"ask\":%.5f,"
      "\"close\":%s,\"high\":%s,\"low\":%s,\"volume\":%s,"
      "\"hour\":%d,\"dow\":%d,\"spread\":%.1f}",
      sym, tick.bid, tick.ask,
      cArr, hArr, lArr, vArr,
      dt.hour, dow, spread
   );

   char   postData[], res[];
   string headers = "Content-Type: application/json\r\n";
   if (StringLen(API_TOKEN) > 0)
      headers += "X-APEXHYDRA-TOKEN: " + API_TOKEN + "\r\n";
   string resHeaders;
   StringToCharArray(json, postData, 0, StringLen(json));

   // ── WebRequest with retry logic and improved error handling ──────────────
   int code = 0;
   int retryCount = 0;
   const int MAX_RETRIES = 3;
   const int TIMEOUT_MS = 2000;  // Reduced from 8000ms to 2000ms
   
   while (retryCount < MAX_RETRIES)
   {
      code = WebRequest(
         "POST", MODAL_BASE_URL + "/signal",
         headers, TIMEOUT_MS, postData, res, resHeaders
      );
      
      if (code != -1 && code == 200)
      {
         // Success - reset failure counter
         g_consecutiveFailures = 0;
         g_lastSuccessTime = TimeCurrent();
         g_connectionErrorReported = false;
         break;
      }
      
      retryCount++;
      
      if (retryCount < MAX_RETRIES)
      {
         // FIX BUG#4: Removed Sleep(1000). MQL5 Sleep() blocks the ENTIRE MT5
         // event loop — with 5 symbols × 3 retries × 1s sleep the timer was
         // stalled for 45+ seconds during server outages, halting position
         // management and equity reporting. The 2s WebRequest timeout is
         // sufficient backpressure; no sleep needed between retries.
         Print("[", sym, "] WebRequest attempt ", retryCount, " failed (code=", code,
               ", error=", GetLastError(), ") - retrying...");
      }
   }
   
   // Handle final failure after all retries
   if (code == -1 || code != 200)
   {
      g_consecutiveFailures++;
      
      if (code == -1)
      {
         int errorCode = GetLastError();
         Print("[", sym, "] ✘ WebRequest FATAL error ", errorCode,
               " — add '", MODAL_BASE_URL, "' to Tools→Options→Expert Advisors");
         SendLog("ERROR", sym, StringFormat("WebRequest FATAL error %d - check URL in EA settings", errorCode));
      }
      else
      {
         string resp = CharArrayToString(res, 0, ArraySize(res));
         Print("[", sym, "] ✘ HTTP ", code, ": ", resp);
         SendLog("ERROR", sym, StringFormat("HTTP %d: %s", code, resp));
      }
      
      // Report connection issues after 3 consecutive failures
      if (g_consecutiveFailures >= 3 && !g_connectionErrorReported)
      {
         SendLog("ERROR", "SYSTEM", 
                 StringFormat("Connection failed %d times consecutively - check server connectivity", 
                             g_consecutiveFailures));
         g_connectionErrorReported = true;
      }
      
      return;
   }
      
   // Parse successful response
   string resp = CharArrayToString(res, 0, ArraySize(res));
   string action   = ExtractStr(resp, "action");
   double conf     = ExtractDbl(resp, "confidence");
   string strategy = ExtractStr(resp, "strategy");
   string regime   = ExtractStr(resp, "regime");
   string reason   = ExtractStr(resp, "reason");
   double lotScale    = ExtractDbl(resp, "lot_scale");
   if (lotScale <= 0) lotScale = 1.0;

   // Update risk capital cap ONLY if the response explicitly contains the key.
   // Early-return responses (spread_too_wide, session_blocked etc.) omit this
   // field entirely — ExtractDbl returns 0 for missing keys, which would wrongly
   // reset a $300 cap back to $0. Only update when key is present in the JSON.
   if (StringFind(resp, "\"risk_capital\"") >= 0)
   {
      double newRiskCap = ExtractDbl(resp, "risk_capital");
      if (newRiskCap != g_riskCapital)
      {
         Print("[", sym, "] Risk capital updated: $", DoubleToString(g_riskCapital, 0),
               " -> $", DoubleToString(newRiskCap, 0),
               newRiskCap == 0 ? " (full equity mode)" : " (dashboard cap active)");
         g_riskCapital = newRiskCap;
      }
   }

   Print("[", sym, "] ", action,
         " conf=",     DoubleToString(conf, 3),
         " strategy=", strategy,
         " regime=",   regime,
         " reason=",   reason);

   if (action == "NONE")
   {
      // Log interesting skip reasons (not routine session/dead-zone blocks which fire every minute)
      bool isRoutine = (StringFind(reason, "off_peak") >= 0 ||
                        StringFind(reason, "dead_zone") >= 0 ||
                        StringFind(reason, "weekend") >= 0 ||
                        StringFind(reason, "bot_stopped") >= 0);
      if (!isRoutine && StringLen(reason) > 0)
         SendLog("INFO", sym, StringFormat("SKIPPED | reason=%s | conf=%.3f | %s/%s",
                                            reason, conf, strategy, regime));
      return;
   }

   if (CLOSE_ON_REVERSE)
   {
      // CLOSE_ON_REVERSE FIX: only close opposite position if it is currently
      // at a LOSS or flat. Never close a profitable position to reverse direction —
      // that was causing the user to manually intervene to protect gains.
      // Profitable positions are left to run to their TP or trailing stop.
      if (action == "BUY" && CountPositions(sym, POSITION_TYPE_SELL) > 0)
      {
         for (int i = PositionsTotal()-1; i >= 0; i--)
         {
            ulong tk = PositionGetTicket(i);
            if (tk == 0) continue;
            if (PositionGetString(POSITION_SYMBOL) != sym) continue;
            if ((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) != POSITION_TYPE_SELL) continue;
            if (PositionGetString(POSITION_COMMENT) != "ApexHydraFTMO") continue;
            double pnl = PositionGetDouble(POSITION_PROFIT);
            if (pnl <= 0) // only close if losing or flat
               trade.PositionClose(tk);
            else
               Print("[", sym, "] CLOSE_ON_REVERSE skipped: SELL is in profit ($",
                     DoubleToString(pnl,2), ") — letting it run to TP");
         }
      }
      if (action == "SELL" && CountPositions(sym, POSITION_TYPE_BUY) > 0)
      {
         for (int i = PositionsTotal()-1; i >= 0; i--)
         {
            ulong tk = PositionGetTicket(i);
            if (tk == 0) continue;
            if (PositionGetString(POSITION_SYMBOL) != sym) continue;
            if ((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) != POSITION_TYPE_BUY) continue;
            if (PositionGetString(POSITION_COMMENT) != "ApexHydraFTMO") continue;
            double pnl = PositionGetDouble(POSITION_PROFIT);
            if (pnl <= 0)
               trade.PositionClose(tk);
            else
               Print("[", sym, "] CLOSE_ON_REVERSE skipped: BUY is in profit ($",
                     DoubleToString(pnl,2), ") — letting it run to TP");
         }
      }
   }

   ENUM_POSITION_TYPE side = (action=="BUY") ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
   if (CountPositions(sym, side) >= MAX_POSITIONS_SYM)
   {
      Print("[", sym, "] Max positions reached — skipping");
      SendLog("INFO", sym, "Max positions reached — signal skipped (already in trade)");
      return;
   }

   //── Calculate SL/TP using ATR (1:2 risk/reward)──────────────────
   double point   = SymbolInfoDouble(sym, SYMBOL_POINT);
   double ask     = SymbolInfoDouble(sym, SYMBOL_ASK);
   double bid     = SymbolInfoDouble(sym, SYMBOL_BID);
   int    digits  = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   // Calculate pip size based on symbol type
   // MT5 symbols never contain "/" — isJpyCross was always false for USDJPY,
   // causing pipSize = point (0.001) instead of point*10 (0.01).
   // Result: ATR read as 1000+ pips instead of 100, SL placed 15 yen away.
   bool isJpyCross = (StringFind(sym, "JPY") >= 0);  // FIXED: removed "/" check
   // isMetals: covers XAUUSD (gold) and XAGUSD (silver).
   // Both use point-based pip (not point*10) — for XAUUSD point=0.01, pip=0.01;
   // for XAGUSD point=0.001, pip=0.001. Using point*10 would inflate pip counts 10×.
   bool isMetals   = (StringFind(sym, "XAU") >= 0 || StringFind(sym, "XAG") >= 0 ||
                      StringFind(sym, "GOLD") >= 0);
   bool isGold     = (StringFind(sym, "XAU") >= 0 || StringFind(sym, "GOLD") >= 0); // kept for log display
   double pipSize  = isMetals ? point : point * 10;
      
   // Get ATR value for current symbol.
   // CRITICAL FIX: iATR() returns a handle (integer ~5-20), NOT the ATR price value.
   // Must use CopyBuffer to extract the actual value. Previously atrVal was set to
   // the handle number (e.g. 10), giving ATR=10000 pips for USDJPY and SL 15 yen away.
   double atrVal = 0;
   {
      int    _atrHandle = iATR(sym, TF, ATR_PERIOD);
      double _atrBuf[];
      ArraySetAsSeries(_atrBuf, true);
      // BUG FIX: previous code only called IndicatorRelease() inside the
      // "success" branch of the CopyBuffer check.  If CopyBuffer failed for
      // any reason (e.g. history not yet loaded, invalid handle), the handle
      // leaked permanently.  With 4 symbols scanning every 60 seconds that is
      // 4 leaked handles per minute during any failure period.  MT5 caps its
      // indicator handle pool — after enough leaks ATR silently stops returning
      // values for ALL symbols and every new trade falls back to fixed-pip
      // SL/TP, destroying the dynamic R:R system.  The ManagePositions()
      // function had this same bug and was fixed earlier (BUG#3 in that
      // section); this is the identical fix applied to ScanSymbol().
      //
      // Fix: call IndicatorRelease() unconditionally — success or failure.
      if (_atrHandle != INVALID_HANDLE)
      {
         if (CopyBuffer(_atrHandle, 0, 0, 1, _atrBuf) > 0)
            atrVal = _atrBuf[0];
         IndicatorRelease(_atrHandle);   // ALWAYS release — prevents handle pool exhaustion
      }
   }
   if (atrVal <= 0)
   {
      // Fallback to fixed pips if ATR unavailable
      int slPips = SL_PIPS_FALLBACK;
      if (StringFind(sym, "JPY") >= 0)
         slPips = SL_PIPS_JPY_CROSS;
      else if (StringFind(sym, "XAU") >= 0 || StringFind(sym, "GOLD") >= 0)
         slPips = SL_PIPS_GOLD;
      atrVal = slPips * pipSize;
      Print("[", sym, "] ATR unavailable — using fallback ", slPips, " pips");
   }
      
   // Determine SL/TP distances
   double slDist, tpDist;
      
   if (atrVal > 0)
   {
      // ATR-based: adapts to current volatility automatically
      slDist = ATR_SL_MULT * atrVal;
      tpDist = ATR_TP_MULT * atrVal;
      // FIX: Gold display bug — broker may quote XAUUSD to 3dp (point=0.001),
      // so atrVal/pipSize inflated the display 10x (e.g. ATR=17382 instead of 1738).
      // Gold ATR is most readable in raw price points (e.g. "17.4 pts") since
      // gold "pips" are ambiguous across brokers. Non-gold keeps the pip display.
      if (isGold)
         Print("[", sym, "] ATR=", DoubleToString(atrVal, 2),
               " pts | SL=", DoubleToString(slDist, 2),
               " TP=", DoubleToString(tpDist, 2));
      else
         Print("[", sym, "] ATR=", DoubleToString(atrVal / pipSize, 1),
               " pips | SL=", DoubleToString(slDist / pipSize, 1),
               " TP=", DoubleToString(tpDist / pipSize, 1));
   }
   else
   {
      // Fallback to fixed pips
      // FIXED: removed "/" check — MT5 symbols never contain "/"
      bool isJpyFallback = (StringFind(sym, "JPY") >= 0);
      bool isGoldFallback = (StringFind(sym, "XAU") >= 0 || StringFind(sym, "GOLD") >= 0);
      int slPips = isGoldFallback ? SL_PIPS_GOLD : (isJpyFallback ? SL_PIPS_JPY_CROSS : SL_PIPS_FALLBACK);
      int tpPips = isGoldFallback ? TP_PIPS_GOLD : (isJpyFallback ? TP_PIPS_JPY_CROSS : TP_PIPS_FALLBACK);
      slDist = slPips * pipSize;
      tpDist = tpPips * pipSize;
      Print("[", sym, "] ATR unavailable — using fixed pips SL=", slPips, " TP=", tpPips);
   }
      
   //── Dynamic Lot Sizing: 0.5% account equity risk management ────────────────
   // PROFITABLE FIX #11: Reduced from 1.0% → 0.5% risk per trade.
   // With 3 concurrent positions allowed, max simultaneous risk is now 1.5%
   // instead of 3%. Raise back to 1.0% after 100+ trades confirm positive expectancy.
   double dynamicLot = CalculateDynamicLotSize(sym, slDist, 0.5); // 0.5% risk
   double lot = (dynamicLot > 0) ? dynamicLot : SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   lot *= lotScale; // Apply any scaling from AI signal
   
   // Validate lot size against symbol constraints
   double lotStep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double lotMin = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double lotMax = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   lot = MathMax(lotMin, MathMin(lotMax, MathRound(lot / lotStep) * lotStep));
      
   // Enforce broker minimum stops level so SL/TP are not stripped (critical for gold/XAUUSD).
   // If distance is below broker minimum, position can open with no SL/TP on chart.
   long stopsLevel = (long)SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   double minDistPrice = (stopsLevel > 0) ? (stopsLevel * point) : (10 * pipSize);
   if (isMetals)
      minDistPrice = MathMax(minDistPrice, 30.0 * pipSize);  // many brokers require 30+ pts for gold/silver
   if (slDist < minDistPrice) slDist = minDistPrice;
   if (tpDist < minDistPrice) tpDist = minDistPrice;

   // Set SL/TP prices based on previously calculated distances
   double sl_price = 0, tp_price = 0;
   if (action == "BUY")
   {
      sl_price = NormalizeDouble(ask - slDist, digits);
      tp_price = NormalizeDouble(ask + tpDist, digits);
   }
   else // SELL
   {
      sl_price = NormalizeDouble(bid + slDist, digits);
      tp_price = NormalizeDouble(bid - tpDist, digits);
   }
      
   bool ok = false;
   int tradeRetCode = 0;
   
   // FIX 4: Retry once on requote — refresh bid/ask before second attempt
   for (int attempt = 1; attempt <= 2; attempt++)
   {
      // Refresh prices on retry
      if (attempt == 2)
      {
         ask = SymbolInfoDouble(sym, SYMBOL_ASK);
         bid = SymbolInfoDouble(sym, SYMBOL_BID);
         if (action == "BUY")
         {
            sl_price = NormalizeDouble(ask - slDist, digits);
            tp_price = NormalizeDouble(ask + tpDist, digits);
         }
         else
         {
            sl_price = NormalizeDouble(bid + slDist, digits);
            tp_price = NormalizeDouble(bid - tpDist, digits);
         }
         Print("[", sym, "] Requote retry with refreshed prices ask=", DoubleToString(ask, digits));
      }
      
      if (action == "BUY")
         ok = trade.Buy(lot, sym, ask, sl_price, tp_price, "ApexHydraFTMO");
      else
         ok = trade.Sell(lot, sym, bid, sl_price, tp_price, "ApexHydraFTMO");
      
      tradeRetCode = (int)trade.ResultRetcode();
      
      // 10004 = TRADE_RETCODE_REQUOTE — retry once
      if (!ok && tradeRetCode == 10004 && attempt == 1)
      {
         Print("[", sym, "] Requote — retrying with refreshed prices...");
         continue;
      }
      break;  // success or non-requote error — stop
   }
      
   if (ok)
   {
      // Reset failure counter on successful order
      if (symIdx >= 0 && symIdx < g_symbolCount)
         g_orderFailCount[symIdx] = 0;

      ulong orderTicket = trade.ResultOrder();
      ulong posTicket   = orderTicket;  // default for ReportOpen
      // If broker opened position but stripped SL/TP (common with gold), set them now.
      // Match by symbol + type; use most recent position (just opened).
      ENUM_POSITION_TYPE wantType = (action == "BUY") ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
      ulong bestTicket = 0;
      datetime bestTime = 0;
      for (int i = 0; i < PositionsTotal(); i++)
      {
         ulong ticket = PositionGetTicket(i);
         if (ticket == 0) continue;
         if (!PositionSelectByTicket(ticket)) continue;
         if (PositionGetString(POSITION_SYMBOL) != sym) continue;
         if (PositionGetInteger(POSITION_TYPE) != wantType) continue;
         datetime pt = (datetime)PositionGetInteger(POSITION_TIME);
         if (pt >= bestTime) { bestTime = pt; bestTicket = ticket; }
      }
      if (bestTicket != 0)
      {
         posTicket = bestTicket;
         if (PositionSelectByTicket(bestTicket))
         {
            double psl = PositionGetDouble(POSITION_SL);
            double ptp = PositionGetDouble(POSITION_TP);
            if (psl == 0 && ptp == 0)
            {
               if (trade.PositionModify(bestTicket, sl_price, tp_price))
                  Print("[", sym, "] SL/TP applied (post-open): broker had not set them on chart");
            }
         }
      }

      Print("[", sym, "] ✔ ", action, " ", DoubleToString(lot,2),
            " lots | SL=", DoubleToString(sl_price, digits),
            " TP=", DoubleToString(tp_price, digits));
      ReportOpen(sym, action, lot, posTicket);
      SendLog("INFO", sym, StringFormat("OPENED %s %.2f lots | SL=%s TP=%s | conf=%.3f | ticket=%I64u",
              action, lot, DoubleToString(sl_price, digits), DoubleToString(tp_price, digits), conf, posTicket));
   }
   else
   {
      string err = trade.ResultRetcodeDescription();
      Print("[", sym, "] ✘ ", err, " (retcode=", tradeRetCode, ")");
      SendLog("ERROR", sym, StringFormat("ORDER FAILED %s %.2f lots | %s", action, lot, err));
      
      // FIX 3: Track failures and impose cooldown to prevent spam loop
      if (symIdx >= 0 && symIdx < g_symbolCount)
      {
         g_orderFailCount[symIdx]++;
         if (g_orderFailCount[symIdx] >= ORDER_FAIL_THRESHOLD)
         {
            g_orderCooldownUntil[symIdx] = TimeCurrent() + ORDER_COOLDOWN_SECS;
            g_orderFailCount[symIdx]     = 0;
            PrintFormat("[%s] ⛔ %d consecutive order failures — pausing scans for %d min",
                        sym, ORDER_FAIL_THRESHOLD, ORDER_COOLDOWN_SECS / 60);
            SendLog("WARN", sym, StringFormat(
               "ORDER SPAM GUARD: %d failures → %d-min cooldown | last err=%s",
               ORDER_FAIL_THRESHOLD, ORDER_COOLDOWN_SECS / 60, err));
         }
      }
   }
      
   // Report updated balance immediately after execution
   ReportEquity();
}

//+------------------------------------------------------------------+
int CountPositions(string sym, ENUM_POSITION_TYPE posType)
{
   int n = 0;
   for (int i = PositionsTotal()-1; i >= 0; i--)
   {
      if (PositionGetTicket(i) == 0) continue;
      // FIX BUG#2: only count our own positions — ClosePositions() already did
      // this but CountPositions() didn't, causing manual/external trades to count
      // toward the position limit and block the bot from opening its own trades.
      if (PositionGetString(POSITION_COMMENT) != "ApexHydraFTMO") continue;
      if (PositionGetString(POSITION_SYMBOL) == sym &&
          (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) == posType) n++;
   }
   return n;
}

void ClosePositions(string sym, ENUM_POSITION_TYPE posType)
{
   for (int i = PositionsTotal()-1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (ticket == 0) continue;
      // BUG FIX: only close OUR positions — never touch manually-opened trades
      if (PositionGetString(POSITION_COMMENT) != "ApexHydraFTMO") continue;
      if (PositionGetString(POSITION_SYMBOL) == sym &&
          (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) == posType)
      {
         if (trade.PositionClose(ticket))
            Print("[", sym, "] Closed ", ticket, " (reverse)");
         else
            Print("[", sym, "] Failed to close ", ticket);
      }
   }
}

//+------------------------------------------------------------------+
string ExtractStr(const string json, const string key)
{
   string tag = "\"" + key + "\":\"";
   int s = StringFind(json, tag);
   if (s < 0) return "";
   s += StringLen(tag);
   int e = StringFind(json, "\"", s);
   return (e < 0) ? "" : StringSubstr(json, s, e - s);
}

double ExtractDbl(const string json, const string key)
{
   string tag = "\"" + key + "\":";
   int s = StringFind(json, tag);
   if (s < 0) return 0.0;
   s += StringLen(tag);
   string val = "";
   for (int i = s; i < StringLen(json); i++)
   {
      string ch = StringSubstr(json, i, 1);
      if (ch=="," || ch=="}" || ch==" " || ch=="\n" || ch=="\"") break;
      val += ch;
   }
   return StringToDouble(val);
}

//+------------------------------------------------------------------+
// ManagePositions — ATR trailing stop + partial close at TP
// Called every 10 seconds from OnTimer.
//
// Logic:
//   1. Partial close: when price hits TP, close PARTIAL_CLOSE_PCT of
//      the position and move SL to breakeven. Let the rest run free.
//   2. ATR trail: once position is 1 ATR in profit, trail SL at
//      TRAIL_ATR_MULT × ATR behind price to lock in gains.
//+------------------------------------------------------------------+
void ManagePositions()
{
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (ticket == 0) continue;
      if (PositionGetString(POSITION_COMMENT) != "ApexHydraFTMO") continue;

      // PROFITABLE FIX #9: Profit-aware time exit.
      // Old: hard close after 4 hours unconditionally — killed profitable runners.
      // New: only force-close LOSING or breakeven positions after 8 hours.
      // Profitable positions are allowed to run until TP or trailing stop.
      datetime openTime = (datetime)PositionGetInteger(POSITION_TIME);
      int hoursOpen     = (int)((TimeCurrent() - openTime) / 3600);
      if (hoursOpen >= 8)
      {
         string  sym_    = PositionGetString(POSITION_SYMBOL);
         double  posPnl  = PositionGetDouble(POSITION_PROFIT);
         if (posPnl <= 0)  // only exit stale losing/flat positions
         {
            if (trade.PositionClose(ticket))
               PrintFormat("[%s] Time exit (8h, P&L=%.2f): closed ticket %I64u",
                           sym_, posPnl, ticket);
            else
               Print("[", sym_, "] Time exit failed: ", trade.ResultRetcodeDescription());
            continue;
         }
         // Position is profitable — let trailing stop handle it
         Print("[", sym_, "] Time exit skipped (", hoursOpen, "h, P&L=",
               DoubleToString(posPnl, 2), " in profit — letting run)");
      }

      string sym    = PositionGetString(POSITION_SYMBOL);
      double openPx = PositionGetDouble(POSITION_PRICE_OPEN);
      double curSL  = PositionGetDouble(POSITION_SL);
      double curTP  = PositionGetDouble(POSITION_TP);
      double lots   = PositionGetDouble(POSITION_VOLUME);
      double bid    = SymbolInfoDouble(sym, SYMBOL_BID);
      double ask    = SymbolInfoDouble(sym, SYMBOL_ASK);
      int    digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double point  = SymbolInfoDouble(sym, SYMBOL_POINT);
      bool   isJpy    = (StringFind(sym, "JPY") >= 0);
      // isMetals covers XAUUSD and XAGUSD — both use point-based pip (not point*10).
      // XAUUSD: point=0.01, pip=0.01.  XAGUSD: point=0.001, pip=0.001.
      // FIX 6c: extended from XAU-only to XAU+XAG so silver gets the same correct
      // pipSize treatment as gold when calculating breakeven/trail SL distances.
      bool   isMetals = (StringFind(sym, "XAU") >= 0 || StringFind(sym, "XAG") >= 0 ||
                         sym == GOLD_SYMBOL);
      bool   isGold   = isMetals; // alias kept for any future gold-specific branching
      double pipSize  = isMetals ? point : point * 10;

      ENUM_POSITION_TYPE posType = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);

      // ── Get live ATR for this symbol ─────────────────────────────
      double atrBuf[];
      ArraySetAsSeries(atrBuf, true);
      int atrHandle = iATR(sym, TF, ATR_PERIOD);
      double atrVal = 0;
      // FIX BUG#3: IndicatorRelease was only called on success. If CopyBuffer
      // failed the handle leaked permanently. With 5 symbols × every 30s,
      // MT5 exhausted its handle pool over hours and ATR silently stopped working.
      if (atrHandle != INVALID_HANDLE)
      {
         if (CopyBuffer(atrHandle, 0, 0, 1, atrBuf) > 0)
            atrVal = atrBuf[0];
         IndicatorRelease(atrHandle); // always release — success or failure
      }
      // When ATR fails (symbol not in Market Watch, wrong TF, etc.) we used to skip
      // the position — SL never moved and user had to manage manually. Now use a
      // pip-based fallback so breakeven/half-profit/trail still run.
      if (atrVal <= 0)
      {
         static string g_lastAtrWarnSym = "";
         static datetime g_lastAtrWarnTime = 0;
         if (sym != g_lastAtrWarnSym || (TimeCurrent() - g_lastAtrWarnTime) >= 300)
         {
            Print("[", sym, "] ATR unavailable — using pip-based fallback for SL management (add symbol to Market Watch if needed)");
            g_lastAtrWarnSym = sym;
            g_lastAtrWarnTime = TimeCurrent();
         }
         atrVal = 15.0 * pipSize; // ~15 pips fallback so tpDist/trailDist are valid
      }

      double trailDist = TRAIL_ATR_MULT * atrVal;
      double lotStep   = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
      double lotMin    = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);

      // ── BUY position management ───────────────────────────────────
      if (posType == POSITION_TYPE_BUY)
      {
         double profit = bid - openPx;
         double tpDist = (curTP > openPx) ? (curTP - openPx) : atrVal * ATR_TP_MULT;  // distance to TP

         // ── Metals quick-exit at ~METAL_QUICK_R × R (R = SL distance) ─────────────
         // Only for XAU/XAG and only when TP is set above entry. This closes the full
         // position once price has moved roughly 1R in profit so you don't have to
         // wait for the full 1.5R TP on very volatile metals.
         if (METAL_QUICK_R > 0.0 && isMetals && curTP > openPx && tpDist > 0)
         {
            double rDist     = tpDist / 1.5;              // since TP ≈ 1.5R by design
            double quickLvl  = openPx + METAL_QUICK_R * rDist;
            // small safety band: require price to be slightly beyond quick level
            if (bid >= quickLvl)
            {
               double pts = (bid - openPx) / pipSize;
               if (trade.PositionClose(ticket))
               {
                  Print("[", sym, "] BUY metals quick-exit at ~", DoubleToString(METAL_QUICK_R, 2),
                        "R | close price=", DoubleToString(bid, digits),
                        " (", DoubleToString(pts, 1), " pips)");
                  continue;
               }
            }
         }

         // ── Partial close at TP ───────────────────────────────────
         if (USE_PARTIAL_CLOSE && curTP > 0 && bid >= curTP)
         {
            double closeVol = NormalizeDouble(lots * PARTIAL_CLOSE_PCT, 2);
            closeVol = MathMax(lotMin, MathRound(closeVol / lotStep) * lotStep);

            if (closeVol >= lots)
            {
               // Position is at minimum lot size — partial close is impossible
               // (0.01 × 50% = 0.005 which is below broker minimum).
               // Instead: extend TP by 1.5× and activate trailing stop to lock gains.
               // This is what lets a winner run rather than closing for peanuts.
               double newTP = NormalizeDouble(openPx + ATR_TP_MULT * 1.5 * atrVal, digits);
               if (newTP > curTP)
               {
                  trade.PositionModify(ticket, curSL, newTP);
                  Print("[", sym, "] BUY min-lot: can't partial close — TP extended to ",
                        DoubleToString(newTP, digits), " letting winner run.");
               }
            }
            else
            {
               trade.PositionClosePartial(ticket, closeVol);
               double newSL = NormalizeDouble(openPx + pipSize, digits);
               trade.PositionModify(ticket, newSL, 0);
               Print("[", sym, "] Partial close ", DoubleToString(closeVol, 2),
                     " lots at TP. SL → breakeven. Remainder running free.");
            }
            continue;
         }

         // ── Breakeven stop: only after price has passed SL_MOVE_PAST_PCT of the way to TP ──
         // Gives the trade room to recover; avoids moving SL too early on normal pullbacks.
         // Enforce min distance SL–TP to avoid broker "common error" on tight-TP pairs (e.g. EURGBP).
         double beThreshold = tpDist * MathMax(0.5, MathMin(0.95, SL_MOVE_PAST_PCT));
         if (profit >= beThreshold && curSL < openPx)
         {
            long stopsLevel = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
            double minDist  = (stopsLevel > 0) ? (stopsLevel * point) : pipSize;
            minDist         = MathMax(minDist, 5.0 * pipSize);
            if (isMetals)
               minDist = MathMax(minDist, 30.0 * pipSize);
            double beSL = NormalizeDouble(openPx + pipSize, digits);
            if (bid - beSL < minDist)
               beSL = NormalizeDouble(bid - minDist, digits);
            // When TP is not set (curTP=0), allow breakeven; otherwise require min distance SL–TP
            bool beOk = (curTP <= openPx || curTP - beSL >= minDist);
            if (beOk)
            {
               if (trade.PositionModify(ticket, beSL, curTP))
               {
                  curSL = beSL;
                  Print("[", sym, "] BUY breakeven SL → ", DoubleToString(beSL, digits),
                        " (profit=", DoubleToString(profit / pipSize, 1), " pips, past ", DoubleToString(SL_MOVE_PAST_PCT * 100, 0), "% to TP)");
               }
               else
                  Print("[", sym, "] BUY breakeven modify FAILED: ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription(),
                        " (SL=", DoubleToString(beSL, digits), " TP=", DoubleToString(curTP, digits), " minDist=", DoubleToString(minDist, digits), ")");
            }
         }

         // ── Half-profit lock: move SL to lock HALF_PROFIT_RATIO of TP gain, but only after
         // price has passed SL_MOVE_PAST_PCT of the way to TP (gives room to recover).
         // Respect broker minimum distance between SL and TP and between current price and SL
         // (10016 invalid stops on XAUUSD when SL too close to bid).
         if (USE_HALF_PROFIT_LOCK && curTP > openPx)
         {
            long stopsLevel = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
            double minDist  = (stopsLevel > 0) ? (stopsLevel * point) : pipSize;
            minDist         = MathMax(minDist, 5.0 * pipSize);
            if (isMetals)
               minDist = MathMax(minDist, 30.0 * pipSize);  // brokers often require 30+ pips for gold/silver
            double halfLevel = openPx + tpDist * MathMax(0.01, MathMin(0.99, HALF_PROFIT_RATIO));
            double moveThreshold = openPx + tpDist * MathMax(0.5, MathMin(0.95, SL_MOVE_PAST_PCT));
            if (bid >= moveThreshold && bid >= halfLevel && curSL < halfLevel - pipSize)
            {
               double lockSL = NormalizeDouble(halfLevel, digits);
               // Broker requires min distance from current price to SL (avoid 10016 invalid stops)
               if (bid - lockSL < minDist)
                  lockSL = NormalizeDouble(bid - minDist, digits);
               if (lockSL > openPx && curTP - lockSL >= minDist)
               {
                  // Skip modify if new SL is effectively same as current (avoids 10025 no changes)
                  if (MathAbs(lockSL - curSL) >= point)
                  {
                     if (trade.PositionModify(ticket, lockSL, curTP))
                     {
                        curSL = lockSL;
                        Print("[", sym, "] BUY half-profit lock SL → ", DoubleToString(lockSL, digits),
                              " (", DoubleToString(HALF_PROFIT_RATIO * 100, 0), "% of TP, after ", DoubleToString(SL_MOVE_PAST_PCT * 100, 0), "% to TP)");
                     }
                     else if (trade.ResultRetcode() != 10025)
                        Print("[", sym, "] BUY half-profit modify FAILED: ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
                  }
               }
            }
         }

         // ── ATR trailing stop — only after price is well past halfway to TP ──
         // Uses SL_MOVE_PAST_PCT + 20% so trail starts later (e.g. 80% of way if SL_MOVE_PAST_PCT=0.6).
         // Gives the trade room to recover before trailing locks in profit.
         if (USE_TRAILING_STOP && profit >= tpDist * MathMin(0.95, SL_MOVE_PAST_PCT + 0.2))
         {
            double newSL = NormalizeDouble(bid - trailDist, digits);
            if (newSL > curSL + pipSize)  // only move up, never down
            {
               if (!trade.PositionModify(ticket, newSL, curTP))
                  Print("[", sym, "] BUY trail modify FAILED: ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription(),
                        " (newSL=", DoubleToString(newSL, digits), ")");
               else
                  Print("[", sym, "] Trail SL → ", DoubleToString(newSL, digits),
                        " (", DoubleToString((newSL - openPx) / pipSize, 1), " pips locked)");
            }
         }
      }

      // ── SELL position management ──────────────────────────────────
      else
      {
         double profit = openPx - ask;
         double tpDist = (curTP > 0 && curTP < openPx) ? (openPx - curTP) : atrVal * ATR_TP_MULT;  // distance to TP

         // ── Metals quick-exit at ~METAL_QUICK_R × R for SELL ─────────────────────
         if (METAL_QUICK_R > 0.0 && isMetals && curTP > 0 && curTP < openPx && tpDist > 0)
         {
            double rDist     = tpDist / 1.5;
            double quickLvl  = openPx - METAL_QUICK_R * rDist;
            if (ask <= quickLvl)
            {
               double pts = (openPx - ask) / pipSize;
               if (trade.PositionClose(ticket))
               {
                  Print("[", sym, "] SELL metals quick-exit at ~", DoubleToString(METAL_QUICK_R, 2),
                        "R | close price=", DoubleToString(ask, digits),
                        " (", DoubleToString(pts, 1), " pips)");
                  continue;
               }
            }
         }

         // ── Partial close at TP ───────────────────────────────────
         if (USE_PARTIAL_CLOSE && curTP > 0 && ask <= curTP)
         {
            double closeVol = NormalizeDouble(lots * PARTIAL_CLOSE_PCT, 2);
            closeVol = MathMax(lotMin, MathRound(closeVol / lotStep) * lotStep);

            if (closeVol >= lots)
            {
               // Same minimum-lot fix as BUY — extend TP instead of closing whole position.
               double newTP = NormalizeDouble(openPx - ATR_TP_MULT * 1.5 * atrVal, digits);
               if (newTP < curTP)
               {
                  trade.PositionModify(ticket, curSL, newTP);
                  Print("[", sym, "] SELL min-lot: can't partial close — TP extended to ",
                        DoubleToString(newTP, digits), " letting winner run.");
               }
            }
            else
            {
               trade.PositionClosePartial(ticket, closeVol);
               // BUG FIX: was (openPx - pipSize) which placed SL BELOW entry
               // on a SELL — that is in-profit territory, not breakeven.
               // For a SELL, breakeven = entry price + one pip (above entry).
               double newSL = NormalizeDouble(openPx + pipSize, digits);
               trade.PositionModify(ticket, newSL, 0);
               Print("[", sym, "] Partial close ", DoubleToString(closeVol, 2),
                     " lots at TP. SL → breakeven. Remainder running free.");
            }
            continue;
         }

         // ── Breakeven stop: only after price has passed SL_MOVE_PAST_PCT of the way to TP ──
         // Same as BUY — gives the trade room to recover before locking breakeven.
         // Enforce min distance SL–TP to avoid broker "common error" on tight-TP pairs.
         double beThresholdSell = tpDist * MathMax(0.5, MathMin(0.95, SL_MOVE_PAST_PCT));
         if (profit >= beThresholdSell && curSL > openPx)
         {
            long stopsLevel = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
            double minDist  = (stopsLevel > 0) ? (stopsLevel * point) : pipSize;
            minDist         = MathMax(minDist, 5.0 * pipSize);
            if (isMetals)
               minDist = MathMax(minDist, 30.0 * pipSize);
            double beSL = NormalizeDouble(openPx - pipSize, digits);
            if (beSL - ask < minDist)
               beSL = NormalizeDouble(ask + minDist, digits);
            // When TP is not set (curTP=0), allow breakeven; otherwise require min distance
            bool beOkSell = (curTP <= 0 || curTP >= openPx || beSL - curTP >= minDist);
            if (beOkSell)
            {
               if (trade.PositionModify(ticket, beSL, curTP))
               {
                  curSL = beSL;
                  Print("[", sym, "] SELL breakeven SL → ", DoubleToString(beSL, digits),
                        " (profit=", DoubleToString(profit / pipSize, 1), " pips, past ", DoubleToString(SL_MOVE_PAST_PCT * 100, 0), "% to TP)");
               }
               else
                  Print("[", sym, "] SELL breakeven modify FAILED: ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription(),
                        " (SL=", DoubleToString(beSL, digits), " TP=", DoubleToString(curTP, digits), ")");
            }
         }

         // ── Half-profit lock: move SL to lock HALF_PROFIT_RATIO of TP gain, but only after
         // price has passed SL_MOVE_PAST_PCT of the way to TP (gives room to recover).
         // Respect min distance from current price to SL (avoid 10016 invalid stops on metals).
         if (USE_HALF_PROFIT_LOCK && curTP > 0 && curTP < openPx)
         {
            long stopsLevel = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
            double minDist  = (stopsLevel > 0) ? (stopsLevel * point) : pipSize;
            minDist         = MathMax(minDist, 5.0 * pipSize);
            if (isMetals)
               minDist = MathMax(minDist, 30.0 * pipSize);
            double halfLevel = openPx - tpDist * MathMax(0.01, MathMin(0.99, HALF_PROFIT_RATIO));
            double moveThreshold = openPx - tpDist * MathMax(0.5, MathMin(0.95, SL_MOVE_PAST_PCT));
            if (ask <= moveThreshold && ask <= halfLevel && (curSL > halfLevel + pipSize || curSL == 0))
            {
               double lockSL = NormalizeDouble(halfLevel, digits);
               if (lockSL - ask < minDist)
                  lockSL = NormalizeDouble(ask + minDist, digits);
               if (lockSL < openPx && lockSL - curTP >= minDist)
               {
                  if (MathAbs(lockSL - curSL) >= point)
                  {
                     if (trade.PositionModify(ticket, lockSL, curTP))
                     {
                        curSL = lockSL;
                        Print("[", sym, "] SELL half-profit lock SL → ", DoubleToString(lockSL, digits),
                              " (", DoubleToString(HALF_PROFIT_RATIO * 100, 0), "% of TP, after ", DoubleToString(SL_MOVE_PAST_PCT * 100, 0), "% to TP)");
                     }
                     else if (trade.ResultRetcode() != 10025)
                        Print("[", sym, "] SELL half-profit modify FAILED: ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
                  }
               }
            }
         }

         // ── ATR trailing stop — only after price is well past halfway to TP ──
         // Same as BUY: trail starts at SL_MOVE_PAST_PCT + 20% of the way to TP.
         if (USE_TRAILING_STOP && profit >= tpDist * MathMin(0.95, SL_MOVE_PAST_PCT + 0.2))
         {
            double newSL = NormalizeDouble(ask + trailDist, digits);
            if (newSL < curSL - pipSize)  // only move down, never up
            {
               if (!trade.PositionModify(ticket, newSL, curTP))
                  Print("[", sym, "] SELL trail modify FAILED: ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription(),
                        " (newSL=", DoubleToString(newSL, digits), ")");
               else
                  Print("[", sym, "] Trail SL → ", DoubleToString(newSL, digits),
                        " (", DoubleToString((openPx - newSL) / pipSize, 1), " pips locked)");
            }
         }
      }
   }
}

//+------------------------------------------------------------------+
// CheckCommands — polls Modal /commands endpoint every 10 seconds.
// If "close_all" command is received, closes every open position immediately.
//+------------------------------------------------------------------+
void CheckCommands()
{
   char   res[];
   string resHeaders;
   string headers = "Content-Type: application/json\r\n";
   if (StringLen(API_TOKEN) > 0)
      headers += "X-APEXHYDRA-TOKEN: " + API_TOKEN + "\r\n";
   char   dummy[];
   ArrayResize(dummy, 0);   // explicit empty array — some MT5 builds require this

   int code = WebRequest(
      "GET", MODAL_BASE_URL + "/commands",
      headers, 5000, dummy, res, resHeaders
   );

   if (code != 200) return;

   string resp = CharArrayToString(res, 0, ArraySize(res));

   // Check if close_all command is present
   if (StringFind(resp, "close_all") < 0) return;

   Print("=== DASHBOARD STOP: Closing ALL ApexHydraFTMO positions ===");

   int closed = 0;
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (ticket == 0) continue;
      // Only close positions opened by this EA — leave manual/other EAs' positions alone
      if (PositionGetString(POSITION_COMMENT) != "ApexHydraFTMO") continue;

      string sym = PositionGetString(POSITION_SYMBOL);
      if (trade.PositionClose(ticket))
      {
         Print("Closed position ", ticket, " (", sym, ")");
         closed++;
      }
      else
         Print("Failed to close ", ticket, ": ", trade.ResultRetcodeDescription());
   }

   Print("=== Close all complete: ", closed, " positions closed ===");
   ReportEquity();

   // Send explicit acknowledgment back to Modal so the dashboard can
   // display when the STOP + close_all was actually completed.
   string ackJson = StringFormat("{\"source\":\"ea\",\"closed\":%d}", closed);
   char   postDataAck[];
   StringToCharArray(ackJson, postDataAck, 0, StringLen(ackJson));

   string ackHeaders = "Content-Type: application/json\r\n";
   if (StringLen(API_TOKEN) > 0)
      ackHeaders += "X-APEXHYDRA-TOKEN: " + API_TOKEN + "\r\n";

   char ackRes[];
   string ackResHeaders;
   int ackCode = WebRequest(
      "POST", MODAL_BASE_URL + "/closeall_ack",
      ackHeaders, 5000, postDataAck, ackRes, ackResHeaders
   );

   if (ackCode != 200)
      Print("[CLOSEALL_ACK] HTTP ", ackCode, ": ", CharArrayToString(ackRes));
}

//+------------------------------------------------------------------+
// OnTradeTransaction — reports real PnL to Modal /close on every fill
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest     &request,
                        const MqlTradeResult      &result)
{
   if (trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   ulong dealTicket = trans.deal;
   if (dealTicket == 0) return;
   if (!HistoryDealSelect(dealTicket)) return;

   // ── Intercept balance events (deposits / withdrawals) ────────────────────
   // MT5 fires OnTradeTransaction with DEAL_TYPE_BALANCE for every capital
   // movement. We catch them here in real time so the dashboard updates
   // instantly rather than waiting for the next scheduled scan.
   long dealType = HistoryDealGetInteger(dealTicket, DEAL_TYPE);
   if (dealType == DEAL_TYPE_BALANCE)
   {
      double   profit   = HistoryDealGetDouble(dealTicket, DEAL_PROFIT);
      // For a live balance event, AccountInfoDouble(ACCOUNT_BALANCE) reflects
      // the balance *after* the deposit/withdrawal has already been applied —
      // MT5 updates the account before firing OnTradeTransaction.
      double   balAfter = AccountInfoDouble(ACCOUNT_BALANCE);
      datetime dealTime = (datetime)HistoryDealGetInteger(dealTicket, DEAL_TIME);

      if (profit == 0) return;  // zero-amount correction — ignore

      string txnType = (profit > 0) ? "deposit" : "withdrawal";
      PrintFormat("[TRANSACTION] Live %s detected: $%.2f | ticket=%I64u",
                  txnType, MathAbs(profit), dealTicket);

      ReportTransaction(txnType, MathAbs(profit), balAfter, dealTicket, dealTime);

      // Immediately push a fresh equity snapshot so the dashboard reflects the
      // new balance right away without waiting for the 30-second equity timer.
      ReportEquity();
      return;  // balance deals are not trade closes — stop processing here
   }
   // ── End balance event intercept ───────────────────────────────────────────

   ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(dealTicket, DEAL_ENTRY);
   if (entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_INOUT) return;

   string   sym      = HistoryDealGetString(dealTicket,  DEAL_SYMBOL);
   double   pnl      = HistoryDealGetDouble(dealTicket,  DEAL_PROFIT)
                     + HistoryDealGetDouble(dealTicket,  DEAL_SWAP)
                     + HistoryDealGetDouble(dealTicket,  DEAL_COMMISSION);
   double   price    = HistoryDealGetDouble(dealTicket,  DEAL_PRICE);
   datetime dealTime = (datetime)HistoryDealGetInteger(dealTicket, DEAL_TIME);

   // Server matches /close by ticket stored at /open — that is the POSITION ticket.
   // DEAL_POSITION_ID links this closing deal to the position we reported at /open.
   ulong positionId = (ulong)HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID);
   if (positionId == 0)
      positionId = dealTicket;   // fallback: some brokers may not set DEAL_POSITION_ID

   MqlDateTime mdt;
   TimeToStruct(dealTime, mdt);
   string closeTime = StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ",
      mdt.year, mdt.mon, mdt.day, mdt.hour, mdt.min, mdt.sec);

   // Send "ticket" = position id so server can match the trade row from /open.
   string json = StringFormat(
      "{\"ticket\":%I64u,\"symbol\":\"%s\","
      "\"pnl\":%.2f,\"close_price\":%.5f,\"close_time\":\"%s\"}",
      positionId, sym, pnl, price, closeTime
   );

   char   postData[], res[];
   string headers = "Content-Type: application/json\r\n";
   if (StringLen(API_TOKEN) > 0)
      headers += "X-APEXHYDRA-TOKEN: " + API_TOKEN + "\r\n";
   string resHeaders;
   StringToCharArray(json, postData, 0, StringLen(json));

   int code = WebRequest("POST", MODAL_BASE_URL + "/close",
                         headers, 5000, postData, res, resHeaders);

   if (code == 200)
   {
      PrintFormat("[%s] ✔ Close reported | PnL $%.2f | positionId %I64u (sent to server)", sym, pnl, positionId);
      SendLog(pnl >= 0 ? "INFO" : "WARN", sym,
              StringFormat("CLOSED positionId=%I64u | PnL=$%.2f | price=%s",
                           positionId, pnl, DoubleToString(price, 5)));
   }
   else
   {
      PrintFormat("[%s] ✘ Close report failed (%d) ticket %I64u", sym, code, dealTicket);
      SendLog("ERROR", sym, StringFormat("CLOSE REPORT FAILED HTTP=%d ticket=%I64u PnL=$%.2f",
                                          code, dealTicket, pnl));
   }

   // Report fresh balance immediately after close
   ReportEquity();
}