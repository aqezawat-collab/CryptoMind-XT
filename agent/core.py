"""
Agent - ReAct loop replacing the old deterministic bot.

Instead of: if signal > threshold -> trade (bot)
We do: LLM sees market + balance + positions + indicators -> reasons -> decides (agent)

Both autonomous loop and chat go through same ReAct engine.
"""

import json
import logging
import time
from typing import List, Dict

logger = logging.getLogger("agent_core")

SYSTEM_PROMPT = """You are XT Futures AI Trader - a fully autonomous trading agent, NOT a chatbot assistant.

Your job is to analyze markets and make profitable trading decisions. You are the trader.

MARKET CONTEXT:
- You trade XT.com USDT perpetual futures
- Orders are in CONTRACTS: 1 btc_usdt contract = 0.0001 BTC, 1 doge_usdt contract = 10 DOGE
- Symbols are lowercase with underscore: btc_usdt, eth_usdt, sol_usdt
- Leverage is capped per symbol by notional bracket - may be clamped
- Every position must have TP/SL - exchange TP/SL is primary, software guard is backup
- ROI is return on margin (leverage-amplified)

YOUR CAPABILITIES (tools):
- get_status, get_balance, get_positions_detail, get_market_data, scan_market, get_contract_info
- open_trade (LONG/SHORT), close_trade, close_all_trades
- set_leverage, set_symbol, set_setting (for min_agreeing_strategies, report_interval_sec, timeframes, etc), manage_position, do_not_trade, remember

AGENT RULES:
1. ALWAYS start by calling get_status and/or get_balance + scan_market + get_market_data before deciding. Never trade blind.
2. Explain your reasoning step-by-step: trend, indicator reading, risk, position sizing.
3. If you open a trade, you MUST have a clear thesis (trend + confirmation).
4. If market is unclear, choppy, or risky - call do_not_trade with reason. Not trading is a valid decision.
5. Manage open positions: if ROI > 15% consider trailing, if opposite signal strong consider closing.
6. Be concise but show your work. User wants to see your reasoning.
7. NEVER say "I cannot trade" or "I am just an AI" - you CAN trade via open_trade and via set_setting you CAN change settings including min_agreeing_strategies and report_interval_sec. Do it when user asks.
8. You CAN change settings: min_agreeing_strategies (1-3), report_interval_sec (60-600), timeframes, leverage, etc via set_setting. If user asks to change them, do it.
9. Language: respond in same language as user (Persian/Finglish/English).
10. Risk: never risk more than available balance, check contract min notional before sizing.

EXAMPLE REASONING:
"BTC 1h uptrend, 15m pullback to EMA support, RSI 45 oversold, funding neutral. Scan shows LONG 78% confidence. Balance 120 USDT available. Thesis: long pullback to trend. Action: open_trade LONG"
vs
"BTC choppy 1h/4h, scan NEUTRAL 45%, RSI 50 no direction. Decision: do_not_trade - no edge now."
"""

class Agent:
    def __init__(self, trader, memory, brain):
        self.trader = trader
        self.memory = memory
        self.brain = brain
        from agent.tools import AgentTools, TOOLS
        self.tools = AgentTools(trader, memory)
        self.TOOLS = TOOLS
        from config import Config
        self.Config = Config

    def get_model_info(self) -> str:
        return self.brain.get_model_info()

    def _build_history_messages(self, user_message: str, limit: int = 20) -> List[Dict]:
        history = self.memory.get_chat_history(limit)
        # Remove last if duplicates user_message (avoid double user msg)
        msgs = []
        for m in history:
            msgs.append({"role": m["role"], "content": m["content"]})
        # If last is same as user_message, pop it to avoid dup
        if msgs and msgs[-1].get("role") == "user" and msgs[-1].get("content") == user_message:
            msgs.pop()
        return msgs

    def chat(self, user_message: str) -> str:
        """Chat with agent - full ReAct loop with tool calling."""
        self.memory.add_chat_message("user", user_message)
        context_summary = self.memory.get_trade_summary_for_ai()
        ai_ctx = self.memory.get_ai_context()

        system = SYSTEM_PROMPT
        system += f"\n\nCURRENT STATE:\n{context_summary}\n\nAI Memory:\n{json.dumps(ai_ctx, indent=2)}"

        messages: List[Dict] = [{"role": "system", "content": system}]
        # History without duplicating current message
        history = self._build_history_messages(user_message, 12)
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        max_steps = self.Config.AGENT_MAX_STEPS
        for step in range(max_steps):
            try:
                content, tool_calls, model_used = self.brain.chat(messages, tools=self.TOOLS, tool_choice="auto")
            except Exception as e:
                logger.error(f"Brain chat failed step {step}: {e}")
                err_msg = f"Agent brain error: {e}"
                self.memory.add_chat_message("assistant", err_msg)
                return err_msg

            if not tool_calls:
                # Final answer
                final = content or "No response."
                self.memory.add_chat_message("assistant", final)
                return final

            # Execute tools and append to messages
            # Need to add assistant message with tool_calls first
            messages.append({
                "role": "assistant",
                "content": content or "",
                "tool_calls": [{"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}} for tc in tool_calls]
            })

            for tc in tool_calls:
                result = self.tools.execute(tc["name"], tc["arguments"])
                logger.info(f"Agent step {step} tool {tc['name']}({tc['arguments']}) -> {result[:200]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tc["name"],
                    "content": result
                })

            # If last tool was do_not_trade or open/close, let loop continue to let LLM summarize

        # If max steps exceeded, get final summary
        try:
            content, _, _ = self.brain.chat(messages, tools=None)
            final = content or "Max steps reached without final answer."
            self.memory.add_chat_message("assistant", final)
            return final
        except Exception as e:
            return f"Max steps reached. Last error: {e}"

    def autonomous_tick(self) -> str:
        """
        One autonomous decision cycle.
        Called every AGENT_AUTONOMOUS_INTERVAL_SEC by the loop.
        The agent decides by itself whether to trade, manage, or wait.
        """
        prompt = (
            "This is your autonomous check. Analyze current market and positions, decide to trade or not.\n"
            "Steps: 1) get_status 2) get_balance 3) scan_market 4) get_market_data 5) decide open_trade or do_not_trade or manage_position.\n"
            "Be autonomous - you decide. Do not ask user for permission unless really risky."
        )
        # We don't add this to user chat history as it's system-initiated
        system = SYSTEM_PROMPT
        context_summary = self.memory.get_trade_summary_for_ai()
        system += f"\n\nCURRENT STATE:\n{context_summary}"
        messages: List[Dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ]

        max_steps = self.Config.AGENT_MAX_STEPS
        log = []
        for step in range(max_steps):
            try:
                content, tool_calls, _ = self.brain.chat(messages, tools=self.TOOLS, tool_choice="auto")
            except Exception as e:
                err = f"Autonomous tick brain error: {e}"
                logger.error(err)
                return err

            if not tool_calls:
                # Agent produced final reasoning
                final = content or "No action."
                logger.info(f"Autonomous tick final: {final[:300]}")
                return final

            messages.append({
                "role": "assistant",
                "content": content or "",
                "tool_calls": [{"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}} for tc in tool_calls]
            })

            for tc in tool_calls:
                result = self.tools.execute(tc["name"], tc["arguments"])
                log.append(f"{tc['name']} -> {result[:120]}")
                logger.info(f"Autonomous step {step} {tc['name']} -> {result[:200]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tc["name"],
                    "content": result
                })

                # If agent explicitly decided not to trade, we can finish early
                if tc["name"] == "do_not_trade":
                    return f"Agent decided NOT to trade: {tc['arguments'].get('reason','')} | {content}"

                if tc["name"] in ("open_trade", "close_trade", "close_all_trades"):
                    # Let one more loop to allow agent to summarize
                    pass

        # After max steps, ask for summary
        try:
            content, _, _ = self.brain.chat(messages, tools=None)
            return content or f"Autonomous tick steps: {'; '.join(log)}"
        except Exception as e:
            return f"Tick done, log: {'; '.join(log)} error: {e}"

    def remember(self, key: str, value: str):
        self.memory.set_ai_context(key, value)

    def recall(self, key: str = None):
        return self.memory.get_ai_context(key)
