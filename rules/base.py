"""Stable plugin interface for deterministic continuity rules."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

Issue = dict[str, Any]
IssueFactory = Callable[[str, str, str, float, str], Issue]


@dataclass(frozen=True)
class RuleContext:
    """Read-only inputs visible to a local rule plugin."""

    shot_id: str
    canon: Any
    pack: dict[str, Any]
    observations: frozenset[str]
    previous_image: Any = None
    current_image: Any = None
    next_image: Any = None
    video_frames: tuple[Any, ...] = ()
    video_timestamps_s: tuple[float, ...] = ()

    @property
    def allowed_rule_ids(self) -> frozenset[str]:
        return frozenset(
            str(rule["id"]) for rule in self.pack.get("rules", []) if rule.get("id")
        )


class RulePlugin(Protocol):
    """A deterministic rule evaluator with no hidden global state."""

    plugin_id: str
    priority: int

    def evaluate(
        self, context: RuleContext, issue_factory: IssueFactory
    ) -> list[Issue]:
        """Return zero or more contract-compatible issue dictionaries."""


class RuleRegistry:
    """Ordered plugin registry with scope filtering and finding de-duplication."""

    def __init__(self, plugins: Iterable[RulePlugin] = ()):
        self._plugins: dict[str, RulePlugin] = {}
        for plugin in plugins:
            self.register(plugin)

    @property
    def plugins(self) -> tuple[RulePlugin, ...]:
        return tuple(
            sorted(
                self._plugins.values(),
                key=lambda plugin: (plugin.priority, plugin.plugin_id),
            )
        )

    def register(self, plugin: RulePlugin) -> None:
        plugin_id = getattr(plugin, "plugin_id", "")
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise ValueError("Rule plugin must define a non-empty plugin_id.")
        if plugin_id in self._plugins:
            raise ValueError(f"Duplicate rule plugin id: {plugin_id}")
        priority = getattr(plugin, "priority", None)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise TypeError(f"Rule plugin {plugin_id} must define an integer priority.")
        self._plugins[plugin_id] = plugin

    def evaluate(
        self, context: RuleContext, issue_factory: IssueFactory
    ) -> list[Issue]:
        merged: dict[str, Issue] = {}
        allowed = context.allowed_rule_ids
        for plugin in self.plugins:
            findings = plugin.evaluate(context, issue_factory)
            if not isinstance(findings, list):
                raise TypeError(f"Rule plugin {plugin.plugin_id} must return a list.")
            for finding in findings:
                if not isinstance(finding, dict):
                    raise TypeError(
                        f"Rule plugin {plugin.plugin_id} returned a non-object finding."
                    )
                rule_id = finding.get("rule_id")
                if not isinstance(rule_id, str) or not rule_id:
                    raise ValueError(
                        f"Rule plugin {plugin.plugin_id} returned a finding without rule_id."
                    )
                if rule_id not in allowed:
                    continue
                previous = merged.get(rule_id)
                if previous is None or finding.get("confidence", 0) > previous.get(
                    "confidence", 0
                ):
                    merged[rule_id] = finding
        return list(merged.values())
