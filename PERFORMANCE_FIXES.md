# Performance Optimization Guide

This document outlines key performance issues identified in CryptoMind-XT-1 and their fixes.

## Issues Fixed

### 1. **N+1 Positions Query Pattern** ⚠️ HIGH PRIORITY
**Location**: `bot/trader.py` (lines 81-95), `bot/position_manager.py` (multiple)

**Problem**:
```python
# get_status_report() iterates trades and calls get_position_pnl() separately
for t in open_trades:
    pos = self.position_mgr.get_position_pnl(t["symbol"], t["position_side"])  # Individual API call per trade
```

If 10 positions are open → 10+ API calls per status check.

**Solution**:
- Use `get_positions_batch_optimized()` to fetch ALL positions once
- Pass the batch dict to `get_position_pnl_optimized(positions_batch=...)`
- Reduces 10 API calls → 1 API call

**Implementation**:
```python
# In get_status_report():
positions_batch = self.position_mgr.get_positions_batch_optimized()
for t in open_trades:
    pos = self.position_mgr.get_position_pnl_optimized(
        t["symbol"], t["position_side"], 
        positions_batch=positions_batch  # Reuse batch
    )
```

---

### 2. **Redundant adopt_exchange_positions() Calls** ⚠️ HIGH PRIORITY
**Location**: `bot/trader.py` (lines 615-664), line 629

**Problem**:
```python
def _auto_trade_loop(self):
    while not self._stop_monitor.is_set():
        # Called on EVERY iteration (~15s default guard_interval)
        for event in self.position_mgr.adopt_exchange_positions():  # Full fetch every cycle
```

Fetches all positions every 15 seconds, even if none have changed.

**Solution**:
- Cache adoption results with 5-minute TTL
- Only re-check on explicit `/sync` command or daily timer

**Implementation**:
```python
# Add adoption interval (default 300s = 5 minutes)
adoption_interval = 300  # Move to config
last_adoption = 0.0

while not self._stop_monitor.is_set():
    now = time.time()
    if now - last_adoption >= adoption_interval:
        last_adoption = now
        for event in self.position_mgr.adopt_exchange_positions():
            # ... handle adopted positions
```

---

### 3. **ATR Recalculation on Every TP/SL** ⚠️ HIGH PRIORITY
**Location**: `bot/position_manager.py` (lines 496-552)

**Problem**:
```python
def calculate_dynamic_tpsl(self, ...):
    atr = self._calculate_atr(symbol, "5m")  # Fetches klines + computes pandas DF every time
    # Called on: entry, fill drift check, reversal, mid-management
```

For one trade with TP/SL: entry (1 ATR) + fill check (1 ATR) + mid-management (1 ATR) = 3+ fetches.

**Solution**:
- Cache ATR values per symbol with 120-second TTL
- Use `bot/cache_manager.py` (new file)

**Implementation**:
```python
from bot.cache_manager import get_cache

def _calculate_atr(self, symbol: str, interval: str, period: int = 14) -> float:
    cache_key = f"atr_{symbol}_{interval}"
    cached = get_cache().get(cache_key)
    if cached is not None:
        return cached
    
    # ... fetch klines and compute ...
    
    get_cache().set(cache_key, atr, ttl_seconds=120.0)
    return atr
```

**Files Added**:
- `bot/cache_manager.py` – CacheManager class with TTL support

---

### 4. **Blocking Sleeps in Critical Loop** ⚠️ MEDIUM PRIORITY
**Location**: `bot/trader.py` (lines 373-380, 138)

**Problem**:
```python
# Order confirmation: sleeps 1s × 3 retries = 3s blocking
for _ in range(3):
    pos = self.position_mgr.get_position_pnl(symbol, direction)
    if pos["exists"]:
        break
    time.sleep(1.0)  # Blocks auto-trade loop
```

During fill wait, the loop cannot check other positions or receive shutdown signals.

**Solution**:
- Use `threading.Event.wait(timeout)` or exponential backoff
- Non-blocking checks

**Implementation**:
```python
# Use the existing _stop_monitor.wait() pattern
filled_qty = 0
for attempt in range(3):
    pos = self.position_mgr.get_position_pnl(symbol, direction)
    if pos["exists"] and pos["position_size"] > 0:
        filled_qty = int(round(pos["position_size"]))
        break
    if attempt < 2:
        self._stop_monitor.wait(1.0)  # Non-blocking, can respond to stop signal
```

---

### 5. **Inefficient DataFrame Operations in ATR** ⚠️ MEDIUM PRIORITY
**Location**: `bot/position_manager.py` (lines 533-552)

**Problem**:
```python
df = pd.DataFrame(rows).rename(columns={...})  # Step 1
for col in [...]:
    df[col] = pd.to_numeric(...)  # Step 2: Loop
df.dropna(...)  # Step 3
df.sort_values(...)  # Step 4
df.reset_index(...)  # Step 5
# Creates 5 intermediate copies of data
```

**Solution**:
- Single-pass column mapping
- Use inplace operations
- Pre-allocate where possible

**Implementation** (in `position_manager_optimized.py`):
```python
df = pd.DataFrame(rows)
numeric_cols = {"h": "high", "l": "low", "c": "close", "t": "timestamp"}
for old_col, new_col in numeric_cols.items():
    if old_col in df.columns:
        df.rename(columns={old_col: new_col}, inplace=True)
        df[new_col] = pd.to_numeric(df[new_col], errors="coerce")

df = df.dropna(subset=["high", "low", "close"])
if "timestamp" in df.columns:
    df.sort_values("timestamp", inplace=True)
df.reset_index(drop=True, inplace=True)
```

---

### 6. **String Parsing in Hot Loop** ⚠️ LOW PRIORITY
**Location**: `bot/trader.py` (lines 197-200)

**Problem**:
```python
def _reversal_check(self, result: dict):
    # Called every scan cycle (default 60s)
    enabled = str(self.memory.get_setting("reversal_enabled", ...)).lower()
    if enabled not in ("1", "true", "yes", "on"):  # String parsing every time
```

**Solution**:
- Cache the parsed boolean with TTL

**Implementation**:
```python
def _reversal_check(self, result: dict):
    cache_key = "setting_reversal_enabled"
    enabled = get_cache().get(cache_key)
    if enabled is None:
        raw = str(self.memory.get_setting("reversal_enabled", Config.REVERSAL_ENABLED)).lower()
        enabled = raw in ("1", "true", "yes", "on")
        get_cache().set(cache_key, enabled, ttl_seconds=300.0)  # 5min TTL
    
    if not enabled:
        return
    # ...
```

---

## Implementation Roadmap

### Phase 1 (Highest ROI) – Week 1
1. ✅ Create `bot/cache_manager.py`
2. Add `get_positions_batch_optimized()` to `bot/position_manager.py`
3. Update `get_status_report()` in `bot/trader.py` to use batch positions
4. Update `mid_manage_positions()` to use batch positions

### Phase 2 – Week 2
5. Integrate ATR caching into `_calculate_atr()`
6. Add adoption interval caching to `_auto_trade_loop()`
7. Replace blocking sleeps in order confirmation

### Phase 3 (Polish) – Week 3
8. Optimize DataFrame operations (replace loop with inplace)
9. Cache parsed boolean settings
10. Add monitoring/metrics for cache hit rates

---

## Testing Checklist

- [ ] Load test with 10+ open positions; verify `get_status_report()` uses 1 API call instead of 10+
- [ ] Monitor adoption calls; verify they drop from every 15s to every 300s
- [ ] Check ATR cache hits; expect >70% hit rate in normal trading
- [ ] Verify order fills complete without blocking other trades
- [ ] No regression in PnL accuracy or signal detection
- [ ] Memory usage stable over 24h run

---

## Monitoring

Add logging to track cache effectiveness:
```python
logger.info(f"ATR cache hit for {symbol} {interval}: {cached_atr}")
logger.debug(f"Positions batch cached: {len(result)} positions")
```

Query logs for cache hit patterns to validate optimization impact.

---

## Files Modified/Added

- **Added**: `bot/cache_manager.py` – Cache manager with TTL
- **Added**: `bot/position_manager_optimized.py` – Reference implementations
- **Modify**: `bot/position_manager.py` – Integrate optimizations
- **Modify**: `bot/trader.py` – Use batch positions, adoption caching, non-blocking sleeps
