"""Selects the active intent analyzer based on :class:`Config`."""

from __future__ import annotations

from ...config import Config, get_config
from .ast_analyzer import AstIntentAnalyzer
from .base import IntentAnalyzer
from .regex_analyzer import RegexIntentAnalyzer

_REGISTRY: dict[str, type] = {
    "regex": RegexIntentAnalyzer,
    "ast": AstIntentAnalyzer,
}


def get_analyzer(cfg: Config | None = None) -> IntentAnalyzer:
    cfg = cfg or get_config()
    cls = _REGISTRY.get(cfg.intent_analyzer, RegexIntentAnalyzer)
    return cls()
