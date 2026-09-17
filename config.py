"""Central configuration for the paper-trading agent.

Every tunable lives here and can be overridden with environment variables
(see .env.example). Safety switches are HARD-CODED: this system can never
enable real-money trading.
"""
import os
import sys

def _bool(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")

class SafetyError(RuntimeError):
    """Raised if anything ever tries to enable real-money trading."""

class Config:
    # ------------------------------------------------------------------ safety
    # These are compile-time constants, NOT configurable. Real trading is
    # permanently disabled. There is no wallet connection, no private keys,
    # no real order routing anywhere in the codebase.
    REAL_TRADING: bool = False
    WALLET_CONNECTION: str = "disabled"
    ORDER_EXECUTION: str = "simulation_only"
    KILL_SWITCH: bool = False  # env KILL_SWITCH=true stops all new trades

    def __init__(self, env=None):
        env = dict(os.environ) if env is None else dict(env)

        if _bool(env.get("REAL_TRADING", "false")):
            # Fail safely: refuse to even start if someone tries real trading.
            raise SafetyError(
                "REAL_TRADING=true is not supported. This project is "
                "PAPER TRADING ONLY and real-money execution is disabled."
            )
        if env.get("PRIVATE_KEY") or env.get("SEED_PHRASE"):
            raise SafetyError(
                "Never provide private keys or seed phrases. This system "
                "never connects to a real wallet."
            )

        # ------------------------------------------------------------- trading
        self.STARTING_BALANCE = float(env.get("STARTING_BALANCE", "100"))
        self.SCAN_INTERVAL_MINUTES = float(env.get("SCAN_INTERVAL_MINUTES", "10"))
        # Minimum edge to enter a trade. Default 0.05; override at runtime
        # with the repository variable MIN_EDGE (Settings -> Secrets and
        # variables -> Actions -> Variables) - the workflow wires it in, so
        # you can tune it from the GitHub mobile app without code changes.
        self.MIN_EDGE = float(env.get("MIN_EDGE", "0.05"))
        self.MIN_CONFIDENCE = float(env.get("MIN_CONFIDENCE", "0.60"))
        self.MAX_SPREAD = float(env.get("MAX_SPREAD", "0.05"))
        self.MIN_LIQUIDITY = float(env.get("MIN_LIQUIDITY", "1000"))
        self.MIN_VOLUME = float(env.get("MIN_VOLUME", "5000"))
        self.MIN_DAYS_TO_RESOLUTION = float(env.get("MIN_DAYS_TO_RESOLUTION", "1"))
        self.MAX_DAYS_TO_RESOLUTION = float(env.get("MAX_DAYS_TO_RESOLUTION", "180"))
        self.MAX_AI_CALLS_PER_CYCLE = int(env.get("MAX_AI_CALLS_PER_CYCLE", "25"))

        # ------------------------------------------------------------- risk
        self.MAX_POSITION_PERCENT = float(env.get("MAX_POSITION_PERCENT", "0.06"))
        self.MAX_OPEN_EXPOSURE_PERCENT = float(env.get("MAX_OPEN_EXPOSURE_PERCENT", "0.25"))
        self.MAX_DAILY_LOSS_PERCENT = float(env.get("MAX_DAILY_LOSS_PERCENT", "0.10"))
        self.MAX_DRAWDOWN_PERCENT = float(env.get("MAX_DRAWDOWN_PERCENT", "0.30"))
        self.MAX_OPEN_POSITIONS = int(env.get("MAX_OPEN_POSITIONS", "15"))
        self.KELLY_FRACTION = float(env.get("KELLY_FRACTION", "0.25"))

        # ------------------------------------------------------- simulation
        self.SIMULATED_FEES = _bool(env.get("SIMULATED_FEES", "true"))
        self.FEE_PERCENT = float(env.get("FEE_PERCENT", "0.002"))
        self.SLIPPAGE_PERCENT = float(env.get("SLIPPAGE_PERCENT", "0.01"))
        self.EXECUTION_DELAY_SECONDS = float(env.get("EXECUTION_DELAY_SECONDS", "0"))
        self.REALISTIC_EXECUTION = _bool(env.get("REALISTIC_EXECUTION", "true"))

        # ------------------------------------------------------------ runtime
        self.KILL_SWITCH = _bool(env.get("KILL_SWITCH", "false"))
        self.DATABASE_PATH = env.get("DATABASE_PATH", "data/trading.db")
        self.LOG_DIR = env.get("LOG_DIR", "data/logs")
        self.DASHBOARD_PATH = env.get("DASHBOARD_PATH", "dashboard.html")
        self.DATA_SOURCE = env.get("DATA_SOURCE", "polymarket")  # polymarket | mock
        self.AI_PROVIDER = env.get("AI_PROVIDER", "heuristic")  # heuristic | openai
        self.MAX_MARKETS_PER_SCAN = int(env.get("MAX_MARKETS_PER_SCAN", "1000"))
        self.CACHE_TTL_SECONDS = int(env.get("CACHE_TTL_SECONDS", "300"))

        # Optional LLM (OpenAI-compatible endpoint). No key => heuristic.
        self.OPENAI_API_KEY = env.get("OPENAI_API_KEY", "")
        self.OPENAI_BASE_URL = env.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.LLM_MODEL = env.get("LLM_MODEL", "gpt-4o-mini")

    # ---------------------------------------------------------------- helpers
    def assert_safe(self):
        """Hard runtime guard - call before any execution path."""
        if self.REAL_TRADING:
            raise SafetyError("real trading must never be enabled")
        if self.KILL_SWITCH:
            return False
        return True

    def summary(self) -> dict:
        return {
            "REAL_TRADING": self.REAL_TRADING,
            "WALLET_CONNECTION": self.WALLET_CONNECTION,
            "ORDER_EXECUTION": self.ORDER_EXECUTION,
            "KILL_SWITCH": self.KILL_SWITCH,
            "STARTING_BALANCE": self.STARTING_BALANCE,
            "SCAN_INTERVAL_MINUTES": self.SCAN_INTERVAL_MINUTES,
            "MIN_EDGE": self.MIN_EDGE,
            "MIN_CONFIDENCE": self.MIN_CONFIDENCE,
            "MAX_POSITION_PERCENT": self.MAX_POSITION_PERCENT,
            "AI_PROVIDER": self.AI_PROVIDER,
            "DATA_SOURCE": self.DATA_SOURCE,
        }