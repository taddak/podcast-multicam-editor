from .base import Brain, BrainError, Decision, DecisionContext, TradeIntent
from .rules import RuleBrain

__all__ = ["Brain", "BrainError", "Decision", "DecisionContext", "TradeIntent", "RuleBrain", "build_brain"]


def build_brain(cfg):
    """Primary brain for the configured run."""
    if cfg.brain.type == "claude":
        from .claude import ClaudeBrain
        return ClaudeBrain(cfg.brain)
    return RuleBrain(cfg.risk.max_positions, cfg.risk.min_stop_loss_pct, cfg.risk.max_stop_loss_pct)
