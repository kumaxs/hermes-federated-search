"""
Federated search provider — aggregate results from multiple search backends
with LLM-based relevance ranking.

Configuration in config.yaml::

    web:
      search_backend: federated
      federated:
        timeout: 10                  # seconds to wait for all backends
        max_results: 8               # top N results after ranking

        # ── K-way aggregation ──
        k: 3                         # exactly K backends required (max 32)
        min_backends: 2              # at least N backends must succeed

        # ── ranking ──
        ranker:                      # LLM for relevance ranking
                                     # Note: LLM providers may have concurrency limits.
                                     # When the ranking LLM fails (timeout, rate limit, etc.)
                                     # the plugin automatically falls back to keyword scoring.
                                     # For best results, use a non-main-model provider
                                     # or one with higher concurrency limits.
          provider: opencode-go
          model: deepseek-v4-flash
          prompt: |                  # optional: custom ranking preferences
            优先中文来源，优先官方文档

        # ── health checks (cached, avoids per-search quota consumption) ──
        health_check:
          ttl_seconds: 300           # cache probe results for N seconds
          timeout: 5                 # per-probe timeout

        # ── K backends (exactly K) ──
        backends:
          - name: tavily             # use an existing registered provider
            required: false
          - name: minimax            # custom HTTP backend
            type: custom
            base_url: "https://api.minimaxi.com"
            api_key_env: MINIMAX_CN_API_KEY
            search_path: /v1/coding_plan/search
            query_param: "q"
            required: false
          - name: searxng
            type: custom
            base_url: "https://search.example.com"
            api_key_env: SEARXNG_API_KEY
            required: true

Multi-card pool (``type: custom`` lines)::

    backends:
      - name: minimax
        type: custom
        base_url: "https://api.minimaxi.com"
        search_path: /v1/coding_plan/search
        query_param: "q"
        credentials: [MINIMAX_KEY_1, MINIMAX_KEY_2]   # env var NAMES only
        pool_strategy: round_robin    # fill_first | round_robin | random

Only environment variable *names* live in config; the values are read from
the runtime environment (per-profile ``.env``) and never persisted or
logged.  A failed card cools down (401→300s, 403→300s, 429→3600s, other→
300s; a single usable card caps transient cooldowns at 60s) and the request
fails over to the next usable card.

The same options are editable through the official Settings → Plugins form
(``plugin.yaml`` ``config_schema``; keys land under
``plugins.entries.hermes-federated-search.settings``).  Per top-level key,
settings win over the legacy ``web.federated`` section — both stay
supported.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import random
import re
import time
from typing import Any, Dict, List, Optional

from agent.web_search_provider import WebSearchProvider, get_provider_env

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = 10
_DEFAULT_MAX_RESULTS = 8
_CUSTOM_BACKEND_TIMEOUT = 15
_CUSTOM_BACKEND_MAX_RESULTS = 10
_SKIP_RANK_IF = 3
_MAX_RANK_INPUT = 10
_RANK_TIMEOUT = 20

# K-way aggregation
_MAX_K = 32

# Health check defaults
_DEFAULT_HEALTH_TTL = 300
_DEFAULT_HEALTH_TIMEOUT = 5

# Official plugin settings tree (Settings → Plugins, manifest v2 config_schema).
# Per top-level key its values win over the legacy ``web.federated`` section.
_PLUGIN_ID = "hermes-federated-search"
_SETTINGS_SEGMENTS = ("plugins", "entries", _PLUGIN_ID, "settings")

# Multi-card pool (line-level ``credentials``)
_POOL_STRATEGIES = ("fill_first", "round_robin", "random")
_DEFAULT_POOL_STRATEGY = "fill_first"
# Cooldowns mirror the Hermes credential-pool values (401→300s, 403→300s,
# 429→3600s); anything else (5xx / network / unknown) uses the v1 300s default.
_CARD_COOLDOWN_SECONDS = {401: 300, 403: 300, 429: 3600}
_DEFAULT_CARD_COOLDOWN = 300
# A line with a single usable card caps *transient* cooldowns at 60s
# (host sole_credential alignment; the legacy api_key_env path is NOT capped).
_SOLE_CARD_TRANSIENT_CAP = 60
_CREDENTIAL_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
# Secret-ID shapes that still pass the env-name grammar — reject them so a
# pasted key never lands in config.yaml (residual window closed by the
# desensitization scan).  The vendor prefixes are spelled with a character
# class so this rejection rule itself carries no literal key-ID prefix
# (desensitization scan: zero strong-signal hits on the repository).
_SECRET_LIKE_PATTERNS = (re.compile(r"^(?:A[K]IA|A[S]IA)[0-9A-Z]{16}$"),)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _read_config() -> Optional[Dict[str, Any]]:
    """Read federated search configuration from the raw config file.

    Merges two sections, per top-level key (official settings win):

    - ``web.federated`` — legacy location (kept working; no forced migration)
    - ``plugins.entries.hermes-federated-search.settings`` — the official
      plugin settings tree written by Settings → Plugins (manifest v2
      ``config_schema``)

    Returns ``None`` when neither section exists (not configured).  Raw-YAML
    access is kept rather than ``load_config()`` because the schema validator
    strips unknown keys from the ``web`` section during loading.
    """
    try:
        from hermes_cli.config import get_config_path
        try:
            import hermes_yaml as yaml
        except ImportError:  # standalone installs without the Hermes internal module
            import yaml
        config_path = get_config_path()
        if not config_path.exists():
            return None
        with open(config_path, encoding="utf-8-sig") as f:
            cfg = yaml.safe_load(f) or {}

        web = cfg.get("web")
        web_section = web.get("federated") if isinstance(web, dict) else None
        settings: Any = cfg
        for segment in _SETTINGS_SEGMENTS:
            settings = settings.get(segment) if isinstance(settings, dict) else None
        if not isinstance(web_section, dict):
            web_section = None
        if not isinstance(settings, dict):
            settings = None
        if web_section is None and settings is None:
            return None

        merged: Dict[str, Any] = {}
        if web_section:
            merged.update(web_section)
        if settings:
            merged.update(settings)
        return merged
    except Exception:  # health: allow BLE001 -- best-effort raw-config read; failure falls back to defaults
        return None


def _get_registered_provider(name: str) -> Optional[WebSearchProvider]:
    try:
        from agent.web_search_registry import get_provider
        return get_provider(name)
    except Exception:  # health: allow BLE001 -- optional backend lookup; an unregistered name is skipped
        return None


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def _pool_strategy_error(value: Any, label: str) -> Optional[str]:
    """Validate a pool-strategy setting without echoing its raw value."""
    if str(value) in _POOL_STRATEGIES:
        return None
    return f"{label} must be one of: {', '.join(_POOL_STRATEGIES)}"


def _validate_credentials(backend_name: str, credentials: Any) -> Optional[str]:
    """Validate one custom line's ``credentials`` list. Error message or None.

    Rules:

    - must be a list of environment variable names matching
      ``^[A-Z][A-Z0-9_]{0,127}$`` (empty list = not configured);
    - known secret-value shapes are rejected so a pasted key never lands in
      config.yaml (a value only ever lives in the runtime environment);
    - duplicate names are deduplicated at selection time (warned here).

    Error messages carry the item index and the rule, never the offending
    value.
    """
    if credentials is None:
        return None
    if not isinstance(credentials, list):
        return (
            f"backend '{backend_name}': credentials must be a list of "
            f"environment variable names"
        )
    seen: set = set()
    for i, item in enumerate(credentials):
        if not isinstance(item, str):
            return (
                f"backend '{backend_name}': credentials[{i}] must be an "
                f"environment variable name string"
            )
        if not _CREDENTIAL_NAME_RE.match(item):
            return (
                f"backend '{backend_name}': credentials[{i}] is not a valid "
                f"environment variable name (expected ^[A-Z][A-Z0-9_]{{0,127}}$)"
            )
        if any(pattern.match(item) for pattern in _SECRET_LIKE_PATTERNS):
            return (
                f"backend '{backend_name}': credentials[{i}] looks like a "
                f"secret value — list only the environment variable name; the "
                f"key itself belongs in the runtime environment"
            )
        if item in seen:
            logger.warning(
                "backend '%s': duplicate credential name %s ignored",
                backend_name, item,
            )
        seen.add(item)
    return None


def _validate_config(config: Dict[str, Any]) -> Optional[str]:
    """Validate federated search configuration. Returns error message or None.

    Checks:
    - k ≤ _MAX_K
    - len(backends) == k
    - 1 ≤ min_backends ≤ k
    - ranking_mode / pool_strategy are known values
    - custom lines: credentials grammar + mis-entry rejection (multi-card)
    - registered lines: credentials are ignored (warning, host single card)
    """
    backends = config.get("backends", [])
    if not isinstance(backends, list) or not backends:
        return "no search backends configured"

    k = int(config.get("k", len(backends)))
    if k > _MAX_K:
        return f"k={k} exceeds maximum {_MAX_K}"
    if k < 1:
        return f"k={k} must be at least 1"

    if len(backends) != k:
        return (
            f"k={k} but {len(backends)} backend(s) configured — "
            f"exactly {k} required"
        )

    min_backends = int(config.get("min_backends", 1))
    if min_backends < 1:
        return f"min_backends={min_backends} must be at least 1"
    if min_backends > k:
        return f"min_backends={min_backends} cannot exceed k={k}"

    if config.get("pool_strategy") is not None:
        error = _pool_strategy_error(config.get("pool_strategy"), "pool_strategy")
        if error:
            return error
    if config.get("ranking_mode") is not None:
        mode = str(config.get("ranking_mode"))
        if mode not in ("auto", "keyword", "llm"):
            return "ranking_mode must be one of: auto, keyword, llm"

    for backend in backends:
        if not isinstance(backend, dict):
            continue
        name = str(backend.get("name", "?"))
        typ = str(backend.get("type", "") or "")
        credentials = backend.get("credentials")
        if credentials is not None and typ != "custom":
            logger.warning(
                "backend '%s': 'credentials' is ignored on a registered "
                "provider line (the host resolves its own key; multi-card "
                "pooling requires type: custom)", name,
            )
            continue
        if typ == "custom":
            error = _validate_credentials(name, credentials)
            if error:
                return error
            line_strategy = backend.get("pool_strategy")
            if line_strategy is not None:
                error = _pool_strategy_error(
                    line_strategy, f"backend '{name}' pool_strategy")
                if error:
                    return error

    return None


# ---------------------------------------------------------------------------
# Health check cache
# ---------------------------------------------------------------------------


class _HealthCache:
    """TTL-based cache for backend health probe results.

    Avoids consuming provider quota on every search by caching HEAD probe
    results for ``ttl_seconds``.  A real search failure (HTTP 401/403/429)
    bypasses the cache and marks the backend unavailable for the configured
    cooldown period.
    """

    def __init__(self, ttl_seconds: int = _DEFAULT_HEALTH_TTL):
        self._ttl = ttl_seconds
        self._cache: Dict[str, tuple[bool, float]] = {}
        # Cooldown after real search failure: status_code → seconds
        self._failure_cooldown: Dict[int, int] = {
            401: 300,
            403: 300,
            429: 3600,
        }

    def is_available(self, backend_name: str) -> Optional[bool]:
        """Return cached availability or None if cache miss."""
        entry = self._cache.get(backend_name)
        if entry is None:
            return None
        available, timestamp = entry
        if time.time() - timestamp < self._ttl:
            return available
        # Expired — remove and return miss
        del self._cache[backend_name]
        return None

    def set_available(self, backend_name: str, available: bool) -> None:
        self._cache[backend_name] = (available, time.time())

    def mark_failed(self, backend_name: str, status_code: int) -> None:
        """Mark backend unavailable after a real search failure.

        Uses a longer cooldown than the probe TTL — quota/billing failures
        don't resolve in seconds.
        """
        cooldown = self._failure_cooldown.get(status_code, 300)
        # Store with an artificial timestamp so the entry expires after
        # cooldown seconds regardless of normal TTL.
        self._cache[backend_name] = (False, time.time() - self._ttl + cooldown)


# ---------------------------------------------------------------------------
# Multi-card pool (line-level ``credentials``)
# ---------------------------------------------------------------------------


def _card_value(env_name: str) -> str:
    """Resolve one card's secret from the runtime environment.

    Values are never persisted or logged.  Empty / whitespace-only values and
    values containing line breaks (a mis-pasted multi-line secret) are treated
    as not configured.
    """
    value = (get_provider_env(env_name) or "").strip()
    if not value:
        return ""
    if "\n" in value or "\r" in value:
        logger.warning("card %s skipped: value contains a line break", env_name)
        return ""
    return value


class _CardPool:
    """Process-local credential pool for one federated line.

    State is keyed by (line name, env var name): cooldown expiry timestamps,
    plus one round-robin cursor per line.  Cooldowns mirror the Hermes
    credential-pool values (401→300s, 403→300s, 429→3600s); other statuses
    (5xx / network / unknown) use the v1 300s default, capped at 60s when the
    line has a single usable card (sole-credential alignment; pool path only).
    Restarting Hermes clears every cooldown (in-process state by design).
    """

    def __init__(self) -> None:
        self._cooldowns: Dict[str, float] = {}
        self._cursors: Dict[str, int] = {}
        self.default_strategy = _DEFAULT_POOL_STRATEGY

    @staticmethod
    def _key(line: str, env_name: str) -> str:
        return f"{line}\x00{env_name}"

    def remaining_cooldown(self, line: str, env_name: str) -> float:
        """Seconds until *env_name* is usable again (0 when not cooling)."""
        key = self._key(line, env_name)
        expiry = self._cooldowns.get(key)
        if expiry is None:
            return 0.0
        left = expiry - time.time()
        if left <= 0:
            self._cooldowns.pop(key, None)
            return 0.0
        return left

    def mark_failed(
        self,
        line: str,
        env_name: str,
        status_code: Optional[int],
        sole_card: bool = False,
    ) -> int:
        """Cool a failed card down; returns the cooldown applied, in seconds."""
        if status_code in _CARD_COOLDOWN_SECONDS:
            seconds = _CARD_COOLDOWN_SECONDS[status_code]
        else:
            seconds = _DEFAULT_CARD_COOLDOWN
            if sole_card:
                seconds = min(seconds, _SOLE_CARD_TRANSIENT_CAP)
        self._cooldowns[self._key(line, env_name)] = time.time() + seconds
        return seconds

    def select(
        self,
        line: str,
        cards: List[str],
        strategy: Optional[str] = None,
        exclude: Any = frozenset(),
    ) -> Optional[str]:
        """Pick the next usable card for *line*, or ``None`` when none is left.

        Usable = value present (the caller passes only value-bearing names)
        and not cooling down.  ``fill_first`` takes the first usable card in
        order, ``round_robin`` rotates a per-line cursor, ``random`` picks
        uniformly inside the usable set.  ``exclude`` skips names already
        tried within the current request.
        """
        usable = [
            card for card in cards
            if card not in exclude and self.remaining_cooldown(line, card) <= 0
        ]
        if not usable:
            return None
        if strategy not in _POOL_STRATEGIES:
            strategy = self.default_strategy
        if strategy not in _POOL_STRATEGIES:
            strategy = _DEFAULT_POOL_STRATEGY
        if strategy == "random":
            return random.choice(usable)
        if strategy == "round_robin":
            count = len(cards)
            start = self._cursors.get(line, 0) % count
            for offset in range(count):
                index = (start + offset) % count
                if cards[index] in usable:
                    self._cursors[line] = (index + 1) % count
                    return cards[index]
            return None
        return usable[0]


# ---------------------------------------------------------------------------
# Backend health probe
# ---------------------------------------------------------------------------


def _probe_backend(
    backend: Dict[str, Any],
    timeout: int = _DEFAULT_HEALTH_TIMEOUT,
) -> bool:
    """Lightweight availability probe for a custom HTTP backend.

    Sends HEAD to ``base_url`` (no search query, no API consumption).
    For registered providers, delegates to ``provider.is_available()``.

    Returns True when the backend appears reachable.
    """
    name = str(backend.get("name", "?"))
    typ = str(backend.get("type", "") or "")

    if typ != "custom":
        provider = _get_registered_provider(name)
        if provider is None:
            return False
        try:
            return provider.is_available()
        except Exception:  # health: allow BLE001 -- best-effort availability probe; any failure reads as unavailable
            return False

    # Custom HTTP backend — HEAD probe
    import httpx
    base_url = (backend.get("base_url") or "").rstrip("/")
    if not base_url:
        return False

    try:
        resp = httpx.head(base_url, timeout=timeout, follow_redirects=True)
        # 2xx/3xx/4xx all mean the service is reachable (auth failures are
        # handled at search time, not probe time)
        return resp.status_code < 500
    except Exception:  # health: allow BLE001 -- best-effort HTTP probe; any failure reads as unreachable
        return False


# ---------------------------------------------------------------------------
# LLM ranking & keyword ranking
# ---------------------------------------------------------------------------


def _keyword_rank(
    query: str,
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Rank results by keyword match frequency in title + description.

    Fast, no LLM needed. De-duplicates by URL.
    """
    seen_urls = set()
    deduped = []
    for r in results:
        url = r.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            deduped.append(r)

    terms = query.lower().split()
    def _score(r):
        title = (r.get("title", "") or "").lower()
        desc = (r.get("description", "") or "").lower()
        return sum(1 for t in terms if t in title or t in desc)

    deduped.sort(key=_score, reverse=True)
    return deduped


def _rank_results(
    query: str,
    results: List[Dict[str, Any]],
    ranker_config: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Rank search results by relevance to *query*.

    Ranking mode is selected by the ``provider`` field in *ranker_config*:

    - ``"none"`` / unset / empty → keyword match scoring (fast, no LLM).
    - ``"auto"`` or a real provider name → LLM-based ranking.
      On LLM failure (timeout, rate limit, concurrency limit) the function
      automatically falls back to keyword (``none``) ranking.

    If *ranker_config* contains a ``prompt`` key, it is used as the system
    prompt for LLM ranking, allowing users to inject preferences
    (e.g. "prefer Chinese sources", "prefer official documentation").

    Optimizations:
    - Skip LLM ranking when <= _SKIP_RANK_IF results.
    - Truncate titles to 60 chars, descriptions to 80 chars.
    - Hard timeout via _RANK_TIMEOUT.
    - Thinking is disabled on the LLM call (``reasoning_effort=off``): the
      small output budget goes to the JSON array, not hidden reasoning.
    """
    if not results or len(results) <= _SKIP_RANK_IF:
        return results

    provider = (ranker_config or {}).get("provider") or ""
    model = (ranker_config or {}).get("model") or ""

    # "none" → keyword ranking, no LLM
    if not provider or provider == "none":
        return _keyword_rank(query, results)

    # ── LLM-based ranking ──
    try:
        from agent.auxiliary_client import call_llm

        lines = []
        for i, r in enumerate(results):
            title = (r.get("title", "") or "")[:60]
            desc = (r.get("description", "") or "")[:80]
            lines.append(f"[{i + 1}] {title}\n{desc}")
        results_text = "\n".join(lines)

        custom_prompt = (ranker_config or {}).get("prompt", "").strip()
        if custom_prompt:
            sys = custom_prompt + (
                "\n\nReturn ONLY a JSON array of result indices ranked by "
                "relevance, e.g. [3,1,5,2,4]. Include ALL results. "
                "No other text."
            )
        else:
            sys = (
                "You rank search results by relevance. Rules:\n"
                "- Return ONLY a JSON array of result indices ranked by "
                "relevance, e.g. [3,1,5,2,4]\n"
                "- Include ALL results. Most relevant first.\n"
                "- No other text."
            )
        user = f"Query: {query}\n\nResults:\n{results_text}\n\nRanked indices:"

        logger.info(
            "LLM ranking %d results (provider=%s, model=%s)",
            len(results), provider or "auto", model or "auto",
        )

        response = call_llm(
            task="web_extract",
            provider=provider or None,
            model=model or None,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0,
            max_tokens=128,
            timeout=_RANK_TIMEOUT,
            # Ranking is a mechanical ordering step: keep thinking-capable routes
            # (e.g. DeepSeek on CommandCode defaults to thinking) from spending the
            # small output budget on hidden reasoning — empty output / timeout.
            extra_body={"reasoning_effort": "off"},
        )

        raw = (response.choices[0].message.content or "").strip()
        import json, re

        match = re.search(r"\[[\d\s,,]+\]", raw)
        if match:
            indices = json.loads(match.group())
            if isinstance(indices, list):
                ranked, seen = [], set()
                for idx in indices:
                    pos = int(idx) - 1
                    if 0 <= pos < len(results) and pos not in seen:
                        ranked.append(results[pos])
                        seen.add(pos)
                for i, r in enumerate(results):
                    if i not in seen:
                        ranked.append(r)
                return ranked

        logger.warning("LLM ranking unparseable, falling back to keyword ranking")
    except Exception as exc:
        logger.warning("LLM ranking failed (%s), falling back to keyword ranking", exc, exc_info=True)

    return _keyword_rank(query, results)


def _apply_ranking_mode(
    ranker_config: Optional[Dict[str, Any]],
    ranking_mode: str,
) -> Optional[Dict[str, Any]]:
    """Map the ``ranking_mode`` config item onto the ``ranker`` dict.

    - ``auto`` (default) → unchanged; ``ranker.provider`` decides
      (unset/``none`` → keyword, otherwise LLM with keyword fallback).
    - ``keyword`` → force keyword scoring (``ranker.provider = "none"``).
    - ``llm`` → force LLM ranking (``ranker.provider`` unset/``none`` →
      ``"auto"``, i.e. the default auxiliary model).

    The ``ranker`` dict (provider/model/prompt) stays usable alongside.
    """
    mode = (ranking_mode or "auto").strip().lower()
    if mode == "keyword":
        config = dict(ranker_config or {})
        config["provider"] = "none"
        return config
    if mode == "llm":
        config = dict(ranker_config or {})
        if not config.get("provider") or str(config.get("provider")) == "none":
            config["provider"] = "auto"
        return config
    return ranker_config


# ---------------------------------------------------------------------------
# Custom HTTP backend search
# ---------------------------------------------------------------------------


def _search_custom_backend(
    backend_config: Dict[str, Any],
    query: str,
    limit: int,
    card_pool: Optional[_CardPool] = None,
) -> tuple[List[Dict[str, Any]], Optional[int]]:
    """Execute a search against a custom HTTP endpoint.

    Single-card config keys: base_url, api_key_env, search_path, query_param
    (default ``q``), auth_style (``bearer`` or ``x-api-key``, default ``bearer``).

    Multi-card: ``credentials`` (list of environment variable names — values
    are read from the runtime environment, never from config) plus an optional
    line-level ``pool_strategy`` (fill_first | round_robin | random).  With
    ``credentials`` a failed card cools down in *card_pool* and the request
    fails over to the next usable card; each card is dialed at most once.

    Returns ``(results, status_code)`` where *status_code* is ``None`` on
    success or non-HTTP errors, and the response status code on HTTP errors
    so the caller can invoke health-cache cooldowns.
    """
    import httpx

    base_url = (backend_config.get("base_url") or "").rstrip("/")
    search_path = backend_config.get("search_path", "/v1/coding_plan/search")
    query_param = backend_config.get("query_param", "q")
    auth_style = backend_config.get("auth_style", "bearer")
    credentials = backend_config.get("credentials")

    if isinstance(credentials, list) and credentials:
        line = str(backend_config.get("name", "?"))
        pool = card_pool if isinstance(card_pool, _CardPool) else _CardPool()
        strategy = backend_config.get("pool_strategy")
        if strategy not in _POOL_STRATEGIES:
            strategy = None  # fall back to the pool's configured default
        return _search_custom_backend_pooled(
            base_url, search_path, query_param, auth_style,
            credentials, line, strategy, pool, query, limit,
        )

    api_key_env = backend_config.get("api_key_env", "")
    api_key = get_provider_env(api_key_env) if api_key_env else ""

    if not base_url or not api_key:
        return [], None

    url = f"{base_url}{search_path}"
    if auth_style == "x-api-key":
        headers = {"Content-Type": "application/json", "x-api-key": api_key}
    else:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    payload = {query_param: query, "max_results": min(limit, _CUSTOM_BACKEND_MAX_RESULTS)}

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=_CUSTOM_BACKEND_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        results = _extract_custom_results(data)
        if not results:
            logger.warning("Custom backend returned no parseable results")
        return results, None
    except httpx.TimeoutException:
        logger.warning("Custom backend timed out: %s", url)
    except httpx.HTTPStatusError as exc:
        logger.warning("Custom backend HTTP error: %s (%s)", exc, exc.response.text[:200])
        return [], exc.response.status_code
    except Exception as exc:
        logger.warning("Custom backend failed: %s", exc, exc_info=True)
    return [], None


def _search_custom_backend_pooled(
    base_url: str,
    search_path: str,
    query_param: str,
    auth_style: str,
    credentials: List[Any],
    line: str,
    strategy: Optional[str],
    pool: _CardPool,
    query: str,
    limit: int,
) -> tuple[List[Dict[str, Any]], Optional[int]]:
    """Multi-card search for one custom line: select → dial → fail over.

    A card that fails (HTTP status — classified or not — or a network error)
    cools down in *pool* and the request moves to the next usable card.  A
    single usable card caps transient cooldowns at 60s.  All cards exhausted
    → line-level failure, keeping the ``(results, status_code)`` contract
    (*status_code* = last HTTP failure, or ``None`` when nothing was dialed).
    """
    import httpx

    cards = list(dict.fromkeys(c for c in credentials if isinstance(c, str)))
    values = {name: _card_value(name) for name in cards}
    usable = [name for name in cards if values[name]]
    if not usable:
        logger.warning(
            "backend '%s': no usable credentials among %d configured card(s)",
            line, len(cards),
        )
        return [], None
    if not base_url:
        return [], None

    sole_card = len(usable) <= 1
    url = f"{base_url}{search_path}"
    payload = {query_param: query, "max_results": min(limit, _CUSTOM_BACKEND_MAX_RESULTS)}

    tried: set = set()
    last_status: Optional[int] = None
    while True:
        env_name = pool.select(line, usable, strategy=strategy, exclude=tried)
        if env_name is None:
            break
        tried.add(env_name)
        if auth_style == "x-api-key":
            headers = {"Content-Type": "application/json", "x-api-key": values[env_name]}
        else:
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {values[env_name]}"}
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=_CUSTOM_BACKEND_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            results = _extract_custom_results(data)
            if not results:
                logger.warning("Custom backend returned no parseable results")
            return results, None
        except httpx.TimeoutException:
            logger.warning("Custom backend timed out: %s (card %s)", line, env_name)
            pool.mark_failed(line, env_name, None, sole_card)
        except httpx.HTTPStatusError as exc:
            # Status only — never echo the response body (it can quote the key).
            last_status = exc.response.status_code
            logger.warning(
                "Custom backend HTTP error: %s (card %s, status %s)",
                line, env_name, last_status,
            )
            pool.mark_failed(line, env_name, last_status, sole_card)
        except Exception as exc:
            logger.warning("Custom backend failed: %s (card %s)", line, env_name, exc, exc_info=True)
            pool.mark_failed(line, env_name, None, sole_card)
    return [], last_status


def _extract_custom_results(data: Any) -> List[Dict[str, Any]]:
    """Extract search results from various API response shapes."""
    if not isinstance(data, dict):
        return []

    results: List[Dict[str, Any]] = []

    # {organic: [{title, link, snippet, date}, ...]}
    organic = data.get("organic")
    if isinstance(organic, list):
        for item in organic:
            if isinstance(item, dict):
                results.append({
                    "title": str(item.get("title", "") or ""),
                    "url": str(item.get("link", "") or item.get("url", "") or ""),
                    "description": str(item.get("snippet", "") or item.get("content", "") or ""),
                    "position": len(results) + 1,
                })
        if results:
            return results

    # {data: {web: [...]}}
    web_data = data.get("data")
    if isinstance(web_data, dict):
        web_list = web_data.get("web")
        if isinstance(web_list, list):
            for item in web_list:
                if isinstance(item, dict):
                    results.append({
                        "title": str(item.get("title", "") or ""),
                        "url": str(item.get("url", "") or ""),
                        "description": str(item.get("description", "") or item.get("content", "") or ""),
                        "position": len(results) + 1,
                    })
            if results:
                return results

    # {results: [{...}]}
    raw = data.get("results")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                results.append({
                    "title": str(item.get("title", "") or ""),
                    "url": str(item.get("url", "") or item.get("link", "") or ""),
                    "description": str(item.get("content", "") or item.get("snippet", "") or ""),
                    "position": len(results) + 1,
                })
        if results:
            return results
    return results


# ---------------------------------------------------------------------------
# Per-backend worker (used by ThreadPoolExecutor)
# ---------------------------------------------------------------------------


def _search_one_backend(
    backend: Dict[str, Any],
    query: str,
    limit: int,
    card_pool: Optional[_CardPool] = None,
) -> tuple[List[Dict[str, Any]], Optional[int]]:
    """Single-backend search worker for thread-pool execution.

    Returns ``(results, status_code)``. *status_code* carries the HTTP status
    for custom backends that fail with HTTP errors so the caller can invoke
    health-cache cooldowns.  *card_pool* carries the process-local multi-card
    state for custom lines with ``credentials``.
    """
    name = str(backend.get("name", "?"))
    typ = str(backend.get("type", "") or "")

    try:
        if typ == "custom":
            results, status_code = _search_custom_backend(backend, query, limit, card_pool)
        else:
            provider = _get_registered_provider(name)
            if provider is None:
                logger.warning("Backend '%s' not registered, skipping", name)
                return [], None
            resp = provider.search(query, limit=limit)
            if isinstance(resp, dict) and resp.get("success"):
                data = resp.get("data", {})
                items = data.get("web", []) if isinstance(data, dict) else []
                results = [
                    {"title": str(r.get("title", "") or ""),
                     "url": str(r.get("url", "") or ""),
                     "description": str(r.get("description", "") or ""),
                     "position": i + 1}
                    for i, r in enumerate(items) if isinstance(r, dict)
                ]
            else:
                err = resp.get("error", "unknown") if isinstance(resp, dict) else "unknown"
                logger.warning("Backend '%s' failed: %s", name, err)
                return [], None
            status_code = None
        logger.info("Backend '%s' returned %d results", name, len(results))
        return results, status_code
    except Exception as exc:
        logger.warning("Backend '%s' error: %s", name, exc, exc_info=True)
        return [], None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class FederatedSearchProvider(WebSearchProvider):
    """Aggregated search provider that fans out to multiple sub-backends.

    Supports K-way aggregation with per-backend health checking,
    configurable re-rank prompts, and fault-tolerance via ``min_backends``
    and ``required`` flags.
    """

    def __init__(self) -> None:
        super().__init__()
        self._health_cache: Optional[_HealthCache] = None
        self._card_pool: Optional[_CardPool] = None

    def _get_health_cache(self, config: Dict[str, Any]) -> _HealthCache:
        if self._health_cache is None:
            hc = config.get("health_check") or {}
            ttl = int(hc.get("ttl_seconds", _DEFAULT_HEALTH_TTL))
            self._health_cache = _HealthCache(ttl_seconds=ttl)
        return self._health_cache

    def _get_card_pool(self, config: Dict[str, Any]) -> _CardPool:
        """Process-local multi-card state; survives across searches of this
        provider instance (restart clears cooldowns and cursors)."""
        if self._card_pool is None:
            self._card_pool = _CardPool()
        strategy = str(config.get("pool_strategy") or _DEFAULT_POOL_STRATEGY)
        if strategy in _POOL_STRATEGIES:
            self._card_pool.default_strategy = strategy
        return self._card_pool

    @property
    def name(self) -> str:
        return "federated"

    @property
    def display_name(self) -> str:
        return "Federated Search"

    def is_available(self) -> bool:
        config = _read_config()
        if not config:
            return False
        backends = config.get("backends")
        return isinstance(backends, list) and len(backends) > 0

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        try:
            from tools.interrupt import is_interrupted
            if is_interrupted():
                return {"success": False, "error": "Interrupted"}

            config = _read_config()
            if not config:
                return {"success": False, "error": "federated search not configured"}

            # ── config validation ──
            validation_error = _validate_config(config)
            if validation_error:
                return {"success": False, "error": validation_error}

            backends: List[Dict[str, Any]] = config["backends"]
            timeout = int(config.get("timeout", _DEFAULT_TIMEOUT))
            max_results = int(config.get("max_results", _DEFAULT_MAX_RESULTS))
            ranker_config = _apply_ranking_mode(
                config.get("ranker"), str(config.get("ranking_mode") or "auto"))
            min_backends = int(config.get("min_backends", 1))

            # ── multi-card pool (custom lines with credentials only) ──
            needs_card_pool = any(
                isinstance(b, dict)
                and str(b.get("type", "") or "") == "custom"
                and isinstance(b.get("credentials"), list)
                and b.get("credentials")
                for b in backends
            )
            card_pool = self._get_card_pool(config) if needs_card_pool else None

            # ── health checks (cached, TTL-based) ──
            # Only active when ``health_check`` is explicitly configured.
            # Without it, all backends are dispatched directly (backward compat).
            hc_config = config.get("health_check")
            health_cache: Optional[_HealthCache] = None
            if hc_config is not None:
                health_cache = self._get_health_cache(config)
                probe_timeout = int(hc_config.get("timeout", _DEFAULT_HEALTH_TIMEOUT))

                active_backends: List[Dict[str, Any]] = []
                for b in backends:
                    name = str(b.get("name", "?"))
                    cached = health_cache.is_available(name)
                    if cached is False:
                        logger.info("Backend '%s' cached as unavailable, skipping probe", name)
                        continue
                    if cached is True:
                        active_backends.append(b)
                        continue
                    available = _probe_backend(b, timeout=probe_timeout)
                    health_cache.set_available(name, available)
                    if available:
                        active_backends.append(b)
                    else:
                        logger.info("Backend '%s' probe failed, skipping", name)
            else:
                active_backends = list(backends)

            if not active_backends:
                return {"success": False, "error": "No backends available"}

            logger.info(
                "Federated search: '%s' (%d/%d backends active, timeout=%ds, max_results=%d)",
                query, len(active_backends), len(backends), timeout, max_results,
            )

            # ── parallel backend execution ──
            all_results: List[Dict[str, Any]] = []
            backend_results: Dict[str, tuple[List[Dict], Optional[int]]] = {}
            errors: List[str] = []
            start_time = time.time()

            pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(active_backends), _MAX_K),
            )
            try:
                # Fresh worker threads do not inherit the caller's ContextVars
                # (profile home override + secret scope). Under multiplexed
                # hosting a bare worker's credential reads fail closed with
                # UnscopedSecretError; run each worker in a copy of the caller's
                # context — the same pattern as in-tree cron/scheduler_delivery.py.
                extra = (card_pool,) if card_pool is not None else ()
                futures = {
                    pool.submit(
                        contextvars.copy_context().run,
                        _search_one_backend, b, query, limit, *extra,
                    ): b
                    for b in active_backends
                }

                done, not_done = concurrent.futures.wait(
                    futures, timeout=timeout,
                )

                for f in not_done:
                    b = futures[f]
                    name = str(b.get("name", "?"))
                    errors.append(f"backend '{name}' timed out")
                    backend_results[name] = ([], None)
                    f.cancel()

                for f in done:
                    if is_interrupted():
                        break
                    b = futures[f]
                    name = str(b.get("name", "?"))
                    try:
                        results, status_code = f.result(timeout=2)
                        all_results.extend(results)
                        backend_results[name] = (results, status_code)
                        # Wire real search failures into the health cache so
                        # the documented 401/403/429 cooldown actually fires.
                        if health_cache is not None and status_code is not None:
                            health_cache.mark_failed(name, status_code)
                    except Exception as exc:  # health: allow BLE001 -- per-backend failure is surfaced in the response's errors list
                        errors.append(f"backend '{name}' failed: {exc}")
                        backend_results[name] = ([], None)

                if not_done:
                    logger.warning(
                        "Federated search timed out after %ds, collected partial "
                        "results from %d/%d backends",
                        timeout, len(done), len(futures),
                    )
            finally:
                pool.shutdown(wait=False)

            # ── outcome evaluation ──
            successful = [
                name for name, (results, _) in backend_results.items()
                if results
            ]
            required_failed = []
            for b in backends:
                name = str(b.get("name", "?"))
                if b.get("required", False) and name not in successful:
                    required_failed.append(name)

            if required_failed:
                for name in required_failed:
                    if health_cache is not None:
                        health_cache.set_available(name, False)
                return {
                    "success": False,
                    "error": (
                        f"Required backend(s) failed: {', '.join(required_failed)}"
                    ),
                }

            if len(successful) < min_backends:
                return {
                    "success": False,
                    "error": (
                        f"Only {len(successful)}/{min_backends} backends succeeded "
                        f"(minimum {min_backends} required)"
                    ),
                }

            if not all_results:
                if errors:
                    return {"success": False, "error": "All backends failed: " + "; ".join(errors)}
                return {"success": True, "data": {"web": []}}

            # ── ranking ──
            rank_input_count = max(max_results + 5, _MAX_RANK_INPUT)
            rank_input = all_results[:rank_input_count]
            ranked = _rank_results(query, rank_input, ranker_config)

            # Top N
            output_count = min(limit, max_results)
            top = ranked[:output_count]
            for i, r in enumerate(top):
                r["position"] = i + 1

            logger.info(
                "Federated search: %d raw -> %d ranked (total %.1fs, %d/%d backends)",
                len(all_results), len(top), time.time() - start_time,
                len(successful), len(active_backends),
            )

            return {
                "success": True,
                "data": {
                    "web": [
                        {"title": r.get("title", ""), "url": r.get("url", ""),
                         "description": r.get("description", ""), "position": r.get("position", i + 1)}
                        for i, r in enumerate(top)
                    ],
                },
            }

        except Exception as exc:
            logger.error("Federated search error: %s", exc, exc_info=True)
            return {"success": False, "error": f"Federated search failed: {exc}"}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Federated Search",
            "badge": "advanced",
            "tag": (
                "Aggregate multiple search backends with LLM-based ranking. "
                "Configure under Settings → Plugins (or web.federated in "
                "config.yaml). Custom lines support multi-card credential "
                "pools (fill_first / round_robin / random)."
            ),
            "env_vars": [],
        }
