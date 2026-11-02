"""
╔══════════════════════════════════════════════════════════════════════════╗
║   NIFTY 50 LIQUIDITY HUNT BOT — V7 COMPLETE EDITION (nifty_bot.py)       ║
║   Namespace: NIFTY:*  |  Logger: [NIFTY]  |  Upstox V2/V3 1-Yr Token     ║
║                                                                          ║
║   ALL VERIFIED BUGS FIXED + COMPLETE V6/V7 YELLOW FLASH ENGINE PORTED:   ║
║   1. REAL STRUCTURAL OPTION SL (NO FAKE CLAMPING):                       ║
║      - Option SL is placed strictly below the entry Green candle's low   ║
║        (or sweep wick low). If real structural Option SL is too wide     ║
║        (> MAX_OPTION_RISK_PTS, default ₹15), the trade is SKIPPED        ║
║        ("Unch bhavala entry nahi") instead of faking a clamped SL!       ║
║   2. NIFTY TUESDAY WEEKLY EXPIRY FALLBACK FIXED:                         ║
║      - Nifty weekly expiry fallback uses Tuesday (weekday == 1), never   ║
║        Thursday.                                                         ║
║   3. STRICT AI DIRECTION & FAILURE GATE (NO RUBBER-STAMP / NO FLIP):     ║
║      - Enforces `ai_signal == python_action`. If AI outputs the opposite ║
║        side (e.g. BUY_PE on a BUY_CE sweep), the trade is REJECTED.      ║
║      - If both Gemini and Claude APIs fail/timeout, the bot logs         ║
║        AI_UNAVAILABLE and WAITS (never auto-approves blindly).           ║
║   4. STRICT SWEEP INEQUALITY (< low / > high):                           ║
║      - A sweep requires `candle_low < zone_low` (for support) or         ║
║        `candle_high > zone_high` (for resistance). Touching above pivot  ║
║        is never falsely called a "broke/swept" level.                    ║
║   5. MULTI-DAY 15M TREND + REAL EMA21 / HH-HL STRUCTURE AT 09:20 AM:     ║
║      - Combines 5-day historical 15M candles with today's intraday 15M   ║
║        candles so EMA9/EMA21 and HH_HL / LH_LL structure are 100% active ║
║        from 09:20 AM (never stuck in SIDEWAYS all morning).              ║
║   6. TIMESTAMP-BASED drop_incomplete_candle() EVERYWHERE:                ║
║      - Applied to 1D, 1H, 30M, 15M, 5M, 1M, Option candles, and          ║
║        Morning-to-Afternoon memory sync. Never blindly drops a completed ║
║        candle or reads a half-formed candle.                             ║
║   7. GEMINI 2.5 FLASH `thinkingBudget: 0` + `finishReason` LOGGING:      ║
║      - Prevents Gemini 2.5 Flash from burning `maxOutputTokens` on       ║
║        internal thinking tokens and truncating JSON output.              ║
║   8. FULL DELTA-ENCODED CANDLES SENT TO AI + WAIT DEFAULT + ANTI-GAMING: ║
║      - Sends shared-base delta-encoded 1H, 15M, 5M, 1M candles +         ║
║        Dominance Candle + Zone History + Distance-from-sweep to AI.      ║
║      - Enforces anti-gaming confirmation check (>=12 chars + real price).║
║   9. COMPLETE 5-SETUP ENGINE + EOD OUTCOME TRACKING (PART G JOURNAL):    ║
║      - S1: 2nd-Time Liquidity Hunt (Trend-Side Fast vs Counter-Trend     ║
║        Blue-Box -> Yellow-Box 2nd Hunt + Option Decay Low Sweep).        ║
║      - S2: Prev-Day High/Low/Close (PDH/PDL/PDC) & Clean Origin Hunt.    ║
║      - S3: Sideways Compression Box (Fakeout Hunt Reclaim + Micro-Candle ║
║        Seller/Buyer Absorption — NOT ORB!).                              ║
║      - TREND_PULLBACK: 15M Trend + 5M Retail Trailing SL Sweep.          ║
║      - MORNING_AFTERNOON_MEMORY & SELECTIVE 6M HISTORY (±50 pts):        ║
║        Morning (09:15–11:30) liquidity boxes re-synced in afternoon      ║
║        when price returns within ±50 pts; 6M history requires multi-day  ║
║        structural pivot + >=120 pt institutional displacement.           ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import os
import json
import math
import time
import html
import logging
import threading
import requests
import pandas as pd
from datetime import datetime, timedelta
from urllib.parse import quote
import pytz

IST = pytz.timezone("Asia/Kolkata")
log = logging.getLogger("NIFTY")

# ── Environment Configuration ────────────────────────────────────────
UPSTOX_TOKEN     = os.getenv("UPSTOX_ANALYTICS_TOKEN", "")
GEMINI_API_KEYS  = [k.strip() for k in os.getenv("GEMINI_API_KEYS", os.getenv("GEMINI_API_KEY", "")).split(",") if k.strip()]
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
TG_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID       = os.getenv("NIFTY_TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", ""))

NIFTY_KEY        = "NSE_INDEX|Nifty 50"
NIFTY_KEY_ENC    = quote(NIFTY_KEY)
UPSTOX_V2        = "https://api.upstox.com/v2"
UPSTOX_V3        = "https://api.upstox.com/v3"

# ── Bot & Cooldown Settings ──────────────────────────────────────────
PRICE_RANGE_WINDOW_PTS = int(os.getenv("NIFTY_PRICE_RANGE_PTS", "50"))      # ±50 pts Morning & Historical sync window
TURBO_1M_PROXIMITY_PTS = int(os.getenv("NIFTY_ZONE_PROXIMITY_PTS", "50"))   # 1m polling when within ±50 pts of zone
WAIT_COOLDOWN_MIN      = int(os.getenv("NIFTY_WAIT_COOLDOWN_MIN", "10"))
SIGNAL_COOLDOWN_MIN    = int(os.getenv("NIFTY_SIGNAL_COOLDOWN_MIN", "20"))
PULLBACK_COOLDOWN_MIN  = int(os.getenv("NIFTY_PULLBACK_COOLDOWN_MIN", "12"))
MAX_ZONES              = int(os.getenv("NIFTY_MAX_ZONES", "12"))
MAX_AI_RAW_LOG         = int(os.getenv("NIFTY_MAX_AI_RAW_LOG", "100"))
MIN_CONFIRMATIONS      = int(os.getenv("NIFTY_MIN_CONFIRMATIONS", "2"))
SIGNAL_VALID_MIN       = int(os.getenv("NIFTY_SIGNAL_VALID_MIN", "45"))

# ── Candle & Setup Engine Thresholds ─────────────────────────────────
SIDEWAYS_MIN_CANDLES   = int(os.getenv("NIFTY_SIDEWAYS_MIN_CANDLES", "5"))
SIDEWAYS_MAX_RANGE     = int(os.getenv("NIFTY_SIDEWAYS_MAX_RANGE", "35"))
MICRO_BODY_MAX_PTS     = int(os.getenv("NIFTY_MICRO_BODY_MAX_PTS", "6"))
ORIGIN_MIN_MOVE_PTS    = int(os.getenv("NIFTY_ORIGIN_MIN_MOVE_PTS", "45"))
HIST_MIN_DISPLACEMENT  = int(os.getenv("NIFTY_HIST_MIN_DISPLACEMENT", "120"))  # Strict >=120pt institutional move
BLUE_BOX_BOUNCE_PTS    = int(os.getenv("NIFTY_BLUE_BOX_BOUNCE_PTS", "30"))
LAST_AI_CALL_MINUTE    = int(os.getenv("NIFTY_LAST_AI_CALL_MINUTE", "10"))
REJECTION_WICK_RATIO   = float(os.getenv("NIFTY_REJECTION_WICK_RATIO", "0.30"))
DOMINANCE_MIN_RANGE    = int(os.getenv("NIFTY_DOMINANCE_MIN_RANGE", "18"))
DOMINANCE_LOOKBACK     = int(os.getenv("NIFTY_DOMINANCE_LOOKBACK", "15"))
REF_SL_BUFFER          = int(os.getenv("NIFTY_REF_SL_BUFFER", "8"))
REF_TARGET_FALLBACK    = int(os.getenv("NIFTY_REF_TARGET_FALLBACK", "65"))
OPTION_LOOKBACK        = int(os.getenv("NIFTY_OPTION_LOOKBACK", "20"))
MAX_INDEX_CHASE_PTS    = int(os.getenv("NIFTY_MAX_INDEX_CHASE_PTS", "38"))
MAX_OPTION_RISK_PTS    = float(os.getenv("NIFTY_MAX_OPTION_RISK_PTS", "15.0"))  # Real SL guard, never clamped!

# ── Watchdog & Holidays ──────────────────────────────────────────────
FEED_FAIL_ALERT_AFTER  = int(os.getenv("FEED_FAIL_ALERT_AFTER", "3"))
NSE_HOLIDAYS           = {d.strip() for d in os.getenv("NSE_HOLIDAYS", "").split(",") if d.strip()}

# ── AI Models ────────────────────────────────────────────────────────
GEMINI_MODEL              = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ANALYSIS_CLAUDE_MODEL     = os.getenv("ANALYSIS_MODEL", "claude-sonnet-4-6")
DECISION_CLAUDE_MODEL     = os.getenv("CLAUDE_BACKUP_MODEL", "claude-haiku-4-5-20251001")
FIRST_ANALYSIS_MAX_TOKENS = int(os.getenv("FIRST_ANALYSIS_MAX_TOKENS", "2600"))
ZONE_DECISION_MAX_TOKENS  = int(os.getenv("ZONE_DECISION_MAX_TOKENS", "900"))

_nifty_lock   = threading.Lock()
_gemini_idx   = 0
_feed_fails   = 0
_feed_alerted = False


def esc(text):
    return html.escape(str(text), quote=False) if text is not None else ""


# ═══════════════════════════════════════════════════════════════════════
# SECTION 1: NAMESPACED STATE ENGINE (PREFIX: "NIFTY:")
# ═══════════════════════════════════════════════════════════════════════

class NiftyState:
    """Strictly prefixes every Redis/RAM key with 'NIFTY:' so BTC and GOLD never collide."""
    PREFIX = "NIFTY:"

    def __init__(self, shared_store):
        self.store = shared_store

    def set(self, key, value, ttl=86400):
        self.store.set(f"{self.PREFIX}{key}", value, ttl=ttl)

    def get(self, key):
        return self.store.get(f"{self.PREFIX}{key}")

    def delete(self, key):
        self.store.delete(f"{self.PREFIX}{key}")


def save_zones(state: NiftyState, zones):
    state.set("zones", zones[:MAX_ZONES])
    state.set("zones_date", datetime.now(IST).strftime("%Y-%m-%d"))
    log.info(f"[NIFTY] ✅ {len(zones[:MAX_ZONES])} zones saved")


def get_zones(state: NiftyState):
    return state.get("zones") or []


def update_single_zone_state(state: NiftyState, zone_id, touch_count=None, blue_box_extreme=None,
                             flipped_type=None, preferred_action=None):
    zones = get_zones(state)
    changed = False
    for z in zones:
        if z.get("id") == zone_id:
            if touch_count is not None:
                z["touch_count"] = touch_count
            if blue_box_extreme is not None:
                z["blue_box_extreme"] = blue_box_extreme
            if flipped_type:
                z["type"] = flipped_type
            if preferred_action:
                z["preferred_action"] = preferred_action
            changed = True
            break
    if changed:
        save_zones(state, zones)


def save_morning_context(state: NiftyState, ctx):
    state.set("morning_context", ctx)
    state.set("morning_context_date", datetime.now(IST).strftime("%Y-%m-%d"))


def get_morning_context(state: NiftyState):
    return state.get("morning_context") or {}


def is_today_data_valid(state: NiftyState):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return bool(get_zones(state) and get_morning_context(state)
                and state.get("zones_date") == today and state.get("morning_context_date") == today)


def get_signal_history(state: NiftyState):
    return state.get("signal_history") or []


def save_signal_log(state: NiftyState, entry):
    h = get_signal_history(state)
    h.append(entry)
    state.set("signal_history", h)


def save_ai_raw_log(state: NiftyState, entry):
    logs = state.get("ai_raw_log") or []
    logs.append(entry)
    state.set("ai_raw_log", logs[-MAX_AI_RAW_LOG:])


def save_flip_event(state: NiftyState, zone_id, direction):
    events = state.get("flip_log") or []
    events.append({"time": datetime.now(IST).strftime("%H:%M"), "zone_id": zone_id, "direction": direction})
    state.set("flip_log", events[-20:])


def get_structure_shift(state: NiftyState, window_minutes=60, min_flips=2):
    events = state.get("flip_log") or []
    if not events:
        return None
    now = datetime.now(IST)
    recent = []
    for e in events:
        try:
            t = IST.localize(datetime.strptime(e["time"], "%H:%M").replace(
                year=now.year, month=now.month, day=now.day))
            if 0 <= (now - t).total_seconds() / 60 <= window_minutes:
                recent.append(e)
        except Exception:
            continue
    down = [e for e in recent if e["direction"] == "down"]
    up = [e for e in recent if e["direction"] == "up"]
    if len(down) >= min_flips and len(down) > len(up):
        return {"direction": "down", "count": len(down), "since": down[0]["time"]}
    if len(up) >= min_flips and len(up) > len(down):
        return {"direction": "up", "count": len(up), "since": up[0]["time"]}
    return None


def zone_cooldown_ok(state: NiftyState, zone_id):
    cd = state.get(f"cooldown_{zone_id}")
    if cd:
        try:
            until = datetime.fromisoformat(cd["until"])
            if until.tzinfo is None:
                until = IST.localize(until)
            if datetime.now(IST) < until:
                return False
        except Exception:
            pass
    return True


def mark_zone_cooldown(state: NiftyState, zone_id, signal_type, custom_mins=None):
    if custom_mins is not None:
        mins = custom_mins
    else:
        mins = SIGNAL_COOLDOWN_MIN if signal_type in ("BUY_CE", "BUY_PE") else WAIT_COOLDOWN_MIN
    until = datetime.now(IST) + timedelta(minutes=mins)
    state.set(f"cooldown_{zone_id}", {"until": until.isoformat()}, ttl=mins * 60 + 120)
    log.info(f"[NIFTY] ⏳ Cooldown set on {zone_id} for {mins} min ({signal_type})")


def flush_day(state: NiftyState):
    for k in ["zones", "zones_date", "morning_context", "morning_context_date",
              "signal_history", "ai_raw_log", "flip_log", "active_s3_box", "hist_memory_bank"]:
        state.delete(k)
    log.info("[NIFTY] 🧹 Day data flushed")


# ═══════════════════════════════════════════════════════════════════════
# SECTION 2: UPSTOX FETCHERS (1D, 1H, 30M, 15M, 5M, 1M + OPTIONS)
# ═══════════════════════════════════════════════════════════════════════

def _feed_ok():
    global _feed_fails, _feed_alerted
    if _feed_alerted:
        tg_send("✅ <b>[NIFTY] DATA FEED BACK</b> — Upstox responding again.")
    _feed_fails = 0
    _feed_alerted = False


def _feed_fail(detail):
    global _feed_fails, _feed_alerted
    _feed_fails += 1
    log.error(f"[NIFTY] Feed failure #{_feed_fails}: {detail}")
    if _feed_fails >= FEED_FAIL_ALERT_AFTER and not _feed_alerted:
        _feed_alerted = True
        tg_send(f"🔴 <b>[NIFTY] DATA FEED DOWN</b>\n{_feed_fails} consecutive Upstox failures.\n"
                f"Last error: {esc(str(detail)[:120])}\n\n"
                f"Check Upstox token or network status!")


def upstox_get(url, retries=3):
    hdrs = {"Authorization": f"Bearer {UPSTOX_TOKEN}", "Accept": "application/json"}
    last_detail = "unknown"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=hdrs, timeout=15)
            if r.status_code == 200:
                d = r.json()
                if d.get("status") == "success":
                    _feed_ok()
                    return d.get("data", {})
                last_detail = f"status={d.get('status')}"
            elif r.status_code in (401, 403):
                last_detail = f"HTTP {r.status_code} — token expired/invalid"
                break
            elif r.status_code == 429:
                last_detail = "HTTP 429 rate limited"
                time.sleep(2 ** attempt)
            else:
                last_detail = f"HTTP {r.status_code}"
        except Exception as e:
            last_detail = str(e)
        time.sleep(1)
    _feed_fail(last_detail)
    return None


def get_ltp():
    data = upstox_get(f"{UPSTOX_V3}/market-quote/ltp?instrument_key={NIFTY_KEY_ENC}")
    if data:
        first = next(iter(data.values()), None)
        if first and "last_price" in first:
            return float(first["last_price"])
    return None


def _to_df(data, keep_float=False):
    if not data or "candles" not in data:
        return pd.DataFrame()
    df = pd.DataFrame(data["candles"], columns=["ts", "o", "h", "l", "c", "v", "oi"])
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts").reset_index(drop=True)
    if keep_float:
        df[["o", "h", "l", "c"]] = df[["o", "h", "l", "c"]].astype(float).round(2)
    else:
        df[["o", "h", "l", "c"]] = df[["o", "h", "l", "c"]].round(0).astype(int)
    return df


def _merge_hist_and_intraday(hist_df, intra_df):
    """Combines historical multi-day candles with today's live intraday candles without duplicates."""
    if hist_df is None or hist_df.empty:
        return intra_df if intra_df is not None else pd.DataFrame()
    if intra_df is None or intra_df.empty:
        return hist_df
    combined = pd.concat([hist_df, intra_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["ts"], keep="last").sort_values("ts").reset_index(drop=True)
    return combined


def fetch_historical(unit, interval, candles_needed, instrument_key_enc=None, keep_float=False, days_override=None):
    key_enc = instrument_key_enc or NIFTY_KEY_ENC
    now = datetime.now(IST)
    to_date = now.strftime("%Y-%m-%d")
    if days_override is not None:
        from_date = (now - timedelta(days=days_override)).strftime("%Y-%m-%d")
    elif unit == "days":
        from_date = (now - timedelta(days=candles_needed + 15)).strftime("%Y-%m-%d")
    elif unit == "hours":
        from_date = (now - timedelta(days=18)).strftime("%Y-%m-%d")
    else:
        from_date = (now - timedelta(days=6)).strftime("%Y-%m-%d")
    data = upstox_get(f"{UPSTOX_V3}/historical-candle/{key_enc}/{unit}/{interval}/{to_date}/{from_date}")
    df = _to_df(data, keep_float=keep_float)
    return df.tail(candles_needed) if (not df.empty and candles_needed) else df


def fetch_intraday(unit, interval, instrument_key_enc=None, keep_float=False):
    key_enc = instrument_key_enc or NIFTY_KEY_ENC
    return _to_df(
        upstox_get(f"{UPSTOX_V3}/historical-candle/intraday/{key_enc}/{unit}/{interval}"),
        keep_float=keep_float,
    )


def fetch_top_down_data():
    """Fetches full multi-timeframe hierarchy (1D, 1H, 30M, 15M, 5M) merging history + intraday
    so morning indicators (EMA21, 15M swings) have 5+ days of warmup immediately at 09:20 AM."""
    log.info("[NIFTY] Fetching Top-Down TF data (1D, 1H, 30M, 15M, 5M)...")
    daily = fetch_historical("days", 1, 20)
    h1    = _merge_hist_and_intraday(fetch_historical("hours", 1, 30), fetch_intraday("hours", 1))
    m30   = _merge_hist_and_intraday(fetch_historical("minutes", 30, 45), fetch_intraday("minutes", 30))
    m15   = _merge_hist_and_intraday(fetch_historical("minutes", 15, 60), fetch_intraday("minutes", 15))
    m5    = _merge_hist_and_intraday(fetch_historical("minutes", 5, 80), fetch_intraday("minutes", 5))
    return {"daily": daily, "h1": h1, "m30": m30, "m15": m15, "m5": m5}


def fetch_live_monitor_data(include_1m=True):
    """Fetches merged historical + intraday for 1H, 30M, 15M, 5M, and 1M so 15M trend & EMAs
    never run out of bars during the first hour of trading (09:15–10:15 AM)."""
    h1  = _merge_hist_and_intraday(fetch_historical("hours", 1, 20), fetch_intraday("hours", 1))
    m30 = _merge_hist_and_intraday(fetch_historical("minutes", 30, 30), fetch_intraday("minutes", 30))
    m15 = _merge_hist_and_intraday(fetch_historical("minutes", 15, 45), fetch_intraday("minutes", 15))
    m5  = _merge_hist_and_intraday(fetch_historical("minutes", 5, 65), fetch_intraday("minutes", 5))

    m1 = pd.DataFrame()
    if include_1m:
        m1 = fetch_intraday("minutes", 1)
        if m1.empty or len(m1) < 15:
            m1 = _merge_hist_and_intraday(fetch_historical("minutes", 1, 60), m1)

    return {"h1": h1, "m30": m30, "m15": m15, "m5": m5, "m1": m1}


# ── Option Contract & Option Chart Fetchers (BUG #2 FIXED: TUESDAY EXPIRY) ──

def get_weekly_expiry_fallback():
    """BUG FIX #2: NIFTY weekly/monthly expiry = Tuesday (weekday == 1), NOT Thursday."""
    now = datetime.now(IST)
    days_to_tue = (1 - now.weekday()) % 7
    if days_to_tue == 0 and (now.hour > 15 or (now.hour == 15 and now.minute >= 30)):
        days_to_tue = 7
    return (now + timedelta(days=days_to_tue)).strftime("%Y-%m-%d")


def get_nearest_expiry(state: NiftyState):
    cached = state.get("nearest_expiry_cache")
    if cached and cached.get("fetched_at"):
        try:
            fetched_at = datetime.fromisoformat(cached["fetched_at"])
            if fetched_at.tzinfo is None:
                fetched_at = IST.localize(fetched_at)
            if datetime.now(IST) - fetched_at < timedelta(hours=6):
                return cached["expiry"]
        except Exception:
            pass
    try:
        data = upstox_get(f"{UPSTOX_V2}/option/contract?instrument_key={NIFTY_KEY_ENC}")
        expiries = set()
        if isinstance(data, list):
            expiries = {i.get("expiry") for i in data if isinstance(i, dict) and i.get("expiry")}
        elif isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    expiries |= {i.get("expiry") for i in v if isinstance(i, dict) and i.get("expiry")}
        today = datetime.now(IST).strftime("%Y-%m-%d")
        future = sorted(e for e in expiries if e and e >= today)
        if not future:
            return get_weekly_expiry_fallback()
        nearest = future[0]
        state.set("nearest_expiry_cache",
                  {"expiry": nearest, "fetched_at": datetime.now(IST).isoformat()}, ttl=6 * 3600 + 300)
        return nearest
    except Exception as e:
        log.error(f"[NIFTY] get_nearest_expiry error: {e}")
        return get_weekly_expiry_fallback()


def get_option_instrument_key(state: NiftyState, atm: int, option_type: str, expiry: str):
    cache_key = f"opt_inst_{expiry}_{atm}_{option_type}"
    cached = state.get(cache_key)
    if cached:
        return cached
    try:
        data = upstox_get(f"{UPSTOX_V2}/option/contract?instrument_key={NIFTY_KEY_ENC}&expiry_date={expiry}")
        if not isinstance(data, list):
            return None
        for item in data:
            try:
                strike = int(float(item.get("strike_price", -1)))
            except (TypeError, ValueError):
                continue
            itype = item.get("instrument_type") or item.get("option_type")
            if strike == atm and itype == option_type:
                inst_key = item.get("instrument_key")
                if inst_key:
                    state.set(cache_key, inst_key, ttl=3600)
                return inst_key
        return None
    except Exception as e:
        log.error(f"[NIFTY] get_option_instrument_key error: {e}")
        return None


def fetch_option_candles(instrument_key: str, interval: int = 1):
    """Fetches Option chart candles keeping exact float decimals (e.g. 18.05, 16.50)."""
    enc = quote(instrument_key)
    df = fetch_intraday("minutes", interval, instrument_key_enc=enc, keep_float=True)
    if df.empty or len(df) < 12:
        hist = fetch_historical("minutes", interval, 45, instrument_key_enc=enc, keep_float=True)
        df = _merge_hist_and_intraday(hist, df)
    return df


# ═══════════════════════════════════════════════════════════════════════
# SECTION 3: TIMESTAMP-BASED CANDLE DROP & 15M TREND ENGINE (BUG #6 FIXED)
# ═══════════════════════════════════════════════════════════════════════

def drop_incomplete_candle(df, interval_minutes=5, buffer_sec=8):
    """
    Timestamp-verified incomplete candle filter.
    Drops the last row ONLY if `now < candle_start_ts + interval_minutes + buffer_sec`.
    Never blindly drops a completed candle!
    """
    if df is None or df.empty or len(df) < 2:
        return pd.DataFrame() if df is None else df
    try:
        last_ts = pd.Timestamp(df.iloc[-1]["ts"])
        last_ts = (IST.localize(last_ts.to_pydatetime()) if last_ts.tzinfo is None
                   else last_ts.tz_convert(IST).to_pydatetime())
        if datetime.now(IST) < last_ts + timedelta(minutes=interval_minutes, seconds=buffer_sec):
            return df.iloc[:-1].copy()
    except Exception as e:
        log.error(f"[NIFTY] drop_incomplete_candle error: {e}")
    return df


def compute_15m_trend(m15_df, m5_df=None):
    """
    BUG FIX #6: Computes 15M Trend Structure (HH_HL = UPTREND, LH_LL = DOWNTREND, else SIDEWAYS)
    across 35+ merged multi-day + intraday 15M candles so real EMA9/EMA21 and swing pivots are
    available right from 09:20 AM (never stuck in SIDEWAYS until 10:15 AM).
    """
    df = drop_incomplete_candle(m15_df, 15)
    if df.empty or len(df) < 6:
        return {"trend": "SIDEWAYS", "structure": "UNCLEAR", "last_swing_high": 0, "last_swing_low": 0, "ema9": 0, "ema21": 0}

    # Compute true EMA9 and EMA21 over full 35-45 bar series
    ema9_series = df["c"].ewm(span=9, adjust=False).mean()
    ema21_series = df["c"].ewm(span=21, adjust=False).mean()
    ema9_val = round(float(ema9_series.iloc[-1]), 1)
    ema21_val = round(float(ema21_series.iloc[-1]), 1)

    recent = df.tail(20).reset_index(drop=True)
    swing_highs, swing_lows = [], []
    for i in range(1, len(recent) - 1):
        h_prev, h_curr, h_next = recent.loc[i - 1, "h"], recent.loc[i, "h"], recent.loc[i + 1, "h"]
        l_prev, l_curr, l_next = recent.loc[i - 1, "l"], recent.loc[i, "l"], recent.loc[i + 1, "l"]
        if h_curr >= h_prev and h_curr >= h_next:
            swing_highs.append(int(h_curr))
        if l_curr <= l_prev and l_curr <= l_next:
            swing_lows.append(int(l_curr))

    last_close = int(recent.iloc[-1]["c"])
    prev_close = int(recent.iloc[-4]["c"]) if len(recent) >= 4 else int(recent.iloc[0]["c"])
    net_move = last_close - prev_close

    hh = len(swing_highs) >= 2 and swing_highs[-1] > swing_highs[-2]
    hl = len(swing_lows) >= 2 and swing_lows[-1] > swing_lows[-2]
    lh = len(swing_highs) >= 2 and swing_highs[-1] < swing_highs[-2]
    ll = len(swing_lows) >= 2 and swing_lows[-1] < swing_lows[-2]

    ema_bull = (last_close > ema9_val > ema21_val) and (ema9_val - ema21_val >= 8)
    ema_bear = (last_close < ema9_val < ema21_val) and (ema21_val - ema9_val >= 8)

    if (hh and hl) or (hl and net_move > 30) or (ema_bull and net_move > 25) or net_move > 60:
        trend, structure = "UPTREND", "HH_HL"
    elif (lh and ll) or (lh and net_move < -30) or (ema_bear and net_move < -25) or net_move < -60:
        trend, structure = "DOWNTREND", "LH_LL"
    else:
        trend, structure = "SIDEWAYS", "SIDEWAYS"

    return {
        "trend": trend,
        "structure": structure,
        "last_swing_high": swing_highs[-1] if swing_highs else int(recent["h"].max()),
        "last_swing_low": swing_lows[-1] if swing_lows else int(recent["l"].min()),
        "net_move_1hr": net_move,
        "ema9": ema9_val,
        "ema21": ema21_val,
    }


def is_setup_trend_side(action, trend_15m):
    """Returns 'TREND_SIDE', 'COUNTER_TREND', or 'NEUTRAL_SIDE'."""
    if trend_15m == "UPTREND":
        return "TREND_SIDE" if action == "BUY_CE" else "COUNTER_TREND"
    if trend_15m == "DOWNTREND":
        return "TREND_SIDE" if action == "BUY_PE" else "COUNTER_TREND"
    return "NEUTRAL_SIDE"


# ═══════════════════════════════════════════════════════════════════════
# SECTION 4: AUTO-MATH, MORNING-TO-AFTERNOON (±50 PTS) & 6M MEMORY ZONES
# ═══════════════════════════════════════════════════════════════════════

def _overlap_ratio(a_low, a_high, b_low, b_high):
    ol = max(0, min(a_high, b_high) - max(a_low, b_low))
    return ol / max(min(a_high - a_low, b_high - b_low), 1)


def extract_auto_math_zones(tf_data, ltp):
    """
    Deterministic Zone Extractor:
      1. Previous Trading Day High (PDH), Low (PDL), Close (PDC) -> S2 Setups
      2. 30M & 15M Clean Swing Highs (PUT triggers) & Swing Lows (CALL triggers) -> S1 Setups
      3. Big-Move Origin Zones (Supply/Demand bases where >=45pt displacement started)
    """
    zones = []
    daily = drop_incomplete_candle(tf_data.get("daily", pd.DataFrame()), 375)
    m15   = drop_incomplete_candle(tf_data.get("m15", pd.DataFrame()), 15)

    # 1. Previous Day High / Low / Close (S2 Setups)
    if not daily.empty:
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        last_date = pd.Timestamp(daily.iloc[-1]["ts"]).strftime("%Y-%m-%d")
        pdc_row = daily.iloc[-2] if (last_date == today_str and len(daily) >= 2) else daily.iloc[-1]
        pdh, pdl, pdc = int(pdc_row["h"]), int(pdc_row["l"]), int(pdc_row["c"])

        zones.append({
            "id": "PDH", "type": "RESISTANCE", "low": pdh - 8, "high": pdh + 8,
            "pivot": pdh, "strength": "STRONG", "tags": ["PREV_DAY_HIGH", "S2_LEVEL"],
            "preferred_action": "BUY_PE", "why": f"Previous Day High ({pdh}) liquidity pool (S2)",
            "touch_count": 0, "blue_box_extreme": None, "source": "PYTHON_MATH"
        })
        zones.append({
            "id": "PDL", "type": "SUPPORT", "low": pdl - 8, "high": pdl + 8,
            "pivot": pdl, "strength": "STRONG", "tags": ["PREV_DAY_LOW", "S2_LEVEL"],
            "preferred_action": "BUY_CE", "why": f"Previous Day Low ({pdl}) liquidity pool (S2)",
            "touch_count": 0, "blue_box_extreme": None, "source": "PYTHON_MATH"
        })
        if abs(pdc - pdh) > 25 and abs(pdc - pdl) > 25:
            ztype = "SUPPORT" if ltp >= pdc else "RESISTANCE"
            act = "BUY_CE" if ztype == "SUPPORT" else "BUY_PE"
            zones.append({
                "id": "PDC", "type": ztype, "low": pdc - 7, "high": pdc + 7,
                "pivot": pdc, "strength": "MED", "tags": ["PREV_DAY_CLOSE", "S2_LEVEL"],
                "preferred_action": act, "why": f"Previous Day Close ({pdc}) reference level (S2)",
                "touch_count": 0, "blue_box_extreme": None, "source": "PYTHON_MATH"
            })

    # 2. 15M Swing Highs / Swing Lows (S1 Setups)
    if not m15.empty and len(m15) >= 8:
        df15 = m15.tail(35).reset_index(drop=True)
        sh_idx, sl_idx = 1, 1
        for i in range(2, len(df15) - 2):
            h_curr = int(df15.loc[i, "h"])
            l_curr = int(df15.loc[i, "l"])
            if (h_curr >= df15.loc[i - 1, "h"] and h_curr >= df15.loc[i - 2, "h"]
                    and h_curr >= df15.loc[i + 1, "h"] and h_curr >= df15.loc[i + 2, "h"]):
                if abs(h_curr - ltp) <= 350:
                    ztype = "RESISTANCE" if ltp <= h_curr + 10 else "FLIP_SUPPORT"
                    act = "BUY_PE" if ztype == "RESISTANCE" else "BUY_CE"
                    zones.append({
                        "id": f"SW_H{sh_idx}", "type": ztype, "low": h_curr - 7, "high": h_curr + 7,
                        "pivot": h_curr, "strength": "STRONG", "tags": ["15M_SWING_HIGH", "S1_LEVEL"],
                        "preferred_action": act, "why": f"15M Swing High liquidity line ({h_curr})",
                        "touch_count": 0, "blue_box_extreme": None, "source": "PYTHON_MATH"
                    })
                    sh_idx += 1
            if (l_curr <= df15.loc[i - 1, "l"] and l_curr <= df15.loc[i - 2, "l"]
                    and l_curr <= df15.loc[i + 1, "l"] and l_curr <= df15.loc[i + 2, "l"]):
                if abs(l_curr - ltp) <= 350:
                    ztype = "SUPPORT" if ltp >= l_curr - 10 else "FLIP_RESISTANCE"
                    act = "BUY_CE" if ztype == "SUPPORT" else "BUY_PE"
                    zones.append({
                        "id": f"SW_L{sl_idx}", "type": ztype, "low": l_curr - 7, "high": l_curr + 7,
                        "pivot": l_curr, "strength": "STRONG", "tags": ["15M_SWING_LOW", "S1_LEVEL"],
                        "preferred_action": act, "why": f"15M Swing Low liquidity line ({l_curr})",
                        "touch_count": 0, "blue_box_extreme": None, "source": "PYTHON_MATH"
                    })
                    sl_idx += 1

    # 3. Clean Origin Zones (where >=45pt displacement started)
    if not m15.empty and len(m15) >= 10:
        df15 = m15.tail(35).reset_index(drop=True)
        orig_idx = 1
        for i in range(1, len(df15) - 3):
            move_3 = int(df15.loc[i + 2, "c"]) - int(df15.loc[i, "o"])
            if move_3 <= -ORIGIN_MIN_MOVE_PTS:
                base_lvl = int(df15.loc[i, "o"])
                if abs(base_lvl - ltp) <= 350:
                    zones.append({
                        "id": f"ORIG_PE{orig_idx}", "type": "RESISTANCE",
                        "low": base_lvl - 9, "high": base_lvl + 9, "pivot": base_lvl,
                        "strength": "STRONG", "tags": ["CLEAN_ORIGIN", "BREAKDOWN_ORIGIN", "S2_LEVEL"],
                        "preferred_action": "BUY_PE",
                        "why": f"Breakdown Origin ({base_lvl}) where {abs(move_3)}pt drop started",
                        "touch_count": 0, "blue_box_extreme": None, "source": "PYTHON_MATH"
                    })
                    orig_idx += 1
            elif move_3 >= ORIGIN_MIN_MOVE_PTS:
                base_lvl = int(df15.loc[i, "o"])
                if abs(base_lvl - ltp) <= 350:
                    zones.append({
                        "id": f"ORIG_CE{orig_idx}", "type": "SUPPORT",
                        "low": base_lvl - 9, "high": base_lvl + 9, "pivot": base_lvl,
                        "strength": "STRONG", "tags": ["CLEAN_ORIGIN", "RALLY_ORIGIN", "S2_LEVEL"],
                        "preferred_action": "BUY_CE",
                        "why": f"Rally Origin ({base_lvl}) where +{move_3}pt rally started",
                        "touch_count": 0, "blue_box_extreme": None, "source": "PYTHON_MATH"
                    })
                    orig_idx += 1

    return deduplicate_and_merge_zones(zones, [], ltp)


def fetch_selective_historical_memory_zones(state: NiftyState, ltp: float):
    """
    SELECTIVE 6-MONTH PRICE-RANGE MEMORY FETCHER (±50 pts Window):
    - Only selects genuinely major multi-week / 5-day structural turning points within
      [LTP - 50, LTP + 50] that triggered a massive >=120 pt institutional reversal within 3 sessions
      (prevents tagging normal everyday 80pt swings as institutional zones).
    """
    cached = state.get("hist_memory_bank")
    if cached and abs(cached.get("center_ltp", 0) - ltp) <= 25:
        return cached.get("zones", [])

    daily_6m = drop_incomplete_candle(fetch_historical("days", 1, 180, days_override=185), 375)
    selected_zones = []
    low_bound = ltp - PRICE_RANGE_WINDOW_PTS
    high_bound = ltp + PRICE_RANGE_WINDOW_PTS

    if not daily_6m.empty and len(daily_6m) >= 12:
        for i in range(5, len(daily_6m) - 3):
            row = daily_6m.iloc[i]
            d_str = pd.Timestamp(row["ts"]).strftime("%Y-%m-%d")
            h_val, l_val = int(row["h"]), int(row["l"])

            # Must be a true 10-day structural extreme AND cause >= 120 pt displacement
            if low_bound <= l_val <= high_bound:
                window_min = int(daily_6m.iloc[i - 5:i + 4]["l"].min())
                if l_val == window_min:
                    next_bounce = int(daily_6m.iloc[i:min(len(daily_6m), i + 4)]["h"].max()) - l_val
                    if next_bounce >= HIST_MIN_DISPLACEMENT:
                        selected_zones.append({
                            "id": f"HIST_SUP_{l_val}",
                            "type": "SUPPORT",
                            "low": l_val - 7,
                            "high": l_val + 7,
                            "pivot": l_val,
                            "strength": "STRONG",
                            "tags": ["HISTORICAL_MEMORY_ZONE", "S2_LEVEL"],
                            "preferred_action": "BUY_CE",
                            "why": f"6M Institutional Memory Support ({d_str} @ {l_val}): +{next_bounce}pt reversal",
                            "touch_count": 0,
                            "blue_box_extreme": None,
                            "source": "HIST_6M_MEMORY"
                        })

            if low_bound <= h_val <= high_bound:
                window_max = int(daily_6m.iloc[i - 5:i + 4]["h"].max())
                if h_val == window_max:
                    next_drop = h_val - int(daily_6m.iloc[i:min(len(daily_6m), i + 4)]["l"].min())
                    if next_drop >= HIST_MIN_DISPLACEMENT:
                        selected_zones.append({
                            "id": f"HIST_RES_{h_val}",
                            "type": "RESISTANCE",
                            "low": h_val - 7,
                            "high": h_val + 7,
                            "pivot": h_val,
                            "strength": "STRONG",
                            "tags": ["HISTORICAL_MEMORY_ZONE", "S2_LEVEL"],
                            "preferred_action": "BUY_PE",
                            "why": f"6M Institutional Memory Resistance ({d_str} @ {h_val}): -{next_drop}pt drop",
                            "touch_count": 0,
                            "blue_box_extreme": None,
                            "source": "HIST_6M_MEMORY"
                        })

    state.set("hist_memory_bank", {"center_ltp": ltp, "zones": selected_zones[:4]}, ttl=3600)
    return selected_zones[:4]


def sync_morning_session_boxes_for_afternoon(m5_df, ltp):
    """
    MORNING-TO-AFTERNOON MEMORY SYNC (±50 pts Window):
    - Uses ONLY completed 5M candles (`drop_incomplete_candle(m5_df, 5)`).
    - Records Morning Session (09:15 to 11:30) liquidity boxes & swing lows/highs.
    - When afternoon price returns within ±50 pts of a morning box, marks it with
      `touch_count=1` and `blue_box_extreme=morning_level` so a 2nd sweep in the
      afternoon triggers an immediate S1 2nd-Hunt (Double-Box) setup!
    """
    morning_zones = []
    m5_clean = drop_incomplete_candle(m5_df, 5)
    if m5_clean.empty or len(m5_clean) < 6:
        return morning_zones

    now_ist = datetime.now(IST)
    # Only activate Morning-to-Afternoon memory after 10:30 AM
    if now_ist.hour < 10 or (now_ist.hour == 10 and now_ist.minute < 30):
        return morning_zones

    df = m5_clean.copy()
    df["ist_time"] = df["ts"].dt.tz_convert(IST).dt.strftime("%H:%M")
    today_str = now_ist.strftime("%Y-%m-%d")
    df["ist_date"] = df["ts"].dt.tz_convert(IST).dt.strftime("%Y-%m-%d")
    morn_df = df[(df["ist_date"] == today_str) & (df["ist_time"] >= "09:15") & (df["ist_time"] <= "11:30")].reset_index(drop=True)

    if len(morn_df) < 4:
        return morning_zones

    # 1. Early Morning Opening Liquidity Box (09:15 - 09:35)
    early_df = morn_df[morn_df["ist_time"] <= "09:35"]
    if not early_df.empty:
        morn_low = int(early_df["l"].min())
        morn_high = int(early_df["h"].max())
        if abs(ltp - morn_low) <= PRICE_RANGE_WINDOW_PTS:
            morning_zones.append({
                "id": f"MORN_BOX_L_{morn_low}", "type": "SUPPORT",
                "low": morn_low - 6, "high": morn_low + 6, "pivot": morn_low,
                "strength": "STRONG", "tags": ["MORNING_MEMORY_BOX", "1ST_LIQUIDITY_READY", "S1_LEVEL"],
                "preferred_action": "BUY_CE",
                "why": f"Morning 09:15-09:35 Liquidity Box Low ({morn_low}) — revisited within ±50pts for 2nd Box Hunt",
                "touch_count": 1, "blue_box_extreme": morn_low, "source": "MORNING_SYNC"
            })
        if abs(ltp - morn_high) <= PRICE_RANGE_WINDOW_PTS:
            morning_zones.append({
                "id": f"MORN_BOX_H_{morn_high}", "type": "RESISTANCE",
                "low": morn_high - 6, "high": morn_high + 6, "pivot": morn_high,
                "strength": "STRONG", "tags": ["MORNING_MEMORY_BOX", "1ST_LIQUIDITY_READY", "S1_LEVEL"],
                "preferred_action": "BUY_PE",
                "why": f"Morning 09:15-09:35 Liquidity Box High ({morn_high}) — revisited within ±50pts for 2nd Box Hunt",
                "touch_count": 1, "blue_box_extreme": morn_high, "source": "MORNING_SYNC"
            })

    # 2. Morning 5M Swing Lows / Highs that bounced >= 30 pts
    for i in range(1, len(morn_df) - 2):
        l_c, h_c = int(morn_df.loc[i, "l"]), int(morn_df.loc[i, "h"])
        t_lbl = morn_df.loc[i, "ist_time"]
        after = morn_df.iloc[i + 1:]

        if l_c <= morn_df.loc[i - 1, "l"] and l_c <= morn_df.loc[i + 1, "l"]:
            bounce = int(after["h"].max()) - l_c
            if bounce >= BLUE_BOX_BOUNCE_PTS and abs(ltp - l_c) <= PRICE_RANGE_WINDOW_PTS:
                morning_zones.append({
                    "id": f"MORN_SWL_{l_c}", "type": "SUPPORT",
                    "low": l_c - 6, "high": l_c + 6, "pivot": l_c,
                    "strength": "STRONG", "tags": ["MORNING_MEMORY_BOX", "1ST_LIQUIDITY_READY", "S1_LEVEL"],
                    "preferred_action": "BUY_CE",
                    "why": f"Morning ({t_lbl}) Swing Low {l_c} (bounced +{bounce}pts) — ready for 2nd Hunt",
                    "touch_count": 1, "blue_box_extreme": l_c, "source": "MORNING_SYNC"
                })

        if h_c >= morn_df.loc[i - 1, "h"] and h_c >= morn_df.loc[i + 1, "h"]:
            drop = h_c - int(after["l"].min())
            if drop >= BLUE_BOX_BOUNCE_PTS and abs(ltp - h_c) <= PRICE_RANGE_WINDOW_PTS:
                morning_zones.append({
                    "id": f"MORN_SWH_{h_c}", "type": "RESISTANCE",
                    "low": h_c - 6, "high": h_c + 6, "pivot": h_c,
                    "strength": "STRONG", "tags": ["MORNING_MEMORY_BOX", "1ST_LIQUIDITY_READY", "S1_LEVEL"],
                    "preferred_action": "BUY_PE",
                    "why": f"Morning ({t_lbl}) Swing High {h_c} (dropped -{drop}pts) — ready for 2nd Hunt",
                    "touch_count": 1, "blue_box_extreme": h_c, "source": "MORNING_SYNC"
                })

    return morning_zones


def update_live_intraday_zones(state: NiftyState, existing_zones, tf_data, ltp):
    """
    Runs EVERY 5 MINUTES in live market (0 AI calls):
      1. Detects fresh Intraday 5M Swings that bounced >= 30 pts -> marks Blue Box (touch_count=1).
      2. Syncs Morning-to-Afternoon boxes within ±50 pts.
      3. Syncs 6-Month Selective Historical Institutional Memory zones within ±50 pts.
      4. Detects Live S3 Sideways Compression Boxes (5+ candles within 12–35 pts range).
    """
    m5 = drop_incomplete_candle(tf_data.get("m5", pd.DataFrame()), 5)
    if m5.empty or len(m5) < 8:
        return existing_zones

    new_live = []
    recent = m5.tail(22).reset_index(drop=True)

    # 1. Live 5M Swing High / Low with >= 30pt bounce (Blue Box / 1st Liquidity)
    for i in range(2, len(recent) - 3):
        l_curr = int(recent.loc[i, "l"])
        h_curr = int(recent.loc[i, "h"])
        after_slice = recent.iloc[i + 1:]

        if l_curr <= recent.loc[i - 1, "l"] and l_curr <= recent.loc[i - 2, "l"] and l_curr <= recent.loc[i + 1, "l"]:
            max_bounce = int(after_slice["h"].max()) - l_curr
            min_after = int(after_slice["l"].min())
            if max_bounce >= BLUE_BOX_BOUNCE_PTS and min_after >= l_curr - 3:
                new_live.append({
                    "id": f"LIVE_L_{l_curr}", "type": "SUPPORT",
                    "low": l_curr - 6, "high": l_curr + 6, "pivot": l_curr,
                    "strength": "STRONG", "tags": ["LIVE_BLUE_BOX", "1ST_LIQUIDITY_READY", "S1_LEVEL"],
                    "preferred_action": "BUY_CE",
                    "why": f"Live Intraday Swing Low ({l_curr}) bounced +{max_bounce}pts — 1st Liquidity (Blue Box) ready for 2nd Hunt!",
                    "touch_count": 1, "blue_box_extreme": l_curr, "source": "LIVE_PYTHON"
                })

        if h_curr >= recent.loc[i - 1, "h"] and h_curr >= recent.loc[i - 2, "h"] and h_curr >= recent.loc[i + 1, "h"]:
            max_drop = h_curr - int(after_slice["l"].min())
            max_after = int(after_slice["h"].max())
            if max_drop >= BLUE_BOX_BOUNCE_PTS and max_after <= h_curr + 3:
                new_live.append({
                    "id": f"LIVE_H_{h_curr}", "type": "RESISTANCE",
                    "low": h_curr - 6, "high": h_curr + 6, "pivot": h_curr,
                    "strength": "STRONG", "tags": ["LIVE_BLUE_BOX", "1ST_LIQUIDITY_READY", "S1_LEVEL"],
                    "preferred_action": "BUY_PE",
                    "why": f"Live Intraday Swing High ({h_curr}) dropped -{max_drop}pts — 1st Liquidity (Blue Box) ready for 2nd Hunt!",
                    "touch_count": 1, "blue_box_extreme": h_curr, "source": "LIVE_PYTHON"
                })

    # 2. Morning-to-Afternoon Memory Sync (±50 pts)
    new_live.extend(sync_morning_session_boxes_for_afternoon(m5, ltp))

    # 3. Selective 6-Month Historical Memory Sync (±50 pts)
    new_live.extend(fetch_selective_historical_memory_zones(state, ltp))

    # 4. Live S3 Sideways Compression Box Detection (5+ completed candles within 12-35 pts range)
    if len(recent) >= SIDEWAYS_MIN_CANDLES + 1:
        box_slice = recent.iloc[-(SIDEWAYS_MIN_CANDLES + 1):-1]
        box_h, box_l = int(box_slice["h"].max()), int(box_slice["l"].min())
        box_rng = box_h - box_l
        if 12 <= box_rng <= SIDEWAYS_MAX_RANGE:
            state.set("active_s3_box", {
                "low": box_l, "high": box_h, "range": box_rng,
                "updated_at": datetime.now(IST).strftime("%H:%M")
            }, ttl=1800)

    if not new_live:
        return existing_zones

    merged = deduplicate_and_merge_zones(existing_zones, new_live, ltp)
    save_zones(state, merged)
    return merged


def normalize_zone_types(zones, ltp):
    out = []
    for z in zones:
        z = dict(z)
        try:
            z["low"], z["high"] = int(float(z.get("low", 0))), int(float(z.get("high", 0)))
        except (TypeError, ValueError):
            continue
        if z["high"] < z["low"]:
            continue
        if z["high"] == z["low"]:
            z["low"] -= 6
            z["high"] += 6
        z.setdefault("pivot", round((z["low"] + z["high"]) / 2))
        z.setdefault("touch_count", 0)
        z.setdefault("blue_box_extreme", None)
        z.setdefault("tags", [])
        if z.get("type") == "FLIP":
            z["type"] = "FLIP_SUPPORT" if ltp >= z["low"] else "FLIP_RESISTANCE"
        out.append(z)
    return out


def deduplicate_and_merge_zones(base_zones, extra_zones, ltp):
    combined = normalize_zone_types(base_zones, ltp) + normalize_zone_types(extra_zones, ltp)
    merged = []
    for cand in combined:
        if abs(cand["pivot"] - ltp) > 450 and "S2_LEVEL" not in cand.get("tags", []):
            continue
        found_overlap = False
        for existing in merged:
            if _overlap_ratio(existing["low"], existing["high"], cand["low"], cand["high"]) >= 0.45 or abs(existing["pivot"] - cand["pivot"]) <= 12:
                found_overlap = True
                existing["touch_count"] = max(existing.get("touch_count", 0), cand.get("touch_count", 0))
                if cand.get("blue_box_extreme") and not existing.get("blue_box_extreme"):
                    existing["blue_box_extreme"] = cand["blue_box_extreme"]
                existing["tags"] = list(set(existing.get("tags", []) + cand.get("tags", [])))
                if cand.get("strength") == "STRONG":
                    existing["strength"] = "STRONG"
                break
        if not found_overlap:
            merged.append(cand)

    merged.sort(key=lambda z: abs(z["pivot"] - ltp))
    return merged[:MAX_ZONES]


# ═══════════════════════════════════════════════════════════════════════
# SECTION 5: CORE SETUP & LIQUIDITY HUNT ENGINE (BUGS #1 & #5 FIXED)
# ═══════════════════════════════════════════════════════════════════════

def zone_buffer(zone):
    return max(8, round((zone.get("high", 0) - zone.get("low", 0)) * 0.20))


def get_touched_zone(ltp, zones, extra_proximity=0):
    tradable, no_trade = [], []
    for z in zones:
        low, high = z.get("low", 0), z.get("high", 0)
        buf = zone_buffer(z) + extra_proximity
        if not (low - buf <= ltp <= high + buf):
            continue
        dist = abs(z.get("pivot", (low + high) / 2) - ltp)
        (no_trade if z.get("type") in ("NO_TRADE", "SIDEWAYS") else tradable).append((dist, z))
    pool = tradable or no_trade
    return min(pool, key=lambda x: x[0])[1] if pool else None


def is_price_near_any_zone(state: NiftyState, ltp, zones, proximity_pts=TURBO_1M_PROXIMITY_PTS):
    """Used by the 1-Minute Scanner: returns True if LTP is within ±50 pts of any active zone or S3 box."""
    if get_touched_zone(ltp, zones, extra_proximity=proximity_pts) is not None:
        return True
    s3_box = state.get("active_s3_box")
    if s3_box and (s3_box["low"] - proximity_pts <= ltp <= s3_box["high"] + proximity_pts):
        return True
    return False


def detect_synthetic_or_single_sweep(df, level_low, level_high, side, pivot=None):
    """
    BUG FIX #5: Strict Inequality Sweep Detection!
      - For BUY_CE (Support): `low` MUST strictly pierce below `level_low` (or strictly `< pivot`),
        and `close` must reclaim `> level_low` (or `>= pivot`). A low of 24001 on a 24000 zone will
        NEVER be falsely reported as "swept/broke 24000"!
      - For BUY_PE (Resistance): `high` MUST strictly pierce above `level_high` (or strictly `> pivot`),
        and `close` must reject `< level_high` (or `<= pivot`).
      - Detects Single-Candle Wick Sweeps, 2-Candle Marubozu Synthetic Wicks, and 3-Candle Sweeps.
    """
    if df is None or df.empty or len(df) < 3:
        return False, 0, None, 0, "no data"

    c1 = df.iloc[-1]
    c2 = df.iloc[-2]
    c3 = df.iloc[-3]

    o1, h1, l1, cl1 = int(c1["o"]), int(c1["h"]), int(c1["l"]), int(c1["c"])
    o2, h2, l2, cl2 = int(c2["o"]), int(c2["h"]), int(c2["l"]), int(c2["c"])
    o3, h3, l3, cl3 = int(c3["o"]), int(c3["h"]), int(c3["l"]), int(c3["c"])

    rng1 = max(h1 - l1, 1)
    dn_wick1 = min(o1, cl1) - l1
    up_wick1 = h1 - max(o1, cl1)

    sup_ref = int(pivot) if pivot is not None else int(level_low)
    res_ref = int(pivot) if pivot is not None else int(level_high)

    if side == "BUY_CE":
        # A. Single-candle bullish sweep & reclaim: l1 MUST strictly break < sup_ref (or < level_low)
        if l1 < sup_ref and cl1 > sup_ref and (cl1 > o1 or dn_wick1 >= rng1 * REJECTION_WICK_RATIO):
            return True, l1, "SINGLE_CANDLE_SWEEP", l1, f"Wick strictly swept {l1} below {sup_ref} & reclaimed to {cl1}"

        # B. 2-Candle Marubozu / Synthetic Wick Reclaim
        low_2c = min(l1, l2)
        if low_2c < sup_ref and cl1 > o1 and cl1 > sup_ref and (cl1 - o1) >= 4:
            return True, low_2c, "MARUBOZU_2C_SYNTHETIC_SWEEP", low_2c, (
                f"2-Candle Synthetic Wick: swept {low_2c} below {sup_ref} & Green candle reclaimed {cl1}"
            )

        # C. 3-Candle Synthetic Sweep
        low_3c = min(l1, l2, l3)
        if low_3c < level_low and cl1 > o1 and cl1 > sup_ref and (cl1 - o1) >= 6:
            return True, low_3c, "SYNTHETIC_3C_SWEEP", low_3c, (
                f"3-Candle Sweep & Reclaim: low {low_3c} below {level_low} reclaimed by Green close {cl1}"
            )

    elif side == "BUY_PE":
        # A. Single-candle bearish sweep & reject: h1 MUST strictly break > res_ref (or > level_high)
        if h1 > res_ref and cl1 < res_ref and (cl1 < o1 or up_wick1 >= rng1 * REJECTION_WICK_RATIO):
            return True, h1, "SINGLE_CANDLE_SWEEP", h1, f"Wick strictly swept {h1} above {res_ref} & rejected to {cl1}"

        # B. 2-Candle Marubozu / Synthetic Wick Reject
        high_2c = max(h1, h2)
        if high_2c > res_ref and cl1 < o1 and cl1 < res_ref and (o1 - cl1) >= 4:
            return True, high_2c, "MARUBOZU_2C_SYNTHETIC_SWEEP", high_2c, (
                f"2-Candle Synthetic Wick: spiked to {high_2c} above {res_ref} & Red candle rejected to {cl1}"
            )

        # C. 3-Candle Synthetic Reject
        high_3c = max(h1, h2, h3)
        if high_3c > level_high and cl1 < o1 and cl1 < res_ref and (o1 - cl1) >= 6:
            return True, high_3c, "SYNTHETIC_3C_SWEEP", high_3c, (
                f"3-Candle Sweep & Reject: high {high_3c} above {level_high} rejected by Red close {cl1}"
            )

    return False, 0, None, 0, "no strict level break yet"


def check_pre_hunt_pause(m5_df):
    if m5_df is None or len(m5_df) < 6:
        return False, ""
    pre = m5_df.iloc[-6:-1]
    rng = int(pre["h"].max()) - int(pre["l"].min())
    if rng <= SIDEWAYS_MAX_RANGE:
        return True, f"Pre-hunt pause ({rng}pt tight box) before sweep"
    return False, ""


def analyze_option_chart_confirmation(state: NiftyState, ltp: float, action: str, require_2nd_hunt: bool = False):
    """
    BUG FIX #1: REAL STRUCTURAL OPTION SL (NO FAKE CLAMPING!) + 1ST GREEN CANDLE WINDOW:
      - Uses timestamp-based `drop_incomplete_candle()` on both 1M and 5M Option charts.
      - Enforces 1st/2nd Green Candle Entry Window: if 3+ consecutive green candles have already
        printed on the option chart, rejects as late chase ("Unch bhavala entry nahi").
      - Places `opt_sl` strictly below the entry Green candle's low (or 2-candle sweep low) minus ₹0.50 buffer.
      - NEVER clamps `opt_risk` to fake a small SL! If real structural `opt_risk > MAX_OPTION_RISK_PTS`
        (default ₹15), returns `confirmed=False` so a blown-out wide-SL candle is skipped!
    """
    side = "CE" if action == "BUY_CE" else "PE" if action == "BUY_PE" else None
    if not side or not ltp:
        return {"confirmed": False, "reason": "invalid side/ltp"}

    atm = round(ltp / 50) * 50
    expiry = get_nearest_expiry(state)
    inst_key = get_option_instrument_key(state, atm, side, expiry)
    if not inst_key:
        return {"confirmed": False, "reason": f"could not resolve {atm} {side}"}

    opt_1m = drop_incomplete_candle(fetch_option_candles(inst_key, interval=1), 1, buffer_sec=4)
    opt_5m = drop_incomplete_candle(fetch_option_candles(inst_key, interval=5), 5, buffer_sec=6)

    df = opt_1m if (not opt_1m.empty and len(opt_1m) >= 8) else opt_5m
    tf_label = "1M" if (not opt_1m.empty and len(opt_1m) >= 8) else "5M"
    if df.empty or len(df) < 6:
        return {"confirmed": False, "reason": "insufficient option candles"}

    d = df.tail(OPTION_LOOKBACK).reset_index(drop=True)
    last = d.iloc[-1]
    prev = d.iloc[-2]
    prev2 = d.iloc[-3]

    o_last, h_last, l_last, c_last = float(last["o"]), float(last["h"]), float(last["l"]), float(last["c"])
    o_prev, h_prev, l_prev, c_prev = float(prev["o"]), float(prev["h"]), float(prev["l"]), float(prev["c"])
    o_prev2, c_prev2 = float(prev2["o"]), float(prev2["c"])

    # 1. Check Green confirmation candle with meaningful body (>= 2% of premium or >= ₹0.60, closing in top half)
    min_body = max(0.60, round(c_last * 0.018, 2))
    body = round(c_last - o_last, 2)
    is_green_now = (body >= min_body) and (c_last >= l_last + (h_last - l_last) * 0.50)

    if not is_green_now:
        return {
            "confirmed": False,
            "strike": f"{atm} {side}",
            "tf": tf_label,
            "reason": f"{atm} {side} ({tf_label}) not a decisive green candle (O:{o_last} C:{c_last}, min_body:{min_body})"
        }

    # 2. Enforce 1st / 2nd Green Candle Entry Window (reject if 3 consecutive green candles already ran)
    if (c_prev > o_prev) and (c_prev2 > o_prev2) and (c_last - o_prev2 > c_last * 0.12):
        return {
            "confirmed": False,
            "strike": f"{atm} {side}",
            "tf": tf_label,
            "reason": f"{atm} {side} ({tf_label}) 1st-Green-Candle window missed (3 green candles already ran)"
        }

    # 3. Prior swing low on Option premium chart (excluding last 2 candles)
    prior_window = d.iloc[:-2]
    swing_low = float(prior_window["l"].min()) if not prior_window.empty else l_prev
    recent_dip_low = min(l_last, l_prev, float(prev2["l"]))
    swept_swing_low = recent_dip_low <= swing_low * 1.03

    if require_2nd_hunt and not swept_swing_low:
        return {
            "confirmed": False,
            "strike": f"{atm} {side}",
            "tf": tf_label,
            "swept_swing_low": False,
            "reason": f"{atm} {side} ({tf_label}) green candle present, but did not sweep/test prior option low ₹{swing_low}"
        }

    # 4. REAL Structural SL = strictly below the entry Green candle's low (or dip low if adjacent)
    opt_entry = round(c_last, 2)
    structural_low = min(l_last, l_prev) if (l_last - l_prev) <= 2.0 else l_last
    opt_sl = round(max(0.50, structural_low - 0.50), 2)
    opt_risk = round(opt_entry - opt_sl, 2)

    # Guard against wide/spiky SL without clamping!
    if opt_risk <= 0.40:
        opt_risk = 0.50
        opt_sl = round(opt_entry - opt_risk, 2)
    elif opt_risk > MAX_OPTION_RISK_PTS:
        return {
            "confirmed": False,
            "strike": f"{atm} {side}",
            "tf": tf_label,
            "reason": f"Real structural Option SL too wide (Entry ₹{opt_entry} - SL ₹{opt_sl} = ₹{opt_risk} > max ₹{MAX_OPTION_RISK_PTS}) — skipping late/wide candle!"
        }

    breakeven_1_1x = round(opt_entry + 1.1 * opt_risk, 2)
    target_1_2 = round(opt_entry + 2.0 * opt_risk, 2)
    target_1_35 = round(opt_entry + 3.5 * opt_risk, 2)

    hunt_note = (f"Option swept/tested low ₹{swing_low:.2f} (dip ₹{recent_dip_low:.2f}) & closed 1st Green at ₹{opt_entry:.2f}"
                 if swept_swing_low else f"Option 1st Green candle closed at ₹{opt_entry:.2f} above structural low ₹{structural_low:.2f}")

    return {
        "confirmed": True,
        "strike": f"{atm} {side}",
        "tf": tf_label,
        "opt_entry": opt_entry,
        "opt_sl": opt_sl,
        "opt_risk": opt_risk,
        "breakeven_alert": breakeven_1_1x,
        "opt_target_1_2": target_1_2,
        "opt_target_big": target_1_35,
        "swept_swing_low": swept_swing_low,
        "reason": f"[{atm} {side} {tf_label}] {hunt_note} | Real SL: ₹{opt_sl} (Risk ₹{opt_risk}) | 1.1x BE: ₹{breakeven_1_1x}",
    }


def evaluate_zone_setup(state: NiftyState, tf_data, ltp, zone, trend_info):
    """
    Master Setup Evaluator for S1 & S2 at a touched zone:
      - Setups are identified on 5M / 15M structure (or 09:20–09:40 Opening 1M Sniper).
      - S2: Prev-Day High/Low/Close (PDH/PDL/PDC), Clean Origin, or 6M Institutional Memory Zone.
      - S1 Trend-Side (aligned with 15M trend): Fast entry on 1st hunt + Option Green candle.
      - S1 Counter-Trend (against 15M trend): 1st touch records Blue Box (1st Liquidity) and WAITS!
        Requires 2nd Hunt (Yellow Box) OR Pre-Hunt Sideways Pause + Option Green candle.
    """
    m15 = drop_incomplete_candle(tf_data.get("m15", pd.DataFrame()), 15)
    m5  = drop_incomplete_candle(tf_data.get("m5", pd.DataFrame()), 5)
    m1  = drop_incomplete_candle(tf_data.get("m1", pd.DataFrame()), 1, buffer_sec=4)

    z_id = zone.get("id", "?")
    z_low, z_high = zone.get("low", 0), zone.get("high", 0)
    z_pivot = zone.get("pivot", round((z_low + z_high) / 2))
    z_type = zone.get("type", "")
    tags = zone.get("tags", [])
    touch_count = zone.get("touch_count", 0)
    blue_extreme = zone.get("blue_box_extreme")

    if z_type in ("SUPPORT", "FLIP_SUPPORT"):
        action = "BUY_CE"
    elif z_type in ("RESISTANCE", "FLIP_RESISTANCE"):
        action = "BUY_PE"
    else:
        action = zone.get("preferred_action", "WAIT")
    if action not in ("BUY_CE", "BUY_PE"):
        return {"status": "NO_SETUP", "reason": "zone has no directional action"}

    trend_15m = trend_info.get("trend", "SIDEWAYS")
    hunt_side = is_setup_trend_side(action, trend_15m)
    is_s2_zone = any(t in tags for t in ("PREV_DAY_HIGH", "PREV_DAY_LOW", "PREV_DAY_CLOSE", "S2_LEVEL", "CLEAN_ORIGIN", "HISTORICAL_MEMORY_ZONE"))

    # Primary structural sweep check on 5M and 15M; 1M allowed when zone is already pre-primed (touch_count >= 1 or morning session)
    swept_5m, ext_5m, pat_5m, sl_5m, det_5m = detect_synthetic_or_single_sweep(m5, z_low, z_high, action, pivot=z_pivot)
    swept_15m, ext_15m, pat_15m, sl_15m, det_15m = detect_synthetic_or_single_sweep(m15, z_low, z_high, action, pivot=z_pivot)
    swept_1m, ext_1m, pat_1m, sl_1m, det_1m = detect_synthetic_or_single_sweep(m1, z_low, z_high, action, pivot=z_pivot)

    now_str = datetime.now(IST).strftime("%H:%M")
    allow_1m_structural = (touch_count >= 1) or ("09:20" <= now_str <= "09:45") or is_s2_zone

    if swept_5m:
        trig_tf, sweep_ext, pat_name, idx_sl, sweep_detail = "5M", ext_5m, pat_5m, sl_5m, det_5m
    elif swept_15m:
        trig_tf, sweep_ext, pat_name, idx_sl, sweep_detail = "15M", ext_15m, pat_15m, sl_15m, det_15m
    elif swept_1m and allow_1m_structural:
        trig_tf, sweep_ext, pat_name, idx_sl, sweep_detail = "1M", ext_1m, pat_1m, sl_1m, det_1m
    else:
        return {"status": "NO_SETUP", "reason": "Whale Trap Guard: Zone not strictly swept/reclaimed yet"}

    # Distance from sweep extreme check (No chasing runaway candles)
    chase_dist = abs(ltp - sweep_ext)
    if chase_dist > MAX_INDEX_CHASE_PTS:
        return {"status": "NO_SETUP", "reason": f"Price already ran {chase_dist:.0f}pts from sweep extreme {sweep_ext} (> {MAX_INDEX_CHASE_PTS}pts)"}

    has_pause, pause_detail = check_pre_hunt_pause(m5)
    opt_check = analyze_option_chart_confirmation(state, ltp, action, require_2nd_hunt=False)

    # ── CASE 1: S2 SETUP (Prev-Day High / Low / Origin / 6M Memory Zone Hunt) ──
    if is_s2_zone:
        if not opt_check.get("confirmed"):
            return {"status": "WAITING_OPTION_GREEN", "reason": f"S2 Index swept ({sweep_detail}), waiting for Option Green: {opt_check.get('reason')}"}
        update_single_zone_state(state, z_id, touch_count=touch_count + 1, blue_box_extreme=sweep_ext)
        return {
            "status": "TRIGGERED",
            "setup_code": "S2",
            "setup_name": "S2 (Prev-Day / Origin / Memory Level Liquidity Hunt)",
            "action": action,
            "hunt_side": hunt_side,
            "trigger_tf": trig_tf,
            "pattern": pat_name,
            "sweep_extreme": sweep_ext,
            "chase_distance_pts": round(chase_dist, 1),
            "index_sl": idx_sl - REF_SL_BUFFER if action == "BUY_CE" else idx_sl + REF_SL_BUFFER,
            "sweep_detail": f"{sweep_detail} ({pause_detail})" if has_pause else sweep_detail,
            "option_info": opt_check,
        }

    # ── CASE 2: S1 TREND-SIDE HUNT (Rulebook: 1st hunt chalto, fast entry!) ──
    if hunt_side == "TREND_SIDE":
        if not opt_check.get("confirmed"):
            return {"status": "WAITING_OPTION_GREEN", "reason": f"Trend-side S1 swept ({sweep_detail}), waiting for Option Green: {opt_check.get('reason')}"}
        update_single_zone_state(state, z_id, touch_count=touch_count + 1, blue_box_extreme=sweep_ext)
        return {
            "status": "TRIGGERED",
            "setup_code": "S1",
            "setup_name": "S1 (Trend-Side Fast Liquidity Hunt)",
            "action": action,
            "hunt_side": "TREND_SIDE",
            "trigger_tf": trig_tf,
            "pattern": pat_name,
            "sweep_extreme": sweep_ext,
            "chase_distance_pts": round(chase_dist, 1),
            "index_sl": idx_sl - REF_SL_BUFFER if action == "BUY_CE" else idx_sl + REF_SL_BUFFER,
            "sweep_detail": sweep_detail,
            "option_info": opt_check,
        }

    # ── CASE 3: S1 COUNTER-TREND / NEUTRAL HUNT (1st touch = Blue Box! Wait for 2nd Hunt!) ──
    if touch_count == 0 and not has_pause:
        update_single_zone_state(state, z_id, touch_count=1, blue_box_extreme=sweep_ext)
        mark_zone_cooldown(state, z_id, "BLUE_BOX_CREATED", custom_mins=10)
        send_blue_box_alert(zone, ltp, sweep_ext, action, hunt_side)
        return {"status": "BLUE_BOX_RECORDED", "reason": f"1st Liquidity (Blue Box) recorded at {sweep_ext}. Waiting for 2nd Hunt (Yellow Box)."}

    deeper_index_sweep = False
    equal_index_sweep = False
    if blue_extreme:
        if action == "BUY_CE":
            deeper_index_sweep = sweep_ext <= blue_extreme
            equal_index_sweep = abs(sweep_ext - blue_extreme) <= 10
        else:
            deeper_index_sweep = sweep_ext >= blue_extreme
            equal_index_sweep = abs(sweep_ext - blue_extreme) <= 10
    else:
        deeper_index_sweep = True

    option_swept_low = opt_check.get("swept_swing_low", False)

    if not (deeper_index_sweep or has_pause or (equal_index_sweep and option_swept_low)):
        return {
            "status": "NO_SETUP",
            "reason": f"Counter-trend 2nd touch ({sweep_ext}) did not sweep Blue Box ({blue_extreme}) and Option did not sweep low"
        }

    if not opt_check.get("confirmed"):
        return {"status": "WAITING_OPTION_GREEN", "reason": f"2nd Hunt swept ({sweep_detail}), waiting for Option Green: {opt_check.get('reason')}"}

    update_single_zone_state(state, z_id, touch_count=touch_count + 1, blue_box_extreme=sweep_ext)
    hunt_desc = (
        f"2nd Deeper Hunt (swept Blue Box {blue_extreme} → {sweep_ext})"
        if deeper_index_sweep and blue_extreme else
        f"2nd Hunt (Index Equal Level {sweep_ext} + Option Decay Sweep)"
        if equal_index_sweep and option_swept_low else
        f"Pre-Hunt Pause ({pause_detail}) + Liquidity Hunt at {sweep_ext}"
    )

    return {
        "status": "TRIGGERED",
        "setup_code": "S1",
        "setup_name": "S1 (2nd-Time Liquidity Hunt — Yellow Box)",
        "action": action,
        "hunt_side": hunt_side,
        "trigger_tf": trig_tf,
        "pattern": pat_name,
        "sweep_extreme": sweep_ext,
        "chase_distance_pts": round(chase_dist, 1),
        "index_sl": idx_sl - REF_SL_BUFFER if action == "BUY_CE" else idx_sl + REF_SL_BUFFER,
        "sweep_detail": f"{hunt_desc} | {sweep_detail}",
        "option_info": opt_check,
    }


def evaluate_s3_sideways_setup(state: NiftyState, tf_data, ltp, trend_info):
    """
    Rulebook v6 S3 Setup (Sideways Market — 2 Variants, NOT ORB!):
      - Variant A (S3_FAKEOUT_HUNT): Tight sideways box (12-35 pts) -> first breakout is a
        Fake Liquidity Grab (Trap) -> triggers when price reclaims back inside the box!
      - Variant B (S3_MICRO_ABSORPTION): Tiny-body candles (<= 6 pts) form at box boundary
        showing seller/buyer exhaustion, followed by expansion candle into the box.
    """
    s3_box = state.get("active_s3_box")
    if not s3_box:
        return None
    if not zone_cooldown_ok(state, "S3_BOX"):
        return None

    m5 = drop_incomplete_candle(tf_data.get("m5", pd.DataFrame()), 5)
    m1 = drop_incomplete_candle(tf_data.get("m1", pd.DataFrame()), 1, buffer_sec=4)
    if m5.empty or len(m5) < 6:
        return None

    box_l, box_h = s3_box["low"], s3_box["high"]
    trend_15m = trend_info.get("trend", "SIDEWAYS")

    # Variant A1: Fakeout Sweep strictly below Box Low -> Reclaim inside Box (BUY_CE)
    swept_dn_5m, ext_dn, pat_dn, sl_dn, det_dn = detect_synthetic_or_single_sweep(m5, box_l, box_h, "BUY_CE", pivot=box_l)
    swept_dn_1m, ext_dn1, pat_dn1, sl_dn1, det_dn1 = detect_synthetic_or_single_sweep(m1, box_l, box_h, "BUY_CE", pivot=box_l)
    if (swept_dn_5m or swept_dn_1m) and trend_15m != "DOWNTREND":
        opt_check = analyze_option_chart_confirmation(state, ltp, "BUY_CE")
        if opt_check.get("confirmed"):
            trig_tf = "5M" if swept_dn_5m else "1M"
            sl_val = sl_dn if swept_dn_5m else sl_dn1
            det_val = det_dn if swept_dn_5m else det_dn1
            return {
                "status": "TRIGGERED",
                "setup_code": "S3",
                "setup_name": "S3 (Sideways Fake Breakdown Hunt → Opposite Side Upside)",
                "action": "BUY_CE",
                "hunt_side": is_setup_trend_side("BUY_CE", trend_15m),
                "trigger_tf": trig_tf,
                "pattern": "S3_FAKEOUT_RECLAIM",
                "sweep_extreme": sl_val,
                "chase_distance_pts": round(abs(ltp - sl_val), 1),
                "index_sl": sl_val - REF_SL_BUFFER,
                "sweep_detail": f"Sideways Box ({box_l}-{box_h}) downside liquidity hunted & reclaimed: {det_val}",
                "option_info": opt_check,
                "zone_obj": {"id": "S3_BOX", "type": "SIDEWAYS_BOX", "low": box_l, "high": box_h, "pivot": box_l, "strength": "STRONG"},
            }

    # Variant A2: Fakeout Sweep strictly above Box High -> Reject inside Box (BUY_PE)
    swept_up_5m, ext_up, pat_up, sl_up, det_up = detect_synthetic_or_single_sweep(m5, box_l, box_h, "BUY_PE", pivot=box_h)
    swept_up_1m, ext_up1, pat_up1, sl_up1, det_up1 = detect_synthetic_or_single_sweep(m1, box_l, box_h, "BUY_PE", pivot=box_h)
    if (swept_up_5m or swept_up_1m) and trend_15m != "UPTREND":
        opt_check = analyze_option_chart_confirmation(state, ltp, "BUY_PE")
        if opt_check.get("confirmed"):
            trig_tf = "5M" if swept_up_5m else "1M"
            sl_val = sl_up if swept_up_5m else sl_up1
            det_val = det_up if swept_up_5m else det_up1
            return {
                "status": "TRIGGERED",
                "setup_code": "S3",
                "setup_name": "S3 (Sideways Fake Breakout Hunt → Opposite Side Downside)",
                "action": "BUY_PE",
                "hunt_side": is_setup_trend_side("BUY_PE", trend_15m),
                "trigger_tf": trig_tf,
                "pattern": "S3_FAKEOUT_REJECT",
                "sweep_extreme": sl_val,
                "chase_distance_pts": round(abs(ltp - sl_val), 1),
                "index_sl": sl_val + REF_SL_BUFFER,
                "sweep_detail": f"Sideways Box ({box_l}-{box_h}) upside liquidity hunted & rejected: {det_val}",
                "option_info": opt_check,
                "zone_obj": {"id": "S3_BOX", "type": "SIDEWAYS_BOX", "low": box_l, "high": box_h, "pivot": box_h, "strength": "STRONG"},
            }

    # Variant B: Micro-Candle Absorption at Box Boundary
    c1, c2, c3 = m5.iloc[-1], m5.iloc[-2], m5.iloc[-3]
    b2 = abs(int(c2["c"]) - int(c2["o"]))
    b3 = abs(int(c3["c"]) - int(c3["o"]))
    c1_body = int(c1["c"]) - int(c1["o"])

    if b2 <= MICRO_BODY_MAX_PTS and b3 <= MICRO_BODY_MAX_PTS and int(c2["l"]) <= box_l + 8 and c1_body >= 10 and int(c1["c"]) > int(c2["h"]):
        opt_check = analyze_option_chart_confirmation(state, ltp, "BUY_CE")
        if opt_check.get("confirmed"):
            low_sl = min(int(c1["l"]), int(c2["l"]), int(c3["l"]))
            return {
                "status": "TRIGGERED",
                "setup_code": "S3",
                "setup_name": "S3 (Sideways Micro-Candle Seller Exhaustion → Blast)",
                "action": "BUY_CE",
                "hunt_side": is_setup_trend_side("BUY_CE", trend_15m),
                "trigger_tf": "5M",
                "pattern": "S3_MICRO_ABSORPTION",
                "sweep_extreme": low_sl,
                "chase_distance_pts": round(abs(ltp - low_sl), 1),
                "index_sl": low_sl - REF_SL_BUFFER,
                "sweep_detail": f"Tiny absorption candles ({b3}pt, {b2}pt) held at box bottom {box_l} + +{c1_body}pt Green expansion",
                "option_info": opt_check,
                "zone_obj": {"id": "S3_BOX", "type": "SIDEWAYS_BOX", "low": box_l, "high": box_h, "pivot": box_l, "strength": "STRONG"},
            }

    return None


def evaluate_trend_pullback_hunt(state: NiftyState, tf_data, ltp, trend_info):
    """
    Evaluates the Yellow-Box Trend Pullback Setup in an active 15M Trend:
      - In DOWNTREND: Pullback strictly breaks above previous 5M candle/swing HIGH
        (hunting retail trailing SLs) and immediately closes RED back below that high -> BUY_PE!
      - In UPTREND: Pullback strictly breaks below previous 5M candle/swing LOW
        (hunting retail trailing SLs) and immediately closes GREEN back above that low -> BUY_CE!
    """
    trend_15m = trend_info.get("trend", "SIDEWAYS")
    if trend_15m not in ("UPTREND", "DOWNTREND"):
        return None
    if not zone_cooldown_ok(state, "TREND_PULLBACK"):
        return None

    m5 = drop_incomplete_candle(tf_data.get("m5", pd.DataFrame()), 5)
    if m5.empty or len(m5) < 5:
        return None

    c1, c2, c3 = m5.iloc[-1], m5.iloc[-2], m5.iloc[-3]
    o1, h1, l1, cl1 = int(c1["o"]), int(c1["h"]), int(c1["l"]), int(c1["c"])
    o2, h2, l2, cl2 = int(c2["o"]), int(c2["h"]), int(c2["l"]), int(c2["c"])
    h3, l3 = int(c3["h"]), int(c3["l"])

    if trend_15m == "DOWNTREND":
        prev_swing_high = max(h2, h3)
        swept_prev_high = (h1 > h2 + 2 and cl1 < o1 and cl1 < h2) or (h2 > h3 + 2 and cl1 < o1 and cl1 < l2)
        if swept_prev_high:
            opt_check = analyze_option_chart_confirmation(state, ltp, "BUY_PE")
            if opt_check.get("confirmed"):
                sl_high = max(h1, h2)
                return {
                    "status": "TRIGGERED",
                    "setup_code": "TREND_PULLBACK",
                    "setup_name": "Trend Pullback Hunt (Retail Trailing SL Sweep in Downtrend)",
                    "action": "BUY_PE",
                    "hunt_side": "TREND_SIDE",
                    "trigger_tf": "5M",
                    "pattern": "PULLBACK_HIGH_SWEEP_RED",
                    "sweep_extreme": sl_high,
                    "chase_distance_pts": round(abs(ltp - sl_high), 1),
                    "index_sl": sl_high + REF_SL_BUFFER,
                    "sweep_detail": f"Downtrend pullback swept prev candle high ({prev_swing_high} → {sl_high}) & closed Red at {cl1}",
                    "option_info": opt_check,
                    "zone_obj": {"id": "TREND_PULLBACK", "type": "PULLBACK_SWEEP", "low": cl1 - 10, "high": sl_high, "pivot": prev_swing_high, "strength": "MED"},
                }

    if trend_15m == "UPTREND":
        prev_swing_low = min(l2, l3)
        swept_prev_low = (l1 < l2 - 2 and cl1 > o1 and cl1 > l2) or (l2 < l3 - 2 and cl1 > o1 and cl1 > h2)
        if swept_prev_low:
            opt_check = analyze_option_chart_confirmation(state, ltp, "BUY_CE")
            if opt_check.get("confirmed"):
                sl_low = min(l1, l2)
                return {
                    "status": "TRIGGERED",
                    "setup_code": "TREND_PULLBACK",
                    "setup_name": "Trend Pullback Hunt (Retail Trailing SL Sweep in Uptrend)",
                    "action": "BUY_CE",
                    "hunt_side": "TREND_SIDE",
                    "trigger_tf": "5M",
                    "pattern": "PULLBACK_LOW_SWEEP_GREEN",
                    "sweep_extreme": sl_low,
                    "chase_distance_pts": round(abs(ltp - sl_low), 1),
                    "index_sl": sl_low - REF_SL_BUFFER,
                    "sweep_detail": f"Uptrend pullback swept prev candle low ({prev_swing_low} → {sl_low}) & closed Green at {cl1}",
                    "option_info": opt_check,
                    "zone_obj": {"id": "TREND_PULLBACK", "type": "PULLBACK_SWEEP", "low": sl_low, "high": cl1 + 10, "pivot": prev_swing_low, "strength": "MED"},
                }

    return None


def compute_smart_target(action, hunt_side, ltp, zones):
    """
    Rulebook v6 Target Rule:
      - Trend-side hunt: distant target (next major S/R), hold longer.
      - Counter-trend hunt: nearest target (first S/R).
    """
    cands = [z for z in zones if z.get("type") not in ("NO_TRADE", "SIDEWAYS")]
    if action == "BUY_CE":
        above = sorted([z for z in cands if z.get("low", 0) > ltp + 15], key=lambda z: z["low"])
        if not above:
            return int(ltp + REF_TARGET_FALLBACK)
        if hunt_side == "TREND_SIDE" and len(above) >= 2:
            strong_above = [z for z in above if z.get("strength") == "STRONG"]
            return int(strong_above[0]["low"] if strong_above and strong_above[0]["low"] > ltp + 35 else above[1]["low"])
        return int(above[0]["low"])
    elif action == "BUY_PE":
        below = sorted([z for z in cands if z.get("high", 0) < ltp - 15], key=lambda z: z["high"], reverse=True)
        if not below:
            return int(ltp - REF_TARGET_FALLBACK)
        if hunt_side == "TREND_SIDE" and len(below) >= 2:
            strong_below = [z for z in below if z.get("strength") == "STRONG"]
            return int(strong_below[0]["high"] if strong_below and strong_below[0]["high"] < ltp - 35 else below[1]["high"])
        return int(below[0]["high"])
    return int(ltp)


def detect_dominance_candle(df, ltp, lookback=DOMINANCE_LOOKBACK):
    if df is None or df.empty or len(df) < 3:
        return None
    d = df.tail(lookback).copy()
    d["rng"] = d["h"] - d["l"]
    row = d.loc[d["rng"].idxmax()]
    rng = int(row["rng"])
    if rng < DOMINANCE_MIN_RANGE:
        return None
    o, c, h, l = int(row["o"]), int(row["c"]), int(row["h"]), int(row["l"])
    mid = round((h + l) / 2)
    side = "SELLER" if c < o else "BUYER"
    return {"low": l, "high": h, "mid": mid, "side": side, "rng": rng,
            "reclaimed": (ltp > mid) if side == "SELLER" else (ltp < mid)}


def get_zone_history(state: NiftyState, zone, threshold=0.5):
    zl, zh = zone.get("low", 0), zone.get("high", 0)
    if zh <= zl:
        return []
    out = []
    for e in get_signal_history(state):
        if e.get("signal") not in ("BUY_CE", "BUY_PE"):
            continue
        el, eh = e.get("zone_low"), e.get("zone_high")
        if el is None or eh is None:
            continue
        if _overlap_ratio(zl, zh, el, eh) >= threshold:
            out.append(e)
    return out


# ═══════════════════════════════════════════════════════════════════════
# SECTION 6: DUAL AI ROUTER (GEMINI `thinkingBudget: 0` -> CLAUDE BACKUP)
#            + DELTA-ENCODED CANDLE PROMPTS (BUGS #3, #4, #5 FIXED)
# ═══════════════════════════════════════════════════════════════════════

def shared_base(*dfs):
    lows = [int(d["l"].min()) for d in dfs if d is not None and not d.empty]
    if not lows:
        return 24000
    return math.floor(min(lows) / 500) * 500


def compress_ohlc(df, base, max_candles=None):
    if df is None or df.empty:
        return "N/A"
    d = df.tail(max_candles) if max_candles else df
    return "\n".join(f"{int(r['o'])-base} {int(r['h'])-base} {int(r['l'])-base} {int(r['c'])-base}"
                     for _, r in d.iterrows())


def compress_hl(df, base, max_candles=None):
    if df is None or df.empty:
        return "N/A"
    d = df.tail(max_candles) if max_candles else df
    return "\n".join(f"{int(r['h'])-base} {int(r['l'])-base}" for _, r in d.iterrows())


FIRST_ANALYSIS_SYSTEM = """You are the NIFTY50 Top-Down Liquidity & Zone Architect for the 'Yellow Flash Trading Rulebook v6'.
The user manually confirms every trade on Index + Option charts.

CORE PHILOSOPHY:
- Big market moves require LIQUIDITY as fuel (retail stop-losses resting above swing highs / PDH or below swing lows / PDL).
- Top-Down Hierarchy: 1D -> 1H -> 30M -> 15M -> 5M.
- Active Intraday Trend Direction is governed by the 15M chart structure (HH_HL = UPTREND, LH_LL = DOWNTREND, else SIDEWAYS).
- Do NOT predict gap direction. Mark BOTH CALL-side (Support/Demand below price) and PUT-side (Resistance/Supply above price) liquidity zones for an IF-THEN plan:
  * Level A (Resistance / Supply Origin / PDH) reject + hunt = PUT (BUY_PE)
  * Level B (Support / Demand Origin / PDL) hunt + reclaim = CALL (BUY_CE)

All candle numbers are delta-encoded with ONE shared BASE. Convert back to REAL prices (value + BASE) in your JSON output.
Respond ONLY with valid JSON."""

ZONE_DECISION_SYSTEM = """You are a skeptical NIFTY50 'Yellow Flash Rulebook v6' Final Trade Supervisor.
The user manually confirms every trade. Your DEFAULT verdict is WAIT. Only output BUY_CE or BUY_PE when the candle data below proves a clean liquidity sweep and reclaim/rejection.

YELLOW FLASH RULEBOOK v6 CHECKLIST YOU MUST VERIFY FROM THE RAW CANDLES:
1. Judge the Actual Candles: Inspect the delta-encoded 1H, 15M, 5M, and 1M candles provided below. Do NOT rubber-stamp Python's summary if the candles show mid-range chop, weak closes, or conflicting wicks.
2. Direction Lock: You may ONLY approve the exact direction Python detected (`BUY_CE` or `BUY_PE`) or return `WAIT`. Never flip `BUY_CE` to `BUY_PE` or vice versa.
3. Setup Rules:
   - S1 Trend-Side (aligned with 15M trend): 1st clean hunt + confirmation candle is valid.
   - S1 Counter-Trend (against 15M trend): Must be at a major zone AND have either a 2nd hunt (or Option decay low sweep) or a pre-hunt sideways pause.
   - S2 (Prev-Day High/Low or Clean Origin): Must show decisive rejection wick or 2-candle synthetic reclaim.
   - S3 (Sideways Box): Fakeout outside box reclaimed back inside, or micro-candle exhaustion at boundary.
   - TREND_PULLBACK: Pullback swept previous 5M candle high/low and closed back in 15M trend direction.
4. No Chasing ('Unch bhavala entry nahi'): Check `Distance from Sweep Extreme`. If price has already run >35 pts away from the sweep extreme or into an opposing obstacle, output WAIT.
5. Confirmations: Provide 2 to 4 concrete observations citing exact REAL index/option prices (delta + BASE). Generic phrases without numbers are rejected.

Respond ONLY with valid JSON."""


def build_top_down_prompt_string(tf_data, auto_zones, trend_15m):
    daily = drop_incomplete_candle(tf_data.get("daily", pd.DataFrame()), 375)
    h1    = drop_incomplete_candle(tf_data.get("h1", pd.DataFrame()), 60)
    m30   = drop_incomplete_candle(tf_data.get("m30", pd.DataFrame()), 30)
    m15   = drop_incomplete_candle(tf_data.get("m15", pd.DataFrame()), 15)
    m5    = drop_incomplete_candle(tf_data.get("m5", pd.DataFrame()), 5)
    if daily.empty:
        return None

    base = shared_base(daily, h1, m30, m15, m5)
    pdc_row = daily.iloc[-1]
    pdh, pdl, pdc = int(pdc_row["h"]), int(pdc_row["l"]), int(pdc_row["c"])
    ltp_approx = int(m5.iloc[-1]["c"]) if not m5.empty else 0

    auto_str = "\n".join(
        f"  {z['id']}: {z['type']} {z['low']}-{z['high']} ({z['preferred_action']}) — {z['why']}"
        for z in auto_zones[:8]
    )

    return f"""=== NIFTY50 TOP-DOWN LIQUIDITY ANALYSIS | {datetime.now(IST).strftime('%d-%m-%Y %H:%M')} ===
ALL candle blocks use ONE base: BASE={base}. Real price = value + {base}.

[KEY LEVELS — REAL PRICES]
PDH:{pdh} | PDL:{pdl} | PDC:{pdc} | LTP:{ltp_approx}
15M Trend Structure: {trend_15m.get('trend')} ({trend_15m.get('structure')}) | EMA9:{trend_15m.get('ema9')} EMA21:{trend_15m.get('ema21')}

[PYTHON AUTO-DETECTED ZONES (Verify & Refine)]
{auto_str}

[1D - 12 candles] (H L)
{compress_hl(daily, base, 12)}

[1H - 20 candles] (O H L C)
{compress_ohlc(h1, base, 20)}

[30M - 24 candles] (O H L C)
{compress_ohlc(m30, base, 24)}

[15M - 32 candles] (O H L C)
{compress_ohlc(m15, base, 32)}

[5M - 45 candles] (O H L C)
{compress_ohlc(m5, base, 45)}"""


def build_supervisor_prompt_string(state: NiftyState, tf_data, ltp, zone, all_zones, setup_payload, trend_info):
    h1  = drop_incomplete_candle(tf_data.get("h1", pd.DataFrame()), 60)
    m15 = drop_incomplete_candle(tf_data.get("m15", pd.DataFrame()), 15)
    m5  = drop_incomplete_candle(tf_data.get("m5", pd.DataFrame()), 5)
    m1  = drop_incomplete_candle(tf_data.get("m1", pd.DataFrame()), 1, buffer_sec=4)

    base = shared_base(h1, m15, m5, m1)
    opt = setup_payload.get("option_info", {})
    dominance = detect_dominance_candle(m5, ltp)
    shift = get_structure_shift(state)
    history = get_zone_history(state, zone)

    other_zones = [
        f"  {z['id']}: {z['type']} {z['low']}-{z['high']} [{z.get('strength','?')}]"
        for z in all_zones if z.get("id") != zone.get("id")
    ]
    other_str = "\n".join(other_zones[:6]) if other_zones else "None"

    extra_ctx = ""
    if dominance:
        extra_ctx += (f"\n[DOMINANCE CANDLE] {dominance['low']}-{dominance['high']} ({dominance['rng']}pts) "
                      f"Side:{dominance['side']} 50%:{dominance['mid']} Reclaimed:{'YES' if dominance['reclaimed'] else 'NO'}")
    if shift:
        extra_ctx += f"\n[STRUCTURE SHIFT] {shift['count']} '{shift['direction']}' zone flips since {shift['since']}"
    if history:
        h_lines = "; ".join(f"{m.get('time','?')} {m.get('signal','?')}→{m.get('result','?')}" for m in history)
        extra_ctx += f"\n[ZONE HISTORY TODAY] {h_lines} (Raise bar if repeated SL hits!)"

    return f"""=== YELLOW FLASH SETUP VERIFICATION | {datetime.now(IST).strftime('%H:%M:%S')} ===
ALL candle blocks below use ONE base: BASE={base}. Real price = value + {base}.

[CANDIDATE SETUP — DEFAULT TO 'WAIT' UNLESS CANDLES CLEARLY CONFIRM]
Setup Code : {setup_payload.get('setup_code')} — {setup_payload.get('setup_name')}
Proposed   : {setup_payload.get('action')} (You MUST output '{setup_payload.get('action')}' or 'WAIT' — never flip direction)
Hunt Side  : {setup_payload.get('hunt_side')} | 15M Trend: {trend_info.get('trend')} ({trend_info.get('structure')}, EMA9:{trend_info.get('ema9')}, EMA21:{trend_info.get('ema21')})
Trigger TF : {setup_payload.get('trigger_tf')} | Pattern: {setup_payload.get('pattern')}
Sweep Info : {setup_payload.get('sweep_detail')}
Sweep Ext  : {setup_payload.get('sweep_extreme')} | Current LTP: {ltp} | Distance from Sweep Extreme: {setup_payload.get('chase_distance_pts')} pts

[TOUCHED ZONE & OTHER ZONES — REAL PRICES]
Touched    : {zone.get('id')} ({zone.get('type')}) {zone.get('low')}-{zone.get('high')} [{zone.get('strength','STRONG')}] — {zone.get('why','')}
Index SL   : {setup_payload.get('index_sl')} | Index Target: {setup_payload.get('index_target')}
Other Zones:
{other_str}{extra_ctx}

[OPTION CHART — REAL STRUCTURAL LEVELS]
Contract   : {opt.get('strike')} ({opt.get('tf')})
Opt Entry  : ₹{opt.get('opt_entry')} | Real Structural SL: ₹{opt.get('opt_sl')} (Risk: ₹{opt.get('opt_risk')}) | 1.1x BE Alert: ₹{opt.get('breakeven_alert')}
Detail     : {opt.get('reason')}

[1H - 10 candles] (O H L C | oldest→newest)
{compress_ohlc(h1, base, 10)}

[15M - 16 candles] (O H L C | oldest→newest)
{compress_ohlc(m15, base, 16)}

[5M - 24 candles] (O H L C | oldest→newest)
{compress_ohlc(m5, base, 24)}

[1M - 20 candles] (O H L C | oldest→newest)
{compress_ohlc(m1, base, 20)}"""


def _parse_json_safe(raw):
    if not raw:
        return None
    try:
        clean = raw.strip().replace("```json", "").replace("```", "").strip()
        s, e = clean.find("{"), clean.rfind("}") + 1
        return json.loads(clean[s:e]) if s != -1 else None
    except Exception as ex:
        log.error(f"[NIFTY] JSON parse error: {ex}")
        return None


def call_ai_json(system_prompt, user_prompt, max_tokens=ZONE_DECISION_MAX_TOKENS, claude_model=DECISION_CLAUDE_MODEL):
    """
    BUG FIX #4 & #5:
      - Sets `thinkingConfig: {"thinkingBudget": 0}` on Gemini 2.5 Flash so thinking tokens
        never eat `maxOutputTokens` and truncate JSON!
      - Logs `finishReason` from Gemini responses.
      - Retries once per provider on malformed JSON.
      - Falls back to Claude if Gemini fails/rate-limits.
      - If BOTH fail, returns `(None, None, "AI_UNAVAILABLE")` — NEVER fabricates a deterministic HIGH approval!
    """
    global _gemini_idx
    last_raw = None

    # 1. Try Gemini Free Tier Keys (with thinkingBudget: 0)
    if GEMINI_API_KEYS:
        for _ in range(len(GEMINI_API_KEYS)):
            api_key = GEMINI_API_KEYS[_gemini_idx % len(GEMINI_API_KEYS)]
            _gemini_idx += 1
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "generationConfig": {
                    "temperature": 0.0,
                    "maxOutputTokens": max_tokens,
                    "responseMimeType": "application/json",
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            }
            for attempt in (1, 2):
                try:
                    r = requests.post(url, json=payload, timeout=25)
                    if r.status_code == 200:
                        data = r.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            finish_reason = candidates[0].get("finishReason", "UNKNOWN")
                            raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                            last_raw = raw_text
                            parsed = _parse_json_safe(raw_text)
                            if parsed:
                                log.info(f"[NIFTY] Gemini ({GEMINI_MODEL}) finishReason={finish_reason} OK")
                                return parsed, raw_text, f"GEMINI ({GEMINI_MODEL})"
                            log.warning(f"[NIFTY] Gemini JSON parse failed (finishReason={finish_reason}, attempt={attempt})")
                    elif r.status_code in (429, 500, 503):
                        log.warning(f"[NIFTY] Gemini HTTP {r.status_code} on key #{_gemini_idx} — rotating key...")
                        break
                    else:
                        log.warning(f"[NIFTY] Gemini HTTP {r.status_code}: {r.text[:160]}")
                        break
                except Exception as e:
                    log.warning(f"[NIFTY] Gemini request error (attempt={attempt}): {e}")
                time.sleep(0.8)

    # 2. Backup: Anthropic Claude API
    if ANTHROPIC_KEY:
        log.info(f"[NIFTY] Switching to Backup Claude ({claude_model})...")
        hdrs = {
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": claude_model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        for attempt in (1, 2):
            try:
                r = requests.post("https://api.anthropic.com/v1/messages", headers=hdrs, json=body, timeout=35)
                if r.status_code == 200:
                    raw_text = r.json()["content"][0]["text"]
                    last_raw = raw_text
                    parsed = _parse_json_safe(raw_text)
                    if parsed:
                        return parsed, raw_text, f"CLAUDE_BACKUP ({claude_model})"
                else:
                    log.error(f"[NIFTY] Claude HTTP {r.status_code}: {r.text[:160]}")
                    break
            except Exception as e:
                log.error(f"[NIFTY] Claude request error (attempt={attempt}): {e}")
            time.sleep(1)

    return None, last_raw, "AI_UNAVAILABLE"


def run_top_down_analysis(state: NiftyState, tf_data, auto_zones, trend_15m, mode="MORNING"):
    log.info(f"[NIFTY] 🌅 Running {mode} Top-Down Analysis...")
    prompt_str = build_top_down_prompt_string(tf_data, auto_zones, trend_15m)
    if not prompt_str:
        return None

    user_prompt = prompt_str + """

RETURN ONLY this JSON (use REAL index prices = delta + BASE):
{"bias":"UPTREND/DOWNTREND/SIDEWAYS","structure":"HH_HL/LH_LL/SIDEWAYS","day_type":"TRENDING/RANGING/VOLATILE","if_then_plan":"1 line IF-THEN plan for both CALL and PUT sides","zones":[{"id":"Z1","type":"SUPPORT/RESISTANCE/FLIP","low":0,"high":0,"strength":"STRONG/MED","tags":["S1_LEVEL/S2_LEVEL/CLEAN_ORIGIN"],"preferred_action":"BUY_CE/BUY_PE","why":"short reason"}]}"""

    result, raw, provider = call_ai_json(
        FIRST_ANALYSIS_SYSTEM, user_prompt,
        max_tokens=FIRST_ANALYSIS_MAX_TOKENS, claude_model=ANALYSIS_CLAUDE_MODEL
    )
    save_ai_raw_log(state, {
        "time": datetime.now(IST).strftime("%H:%M"), "mode": mode,
        "provider": provider, "raw": raw[:400] if raw else None, "parsed": bool(result)
    })
    return result


def run_ai_supervisor(state: NiftyState, tf_data, ltp, zone, all_zones, setup_payload, trend_info):
    prompt_str = build_supervisor_prompt_string(state, tf_data, ltp, zone, all_zones, setup_payload, trend_info)
    user_prompt = prompt_str + """

RETURN ONLY this JSON:
{"signal":"BUY_CE/BUY_PE/WAIT","confidence":"HIGH/MED/LOW","setup_tag":"S1/S2/S3/TREND_PULLBACK","confirmations":["concrete fact 1 citing exact real price","concrete fact 2 citing exact real price"],"ai_view":"1 honest line on why this setup works or why to wait","message":"1 short Marathi/Hinglish line for Telegram"}"""

    result, raw, provider = call_ai_json(
        ZONE_DECISION_SYSTEM, user_prompt,
        max_tokens=ZONE_DECISION_MAX_TOKENS, claude_model=DECISION_CLAUDE_MODEL
    )
    save_ai_raw_log(state, {
        "time": datetime.now(IST).strftime("%H:%M:%S"),
        "mode": "SUPERVISOR",
        "provider": provider,
        "setup": setup_payload.get("setup_code"),
        "zone_id": zone.get("id"),
        "ltp": ltp,
        "raw": raw[:400] if raw else None,
        "parsed": bool(result),
    })
    return result, provider


# ═══════════════════════════════════════════════════════════════════════
# SECTION 7: STRICT VALIDATION, OUTCOME TRACKING & EOD JOURNAL (BUG #3 & #4 FIXED)
# ═══════════════════════════════════════════════════════════════════════

FILLER = {"none", "na", "n/a", "null", "", "looks good", "strong setup", "good entry", "bullish", "bearish",
          "price looking good", "momentum", "trend is good", "confirmed", "valid setup"}


def _real_confirmations(confs):
    real = [
        str(c).strip() for c in (confs or [])
        if len(str(c).strip()) >= 12 and str(c).strip().lower() not in FILLER
    ]
    has_num = any(any(ch.isdigit() for ch in s) for s in real)
    return real, has_num


def validate_supervisor_decision(ai_res, expected_action, ltp, idx_sl):
    """
    BUG FIX #3 & #4:
      - If `ai_res` is None (both Gemini and Claude failed), returns False -> bot WAITS!
      - If `ai_res["signal"] != expected_action` (e.g. Python found a BUY_CE sweep on a CE
        contract, but AI returned BUY_PE), returns False -> blocks direction mismatch!
      - Requires HIGH or MED confidence and >= 2 real price-citing confirmations.
    """
    if not ai_res:
        log.warning("[NIFTY] Rejected: AI Supervisor unavailable — refusing to send unverified alert")
        return False, "AI_UNAVAILABLE"

    sig = ai_res.get("signal", "WAIT")
    conf = ai_res.get("confidence", "LOW")

    if sig == "WAIT":
        return False, "AI_WAIT"

    if sig != expected_action:
        log.warning(f"[NIFTY] Rejected: AI direction ({sig}) conflicts with Python setup direction ({expected_action})!")
        return False, f"DIRECTION_MISMATCH ({sig} != {expected_action})"

    if conf not in ("HIGH", "MED"):
        log.info(f"[NIFTY] Rejected: LOW confidence ({conf})")
        return False, "LOW_CONFIDENCE"

    if idx_sl:
        if sig == "BUY_CE" and ltp <= idx_sl:
            return False, f"LTP {ltp} already at/below Index SL {idx_sl}"
        if sig == "BUY_PE" and ltp >= idx_sl:
            return False, f"LTP {ltp} already at/above Index SL {idx_sl}"

    real_confs, has_num = _real_confirmations(ai_res.get("confirmations", []))
    if len(real_confs) < MIN_CONFIRMATIONS or not has_num:
        log.info(f"[NIFTY] Rejected: insufficient concrete confirmations ({len(real_confs)})")
        return False, "WEAK_CONFIRMATIONS"

    return True, "APPROVED"


def process_triggered_setup(state: NiftyState, tf_data, ltp, zone, all_zones, trend_info, setup_payload):
    zone_id = zone.get("id", "?")
    action = setup_payload["action"]
    hunt_side = setup_payload["hunt_side"]
    setup_code = setup_payload["setup_code"]

    idx_sl = int(setup_payload["index_sl"])
    idx_target = compute_smart_target(action, hunt_side, ltp, all_zones)
    setup_payload["index_target"] = idx_target

    idx_risk = abs(ltp - idx_sl)
    if idx_risk > 45:
        log.info(f"[NIFTY] ⏭️ Skipped {zone_id}: Index SL risk too wide ({idx_risk} pts > 45 pts)")
        mark_zone_cooldown(state, zone_id, "WAIT", custom_mins=8)
        return

    ai_res, provider = run_ai_supervisor(state, tf_data, ltp, zone, all_zones, setup_payload, trend_info)
    is_valid, reason = validate_supervisor_decision(ai_res, action, ltp, idx_sl)

    if not is_valid:
        log.info(f"[NIFTY] ⏭️ Filtered {zone_id} ({setup_code}): {reason}")
        save_signal_log(state, {
            "time": datetime.now(IST).strftime("%H:%M"),
            "zone_id": zone_id,
            "zone_low": zone.get("low"),
            "zone_high": zone.get("high"),
            "setup_code": setup_code,
            "hunt_side": hunt_side,
            "signal": "WAIT" if reason == "AI_WAIT" else "REJECTED",
            "confidence": (ai_res or {}).get("confidence", "NONE"),
            "reason": reason,
            "result": "FILTERED",
        })
        mark_zone_cooldown(state, zone_id, "WAIT", custom_mins=WAIT_COOLDOWN_MIN)
        return

    cd_mins = PULLBACK_COOLDOWN_MIN if setup_code == "TREND_PULLBACK" else SIGNAL_COOLDOWN_MIN
    mark_zone_cooldown(state, zone_id, action, custom_mins=cd_mins)

    opt = setup_payload.get("option_info", {})
    save_signal_log(state, {
        "time": datetime.now(IST).strftime("%H:%M"),
        "zone_id": zone_id,
        "zone_low": zone.get("low"),
        "zone_high": zone.get("high"),
        "setup_code": setup_code,
        "setup_name": setup_payload.get("setup_name"),
        "hunt_side": hunt_side,
        "signal": action,
        "confidence": ai_res.get("confidence"),
        "provider": provider,
        "ltp": ltp,
        "ref_sl": idx_sl,
        "ref_target": idx_target,
        "opt_strike": opt.get("strike"),
        "opt_entry": opt.get("opt_entry"),
        "opt_sl": opt.get("opt_sl"),
        "opt_risk": opt.get("opt_risk"),
        "opt_1_1x": opt.get("breakeven_alert"),
        "result": "OPEN",
    })

    send_trade_signal_alert(ai_res, provider, ltp, zone, setup_payload, trend_info)


def track_open_signals(state: NiftyState, ltp: float):
    """Tracks OPEN signals against Index reference SL & Target for Part G EOD Journal."""
    history = get_signal_history(state)
    if not history:
        return
    now = datetime.now(IST)
    updated = False
    for sig in history:
        if sig.get("result") != "OPEN":
            continue
        ref_sl, ref_tgt, stype = sig.get("ref_sl", 0), sig.get("ref_target", 0), sig.get("signal", "")
        if stype == "BUY_CE":
            if ref_sl and ltp <= ref_sl:
                sig["result"] = "REF_SL_HIT"; updated = True; continue
            if ref_tgt and ltp >= ref_tgt:
                sig["result"] = "REF_TARGET_HIT"; updated = True; continue
        elif stype == "BUY_PE":
            if ref_sl and ltp >= ref_sl:
                sig["result"] = "REF_SL_HIT"; updated = True; continue
            if ref_tgt and ltp <= ref_tgt:
                sig["result"] = "REF_TARGET_HIT"; updated = True; continue
        try:
            t = IST.localize(datetime.strptime(sig["time"], "%H:%M").replace(
                year=now.year, month=now.month, day=now.day))
            if (now - t).total_seconds() / 60 > SIGNAL_VALID_MIN:
                sig["result"] = "EXPIRED"
                updated = True
        except Exception:
            pass
    if updated:
        state.set("signal_history", history)


# ═══════════════════════════════════════════════════════════════════════
# SECTION 8: TELEGRAM ALERTS & PART G EOD SUMMARY
# ═══════════════════════════════════════════════════════════════════════

def tg_send(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.info(f"[NIFTY-TG-DRY] {text[:120]}...")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code != 200:
            log.error(f"[NIFTY] Telegram send failed {r.status_code}: {r.text[:250]}")
    except Exception as e:
        log.error(f"[NIFTY] Telegram error: {e}")


def send_zone_brief(ctx, zones, trend_15m):
    today = datetime.now(IST).strftime("%d %b %Y %H:%M")
    call_zones = [z for z in zones if z.get("preferred_action") == "BUY_CE"]
    put_zones  = [z for z in zones if z.get("preferred_action") == "BUY_PE"]

    call_str = "\n".join(
        f"  🟢 <b>{esc(z['id'])}</b>: {z['low']}–{z['high']} [{esc(z.get('strength','?'))}] — {esc(z.get('why',''))}"
        for z in call_zones[:5]
    ) or "  None near LTP"

    put_str = "\n".join(
        f"  🔴 <b>{esc(z['id'])}</b>: {z['low']}–{z['high']} [{esc(z.get('strength','?'))}] — {esc(z.get('why',''))}"
        for z in put_zones[:5]
    ) or "  None near LTP"

    tg_send(f"""⚡ <b>[NIFTY 50] YELLOW FLASH V7 — ZONES | {today}</b>

<b>15M Trend:</b> {esc(trend_15m.get('trend','?'))} ({esc(trend_15m.get('structure','?'))} | EMA9:{trend_15m.get('ema9')} / EMA21:{trend_15m.get('ema21')})
<b>Day Type :</b> {esc(ctx.get('day_type','?'))}
<b>IF-THEN  :</b> {esc(ctx.get('if_then_plan','Both sides ready — react on zone hunt!'))}

<b>🔴 PUT SIDE ZONES (Level Reject + Hunt = BUY PE):</b>
{put_str}

<b>🟢 CALL SIDE ZONES (Level Hunt + Reclaim = BUY CE):</b>
{call_str}

<i>Rulebook: Prediction nahi! Je level adhi vajel tya side cha setup bagha.</i>""")


def send_blue_box_alert(zone, ltp, sweep_ext, action, hunt_side):
    side_lbl = "CALL (CE)" if action == "BUY_CE" else "PUT (PE)"
    tg_send(f"""🔵 <b>[NIFTY 50] 1ST LIQUIDITY CREATED (BLUE BOX) | {datetime.now(IST).strftime('%H:%M')}</b>

<b>Zone      :</b> {esc(zone.get('id'))} ({zone.get('low')}–{zone.get('high')})
<b>Hunt Side :</b> {esc(hunt_side)} ({side_lbl} Watch)
<b>1st Touch :</b> {sweep_ext} (LTP: {ltp})

⚠️ <b>Rulebook Counter-Trend Rule:</b>
Ha 1st touch (Blue Box) ahe — ithe retailers che SL jama hotil.
Ghai karu nako! Parat <b>2nd Hunt (Yellow Box)</b> + Option 1st Green Candle yeu de.""")


def send_trade_signal_alert(ai_res, provider, ltp, zone, setup_payload, trend_info):
    sig = ai_res.get("signal", "WAIT")
    emoji = "🟢" if sig == "BUY_CE" else "🔴"
    opt = setup_payload.get("option_info", {})
    confs = "\n".join(f"  ✔ {esc(c)}" for c in ai_res.get("confirmations", [])[:4] if c)

    tg_send(f"""{emoji} <b>[NIFTY 50] {esc(sig)} — {esc(setup_payload.get('setup_code'))} CONFIRMED | {datetime.now(IST).strftime('%H:%M:%S')}</b>

<b>Setup     :</b> {esc(setup_payload.get('setup_name'))}
<b>Hunt Side :</b> {esc(setup_payload.get('hunt_side'))} (15M: {esc(trend_info.get('trend'))})
<b>Zone      :</b> {esc(zone.get('id'))} ({zone.get('low')}–{zone.get('high')}) | TF: {esc(setup_payload.get('trigger_tf'))}
<b>Index LTP :</b> {ltp} | <b>Conf:</b> {esc(ai_res.get('confidence'))} ({esc(provider)})

🎯 <b>OPTION CHART & HOLD PLAN (Rulebook v6):</b>
  • <b>Contract :</b> <code>{esc(opt.get('strike','ATM'))}</code> ({esc(opt.get('tf','1M'))} 1st Green Candle ✅)
  • <b>Opt Entry:</b> ~₹{opt.get('opt_entry', 0)}
  • <b>Real SL  :</b> <b>₹{opt.get('opt_sl', 0)}</b> (Strictly below Green candle low — Real Risk: ₹{opt.get('opt_risk', 0)})
  • <b>1.1x Alert:</b> <b>₹{opt.get('breakeven_alert', 0)}</b> <i>(Price ithe gela ki SL Breakeven var anaycha & Screen OFF!)</i>
  • <b>Ref Tgt  :</b> ₹{opt.get('opt_target_1_2', 0)} (1:2) → ₹{opt.get('opt_target_big', 0)} (Runner)

📊 <b>Index Reference Levels:</b>
  • <b>Sweep Ext:</b> {setup_payload.get('sweep_extreme')} | <b>Index SL:</b> {setup_payload.get('index_sl')} | <b>Target ({esc(setup_payload.get('hunt_side'))}):</b> {setup_payload.get('index_target')}

<b>Why this Setup:</b>
  • {esc(setup_payload.get('sweep_detail'))}
{confs}

🧠 <b>AI View:</b> {esc(ai_res.get('ai_view',''))}
💬 {esc(ai_res.get('message',''))}

<i>30-Sec Checklist: 1st green candle ahe ka? Real SL & Target broker madhe fix kara!</i>""")
    log.info(f"[NIFTY] 🔥 Alert sent: {sig} ({setup_payload.get('setup_code')}) @ {ltp}")


def run_nifty_eod_summary(shared_store):
    """Part G Journal Summary: breaks down outcomes by Setup Code (S1, S2, S3, TREND_PULLBACK) and Hunt Side."""
    state = NiftyState(shared_store)
    all_hist = get_signal_history(state)
    trades = [s for s in all_hist if s.get("signal") in ("BUY_CE", "BUY_PE")]
    today = datetime.now(IST).strftime("%d %b %Y")

    ce = [s for s in trades if s.get("signal") == "BUY_CE"]
    pe = [s for s in trades if s.get("signal") == "BUY_PE"]
    hit = sum(1 for s in trades if s.get("result") == "REF_TARGET_HIT")
    sl  = sum(1 for s in trades if s.get("result") == "REF_SL_HIT")
    exp = sum(1 for s in trades if s.get("result") == "EXPIRED")
    op  = sum(1 for s in trades if s.get("result") == "OPEN")

    # Setup-wise breakdown for Part G Review
    setup_stats = {}
    for s in trades:
        sc = s.get("setup_code", "OTHER")
        setup_stats.setdefault(sc, {"tot": 0, "hit": 0, "sl": 0})
        setup_stats[sc]["tot"] += 1
        if s.get("result") == "REF_TARGET_HIT":
            setup_stats[sc]["hit"] += 1
        elif s.get("result") == "REF_SL_HIT":
            setup_stats[sc]["sl"] += 1

    setup_lines = "\n".join(
        f"  • <b>{esc(k)}</b>: {v['tot']} trades (✅ {v['hit']} | ❌ {v['sl']})"
        for k, v in setup_stats.items()
    ) or "  • No confirmed trades today"

    detail = ""
    for s in trades:
        r = s.get("result", "?")
        em = "✅" if r == "REF_TARGET_HIT" else "❌" if r == "REF_SL_HIT" else "⏰"
        detail += (f"\n{em} {s['time']} <b>{esc(s.get('setup_code','S1'))}</b> [{esc(s.get('hunt_side',''))}] "
                   f"{esc(s['signal'])} ({esc(s.get('opt_strike',''))} SL:₹{s.get('opt_sl',0)}) → {esc(r)}")

    tg_send(f"""📈 <b>[NIFTY 50] YELLOW FLASH V7 — EOD JOURNAL | {today}</b>

<b>Total Setups Triggered:</b> {len(trades)} (🟢 CE: {len(ce)} | 🔴 PE: {len(pe)})
<b>Index Ref Outcomes (Signal Proxy):</b> ✅ Target: {hit} | ❌ SL: {sl} | ⏰ Expired/Open: {exp + op}

<b>Part G Setup Breakdown:</b>
{setup_lines}
{detail}

<i>Journal Reminder: Setup code (S1/S2/S3/PB), Hunt Side, ani Rule Followed % note kara!</i>""")
    flush_day(state)


# ═══════════════════════════════════════════════════════════════════════
# SECTION 9: ORCHESTRATOR JOBS (CALLED BY main.py SCHEDULER)
# ═══════════════════════════════════════════════════════════════════════

def is_nifty_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y-%m-%d") in NSE_HOLIDAYS:
        return False
    from datetime import time as dtime
    return dtime(9, 15) <= now.time() <= dtime(15, 30)


def morning_job(shared_store):
    if not is_nifty_market_open() or not UPSTOX_TOKEN:
        return
    log.info("[NIFTY] 🌅 MORNING TOP-DOWN ANALYSIS JOB (09:20 IST)")
    with _nifty_lock:
        try:
            state = NiftyState(shared_store)
            ltp = get_ltp() or 0
            tf_data = fetch_top_down_data()
            trend_15m = compute_15m_trend(tf_data.get("m15", pd.DataFrame()), tf_data.get("m5", pd.DataFrame()))
            auto_zones = extract_auto_math_zones(tf_data, ltp)
            hist_zones = fetch_selective_historical_memory_zones(state, ltp)

            ai_res = run_top_down_analysis(state, tf_data, auto_zones + hist_zones, trend_15m, mode="MORNING")
            ai_zones = ai_res.get("zones", []) if ai_res else []
            merged_zones = deduplicate_and_merge_zones(auto_zones + hist_zones, ai_zones, ltp)

            ctx = {
                "bias": trend_15m.get("trend"),
                "structure": trend_15m.get("structure"),
                "day_type": (ai_res or {}).get("day_type", "UNCLEAR"),
                "if_then_plan": (ai_res or {}).get("if_then_plan", "React on PDH/PDL & Swing hunts"),
            }
            save_morning_context(state, ctx)
            save_zones(state, merged_zones)
            send_zone_brief(ctx, merged_zones, trend_15m)
        except Exception as e:
            log.exception("[NIFTY] Morning job error")
            tg_send(f"⚠️ <b>[NIFTY] Morning job error:</b> {esc(str(e)[:100])}")


def hourly_job(shared_store):
    if not is_nifty_market_open() or not UPSTOX_TOKEN:
        return
    log.info("[NIFTY] 🔄 HOURLY TOP-DOWN SYNC")
    with _nifty_lock:
        try:
            state = NiftyState(shared_store)
            ltp = get_ltp()
            if not ltp:
                return
            tf_data = fetch_top_down_data()
            trend_15m = compute_15m_trend(tf_data.get("m15", pd.DataFrame()), tf_data.get("m5", pd.DataFrame()))
            auto_zones = extract_auto_math_zones(tf_data, ltp)
            hist_zones = fetch_selective_historical_memory_zones(state, ltp)

            ai_res = run_top_down_analysis(state, tf_data, auto_zones + hist_zones, trend_15m, mode="HOURLY")
            ai_zones = ai_res.get("zones", []) if ai_res else []

            existing = get_zones(state)
            merged = deduplicate_and_merge_zones(existing + auto_zones + hist_zones, ai_zones, ltp)
            ctx = get_morning_context(state)
            ctx.update({
                "bias": trend_15m.get("trend"),
                "structure": trend_15m.get("structure"),
                "day_type": (ai_res or {}).get("day_type", ctx.get("day_type", "UNCLEAR")),
                "if_then_plan": (ai_res or {}).get("if_then_plan", ctx.get("if_then_plan", "")),
            })
            save_morning_context(state, ctx)
            save_zones(state, merged)
            tg_send(f"🔄 <b>[NIFTY 50] Hourly Zone Sync | {datetime.now(IST).strftime('%H:%M')}</b>\n"
                    f"15M Trend: <b>{esc(trend_15m.get('trend'))}</b> (EMA9:{trend_15m.get('ema9')}) | Active Zones: {len(merged)}")
        except Exception:
            log.exception("[NIFTY] Hourly job error")


def run_nifty_1m_tick(shared_store):
    """
    Unified 1-Minute + 5-Minute Scanner called every 1 minute by `main.py`:
      - At 09:20 AM (or if zones are missing): runs `morning_job`.
      - At 15:30 PM: runs `run_nifty_eod_summary`.
      - On 5M boundaries (:00, :05, :10...): updates live dynamic zones, Morning-to-Afternoon
        ±50pt memory boxes, 6M Historical ±50pt memory zones, S3 boxes, and checks all setups.
      - On intermediate 1-minute ticks (:01, :02, :03, :04): checks LTP first; if LTP is within
        ±50 pts of any active zone or S3 box, fetches 1M Index + 1M Option candles from Upstox
        to catch the 1st Green Candle immediately!
    """
    if not is_nifty_market_open() or not UPSTOX_TOKEN:
        return
    now = datetime.now(IST)
    if now.hour == 15 and now.minute >= 28:
        state = NiftyState(shared_store)
        if state.get("zones"):
            run_nifty_eod_summary(shared_store)
        return
    if now.hour == 9 and now.minute < 20:
        return
    if now.hour == 15 and now.minute > LAST_AI_CALL_MINUTE:
        return

    if not _nifty_lock.acquire(blocking=False):
        return
    try:
        state = NiftyState(shared_store)
        ltp = get_ltp()
        if not ltp:
            return
        track_open_signals(state, ltp)

        if not is_today_data_valid(state):
            _nifty_lock.release()
            morning_job(shared_store)
            _nifty_lock.acquire()

        zones = get_zones(state)
        if not zones:
            return

        is_5m_boundary = (now.minute % 5 == 0)

        # Hourly sync trigger at 10:02, 11:02, 12:02, 13:02, 14:02
        if now.minute == 2 and now.hour in (10, 11, 12, 13, 14):
            last_hr = state.get("last_hourly_sync_hr")
            if last_hr != now.hour:
                state.set("last_hourly_sync_hr", now.hour, ttl=3600)
                _nifty_lock.release()
                hourly_job(shared_store)
                _nifty_lock.acquire()
                zones = get_zones(state)

        # On intermediate 1M ticks, only fetch full candles if LTP is within ±50 pts of a zone
        if not is_5m_boundary and not is_price_near_any_zone(state, ltp, zones, proximity_pts=TURBO_1M_PROXIMITY_PTS):
            return

        tf_data = fetch_live_monitor_data(include_1m=True)
        trend_info = compute_15m_trend(tf_data.get("m15", pd.DataFrame()), tf_data.get("m5", pd.DataFrame()))

        if is_5m_boundary:
            zones = update_live_intraday_zones(state, zones, tf_data, ltp)

        opening_session = (now.hour == 9 and now.minute < 45)

        # 1. Evaluate S1 & S2 Setups at Touched Zone
        touched = get_touched_zone(ltp, zones)
        if touched and touched.get("type") not in ("NO_TRADE", "SIDEWAYS"):
            zid = touched.get("id", "?")
            if zone_cooldown_ok(state, zid):
                if opening_session and touched.get("strength") != "STRONG" and "S2_LEVEL" not in touched.get("tags", []):
                    log.info(f"[NIFTY] Opening 30m filter: skipping minor zone {zid}")
                else:
                    eval_res = evaluate_zone_setup(state, tf_data, ltp, touched, trend_info)
                    if eval_res.get("status") == "TRIGGERED":
                        process_triggered_setup(state, tf_data, ltp, touched, zones, trend_info, eval_res)
                        return

        # 2. Evaluate S3 Sideways Fakeout / Micro-Absorption Setup
        if not opening_session:
            s3_res = evaluate_s3_sideways_setup(state, tf_data, ltp, trend_info)
            if s3_res and s3_res.get("status") == "TRIGGERED":
                process_triggered_setup(state, tf_data, ltp, s3_res["zone_obj"], zones, trend_info, s3_res)
                return

        # 3. Evaluate Trend Pullback Hunt (Yellow Boxes in running 15M Trend — evaluated on 5M boundaries)
        if is_5m_boundary and not opening_session:
            pb_res = evaluate_trend_pullback_hunt(state, tf_data, ltp, trend_info)
            if pb_res and pb_res.get("status") == "TRIGGERED":
                process_triggered_setup(state, tf_data, ltp, pb_res["zone_obj"], zones, trend_info, pb_res)
                return

    except Exception:
        log.exception("[NIFTY] run_nifty_1m_tick error")
    finally:
        try:
            _nifty_lock.release()
        except RuntimeError:
            pass
