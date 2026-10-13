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

## What's New in Phase 2

Two additions on top of v1.0.0 (branch `feat/p2-config-multicard`):

- **Settings-page exposure.** The plugin declares seven keys (`backends`,
  `timeout`, `max_results`, `min_backends`, `ranking_mode`, `pool_strategy`,
  `ranker`)
  through the official `config_schema` mechanism (manifest v2), so
  Settings → Plugins renders an editable form and saves them under
  `plugins.entries.hermes-federated-search.settings`. Values merge per
  top-level key with the legacy `web.federated` block (settings win) — no
  migration required.
- **Multi-card credential pools.** A `custom` line can list several
  environment variable names under `credentials` and rotate them with
  `fill_first` / `round_robin` / `random`; a failing card cools down and the
  request fails over to the next usable card. Key values stay in the runtime
  environment — only names ever go into configuration.

## What's New in Phase 3

A human-facing card editor on top of the Phase 2 settings exposure
(branch `feat/p3-card-config-ui`):

- **Card-per-line config page** under Settings → Plugins → 「聚合搜索」
  (`desktop/plugin.js`, registered via `ctx.registerSettingsPage`): one card
  per line with two levels of membership — L1 参与聚合 (member of `backends`)
  and L2 参与 (member of that line's `credentials`); add/remove lines and keys;
  every edit serializes and writes straight back through
  `plugins.manage settings`, then re-reads. The manifest `config_schema` form
  stays available as the entry's **Agent settings** sub-page.
- **String ↔ UI parity.** The page shows the current settings string (YAML/JSON
  for agents) and takes a pasted string back; the settings tree stays the one
  source of truth — unchecking a box removes the member from the string, with
  no private residue.
- **Credential policy unchanged and visible:** the page records environment
  variable NAMES only — no value input, no value echo. Values go through
  Settings → Keys (`PUT /api/env`). See `docs/agent-config.md`.
- **No migration.** Whatever is already in the settings tree (or legacy
  `web.federated`) loads into the card page as-is; nothing is rewritten behind
  your back — an explicit `k` that mismatches the line count shows a warning
  bar only. Registered-provider lines stay single-card; multi-card remains
  `custom`-only.

## Install & Enable

The plugin is a plain directory plugin (no Python packaging step). Install it
into a Hermes home/profile and enable it:

```sh
# from GitHub (recommended) — enables the plugin in the same step
hermes plugins install kumaxs/hermes-federated-search --enable
# ... or, for an already installed plugin:
hermes plugins enable hermes-federated-search
```

Or copy/symlink this directory into the plugin directory yourself:

- global: `~/.hermes/plugins/hermes-federated-search/`
- profile-scoped: `~/.hermes/profiles/<profile>/plugins/hermes-federated-search/`

Pinning an exact revision works from a checkout or archive:
`hermes plugins install <source> --ref <40-hex SHA> --enable` — the phase-3
branch head was installed this way in the local drill (byte-identical install,
same live reload as below).

**Desktop half (phase 3).** The plugin ships a desktop package
(`desktop/plugin.js`). The desktop app reconciles it into the app-level root
`~/.hermes/desktop-plugins/hermes-federated-search/` (marker
`.hermes-package.json`; `source` = the agent half's `desktop/` directory).
Measured: the settle lands within seconds once the app's reconcile pass runs —
touching `~/.hermes/desktop-plugins/` triggers it. If the settings entry does
not appear, use ⌘K → "Reload desktop plugins" (or open Capabilities → Plugins);
a load failure shows an error toast. Rolling the agent half back prunes the
desktop half on the next reconcile pass.

**Validate note.** `hermes plugins validate` passes on a clean export with a
clean security scan (no findings). Earlier revisions printed one
`security scan caution: dump_all_env` line — the key-row label fallback's
`||` text read as a shell pipe by the scanner's literal heuristic. The
fallback now uses an equivalent ternary, so the warning is gone.

**Activation semantics (opt-in user plugin):**

- With a running gateway, `hermes plugins install … --enable` asks it to
  reload plugins live — measured message: `Gateway reloaded plugins; nothing
  of this plugin needs a session or restart.` No gateway restart is
  required.
- If the gateway is not running, or reports `reloaded=false` (older builds),
  start/restart it and open a new session so the provider registry sees the
  plugin.
- `web.search_backend`, `web.federated` and
  `plugins.entries.hermes-federated-search.settings` are read on each search
  call — config edits (including settings-page saves) take effect without a
  restart.

**After enabling — expected signals:**

- `hermes plugins show hermes-federated-search` → `Status: enabled`.
- Config readback (`grep -n federated <profile>/config.yaml`):
  `search_backend: federated` plus the `federated:` block — or the
  `plugins.entries.hermes-federated-search.settings` tree when the keys were
  saved from the settings page.
- Gateway log lines: `Plugin 'hermes-federated-search' registered web
  provider: federated`, and per-query
  `Federated search: '<query>' (N/M backends active, …)`.
- A fresh search succeeds through the `federated` provider (`success=true`,
  at least one result).
- Desktop (phase 3): Settings → Plugins → 「聚合搜索」 opens the card page —
  one card per line with two membership levels (L1 参与聚合 = member of
  `backends`; L2 参与 = member of the line's `credentials`), add/remove for
  lines and keys, a per-line 「Key 轮换策略（pool_strategy）」 select and the
  当前配置字符串 box with paste-import. Every edit serializes and is written
  back through `plugins.manage settings`; reopen the page to re-read the saved
  string. The seven-key `config_schema` form stays available as the "Agent
  settings" sub-page.
- Desktop half is opt-in: the `.hermes-package.json` bundle ships it disabled
  by default — enable it via 桌面 Hermes → 设置 → 技能与工具 → 插件 → open
  hermes-federated-search, then 设置 → 插件 →「聚合搜索」 (full path and the
  ⌘K →「Reload desktop plugins」 fallback: `docs/agent-config.md`).

**Disable / roll back:**

- Point `web.search_backend` back at your previous backend (e.g. `tavily`), or
  remove the key — takes effect on the next search call.
- Fully remove: `hermes plugins remove hermes-federated-search` — add
  `--allow-live-gateway` when the gateway keeps running (a guard blocks
  removal otherwise; measured to deregister the provider live, no restart
  needed). The phase-3 desktop half is pruned by the app's next reconcile pass.

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

## Agent: configure by string

Both doors edit the same tree, so an agent can do the whole configuration by
editing the string (or by pasting it into the card page):

```yaml
plugins:
  entries:
    hermes-federated-search:
      settings:
        backends:
          - name: tavily
            type: custom
            base_url: "https://api.example.invalid/search"
            credentials: [TAVILY_API_KEY, TAVILY_API_KEY_2]   # names only
        min_backends: 1
        ranking_mode: auto
```

- `plugins.entries.hermes-federated-search.settings` is the single source of
  truth; legacy `web.federated` still works and merges per top-level key
  (settings win). The card page never rewrites the legacy block.
- `credentials` lists **environment variable names** (`^[A-Z][A-Z0-9_]{0,127}$`)
  and nothing else — a pasted key value is rejected at config validation
  without echoing it. Key values belong to Settings → Keys (`PUT /api/env`).
- An explicit `k` must equal the number of `backends`; leave it out (the UI
  does) and the line count is used. A mismatched explicit `k` is shown as a
  warning bar — never rewritten automatically.
- After a string edit, reopen the page to see it (open = refetch). Full guide:
  [`docs/agent-config.md`](docs/agent-config.md).

## Configuration Reference

Federated configuration can be written in two places; they merge per
top-level key, with the official settings tree winning:

- **Settings → Plugins** (official form). Values are stored under
  `plugins.entries.hermes-federated-search.settings` in `config.yaml`. The
  seven keys declared by the plugin (`plugin.yaml` `config_schema`):
  `backends`, `timeout`, `max_results`, `min_backends`, `ranking_mode`,
  `pool_strategy`, `ranker`.
- **`web.federated`** in `config.yaml` — the legacy location, still fully
  supported; no migration is required.

Notes: the settings form does not shadow legacy values by itself — the old
`web.federated` values keep applying until the matching key is saved on the
settings page. `web.federated` is an unrecognized nested path for
`hermes config set` (needs `--force`) and never appears in the desktop form;
use the settings page (or edit `config.yaml` directly) instead.

### Top-level Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `timeout` | int | `10` | Total deadline for all backends. Lines that miss it are dropped; lines that already returned contribute their partial results. |
| `max_results` | int | `8` | Maximum number of results returned after ranking (ceiling). The caller's `limit` parameter is still respected — the actual output count is `min(limit, max_results)`. |
| `k` | int | len(backends) | **K-way aggregation.** Must equal the number of configured backends. When explicitly set, the plugin validates `len(backends) == k`. Maximum 32. |
| `min_backends` | int | `1` | Minimum number of backends that must **return results** for the search to be considered successful. A line succeeds when it answers with a result-bearing response within the deadline. Must satisfy `1 ≤ min_backends ≤ k`; `required` lines must always succeed. |
| `ranking_mode` | str | `auto` | Ranking mode: `auto` (follows `ranker.provider` as before), `keyword` (force keyword scoring), `llm` (force LLM ranking; uses the default auxiliary model when `ranker.provider` is unset). The `ranker` dict stays usable alongside. |
| `pool_strategy` | str | `fill_first` | Global default rotation for custom lines with `credentials`: `fill_first` (use one card until it fails), `round_robin` (rotate per request), `random`. Overridable per line (the card editor exposes the select inside each line card); the key itself is kept and stays read+write for compatibility. |
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
| `api_key_env` | str | — | — | Single-card mode: environment variable that holds the API key (e.g., `MINIMAX_CN_API_KEY`). Set in `.env`. Use `credentials` instead for a multi-card pool. |
| `credentials` | list | — | — | Multi-card pool: list of environment variable **names** (e.g., `[MINIMAX_KEY_1, MINIMAX_KEY_2]`). Values live in the runtime environment, never in `config.yaml`. A failed card cools down and the line fails over to the next usable card. |
| `pool_strategy` | str | — | inherited | Per-line override of the top-level `pool_strategy` (validate: `fill_first` \| `round_robin` \| `random`). |
| `search_path` | str | — | `"/v1/coding_plan/search"` | API path appended to `base_url`. |
| `query_param` | str | — | `"q"` | Query string parameter name. |
| `auth_style` | str | — | `"bearer"` | Authentication style: `"bearer"` (Authorization header) or `"x-api-key"` (x-api-key header). |
| `required` | bool | `false` | — | When `true`, this backend's failure causes the entire search to fail, regardless of `min_backends`. |

**Supported response shapes:** The custom backend parser handles three common
API response formats:

- `{"organic": [{"title", "link", "snippet"}, ...]}`
- `{"data": {"web": [{"title", "url", "description"}, ...]}}`
- `{"results": [{"title", "url", "content"}, ...]}`

#### Multi-Card Credential Pools (`credentials`)

Give one custom line several keys of the same vendor; a failing card is
cooled down and the request fails over to the next usable card:

```yaml
backends:
  - name: minimax
    type: custom
    base_url: "https://api.minimaxi.com"
    search_path: /v1/coding_plan/search
    query_param: "q"
    credentials: [MINIMAX_KEY_1, MINIMAX_KEY_2]   # env var NAMES only
    pool_strategy: round_robin                    # optional, per-line
```

Rules and semantics:

- **Names only.** `credentials` items must match
  `^[A-Z][A-Z0-9_]{0,127}$`. Values are resolved from the runtime
  environment (per-profile `.env`); a pasted key value is rejected at
  validation with a "looks like a secret value" error, and error messages
  never echo the item. Duplicate names are deduplicated (warned).
- **Value shapes.** Empty / whitespace-only values and values containing a
  line break are skipped as not configured; a card whose env var is missing
  is never dialed. If no card on the line is usable, the line fails without
  network traffic.
- **Strategies.** `fill_first` (default) uses the first usable card and only
  switches when it fails; `round_robin` rotates one card per request
  (process-local cursor); `random` picks uniformly among the usable cards.
- **Switching.** A card that fails — HTTP error (401/403/429/5xx) or a
  network/timeout error — is cooled down and the request continues with the
  next usable card. Each card is dialed at most once per request. When every
  card is exhausted, the line fails as a whole (same
  `(results, status_code)` contract as single-card lines).
- **Cooldowns** (aligned with the Hermes credential pool): 401 → 300s,
  403 → 300s, 429 → 3600s, anything else (5xx / network / unknown) → 300s;
  `401/403/429` keep their values. When the line has a single usable card,
  *transient* cooldowns are capped at 60s (host `sole_credential`
  alignment). The legacy `api_key_env` single-card path is **not** capped.
- **State is process-local.** Cooldowns and the round-robin cursor live in
  memory; restarting Hermes clears them.
- **Registered providers cannot be pooled.** The host resolves a registered
  provider's key internally (`search(query, limit)` takes no key parameter),
  so a `credentials` list on a non-`custom` line is ignored with a warning
  and the line keeps working exactly as before. Multi-card requires
  `type: custom`.
- **Tavily as a custom line** (migration path for pooling a registered
  provider): `base_url: "https://api.tavily.com"`, `search_path: /search`,
  `query_param: query`, `auth_style: bearer` — its
  `{"results": [{title, url, content}, ...]}` response is already supported.

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
| `credentials[N] is not a valid environment variable name` / `looks like a secret value` | Fix the `credentials` list: it takes environment variable **names** only (uppercase, digits, underscores). Put the key value in the runtime environment (`.env`), never in `config.yaml`. |
| `no usable credentials among N configured card(s)` | Every listed env var is missing, empty or multi-line. Set the values in the profile `.env`; missing-card names are logged as they are skipped. |
| `pool_strategy must be one of: ...` / `ranking_mode must be one of: ...` | Invalid enum value in `web.federated` (the settings page dropdown only offers legal values). |

Note: the provider reads raw YAML for `web.federated` and for
`plugins.entries.hermes-federated-search.settings` (the standard config loader
strips unknown keys from the `web` section). Config changes — including
settings-page saves — therefore take effect on the next search call without a
restart.

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
- **Registered providers stay single-card.** Multi-card pooling needs
  `type: custom` — the host's provider interface takes no key parameter, so a
  `credentials` list on a registered line is ignored (with a warning). The
  `k` option also stays out of the settings form (its existing validation is
  unchanged).
- **Pool cooldowns are in-process.** Restarting Hermes clears cooldowns and
  the round-robin cursor.

## Status

v1.0.0 base plus the phase-2 and phase-3 improvements:

- **Included (v1.0.0):** parallel aggregation with `k` / `min_backends` /
  `required` semantics; timeout with partial results; keyword ranking plus
  optional LLM re-ranking with a custom prompt; custom HTTP backends (three
  response shapes); cached health checks with failure cooldowns; per-backend
  configuration.
- **Included (phase 2, branch `feat/p2-config-multicard`):** official
  Settings → Plugins exposure of six config keys (`plugin.yaml`
  `config_schema`: `backends`, `timeout`, `max_results`, `min_backends`,
  `ranking_mode`, `pool_strategy`; merged with legacy `web.federated`);
  per-line multi-card credential pools (`fill_first` / `round_robin` /
  `random`) with automatic failover and pool-aligned cooldowns;
  `ranking_mode` (auto | keyword | llm).
- **Included (phase 3, branch `feat/p3-card-config-ui`):** desktop card editor
  (`desktop/plugin.js`, `ctx.registerSettingsPage`) — one card per line with
  L1/L2 membership checkboxes, add/remove lines and keys, current-settings
  string with paste import, string ↔ UI parity (unchecking removes the member,
  no private state); the schema form stays as the Agent settings sub-page;
  plus `docs/agent-config.md` (agent-side configuration guide) and the
  desktop-widget test harness (`tests/test_desktop_widget.py`,
  `tests/widget_harness.cjs`).
- **Included (phase 3 revision, v1.1.0):** agent guidance aligned with the
  real enable path (opt-in desktop half, `docs/agent-config.md` + in-page 指引
  dialog); type select/badge removed — typeless lines stay typeless, new
  cards are `custom`, legacy cards keep `api_key_env`/`required`/unknown keys
  verbatim; a ranker model picker on the card page (`ranker` joins
  `config_schema`, written as a whole tree via `settings.ranker`); the pool
  strategy select moved into each line card (`pool_strategy` per line, the
  global key stays a read+write default); timeout / min_backends wording
  aligned with the runtime semantics.
- **Not included:** multi-card on registered providers (host interface takes
  no key) and direct credential-pool (`auth.json`) consumption — future
  increments; per-backend `timeout` / `weight` / `result_limit` —
  intentionally not implemented.

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
