import hashlib
import hmac
import logging
import os
import time
import requests
from requests.exceptions import RequestException

logger = logging.getLogger("xt_client")


class XTError(Exception):
    pass


class XTClient:
    """XT USDT-M futures REST client.

    Signing rules (validated against the live API):
    - Header keys carry a prefix. The CURRENT official docs (doc.xt.com) and
      this bot's earlier live tests use "validate-*" (validate-appkey /
      validate-timestamp / validate-signature). CCXT still sends the older
      "xt-validate-*". The live server accepts the current documented prefix;
      if a request ever fails with "invalid signature" and nothing else
      changed, set the env var XT_SIGN_PREFIX=xt-validate to switch.
    - Signed string = "<prefix>-appkey=..&<prefix>-timestamp=.." + "#path"
      [+ "#sorted_params"] where sorted_params is exactly the payload the
      server reconstructs: for GET the query string, for POST the
      form-urlencoded BODY. Sending POST params in the query string while the
      server hashes the (empty) body is the classic cause of
      "invalid signature" - so POST params go in the body (data=), GET params
      in the query string.

    CROSS (CROSSED) margin notes:
    - XT position type value is "CROSSED" (NOT "CROSS"); "ISOLATED" is the
      other value. set_position_type() normalizes "CROSS" for you.
    - adjust_margin() and set_auto_margin() are ISOLATED-position features
      only. In CROSSED mode do NOT call them - they error or do nothing.
    - In CROSSED mode the only fix for an "insufficient margin/balance" error
      is to size the order down from availableBalance (get_balances()).
    """

    MARKET = "/future/market"
    USER = "/future/user"
    TRADE = "/future/trade"

    # The XT create-profit endpoint REQUIRES expireTime (ms). A far-future
    # value makes the TP/SL effectively permanent. Override per-call if needed.
    TP_SL_DEFAULT_EXPIRY_MS = 4102444800000  # 2100-01-01 00:00 UTC

    # "validate" (current docs) or "xt-validate" (older CCXT contract).
    SIGN_PREFIX = os.getenv("XT_SIGN_PREFIX", "validate").strip("-").lower()

    def __init__(self, host: str, access_key: str, secret_key: str, timeout: int = 10):
        self.host = host.rstrip("/")
        self._ak = access_key
        self._sk = secret_key
        self.timeout = timeout
        self._session = requests.Session()

    # ---------- signing ----------

    def _sign_headers(self, path: str, params: dict = None):
        p = self.SIGN_PREFIX
        ts = str(int(time.time() * 1000))
        msg = f"{p}-appkey={self._ak}&{p}-timestamp={ts}"
        if params:
            param_str = "&".join(f"{k}={params[k]}" for k in sorted(params))
            msg += f"#{path}#{param_str}"
        else:
            msg += f"#{path}"
        sig = hmac.new(self._sk.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {
            "Content-type": "application/x-www-form-urlencoded",
            f"{p}-appkey": self._ak,
            f"{p}-timestamp": ts,
            f"{p}-signature": sig,
            f"{p}-algorithms": "HmacSHA256",
            # Docs recommend <= 5000ms; the default is 5000.
            f"{p}-recvwindow": "5000",
        }

    # ---------- transport ----------

    def _request(self, method: str, path: str, params: dict = None, signed: bool = False):
        url = self.host + path
        params = {k: v for k, v in (params or {}).items() if v is not None}

        # Order-placing endpoints are NOT idempotent: if the first attempt
        # actually reached the exchange, blind-retrying after a timeout/5xx
        # can place a duplicate order. Never auto-retry these.
        no_retry = method == "POST" and (
            path.endswith("/order/create")
            or path.endswith("/order/create-batch")
            or path.endswith("/entrust/create-profit")
            or path.endswith("/entrust/create-plan")
            or path.endswith("/entrust/create-track")
        )

        max_retries = 1 if no_retry else 5
        for attempt in range(max_retries):
            try:
                if signed:
                    headers = self._sign_headers(path, params)
                else:
                    headers = {"Content-type": "application/json"}

                if method == "GET":
                    # GET: params go in the query string (signed the same way).
                    resp = self._session.get(url, params=params, headers=headers,
                                             timeout=self.timeout)
                else:
                    # POST: params MUST go in the form-encoded BODY. The signed
                    # param string above is identical to this body, so the
                    # server-side hash matches.
                    resp = self._session.post(url, data=params, headers=headers,
                                              timeout=self.timeout)

                if resp.status_code == 429:
                    if no_retry:
                        raise XTError(f"{path} -> HTTP 429 rate limited (not retried, "
                                      f"order endpoint): {resp.text[:200]}")
                    retry_after = int(resp.headers.get("Retry-After", 2 ** attempt))
                    logger.warning(f"XT Rate Limit hit (429). Sleeping for {retry_after}s...")
                    time.sleep(retry_after)
                    continue

                if 500 <= resp.status_code < 600:
                    if no_retry:
                        raise XTError(f"{path} -> HTTP {resp.status_code} (not retried, "
                                      f"order endpoint): {resp.text[:200]}")
                    wait_time = 2 ** attempt
                    logger.warning(f"XT Server Error ({resp.status_code}). Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue

                return self._unwrap(resp, path)

            except RequestException as e:
                if no_retry:
                    raise XTError(f"Network error on {path} (not retried, order endpoint): {e}")
                wait_time = 2 ** attempt
                logger.warning(f"Network error: {e}. Retrying in {wait_time}s...")
                if attempt == max_retries - 1:
                    raise XTError(f"Network error after {max_retries} attempts: {e}")
                time.sleep(wait_time)

        raise XTError(f"Request failed after {max_retries} attempts: {path}")

    def _public(self, method: str, path: str, params: dict = None):
        return self._request(method, path, params, signed=False)

    def _private(self, method: str, path: str, params: dict = None):
        return self._request(method, path, params, signed=True)

    @staticmethod
    def _unwrap(resp, path: str):
        try:
            payload = resp.json()
        except ValueError:
            raise XTError(f"{path} -> HTTP {resp.status_code}, non-JSON body: {resp.text[:200]}")

        if not isinstance(payload, dict):
            return payload

        code = payload.get("returnCode", payload.get("rc", 0))

        if code != 0:
            err = payload.get("error") or {}
            msg = payload.get("msgInfo") or payload.get("mc") or "Unknown error"
            detail = err.get("msg") or err.get("code") or ""
            raise XTError(f"{path} -> returnCode={code} {msg} {detail}".strip())

        if "result" in payload:
            return payload["result"]
        if "data" in payload:
            return payload["data"]
        return payload

    # ---------- public market data ----------

    def get_symbol_detail(self, symbol: str) -> dict:
        return self._public("GET", f"{self.MARKET}/v1/public/symbol/detail", {"symbol": symbol})

    def get_klines(self, symbol: str, interval: str, limit: int = None,
                   start_time: int = None, end_time: int = None) -> list:
        return self._public("GET", f"{self.MARKET}/v1/public/q/kline", {
            "symbol": symbol, "interval": interval, "limit": limit,
            "startTime": start_time, "endTime": end_time,
        }) or []

    def get_agg_ticker(self, symbol: str) -> dict:
        return self._public("GET", f"{self.MARKET}/v1/public/q/agg-ticker", {"symbol": symbol}) or {}

    def get_mark_price(self, symbol: str) -> dict:
        return self._public("GET", f"{self.MARKET}/v1/public/q/symbol-mark-price",
                            {"symbol": symbol}) or {}

    def get_leverage_brackets(self, symbol: str) -> list:
        data = self._public("GET", f"{self.MARKET}/v1/public/leverage/bracket/detail",
                            {"symbol": symbol}) or {}
        return data.get("leverageBrackets") or []

    def get_funding_rate(self, symbol: str) -> dict:
        return self._public("GET", f"{self.MARKET}/v1/public/q/funding-rate",
                            {"symbol": symbol}) or {}

    # ---------- account ----------

    def get_balances(self) -> list:
        # In CROSSED mode size orders from "availableBalance" of the quote coin.
        data = self._private("GET", f"{self.USER}/v1/balance/list")
        return data if isinstance(data, list) else []

    def get_listen_key(self) -> str:
        # Valid for 8 hours (server-side) - re-request before expiry if you use
        # the user-data WebSocket stream.
        data = self._private("GET", f"{self.USER}/v1/user/listen-key")
        if isinstance(data, dict):
            return data.get("listenKey") or data.get("accessToken") or ""
        return data or ""

    # ---------- positions ----------

    def get_positions(self, symbol: str = None) -> list:
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._private("GET", f"{self.USER}/v1/position/list", params)
        return data if isinstance(data, list) else []

    def set_leverage(self, symbol: str, position_side: str, leverage: int):
        return self._private("POST", f"{self.USER}/v1/position/adjust-leverage", {
            "symbol": symbol, "positionSide": position_side, "leverage": leverage,
        })

    def set_position_type(self, symbol: str, position_side: str, position_type: str):
        # XT API values are "CROSSED" and "ISOLATED" (NOT "CROSS"). "CROSS" is
        # accepted and normalized so old callers keep working.
        pt = str(position_type).upper()
        if pt == "CROSS":
            pt = "CROSSED"
        if pt not in ("CROSSED", "ISOLATED"):
            raise XTError(f"set_position_type: invalid positionType {position_type!r} "
                          f"(expected CROSSED or ISOLATED)")
        return self._private("POST", f"{self.USER}/v1/position/change-type", {
            "symbol": symbol, "positionSide": position_side, "positionType": pt,
        })

    def adjust_margin(self, symbol: str, position_side: str, margin, direction: str):
        # ISOLATED-only feature. In CROSSED mode you cannot add/reduce margin on
        # a single position - do NOT call this when trading CROSSED.
        direction = str(direction).upper()
        if direction not in ("ADD", "SUB"):
            raise XTError(f"adjust_margin: direction must be ADD or SUB, got {direction!r}")
        return self._private("POST", f"{self.USER}/v1/position/margin", {
            "symbol": symbol, "positionSide": position_side,
            "margin": margin, "type": direction,
        })

    def set_auto_margin(self, symbol: str, position_side: str, enabled: bool):
        # ISOLATED-only feature. "Auto margin" does NOT exist in CROSSED mode
        # (the whole available balance already backs the position) - do NOT
        # call this when trading CROSSED; it errors or is a no-op.
        return self._private("POST", f"{self.USER}/v1/position/auto-margin", {
            "symbol": symbol, "positionSide": position_side, "autoMargin": bool(enabled),
        })

    def get_leverage_info(self, symbol: str) -> list:
        data = self._private("GET", f"{self.TRADE}/v1/position/leverage/list", {"symbol": symbol})
        if isinstance(data, dict):
            return data.get("items") or []
        return data if isinstance(data, list) else []

    # ---------- orders ----------

    def create_order(self, symbol: str, position_side: str, order_side: str,
                     order_type: str, orig_qty: int, price=None,
                     time_in_force: str = None, client_order_id: str = None,
                     reduce_only: bool = None):
        params = {
            "symbol": symbol, "positionSide": position_side, "orderSide": order_side,
            "orderType": order_type, "origQty": int(orig_qty),
        }
        if price is not None:
            params["price"] = price
        if time_in_force is not None:
            params["timeInForce"] = time_in_force
        if client_order_id is not None:
            params["clientOrderId"] = client_order_id
        # In CROSSED + low margin, close/reduce with reduce_only=True so the
        # order can never open a new position.
        if reduce_only is not None:
            params["reduceOnly"] = bool(reduce_only)
        return self._private("POST", f"{self.TRADE}/v1/order/create", params)

    def cancel_order(self, order_id):
        return self._private("POST", f"{self.TRADE}/v1/order/cancel", {"orderId": order_id})

    def cancel_all_orders(self, symbol: str):
        return self._private("POST", f"{self.TRADE}/v1/order/cancel-all", {"symbol": symbol})

    def get_order(self, order_id):
        return self._private("GET", f"{self.TRADE}/v1/order/detail", {"orderId": order_id})

    def get_orders(self, symbol: str = None, state: str = "NEW", page: int = 1, size: int = 50):
        params = {"state": state, "page": page, "size": size}
        if symbol:
            params["symbol"] = symbol
        data = self._private("GET", f"{self.TRADE}/v1/order-entrust/list", params) or {}
        return data.get("items") or []

    # ---------- take profit / stop loss ----------

    def create_tpsl(self, symbol: str, position_side: str, orig_qty: int,
                    trigger_profit_price, trigger_stop_price, expire_time_ms: int = None,
                    profit_order_type: str = "MARKET", stop_order_type: str = "MARKET",
                    profit_tif: str = "IOC", stop_tif: str = "IOC",
                    profit_price=None, stop_price=None):
        # expireTime is REQUIRED by the XT API for this endpoint. When omitted
        # it defaults to year 2100 -> effectively NO time limit.
        if expire_time_ms is None:
            expire_time_ms = self.TP_SL_DEFAULT_EXPIRY_MS
        params = {
            "symbol": symbol,
            "positionSide": position_side,
            "origQty": int(orig_qty),
            "triggerProfitPrice": trigger_profit_price,
            "triggerStopPrice": trigger_stop_price,
            "expireTime": int(expire_time_ms),
            "profitDelegateOrderType": profit_order_type,
            "profitDelegateTimeInForce": profit_tif,
            "stopDelegateOrderType": stop_order_type,
            "stopDelegateTimeInForce": stop_tif,
        }
        if profit_price is not None:
            params["profitDelegatePrice"] = profit_price
        if stop_price is not None:
            params["stopDelegatePrice"] = stop_price
        return self._private("POST", f"{self.TRADE}/v1/entrust/create-profit", params)

    def update_tpsl(self, profit_id, trigger_profit_price=None, trigger_stop_price=None):
        params = {"profitId": profit_id}
        if trigger_profit_price is not None:
            params["triggerProfitPrice"] = trigger_profit_price
        if trigger_stop_price is not None:
            params["triggerStopPrice"] = trigger_stop_price
        return self._private("POST", f"{self.TRADE}/v1/entrust/update-profit-stop", params)

    def cancel_tpsl(self, profit_id):
        return self._private("POST", f"{self.TRADE}/v1/entrust/cancel-profit-stop",
                             {"profitId": profit_id})

    def cancel_all_tpsl(self, symbol: str):
        return self._private("POST", f"{self.TRADE}/v1/entrust/cancel-all-profit-stop",
                             {"symbol": symbol})

    def get_tpsl_orders(self, symbol: str, state: str = "NOT_TRIGGERED",
                        page: int = 1, size: int = 50) -> list:
        params = {"state": state, "page": page, "size": size}
        if symbol:
            params["symbol"] = symbol
        data = self._private("GET", f"{self.TRADE}/v1/entrust/profit-list", params) or {}
        return data.get("items") or []
