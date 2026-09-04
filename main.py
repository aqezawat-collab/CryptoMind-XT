import logging
import os
import signal
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from config import Config
from bot.memory import LongTermMemory
from bot.trader import XTTrader

is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None

handlers = [logging.StreamHandler(sys.stdout)]
if not is_railway:
    os.makedirs("logs", exist_ok=True)
    handlers.append(logging.FileHandler("logs/trader.log"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=handlers,
)
logger = logging.getLogger("main")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def _start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health check server listening on port {port}")
    server.serve_forever()


def _resolve_xt_credentials():
    """Load XT API key/secret from env or the AItradekit credential files.

    Mirrors AItradekit's load_credentials() so CryptoMind-XT runs in the same
    environment without re-entering the secret. Returns ("", "") if unset.
    """
    ak = os.getenv("XT_API_KEY", "")
    sk = os.getenv("XT_API_SECRET", "")
    if ak and sk:
        return ak, sk
    import json
    for path in (os.path.expanduser("~/.xt-tradekit/credentials.json"),
                 os.path.expanduser("~/.xt-exchange/credentials.json")):
        if os.path.exists(path):
            try:
                with open(path) as f:
                    creds = json.load(f)
                ak = creds.get("access_key", "")
                sk = creds.get("secret_key", "")
                if ak and sk:
                    return ak, sk
            except Exception:
                continue
    return "", ""


def run_headless():
    """Run the auto-trader without Telegram / AI / MySQL.

    Uses XT credentials + a SQLite database and logs every notification (trades,
    reversals, PnL reports) to the logger, so a tail of the log is the status
    feed. This is the unified, single-process replacement for the standalone
    signal_bot + cron setup.
    """
    logger.info("Starting CryptoMind-XT in HEADLESS mode (no Telegram / AI).")
    ak, sk = _resolve_xt_credentials()
    if not ak or not sk:
        logger.error("XT credentials not found. Set XT_API_KEY/XT_API_SECRET or "
                     "place them in ~/.xt-tradekit/credentials.json.")
        sys.exit(1)
    Config.XT_API_KEY = ak
    Config.XT_API_SECRET = sk

    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        database_url = "sqlite:///data/memory.db"
        logger.warning(
            "DATABASE_URL is not set — falling back to SQLite at data/memory.db. "
            "ALL DATA (trades, settings, history) WILL BE LOST on every deploy. "
            "Add a MySQL service to Railway and set: "
            "DATABASE_URL=${{MySQL.MYSQL_URL}} (or ${{MySQL.DATABASE_URL}})."
        )
    else:
        logger.info(f"Database: {'MySQL' if 'mysql' in database_url else 'SQLite'}")

    memory = LongTermMemory(database_url=database_url)
    seeded = [k for k, v in Config.default_settings().items()
              if memory.set_setting_default(k, v)]
    if seeded:
        logger.info(f"Seeded default settings: {', '.join(sorted(seeded))}")
    else:
        logger.info("Existing settings preserved")

    trader = XTTrader(memory=memory)
    # Surface every notification (trades, reversals, PnL reports) via the log.
    trader.set_notify_callback(lambda m: logger.info(f"NOTIFY: {m}"))

    try:
        balances = trader.xt.get_balances()
        usdt = next((b for b in balances if str(b.get("coin", "")).upper() == "USDT"), None)
        if usdt:
            logger.info(f"XT connection OK. USDT wallet balance: "
                        f"{usdt.get('walletBalance')}")
        else:
            logger.warning("XT connection OK but no USDT balance found.")
    except Exception as e:
        logger.error(f"XT API check failed: {e}")

    try:
        adopted = trader.position_mgr.adopt_exchange_positions()
        for a in adopted:
            logger.warning(f"Adopted untracked position: {a['symbol']} "
                           f"{a['position_side']} {a['size']}c @ {a['entry_price']} "
                           f"{a['leverage']}x, stop={'yes' if a['has_stop'] else 'NO'}")
        if not adopted:
            logger.info("No untracked exchange positions found")
    except Exception as e:
        logger.error(f"Position adoption failed at startup: {e}")

    trader.start_auto_trade()
    # Breakeven/trailing/TP-SL protection runs regardless of auto-trade.
    trader.start_mid_manager()
    logger.info("Headless auto-trade started. Send SIGINT (Ctrl+C) to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down via KeyboardInterrupt...")
    finally:
        trader.stop_mid_manager()
        trader.stop_auto_trade()
        memory.close()
        logger.info("CryptoMind-XT stopped.")


def main():
    if "--headless" in sys.argv:
        run_headless()
        return
    # Telegram + AI are only needed for the interactive bot; importing them here
    # (not at module top) keeps the headless path free of those heavy deps.
    from bot.ai_chat import AIChat
    from bot.telegram_bot import TelegramBot
    missing = Config.validate()
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        logger.error("Set them in Railway Variables or .env file.")
        sys.exit(1)

    logger.info("Initializing XT AI Trader...")
    logger.info(f"AI Model: {Config.AI_MODEL}")
    logger.info(f"Railway: {is_railway}")
    logger.info(f"PORT env: {os.getenv('PORT', '<unset>')}")

    # Railway always injects PORT; fall back to it as the deploy signal so the
    # health endpoint comes up even if RAILWAY_ENVIRONMENT is absent.
    should_serve_health = is_railway or os.getenv("PORT") is not None
    if should_serve_health:
        # Bring the health port up before any slow initialisation, otherwise
        # the platform's proxy kills the deploy before it is marked healthy.
        health_thread = threading.Thread(target=_start_health_server, daemon=True)
        health_thread.start()

    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        database_url = "sqlite:///data/memory.db"
        logger.warning("DATABASE_URL is not set, falling back to SQLite at "
                       "data/memory.db. On Railway that file lives inside the "
                       "container and is DESTROYED on every deploy, so open trades "
                       "and settings will be lost. Point DATABASE_URL at the MySQL "
                       "service (e.g. DATABASE_URL = ${{ MySQL.MYSQL_URL }} in the "
                       "service's Variables).")
    else:
        masked = database_url.split("@")[-1] if "@" in database_url else database_url
        logger.info(f"DATABASE_URL resolved to host: {masked}")
    logger.info(f"Database: {'MySQL' if 'mysql' in database_url else 'SQLite'}")

    memory = None
    try:
        memory = LongTermMemory(database_url=database_url)
        logger.info("Database schema ready")
    except Exception as e:
        logger.error(f"Database init failed: {e}")
        logger.error("Fix DATABASE_URL / MySQL availability before redeploying.")
        sys.exit(1)

    # Only seed missing keys, otherwise every Railway restart would wipe the
    # settings the user configured over Telegram.
    seeded = [k for k, v in Config.default_settings().items()
              if memory.set_setting_default(k, v)]
    if seeded:
        logger.info(f"Seeded default settings: {', '.join(sorted(seeded))}")
    else:
        logger.info("Existing settings preserved")

    trader = XTTrader(memory=memory)
    ai_chat = AIChat(memory=memory)
    ai_chat.bind_trader(trader)
    logger.info(f"AI model: {ai_chat.get_model_info()}")

    try:
        balances = trader.xt.get_balances()
        usdt = next((b for b in balances if str(b.get("coin", "")).upper() == "USDT"), None)
        if usdt:
            logger.info(f"XT connection OK. USDT wallet balance: {usdt.get('walletBalance')}")
        else:
            logger.warning("XT connection OK but no USDT balance found. "
                           "Is the futures account opened?")
    except Exception as e:
        logger.error(f"XT API check failed: {e}")
        logger.error("Verify XT_API_KEY / XT_API_SECRET, that the key has futures "
                     "permissions, and that this server's IP is whitelisted.")

    # A wiped database or a manually opened position would otherwise be
    # invisible to every guard, since they all iterate the local trade table.
    try:
        adopted = trader.position_mgr.adopt_exchange_positions()
        for a in adopted:
            logger.warning(f"Adopted untracked position: {a['symbol']} "
                           f"{a['position_side']} {a['size']}c @ {a['entry_price']} "
                           f"{a['leverage']}x, stop={'yes' if a['has_stop'] else 'NO'}")
        if not adopted:
            logger.info("No untracked exchange positions found")
    except Exception as e:
        logger.error(f"Position adoption failed at startup: {e}")

    telegram_bot = TelegramBot(trader=trader, ai_chat=ai_chat, memory=memory)
    # Start the always-on mid-management guardian (breakeven + trailing stop +
    # TP/SL protection). It fires by itself - no /midmanage required - and the
    # notify callback is already wired by TelegramBot above.
    trader.start_mid_manager()

    logger.info("XT AI Trader started. Telegram bot is listening...")
    logger.info("Send /start to your bot to begin.")

    try:
        telegram_bot.run()
    except KeyboardInterrupt:
        logger.info("Shutting down via KeyboardInterrupt...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        trader.stop_mid_manager()
        trader.stop_auto_trade()
        memory.close()
        logger.info("XT AI Trader stopped.")


if __name__ == "__main__":
    main()
