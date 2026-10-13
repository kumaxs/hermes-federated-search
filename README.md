# hermes-federated-search

**Federated** web search backend plugin for
[Hermes Agent](https://github.com/NousResearch/hermes-agent). It fans out each
search request to multiple configured sub-backends (Tavily, SearXNG, custom
HTTP endpoints, ...) and ranks the combined results by relevance — keyword
scoring by default, optional LLM re-ranking.

Provider name: `federated` — selected with `web.search_backend: federated`.
It plugs into the existing `web.search_backend` config key; no changes to the
tool chain are required.

## Why Federated?

- **Multi-backend resilience.** If one backend is down or rate-limited, results
  from others still come through. Configure Tavily for English search and a
  custom backend (e.g., MiniMax) for Chinese search — both contribute to every
  query.
- **LLM-based ranking.** An auxiliary LLM reranks merged results for relevance.
  Falls back to keyword-based scoring automatically if the LLM is unavailable.
- **Zero tool changes.** The `web_search` tool passes results through the same
  pipeline; the provider boundary is transparent to the agent.

## Install & Enable

The plugin is a plain directory plugin (no Python packaging step). Install it
into a Hermes home/profile and enable it:

```sh
# from GitHub (recommended)
hermes plugins install kumaxs/hermes-federated-search
hermes plugins enable hermes-federated-search
```

Or copy/symlink this directory into the plugin directory yourself:

- global: `~/.hermes/plugins/hermes-federated-search/`
- profile-scoped: `~/.hermes/profiles/<profile>/plugins/hermes-federated-search/`

**Activation semantics (opt-in user plugin):**

- Like any user plugin, it is loaded when the gateway starts — after
  enabling/disabling, restart the gateway and start a new session so the tool
  surface and provider registry see the plugin.
- `web.search_backend` and `web.federated` are read on each search call —
  config edits take effect without a restart.

**Disable / roll back:**

- Point `web.search_backend` back at your previous backend (e.g. `tavily`), or
  remove the key — takes effect on the next search call.
- Fully remove: `hermes plugins disable hermes-federated-search` (restart the
  gateway) and delete the plugin directory.

Requirements: `httpx` (present in Hermes environments). Config reading uses
`hermes_yaml` when available (Hermes checkouts) and falls back to PyYAML.

## Quick Start

Enable federated search with two backends:

```yaml
# ~/.hermes/config.yaml
web:
  search_backend: federated
  federated:
    backends:
      - name: tavily
      - name: minimax
        type: custom
        base_url: "https://api.minimaxi.com"
        api_key_env: MINIMAX_CN_API_KEY
```

With this configuration, every `web_search` call queries both Tavily and MiniMax
in parallel, merges results, ranks by keyword scoring, and returns up to 8
results.

## Configuration Reference

All federated configuration lives under `web.federated` in `config.yaml`.

### Top-level Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `timeout` | int | `10` | Maximum seconds to wait for all backends. If the deadline expires, partial results from completed backends are returned. |
| `max_results` | int | `8` | Maximum number of results returned after ranking (ceiling). The caller's `limit` parameter is still respected — the actual output count is `min(limit, max_results)`. |
| `k` | int | len(backends) | **K-way aggregation.** Must equal the number of configured backends. When explicitly set, the plugin validates `len(backends) == k`. Maximum 32. |
| `min_backends` | int | `1` | Minimum number of backends that must succeed for the search to be considered successful. Must satisfy `1 ≤ min_backends ≤ k`. |
| `ranker` | dict | — | Optional LLM ranking configuration (see below). |
| `health_check` | dict | — | Optional cached health probing (see below). When absent, all backends are dispatched directly (backward compatible). |
| `backends` | list | — | **Required.** List of sub-backend configurations. |

### Ranking (`ranker`)

The `ranker` section controls how merged results are ordered by relevance.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `ranker.provider` | str | — | LLM provider for ranking. Set to `"none"` (or omit) for keyword-based scoring (fast, zero cost). Use a real provider (e.g., `"opencode-go"`) for LLM ranking. |
| `ranker.model` | str | — | Model name for the ranking LLM (e.g., `"deepseek-v4-flash"`). |
| `ranker.prompt` | str | — | Optional custom system prompt for the ranking LLM. Inject preferences like "prefer Chinese sources" or "prefer official documentation". When omitted, a default prompt is used. |

**Ranking modes:**

- **Keyword** (`provider: "none"` or unset): Scores results by query term
  frequency in title + description. Fast, no API cost.
- **LLM** (real provider): Sends results to an auxiliary LLM for relevance
  ranking. Falls back to keyword mode automatically on timeout, rate limit, or
  parse failure.

**Recommendation:** Use a non-main-model provider for ranking to avoid
concurrency contention. When ≤ 3 results, ranking is skipped entirely.

### Backend Configurations (`backends`)

Each entry in the `backends` list represents one search source.

#### Registered Providers

Use an existing Hermes web search provider (e.g., Tavily, Firecrawl, SearXNG):

```yaml
backends:
  - name: tavily
  - name: firecrawl
```

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `name` | str | ✅ | Registered provider name. Must match an available provider (tavily, firecrawl, searxng, exa, parallel, ddgs, brave_free, xai). |
| `required` | bool | `false` | When `true`, this backend's failure causes the entire search to fail, regardless of `min_backends`. |

Registered providers use their existing credentials (`.env` API keys). No
additional configuration needed.

#### Custom HTTP Backends

Connect any search API that accepts a query parameter and returns JSON:

```yaml
backends:
  - name: minimax
    type: custom
    base_url: "https://api.minimaxi.com"
    api_key_env: MINIMAX_CN_API_KEY
    search_path: /v1/coding_plan/search
    query_param: "q"
    auth_style: bearer
```

| Key | Type | Required | Default | Description |
|-----|------|----------|---------|-------------|
| `name` | str | ✅ | — | Display name for logging and error messages. |
| `type` | str | ✅ | — | Must be `"custom"` to enable custom HTTP mode. |
| `base_url` | str | ✅ | — | Base URL of the search API (e.g., `https://api.minimaxi.com`). |
| `api_key_env` | str | ✅ | — | Environment variable that holds the API key (e.g., `MINIMAX_CN_API_KEY`). Set in `.env`. |
| `search_path` | str | — | `"/v1/coding_plan/search"` | API path appended to `base_url`. |
| `query_param` | str | — | `"q"` | Query string parameter name. |
| `auth_style` | str | — | `"bearer"` | Authentication style: `"bearer"` (Authorization header) or `"x-api-key"` (x-api-key header). |
| `required` | bool | `false` | — | When `true`, this backend's failure causes the entire search to fail, regardless of `min_backends`. |

**Supported response shapes:** The custom backend parser handles three common
API response formats:

- `{"organic": [{"title", "link", "snippet"}, ...]}`
- `{"data": {"web": [{"title", "url", "description"}, ...]}}`
- `{"results": [{"title", "url", "content"}, ...]}`

### Health Checks (`health_check`)

Cached availability probing for custom HTTP backends. When configured, the plugin
sends a lightweight `HEAD` request to each custom backend's `base_url` before
dispatching searches. Results are cached for `ttl_seconds` to avoid consuming
provider quota on every call.

When `health_check` is absent, all backends are dispatched directly (backward
compatible).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `health_check.ttl_seconds` | int | `300` | How long probe results are cached (seconds). Set higher for providers with strict rate limits. |
| `health_check.timeout` | int | `5` | Per-probe HEAD request timeout (seconds). |

**Caching and cooldown:**

- Cache hit: backend dispatched immediately (no network request).
- Cache miss / expired: `HEAD` probe → cache result → proceed.
- Real search failure (HTTP 401/403/429): backend immediately marked unavailable
  with provider-specific cooldown (5 min for 401/403, 1 hour for 429).

## Execution Flow

For each search request:

1. **Parallel fan-out.** All backends are queried concurrently via
   `ThreadPoolExecutor`. Each backend runs in its own thread.
2. **Timeout enforcement.** `concurrent.futures.wait()` with the configured
   timeout bounds the total wait time. Backends that haven't finished are
   cancelled; completed results proceed.
3. **Merge & rank.** Results from all successful backends are combined. If a
   `ranker` is configured, the merged set is ranked by relevance (LLM or keyword).
4. **Top N truncation.** Output count is capped at `min(limit, max_results)` —
   the caller's requested limit is respected, with `max_results` serving as a
   ceiling.

## Full Configuration Example

```yaml
web:
  search_backend: federated
  federated:
    # Wait up to 15 seconds for all backends
    timeout: 15

    # Return at most 10 results after ranking
    max_results: 10

    # K-way aggregation: exactly 3 backends, at least 2 must succeed
    k: 3
    min_backends: 2

    # Health checks: cache probe results for 5 minutes
    health_check:
      ttl_seconds: 300

    # LLM ranking with custom preference prompt
    ranker:
      provider: opencode-go
      model: deepseek-v4-flash
      prompt: |
        Prefer Chinese sources and official documentation.
        Prefer results from the last 6 months.

    backends:
      # Registered provider — critical, must succeed
      - name: tavily
        required: true

      # Self-hosted SearXNG — optional
      - name: searxng
        required: false

      # Custom HTTP backend — MiniMax Chinese search
      - name: minimax
        type: custom
        base_url: "https://api.minimaxi.com"
        api_key_env: MINIMAX_CN_API_KEY
        search_path: /v1/coding_plan/search
        required: false
```

## Troubleshooting

| Symptom | Likely Cause |
|---------|--------------|
| `federated search not configured` | `web.federated` section missing from `config.yaml` or the plugin not enabled |
| `no search backends configured` | `web.federated.backends` is empty or not a list |
| `backend '<name>' not registered` | `name` references an unavailable provider. Check `hermes tools` to see registered providers. |
| `All backends failed` | Every backend returned errors. Check credentials and network. The gateway log shows per-backend details. |
| `No backends available` | Health checks marked all backends unavailable. Check the gateway log for per-backend probe failures. |
| `Required backend(s) failed` | A backend with `required: true` returned no results. Check its credentials and availability. |
| `Only N/M backends succeeded` | Fewer than `min_backends` backends returned results. Increase `min_backends` or investigate failing backends. |
| `k=N but M backend(s) configured` | The `k` value doesn't match the number of `backends` entries. Either adjust `k` or add/remove backends. |
| `Custom backend timed out` | The configured API didn't respond within 15s. Verify the `base_url` and `search_path` are correct. |
| `LLM ranking failed, falling back to keyword ranking` | Normal fallback — the ranking LLM was unavailable. Results use keyword scoring instead. |

Note: the provider reads raw YAML for `web.federated` (the standard config
loader strips unknown keys from the `web` section). Config changes therefore
take effect on the next search call without a restart.

## Known Limitations

- **No content extraction.** `supports_extract()` returns `False`. Use another
  provider (e.g., Tavily or Firecrawl) for `web_extract` by setting
  `web.extract_backend`.
- **No nested federation.** Sub-backends are leaf nodes; you cannot chain
  federated providers.
- **Thread-pool overhead.** Each backend runs in a thread. With many backends
  (>10), consider using fewer, higher-quality providers instead.
- **Health checks are optional.** Without `health_check` configured, failing
  backends are dispatched and time out — same as the original behavior.
  Enable `health_check` to avoid waiting on dead backends.
- **Custom ranking prompt requires LLM ranking.** The `ranker.prompt` field is
  only used when a real provider (not `"none"`) is configured for ranking.

## Status (v1.0.0)

First release — the original federated-search feature set, packaged as a
standalone plugin:

- **Included:** parallel aggregation with `k` / `min_backends` / `required`
  semantics; timeout with partial results; keyword ranking plus optional LLM
  re-ranking with a custom prompt; custom HTTP backends (three response
  shapes); cached health checks with failure cooldowns; per-backend
  configuration.
- **Not included:** multi-key credential rotation (credential pool) — planned
  separately; per-backend `timeout` / `weight` / `result_limit` — intentionally
  not implemented.

## Development

The plugin is plain Python (no packaging step). Tests exercise the provider
through the real `WebSearchProvider` ABC, so they run against a Hermes Agent
checkout with the checkout on `PYTHONPATH`:

```sh
PYTHONPATH=/path/to/hermes-agent python -m pytest tests/ -q
```

Requirements: the dependencies of the checkout you test against (`httpx`,
`jiter` / Hermes runtime deps) plus `pytest`.

## License

MIT — see [LICENSE](LICENSE).
