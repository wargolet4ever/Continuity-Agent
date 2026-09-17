"""Public rule-plugin API."""

from .base import Issue, IssueFactory, RuleContext, RulePlugin, RuleRegistry
from .builtin import (
    AttemptRedPixelPlugin,
    ObservationRulePlugin,
    default_rule_registry,
)
from .identity import IdentityConsistencyPlugin, IdentitySimilarity, identity_similarity

__all__ = [
    "AttemptRedPixelPlugin",
    "IdentityConsistencyPlugin",
    "IdentitySimilarity",
    "Issue",
    "IssueFactory",
    "ObservationRulePlugin",
    "RuleContext",
    "RulePlugin",
    "RuleRegistry",
    "default_rule_registry",
    "identity_similarity",
]
