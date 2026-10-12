"""Federated search plugin — web search backend provider.

Aggregates search results from multiple configured sub-backends and ranks
them by relevance (keyword scoring by default, optional LLM re-ranking).
Configuration lives under ``web.federated`` in config.yaml.
"""

from __future__ import annotations

from .provider import FederatedSearchProvider


def register(ctx) -> None:
    """Register the federated search provider with the plugin context."""
    ctx.register_web_search_provider(FederatedSearchProvider())
