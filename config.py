import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    XT_API_KEY: str = os.getenv("XT_API_KEY", "")
    XT_API_SECRET: str = os.getenv("XT_API_SECRET", "")

    AI_API_KEY: str = os.getenv("AI_API_KEY", "")
    AI_BASE_URL: str = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
    # When empty, the bot auto-detects a chat model from the provider's
    # OpenAI-compatible /models endpoint (see bot/ai_chat.py).
    AI_MODEL: str = os.getenv("AI_MODEL", "")

    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_USER_ID: str = os.getenv("TELEGRAM_USER_ID", "")

    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///data/memory.db")

    XT_FUTURES_HOST: str = os.getenv("XT_FUTURES_HOST", "https://fapi.xt.com")

    DEFAULT_SYMBOL: str = "btc_usdt"
    DEFAULT_LEVERAGE: int = 40
    DEFAULT_MARGIN_MODE: str = "CROSSED"
    # 1m-15m catch short-term moves; 1h/4h give the higher-timeframe trend
    # context the scanner was previously blind to (a strong 1h/4h uptrend read
    # as "no signal" when only the micro pullback on short TFs was visible).
    DEFAULT_TIMEFRAMES: list = ["1m", "3m", "5m", "15m", "1h", "4h"]
    DEFAULT_MARGIN_AMOUNT_PCT: float = 25.0
    DEFAULT_RISK_PCT: float = 1.0
    SIGNAL_COOLDOWN_MINUTES: int = 5
    MAX_POSITIONS: int = 3
    MIN_CONFIDENCE: int = 80

    TF_MIN_CONFIDENCE: int = 60

    SCAN_INTERVAL_SEC: int = 60
    GUARD_INTERVAL_SEC: int = 15
    MAX_LOSS_PCT: float = 40.0
    MAX_PROFIT_PCT: float = 500.0
    BREAKEVEN_THRESHOLD_PCT: float = 8.0
    TRAILING_STOP_PCT: float = 15.0

    TRAILING_TRIGGER_ROI_PCT: float = 15.0
    TRAILING_DISTANCE_PCT: float = 0.5

    SL_LIQUIDATION_SAFETY: float = 0.5
    ON_TPSL_FAILURE: str = "close"

    # Close an open position when the opposite-direction signal reaches this
    # confidence (reversal logic). Disabled when REVERSAL_ENABLED is False.
    REVERSAL_ENABLED: bool = True
    REVERSAL_CONFIDENCE: int = 70
    # How often (seconds) to push a PnL + confidence report even with no events.
    REPORT_INTERVAL_SEC: int = 60


    @classmethod
    def validate(cls) -> list:
        missing = []
        required = ["XT_API_KEY", "XT_API_SECRET", "AI_API_KEY",
                    "TELEGRAM_BOT_TOKEN", "TELEGRAM_USER_ID"]
        for key in required:
            if not getattr(cls, key):
                missing.append(key)
        if cls.TELEGRAM_USER_ID and not cls.TELEGRAM_USER_ID.strip().isdigit():
            missing.append("TELEGRAM_USER_ID (must be a numeric Telegram user id)")
        if cls.DEFAULT_LEVERAGE < 1 or cls.DEFAULT_LEVERAGE > 125:
            missing.append("DEFAULT_LEVERAGE should be between 1 and 125")
        if cls.DEFAULT_MARGIN_AMOUNT_PCT < 1 or cls.DEFAULT_MARGIN_AMOUNT_PCT > 100:
            missing.append("DEFAULT_MARGIN_AMOUNT_PCT should be between 1 and 100")
        if cls.DEFAULT_RISK_PCT < 0.1 or cls.DEFAULT_RISK_PCT > 10:
            missing.append("DEFAULT_RISK_PCT should be between 0.1 and 10")
        if cls.MIN_CONFIDENCE < 50 or cls.MIN_CONFIDENCE > 100:
            missing.append("MIN_CONFIDENCE should be between 50 and 100")
        if cls.SL_LIQUIDATION_SAFETY <= 0 or cls.SL_LIQUIDATION_SAFETY > 1:
            missing.append("SL_LIQUIDATION_SAFETY should be between 0 and 1")
        if cls.BREAKEVEN_THRESHOLD_PCT <= 0 or cls.BREAKEVEN_THRESHOLD_PCT > 100:
            missing.append("BREAKEVEN_THRESHOLD_PCT should be between 0 and 100")
        if cls.TRAILING_DISTANCE_PCT <= 0 or cls.TRAILING_DISTANCE_PCT > 20:
            missing.append("TRAILING_DISTANCE_PCT should be between 0 and 20")
        return missing

    @classmethod
    def default_settings(cls) -> dict:
        return {
            "symbol": cls.DEFAULT_SYMBOL,
            "leverage": cls.DEFAULT_LEVERAGE,
            "margin_mode": cls.DEFAULT_MARGIN_MODE,
            "timeframes": ",".join(cls.DEFAULT_TIMEFRAMES),
            "margin_amount_pct": cls.DEFAULT_MARGIN_AMOUNT_PCT,
            "margin_risk_pct": cls.DEFAULT_RISK_PCT,
            "min_confidence": cls.MIN_CONFIDENCE,
            "tf_min_confidence": cls.TF_MIN_CONFIDENCE,
            "cooldown_minutes": cls.SIGNAL_COOLDOWN_MINUTES,
            "max_positions": cls.MAX_POSITIONS,
            "position_mode": "margin",
            "scan_interval_sec": cls.SCAN_INTERVAL_SEC,
            "guard_interval_sec": cls.GUARD_INTERVAL_SEC,
            "max_loss_pct": cls.MAX_LOSS_PCT,
            "max_profit_pct": cls.MAX_PROFIT_PCT,
            "breakeven_threshold_pct": cls.BREAKEVEN_THRESHOLD_PCT,
            "trailing_stop_pct": cls.TRAILING_STOP_PCT,
            "trailing_trigger_roi_pct": cls.TRAILING_TRIGGER_ROI_PCT,
            "trailing_distance_pct": cls.TRAILING_DISTANCE_PCT,
            "sl_liquidation_safety": cls.SL_LIQUIDATION_SAFETY,
            "on_tpsl_failure": cls.ON_TPSL_FAILURE,
            "reversal_enabled": cls.REVERSAL_ENABLED,
            "reversal_confidence": cls.REVERSAL_CONFIDENCE,
            "report_interval_sec": cls.REPORT_INTERVAL_SEC,
        }

    @classmethod
    def to_dict(cls) -> dict:
        redacted = {"XT_API_SECRET", "XT_API_KEY", "AI_API_KEY", "TELEGRAM_BOT_TOKEN"}
        return {k: v for k, v in cls.__dict__.items()
                if not k.startswith("_") and k.isupper() and k not in redacted}
