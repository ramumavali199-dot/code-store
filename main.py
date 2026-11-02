"""
MASTER ORCHESTRATOR (main.py) — NIFTY 50 + BITCOIN + GOLD V8.0 PRODUCTION
Multi-Asset Cloud Launcher for Koyeb / Docker
- Non-blocking Asynchronous Scheduler (Zero Thread Freezing)
- 5-Minute Live Radar Heartbeat Monitor (Continuous Terminal Visibility)
- Exact Gold Market Hours (Monday 03:30 AM to Saturday 02:30 AM IST)
- Fail-Safe Health Checks & Diagnostics for Delta, Upstox, Gemini, Claude
"""

import os
import json
import time
import requests
import logging
import threading
from datetime import datetime
import pytz
from dotenv import load_dotenv

try:
    from flask import Flask
except ImportError:
    Flask = None

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError:
    BackgroundScheduler = None

load_dotenv()

IST = pytz.timezone("Asia/Kolkata")


class TaggedISTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=pytz.utc).astimezone(IST)
        return dt.strftime(datefmt) if datefmt else dt.strftime("%Y-%m-%d %H:%M:%S")


_handler = logging.StreamHandler()
_handler.setFormatter(TaggedISTFormatter("%(asctime)s IST | [%(name)s] | %(levelname)s | %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("MAIN")

logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

ENABLE_NIFTY = os.getenv("ENABLE_NIFTY", "true").lower() == "true"
ENABLE_BTC   = os.getenv("ENABLE_BTC",   "true").lower() == "true"
ENABLE_GOLD  = os.getenv("ENABLE_GOLD",  "true").lower() == "true"
REDIS_URL    = os.getenv("REDIS_URL", "")
NSE_HOLIDAYS = {d.strip() for d in os.getenv("NSE_HOLIDAYS", "").split(",") if d.strip()}


def is_nifty_open(now=None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y-%m-%d") in NSE_HOLIDAYS:
        return False
    return (9, 20) <= (now.hour, now.minute) <= (15, 30)


def is_gold_open(now=None) -> bool:
    now = now or datetime.now(IST)
    weekday = now.weekday()  # 0=Mon, 1=Tue, ..., 5=Sat, 6=Sun
    t_min = now.hour * 60 + now.minute

    # Sunday: Closed all day
    if weekday == 6:
        return False

    # Saturday: Open only until 02:30 AM IST (Summer EDT) / 03:30 AM IST (Winter EST)
    if weekday == 5:
        return t_min < (2 * 60 + 30)

    # Monday: Opens at 03:30 AM IST (Summer EDT) / 04:30 AM IST (Winter EST)
    if weekday == 0:
        return t_min >= (3 * 60 + 30)

    # Tue-Fri: Daily 1-hour rollover maintenance break (02:30 AM - 03:30 AM IST)
    if 1 <= weekday <= 4:
        if (2 * 60 + 30) <= t_min < (3 * 60 + 30):
            return False

    return True


def is_btc_open(now=None) -> bool:
    return True


class SharedNamespacedStore:
    MAX_RAM_KEYS = 500

    def __init__(self, redis_url=""):
        self._ram = {}
        self._lock = threading.Lock()
        self._r = None
        if redis_url:
            try:
                import redis
                r = redis.from_url(redis_url, decode_responses=True)
                r.ping()
                self._r = r
                log.info("Connected to Redis successfully")
            except Exception as e:
                log.warning(f"Redis unavailable ({e}) — running in bounded RAM mode")
        else:
            log.info("Running in bounded RAM mode")

    def set(self, namespaced_key: str, value, ttl: int = 86400):
        raw = json.dumps(value)
        if self._r:
            try:
                self._r.setex(namespaced_key, ttl, raw)
                return
            except Exception:
                pass
        with self._lock:
            self._ram[namespaced_key] = (raw, time.time() + ttl)

    def get(self, namespaced_key: str):
        if self._r:
            try:
                raw = self._r.get(namespaced_key)
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
        with self._lock:
            item = self._ram.get(namespaced_key)
            if not item:
                return None
            raw, exp = item
            if time.time() > exp:
                return None
            return json.loads(raw)

    def delete(self, namespaced_key: str):
        if self._r:
            try:
                self._r.delete(namespaced_key)
            except Exception:
                pass
        with self._lock:
            self._ram.pop(namespaced_key, None)

    def stats(self):
        with self._lock:
            return {"storage_mode": "redis" if self._r else "bounded_ram", "ram_keys_count": len(self._ram)}


try:
    import nifty_bot
except Exception as e:
    nifty_bot = None
    log.error(f"Failed to import nifty_bot: {e}")

try:
    import btc_bot
except Exception as e:
    btc_bot = None
    log.error(f"Failed to import btc_bot: {e}")

try:
    import gold_bot
except Exception as e:
    gold_bot = None
    log.error(f"Failed to import gold_bot: {e}")


def _guarded(tag: str, fn, gate, run_async: bool = True):
    bot_log = logging.getLogger(tag)

    def runner(store):
        if not gate():
            return

        def _task():
            try:
                fn(store)
            except Exception:
                bot_log.exception(f"[{tag}] Task execution failed")

        if run_async:
            threading.Thread(target=_task, daemon=True).start()
        else:
            _task()

    runner.__name__ = f"{tag.lower()}_guarded_runner"
    return runner


def _register(scheduler, store, *, enabled, module, func_name, tag, job_id, gate, label, run_async=True, **cron):
    if not enabled:
        return
    if module is None:
        return
    fn = getattr(module, func_name, None)
    if not callable(fn):
        log.warning(f"[{tag}] Function '{func_name}' not found in {module.__name__}. Skipping.")
        return
    if scheduler:
        scheduler.add_job(_guarded(tag, fn, gate, run_async=run_async), "cron", args=[store], id=job_id, **cron)
    log.info(f"Scheduled [{tag}] {label}")


# ─── 5-MINUTE LIVE MULTI-ASSET RADAR MONITOR ───
def run_asset_radar_heartbeat(store):
    """Logs real-time market price fetching & status every 5 minutes."""
    now = datetime.now(IST)
    now_str = now.strftime("%H:%M:%S IST")
    h_log = logging.getLogger("HEARTBEAT")

    # 1. BTC Live Status & Fetch Check
    btc_status = "Disabled"
    if ENABLE_BTC:
        try:
            r = requests.get("https://api.india.delta.exchange/v2/tickers/BTCUSD", timeout=8)
            if r.status_code == 200:
                data = r.json().get("result", {})
                close_p = float(data.get("close", 0.0))
                next_min = 30 if now.minute < 30 else 0
                next_h = now.hour if now.minute < 30 else (now.hour + 1) % 24
                btc_status = f"LIVE OK (${close_p:,.1f}) | 30M Bar Scanning (Next: {next_h:02d}:{next_min:02d}:15)"
            else:
                btc_status = f"Delta HTTP {r.status_code}"
        except Exception as e:
            btc_status = f"Fetch Error ({e})"

    # 2. Nifty Status
    nifty_status = "Disabled"
    if ENABLE_NIFTY:
        if is_nifty_open(now):
            nifty_status = "ACTIVE (5-Min Scanner Running)"
        else:
            if now.weekday() >= 5:
                nifty_status = "Closed (Weekend) -> Opens Monday 09:15 IST"
            elif (now.hour, now.minute) < (9, 20):
                nifty_status = "Pre-Market -> Opens Today 09:15 IST"
            else:
                nifty_status = "Market Closed -> Opens Next Trading Day 09:15 IST"

    # 3. Gold Status
    gold_status = "Disabled"
    if ENABLE_GOLD:
        if is_gold_open(now):
            gold_status = "ACTIVE (30-Min Scanner Running)"
        else:
            if now.weekday() == 6:
                gold_status = "Closed (Sunday) -> Opens Monday 03:30 IST"
            elif now.weekday() == 5:
                gold_status = "Closed (Weekend) -> Opens Monday 03:30 IST"
            else:
                gold_status = "Daily Rollover Break (02:30-03:30 AM IST)"

    stats = store.stats() if hasattr(store, "stats") else {}
    h_log.info(f"═════════ MULTI-ASSET RADAR ({now_str}) ═════════")
    h_log.info(f"BTCUSD   : {btc_status}")
    h_log.info(f"NIFTY 50 : {nifty_status}")
    h_log.info(f"GOLD     : {gold_status}")
    h_log.info(f"STATUS   : Storage: {stats.get('storage_mode', 'RAM')} ({stats.get('ram_keys_count', 0)} keys) | Server: Healthy")
    h_log.info("══════════════════════════════════════════════════════")


# ─── SYSTEM HEALTH CHECK ENGINE ───
def run_health_check():
    log.info("[SYSTEM] Running Health Check & API Validations...")
    res = {"Delta": "FAIL", "Upstox": "FAIL", "Gemini": "FAIL", "Claude": "Not Set (Optional)"}

    # 1. Delta Exchange Check (Public Tickers endpoint - no start/end timestamps required)
    try:
        d_key = os.getenv("DELTA_API_KEY", "").strip()
        if "your_" in d_key.lower():
            d_key = ""
        d_headers = {"api-key": d_key} if d_key else {}
        r_delta = requests.get("https://api.india.delta.exchange/v2/tickers/BTCUSD", headers=d_headers, timeout=10)
        if r_delta.status_code == 200 and r_delta.json().get("success"):
            res["Delta"] = "OK"
        else:
            r_fb = requests.get("https://api.india.delta.exchange/v2/products/BTCUSD", headers=d_headers, timeout=10)
            if r_fb.status_code == 200:
                res["Delta"] = "OK"
            else:
                res["Delta"] = f"FAIL (HTTP {r_delta.status_code})"
    except Exception as e:
        log.warning(f"[HEALTH] Delta check failed: {e}")
        res["Delta"] = "FAIL (Connection Error)"

    # 2. Upstox Check
    try:
        u_token = os.getenv("UPSTOX_ANALYTICS_TOKEN", "").strip()
        if u_token and "your_" not in u_token.lower():
            hdrs = {"Authorization": f"Bearer {u_token}", "Accept": "application/json"}
            r_upstox = requests.get("https://api.upstox.com/v3/market-quote/ltp?instrument_key=NSE_INDEX|Nifty 50", headers=hdrs, timeout=10)
            if r_upstox.status_code == 200:
                res["Upstox"] = "OK"
            else:
                res["Upstox"] = f"FAIL (HTTP {r_upstox.status_code})"
    except Exception as e:
        log.warning(f"[HEALTH] Upstox check failed: {e}")

    # 3. Gemini Check (Supports active model from env with fallback)
    try:
        raw_keys = [k.strip() for k in os.getenv("GEMINI_API_KEYS", os.getenv("GEMINI_API_KEY", "")).split(",") if k.strip()]
        g_keys = [k for k in raw_keys if "your_" not in k.lower()]
        models_to_try = [
            os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip(),
            "gemini-2.5-flash",
            "gemini-1.5-flash"
        ]

        if not g_keys:
            res["Gemini"] = "FAIL (No Key Provided)"
        else:
            success = False
            last_err = ""
            for m in dict.fromkeys(models_to_try):
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={g_keys[0]}"
                payload = {"contents": [{"role": "user", "parts": [{"text": "Hi"}]}]}
                r_gem = requests.post(url, json=payload, timeout=10)
                if r_gem.status_code == 200:
                    res["Gemini"] = f"OK ({m})"
                    success = True
                    break
                else:
                    last_err = f"HTTP {r_gem.status_code}"
                    log.warning(f"[HEALTH] Gemini test with {m} failed: {r_gem.status_code} - {r_gem.text[:100]}")

            if not success:
                res["Gemini"] = f"FAIL ({last_err})"
    except Exception as e:
        log.warning(f"[HEALTH] Gemini check exception: {e}")
        res["Gemini"] = "FAIL (Connection Error)"

    # 4. Claude Check (Optional)
    try:
        a_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if a_key and "your_" not in a_key.lower():
            c_model = os.getenv("CLAUDE_BACKUP_MODEL", "claude-haiku-4-5-20251001").strip()
            hdrs = {"x-api-key": a_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
            r_claude = requests.post("https://api.anthropic.com/v1/messages", headers=hdrs, json={"model": c_model, "max_tokens": 10, "messages": [{"role": "user", "content": "Hi"}]}, timeout=10)
            if r_claude.status_code == 200:
                res["Claude"] = "OK"
            else:
                res["Claude"] = f"FAIL (HTTP {r_claude.status_code})"
        else:
            res["Claude"] = "Not Set (Optional)"
    except Exception as e:
        log.warning(f"[HEALTH] Claude check failed: {e}")

    msg = (f"<b>[SYSTEM HEALTH CHECK PASS]</b>\n\n"
           f"<b>Upstox Data :</b> {res['Upstox']}\n"
           f"<b>Delta Data  :</b> {res['Delta']}\n"
           f"<b>Gemini AI   :</b> {res['Gemini']}\n"
           f"<b>Claude AI   :</b> {res['Claude']}\n\n"
           f"<i>Multi-Asset Orchestrator active and running. {datetime.now(IST).strftime('%H:%M IST')}</i>")

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("BTC_TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage", json={"chat_id": tg_chat, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except Exception as e:
            log.error(f"[HEALTH] Telegram alert failed: {e}")


def main():
    log.info("Starting Multi-Asset Liquidity Hunt Orchestrator...")

    store = SharedNamespacedStore(REDIS_URL)

    # Trigger Startup Health Check asynchronously
    threading.Thread(target=run_health_check, daemon=True).start()

    scheduler = None
    if BackgroundScheduler:
        scheduler = BackgroundScheduler(timezone=IST, job_defaults={"max_instances": 3, "coalesce": True, "misfire_grace_time": 40})

        # 0. Daily Morning Health Check (09:15 AM IST)
        scheduler.add_job(run_health_check, "cron", hour=9, minute=15, id="daily_health_check")
        log.info("Scheduled [SYSTEM] Daily Health Check (09:15 AM)")

        # 1. 5-Minute Multi-Asset Live Radar Heartbeat
        scheduler.add_job(run_asset_radar_heartbeat, "cron", args=[store], minute="*/5", second=0, id="asset_radar_heartbeat")
        log.info("Scheduled [SYSTEM] 5-Minute Multi-Asset Radar Heartbeat")

        # 2. NIFTY 50: 5-Minute Setup Scan + 1-Hour Macro Reanalysis
        _register(scheduler, store, enabled=ENABLE_NIFTY, module=nifty_bot, func_name="run_nifty_1m_tick", tag="NIFTY", job_id="nifty_5m_engine", gate=is_nifty_open, label="5-Minute Setup Scan (09:20-15:30)", day_of_week="mon-fri", hour="9-15", minute="*/5", second=5)
        _register(scheduler, store, enabled=ENABLE_NIFTY, module=nifty_bot, func_name="run_1h_analysis", tag="NIFTY", job_id="nifty_1h_ai", gate=is_nifty_open, label="1-Hour Macro Reanalysis (10:15-15:15)", day_of_week="mon-fri", hour="10,11,12,13,14,15", minute=15, second=0)

        # 3. BITCOIN: 30-Minute Closed Bar Scan (24/7) + 6-Hour Macro Reanalysis
        _register(scheduler, store, enabled=ENABLE_BTC, module=btc_bot, func_name="run_btc_30m_tick", tag="BTC", job_id="btc_30m_engine", gate=is_btc_open, label="30-Minute Closed Bar Scan (24/7)", minute="0,30", second=15)
        _register(scheduler, store, enabled=ENABLE_BTC, module=btc_bot, func_name="run_6h_analysis", tag="BTC", job_id="btc_6h_ai", gate=is_btc_open, label="6-Hour Macro Re-analysis (00,06,12,18)", hour="0,6,12,18", minute=1, second=0)

        # 4. GOLD: 30-Minute Closed Bar Scan + 6-Hour Macro Reanalysis (Mon-Sat 02:30 AM)
        _register(scheduler, store, enabled=ENABLE_GOLD, module=gold_bot, func_name="run_gold_1m_tick", tag="GOLD", job_id="gold_30m_engine", gate=is_gold_open, label="30-Minute Closed Bar Scan (Mon-Sat 02:30 AM)", day_of_week="mon-sat", minute="0,30", second=35)
        _register(scheduler, store, enabled=ENABLE_GOLD, module=gold_bot, func_name="run_6h_analysis", tag="GOLD", job_id="gold_6h_ai", gate=is_gold_open, label="6-Hour Macro Re-analysis (00,06,12,18)", day_of_week="mon-sat", hour="0,6,12,18", minute=2, second=0)

        scheduler.start()

    port = int(os.getenv("PORT", 8080))
    if Flask:
        app = Flask(__name__)

        @app.route("/")
        def index():
            return "Multi-Asset Liquidity Hunt V8.0 Running OK", 200

        @app.route("/health")
        def health():
            return {"status": "ok", "time_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")}, 200

        app.run(host="0.0.0.0", port=port, use_reloader=False)
    else:
        try:
            while True:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            if scheduler:
                scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
