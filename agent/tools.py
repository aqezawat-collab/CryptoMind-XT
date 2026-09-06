"""
AgentTools - All XT + memory tools exposed to LLM as functions.
This replaces the old ai_chat FUNCTIONS list but with real trading reasoning.
Signal scanner is kept as a TOOL, not as a gate.
"""

import logging
from typing import Dict, List

logger = logging.getLogger("agent_tools")

# Tool definitions exposed to LLM (OpenAI function schema)
TOOLS = [
    {
        "name": "get_status",
        "description": "Get current bot status: open positions, PnL, settings, cooldowns. Always call first to understand state.",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "get_balance",
        "description": "Read live USDT futures balance from XT (wallet, available, frozen).",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "get_market_data",
        "description": "Get current price, klines, and funding rate for a symbol. Use to analyze before trading.",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "e.g. btc_usdt, eth_usdt (lowercase with underscore)"},
            "interval": {"type": "string", "description": "kline interval: 1m,5m,15m,1h,4h,1d", "enum": ["1m","3m","5m","15m","30m","1h","2h","4h","1d"]},
        }, "required": []}
    },
    {
        "name": "scan_market",
        "description": "Run technical indicator scan (RSI/EMA/MACD/BB etc) across timeframes. Returns direction, confidence, signal_strength. Agent should interpret this, not blindly follow.",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "optional symbol, defaults to current"}
        }}
    },
    {
        "name": "get_contract_info",
        "description": "Get contract specs: contract size, min order, min notional, max leverage, price tick.",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "e.g. btc_usdt"}
        }}
    },
    {
        "name": "open_trade",
        "description": "Open a futures trade. Only use when you have strong conviction after analyzing market. Calculates size automatically.",
        "parameters": {"type": "object", "properties": {
            "direction": {"type": "string", "enum": ["LONG", "SHORT"], "description": "Trade direction"},
            "symbol": {"type": "string", "description": "e.g. btc_usdt (defaults to current symbol)"},
            "leverage": {"type": "integer", "description": "Leverage 1-125 (defaults to setting)"},
        }, "required": ["direction"]}
    },
    {
        "name": "close_trade",
        "description": "Close a specific open trade by ID.",
        "parameters": {"type": "object", "properties": {
            "trade_id": {"type": "integer", "description": "Trade ID to close"}
        }, "required": ["trade_id"]}
    },
    {
        "name": "close_all_trades",
        "description": "Close all open positions immediately.",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "set_leverage",
        "description": "Set leverage for next trades.",
        "parameters": {"type": "object", "properties": {
            "leverage": {"type": "integer", "description": "1-125"}
        }, "required": ["leverage"]}
    },
    {
        "name": "set_symbol",
        "description": "Change trading pair.",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "e.g. btc_usdt, eth_usdt, sol_usdt"}
        }, "required": ["symbol"]}
    },
    {
        "name": "get_positions_detail",
        "description": "Get detailed PnL for all open positions from exchange (entry, mark, ROI, unrealized PnL).",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "manage_position",
        "description": "Run mid-position management (breakeven, trailing stop, PnL report).",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "do_not_trade",
        "description": "Explicitly decide to NOT trade now and explain why. Use this when market is unclear or risky.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "Why not trading now"}
        }, "required": ["reason"]}
    },
    {
        "name": "remember",
        "description": "Store a note in long-term memory (market observation, lesson, preference).",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "Memory key"},
            "value": {"type": "string", "description": "Memory value"}
        }, "required": ["key", "value"]}
    },
    {
        "name": "set_setting",
        "description": "Change a trading setting (min_agreeing_strategies, report_interval_sec, timeframes, etc). Use to tune strategy.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "Setting key: min_agreeing_strategies, report_interval_sec, timeframes, min_confidence, tf_min_confidence, leverage, margin_amount_pct, etc"},
            "value": {"type": "string", "description": "New value (e.g. '2' for min_agreeing_strategies, '120' for report_interval_sec, '1m,5m,15m,4h' for timeframes)"}
        }, "required": ["key", "value"]}
    },
    {
        "name": "reset_cooldown",
        "description": "Reset cooldown for a symbol/side so you can trade immediately. Use when cooldown blocks a valid entry.",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "e.g. uai_usdt, defaults to current symbol"},
            "side": {"type": "string", "description": "LONG, SHORT, or ALL", "enum": ["LONG", "SHORT", "ALL"]}
        }}
    },
]


class AgentTools:
    def __init__(self, trader, memory):
        self.trader = trader
        self.memory = memory
        from config import Config
        self.Config = Config

    def execute(self, name: str, args: Dict) -> str:
        handlers = {
            "get_status": self._get_status,
            "get_balance": self._get_balance,
            "get_market_data": self._get_market_data,
            "scan_market": self._scan_market,
            "get_contract_info": self._get_contract_info,
            "open_trade": self._open_trade,
            "close_trade": self._close_trade,
            "close_all_trades": self._close_all_trades,
            "set_leverage": self._set_leverage,
            "set_symbol": self._set_symbol,
            "get_positions_detail": self._get_positions_detail,
            "manage_position": self._manage_position,
            "do_not_trade": self._do_not_trade,
            "remember": self._remember,
            "set_setting": self._set_setting,
            "reset_cooldown": self._reset_cooldown,
        }
        h = handlers.get(name)
        if not h:
            return f"Unknown tool: {name}"
        try:
            return h(args or {})
        except Exception as e:
            logger.error(f"Tool {name} failed: {e}", exc_info=True)
            return f"Tool {name} error: {e}"

    def _get_status(self, args: Dict) -> str:
        if not self.trader:
            return self.memory.get_trade_summary_for_ai()
        return self.trader.get_status_report()

    def _get_balance(self, args: Dict) -> str:
        try:
            item = self.trader.risk._get_usdt_balance(force=True)
        except Exception as e:
            return f"Failed to read balance: {e}"
        if not item:
            return "No USDT balance returned."
        return (f"Wallet: {item.get('walletBalance')} USDT\n"
                f"Available: {item.get('availableBalance')} USDT\n"
                f"Frozen: {item.get('openOrderMarginFrozen')} USDT\n"
                f"Isolated: {item.get('isolatedMargin')} USDT\n"
                f"Crossed: {item.get('crossedMargin')} USDT")

    def _get_market_data(self, args: Dict) -> str:
        symbol = args.get("symbol") or self.memory.get_setting("symbol", self.Config.DEFAULT_SYMBOL)
        interval = args.get("interval") or "1h"
        try:
            ticker = self.trader.xt.get_agg_ticker(symbol) or {}
            klines = self.trader.xt.get_klines(symbol, interval, limit=20) or []
            funding = self.trader.xt.get_funding_rate(symbol) or {}
            price = ticker.get("lastPrice") or ticker.get("price") or "?"
            out = f"{symbol} price: {price}\n"
            out += f"Ticker: {ticker}\n"
            out += f"Funding: {funding}\n"
            out += f"Last {len(klines)} klines ({interval}):\n"
            for k in klines[-5:]:
                out += f"  {k}\n"
            return out
        except Exception as e:
            return f"Market data error for {symbol}: {e}"

    def _scan_market(self, args: Dict) -> str:
        symbol = args.get("symbol") or self.memory.get_setting("symbol", self.Config.DEFAULT_SYMBOL)
        try:
            result = self.trader.scanner.scan_and_report(symbol)
            report = self.trader.scanner.format_signal_report(result)
            return report
        except Exception as e:
            return f"Scan failed for {symbol}: {e}"

    def _get_contract_info(self, args: Dict) -> str:
        symbol = args.get("symbol") or self.memory.get_setting("symbol", self.Config.DEFAULT_SYMBOL)
        try:
            cs = self.trader.risk.get_contract_size(symbol)
            price = self.trader.scanner.get_current_price(symbol)
            one = cs * price if price else 0
            return (f"{symbol}\n"
                    f"Contract size: {cs} ({one:.4f} USDT/contract @ {price})\n"
                    f"Min order: {self.trader.risk.get_min_qty(symbol)} contracts\n"
                    f"Min notional: {self.trader.risk.get_min_notional(symbol)} USDT\n"
                    f"Max leverage: {self.trader.risk.get_max_leverage(symbol)}x\n"
                    f"Price tick: {self.trader.risk.get_price_step(symbol)}")
        except Exception as e:
            return f"Contract info error for {symbol}: {e}"

    def _open_trade(self, args: Dict) -> str:
        if self.Config.AGENT_DRY_RUN == "true":
            return f"[DRY_RUN] Would open {args.get('direction')} {args.get('symbol','?')} leverage={args.get('leverage','?')} - dry run enabled, no order placed."
        direction = args.get("direction")
        if direction not in ("LONG", "SHORT"):
            return "Invalid direction, use LONG or SHORT"
        symbol = args.get("symbol")
        if symbol:
            # Temporarily set symbol in memory for this trade
            self.memory.set_setting("symbol", symbol.lower())
        leverage = args.get("leverage")
        if leverage:
            self.memory.set_setting("leverage", int(leverage))
        return self.trader.execute_trade(direction, order_type="MARKET")

    def _close_trade(self, args: Dict) -> str:
        tid = int(args["trade_id"])
        return self.trader.close_specific_trade(tid)

    def _close_all_trades(self, args: Dict) -> str:
        return self.trader.close_all_positions()

    def _set_leverage(self, args: Dict) -> str:
        lev = int(args["leverage"])
        lev = max(1, min(lev, 125))
        self.memory.set_setting("leverage", lev)
        return f"Leverage set to {lev}x"

    def _set_symbol(self, args: Dict) -> str:
        sym = args["symbol"].lower().strip()
        try:
            detail = self.trader.xt.get_symbol_detail(sym)
            if not detail or not detail.get("symbol"):
                return f"Symbol {sym} not found on XT."
        except Exception as e:
            return f"Could not validate {sym}: {e}"
        self.memory.set_setting("symbol", sym)
        return f"Symbol set to {sym}"

    def _get_positions_detail(self, args: Dict) -> str:
        open_trades = self.memory.get_open_trades()
        if not open_trades:
            return "No open positions."
        out = ""
        batch = self.trader.position_mgr.get_positions_batch_optimized()
        for t in open_trades:
            pos = self.trader.position_mgr.get_position_pnl_optimized(t["symbol"], t["position_side"], positions_batch=batch)
            if not pos["exists"]:
                out += f"ID:{t['id']} {t['symbol']} {t['position_side']} NOT FOUND ON EXCHANGE\n"
            else:
                out += (f"ID:{t['id']} {t['symbol']} {t['position_side']} Entry:{pos['entry_price']} Mark:{pos['mark_price']} "
                        f"Size:{int(pos['position_size'])}c PnL:{pos['unrealized_pnl']:.4f} ROI:{pos['roi']:.2f}% Lev:{pos['leverage']}x\n")
        return out

    def _manage_position(self, args: Dict) -> str:
        return self.trader.run_mid_management()

    def _do_not_trade(self, args: Dict) -> str:
        reason = args.get("reason", "No reason")
        self.memory.add_chat_message("assistant", f"Decision: DO NOT TRADE - {reason}")
        return f"Decision recorded: DO NOT TRADE - {reason}"

    def _reset_cooldown(self, args: Dict) -> str:
        symbol = (args.get("symbol") or self.memory.get_setting("symbol", self.Config.DEFAULT_SYMBOL)).lower()
        side = (args.get("side") or "ALL").upper()
        try:
            from sqlalchemy import text as sql_text
            with self.memory._engine.begin() as conn:
                if side == "ALL":
                    conn.execute(sql_text("DELETE FROM cooldowns WHERE symbol=:s"), {"s": symbol})
                    # also delete generic
                    conn.execute(sql_text("DELETE FROM cooldowns WHERE symbol=:s"), {"s": symbol})
                else:
                    conn.execute(sql_text("DELETE FROM cooldowns WHERE symbol=:s AND side=:side"), {"s": symbol, "side": side})
            return f"Cooldown reset for {symbol} {side} - you can trade now"
        except Exception as e:
            # fallback: set to 0
            try:
                self.memory.set_cooldown(symbol, "LONG", 0)
                self.memory.set_cooldown(symbol, "SHORT", 0)
                return f"Cooldown reset via fallback for {symbol}: {e}"
            except Exception as e2:
                return f"Reset failed: {e2}"

    def _remember(self, args: Dict) -> str:
        self.memory.set_ai_context(args["key"], args["value"])
        return f"Remembered {args['key']}: {args['value']}"

    def _set_setting(self, args: Dict) -> str:
        key = args.get("key", "").strip()
        value = args.get("value", "").strip()
        if not key:
            return "Missing key"
        # Validate and set known settings
        try:
            if key in ("min_agreeing_strategies", "signal_confirm_scans", "tf_min_confidence", "min_confidence", "leverage", "max_positions", "cooldown_minutes", "scan_interval_sec", "guard_interval_sec", "report_interval_sec", "reversal_confidence"):
                self.memory.set_setting(key, int(float(value)))
                return f"Setting {key} set to {value} (int)"
            elif key in ("margin_amount_pct", "margin_risk_pct", "max_loss_pct", "max_profit_pct", "breakeven_threshold_pct", "trailing_stop_pct", "trailing_trigger_roi_pct", "trailing_distance_pct", "sl_liquidation_safety"):
                self.memory.set_setting(key, float(value))
                return f"Setting {key} set to {value} (float)"
            elif key in ("timeframes", "symbol", "margin_mode", "position_mode", "on_tpsl_failure"):
                self.memory.set_setting(key, value)
                return f"Setting {key} set to {value}"
            elif key in ("reversal_enabled",):
                self.memory.set_setting(key, value.lower() in ("1", "true", "yes", "on"))
                return f"Setting {key} set to {value}"
            else:
                self.memory.set_setting(key, value)
                return f"Setting {key} set to {value} (generic)"
        except Exception as e:
            return f"Failed to set {key}: {e}"
