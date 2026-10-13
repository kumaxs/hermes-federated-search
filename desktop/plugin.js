/**
 * Hermes desktop widget for the `hermes-federated-search` agent plugin.
 *
 * Settings → Plugins → 聚合搜索 — a card-per-line editor over the plugin's
 * official settings tree (`plugins.entries.hermes-federated-search.settings`):
 *
 *   - 线路 (this page): one card per search line, with two levels of
 *     membership — L1 参与聚合 (the line is a member of `backends`) and
 *     L2 参与 (the env name is a member of that line's `credentials`).
 *     Every edit serializes the draft and writes it straight back through
 *     `plugins.manage settings`, then re-reads it (one source of truth).
 *   - Agent settings: the manifest `config_schema` form the app folds in as
 *     this entry's second sub-page (kept for agents; not the main door).
 *
 * Credential policy (R4): the page records environment-variable NAMES only.
 * Key VALUES never appear here — no value input, no value echo, no value in
 * an error template. Values belong to the standard credential door,
 * Settings → Keys (`PUT /api/env`).
 *
 * The page edits the profile the running gateway serves — `plugins.manage`
 * answers for the backend's own profile when no `profile` param is sent
 * (app contract: `store/agent-plugins.ts` withProfile — omitted = launch
 * profile). A plugin page cannot read the app's Settings scope selector.
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls. Only these imports
 * resolve: @hermes/plugin-sdk, react, react/jsx-runtime. `ctx` comes from
 * register(ctx), not from an import.
 */

import { Badge, Button, Checkbox, ConfirmDialog, EmptyState, ErrorState, host, Input, Loader, ScrollArea, SegmentedControl, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Textarea, Tip } from "@hermes/plugin-sdk";
import { useCallback, useEffect, useMemo, useState } from "react";
import { jsx, jsxs } from "react/jsx-runtime";

const PLUGIN_KEY = "hermes-federated-search";

// This widget's build. It must equal `version:` in plugin.yaml — the app
// loads the widget from the CLIENT machine while the gateway runs the agent
// half, so the two halves can genuinely be different builds.
// `tests/test_desktop_widget.py` fails the build when they drift.
const WIDGET_VERSION = "1.0.0";

const CONFIG_HINT =
	"凭据只录环境变量名；值请在 Settings → Keys 录入（PUT /api/env），不要写进 config.yaml。";

const OPTION_KEYS = ["min_backends", "ranking_mode", "pool_strategy", "timeout", "max_results"];
const DEFAULT_OPTIONS = {
	min_backends: 1,
	ranking_mode: "auto",
	pool_strategy: "fill_first",
	timeout: 10,
	max_results: 8
};
const RANKING_MODES = ["auto", "keyword", "llm"];
const POOL_STRATEGIES = ["fill_first", "round_robin", "random"];
const CUSTOM_FIELDS = [
	{ key: "base_url", label: "base_url", placeholder: "https://example.invalid/search" },
	{ key: "search_path", label: "search_path", placeholder: "/search" },
	{ key: "query_param", label: "query_param", placeholder: "q" },
	{ key: "auth_style", label: "auth_style", placeholder: "bearer | header | query" }
];
const ENV_NAME_RE = /^[A-Z][A-Z0-9_]{0,127}$/;

// ---------------------------------------------------------------------------
// Model — pure functions over the settings-tree shape
// ---------------------------------------------------------------------------

const asText = (value) => (value == null ? "" : String(value));

function asInt(value, fallback) {
	if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value);
	const parsed = Number.parseInt(asText(value), 10);
	return Number.isFinite(parsed) ? parsed : fallback;
}

const owns = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

function emptyDraft() {
	return {
		backends: [],
		explicitK: null,
		legacyK: null,
		options: { ...DEFAULT_OPTIONS }
	};
}

/** One settings-tree line → one draft line. `raw` keeps extension keys
 *  (api_key_env, required, per-line pool_strategy, …) so an edit in the UI
 *  never drops a key this page does not render. */
function lineFromValue(source) {
	const raw = source && typeof source === "object" && !Array.isArray(source) ? { ...source } : {};
	const line = {
		name: asText(raw.name),
		type: asText(raw.type) || "custom",
		credentials: Array.isArray(raw.credentials)
			? raw.credentials.filter((item) => typeof item === "string")
			: [],
		raw
	};
	for (const field of CUSTOM_FIELDS) line[field.key] = asText(raw[field.key]);
	return line;
}

/** Draft line → settings-tree line. Unknown keys round-trip unchanged; known
 *  keys are only emitted when non-empty or already present in the source. */
function lineToValue(line) {
	const raw = line.raw && typeof line.raw === "object" ? line.raw : {};
	const out = { name: line.name };
	if (line.type) out.type = line.type;
	for (const [key, value] of Object.entries(raw)) {
		if (key === "name" || key === "type" || key === "credentials") continue;
		if (CUSTOM_FIELDS.some((field) => field.key === key)) continue;
		out[key] = value;
	}
	for (const field of CUSTOM_FIELDS) {
		const value = asText(line[field.key]);
		if (value !== "" || owns(raw, field.key)) out[field.key] = value;
	}
	if (line.credentials.length > 0 || owns(raw, "credentials")) {
		out.credentials = [...line.credentials];
	}
	return out;
}

/** Settings-tree values ({key: value} as `plugins.manage list` reports them)
 *  → draft. Tolerant: a damaged tree yields problems, never a crash, and the
 *  caller keeps the raw text read-only instead of overwriting it. */
function draftFromValues(values) {
	const draft = emptyDraft();
	const problems = [];
	const source = values && typeof values === "object" && !Array.isArray(values) ? values : {};
	const rawBackends = source.backends;
	if (rawBackends != null && !Array.isArray(rawBackends)) {
		problems.push("settings.backends 不是列表——已按只读处理，不会覆盖原值。");
	} else if (Array.isArray(rawBackends)) {
		rawBackends.forEach((raw, index) => {
			if (raw && typeof raw === "object" && !Array.isArray(raw)) {
				draft.backends.push(lineFromValue(raw));
			} else {
				problems.push(`settings.backends[${index}] 不是对象——该行未载入。`);
			}
		});
	}
	draft.options.min_backends = asInt(source.min_backends, DEFAULT_OPTIONS.min_backends);
	draft.options.timeout = asInt(source.timeout, DEFAULT_OPTIONS.timeout);
	draft.options.max_results = asInt(source.max_results, DEFAULT_OPTIONS.max_results);
	const ranking = asText(source.ranking_mode) || DEFAULT_OPTIONS.ranking_mode;
	if (RANKING_MODES.includes(ranking)) {
		draft.options.ranking_mode = ranking;
	} else {
		draft.options.ranking_mode = DEFAULT_OPTIONS.ranking_mode;
		problems.push("ranking_mode 不在可选值内——已回退到默认 auto。");
	}
	const pool = asText(source.pool_strategy) || DEFAULT_OPTIONS.pool_strategy;
	if (POOL_STRATEGIES.includes(pool)) {
		draft.options.pool_strategy = pool;
	} else {
		draft.options.pool_strategy = DEFAULT_OPTIONS.pool_strategy;
		problems.push("pool_strategy 不在可选值内——已回退到默认 fill_first。");
	}
	draft.explicitK = typeof source.k === "number" && Number.isFinite(source.k) ? source.k : null;
	return { draft, problems };
}

/** Draft → the values payload written through `plugins.manage settings`.
 *  Only config_schema keys, `k` is never written (absent k = number of
 *  lines, which is what the UI always produces). */
function settingsFromDraft(draft) {
	const ranking = asText(draft.options.ranking_mode);
	const pool = asText(draft.options.pool_strategy);
	return {
		backends: draft.backends.map(lineToValue),
		timeout: asInt(draft.options.timeout, DEFAULT_OPTIONS.timeout),
		max_results: asInt(draft.options.max_results, DEFAULT_OPTIONS.max_results),
		min_backends: asInt(draft.options.min_backends, DEFAULT_OPTIONS.min_backends),
		ranking_mode: RANKING_MODES.includes(ranking) ? ranking : DEFAULT_OPTIONS.ranking_mode,
		pool_strategy: POOL_STRATEGIES.includes(pool) ? pool : DEFAULT_OPTIONS.pool_strategy
	};
}

/** Draft problems that must block a write (S4). Messages carry the item
 *  position and the rule — never the offending value. */
function validateDraft(draft) {
	const problems = [];
	draft.backends.forEach((line, index) => {
		const label = line.name ? `线路「${line.name}」` : `第 ${index + 1} 条线路`;
		if (!asText(line.name).trim()) problems.push(`${label}：name 不能为空。`);
		if (line.type === "custom" && !asText(line.base_url).trim()) {
			problems.push(`${label}：custom 线路必须有 base_url。`);
		}
		line.credentials.forEach((env, keyIndex) => {
			if (!ENV_NAME_RE.test(env)) {
				problems.push(
					`${label} 第 ${keyIndex + 1} 个 Key：不是合法的环境变量名` +
						"（应形如 ^[A-Z][A-Z0-9_]{0,127}$；粘贴的密钥值一律不接受）。"
				);
			}
		});
	});
	return problems;
}

/** S12: an explicit `k` (settings tree wins over legacy, per top-level key —
 *  provider.py merges that way) that disagrees with the line count makes the
 *  provider refuse the config. Warn; never rewrite it from the UI. */
function kMismatchWarning(k, lineCount) {
	if (typeof k !== "number" || !Number.isFinite(k) || k === lineCount) return null;
	return (
		`配置里有显式的 k=${k}，但当前有 ${lineCount} 条线路。` +
		"provider 要求 k 恰好等于线路数，否则搜索会报「k=… but … backend(s) configured」。 " +
		`界面不会自动改写 k——请改成 ${lineCount}（或在 Agent settings / agent 侧改写）。`
	);
}

function cloneDraft(draft) {
	return {
		backends: draft.backends.map((line) => ({
			...line,
			credentials: [...line.credentials],
			raw: { ...(line.raw || {}) }
		})),
		explicitK: draft.explicitK ?? null,
		legacyK: draft.legacyK ?? null,
		options: { ...draft.options }
	};
}

/** L1 取消勾选 = 从字符串移除（无私有残留）。 */
function excludeLine(draft, index) {
	const next = cloneDraft(draft);
	next.backends.splice(index, 1);
	return next;
}

/** L2 取消勾选 / 移除 = 从该线 credentials 移除。 */
function excludeKey(draft, index, keyIndex) {
	const next = cloneDraft(draft);
	if (next.backends[index]) next.backends[index].credentials.splice(keyIndex, 1);
	return next;
}

function updateLineField(draft, index, field, value) {
	const next = cloneDraft(draft);
	if (next.backends[index]) next.backends[index][field] = value;
	return next;
}

function updateOption(draft, key, value) {
	const next = cloneDraft(draft);
	next.options[key] = value;
	return next;
}

function addLine(draft) {
	const next = cloneDraft(draft);
	next.backends.push({
		name: "",
		type: "custom",
		credentials: [],
		raw: {},
		base_url: "",
		search_path: "",
		query_param: "",
		auth_style: ""
	});
	return next;
}

function addKey(draft, index) {
	const next = cloneDraft(draft);
	if (next.backends[index]) next.backends[index].credentials.push("");
	return next;
}

function draftToConfigText(draft) {
	return [
		`# plugins.entries.${PLUGIN_KEY}.settings（聚合搜索 v${WIDGET_VERSION}）`,
		`# ${CONFIG_HINT}`,
		JSON.stringify(settingsFromDraft(draft), null, 2)
	].join("\n");
}

/** String → draft (paste-back door). Comments are ignored; a malformed string
 *  reports an error and yields no draft, so nothing can be written from it. */
function parseConfigText(text) {
	const body = asText(text)
		.split("\n")
		.filter((line) => !line.trimStart().startsWith("#"))
		.join("\n")
		.trim();
	if (!body) return { ok: false, error: "配置字符串为空。", draft: null, problems: [] };
	let parsed = null;
	try {
		parsed = JSON.parse(body);
	} catch (error) {
		return {
			ok: false,
			error: `不是合法 JSON：${error && error.message ? error.message : String(error)}`,
			draft: null,
			problems: []
		};
	}
	const loaded = draftFromValues(parsed);
	if (loaded.problems.length) {
		return { ok: false, error: loaded.problems.join(" "), draft: null, problems: loaded.problems };
	}
	return { ok: true, error: null, draft: loaded.draft, problems: [] };
}

// ---------------------------------------------------------------------------
// Views — pure builders (no hooks), pinned by tests/widget_harness.cjs
// ---------------------------------------------------------------------------

function warningBar(text) {
	return jsx("div", {
		className:
			"rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary) px-3 py-2 text-xs text-(--ui-text-secondary)",
		children: text
	});
}

function keyCardView(line, lineIndex, keyIndex, env, handlers) {
	return jsxs(
		"div",
		{
			className:
				"flex items-center gap-2 rounded-md border border-(--ui-stroke-tertiary) px-2 py-1",
			children: [
				jsx(Checkbox, {
					checked: true,
					"aria-label": "参与",
					onCheckedChange: () => handlers.onExcludeKey(lineIndex, keyIndex)
				}),
				jsx("span", { className: "min-w-0 truncate font-mono text-xs", children: env || "（未命名）" }),
				jsx(Button, {
					variant: "ghost",
					size: "sm",
					onClick: () => handlers.onExcludeKey(lineIndex, keyIndex),
					children: "移除"
				})
			]
		},
		`key-${lineIndex}-${keyIndex}`
	);
}

function lineCardView(line, index, handlers) {
	const isCustom = line.type === "custom";
	return jsxs(
		"div",
		{
			className: "grid gap-3 rounded-lg border border-(--ui-stroke-tertiary) p-3",
			"data-line": line.name || `line-${index}`,
			children: [
			jsxs(
				"div",
				{ className: "flex flex-wrap items-center gap-2", children: [
					jsx(Input, {
						value: line.name,
						placeholder: "线路名（name）",
						className: "max-w-56",
						onChange: (event) => handlers.onField(index, "name", event.target.value)
					}),
					jsx(Badge, {
						variant: isCustom ? "outline" : "secondary",
						children: isCustom ? "custom" : "registered"
					}),
					jsxs("label", {
						className: "ml-auto flex items-center gap-2 text-xs text-(--ui-text-secondary)",
						children: [
							jsx(Checkbox, {
								checked: true,
								"aria-label": "参与聚合",
								onCheckedChange: () => handlers.onExcludeLine(index)
							}),
							"参与聚合"
						]
					}),
					jsx(Button, {
						variant: "ghost",
						size: "sm",
						onClick: () => handlers.onRequestDelete(index),
						children: "删除"
					})
				] },
				`head-${index}`
			),
			jsxs(
				"div",
				{ className: "flex items-center gap-2", children: [
					jsx(SegmentedControl, {
						value: line.type,
						options: [
							{ value: "custom", label: "custom" },
							{ value: "registered", label: "registered" }
						],
						onValueChange: (value) => handlers.onType(index, value)
					}),
					jsx("span", {
						className: "text-xs text-(--ui-text-tertiary)",
						children: isCustom
							? "自定义 HTTP 线路（可多 Key 轮换）"
							: "主机已注册的 provider（凭据由主机解析，credentials 会被忽略）"
					})
				] },
				`type-${index}`
			),
			isCustom
				? jsxs(
						"div",
						{ className: "grid gap-2 sm:grid-cols-2", children: CUSTOM_FIELDS.map((field) =>
							jsx(
								"label",
								{ className: "grid gap-1 text-xs text-(--ui-text-secondary)", children: [
									field.label,
									jsx(Input, {
										value: line[field.key],
										placeholder: field.placeholder,
										"aria-label": field.label,
										onChange: (event) => handlers.onField(index, field.key, event.target.value)
									})
								] },
								`${line.name || index}-${field.key}`
							)
						) },
						`fields-${index}`
					)
				: null,
			jsxs(
				"div",
				{ className: "grid gap-2", children: [
					jsx("div", {
						className: "text-xs text-(--ui-text-secondary)",
						children: `Key（环境变量名）：${CONFIG_HINT}`
					}),
					line.credentials.length === 0
						? jsx("div", {
								className: "text-xs text-(--ui-text-tertiary)",
								children: "（还没有 Key——没有凭据时该线路会被跳过）"
							})
						: jsxs(
								"div",
								{ className: "grid gap-1", children: line.credentials.map((env, keyIndex) =>
									keyCardView(line, index, keyIndex, env, handlers)
								) },
								`keys-${index}`
							),
					jsx(Button, {
						variant: "ghost",
						size: "sm",
						onClick: () => handlers.onAddKey(index),
						children: "＋ 添加 Key"
					})
				] }
			)
		] },
		`line-${index}`
	);
}

function optionsView(draft, handlers) {
	return jsxs(
		"div",
		{ className: "grid gap-3 rounded-lg border border-(--ui-stroke-tertiary) p-3", children: [
			jsx("div", { className: "text-xs font-medium text-(--ui-text-secondary)", children: "聚合选项" }),
			jsxs("div", { className: "grid gap-3 sm:grid-cols-3", children: [
				jsx("label", { className: "grid gap-1 text-xs text-(--ui-text-secondary)", children: [
					"最少成功线路（min_backends）",
					jsx(Input, {
						value: String(draft.options.min_backends),
						"aria-label": "min_backends",
						onChange: (event) => handlers.onOption("min_backends", event.target.value)
					})
				] }),
				jsx("label", { className: "grid gap-1 text-xs text-(--ui-text-secondary)", children: [
					"超时（秒，timeout）",
					jsx(Input, {
						value: String(draft.options.timeout),
						"aria-label": "timeout",
						onChange: (event) => handlers.onOption("timeout", event.target.value)
					})
				] }),
				jsx("label", { className: "grid gap-1 text-xs text-(--ui-text-secondary)", children: [
					"输出条数（max_results）",
					jsx(Input, {
						value: String(draft.options.max_results),
						"aria-label": "max_results",
						onChange: (event) => handlers.onOption("max_results", event.target.value)
					})
				] })
			] }),
			jsxs("div", { className: "grid gap-3 sm:grid-cols-2", children: [
				jsx("label", { className: "grid gap-1 text-xs text-(--ui-text-secondary)", children: [
					"排序方式（ranking_mode）",
					jsx(Select, {
						value: draft.options.ranking_mode,
						onValueChange: (value) => handlers.onOption("ranking_mode", value),
						children: jsxs(SelectTrigger, { "aria-label": "ranking_mode", children: [
							jsx(SelectValue, {}),
							jsx(SelectContent, { children: RANKING_MODES.map((mode) =>
								jsx(SelectItem, { value: mode, children: mode }, `mode-${mode}`)
							) })
						] })
					})
				] }),
				jsx("label", { className: "grid gap-1 text-xs text-(--ui-text-secondary)", children: [
					"多卡使用策略（pool_strategy）",
					jsx(Select, {
						value: draft.options.pool_strategy,
						onValueChange: (value) => handlers.onOption("pool_strategy", value),
						children: jsxs(SelectTrigger, { "aria-label": "pool_strategy", children: [
							jsx(SelectValue, {}),
							jsx(SelectContent, { children: POOL_STRATEGIES.map((policy) =>
								jsx(SelectItem, { value: policy, children: policy }, `pool-${policy}`)
							) })
						] })
					})
				] })
			] })
		] });
}

function configStringView(draft) {
	return jsxs(
		"div",
		{ className: "grid gap-2 rounded-lg border border-(--ui-stroke-tertiary) p-3", children: [
			jsxs("div", { className: "flex items-center gap-2", children: [
				jsx("span", {
					className: "text-xs font-medium text-(--ui-text-secondary)",
					children: `当前配置字符串（v${WIDGET_VERSION}）——agent 直接改这棵树`
				}),
				jsx(Tip, {
					label: "docs/agent-config.md 是唯一权威入口：改哪棵树、键语义、值走 Settings → Keys。",
					children: jsx(Button, { variant: "ghost", size: "sm", children: "指引" })
				})
			] }),
			jsx(Textarea, {
				readOnly: true,
				value: draftToConfigText(draft),
				"aria-label": "当前配置字符串",
				className: "font-mono text-xs",
				rows: 8
			}),
			jsx("div", {
				className: "text-xs text-(--ui-text-tertiary)",
				children: "settings 树是唯一真源；web.federated 旧配置只作只读兜底、本页不改写。"
			})
		] });
}

function emptyView() {
	return jsx(EmptyState, {
		title: "还没有搜索线路",
		description:
			"空配置时搜索会报「no search backends configured」（provider.py:265）。点下面的「＋ 添加线路」建第一条。"
	});
}

function errorView(message) {
	return jsx(ErrorState, {
		title: "聚合搜索配置读不出来",
		description: message || "plugins.manage 没有返回配置。"
	});
}

function linesView({ draft, k, handlers, note }) {
	const warning = kMismatchWarning(k, draft.backends.length);
	return jsxs("div", { className: "grid gap-4", children: [
		note ? warningBar(note) : null,
		warning ? warningBar(warning) : null,
		draft.backends.length === 0
			? emptyView()
			: jsxs("div", { className: "grid gap-3", children: draft.backends.map((line, index) =>
					lineCardView(line, index, handlers)
				) }),
		jsx(Button, { variant: "secondary", size: "sm", onClick: handlers.onAddLine, children: "＋ 添加线路" }),
		optionsView(draft, handlers),
		configStringView(draft)
	] });
}

function PasteBox({ onApply }) {
	const [text, setText] = useState("");
	const [note, setNote] = useState(null);
	return jsxs("div", { className: "grid gap-2 rounded-lg border border-(--ui-stroke-tertiary) p-3", children: [
		jsx("div", {
			className: "text-xs font-medium text-(--ui-text-secondary)",
			children: "从字符串导入（粘贴上面那种 JSON，写回 settings 树）"
		}),
		jsx(Textarea, {
			value: text,
			"aria-label": "配置字符串输入",
			className: "font-mono text-xs",
			rows: 6,
			placeholder: '{ "backends": [ … ], "timeout": 10, … }',
			onChange: (event) => setText(event.target.value)
		}),
		jsxs("div", { className: "flex items-center gap-2", children: [
			jsx(Button, {
				variant: "secondary",
				size: "sm",
				disabled: !text.trim(),
				onClick: () => {
					const parsed = parseConfigText(text);
					if (!parsed.ok) {
						setNote(parsed.error);
						return;
					}
					setNote(null);
					onApply(parsed.draft);
				},
				children: "应用"
			}),
			note ? jsx("span", { className: "text-xs text-(--ui-text-secondary)", children: note }) : null
		] })
	] });
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

async function readGeometryExtras() {
	// Best-effort: `plugins.manage list` only carries schema-declared keys, so
	// an explicit `k` (never declared in config_schema) is only visible in the
	// loaded config. Failure = no reading, never a failed page.
	try {
		const full = await host.request("config.get", { key: "full" });
		const cfg = (full && full.config) || {};
		const entries = ((cfg.plugins || {}).entries) || {};
		const entry = entries[PLUGIN_KEY] || {};
		const settings = entry.settings && typeof entry.settings === "object" ? entry.settings : {};
		const web = cfg.web || {};
		const legacy = web.federated && typeof web.federated === "object" ? web.federated : null;
		return {
			settingsK: typeof settings.k === "number" && Number.isFinite(settings.k) ? settings.k : null,
			legacyK: legacy && typeof legacy.k === "number" && Number.isFinite(legacy.k) ? legacy.k : null
		};
	} catch {
		return { settingsK: null, legacyK: null };
	}
}

async function loadDraft() {
	const listed = await host.request("plugins.manage", { action: "list" });
	const rows = Array.isArray(listed && listed.plugins) ? listed.plugins : [];
	const row = rows.find((item) => item && (item.key === PLUGIN_KEY || item.name === PLUGIN_KEY));
	if (!row) throw new Error(`plugins.manage list 里没有 ${PLUGIN_KEY}——插件未安装或未启用。`);
	const values = {};
	for (const field of row.settings_schema || []) {
		if (field && typeof field.key === "string") values[field.key] = field.value;
	}
	const { draft, problems } = draftFromValues(values);
	const extras = await readGeometryExtras();
	draft.explicitK = extras.settingsK;
	draft.legacyK = extras.legacyK;
	return { draft, problems, rawValues: values };
}

function ConfigPage() {
	const [state, setState] = useState({
		phase: "loading",
		draft: null,
		problems: [],
		rawText: "",
		saveError: null,
		busy: false
	});
	const [deleteIndex, setDeleteIndex] = useState(null);

	const refresh = useCallback(async () => {
		try {
			const loaded = await loadDraft();
			setState({
				phase: "ready",
				draft: loaded.draft,
				problems: loaded.problems,
				rawText: loaded.problems.length ? JSON.stringify(loaded.rawValues, null, 2) : "",
				saveError: null,
				busy: false
			});
		} catch (error) {
			setState({
				phase: "error",
				draft: null,
				problems: [],
				rawText: "",
				saveError: error && error.message ? String(error.message) : String(error),
				busy: false
			});
		}
	}, []);

	useEffect(() => {
		void refresh();
	}, [refresh]);

	const write = useCallback(
		async (next) => {
			const problems = validateDraft(next);
			if (problems.length) {
				setState((prev) => ({ ...prev, draft: next, problems, saveError: null }));
				return;
			}
			const values = settingsFromDraft(next);
			setState((prev) => ({ ...prev, draft: next, problems: [], saveError: null, busy: true }));
			try {
				const result = await host.request("plugins.manage", {
					action: "settings",
					key: PLUGIN_KEY,
					values
				});
				if (result && result.ok === false) throw new Error(result.error || "settings 被拒绝");
				await refresh();
			} catch (error) {
				// Toast + keep the draft so the edit is not lost; the raw config
				// is never overwritten from a failed write.
				host.notifyError(error, "聚合搜索：保存失败（草稿已保留）");
				setState((prev) => ({
					...prev,
					busy: false,
					saveError: error && error.message ? String(error.message) : String(error)
				}));
			}
		},
		[refresh]
	);

	const handlers = useMemo(
		() => ({
			onField: (index, field, value) => {
				if (state.draft) void write(updateLineField(state.draft, index, field, value));
			},
			onType: (index, value) => {
				if (state.draft) void write(updateLineField(state.draft, index, "type", value));
			},
			onOption: (key, value) => {
				if (state.draft) void write(updateOption(state.draft, key, value));
			},
			onAddLine: () => {
				if (state.draft) void write(addLine(state.draft));
			},
			onAddKey: (index) => {
				if (state.draft) void write(addKey(state.draft, index));
			},
			onExcludeLine: (index) => {
				if (state.draft) void write(excludeLine(state.draft, index));
			},
			onExcludeKey: (index, keyIndex) => {
				if (state.draft) void write(excludeKey(state.draft, index, keyIndex));
			},
			onRequestDelete: (index) => setDeleteIndex(index)
		}),
		[state.draft, write]
	);

	if (state.phase === "loading") return jsx(Loader, {});
	if (state.phase === "error") return errorView(state.saveError);

	const draft = state.draft || emptyDraft();
	const effectiveK = draft.explicitK == null ? draft.legacyK : draft.explicitK;
	const note = state.problems.length
		? `字符串非法/损坏：${state.problems.join(" ")}（已只读——不会覆盖原值；可用 Agent settings 修复）`
		: null;

	return jsxs("div", { className: "grid gap-4 p-1", children: [
		jsx("div", {
			className: "text-xs text-(--ui-text-tertiary)",
			children: "本页作用于当前网关 profile 的 settings 树。改了立即写回并回读。"
		}),
		jsx(ScrollArea, { children: linesView({ draft, k: effectiveK, handlers, note }) }),
		state.saveError ? warningBar(`保存失败：${state.saveError}`) : null,
		state.busy ? jsx(Loader, {}) : null,
		state.problems.length
			? jsx(Textarea, {
					readOnly: true,
					value: state.rawText,
					"aria-label": "原始配置（只读）",
					className: "font-mono text-xs",
					rows: 8
				})
			: null,
		jsx(PasteBox, { onApply: (next) => void write(next) }),
		jsx(ConfirmDialog, {
			open: deleteIndex != null,
			onClose: () => setDeleteIndex(null),
			onConfirm: () => {
				if (state.draft && deleteIndex != null) void write(excludeLine(state.draft, deleteIndex));
				setDeleteIndex(null);
			},
			title: "删除这条线路？",
			description:
				"会从 settings.backends 移除该线路（legacy web.federated 原样保留、不改写）。搜索随即按新配置生效。",
			confirmLabel: "删除",
			destructive: true
		})
	] });
}

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------

export default {
	id: PLUGIN_KEY,
	register(ctx) {
		const page = {
			id: "config",
			title: "聚合搜索",
			icon: "search",
			order: 0,
			render: () => jsx(ConfigPage, {})
		};
		if (typeof ctx.registerSettingsPage === "function") {
			ctx.registerSettingsPage(page);
			return;
		}
		// Older hosts: the same entry spelled with the raw area.
		ctx.register({
			id: "config",
			area: "settings.plugins",
			title: page.title,
			render: page.render,
			data: { icon: page.icon }
		});
	}
};

// Pinned by tests/widget_harness.cjs (plain-ESM module: extra exports are inert
// for the app loader). Keep the pure model reachable from the harness.
export const __test = {
	PLUGIN_KEY,
	WIDGET_VERSION,
	DEFAULT_OPTIONS,
	emptyDraft,
	draftFromValues,
	settingsFromDraft,
	validateDraft,
	kMismatchWarning,
	cloneDraft,
	excludeLine,
	excludeKey,
	updateLineField,
	updateOption,
	addLine,
	addKey,
	draftToConfigText,
	parseConfigText,
	loadDraft,
	readGeometryExtras,
	linesView,
	optionsView,
	lineCardView,
	keyCardView,
	emptyView,
	errorView,
	ConfigPage
};
