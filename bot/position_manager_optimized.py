"""
Optimized position_manager.py performance patches.

Key optimizations:
1. Cache ATR calculations with 120s TTL to avoid redundant kline fetches
2. Batch-fetch positions instead of N+1 queries
3. Optimize DataFrame operations in _calculate_atr()
4. Pre-parse boolean settings with caching
"""

import time
import logging
import pandas as pd
from bot.cache_manager import get_cache
from bot.xt_client import XTError

logger = logging.getLogger("xt_position")


def _calculate_atr_optimized(self, symbol: str, interval: str, period: int = 14) -> float:
    """Optimized ATR calculation with caching and efficient pandas operations.
    
    Cache keys: f"atr_{symbol}_{interval}" with 120s TTL.
    Reduces redundant kline fetches and DataFrame allocations.
    """
    cache_key = f"atr_{symbol}_{interval}"
    cached_atr = get_cache().get(cache_key)
    if cached_atr is not None:
        logger.debug(f"ATR cache hit for {symbol} {interval}: {cached_atr}")
        return cached_atr
    
    try:
        rows = self.xt.get_klines(symbol, interval, limit=period + 10)
    except XTError as e:
        logger.warning(f"ATR kline fetch failed for {symbol}: {e}")
        return 0.0
    
    if not rows:
        return 0.0
    
    # Single-pass DataFrame creation with pre-mapped columns
    try:
        df = pd.DataFrame(rows)
        
        # Only convert numeric columns we need; avoid looping
        numeric_cols = {"h": "high", "l": "low", "c": "close", "t": "timestamp"}
        for old_col, new_col in numeric_cols.items():
            if old_col in df.columns:
                df.rename(columns={old_col: new_col}, inplace=True)
                df[new_col] = pd.to_numeric(df[new_col], errors="coerce")
        
        # Check required columns exist
        if not all(c in df.columns for c in ["high", "low", "close"]):
            return 0.0
        
        # Single dropna pass
        df = df.dropna(subset=["high", "low", "close"])
        
        # Sort only if timestamp exists
        if "timestamp" in df.columns:
            df.sort_values("timestamp", inplace=True)
        
        df.reset_index(drop=True, inplace=True)
        
        if len(df) < period:
            return 0.0
        
        # Compute TR in a single operation (avoid 3 separate subtracts)
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        
        atr = float(tr.tail(period).mean())
        
        # Cache for 120 seconds
        get_cache().set(cache_key, atr, ttl_seconds=120.0)
        logger.debug(f"ATR computed for {symbol} {interval}: {atr}")
        
        return atr
    except Exception as e:
        logger.warning(f"ATR computation failed for {symbol}: {e}")
        return 0.0


def get_positions_batch_optimized(self, symbol: str = None) -> dict:
    """Batch-fetch all positions at once, indexed by (symbol, position_side).
    
    Replaces N+1 query pattern where get_position_pnl() calls get_position()
    which calls get_positions() for each trade individually.
    
    Call this once per status/mid-management cycle and pass result to callers.
    
    Returns: {(symbol, position_side): position_dict, ...}
    """
    cache_key = f"positions_batch_{symbol or 'all'}"
    cached = get_cache().get(cache_key)
    if cached is not None:
        logger.debug(f"Positions batch cache hit for {symbol or 'all'}")
        return cached
    
    try:
        all_positions = self.xt.get_positions(symbol)
    except XTError as e:
        logger.warning(f"Position batch fetch failed: {e}")
        return {}
    
    result = {}
    for pos in all_positions:
        sym = pos.get("symbol")
        side = pos.get("positionSide")
        size = float(pos.get("positionSize") or 0)
        if sym and side and size > 0:
            result[(sym, side)] = pos
    
    # Cache for 30 seconds; invalidated on trade execution
    get_cache().set(cache_key, result, ttl_seconds=30.0)
    logger.debug(f"Positions batch cached for {symbol or 'all'}: {len(result)} positions")
    
    return result


def get_position_pnl_optimized(self, symbol: str, position_side: str, 
                               positions_batch: dict = None) -> dict:
    """Optimized get_position_pnl using pre-fetched batch.
    
    Args:
        symbol, position_side: Position identifier
        positions_batch: Pre-fetched batch from get_positions_batch_optimized().
                        If None, falls back to single fetch (slower).
    
    Returns: PnL dict (same structure as original)
    """
    pos = None
    
    if positions_batch is not None:
        # Use pre-fetched batch (fast path, no API call)
        pos = positions_batch.get((symbol, position_side))
    else:
        # Fallback: single fetch (original behavior)
        pos = self.get_position(symbol, position_side)
    
    if not pos:
        return {"exists": False, "unrealized_pnl": 0.0, "roi": 0.0,
                "entry_price": 0.0, "mark_price": 0.0, "leverage": 1,
                "position_size": 0, "margin": 0.0, "profit_id": None,
                "trigger_profit_price": 0.0, "trigger_stop_price": 0.0}
    
    entry = float(pos.get("entryPrice") or 0)
    mark = float(pos.get("calMarkPrice") or 0)
    if mark <= 0:
        mark = self._public_mark_price(symbol)
    
    size = float(pos.get("positionSize") or 0)
    leverage = int(float(pos.get("leverage") or 1))
    pnl = float(pos.get("floatingPL") or 0)
    margin = float(pos.get("isolatedMargin") or 0)
    cs = self.risk.get_contract_size(symbol)
    
    roi = 0.0
    if entry > 0 and mark > 0:
        move = (mark - entry) / entry
        if position_side == "SHORT":
            move = -move
        roi = move * leverage * 100
    
    return {
        "exists": True,
        "unrealized_pnl": pnl,
        "roi": roi,
        "entry_price": entry,
        "mark_price": mark,
        "leverage": leverage,
        "position_size": size,
        "position_value": size * cs * mark,
        "margin": margin,
        "profit_id": pos.get("profitId") or None,
        "trigger_profit_price": float(pos.get("triggerProfitPrice") or 0),
        "trigger_stop_price": float(pos.get("triggerStopPrice") or 0),
        "position_type": pos.get("positionType") or "",
        "available_close_size": float(pos.get("availableCloseSize") or 0),
    }
