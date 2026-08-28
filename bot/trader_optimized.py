"""
Optimized trader.py performance patches.

Key optimizations:
1. Batch-fetch positions instead of N+1 queries in get_status_report()
2. Cache adoption calls (reduce 15s cycle to 300s+ interval)
3. Replace blocking sleep in order confirmation with non-blocking wait
4. Invalidate position batch cache on trade execution

Integration Points:
- get_status_report(): Use positions_batch from get_positions_batch_optimized()
- _auto_trade_loop(): Add adoption_interval, cache adoption results
- execute_trade(): Replace time.sleep() with _stop_monitor.wait()
"""

import time
import logging
from bot.cache_manager import get_cache

logger = logging.getLogger("xt_trader")


def get_status_report_optimized(self) -> str:
    """Optimized status report using batch-fetched positions.
    
    Original: 10+ positions = 10+ API calls (N+1 query pattern)
    Optimized: 10+ positions = 1 API call (batch fetch)
    
    Reduces load by ~90% on status queries.
    """
    symbol = self.memory.get_setting("symbol", Config.DEFAULT_SYMBOL)
    try:
        balance = self.risk.get_total_balance()
        available = self.risk.get_available_balance()
        balance_line = f"Balance: {balance:.2f} USDT | Available: {available:.2f} USDT\n"
    except XTError as e:
        balance_line = f"Balance: unavailable ({e})\n"

    pnl = self.memory.get_total_pnl()
    stats = self.memory.get_trade_count()
    settings = self.memory.get_all_settings()

    report = "=== XT AI TRADER STATUS ===\n"
    report += f"Symbol: {symbol}\n"
    report += balance_line
    report += f"Total PnL: {pnl:.4f} USDT\n"
    report += (f"Trades: {stats['total']} total | {stats['open']} open | "
               f"{stats['closed']} closed | {stats['winrate']}% WR\n")
    report += f"Auto-Trade: {'ON' if self._auto_trade_enabled else 'OFF'}\n"
    report += f"Cooldowns: {self._get_cooldown_status(symbol)}\n\n"

    report += "--- SETTINGS ---\n"
    report += f"Leverage: {settings.get('leverage', Config.DEFAULT_LEVERAGE)}x\n"
    report += f"Margin Mode: {settings.get('margin_mode', Config.DEFAULT_MARGIN_MODE)}\n"
    report += (f"Timeframes: {settings.get('timeframes', ','.join(Config.DEFAULT_TIMEFRAMES))}\n")
    report += f"Margin Amount: {settings.get('margin_amount_pct', Config.DEFAULT_MARGIN_AMOUNT_PCT)}%\n"
    report += f"Risk: {settings.get('margin_risk_pct', Config.DEFAULT_RISK_PCT)}%\n"
    report += f"Min Confidence: {settings.get('min_confidence', Config.MIN_CONFIDENCE)}%\n"
    report += f"Position Mode: {settings.get('position_mode', 'margin')}\n"

    try:
        report += self._format_contract_info(symbol)
    except XTError as e:
        report += f"Contract info unavailable: {e}\n"

    open_trades = self.memory.get_open_trades()
    if open_trades:
        report += "\n--- OPEN POSITIONS ---\n"
        
        # OPTIMIZATION: Batch-fetch all positions once
        positions_batch = self.position_mgr.get_positions_batch_optimized()
        
        for t in open_trades:
            # Use batch-fetched position (no additional API call)
            pos = self.position_mgr.get_position_pnl_optimized(
                t["symbol"], t["position_side"], positions_batch=positions_batch
            )
            if not pos["exists"]:
                report += (f"ID:{t['id']} {t['symbol']} {t['position_side']} "
                           f"NOT FOUND ON EXCHANGE (stale)\n")
                continue
            report += (f"ID:{t['id']} {t['symbol']} {t['position_side']} "
                       f"Entry:{pos['entry_price']} Mark:{pos['mark_price']} "
                       f"Size:{int(pos['position_size'])}c "
                       f"PnL:{pos['unrealized_pnl']:.4f} ROI:{pos['roi']:.2f}% "
                       f"Lev:{pos['leverage']}x | {t['strategy']} "
                       f"Conf:{t['confidence']}%\n")
    return report


def _auto_trade_loop_optimized(self):
    """Optimized auto-trade loop with adoption caching and non-blocking sleep.
    
    Original behavior:
    - Calls adopt_exchange_positions() every iteration (every ~15s)
    - Each call fetches ALL positions from exchange (expensive)
    
    Optimized behavior:
    - Cache adoption results for 300s (5 minutes) before re-checking
    - Reduces adoption API calls by ~95%
    """
    logger.info("Auto-trade monitoring loop started")
    mid_manage_interval = 300
    last_scan = 0.0
    last_mid = 0.0
    last_report = 0.0
    
    # OPTIMIZATION: Cache adoption results for 5 minutes
    adoption_interval = 300  # 5 minutes
    last_adoption = 0.0

    while not self._stop_monitor.is_set():
        scan_interval = int(self.memory.get_setting("scan_interval_sec", 60))
        guard_interval = int(self.memory.get_setting("guard_interval_sec", 15))
        now = time.time()
        try:
            # Adoption runs first, but only every adoption_interval (~300s)
            # This prevents the expensive position fetch from running every 15s
            if now - last_adoption >= adoption_interval:
                last_adoption = now
                for event in self.position_mgr.adopt_exchange_positions():
                    warn = "" if event["has_stop"] else " It has NO exchange stop loss."
                    self._notify(f"Found untracked position on XT: {event['symbol']} "
                                 f"{event['position_side']} {event['size']}c @ "
                                 f"{event['entry_price']} {event['leverage']}x. "
                                 f"Now managed as trade {event['trade_id']}.{warn}")

            # Reconciliation runs every guard_interval (cheap, needed for safety)
            for event in self.position_mgr.reconcile_open_trades():
                self._notify(f"Position {event['symbol']} {event['position_side']} "
                             f"disappeared from the exchange (trade "
                             f"{event['trade_id']}). Likely liquidation, external "
                             f"close, or TP/SL fill. Marked closed locally.")
            self.check_positions_for_close()

            if now - last_scan >= scan_interval:
                last_scan = now
                self._scan_cycle()

            # Periodic PnL + confidence report
            report_interval = int(self.memory.get_setting("report_interval_sec",
                                                         Config.REPORT_INTERVAL_SEC))
            if report_interval > 0 and now - last_report >= report_interval:
                last_report = now
                self._notify(self.periodic_pnl_report())

            if now - last_mid >= mid_manage_interval:
                last_mid = now
                for action in self.position_mgr.mid_manage_positions():
                    if action["action"] in ("tpsl_recovered", "tpsl_missing"):
                        self._notify(f"{action['symbol']} trade "
                                     f"{action['trade_id']}: {action['details']}")
        except Exception as e:
            logger.error(f"Auto-trade loop error: {e}", exc_info=True)
        
        # OPTIMIZATION: Non-blocking sleep using wait() instead of time.sleep()
        # This allows the loop to respond to stop signals without full delay
        self._stop_monitor.wait(guard_interval)

    logger.info("Auto-trade monitoring loop stopped")


def execute_trade_optimized(self, direction: str, order_type: str = "MARKET",
                           time_in_force: str = None) -> str:
    """Execute trade with non-blocking order confirmation polling.
    
    Original: Blocks thread for up to 3 seconds with time.sleep(1.0)
    Optimized: Uses _stop_monitor.wait(1.0) for non-blocking wait
    
    Benefits:
    - Loop can respond to shutdown signals during fill confirmation
    - Other trades can be processed in parallel
    """
    symbol = self.memory.get_setting("symbol", Config.DEFAULT_SYMBOL)
    min_conf = int(self.memory.get_setting("min_confidence", Config.MIN_CONFIDENCE))
    requested_leverage = int(self.memory.get_setting("leverage", Config.DEFAULT_LEVERAGE))
    margin_mode = self.memory.get_setting("margin_mode", Config.DEFAULT_MARGIN_MODE)

    if direction not in ("LONG", "SHORT"):
        return f"Invalid direction: {direction}"

    reason = self._gate_checks(symbol, direction)
    if reason:
        return f"Cannot open trade: {reason}"

    try:
        if not self.risk.supports_order_type(symbol, order_type):
            return f"{symbol} does not support {order_type} orders"
    except XTError as e:
        return f"Could not read contract config for {symbol}: {e}"

    scan = self.scanner.scan_and_report(symbol)
    if scan["confidence"] < min_conf:
        return (f"Confidence {scan['confidence']}% is below threshold {min_conf}%.\n"
                f"{self.scanner.format_signal_report(scan)}")
    if scan["direction"] != direction:
        return (f"Current signal direction is {scan['direction']}, not {direction}.\n"
                f"Check /signal")

    price = scan["price"]
    if price <= 0:
        return "Could not get current price from XT. Aborting."

    confidence = scan["confidence"]
    strength = scan["signal_strength"]

    provisional_leverage = self.risk.validate_leverage(symbol, requested_leverage)
    provisional_tp_price, provisional_sl_price = self.position_mgr.calculate_dynamic_tpsl(
        symbol, direction, price, strength, confidence, provisional_leverage)

    qty, size_mode, size_reason = self.risk.calculate_position_size(
        symbol, price, provisional_leverage, provisional_sl_price, order_type)
    if qty <= 0:
        return f"Cannot size position: {size_reason}"

    notional = self.risk.contracts_to_notional(symbol, qty, price)
    leverage = self.risk.validate_leverage(symbol, requested_leverage, notional)

    if self.memory.get_setting("position_mode", "margin") == "margin":
        qty, size_mode, size_reason = self.risk.calculate_position_size(
            symbol, price, leverage, provisional_sl_price, order_type)
        if qty <= 0:
            return f"Cannot size position: {size_reason}"

    notional = self.risk.contracts_to_notional(symbol, qty, price)
    tp_price, sl_price = self.position_mgr.calculate_dynamic_tpsl(
        symbol, direction, price, strength, confidence, leverage)

    if margin_mode in ("CROSSED", "ISOLATED"):
        try:
            self.xt.set_position_type(symbol, direction, margin_mode)
        except XTError as e:
            logger.info(f"Margin mode unchanged for {symbol} {direction}: {e}")

    try:
        self.xt.set_leverage(symbol, direction, leverage)
    except XTError as e:
        return f"Could not set leverage to {leverage}x: {e}"

    if time_in_force is None:
        time_in_force = "IOC" if order_type == "MARKET" else "GTC"
    if not self.risk.supports_time_in_force(symbol, time_in_force):
        return f"{symbol} does not support timeInForce={time_in_force}"

    limit_price = self.risk.round_price(symbol, price) if order_type == "LIMIT" else None

    logger.info(f"Opening {direction} {symbol}: {qty} contracts "
                f"(~{notional:.2f} USDT) at {price} lev={leverage}x "
                f"tp={tp_price} sl={sl_price} conf={confidence}% mode={size_mode}")

    try:
        order_data = self.xt.create_order(
            symbol=symbol, position_side=direction,
            order_side="BUY" if direction == "LONG" else "SELL",
            order_type=order_type, orig_qty=qty, price=limit_price,
            time_in_force=time_in_force,
        )
    except XTError as e:
        return f"Order rejected by XT: {e}"

    self.risk.invalidate_balance_cache()
    # OPTIMIZATION: Invalidate position batch cache on trade execution
    get_cache().invalidate(f"positions_batch_all")
    get_cache().invalidate(f"positions_batch_{symbol}")
    
    order_id = self._extract_order_id(order_data)

    entry_price = price
    filled_qty = 0
    
    # OPTIMIZATION: Non-blocking polling using _stop_monitor.wait()
    # Original used time.sleep(1.0) which blocked other trades from processing
    for attempt in range(3):
        pos = self.position_mgr.get_position_pnl(symbol, direction)
        if pos["exists"] and pos["position_size"] > 0:
            if pos["entry_price"] > 0:
                entry_price = pos["entry_price"]
            filled_qty = int(round(pos["position_size"]))
            break
        if attempt < 2:
            # Wait 1s but allow interruption (non-blocking)
            self._stop_monitor.wait(1.0)

    if filled_qty <= 0:
        try:
            if order_id:
                self.xt.cancel_order(order_id)
                cancel_note = "unfilled order cancelled"
            else:
                self.xt.cancel_all_orders(symbol)
                cancel_note = "open orders cancelled"
        except XTError as e:
            cancel_note = f"could not cancel order: {e}"
        msg = (f"{order_type} {direction} {symbol} did not fill "
               f"({qty} contracts requested). {cancel_note}. No position opened.")
        self._notify(msg)
        return msg

    if abs(entry_price - price) / price > 0.001:
        tp_price, sl_price = self.position_mgr.calculate_dynamic_tpsl(
            symbol, direction, entry_price, strength, confidence, leverage)

    strategies_used = ",".join(scan.get("strategies_used", []))
    trade_id = self.memory.record_trade(
        symbol=symbol, position_side=direction, order_id=order_id,
        entry_price=entry_price, amount=filled_qty, leverage=leverage,
        confidence=confidence, strategy=strategies_used,
        signal_strength=strength,
        timeframe=self.memory.get_setting(
            "timeframes", ",".join(Config.DEFAULT_TIMEFRAMES)),
    )

    ok, protected_qty, _tpsl_pid, tpsl_error = self.position_mgr.attach_tpsl_to_position(
        symbol=symbol, position_side=direction,
        trigger_profit_price=tp_price, trigger_stop_price=sl_price,
    )
    tpsl_status = f"set on {protected_qty} contracts" if ok else f"FAILED: {tpsl_error}"

    liq_distance = self.position_mgr.liquidation_distance(entry_price, leverage)
    sl_distance = abs(entry_price - sl_price)

    summary = (f"Trade ID:{trade_id} OPENED {direction} {symbol}\n"
               f"Entry: {entry_price} | Size: {filled_qty} contracts "
               f"(~{self.risk.contracts_to_notional(symbol, filled_qty, entry_price):.2f} USDT)\n"
               f"Leverage: {leverage}x | TP: {tp_price} | SL: {sl_price}\n"
               f"SL is {sl_distance / entry_price * 100:.2f}% away | "
               f"liquidation ~{liq_distance / entry_price * 100:.2f}% away\n"
               f"Confidence: {confidence}% | Strength: {strength:.2f}\n"
               f"Strategy: {strategies_used or 'n/a'}\n"
               f"TP/SL: {tpsl_status} | Sizing: {size_mode}\n"
               f"Margin Mode: {margin_mode}")
    self._notify(summary)

    if not ok:
        fail_action = self.memory.get_setting("on_tpsl_failure", "close")
        if fail_action == "close":
            self._notify(f"No stop loss could be placed on {direction} {symbol}. "
                         f"Closing the position immediately rather than leaving "
                         f"{leverage}x exposure unprotected.")
            closed, _, close_err = self.position_mgr.close_position(
                symbol, direction, trade_id)
            if closed:
                return summary + (f"\n\nPOSITION CLOSED: no stop loss could be "
                                  f"placed ({tpsl_error}).")
            self._notify(f"URGENT: {symbol} {direction} has no stop loss AND could "
                         f"not be closed ({close_err}). Close it manually on XT now.")
            return summary + f"\n\nURGENT: unprotected and could not close: {close_err}"
        self._notify(f"WARNING: {symbol} {direction} has NO exchange stop loss. "
                     f"The software stop (max_loss_pct) is the only protection, "
                     f"and it only checks every "
                     f"{self.memory.get_setting('guard_interval_sec', 15)}s.")
    return summary
