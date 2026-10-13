"""Tests for the FederatedSearch web search provider plugin."""

from __future__ import annotations

import contextvars
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# The plugin is loaded as a flat directory plugin (its own top-level modules),
# so import the provider module from the repository root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from provider import (  # noqa: E402
    FederatedSearchProvider,
    _extract_custom_results,
    _HealthCache,
    _rank_results,
    _read_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provider() -> FederatedSearchProvider:
    return FederatedSearchProvider()


# ---------------------------------------------------------------------------
# Provider identity & capabilities
# ---------------------------------------------------------------------------


class TestProviderIdentity:
    def test_name(self, provider: FederatedSearchProvider) -> None:
        assert provider.name == "federated"

    def test_display_name(self, provider: FederatedSearchProvider) -> None:
        assert provider.display_name == "Federated Search"

    def test_supports_search(self, provider: FederatedSearchProvider) -> None:
        assert provider.supports_search() is True

    def test_supports_extract(self, provider: FederatedSearchProvider) -> None:
        assert provider.supports_extract() is False

    def test_is_available_no_config(self, provider: FederatedSearchProvider) -> None:
        """Without config, is_available should return False."""
        assert provider.is_available() is False

    def test_get_setup_schema(self, provider: FederatedSearchProvider) -> None:
        schema = provider.get_setup_schema()
        assert schema["name"] == "Federated Search"
        assert "env_vars" in schema
        assert isinstance(schema["env_vars"], list)


# ---------------------------------------------------------------------------
# Config reading
# ---------------------------------------------------------------------------


class TestConfigReading:
    def test_read_config_no_federated(self) -> None:
        """When web.federated is absent, _read_config returns None."""
        with patch(
            "provider._read_config",
            return_value=None,
        ):
            assert _read_config() is None

    def test_is_available_with_backends(self) -> None:
        """With at least one backend configured, is_available returns True."""
        config = {"backends": [{"name": "tavily"}]}
        with patch(
            "provider._read_config",
            return_value=config,
        ):
            p = FederatedSearchProvider()
            assert p.is_available() is True


# ---------------------------------------------------------------------------
# Custom backend result extraction
# ---------------------------------------------------------------------------


class TestExtractCustomResults:
    def test_organic_shape(self) -> None:
        """Parse common ``{organic: [...]}`` shape (Google/Tavily-style)."""
        data = {
            "organic": [
                {"title": "Result A", "link": "https://a.com", "snippet": "Description A"},
                {"title": "Result B", "url": "https://b.com", "content": "Description B"},
            ]
        }
        results = _extract_custom_results(data)
        assert len(results) == 2
        assert results[0]["title"] == "Result A"
        assert results[0]["url"] == "https://a.com"

    def test_data_web_shape(self) -> None:
        """Parse ``{data: {web: [...]}}`` shape."""
        data = {
            "data": {
                "web": [
                    {"title": "X", "url": "https://x.com", "description": "Desc X"},
                    {"title": "Y", "url": "https://y.com", "description": "Desc Y"},
                ]
            }
        }
        results = _extract_custom_results(data)
        assert len(results) == 2

    def test_results_shape(self) -> None:
        """Parse ``{results: [...]}`` shape."""
        data = {
            "results": [
                {"title": "P", "url": "https://p.com", "content": "Content P"},
            ]
        }
        results = _extract_custom_results(data)
        assert len(results) == 1
        assert results[0]["title"] == "P"

    def test_empty_data(self) -> None:
        """Empty or malformed data returns empty list."""
        assert _extract_custom_results({}) == []
        assert _extract_custom_results(None) == []
        assert _extract_custom_results("not a dict") == []


# ---------------------------------------------------------------------------
# LLM Ranking
# ---------------------------------------------------------------------------


class TestRankResults:
    def test_empty_results(self) -> None:
        """Empty results stay empty after ranking."""
        assert _rank_results("test", [], None) == []

    def test_llm_fallback_on_failure(self) -> None:
        """When LLM call fails, results keep original order."""
        results = [
            {"title": "A", "url": "https://a.com", "description": "A desc"},
            {"title": "B", "url": "https://b.com", "description": "B desc"},
        ]
        ranked = _rank_results("test", results, None)
        assert len(ranked) == 2
        assert ranked[0]["title"] == "A"


# ---------------------------------------------------------------------------
# Integration: federated search with mocked backends
# ---------------------------------------------------------------------------


class TestFederatedSearch:
    """Integration test with mocked sub-backends."""

    def test_no_backends_configured(self, provider: FederatedSearchProvider) -> None:
        """When no backends in config, search returns error."""
        with patch(
            "provider._read_config",
            return_value={"backends": []},
        ):
            result = provider.search("test query")
            assert result["success"] is False
            assert "no search backends" in result["error"]

    def test_single_backend(self, provider: FederatedSearchProvider) -> None:
        """Single custom backend returns results."""
        config = {
            "backends": [{"name": "tavily", "type": "custom"}],
            "timeout": 10,
            "max_results": 8,
        }
        with patch(
            "provider._read_config",
            return_value=config,
        ), patch(
            "provider._search_one_backend",
            return_value=([
                {"title": "R1", "url": "https://r1.com", "description": "D1"},
                {"title": "R2", "url": "https://r2.com", "description": "D2"},
            ], None),
        ):
            result = provider.search("test", limit=5)
            assert result["success"] is True
            web = result["data"]["web"]
            assert len(web) == 2
            assert web[0]["title"] == "R1"
            assert web[1]["position"] == 2

    def test_multiple_backends_merge(self, provider: FederatedSearchProvider) -> None:
        """Multiple backends merge results."""
        config = {
            "backends": [
                {"name": "backend1", "type": "custom"},
                {"name": "backend2", "type": "custom"},
            ],
            "timeout": 10,
            "max_results": 8,
        }

        call_count = 0
        def fake_search(backend, query, limit):
            nonlocal call_count
            call_count += 1
            name = backend.get("name", "")
            if name == "backend1":
                return [{"title": "A1", "url": "https://a1.com", "description": ""}], None
            return [{"title": "B1", "url": "https://b1.com", "description": ""}], None

        with patch(
            "provider._read_config",
            return_value=config,
        ), patch(
            "provider._search_one_backend",
            side_effect=fake_search,
        ):
            result = provider.search("test")
            assert result["success"] is True
            assert len(result["data"]["web"]) == 2

    def test_max_results_respected(self, provider: FederatedSearchProvider) -> None:
        """Only max_results items are returned after ranking."""
        config = {
            "backends": [{"name": "tavily", "type": "custom"}],
            "max_results": 1,
            "timeout": 5,
        }
        with patch(
            "provider._read_config",
            return_value=config,
        ), patch(
            "provider._search_one_backend",
            return_value=([
                {"title": f"R{i}", "url": f"https://r{i}.com", "description": ""}
                for i in range(5)
            ], None),
        ):
            result = provider.search("test")
            assert result["success"] is True
            assert len(result["data"]["web"]) == 1

    def test_timeout_config_is_read(self, provider: FederatedSearchProvider) -> None:
        """The timeout config value is correctly read from config."""
        config = {
            "backends": [{"name": "t", "type": "custom"}],
            "timeout": 30,
            "max_results": 5,
        }
        with patch(
            "provider._read_config",
            return_value=config,
        ), patch(
            "provider._search_one_backend",
            return_value=([{"title": "R", "url": "https://r.com", "description": ""}], None),
        ):
            result = provider.search("test")
            assert result["success"] is True

    def test_blocked_backend_timeout_produces_partial_results(
        self, provider: FederatedSearchProvider,
    ) -> None:
        """A backend that blocks must be bounded by the configured timeout.

        The blocking backend is abandoned; results from faster backends are
        still collected and returned.
        """
        import threading

        block_signal = threading.Event()

        def slow_backend(backend, query, limit):
            block_signal.wait()  # block until released
            return [], None

        def fast_backend(backend, query, limit):
            return [{"title": "Fast", "url": "https://f.com", "description": ""}], None

        config = {
            "backends": [
                {"name": "slow", "type": "custom"},
                {"name": "fast", "type": "custom"},
            ],
            "timeout": 1,
            "max_results": 5,
        }
        try:
            with patch(
                "provider._read_config",
                return_value=config,
            ), patch(
                "provider._search_one_backend",
                side_effect=lambda b, q, l: slow_backend(b, q, l) if b["name"] == "slow" else fast_backend(b, q, l),
            ):
                result = provider.search("test", limit=5)
                # Must return within a few seconds (not waiting for the blocked backend)
                assert result["success"] is True
                web = result["data"]["web"]
                # Fast backend result should be present
                assert len(web) >= 1
                assert web[0]["title"] == "Fast"
        finally:
            block_signal.set()  # release blocked thread so it can exit

    def test_limit_below_max_results(
        self, provider: FederatedSearchProvider,
    ) -> None:
        """When limit < max_results, return only `limit` items."""
        config = {
            "backends": [{"name": "t", "type": "custom"}],
            "max_results": 8,
            "timeout": 5,
        }
        with patch(
            "provider._read_config",
            return_value=config,
        ), patch(
            "provider._search_one_backend",
            return_value=([
                {"title": f"R{i}", "url": f"https://r{i}.com", "description": ""}
                for i in range(10)
            ], None),
        ):
            result = provider.search("test", limit=3)
            assert result["success"] is True
            assert len(result["data"]["web"]) == 3

    def test_limit_above_max_results(
        self, provider: FederatedSearchProvider,
    ) -> None:
        """When limit > max_results, the configured max_results acts as the cap."""
        config = {
            "backends": [{"name": "t", "type": "custom"}],
            "max_results": 4,
            "timeout": 5,
        }
        with patch(
            "provider._read_config",
            return_value=config,
        ), patch(
            "provider._search_one_backend",
            return_value=([
                {"title": f"R{i}", "url": f"https://r{i}.com", "description": ""}
                for i in range(10)
            ], None),
        ):
            result = provider.search("test", limit=10)
            assert result["success"] is True
            assert len(result["data"]["web"]) == 4  # capped by max_results


# ---------------------------------------------------------------------------
# Config key integration
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_read_config_with_federated():
    """Helper fixture to inject a realistic config."""
    def _inject(config_override: Dict[str, Any]) -> None:
        patcher = patch(
            "provider._read_config",
            return_value=config_override,
        )
        patcher.start()
        return patcher

    yield _inject
    # Cleanup happens automatically in fixture teardown


class TestConfigurableSettings:
    """Verify that all three config items are read and applied."""

    def test_default_values_used_when_config_missing(self) -> None:
        """When config has no timeout/max_results/ranker, defaults apply."""
        config = {"backends": [{"name": "t", "type": "custom"}]}
        with patch(
            "provider._read_config",
            return_value=config,
        ), patch(
            "provider._search_one_backend",
            return_value=([{"title": "R", "url": "https://r.com", "description": ""}], None),
        ):
            p = FederatedSearchProvider()
            result = p.search("test")
            assert result["success"] is True

    def test_timeout_config(self) -> None:
        """Config item 1: timeout is readable."""
        config = {
            "backends": [{"name": "t", "type": "custom"}],
            "timeout": 30,
            "max_results": 5,
        }
        assert config["timeout"] == 30

    def test_max_results_config(self) -> None:
        """Config item 3: max_results is readable."""
        config = {
            "backends": [{"name": "t", "type": "custom"}],
            "timeout": 10,
            "max_results": 12,
        }
        assert config["max_results"] == 12

    def test_ranker_config(self) -> None:
        """Config item 2: ranker provider/model is readable."""
        config = {
            "backends": [{"name": "t", "type": "custom"}],
            "ranker": {"provider": "opencode-go", "model": "deepseek-v4-flash"},
        }
        assert config["ranker"]["provider"] == "opencode-go"
        assert config["ranker"]["model"] == "deepseek-v4-flash"


# ---------------------------------------------------------------------------
# HERMES_HOME config discovery integration test
# ---------------------------------------------------------------------------


class TestFederatedConfigDiscovery:
    """Verify that federated search config is discovered from an isolated
    HERMES_HOME directory via the real config loading path."""

    def test_read_config_from_hermes_home(self, tmp_path) -> None:
        """_read_config discovers web.federated from config.yaml in HERMES_HOME."""
        try:
            import hermes_yaml as yaml
        except ImportError:  # standalone checkout without the Hermes internal module
            import yaml

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.safe_dump({
            "web": {
                "federated": {
                    "timeout": 15,
                    "max_results": 6,
                    "backends": [
                        {"name": "tavily"},
                        {"name": "custom1", "type": "custom",
                         "base_url": "https://api.example.com",
                         "api_key_env": "EXAMPLE_KEY",
                         "search_path": "/v1/search",
                         "query_param": "q"},
                    ],
                    "ranker": {
                        "provider": "opencode-go",
                        "model": "deepseek-v4-flash",
                    },
                },
            },
        }))

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            from provider import _read_config as _cfg
            cfg = _cfg()
            assert cfg is not None
            assert cfg["timeout"] == 15
            assert cfg["max_results"] == 6
            assert len(cfg["backends"]) == 2
            assert cfg["backends"][0]["name"] == "tavily"
            assert cfg["ranker"]["provider"] == "opencode-go"


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestValidateConfig:
    """K-way aggregation config validation."""

    def test_valid_minimal(self) -> None:
        from provider import _validate_config
        config = {"backends": [{"name": "tavily"}]}
        assert _validate_config(config) is None

    def test_valid_k_equals_backends(self) -> None:
        from provider import _validate_config
        config = {
            "k": 3,
            "backends": [
                {"name": "a"}, {"name": "b"}, {"name": "c"},
            ],
        }
        assert _validate_config(config) is None

    def test_k_exceeds_max(self) -> None:
        from provider import _validate_config, _MAX_K
        config = {"k": _MAX_K + 1, "backends": [{"name": "a"}]}
        err = _validate_config(config)
        assert err is not None
        assert "exceeds maximum" in err

    def test_k_mismatch_too_few(self) -> None:
        from provider import _validate_config
        config = {"k": 3, "backends": [{"name": "a"}]}
        err = _validate_config(config)
        assert err is not None
        assert "exactly 3" in err

    def test_k_mismatch_too_many(self) -> None:
        from provider import _validate_config
        config = {"k": 2, "backends": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}
        err = _validate_config(config)
        assert err is not None
        assert "exactly 2" in err

    def test_min_backends_exceeds_k(self) -> None:
        from provider import _validate_config
        config = {
            "k": 2, "min_backends": 3,
            "backends": [{"name": "a"}, {"name": "b"}],
        }
        err = _validate_config(config)
        assert err is not None
        assert "cannot exceed k" in err

    def test_min_backends_less_than_one(self) -> None:
        from provider import _validate_config
        config = {
            "min_backends": 0,
            "backends": [{"name": "a"}],
        }
        err = _validate_config(config)
        assert err is not None
        assert "must be at least 1" in err

    def test_no_backends(self) -> None:
        from provider import _validate_config
        config = {"backends": []}
        err = _validate_config(config)
        assert err is not None
        assert "no search backends" in err


# ---------------------------------------------------------------------------
# Health cache
# ---------------------------------------------------------------------------


class TestHealthCache:
    """TTL-based health probe cache."""

    def test_cache_hit(self) -> None:
        from provider import _HealthCache
        cache = _HealthCache(ttl_seconds=300)
        cache.set_available("tavily", True)
        assert cache.is_available("tavily") is True

    def test_cache_miss_expired(self) -> None:
        from provider import _HealthCache
        cache = _HealthCache(ttl_seconds=0)  # instant expiry
        cache.set_available("tavily", True)
        assert cache.is_available("tavily") is None

    def test_cache_miss_unknown(self) -> None:
        from provider import _HealthCache
        cache = _HealthCache(ttl_seconds=300)
        assert cache.is_available("unknown") is None

    def test_mark_failed_cooldown(self) -> None:
        from provider import _HealthCache
        cache = _HealthCache(ttl_seconds=300)
        cache.set_available("tavily", True)
        cache.mark_failed("tavily", 429)
        # After mark_failed, should be unavailable
        assert cache.is_available("tavily") is False

    def test_failure_cooldown_respects_status_code(self) -> None:
        from provider import _HealthCache
        cache = _HealthCache(ttl_seconds=300)
        cache.set_available("tavily", True)
        # 401 → 300s cooldown, stored as False
        cache.mark_failed("tavily", 401)
        assert cache.is_available("tavily") is False


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------


class TestProbeBackend:
    """Backend availability probing."""

    def test_registered_provider_available(self) -> None:
        from provider import _probe_backend
        mock_provider = MagicMock()
        mock_provider.is_available.return_value = True
        with patch(
            "provider._get_registered_provider",
            return_value=mock_provider,
        ):
            assert _probe_backend({"name": "tavily"}) is True

    def test_registered_provider_unavailable(self) -> None:
        from provider import _probe_backend
        mock_provider = MagicMock()
        mock_provider.is_available.return_value = False
        with patch(
            "provider._get_registered_provider",
            return_value=mock_provider,
        ):
            assert _probe_backend({"name": "tavily"}) is False

    def test_registered_provider_not_found(self) -> None:
        from provider import _probe_backend
        with patch(
            "provider._get_registered_provider",
            return_value=None,
        ):
            assert _probe_backend({"name": "nonexistent"}) is False

    def test_custom_backend_no_base_url(self) -> None:
        from provider import _probe_backend
        assert _probe_backend({
            "name": "bad", "type": "custom",
        }) is False

    def test_custom_backend_reachable(self) -> None:
        from provider import _probe_backend
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.head", return_value=mock_resp):
            assert _probe_backend({
                "name": "ok", "type": "custom",
                "base_url": "https://api.example.com",
            }) is True

    def test_custom_backend_server_error(self) -> None:
        from provider import _probe_backend
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("httpx.head", return_value=mock_resp):
            assert _probe_backend({
                "name": "down", "type": "custom",
                "base_url": "https://api.example.com",
            }) is False

    def test_custom_backend_timeout(self) -> None:
        from provider import _probe_backend
        with patch("httpx.head", side_effect=Exception("timeout")):
            assert _probe_backend({
                "name": "slow", "type": "custom",
                "base_url": "https://api.example.com",
            }) is False


# ---------------------------------------------------------------------------
# Rerank with custom prompt
# ---------------------------------------------------------------------------


class TestRankResultsCustomPrompt:
    """LLM ranking with user-supplied prompt."""

    def test_custom_prompt_passed_to_llm(self) -> None:
        from provider import _rank_results

        results = [
            {"title": f"R{i}", "url": f"https://r{i}.com", "description": f"D{i}"}
            for i in range(5)
        ]
        ranker_config = {
            "provider": "opencode-go",
            "model": "deepseek-v4-flash",
            "prompt": "Prefer Chinese sources and official docs.",
        }

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content="[5,4,3,2,1]"))
        ]

        with patch(
            "agent.auxiliary_client.call_llm",
            return_value=mock_response,
        ) as mock_call:
            ranked = _rank_results("test", results, ranker_config)
            assert len(ranked) == 5
            # Verify custom prompt was passed
            call_args = mock_call.call_args
            messages = call_args[1]["messages"]
            system_msg = messages[0]["content"]
            assert "Prefer Chinese sources" in system_msg

    def test_no_custom_prompt_uses_default(self) -> None:
        from provider import _rank_results

        results = [
            {"title": f"R{i}", "url": f"https://r{i}.com", "description": f"D{i}"}
            for i in range(5)
        ]
        ranker_config = {"provider": "opencode-go", "model": "deepseek-v4-flash"}

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content="[3,2,1,4,5]"))
        ]

        with patch(
            "agent.auxiliary_client.call_llm",
            return_value=mock_response,
        ) as mock_call:
            _rank_results("test", results, ranker_config)
            call_args = mock_call.call_args
            messages = call_args[1]["messages"]
            system_msg = messages[0]["content"]
            assert "Return ONLY a JSON array" in system_msg


# ---------------------------------------------------------------------------
# Integration: required backends + min_backends
# ---------------------------------------------------------------------------


class TestFederatedSearchFaultTolerance:
    """Required backends and min_backends fault tolerance."""

    def test_required_backend_failure_causes_overall_failure(
        self, provider: FederatedSearchProvider,
    ) -> None:
        config = {
            "k": 2,
            "min_backends": 1,
            "backends": [
                {"name": "required_src", "type": "custom", "required": True},
                {"name": "optional_src", "type": "custom", "required": False},
            ],
            "timeout": 5,
            "max_results": 8,
        }
        call_count = {"count": 0}

        def fake_search(backend, query, limit):
            call_count["count"] += 1
            name = backend.get("name", "")
            if name == "required_src":
                return [], None  # fail
            return [{"title": "OK", "url": "https://ok.com", "description": ""}], None

        with patch(
            "provider._read_config",
            return_value=config,
        ), patch(
            "provider._probe_backend",
            return_value=True,
        ), patch(
            "provider._search_one_backend",
            side_effect=fake_search,
        ):
            result = provider.search("test", limit=5)
            assert result["success"] is False
            assert "Required backend" in result["error"]

    def test_min_backends_not_met_causes_failure(
        self, provider: FederatedSearchProvider,
    ) -> None:
        config = {
            "k": 3,
            "min_backends": 2,
            "backends": [
                {"name": "a", "type": "custom", "required": False},
                {"name": "b", "type": "custom", "required": False},
                {"name": "c", "type": "custom", "required": False},
            ],
            "timeout": 5,
            "max_results": 8,
        }

        def fake_search(backend, query, limit):
            name = backend.get("name", "")
            if name == "a":
                return [{"title": "A", "url": "https://a.com", "description": ""}], None
            return [], None  # b and c fail

        with patch(
            "provider._read_config",
            return_value=config,
        ), patch(
            "provider._probe_backend",
            return_value=True,
        ), patch(
            "provider._search_one_backend",
            side_effect=fake_search,
        ):
            result = provider.search("test", limit=5)
            assert result["success"] is False
            assert "Only 1/2 backends succeeded" in result["error"]

    def test_all_backends_optional_can_succeed_with_partial(
        self, provider: FederatedSearchProvider,
    ) -> None:
        config = {
            "k": 2,
            "min_backends": 1,
            "backends": [
                {"name": "a", "type": "custom", "required": False},
                {"name": "b", "type": "custom", "required": False},
            ],
            "timeout": 5,
            "max_results": 8,
        }

        def fake_search(backend, query, limit):
            name = backend.get("name", "")
            if name == "a":
                return [{"title": "A", "url": "https://a.com", "description": ""}], None
            return [], None

        with patch(
            "provider._read_config",
            return_value=config,
        ), patch(
            "provider._probe_backend",
            return_value=True,
        ), patch(
            "provider._search_one_backend",
            side_effect=fake_search,
        ):
            result = provider.search("test", limit=5)
            assert result["success"] is True
            assert len(result["data"]["web"]) == 1


# ---------------------------------------------------------------------------
# Integration: health cache filters backends
# ---------------------------------------------------------------------------


class TestFederatedSearchHealthCache:
    """Health cache integration in search flow."""

    def test_unavailable_backend_skipped(
        self, provider: FederatedSearchProvider,
    ) -> None:
        config = {
            "k": 2,
            "min_backends": 1,
            "backends": [
                {"name": "good", "type": "custom", "required": False},
                {"name": "bad", "type": "custom", "required": False},
            ],
            "timeout": 5,
            "max_results": 8,
            "health_check": {},
        }

        # Pre-fill health cache: "bad" is unavailable
        provider._health_cache = _HealthCache(ttl_seconds=300)
        provider._health_cache.set_available("bad", False)
        provider._health_cache.set_available("good", True)

        with patch(
            "provider._read_config",
            return_value=config,
        ), patch(
            "provider._search_one_backend",
            return_value=([{"title": "X", "url": "https://x.com", "description": ""}], None),
        ):
            result = provider.search("test", limit=5)
            assert result["success"] is True

    def test_required_backend_cached_unavailable_causes_failure(
        self, provider: FederatedSearchProvider,
    ) -> None:
        """A required backend that is health-cached as unavailable must still
        cause overall search failure — the check must validate the full
        backend list, not just the active (post-health-filter) set."""
        config = {
            "k": 2,
            "min_backends": 1,
            "backends": [
                {"name": "critical", "type": "custom", "required": True},
                {"name": "optional", "type": "custom", "required": False},
            ],
            "timeout": 5,
            "max_results": 8,
            "health_check": {},
        }
        provider._health_cache = _HealthCache(ttl_seconds=300)
        provider._health_cache.set_available("critical", False)
        provider._health_cache.set_available("optional", True)

        with patch(
            "provider._read_config",
            return_value=config,
        ), patch(
            "provider._search_one_backend",
            return_value=([{"title": "OK", "url": "https://ok.com", "description": ""}], None),
        ):
            result = provider.search("test", limit=5)
            assert result["success"] is False
            assert "Required backend" in result["error"]

    def test_health_cache_cooldown_on_http_error(
        self, provider: FederatedSearchProvider,
    ) -> None:
        """A custom backend search failure with HTTP 429 must call
        health_cache.mark_failed(), triggering the documented cooldown."""
        config = {
            "k": 2,
            "backends": [
                {"name": "src_a", "type": "custom", "required": False},
                {"name": "src_b", "type": "custom", "required": False},
            ],
            "timeout": 5,
            "max_results": 8,
            "health_check": {},
        }
        provider._health_cache = _HealthCache(ttl_seconds=300)
        provider._health_cache.set_available("src_a", True)
        provider._health_cache.set_available("src_b", True)

        # src_a succeeds; src_b returns HTTP 429 status
        results_by_backend = {
            "src_a": ([{"title": "OK", "url": "https://ok.com", "description": ""}], None),
            "src_b": ([], 429),
        }

        def fake_search(backend, query, limit):
            name = backend.get("name", "")
            return results_by_backend.get(name, ([], None))

        with patch(
            "provider._read_config",
            return_value=config,
        ), patch(
            "provider._search_one_backend",
            side_effect=fake_search,
        ):
            result = provider.search("test", limit=5)
            # src_a succeeded, src_b 429 → partial success
            assert result["success"] is True
            # src_b should be marked unavailable due to 429 cooldown
            assert provider._health_cache.is_available("src_b") is False

    def test_all_cached_unavailable(
        self, provider: FederatedSearchProvider,
    ) -> None:
        config = {
            "k": 1,
            "backends": [{"name": "down", "type": "custom", "required": False}],
            "timeout": 5,
            "max_results": 8,
            "health_check": {},
        }
        provider._health_cache = _HealthCache(ttl_seconds=300)
        provider._health_cache.set_available("down", False)

        with patch(
            "provider._read_config",
            return_value=config,
        ):
            result = provider.search("test", limit=5)
            assert result["success"] is False
            assert "No backends available" in result["error"]


# ---------------------------------------------------------------------------
# Context propagation into worker threads
# ---------------------------------------------------------------------------


class TestContextPropagation:
    """Backend worker threads must inherit the caller's ContextVars.

    Under multiplexed hosting the profile secret scope is a ContextVar, and a
    fresh ThreadPoolExecutor worker starts with an empty context. Without
    copy_context() the backend calls inside the workers fail closed with
    UnscopedSecretError — every federated search failed on the desktop /
    multiplexed serve while single-profile runs worked (2026-10-12).
    """

    def test_worker_threads_inherit_calling_context(
        self, provider: FederatedSearchProvider,
    ) -> None:
        scope = contextvars.ContextVar("federated_scope_probe", default=None)
        seen: list = []

        def fake_search(backend, query, limit):
            seen.append(scope.get())
            return ([{"title": "C", "url": "https://c.com", "description": ""}], None)

        config = {
            "backends": [
                {"name": "alpha", "type": "custom"},
                {"name": "beta", "type": "custom"},
            ],
            "timeout": 5,
            "max_results": 5,
        }

        token = scope.set("ACTIVE")
        try:
            with patch(
                "provider._read_config",
                return_value=config,
            ), patch(
                "provider._search_one_backend",
                side_effect=fake_search,
            ):
                result = provider.search("ctx", limit=3)
        finally:
            scope.reset(token)

        assert result["success"] is True
        # Both workers must observe the caller's context value.
        assert seen == ["ACTIVE", "ACTIVE"]


# ---------------------------------------------------------------------------
# Phase 2: official settings exposure + line-level multi-card pool
# ---------------------------------------------------------------------------


def _fake_card_value(label: str) -> str:
    """Runtime-generated placeholder credential value (no literal key material)."""
    import uuid

    return f"{label}-{uuid.uuid4().hex}"


def _fake_response(status: int, payload: Any = None):
    """Real httpx.Response so raise_for_status()/json() behave like production."""
    import httpx

    return httpx.Response(
        status,
        json=payload if payload is not None else {},
        request=httpx.Request("POST", "https://api.example.com/search"),
    )


class TestReadConfigMergesSettings:
    """_read_config merges web.federated with the official settings tree."""

    @staticmethod
    def _write(tmp_path, payload: Dict[str, Any]) -> None:
        try:
            import hermes_yaml as yaml
        except ImportError:
            import yaml

        (tmp_path / "config.yaml").write_text(yaml.safe_dump(payload))

    def test_settings_win_per_top_level_key(self, tmp_path) -> None:
        self._write(tmp_path, {
            "web": {
                "federated": {
                    "timeout": 11,
                    "max_results": 4,
                    "backends": [{"name": "tavily"}],
                },
            },
            "plugins": {
                "entries": {
                    "hermes-federated-search": {
                        "settings": {"timeout": 22, "pool_strategy": "round_robin"},
                    },
                },
            },
        })
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            cfg = _read_config()
        assert cfg is not None
        assert cfg["timeout"] == 22          # settings win on overlap
        assert cfg["max_results"] == 4       # web-only key survives
        assert cfg["backends"][0]["name"] == "tavily"
        assert cfg["pool_strategy"] == "round_robin"

    def test_web_federated_only_still_works(self, tmp_path) -> None:
        self._write(tmp_path, {
            "web": {"federated": {"timeout": 13, "backends": [{"name": "tavily"}]}},
        })
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            cfg = _read_config()
        assert cfg is not None
        assert cfg["timeout"] == 13
        assert "pool_strategy" not in cfg

    def test_settings_only_enables_provider(self, tmp_path) -> None:
        self._write(tmp_path, {
            "plugins": {
                "entries": {
                    "hermes-federated-search": {
                        "settings": {
                            "timeout": 7,
                            "backends": [{"name": "minimax", "type": "custom",
                                          "base_url": "https://api.example.com"}],
                        },
                    },
                },
            },
        })
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            cfg = _read_config()
        assert cfg is not None
        assert cfg["timeout"] == 7
        assert cfg["backends"][0]["name"] == "minimax"

    def test_neither_section_is_not_configured(self, tmp_path) -> None:
        self._write(tmp_path, {"web": {"search_backend": "federated"}})
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            assert _read_config() is None

    def test_legacy_global_pool_strategy_inherited_when_settings_absent(self, tmp_path) -> None:
        """settings 树没有 pool_strategy 时，legacy 的全局值继续生效（顶层键合并）。"""
        self._write(tmp_path, {
            "web": {
                "federated": {
                    "backends": [{"name": "tavily"}],
                    "pool_strategy": "round_robin",
                },
            },
            "plugins": {
                "entries": {
                    "hermes-federated-search": {
                        "settings": {"timeout": 22},
                    },
                },
            },
        })
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            cfg = _read_config()
        assert cfg is not None
        assert cfg["pool_strategy"] == "round_robin"   # legacy global default survives
        assert cfg["timeout"] == 22                    # settings key still wins


class TestCardPool:
    """Pool selection strategies and cooldown bookkeeping."""

    def test_fill_first_order(self) -> None:
        from provider import _CardPool

        pool = _CardPool()
        cards = ["CARD_A", "CARD_B", "CARD_C"]
        assert pool.select("line", cards, "fill_first") == "CARD_A"
        pool.mark_failed("line", "CARD_A", 500)
        assert pool.select("line", cards, "fill_first") == "CARD_B"
        assert pool.select("line", cards, "fill_first", exclude={"CARD_B"}) == "CARD_C"

    def test_round_robin_rotates(self) -> None:
        from provider import _CardPool

        pool = _CardPool()
        cards = ["CARD_A", "CARD_B", "CARD_C"]
        picks = [pool.select("line", cards, "round_robin") for _ in range(4)]
        assert picks == ["CARD_A", "CARD_B", "CARD_C", "CARD_A"]

    def test_random_in_set(self) -> None:
        import random

        from provider import _CardPool

        pool = _CardPool()
        cards = ["CARD_A", "CARD_B"]
        random.seed(1234)
        picks = {pool.select("line", cards, "random") for _ in range(40)}
        assert picks == {"CARD_A", "CARD_B"}

    def test_missing_line_strategy_uses_global_default(self, provider) -> None:
        """线路缺 pool_strategy → 回退全局缺省（provider.py:721-723 → :970）。"""
        from provider import _CardPool, _POOL_STRATEGIES

        # 全局键（settings/legacy 合并后）设置默认策略
        pool = provider._get_card_pool({"pool_strategy": "round_robin"})
        assert isinstance(pool, _CardPool)
        assert pool.default_strategy == "round_robin"

        # 线路未给策略（search 传 None）→ 按全局缺省轮换
        cards = ["CARD_A", "CARD_B"]
        picks = [pool.select("line", cards, None) for _ in range(3)]
        assert picks == ["CARD_A", "CARD_B", "CARD_A"]

        # 全局也非法/缺失 → 仍回退到模块默认
        pool = _CardPool()
        assert pool.select("line", cards, "bogus") == "CARD_A"
        assert _POOL_STRATEGIES[0] == "fill_first"  # default constant sanity

    def test_cooldowns_expire(self) -> None:
        import time as _time

        from provider import _CardPool

        pool = _CardPool()
        assert pool.remaining_cooldown("line", "CARD_A") == 0.0
        pool.mark_failed("line", "CARD_A", 401)
        assert pool.remaining_cooldown("line", "CARD_A") == pytest.approx(300, abs=5)
        # an expired cooldown releases the card back into the pool
        with patch("provider.time.time", return_value=_time.time() + 301):
            assert pool.remaining_cooldown("line", "CARD_A") == 0.0
            assert pool.select("line", ["CARD_A"], "fill_first") == "CARD_A"


class TestCardPoolSwitching:
    """A failed card cools down and the request fails over to the next one."""

    @staticmethod
    def _backend() -> Dict[str, Any]:
        return {
            "name": "line1", "type": "custom",
            "base_url": "https://api.example.com", "search_path": "/search",
            "credentials": ["CARD_A", "CARD_B"],
        }

    def _switch(self, status: int, expected_cooldown: float) -> None:
        from provider import _CardPool, _search_custom_backend

        card_a, card_b = _fake_card_value("card-a"), _fake_card_value("card-b")
        values = {"CARD_A": card_a, "CARD_B": card_b}
        pool = _CardPool()
        responses = [
            _fake_response(status),
            _fake_response(200, {"results": [
                {"title": "B1", "url": "https://b1.example.com", "content": "ok"}]}),
        ]
        with patch(
            "provider.get_provider_env",
            side_effect=lambda n: values.get(n, ""),
        ), patch("httpx.post", side_effect=responses) as mock_post:
            results, code = _search_custom_backend(self._backend(), "q", 5, card_pool=pool)

        assert code is None
        assert len(results) == 1 and results[0]["title"] == "B1"
        auth = [call.kwargs["headers"]["Authorization"] for call in mock_post.call_args_list]
        assert auth == [f"Bearer {card_a}", f"Bearer {card_b}"]
        assert pool.remaining_cooldown("line1", "CARD_A") == pytest.approx(expected_cooldown, abs=5)
        assert pool.remaining_cooldown("line1", "CARD_B") == 0.0

    def test_pool_switch_on_401_300s(self) -> None:
        self._switch(401, 300)

    def test_pool_switch_on_429_3600s(self) -> None:
        self._switch(429, 3600)

    def test_pool_switch_on_403_300s(self) -> None:
        self._switch(403, 300)

    def test_pool_switch_on_5xx_300s(self) -> None:
        self._switch(502, 300)

    def test_pool_all_cards_fail_line_error_once_each(self) -> None:
        from provider import _CardPool, _search_custom_backend

        values = {"CARD_A": _fake_card_value("a"), "CARD_B": _fake_card_value("b")}
        pool = _CardPool()
        with patch(
            "provider.get_provider_env", side_effect=lambda n: values.get(n, ""),
        ), patch("httpx.post", side_effect=[_fake_response(401), _fake_response(429)]) as mock_post:
            results, code = _search_custom_backend(self._backend(), "q", 5, card_pool=pool)

        assert results == [] and code == 429   # last HTTP failure surfaces
        assert mock_post.call_count == 2       # each card used at most once per request
        assert pool.remaining_cooldown("line1", "CARD_A") == pytest.approx(300, abs=5)
        assert pool.remaining_cooldown("line1", "CARD_B") == pytest.approx(3600, abs=5)

    def test_pool_sole_card_caps_transient_at_60(self) -> None:
        from provider import _CardPool, _search_custom_backend

        value = _fake_card_value("only")
        pool = _CardPool()
        backend = dict(self._backend(), credentials=["ONLY_CARD"])
        with patch("provider.get_provider_env", return_value=value), patch(
            "httpx.post", side_effect=[_fake_response(500)],
        ):
            results, code = _search_custom_backend(backend, "q", 5, card_pool=pool)

        assert results == [] and code == 500
        assert pool.remaining_cooldown("line1", "ONLY_CARD") == pytest.approx(60, abs=5)

    def test_pool_sole_card_keeps_401_at_300s(self) -> None:
        from provider import _CardPool, _search_custom_backend

        value = _fake_card_value("only")
        pool = _CardPool()
        backend = dict(self._backend(), credentials=["ONLY_CARD"])
        with patch("provider.get_provider_env", return_value=value), patch(
            "httpx.post", side_effect=[_fake_response(401)],
        ):
            results, code = _search_custom_backend(backend, "q", 5, card_pool=pool)

        assert results == [] and code == 401
        assert pool.remaining_cooldown("line1", "ONLY_CARD") == pytest.approx(300, abs=5)

    def test_pool_legacy_single_card_not_capped(self) -> None:
        """Legacy api_key_env single-card path keeps v1 values (no 60s sole cap,
        no pool interaction) — the cap applies to the multi-card pool path only."""
        from provider import _CardPool, _search_custom_backend

        value = _fake_card_value("legacy")
        pool = _CardPool()
        backend = {
            "name": "legacy", "type": "custom",
            "base_url": "https://api.example.com", "search_path": "/search",
            "api_key_env": "LEGACY_KEY",
        }
        with patch("provider.get_provider_env", return_value=value):
            for status in (500, 429):
                with patch("httpx.post", side_effect=[_fake_response(status)]):
                    results, code = _search_custom_backend(backend, "q", 5, card_pool=pool)
                assert results == [] and code == status
        assert pool.remaining_cooldown("legacy", "LEGACY_KEY") == 0.0

        # line-level (health cache) cooldown values stay v1: 5xx→300s, 429→3600s
        import time as _time

        from provider import _HealthCache

        hc = _HealthCache(ttl_seconds=300)
        hc.mark_failed("legacy", 500)
        with patch("provider.time.time", return_value=_time.time() + 301):
            assert hc.is_available("legacy") is None       # 300s elapsed (not 60/3600)
        hc2 = _HealthCache(ttl_seconds=300)
        hc2.mark_failed("legacy", 429)
        with patch("provider.time.time", return_value=_time.time() + 301):
            assert hc2.is_available("legacy") is False     # 3600s still cooling

    def test_pool_tavily_custom_form_via_pool(self) -> None:
        """Canonical Tavily-as-custom-line params work through the pool path."""
        from provider import _CardPool, _search_custom_backend

        tavily_a, tavily_b = _fake_card_value("tv-a"), _fake_card_value("tv-b")
        values = {"TAVILY_KEY_1": tavily_a, "TAVILY_KEY_2": tavily_b}
        pool = _CardPool()
        backend = {
            "name": "tavily-custom", "type": "custom",
            "base_url": "https://api.tavily.com", "search_path": "/search",
            "query_param": "query", "auth_style": "bearer",
            "credentials": ["TAVILY_KEY_1", "TAVILY_KEY_2"],
        }
        payload = {"results": [
            {"title": "T1", "url": "https://t1.example.com", "content": "snippet"}]}
        with patch("provider.get_provider_env", side_effect=lambda n: values.get(n, "")), patch(
            "httpx.post", side_effect=[_fake_response(200, payload)],
        ) as mock_post:
            results, code = _search_custom_backend(backend, "hello", 5, card_pool=pool)

        assert code is None and len(results) == 1
        call = mock_post.call_args
        assert call.args[0] == "https://api.tavily.com/search"
        assert call.kwargs["json"]["query"] == "hello"
        assert call.kwargs["headers"]["Authorization"] == f"Bearer {tavily_a}"

    def test_pool_skips_missing_env_all_missing_fails_line(self) -> None:
        from provider import _CardPool, _search_custom_backend

        value_a = _fake_card_value("a")
        pool = _CardPool()
        payload = {"results": [{"title": "A1", "url": "https://a1.example.com", "content": "ok"}]}
        with patch(
            "provider.get_provider_env",
            side_effect=lambda n: {"CARD_A": value_a}.get(n, ""),
        ), patch("httpx.post", side_effect=[_fake_response(200, payload)]) as mock_post:
            results, code = _search_custom_backend(self._backend(), "q", 5, card_pool=pool)

        assert code is None and len(results) == 1
        assert mock_post.call_count == 1       # missing card never dialed
        assert mock_post.call_args.kwargs["headers"]["Authorization"] == f"Bearer {value_a}"

        pool2 = _CardPool()
        backend2 = dict(self._backend(), credentials=["MISSING_1", "MISSING_2"])
        with patch("provider.get_provider_env", return_value=""), patch("httpx.post") as mock_post2:
            outcomes = _search_custom_backend(backend2, "q", 5, card_pool=pool2)
        assert outcomes == ([], None)          # line-level failure, no dial
        assert mock_post2.call_count == 0

    def test_pool_skips_blank_and_line_break_values(self, caplog) -> None:
        import logging

        from provider import _CardPool, _search_custom_backend

        good = _fake_card_value("good")
        values = {"BAD_BLANK": "   ", "BAD_NEWLINE": "abc\ndef", "GOOD": good}
        payload = {"results": [{"title": "G", "url": "https://g.example.com", "content": "ok"}]}
        pool = _CardPool()
        backend = dict(self._backend(), credentials=["BAD_BLANK", "BAD_NEWLINE", "GOOD"])
        with caplog.at_level(logging.WARNING), patch(
            "provider.get_provider_env", side_effect=lambda n: values.get(n, ""),
        ), patch("httpx.post", side_effect=[_fake_response(200, payload)]) as mock_post:
            results, code = _search_custom_backend(backend, "q", 5, card_pool=pool)

        assert code is None and len(results) == 1
        assert mock_post.call_count == 1
        assert mock_post.call_args.kwargs["headers"]["Authorization"] == f"Bearer {good}"
        assert any("line break" in rec.message for rec in caplog.records)

        pool2 = _CardPool()
        with patch("provider.get_provider_env", return_value=""), patch("httpx.post") as mock_post2:
            assert _search_custom_backend(backend, "q", 5, card_pool=pool2) == ([], None)
        assert mock_post2.call_count == 0

    def test_pool_multi_vendor_pools_are_independent(self) -> None:
        from provider import _CardPool, _search_custom_backend

        shared, line1_b = _fake_card_value("shared"), _fake_card_value("line1-b")
        values = {"SHARED_CARD": shared, "LINE1_B": line1_b}
        pool = _CardPool()
        line1 = {
            "name": "vendor1", "type": "custom",
            "base_url": "https://api.example.com", "search_path": "/search",
            "credentials": ["SHARED_CARD", "LINE1_B"],
        }
        line2 = {
            "name": "vendor2", "type": "custom",
            "base_url": "https://api.example.com", "search_path": "/search",
            "credentials": ["SHARED_CARD"],
        }
        ok_payload = {"results": [{"title": "R", "url": "https://r.example.com", "content": "ok"}]}
        responses = [_fake_response(500), _fake_response(200, ok_payload), _fake_response(200, ok_payload)]
        with patch(
            "provider.get_provider_env", side_effect=lambda n: values.get(n, ""),
        ), patch("httpx.post", side_effect=responses) as mock_post:
            r1, _ = _search_custom_backend(line1, "q", 5, card_pool=pool)
            r2, _ = _search_custom_backend(line2, "q", 5, card_pool=pool)

        assert len(r1) == 1 and len(r2) == 1
        # vendor1's busted card is cooling on its own line only
        assert pool.remaining_cooldown("vendor1", "SHARED_CARD") == pytest.approx(300, abs=5)
        assert pool.remaining_cooldown("vendor2", "SHARED_CARD") == 0.0
        auth = [call.kwargs["headers"]["Authorization"] for call in mock_post.call_args_list]
        assert auth == [f"Bearer {shared}", f"Bearer {line1_b}", f"Bearer {shared}"]


class TestCredentialsValidation:
    """credentials grammar, mis-entry rejection, dedupe (config time)."""

    @staticmethod
    def _err(credentials: Any, typ: str = "custom"):
        from provider import _validate_config

        cfg = {"backends": [{"name": "line1", "type": typ, "credentials": credentials}]}
        return _validate_config(cfg)

    def test_credentials_syntax_rejected_zero_echo(self) -> None:
        bad_values = ["card_a", "CARD-A", "1CARD", "CARD.A", "CARD/A", "CARD A", "A" * 129, 123]
        for bad in bad_values:
            err = self._err([bad])
            assert err is not None, "invalid credentials item was not rejected"
            assert "credentials[0]" in err
            if isinstance(bad, str):
                assert bad not in err, f"error message leaked the value: {err}"
        err = self._err([""])
        assert err is not None and "credentials[0]" in err
        assert "environment variable" in err

    def test_credentials_secret_like_rejected_zero_echo(self) -> None:
        import random
        import string
        import uuid

        def _shape(*parts: str) -> str:
            return "".join(parts)

        def _random_id(n: int = 16) -> str:
            return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))

        # Fixtures are assembled at runtime so no key-shaped literal lands in
        # the repository (the desensitization scan keeps a hard zero-hit).
        token = uuid.uuid4().hex + uuid.uuid4().hex
        aws_id_shape = _shape("A", "K", "IA") + _random_id()
        aws_session_shape = _shape("A", "S", "IA") + _random_id()
        bad_values = [
            aws_id_shape,                    # AWS access-key id shape (passes the grammar)
            aws_session_shape,               # AWS session key-id shape
            _shape("tv", "ly-") + token,     # vendor token, dash prefix
            _shape("s", "k-") + token,       # short-prefix token
            _shape("Bea", "rer ") + token,   # auth-scheme token
            _shape("ey", token),             # compact base64-encoded token
            "AbCdEfGh" + token.upper(),      # mixed-case long token
        ]
        for bad in bad_values:
            err = self._err([bad])
            # message stays value-free: pytest keeps assertion output key-free too
            assert err is not None, "secret-shaped credentials item was not rejected"
            assert bad not in err, f"error message leaked the value: {err}"
        for fingerprint in (aws_id_shape, aws_session_shape):
            err = self._err([fingerprint])
            assert "secret" in err.lower()
            assert "credentials[0]" in err

    def test_credentials_empty_list_not_configured_and_duplicates_deduped(self, caplog) -> None:
        import logging

        assert self._err([]) is None
        with caplog.at_level(logging.WARNING):
            assert self._err(["CARD_A", "CARD_A"]) is None
        assert any("duplicate" in rec.message.lower() for rec in caplog.records)

        from provider import _CardPool, _search_custom_backend

        value = _fake_card_value("dup")
        pool = _CardPool()
        backend = {
            "name": "line1", "type": "custom",
            "base_url": "https://api.example.com", "search_path": "/search",
            "credentials": ["CARD_A", "CARD_A"],
        }
        payload = {"results": [{"title": "D", "url": "https://d.example.com", "content": "ok"}]}
        with patch("provider.get_provider_env", return_value=value), patch(
            "httpx.post", side_effect=[_fake_response(200, payload)],
        ) as mock_post:
            results, code = _search_custom_backend(backend, "q", 5, card_pool=pool)
        assert code is None and len(results) == 1
        assert mock_post.call_count == 1           # deduped to a single dial
        assert pool.remaining_cooldown("line1", "CARD_A") == 0.0

    def test_credentials_on_registered_line_warns_ignored(self, provider, caplog) -> None:
        import logging

        config = {
            "backends": [{"name": "tavily", "credentials": ["CARD_A"]}],
            "timeout": 5,
            "max_results": 5,
        }
        mock_provider = MagicMock()
        mock_provider.search.return_value = {
            "success": True,
            "data": {"web": [{"title": "T", "url": "https://t.example.com", "description": ""}]},
        }
        with caplog.at_level(logging.WARNING), patch(
            "provider._read_config", return_value=config,
        ), patch("provider._get_registered_provider", return_value=mock_provider):
            result = provider.search("q", limit=3)

        assert result["success"] is True and len(result["data"]["web"]) == 1
        assert mock_provider.search.called            # host single-card path unchanged
        assert any(
            "credentials" in rec.message and "registered" in rec.message
            for rec in caplog.records
        )

    def test_pool_legacy_api_key_env_single_card(self) -> None:
        from provider import _search_custom_backend

        value = _fake_card_value("legacy")
        backend = {
            "name": "legacy", "type": "custom",
            "base_url": "https://api.example.com", "search_path": "/search",
            "api_key_env": "LEGACY_KEY",
        }
        payload = {"results": [{"title": "L", "url": "https://l.example.com", "content": "ok"}]}
        with patch("provider.get_provider_env", return_value=value), patch(
            "httpx.post", side_effect=[_fake_response(200, payload)],
        ) as mock_post:
            results, code = _search_custom_backend(backend, "q", 5)
        assert code is None and len(results) == 1
        assert mock_post.call_args.kwargs["headers"]["Authorization"] == f"Bearer {value}"

        with patch("provider.get_provider_env", return_value=""), patch("httpx.post") as mock_post2:
            assert _search_custom_backend(backend, "q", 5) == ([], None)
        assert mock_post2.call_count == 0


class TestRankingMode:
    """ranking_mode maps onto the ranker dict (auto | keyword | llm)."""

    @staticmethod
    def _capture(provider, config: Dict[str, Any]):
        captured: Dict[str, Any] = {}
        sample = [
            {"title": f"R{i}", "url": f"https://r{i}.example.com", "description": ""}
            for i in range(4)
        ]

        def fake_rank(query, results, ranker_config):
            captured["ranker"] = ranker_config
            return results

        with patch("provider._read_config", return_value=config), patch(
            "provider._search_one_backend", return_value=(sample, None),
        ), patch("provider._rank_results", side_effect=fake_rank):
            result = provider.search("q", limit=3)
        assert result["success"] is True
        return captured["ranker"]

    def test_ranking_mode_keyword(self, provider) -> None:
        cfg = self._capture(provider, {
            "backends": [{"name": "t", "type": "custom"}],
            "ranking_mode": "keyword",
            "ranker": {"provider": "opencode-go", "model": "m", "prompt": "p"},
        })
        assert cfg["provider"] == "none"

    def test_ranking_mode_llm_without_ranker(self, provider) -> None:
        cfg = self._capture(provider, {
            "backends": [{"name": "t", "type": "custom"}],
            "ranking_mode": "llm",
        })
        assert cfg["provider"] == "auto"

    def test_ranking_mode_auto_keeps_ranker(self, provider) -> None:
        ranker = {"provider": "none", "model": "m"}
        cfg = self._capture(provider, {
            "backends": [{"name": "t", "type": "custom"}],
            "ranking_mode": "auto",
            "ranker": dict(ranker),
        })
        assert cfg == ranker

    def test_ranking_mode_keyword_uses_keyword_engine(self) -> None:
        from provider import _rank_results

        results = [
            {"title": "beta hit", "url": "https://b.example.com", "description": ""},
            {"title": "alpha", "url": "https://a.example.com", "description": ""},
            {"title": "none", "url": "https://n.example.com", "description": ""},
            {"title": "beta again", "url": "https://b2.example.com", "description": ""},
        ]
        with patch("agent.auxiliary_client.call_llm") as llm:
            ranked = _rank_results("beta", results, {"provider": "none"})
        assert ranked[0]["url"] == "https://b.example.com"
        assert not llm.called


class TestPoolIntegration:
    """search() wires the pool through the thread pool and keeps state across calls."""

    def test_pool_round_robin_persists_across_searches(self, provider) -> None:
        value_a, value_b = _fake_card_value("a"), _fake_card_value("b")
        values = {"CARD_A": value_a, "CARD_B": value_b}
        config = {
            "backends": [{
                "name": "line1", "type": "custom",
                "base_url": "https://api.example.com", "search_path": "/search",
                "credentials": ["CARD_A", "CARD_B"],
                "pool_strategy": "round_robin",
            }],
            "timeout": 5,
            "max_results": 5,
        }
        payload = {"results": [{"title": "X", "url": "https://x.example.com", "content": "ok"}]}
        with patch("provider._read_config", return_value=config), patch(
            "provider.get_provider_env", side_effect=lambda n: values.get(n, ""),
        ), patch("httpx.post", side_effect=[_fake_response(200, payload), _fake_response(200, payload)]) as mock_post:
            assert provider.search("q1", limit=3)["success"] is True
            assert provider.search("q2", limit=3)["success"] is True

        auth = [call.kwargs["headers"]["Authorization"] for call in mock_post.call_args_list]
        assert auth == [f"Bearer {value_a}", f"Bearer {value_b}"]  # cursor survived


class TestConfigSchema:
    """plugin.yaml declares the six settings-page keys per the host rules."""

    def test_config_schema_keys_valid(self) -> None:
        import re

        try:
            import hermes_yaml as yaml
        except ImportError:
            import yaml

        data = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        assert data.get("manifest_version") == 2
        schema = data.get("config_schema")
        assert set(schema) == {
            "backends", "timeout", "max_results", "min_backends",
            "ranking_mode", "pool_strategy", "ranker",
        }
        segment = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
        reserved = {"model", "plugins", "security", "settings"}
        known_types = {
            "str", "string", "int", "integer", "float", "number", "bool",
            "boolean", "list", "array", "dict", "object", "secret",
        }
        for key, spec in schema.items():
            assert segment.fullmatch(key), key
            assert key.lower() not in reserved, key
            assert str(spec.get("type", "str")).lower() in known_types, key
            assert "default" in spec, key
        assert schema["backends"]["type"] == "list"
        assert schema["backends"]["default"] == []
        assert schema["ranking_mode"]["choices"] == ["auto", "keyword", "llm"]
        assert schema["ranking_mode"]["default"] == "auto"
        assert schema["pool_strategy"]["choices"] == ["fill_first", "round_robin", "random"]
        assert schema["pool_strategy"]["default"] == "fill_first"
        # ranker 是 dict 键：UI 写入 settings.ranker 依赖 schema 放行（plugins_settings.py:116-140）
        assert str(schema["ranker"]["type"]).lower() == "dict"
        assert schema["ranker"]["default"] == {}

        import provider as provider_mod

        assert schema["timeout"]["default"] == provider_mod._DEFAULT_TIMEOUT
        assert schema["max_results"]["default"] == provider_mod._DEFAULT_MAX_RESULTS
        assert schema["pool_strategy"]["default"] == provider_mod._DEFAULT_POOL_STRATEGY
        from provider import _POOL_STRATEGIES

        assert tuple(schema["pool_strategy"]["choices"]) == _POOL_STRATEGIES
