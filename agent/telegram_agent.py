"""
TelegramAgent - replaces old TelegramBot polling that talked to AIChat.
Now talks directly to Agent core (ReAct loop).
"""

import asyncio
import logging
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from config import Config

logger = logging.getLogger("telegram_agent")
TELEGRAM_MAX_LEN = 4000

def _split(text: str):
    if len(text) <= TELEGRAM_MAX_LEN:
        return [text]
    return [text[i:i+TELEGRAM_MAX_LEN] for i in range(0, len(text), TELEGRAM_MAX_LEN)]

class TelegramAgent:
    def __init__(self, trader, agent, memory):
        self.trader = trader
        self.agent = agent
        self.memory = memory
        self.bot = Bot(token=Config.TELEGRAM_BOT_TOKEN)
        self._auth = int(Config.TELEGRAM_USER_ID)
        self._loop = None
        self.trader.set_notify_callback(self._notify_from_thread)

    def _is_auth(self, uid: int) -> bool:
        return uid == self._auth

    def _notify_from_thread(self, msg: str):
        if self._loop is None or self._loop.is_closed():
            logger.warning(f"Notify dropped: {msg[:100]}")
            return
        asyncio.run_coroutine_threadsafe(self._send(msg), self._loop)

    async def _send(self, msg: str):
        try:
            for c in _split(msg):
                await self.bot.send_message(chat_id=self._auth, text=c)
        except Exception as e:
            logger.error(f"Notify fail: {e}")

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_auth(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        await update.message.reply_text(
            "🤖 XT AI AGENT Ready!\n\n"
            "This is NOT a bot anymore - I'm an autonomous agent.\n"
            "I analyze markets myself and decide to trade.\n\n"
            "Commands:\n"
            "/status - Agent + positions\n"
            "/balance - Wallet\n"
            "/pnl - PnL report\n"
            "/signal - Scan signals (with RSI veto)\n"
            "/agent - Force agent autonomous tick now\n"
            "/close [id] - Close position\n"
            "/settings - View settings\n"
            "/check_ai - Test brain\n"
            "/diag - Diagnose\n\n"
            "Or just chat naturally - I reason and can trade!"
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_auth(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        loop = asyncio.get_event_loop()
        report = await loop.run_in_executor(None, self.trader.get_status_report)
        # Fix legacy bot display: Agent is always ON, old Auto-Trade flag is irrelevant
        report = report.replace("Auto-Trade: OFF", "Agent: ON (autonomous every 60s)").replace("Auto-Trade: ON", "Agent: ON (autonomous every 60s)")
        report = f"🧠 Agent: {self.agent.get_model_info()} | AGENT MODE - always ON after deploy\n\n" + report
        for c in _split(report):
            await update.message.reply_text(c)

    async def cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_auth(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        pnl = self.memory.get_total_pnl()
        stats = self.memory.get_trade_count()
        await update.message.reply_text(f"PnL: {pnl:.4f} USDT\nTrades: {stats}\nAgent: {self.agent.get_model_info()}")

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_auth(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        loop = asyncio.get_event_loop()
        try:
            item = await loop.run_in_executor(None, self.trader.risk._get_usdt_balance, True)
            await update.message.reply_text(f"Wallet: {item.get('walletBalance')} USDT\nAvailable: {item.get('availableBalance')} USDT")
        except Exception as e:
            await update.message.reply_text(f"Balance error: {e}")

    async def cmd_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_auth(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        await update.message.reply_text("🤖 Agent thinking... analyzing market now")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self.agent.autonomous_tick)
        for c in _split(f"AGENT TICK:\n{result}"):
            await update.message.reply_text(c)

    async def cmd_check_ai(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_auth(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        await update.message.reply_text(f"Brain: {self.agent.get_model_info()}\nTesting...")
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, self.agent.chat, "Hello, confirm you are online with one line")
        await update.message.reply_text(f"Agent: {res}")

    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_auth(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        loop = asyncio.get_event_loop()
        if context.args:
            tid = int(context.args[0])
            res = await loop.run_in_executor(None, self.trader.close_specific_trade, tid)
        else:
            res = await loop.run_in_executor(None, self.trader.close_all_positions)
        await update.message.reply_text(res)

    async def cmd_diag(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_auth(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, self.trader.diagnose)
        for c in _split(res):
            await update.message.reply_text(c)

    async def cmd_signal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_auth(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        await update.message.reply_text("Scanning signals...")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self.trader.scanner.scan_and_report)
        report = self.trader.scanner.format_signal_report(result)
        for c in _split(report):
            await update.message.reply_text(c)

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        s = self.memory.get_all_settings()
        await update.message.reply_text("\n".join(f"{k}: {v}" for k,v in s.items()) or "No settings")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_auth(update.effective_user.id):
            await update.message.reply_text("Unauthorized.")
            return
        text = update.message.text.strip()
        if text.startswith("/"):
            await update.message.reply_text("Unknown command. /start for list or chat naturally")
            return
        await update.message.reply_chat_action("typing")
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, self.agent.chat, text)
        for c in _split(res):
            await update.message.reply_text(c)

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"TG error: {context.error}", exc_info=context.error)
        try:
            await self._send(f"⚠️ Error: {context.error}")
        except:
            pass

    def run(self):
        app = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("balance", self.cmd_balance))
        app.add_handler(CommandHandler("pnl", self.cmd_pnl))
        app.add_handler(CommandHandler("signal", self.cmd_signal))
        app.add_handler(CommandHandler("agent", self.cmd_agent))
        app.add_handler(CommandHandler("check_ai", self.cmd_check_ai))
        app.add_handler(CommandHandler("close", self.cmd_close))
        app.add_handler(CommandHandler("diag", self.cmd_diag))
        app.add_handler(CommandHandler("settings", self.cmd_settings))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        app.add_error_handler(self.error_handler)
        app.post_init = self._post_init
        logger.info("TelegramAgent polling started")
        app.run_polling()

    async def _post_init(self, app):
        from telegram import BotCommand
        self._loop = asyncio.get_running_loop()
        cmds = [
            BotCommand("start", "Agent help"),
            BotCommand("status", "Status + positions"),
            BotCommand("balance", "Balance"),
            BotCommand("pnl", "PnL"),
            BotCommand("signal", "Scan signals"),
            BotCommand("agent", "Force agent tick"),
            BotCommand("check_ai", "Test brain"),
            BotCommand("close", "Close position"),
            BotCommand("diag", "Diagnose"),
            BotCommand("settings", "Settings"),
        ]
        await app.bot.set_my_commands(cmds)
