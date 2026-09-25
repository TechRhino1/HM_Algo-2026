"""HM Algo 2.0 Settings Engine."""
import os
import json
import logging
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger("JARVIS_Config")

def verify_execution_mode(mode: str) -> str:
    """
    Verifies execution mode.
    Defaults to 'demo' for broker-connected paper testing (safe default).
    Returns 'live' only when explicitly requested.
    Returns 'paper' for offline / backtest simulation.
    """
    norm_mode = str(mode or "demo").lower().strip()
    if norm_mode == "live":
        return "live"
    if norm_mode in {"paper", "backtest", "simulated", "offline"}:
        return "paper"
    return "demo"


@dataclass
class RiskSettings:
    max_risk_per_trade_pct: float = 0.5
    max_portfolio_risk_pct: float = 2.5
    max_daily_loss_pct: float = 4.0
    max_drawdown_pct: float = 10.0
    max_open_positions: int = 3
    max_symbol_positions: int = 2
    min_rr_ratio: float = 2.0
    min_confidence_floor: float = 0.50
    partial_tp_ratio: float = 0.50
    partial_tp_r_multiple: float = 1.5
    # NOTE: `breakeven_atr_multiple` was REMOVED here. It was declared but never
    # read anywhere, and its value (1.0) directly contradicted the canonical exit
    # policy, which defers breakeven to +2R. Leaving a dead field named this
    # invites someone to "fix" it and reintroduce the premature-breakeven defect
    # that was responsible for the negative net profit. Breakeven is now owned
    # solely by jarvis.execution.exit_policy.
    min_trade_score: float = 75.0
    high_risk_trade_score: float = 85.0
    max_symbol_exposure_count: int = 1

@dataclass
class TradingSettings:
    default_mode: str = "demo"
    magic_number: int = 888999
    primary_timeframe: str = "H1"
    macro_timeframe: str = "D1"
    symbols: List[str] = field(default_factory=lambda: ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD"])
    same_symbol_cooldown_sec: int = 600
    # Entry selection authority. False (the default) keeps the legacy 29-check
    # gate stack, which is what the platform has always traded on. True hands
    # selection to jarvis.execution.entry_policy, using the out-of-sample
    # calibrated profile in config/winrate_profiles.json.
    #
    # This is deliberately opt-in. The profiles on disk currently show a
    # NEGATIVE out-of-sample expectancy for 11 of the 16 calibrated symbols, so
    # enabling this does not "improve" the trade set -- it switches most of the
    # universe off. That is a trading decision, not a bug fix, and it must be
    # taken knowingly rather than shipped as a default.
    use_calibrated_entry_policy: bool = False

@dataclass
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8501
    cors_origin: str = ""
    rate_limit_lockout_sec: float = 60.0
    max_login_attempts: int = 5

@dataclass
class MLSettings:
    sgd_learning_rate: float = 0.05
    sgd_l2_regularization: float = 0.01
    brier_score_drift_threshold: float = 0.28
    meta_labeler_min_window: int = 30
    meta_labeler_min_prob: float = 0.55
    bandit_exploration_c: float = 1.414

@dataclass
class JarvisConfig:
    risk: RiskSettings = field(default_factory=RiskSettings)
    trading: TradingSettings = field(default_factory=TradingSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    ml: MLSettings = field(default_factory=MLSettings)

    @classmethod
    def load(cls) -> "JarvisConfig":
        cfg = cls()
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        json_path = os.path.join(base_dir, "config", "settings.json")
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                r_data = data.get("risk", {})
                # (str-key, attr, caster) — previously only three of these were
                # read, so editing settings.json silently had no effect for the
                # rest. Keep the mapping explicit so the contract is obvious.
                risk_keys = (
                    ("max_risk_per_trade_pct", "max_risk_per_trade_pct", float),
                    ("max_daily_loss_pct", "max_daily_loss_pct", float),
                    ("max_drawdown_pct", "max_drawdown_pct", float),
                    ("max_open_positions", "max_open_positions", int),
                    ("max_symbol_positions", "max_symbol_positions", int),
                    ("max_symbol_exposure_count", "max_symbol_exposure_count", int),
                    ("min_rr_ratio", "min_rr_ratio", float),
                    ("min_confidence_floor", "min_confidence_floor", float),
                    ("min_trade_score", "min_trade_score", float),
                    ("high_risk_trade_score", "high_risk_trade_score", float),
                )
                for json_key, attr, caster in risk_keys:
                    if json_key in r_data and r_data[json_key] is not None:
                        try:
                            setattr(cfg.risk, attr, caster(r_data[json_key]))
                        except (TypeError, ValueError) as exc:
                            logger.warning(
                                f"Ignoring invalid risk.{json_key}={r_data[json_key]!r}: {exc}"
                            )
                # `breakeven_atr_trigger` is intentionally NOT loaded: breakeven
                # timing is owned by jarvis.execution.exit_policy. Warn loudly if
                # someone still has it in the file so its presence is not mistaken
                # for an active setting.
                if "breakeven_atr_trigger" in r_data:
                    logger.warning(
                        "config/settings.json contains 'risk.breakeven_atr_trigger' which is "
                        "IGNORED. Breakeven timing is defined in jarvis.execution.exit_policy "
                        "(be_trigger_r, default +2R). Remove the stale key to avoid confusion."
                    )
                t_data = data.get("trading", {})
                if "default_mode" in t_data and str(t_data["default_mode"]).lower() in {"live", "paper", "demo"}:
                    cfg.trading.default_mode = str(t_data["default_mode"]).lower()
                if "allowed_symbols" in t_data:
                    cfg.trading.symbols = list(t_data["allowed_symbols"])
                if "magic_number" in t_data:
                    cfg.trading.magic_number = int(t_data["magic_number"])
                if "same_symbol_cooldown_sec" in t_data:
                    cfg.trading.same_symbol_cooldown_sec = int(t_data["same_symbol_cooldown_sec"])
                if "primary_timeframe" in t_data:
                    cfg.trading.primary_timeframe = str(t_data["primary_timeframe"])
                if "use_calibrated_entry_policy" in t_data:
                    raw = t_data["use_calibrated_entry_policy"]
                    if isinstance(raw, bool):
                        cfg.trading.use_calibrated_entry_policy = raw
                    else:
                        cfg.trading.use_calibrated_entry_policy = str(raw).strip().lower() in {
                            "1", "true", "yes", "on",
                        }
                m_data = data.get("ml", {})
                ml_keys = (
                    ("sgd_learning_rate", "sgd_learning_rate", float),
                    ("sgd_l2_regularization", "sgd_l2_regularization", float),
                    ("brier_score_drift_threshold", "brier_score_drift_threshold", float),
                    ("meta_labeler_min_window", "meta_labeler_min_window", int),
                    ("meta_labeler_min_prob", "meta_labeler_min_prob", float),
                    ("bandit_exploration_c", "bandit_exploration_c", float),
                )
                for json_key, attr, caster in ml_keys:
                    if json_key in m_data and m_data[json_key] is not None:
                        try:
                            setattr(cfg.ml, attr, caster(m_data[json_key]))
                        except (TypeError, ValueError) as exc:
                            logger.warning(
                                f"Ignoring invalid ml.{json_key}={m_data[json_key]!r}: {exc}"
                            )
            except Exception as e:
                logger.warning(f"Could not load config/settings.json: {e}")

        env_mode = os.environ.get("JARVIS_MODE")
        if env_mode and env_mode.lower() in {"live", "paper", "demo"}:
            cfg.trading.default_mode = env_mode.lower()
        env_port = os.environ.get("JARVIS_PORT")
        if env_port and env_port.isdigit():
            cfg.server.port = int(env_port)
        env_symbols = os.environ.get("JARVIS_SYMBOLS")
        if env_symbols:
            cfg.trading.symbols = [s.strip().upper() for s in env_symbols.split(",") if s.strip()]
        env_calibrated = os.environ.get("JARVIS_CALIBRATED_ENTRY")
        if env_calibrated:
            cfg.trading.use_calibrated_entry_policy = env_calibrated.strip().lower() in {
                "1", "true", "yes", "on",
            }
        env_risk = os.environ.get("JARVIS_MAX_RISK_PCT")
        if env_risk:
            try:
                cfg.risk.max_risk_per_trade_pct = float(env_risk)
            except Exception as e:
                logger.warning(f"Ignoring invalid JARVIS_MAX_RISK_PCT={env_risk!r}: {e}")
        return cfg

SETTINGS = JarvisConfig.load()
