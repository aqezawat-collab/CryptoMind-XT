import hashlib
import hmac
import logging
import time
import requests
from requests.exceptions import RequestException

logger = logging.getLogger("xt_client")

class XTError(Exception):
    pass

class XTClient:
    """XT USDT-M futures REST client — signing and endpoints aligned with
    the working AItradekit MCP server (xt_tradekit/xt_futures.py).
    """

    MARKET = "/future/market"
    USER = "/future/user"
    TRADE = "/future/trade"

    def __init__(self, host: str, access_key: str, secret_key: str, timeout: int = 10):
        self.host = host.rstrip("/")
        self._ak = access_key
        self._sk = secret_key
        self.timeout = timeout
        self._session = requests.Session()

    # ---------- signing (mirrors AItradekit MCP exactly) ----------

    def _sign_headers(self, path: str, params: dict = None):
        # Header/message prefix is "xt-validate-*", matching the working XT
        # reference client (the xt-exchange plugin + AItradekit's xt_futures.py),
        # which sign with these exact keys:
        #   msg = "xt-validate-appkey={ak}&xt-validate-timestamp={ts}#{path}[#{sorted_params}]"
        #   headers: xt-validate-appkey / xt-validate-timestamp /
        #            xt-validate-signature / xt-validate-algorithms /
        #            xt-validate-recvwindow
        # Omitting the "xt-" prefix makes the server unable to match the key
        # and every signed call is rejected, so the prefix must be kept.
        ts = str(int(time.time() * 1000))
        msg = f"xt-validate-appkey={self._ak}&xt-validate-timestamp={ts}"
        if params:
            param_str = "&".join(f"{k}={params[k]}" for k in sorted(params))
            msg += f"#{path}#{param_str}"
        else:
            msg += f"#{path}"
        sig = hmac.new(self._sk.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {
            "Content-type": "application/x-www-form-urlencoded",
            "xt-validate-appkey": self._ak,
            "xt-validate-timestamp": ts,
            "xt-validate-signature": sig,
            "xt-validate-algorithms": "HmacSHA256",
            "xt-validate-recvwindow": "60000",
        }

    # ---------- transport ----------

    def _request(self, method: str, path: str, params: dict = None, signed: bool = False):
        url = self.host + path
        params = {k: v for k, v in (params or {}).items() if v is not None}

        max_retries = 5
        for attempt in range(max_retries):
            try:
                if signed:
                    headers = self._sign_headers(path, params)
                else:
                    headers = {"Content-type": "application/json"}

                if method == "GET":
                    resp = self._session.get(url, params=params, headers=headers, timeout=self.timeout)
                else:
                    resp = self._session.post(url, params=params, headers=headers, timeout=self.timeout)

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 2 ** attempt))
                    logger.warning(f"XT Rate Limit hit (429). Sleeping for {retry_after}s...")
                    time.sleep(retry_after)
                    continue

                if 500 <= resp.status_code < 600:
                    wait_time = 2 ** attempt
                    logger.warning(f"XT Server Error ({resp.status_code}). Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue

                return self._unwrap(resp, path)

            except RequestException as e:
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
        data = self._private("GET", f"{self.USER}/v1/balance/list")
        return data if isinstance(data, list) else []

    def get_listen_key(self) -> str:
        data = self._private("GET", f"{self.USER}/v1/user/listen-key")
        if isinstance(data, dict):
            return data.get("listenKey") or data.get("accessToken") or ""
        return data or ""

    # ---------- positions ----------

    def get_positions(self, symbol: str = None) -> list:
        """Uses /future/user/v1/position ("Get active position information"),
        NOT /future/user/v1/position/list ("Get Position Information").

        Confirmed against the official xt-api docs: the /list endpoint's
        response only has autoMargin, availableCloseSize, closeOrderSize,
        entryPrice, isolatedMargin, leverage, openOrderMarginFrozen,
        positionSide, positionSize, positionType, realizedProfit, symbol —
        it has NO calMarkPrice, floatingPL, profitId, triggerProfitPrice, or
        triggerStopPrice. Every one of those missing fields is exactly what
        get_position_pnl(), ensure_tpsl(), check_tpsl_breakeven(), and
        trail_stop_loss() read, which is why mark price / PnL / profitId
        looked intermittently empty rather than a fetch bug: this endpoint
        structurally never returns them. AItradekit's xt_futures.py and
        xt-exchange-plugin's xt_futures.py both call /position/list too, so
        this was inherited from them, not unique to this bot.
        """
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._private("GET", f"{self.USER}/v1/position", params)
        return data if isinstance(data, list) else []

    def set_leverage(self, symbol: str, position_side: str, leverage: int):
        return self._private("POST", f"{self.USER}/v1/position/adjust-leverage", {
            "symbol": symbol, "positionSide": position_side, "leverage": leverage,
        })

    def set_position_type(self, symbol: str, position_side: str, position_type: str):
        return self._private("POST", f"{self.USER}/v1/position/change-type", {
            "symbol": symbol, "positionSide": position_side, "positionType": position_type,
        })

    def adjust_margin(self, symbol: str, position_side: str, margin, direction: str):
        return self._private("POST", f"{self.USER}/v1/position/margin", {
            "symbol": symbol, "positionSide": position_side,
            "margin": margin, "type": direction,
        })

    def set_auto_margin(self, symbol: str, position_side: str, enabled: bool):
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
                     time_in_force: str = None, client_order_id: str = None):
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
                    trigger_profit_price, trigger_stop_price, expire_time_ms: int,
                    profit_order_type: str = "MARKET", stop_order_type: str = "MARKET",
                    profit_tif: str = "IOC", stop_tif: str = "IOC",
                    profit_price=None, stop_price=None):
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
