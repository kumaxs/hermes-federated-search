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

import { Button, Checkbox, ConfirmDialog, Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, EmptyState, ErrorState, host, Input, Loader, ScrollArea, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Textarea, Tip } from "@hermes/plugin-sdk";
import { useCallback, useEffect, useMemo, useState } from "react";
import { jsx, jsxs } from "react/jsx-runtime";

const PLUGIN_KEY = "hermes-federated-search";

// This widget's build. It must equal `version:` in plugin.yaml — the app
// loads the widget from the CLIENT machine while the gateway runs the agent
// half, so the two halves can genuinely be different builds.
// `tests/test_desktop_widget.py` fails the build when they drift.
const WIDGET_VERSION = "1.1.3";

// The built-in rerank system prompt — the provider's unconfigured branch
// (provider.py::_rank_results). The rerank-prompt box shows it as the
// effective value while `ranker.prompt` is unset; a test pins this copy
// against the provider's actual default system message, so they cannot drift.
const DEFAULT_RANKER_PROMPT =
	"You rank search results by relevance. Rules:\n" +
	"- Return ONLY a JSON array of result indices ranked by relevance, e.g. [3,1,5,2,4]\n" +
	"- Include ALL results. Most relevant first.\n" +
	"- No other text.";

// ---------------------------------------------------------------------------
// Copy — every user-facing string, in two bundles. `en` is the primary (the
// schema default for `ui_language`); `zh` is the optional localisation. The
// page speaks the language its own settings tree asks for, not the host
// locale, so an agent can pre-set it through `plugins.manage settings`.
// ---------------------------------------------------------------------------

const UI_LANGUAGES = ["en", "zh"];
const UI_LANGUAGE_DEFAULT = "en";
// The bundle a call that passed no language renders: the released pre-i18n
// copy, which is what tests/widget_harness.cjs pins when it renders the bare
// builders. Every path in the app passes the language it read from the tree.
const BARE_RENDER_LANG = "zh";

/** A settings value → a language this widget ships: en/zh pass, junk → en. */
function resolveLang(value) {
	const lang = asText(value);
	return UI_LANGUAGES.includes(lang) ? lang : UI_LANGUAGE_DEFAULT;
}

/** The language one render tree speaks — resolve once per builder and thread
 *  it down; `undefined` = a bare call, see BARE_RENDER_LANG. */
function renderLang(lang) {
	return lang === undefined ? BARE_RENDER_LANG : resolveLang(lang);
}

/** One message: the `lang` bundle → the en bundle → the key itself. A bundle
 *  value is a string, or a function taking the call-site arguments. */
function t(key, lang, ...args) {
	const bundle = COPY[lang] ?? COPY[UI_LANGUAGE_DEFAULT];
	const value = owns(bundle, key) ? bundle[key] : COPY[UI_LANGUAGE_DEFAULT][key];
	if (typeof value === "function") return value(...args);
	return typeof value === "string" ? value : key;
}

// The 指引 dialog body — one source of truth for the three landing spots
// (docs/agent-config.md, this dialog, README §期望信号). The first line names
// the enable path verbatim so walkthrough greps stay stable.
const COPY = {
	en: {
		"hint.credentials":
			"Record credential NAMES only; the values belong in Settings → Keys (PUT /api/env), never in config.yaml.",
		"guide.openUi":
			"Desktop Hermes → Settings → Skills & Tools → Plugins → open hermes-federated-search (the unified package with .hermes-package.json; its desktop half is off by default) → Settings → Plugins → \"Federated Search\". Entry missing: ⌘K → \"Reload desktop plugins\".",
		"guide.addProvider":
			"①\"＋ Add line\" on the card page; ②fill in name and base_url (search_path/query_param/auth_style optional); ③the Key field takes an environment-variable NAME (^[A-Z][A-Z0-9_]{0,127}$, e.g. TAVILY_API_KEY; one per line for a pool); the values go through Settings → Keys (write the same-named variable).",
		"language.label": "Interface language",
		"language.en": "English",
		"language.zh": "中文",
		"problem.rankerNotObject": (label) =>
			`${label} is not an object — treated as read-only; the current value is never overwritten.`,
		"problem.backendsNotList":
			"settings.backends is not a list — treated as read-only; the current value is never overwritten.",
		"problem.backendNotObject": (index) =>
			`settings.backends[${index}] is not an object — this line was not loaded.`,
		"problem.rankingMode":
			"ranking_mode is not one of the allowed choices — fell back to the default auto.",
		"problem.poolStrategy":
			"pool_strategy is not one of the allowed choices — fell back to the default fill_first.",
		"problem.uiLanguage":
			"ui_language is not one of the allowed choices (en|zh) — fell back to the default en.",
		"line.label": (name) => `Line \"${name}\"`,
		"line.labelIndex": (index) => `Line ${index}`,
		"problem.nameRequired": (label) => `${label}: name must not be empty.`,
		"problem.customBaseUrl": (label) => `${label}: a custom line must have a base_url.`,
		"problem.badEnvName": (label, keyIndex) =>
			`${label}, key #${keyIndex}: not a valid environment-variable name ` +
			"(should look like ^[A-Z][A-Z0-9_]{0,127}$; pasted secret values are never accepted).",
		"k.mismatch": (k, lineCount) =>
			`The config sets an explicit k=${k}, but ${lineCount} line(s) are configured. ` +
			"The provider requires k to equal the line count, or a search reports " +
			"\"k=… but … backend(s) configured\". This page never rewrites k on its own — " +
			`set it to ${lineCount} (here or in Agent settings).`,
		"config.header": (version) =>
			`# plugins.entries.${PLUGIN_KEY}.settings (Federated Search v${version})`,
		"parse.empty": "The config string is empty.",
		"parse.badJson": (message) => `Not valid JSON: ${message}`,
		"key.ariaInclude": "Included",
		"key.unnamed": "(unnamed)",
		"key.remove": "Remove",
		"line.namePlaceholder": "Line name (name)",
		"line.include": "Included in federation",
		"keys.heading": (hint) => `Key (environment-variable name): ${hint}`,
		"keys.none": "(no keys yet — a line without credentials is skipped)",
		"keys.add": "＋ Add Key",
		"line.poolLabel": "Key rotation (pool_strategy)",
		"pool.inherit": "Follow global (no key written)",
		"pool.unknown":
			"The current value is not a valid choice (the provider rejects it); it is preserved unless edited.",
		"ranker.modelLabel": "Ranking model (ranker.model)",
		"ranker.catalogError":
			"The model catalog is unavailable (update the desktop app). The ranker key can be edited right here via \"Import from string\", or under Settings → Skills & Tools → Plugins → hermes-federated-search → Agent settings; secret values go through Settings → Keys.",
		"ranker.broken":
			"ranker is not an object — treated as read-only; it is not overwritten unless edited.",
		"ranker.providerLabel": "Ranking provider (ranker.provider)",
		"ranker.providerUnset": "Not set (llm uses the default auxiliary model)",
		"ranker.modelUnset": "Not set (follows the provider default)",
		"ranker.reasoning":
			"Rerank requests always set reasoning_effort=off (model thinking off): faster, steadier ranking with fewer timeout fallbacks to keyword.",
		"ranker.noProviders":
			"No provider configured — the ranker stays read-only at its current value until edited.",
		"ranker.autoNote":
			"Not set: auto uses keyword; choosing a provider writes settings.ranker (other ranker keys are kept).",
		"ranker.promptLabel": "Rerank prompt (ranker.prompt)",
		"ranker.promptHint":
			"Sent as the ranking system prompt (the fixed JSON-array output requirement is appended); empty = the built-in default prompt. Used only when LLM ranking runs (auto with a provider, or llm).",
		"ranker.promptUnset":
			"Not set — the built-in default prompt is used (relevance order; output is a fixed JSON index array).",
		"ranker.promptSave": "Save",
		"ranker.promptReset": "Restore default (clear the key)",
		"ranker.promptAgentNote":
			"This item can also be configured by an agent: write the text to settings.ranker.prompt (full key plugins.entries.hermes-federated-search.settings.ranker.prompt; a YAML block scalar prompt: | works). Doors: \"Import from string\" on this page, the Agent settings sub-page, or the agent editing the config string — writing conventions in docs/agent-config.md.",
		"options.heading": "Aggregation options",
		"options.minBackends": "Min successful lines (min_backends)",
		"options.minBackendsHint":
			"At least N lines must return results, or the whole search fails; a line counts as successful when it answers with results within the timeout. Lines marked required must succeed.",
		"options.timeout": "Timeout (seconds, timeout)",
		"options.timeoutHint":
			"The timeout is the overall deadline for all lines; lines that miss it are dropped, the lines that answered make up a partial result.",
		"options.maxResults": "Max results (max_results)",
		"options.rankingMode": "Ranking mode (ranking_mode)",
		"options.rankingHint":
			"auto follows the ranker config (unset → keyword; set → LLM with keyword fallback on failure); keyword/llm force one mode.",
		"configString.heading": (version) =>
			`Current config string (v${version}) — the tree agents edit`,
		"configString.tip":
			"docs/agent-config.md is the single authoritative entry: which tree to edit, key semantics, and why values go through Settings → Keys.",
		"configString.guideButton": "Guide",
		"configString.dialogAria": "Configuration guide",
		"configString.dialogTitle": "Configuration guide: open the page, add a provider, fill in keys",
		"configString.dialogClose": "Got it",
		"configString.aria": "Current config string",
		"configString.footer":
			"The settings tree is the single source of truth; the legacy web.federated config is a read-only fallback this page never rewrites.",
		"empty.title": "No search lines yet",
		"empty.description":
			"With no lines configured, a search reports \"no search backends configured\" (provider.py:265). Use \"＋ Add line\" below to create the first one.",
		"error.title": "Federated Search config could not be read",
		"error.missing": "plugins.manage returned no config.",
		"lines.add": "＋ Add line",
		"paste.heading": "Import from string (paste that JSON, writes back to the settings tree)",
		"paste.aria": "Config string input",
		"paste.placeholder": '{ "backends": [ … ], "timeout": 10, … }',
		"paste.apply": "Apply",
		"load.missing": (key) =>
			`plugins.manage list has no ${key} — the plugin is not installed or not enabled.`,
		"write.rejected": "the settings write was rejected",
		"toast.saveFailed": "Federated Search: save failed (the draft was kept)",
		"note.damaged": (problems) =>
			`Damaged config: ${problems} (read-only — the stored value is never overwritten; fix it via Agent settings)`,
		"page.intro":
			"This page edits the settings tree of the profile the gateway serves. Every change is written back and read again right away.",
		"save.failed": (error) => `Save failed: ${error}`,
		"raw.aria": "Raw config (read-only)",
		"delete.title": "Delete this line?",
		"delete.description":
			"The line is removed from settings.backends (the legacy web.federated config is kept as-is, never rewritten). Searches follow the new config right away.",
		"common.delete": "Delete",
		"page.title": "Federated Search"
	},
	zh: {
		"hint.credentials":
			"凭据只录环境变量名；值请在 Settings → Keys 录入（PUT /api/env），不要写进 config.yaml。",
		"guide.openUi":
			"桌面 Hermes → 设置 → 技能与工具 → 插件 → 打开 hermes-federated-search（带 .hermes-package.json 的统一包，桌面半默认不启用）→ 设置 → 插件 →「Federated Search」。条目未出现：⌘K →「Reload desktop plugins」。",
		"guide.addProvider":
			"①卡片页「＋ 添加线路」；②填 name 与 base_url（可选 search_path/query_param/auth_style）；③Key 字段填环境变量名（^[A-Z][A-Z0-9_]{0,127}$，如 TAVILY_API_KEY；多 Key 一行一个）；密钥值走 设置 → Keys（写同名变量）。",
		"language.label": "界面语言",
		"language.en": "English",
		"language.zh": "中文",
		"problem.rankerNotObject": (label) =>
			`${label} 不是对象——已按只读处理，不会覆盖原值。`,
		"problem.backendsNotList": "settings.backends 不是列表——已按只读处理，不会覆盖原值。",
		"problem.backendNotObject": (index) => `settings.backends[${index}] 不是对象——该行未载入。`,
		"problem.rankingMode": "ranking_mode 不在可选值内——已回退到默认 auto。",
		"problem.poolStrategy": "pool_strategy 不在可选值内——已回退到默认 fill_first。",
		"problem.uiLanguage": "ui_language 不在可选值内（en|zh）——已回退到默认 en。",
		"line.label": (name) => `线路「${name}」`,
		"line.labelIndex": (index) => `第 ${index} 条线路`,
		"problem.nameRequired": (label) => `${label}：name 不能为空。`,
		"problem.customBaseUrl": (label) => `${label}：custom 线路必须有 base_url。`,
		"problem.badEnvName": (label, keyIndex) =>
			`${label} 第 ${keyIndex} 个 Key：不是合法的环境变量名` +
			"（应形如 ^[A-Z][A-Z0-9_]{0,127}$；粘贴的密钥值一律不接受）。",
		"k.mismatch": (k, lineCount) =>
			`配置里有显式的 k=${k}，但当前有 ${lineCount} 条线路。` +
			"provider 要求 k 恰好等于线路数，否则搜索会报「k=… but … backend(s) configured」。 " +
			`界面不会自动改写 k——请改成 ${lineCount}（或在 Agent settings / agent 侧改写）。`,
		"config.header": (version) =>
			`# plugins.entries.${PLUGIN_KEY}.settings（Federated Search v${version}）`,
		"parse.empty": "配置字符串为空。",
		"parse.badJson": (message) => `不是合法 JSON：${message}`,
		"key.ariaInclude": "参与",
		"key.unnamed": "（未命名）",
		"key.remove": "移除",
		"line.namePlaceholder": "线路名（name）",
		"line.include": "参与聚合",
		"keys.heading": (hint) => `Key（环境变量名）：${hint}`,
		"keys.none": "（还没有 Key——没有凭据时该线路会被跳过）",
		"keys.add": "＋ 添加 Key",
		"line.poolLabel": "Key 轮换策略（pool_strategy）",
		"pool.inherit": "跟随全局（不写键）",
		"pool.unknown": "当前值不在可选内（provider 校验会拒绝）；未改动则原样保留。",
		"ranker.modelLabel": "排序模型（ranker.model）",
		"ranker.catalogError":
			"模型目录不可用（需更新桌面端）。ranker 键可在本页「从字符串导入」或 设置 → 技能与工具 → 插件 → hermes-federated-search → Agent settings 里直接改；密钥值走 Settings → Keys。",
		"ranker.broken": "ranker 不是对象——已按只读处理，未改动不会覆盖原值。",
		"ranker.providerLabel": "排序 provider（ranker.provider）",
		"ranker.providerUnset": "未设置（llm 走默认辅助模型）",
		"ranker.modelUnset": "未设置（跟随 provider 默认）",
		"ranker.reasoning":
			"重排序请求固定设置 reasoning_effort=off（关闭模型思考）：排序更快更稳，避免超时回退关键词。",
		"ranker.noProviders": "未配置任何 provider——ranker 只读现值，未改动则原样保留。",
		"ranker.autoNote": "未设置：auto 将走关键词；选择 provider 后写回 settings.ranker（其余 ranker 键保留）。",
		"ranker.promptLabel": "重排提示词（ranker.prompt）",
		"ranker.promptHint":
			"作为重排的 system 提示词发送（其后自动追加固定的 JSON 输出要求）；留空＝内置默认提示词。仅 LLM 排序（auto 且已选 provider，或 llm）使用。",
		"ranker.promptUnset":
			"未设置——使用内置默认提示词（按相关度排序；输出固定为 JSON 索引数组）。",
		"ranker.promptSave": "保存",
		"ranker.promptReset": "恢复默认（清除该项）",
		"ranker.promptAgentNote":
			"该项也可由 agent 配置：把文本写进 settings.ranker.prompt（完整路径 plugins.entries.hermes-federated-search.settings.ranker.prompt；YAML 可写多行块 prompt: |）。入口＝本页「从字符串导入」、Agent settings 子页，或 agent 直改配置字符串；写法见 docs/agent-config.md。",
		"options.heading": "聚合选项",
		"options.minBackends": "最少成功线路（min_backends）",
		"options.minBackendsHint":
			"本次「有结果返回」的线路数须 ≥N，不足则整次搜索失败；成功＝线路在时限内返回了有结果的响应。required 线路必须成功。",
		"options.timeout": "超时（秒，timeout）",
		"options.timeoutHint": "超时（timeout）＝等全部线路返回的总时限；到期未返回的线路丢弃、已返回的线路凑部分结果。",
		"options.maxResults": "输出条数（max_results）",
		"options.rankingMode": "排序方式（ranking_mode）",
		"options.rankingHint": "auto＝按 ranker 配置：未配置→关键词；配置→LLM＋失败回退关键词；keyword／llm 为强制模式。",
		"configString.heading": (version) => `当前配置字符串（v${version}）——agent 直接改这棵树`,
		"configString.tip": "docs/agent-config.md 是唯一权威入口：改哪棵树、键语义、值走 Settings → Keys。",
		"configString.guideButton": "指引",
		"configString.dialogAria": "配置指引",
		"configString.dialogTitle": "配置指引：打开界面、加 provider、填 Key",
		"configString.dialogClose": "知道了",
		"configString.aria": "当前配置字符串",
		"configString.footer": "settings 树是唯一真源；web.federated 旧配置只作只读兜底、本页不改写。",
		"empty.title": "还没有搜索线路",
		"empty.description":
			"空配置时搜索会报「no search backends configured」（provider.py:265）。点下面的「＋ 添加线路」建第一条。",
		"error.title": "Federated Search 配置读不出来",
		"error.missing": "plugins.manage 没有返回配置。",
		"lines.add": "＋ 添加线路",
		"paste.heading": "从字符串导入（粘贴上面那种 JSON，写回 settings 树）",
		"paste.aria": "配置字符串输入",
		"paste.placeholder": '{ "backends": [ … ], "timeout": 10, … }',
		"paste.apply": "应用",
		"load.missing": (key) => `plugins.manage list 里没有 ${key}——插件未安装或未启用。`,
		"write.rejected": "settings 被拒绝",
		"toast.saveFailed": "Federated Search：保存失败（草稿已保留）",
		"note.damaged": (problems) => `字符串非法/损坏：${problems}（已只读——不会覆盖原值；可用 Agent settings 修复）`,
		"page.intro": "本页作用于当前网关 profile 的 settings 树。改了立即写回并回读。",
		"save.failed": (error) => `保存失败：${error}`,
		"raw.aria": "原始配置（只读）",
		"delete.title": "删除这条线路？",
		"delete.description": "会从 settings.backends 移除该线路（legacy web.federated 原样保留、不改写）。搜索随即按新配置生效。",
		"common.delete": "删除",
		"page.title": "Federated Search"
	}
};

const OPTION_KEYS = ["min_backends", "ranking_mode", "pool_strategy", "timeout", "max_results"];
const DEFAULT_OPTIONS = {
	min_backends: 1,
	ranking_mode: "auto",
	pool_strategy: "fill_first",
	timeout: 10,
	max_results: 8,
	ui_language: UI_LANGUAGE_DEFAULT
};
const RANKING_MODES = ["auto", "keyword", "llm"];
const POOL_STRATEGIES = ["fill_first", "round_robin", "random"];
// Radix selects cannot carry an empty-string item; these sentinels map to
// "write nothing" (per-line 跟随全局) and "unset" (ranker provider/model).
const POOL_INHERIT = "__inherit__";
const RANKER_UNSET = "__unset__";
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

function emptyRanker() {
	return { present: false, broken: false, tree: {}, provider: "", model: "", prompt: "" };
}

function emptyDraft() {
	return {
		backends: [],
		explicitK: null,
		legacyK: null,
		options: { ...DEFAULT_OPTIONS },
		ranker: emptyRanker()
	};
}

const isPlainObject = (value) =>
	!!value && typeof value === "object" && !Array.isArray(value);

/** Ranker read: settings.ranker wins (the provider's merge is per top-level
 *  key), legacy `web.federated.ranker` is the fallback. A non-object node is
 *  reported as a problem and the write path stays away from it. */
function readRanker(settingsRanker, legacyRanker, lang) {
	const l = renderLang(lang);
	const ranker = emptyRanker();
	const problems = [];
	let source = null;
	let label = null;
	if (settingsRanker != null) {
		source = settingsRanker;
		label = "settings.ranker";
	} else if (legacyRanker != null) {
		source = legacyRanker;
		label = "web.federated.ranker";
	}
	if (source != null) {
		if (isPlainObject(source)) {
			ranker.present = true;
			ranker.tree = { ...source };
			ranker.provider = asText(source.provider);
			ranker.model = asText(source.model);
			ranker.prompt = asText(source.prompt);
		} else {
			ranker.broken = true;
			problems.push(t("problem.rankerNotObject", l, label));
		}
	}
	return { ranker, problems };
}

/** Ranker write: whole-tree merge (unknown keys stay), provider / model /
 *  prompt leaves replaced — clearing a field deletes its leaf (restore
 *  default); a blank prompt counts as unset. The key is omitted when nothing
 *  existed and nothing was chosen (no automatic write), and stays untouched
 *  when the read tree was not an object. */
function rankerFromDraft(ranker) {
	if (!ranker || ranker.broken) return undefined;
	const tree = { ...(ranker.tree || {}) };
	delete tree.provider;
	delete tree.model;
	delete tree.prompt;
	const provider = asText(ranker.provider).trim();
	const model = asText(ranker.model).trim();
	const prompt = asText(ranker.prompt).trim();
	if (provider) tree.provider = provider;
	if (model) tree.model = model;
	if (prompt) tree.prompt = prompt;
	if (!ranker.present && !provider && !model && !prompt) return undefined;
	return tree;
}

/** One settings-tree line → one draft line. `raw` keeps extension keys
 *  (api_key_env, required, unknown keys, …) so an edit in the UI never drops
 *  a key this page does not render. `type` keeps the source verbatim —
 *  missing stays missing (registered semantics in provider.py); the page no
 *  longer injects "custom". */
function lineFromValue(source) {
	const raw = source && typeof source === "object" && !Array.isArray(source) ? { ...source } : {};
	const line = {
		name: asText(raw.name),
		type: asText(raw.type),
		credentials: Array.isArray(raw.credentials)
			? raw.credentials.filter((item) => typeof item === "string")
			: [],
		pool_strategy: asText(raw.pool_strategy),
		raw
	};
	for (const field of CUSTOM_FIELDS) line[field.key] = asText(raw[field.key]);
	return line;
}

/** Draft line → settings-tree line. Unknown keys round-trip unchanged; known
 *  keys are only emitted when non-empty or already present in the source.
 *  Per-line `pool_strategy`: a chosen strategy writes; emptying the select
 *  (跟随全局) deletes the key; a legacy empty string left untouched
 *  round-trips as-is. */
function lineToValue(line) {
	const raw = line.raw && typeof line.raw === "object" ? line.raw : {};
	const out = { name: line.name };
	if (line.type) out.type = line.type;
	for (const [key, value] of Object.entries(raw)) {
		if (key === "name" || key === "type" || key === "credentials" || key === "pool_strategy") continue;
		if (CUSTOM_FIELDS.some((field) => field.key === key)) continue;
		out[key] = value;
	}
	for (const field of CUSTOM_FIELDS) {
		const value = asText(line[field.key]);
		if (value !== "" || owns(raw, field.key)) out[field.key] = value;
	}
	const poolRaw = owns(raw, "pool_strategy") ? asText(raw.pool_strategy) : null;
	const pool = asText(line.pool_strategy);
	if (pool !== "") out.pool_strategy = pool;
	else if (poolRaw === "") out.pool_strategy = "";
	if (line.credentials.length > 0 || owns(raw, "credentials")) {
		out.credentials = [...line.credentials];
	}
	return out;
}

/** Settings-tree values ({key: value} as `plugins.manage list` reports them)
 *  → draft. `extras` carries the raw-config reads (settings.ranker /
 *  web.federated.ranker) that `plugins.manage list` cannot distinguish from a
 *  schema default. Tolerant: a damaged tree yields problems, never a crash,
 *  and the caller keeps the raw text read-only instead of overwriting it. */
function draftFromValues(values, extras) {
	const draft = emptyDraft();
	const problems = [];
	const source = values && typeof values === "object" && !Array.isArray(values) ? values : {};
	// The widget's own language, read like any other option: absent or a junk
	// value lands on the default (en); junk is also reported, en-locale.
	const lang = resolveLang(source.ui_language);
	draft.options.ui_language = lang;
	if (asText(source.ui_language) !== "" && !UI_LANGUAGES.includes(asText(source.ui_language))) {
		problems.push(t("problem.uiLanguage", lang));
	}
	const rawBackends = source.backends;
	if (rawBackends != null && !Array.isArray(rawBackends)) {
		problems.push(t("problem.backendsNotList", lang));
	} else if (Array.isArray(rawBackends)) {
		rawBackends.forEach((raw, index) => {
			if (raw && typeof raw === "object" && !Array.isArray(raw)) {
				draft.backends.push(lineFromValue(raw));
			} else {
				problems.push(t("problem.backendNotObject", lang, index));
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
		problems.push(t("problem.rankingMode", lang));
	}
	const pool = asText(source.pool_strategy) || DEFAULT_OPTIONS.pool_strategy;
	if (POOL_STRATEGIES.includes(pool)) {
		draft.options.pool_strategy = pool;
	} else {
		draft.options.pool_strategy = DEFAULT_OPTIONS.pool_strategy;
		problems.push(t("problem.poolStrategy", lang));
	}
	const rankerLoaded = readRanker(
		extras ? extras.settingsRanker ?? null : source.ranker ?? null,
		extras ? extras.legacyRanker ?? null : null,
		lang
	);
	draft.ranker = rankerLoaded.ranker;
	problems.push(...rankerLoaded.problems);
	draft.explicitK = typeof source.k === "number" && Number.isFinite(source.k) ? source.k : null;
	return { draft, problems };
}

/** Draft → the values payload written through `plugins.manage settings`.
 *  Only config_schema keys, `k` is never written (absent k = number of
 *  lines, which is what the UI always produces); `ranker` is emitted only
 *  when it exists or was explicitly configured. */
function settingsFromDraft(draft) {
	const ranking = asText(draft.options.ranking_mode);
	const pool = asText(draft.options.pool_strategy);
	const payload = {
		backends: draft.backends.map(lineToValue),
		timeout: asInt(draft.options.timeout, DEFAULT_OPTIONS.timeout),
		max_results: asInt(draft.options.max_results, DEFAULT_OPTIONS.max_results),
		min_backends: asInt(draft.options.min_backends, DEFAULT_OPTIONS.min_backends),
		ranking_mode: RANKING_MODES.includes(ranking) ? ranking : DEFAULT_OPTIONS.ranking_mode,
		pool_strategy: POOL_STRATEGIES.includes(pool) ? pool : DEFAULT_OPTIONS.pool_strategy,
		ui_language: UI_LANGUAGES.includes(asText(draft.options.ui_language))
			? asText(draft.options.ui_language)
			: UI_LANGUAGE_DEFAULT
	};
	const ranker = rankerFromDraft(draft.ranker);
	if (ranker !== undefined) payload.ranker = ranker;
	return payload;
}

/** Draft problems that must block a write (S4). Messages carry the item
 *  position and the rule — never the offending value. */
function validateDraft(draft, lang) {
	const l = renderLang(lang);
	const problems = [];
	draft.backends.forEach((line, index) => {
		const label = line.name ? t("line.label", l, line.name) : t("line.labelIndex", l, index + 1);
		if (!asText(line.name).trim()) problems.push(t("problem.nameRequired", l, label));
		if (line.type === "custom" && !asText(line.base_url).trim()) {
			problems.push(t("problem.customBaseUrl", l, label));
		}
		line.credentials.forEach((env, keyIndex) => {
			if (!ENV_NAME_RE.test(env)) {
				problems.push(t("problem.badEnvName", l, label, keyIndex + 1));
			}
		});
	});
	return problems;
}

/** S12: an explicit `k` (settings tree wins over legacy, per top-level key —
 *  provider.py merges that way) that disagrees with the line count makes the
 *  provider refuse the config. Warn; never rewrite it from the UI. */
function kMismatchWarning(k, lineCount, lang) {
	if (typeof k !== "number" || !Number.isFinite(k) || k === lineCount) return null;
	return t("k.mismatch", renderLang(lang), k, lineCount);
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
		options: { ...draft.options },
		ranker: {
			present: !!(draft.ranker && draft.ranker.present),
			broken: !!(draft.ranker && draft.ranker.broken),
			tree: { ...((draft.ranker && draft.ranker.tree) || {}) },
			provider: draft.ranker ? asText(draft.ranker.provider) : "",
			model: draft.ranker ? asText(draft.ranker.model) : "",
			prompt: draft.ranker ? asText(draft.ranker.prompt) : ""
		}
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

function updateRanker(draft, field, value) {
	const next = cloneDraft(draft);
	next.ranker[field] = value;
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

function draftToConfigText(draft, lang) {
	const l = renderLang(lang);
	return [
		t("config.header", l, WIDGET_VERSION),
		`# ${t("hint.credentials", l)}`,
		JSON.stringify(settingsFromDraft(draft), null, 2)
	].join("\n");
}

/** String → draft (paste-back door). Comments are ignored; a malformed string
 *  reports an error and yields no draft, so nothing can be written from it. */
function parseConfigText(text, lang) {
	const l = renderLang(lang);
	const body = asText(text)
		.split("\n")
		.filter((line) => !line.trimStart().startsWith("#"))
		.join("\n")
		.trim();
	if (!body) return { ok: false, error: t("parse.empty", l), draft: null, problems: [] };
	let parsed = null;
	try {
		parsed = JSON.parse(body);
	} catch (error) {
		return {
			ok: false,
			error: t("parse.badJson", l, error && error.message ? error.message : String(error)),
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

function keyCardView(line, lineIndex, keyIndex, env, handlers, lang) {
	const l = renderLang(lang);
	return jsxs(
		"div",
		{
			className:
				"flex items-center gap-2 rounded-md border border-(--ui-stroke-tertiary) px-2 py-1",
			children: [
				jsx(Checkbox, {
					checked: true,
					"aria-label": t("key.ariaInclude", l),
					onCheckedChange: () => handlers.onExcludeKey(lineIndex, keyIndex)
				}),
				jsx("span", { className: "min-w-0 truncate font-mono text-xs", children: env ? env : t("key.unnamed", l) }),
				jsx(Button, {
					variant: "ghost",
					size: "sm",
					onClick: () => handlers.onExcludeKey(lineIndex, keyIndex),
					children: t("key.remove", l)
				})
			]
		},
		`key-${lineIndex}-${keyIndex}`
	);
}

function lineCardView(line, index, handlers, lang) {
	const l = renderLang(lang);
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
						placeholder: t("line.namePlaceholder", l),
						className: "max-w-56",
						onChange: (event) => handlers.onField(index, "name", event.target.value)
					}),
					jsxs("label", {
						className: "ml-auto flex items-center gap-2 text-xs text-(--ui-text-secondary)",
						children: [
							jsx(Checkbox, {
								checked: true,
								"aria-label": t("line.include", l),
								onCheckedChange: () => handlers.onExcludeLine(index)
							}),
							t("line.include", l)
						]
					}),
					jsx(Button, {
						variant: "ghost",
						size: "sm",
						onClick: () => handlers.onRequestDelete(index),
						children: t("common.delete", l)
					})
				] },
				`head-${index}`
			),
			jsxs(
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
			),
			jsxs(
				"div",
				{ className: "grid gap-2", children: [
					jsx("div", {
						className: "text-xs text-(--ui-text-secondary)",
						children: t("keys.heading", l, t("hint.credentials", l))
					}),
					line.credentials.length === 0
						? jsx("div", {
								className: "text-xs text-(--ui-text-tertiary)",
								children: t("keys.none", l)
							})
						: jsxs(
								"div",
								{ className: "grid gap-1", children: line.credentials.map((env, keyIndex) =>
									keyCardView(line, index, keyIndex, env, handlers, l)
								) },
								`keys-${index}`
							),
					jsx(Button, {
						variant: "ghost",
						size: "sm",
						onClick: () => handlers.onAddKey(index),
						children: t("keys.add", l)
					}),
					jsxs("label", { className: "grid gap-1 text-xs text-(--ui-text-secondary)", children: [
						t("line.poolLabel", l),
						jsx(Select, {
							value: line.pool_strategy ? line.pool_strategy : POOL_INHERIT,
							onValueChange: (value) =>
								handlers.onLinePool(index, value === POOL_INHERIT ? "" : value),
							children: jsxs(SelectTrigger, { "aria-label": "pool_strategy", children: [
								jsx(SelectValue, {}),
								jsx(SelectContent, { children: [
									jsx(SelectItem, { value: POOL_INHERIT, children: t("pool.inherit", l) }, "pool-inherit"),
									...POOL_STRATEGIES.map((policy) =>
										jsx(SelectItem, { value: policy, children: policy }, `pool-${policy}`)
									)
								] })
							] })
						}),
						line.pool_strategy && !POOL_STRATEGIES.includes(line.pool_strategy)
							? jsx("span", {
									className: "text-xs text-(--ui-text-tertiary)",
									children: t("pool.unknown", l)
								})
							: null
					] }, `pool-${index}`)
				] }
			)
		] },
		`line-${index}`
	);
}

/** Rerank prompt box: a local draft committed by Save. A multiline field must
 *  not write on every keystroke — each write round-trips the settings tree and
 *  re-reads it (SSOT), which would eat trailing whitespace mid-typing — so the
 *  explicit Save is the commit point; Restore default clears the leaf. While
 *  the leaf is unset the box shows the built-in default as the effective value
 *  (placeholder), matching what the provider will send. */
function RankerPromptBox({ value, onApply, onReset, lang }) {
	const l = renderLang(lang);
	const [text, setText] = useState(value);
	useEffect(() => {
		setText(value);
	}, [value]);
	return jsxs("div", { className: "grid gap-2", children: [
		jsx("div", { className: "text-xs text-(--ui-text-secondary)", children: t("ranker.promptLabel", l) }),
		jsx(Textarea, {
			value: text,
			placeholder: DEFAULT_RANKER_PROMPT,
			"aria-label": "ranker.prompt",
			className: "font-mono text-xs",
			rows: 4,
			onChange: (event) => setText(event.target.value)
		}),
		value === ""
			? jsx("div", { className: "text-xs text-(--ui-text-tertiary)", children: t("ranker.promptUnset", l) })
			: null,
		jsx("div", { className: "text-xs text-(--ui-text-tertiary)", children: t("ranker.promptHint", l) }),
		jsxs("div", { className: "flex flex-wrap items-center gap-2", children: [
			jsx(Button, {
				variant: "secondary",
				size: "sm",
				onClick: () => onApply(text),
				children: t("ranker.promptSave", l)
			}),
			jsx(Button, {
				variant: "ghost",
				size: "sm",
				onClick: () => {
					setText("");
					onReset();
				},
				children: t("ranker.promptReset", l)
			})
		]}),
		jsx("div", { className: "text-xs text-(--ui-text-tertiary)", children: t("ranker.promptAgentNote", l) })
	]});
}

function rankerView(draft, catalog, handlers, lang) {
	const l = renderLang(lang);
	const info = draft.ranker || emptyRanker();
	if (draft.options.ranking_mode === "keyword") return null;
	const providers = catalog && Array.isArray(catalog.providers) ? catalog.providers : [];
	const phase = (catalog && catalog.phase) || "loading";
	const selected = providers.find((item) => item.slug === info.provider) || null;
	const models = selected && Array.isArray(selected.models) ? selected.models : [];
	return jsxs(
		"div",
		{ className: "grid gap-2 rounded-md border border-(--ui-stroke-tertiary) p-2", children: [
			jsx("div", { className: "text-xs text-(--ui-text-secondary)", children: t("ranker.modelLabel", l) }),
			phase === "error"
				? warningBar(t("ranker.catalogError", l))
				: null,
			info.broken
				? warningBar(t("ranker.broken", l))
				: null,
			jsxs("div", { className: "grid gap-2 sm:grid-cols-2", children: [
				jsx("label", { className: "grid gap-1 text-xs text-(--ui-text-secondary)", children: [
					t("ranker.providerLabel", l),
					jsx(Select, {
						value: info.provider ? info.provider : RANKER_UNSET,
						onValueChange: (value) => handlers.onRankerProvider(value === RANKER_UNSET ? "" : value),
						children: jsxs(SelectTrigger, { "aria-label": "ranker.provider", children: [
							jsx(SelectValue, {}),
							jsx(SelectContent, { children: [
								jsx(SelectItem, { value: RANKER_UNSET, children: t("ranker.providerUnset", l) }, "ranker-provider-unset"),
								...providers.map((item) =>
									jsx(SelectItem, { value: item.slug, children: item.slug }, `ranker-provider-${item.slug}`)
								)
							] })
						] })
					})
				] }),
				jsx("label", { className: "grid gap-1 text-xs text-(--ui-text-secondary)", children: [
					t("ranker.modelLabel", l),
					jsx(Select, {
						value: info.model ? info.model : RANKER_UNSET,
						onValueChange: (value) => handlers.onRankerModel(value === RANKER_UNSET ? "" : value),
						children: jsxs(SelectTrigger, { "aria-label": "ranker.model", children: [
							jsx(SelectValue, {}),
							jsx(SelectContent, { children: [
								jsx(SelectItem, { value: RANKER_UNSET, children: t("ranker.modelUnset", l) }, "ranker-model-unset"),
								...models.map((name) =>
									jsx(SelectItem, { value: name, children: name }, `ranker-model-${name}`)
								)
							] })
						] })
					})
				] })
			] }),
			jsx(RankerPromptBox, {
				value: info.prompt,
				onApply: handlers.onRankerPromptApply,
				onReset: handlers.onRankerPromptReset,
				lang: l
			}),
			jsx("div", { className: "text-xs text-(--ui-text-tertiary)", children: t("ranker.reasoning", l) }),
			phase === "empty"
				? jsx("div", { className: "text-xs text-(--ui-text-tertiary)", children: t("ranker.noProviders", l) })
				: null,
			!info.provider && draft.options.ranking_mode === "auto"
				? jsx("div", { className: "text-xs text-(--ui-text-tertiary)", children: t("ranker.autoNote", l) })
				: null
		] }
	);
}

function optionsView(draft, catalog, handlers, lang) {
	const l = renderLang(lang);
	return jsxs(
		"div",
		{ className: "grid gap-3 rounded-lg border border-(--ui-stroke-tertiary) p-3", children: [
			jsx("div", { className: "text-xs font-medium text-(--ui-text-secondary)", children: t("options.heading", l) }),
			jsxs("div", { className: "grid gap-3 sm:grid-cols-3", children: [
				jsx("label", { className: "grid gap-1 text-xs text-(--ui-text-secondary)", children: [
					t("options.minBackends", l),
					jsx(Input, {
						value: String(draft.options.min_backends),
						"aria-label": "min_backends",
						onChange: (event) => handlers.onOption("min_backends", event.target.value)
					}),
					jsx("span", { className: "text-xs text-(--ui-text-tertiary)", children: t("options.minBackendsHint", l) })
				] }),
				jsx("label", { className: "grid gap-1 text-xs text-(--ui-text-secondary)", children: [
					t("options.timeout", l),
					jsx(Input, {
						value: String(draft.options.timeout),
						"aria-label": "timeout",
						onChange: (event) => handlers.onOption("timeout", event.target.value)
					}),
					jsx("span", { className: "text-xs text-(--ui-text-tertiary)", children: t("options.timeoutHint", l) })
				] }),
				jsx("label", { className: "grid gap-1 text-xs text-(--ui-text-secondary)", children: [
					t("options.maxResults", l),
					jsx(Input, {
						value: String(draft.options.max_results),
						"aria-label": "max_results",
						onChange: (event) => handlers.onOption("max_results", event.target.value)
					})
				] })
			] }),
			jsxs("div", { className: "grid gap-3 sm:grid-cols-2", children: [
				jsx("label", { className: "grid gap-1 text-xs text-(--ui-text-secondary)", children: [
					t("options.rankingMode", l),
					jsx(Select, {
						value: draft.options.ranking_mode,
						onValueChange: (value) => handlers.onOption("ranking_mode", value),
						children: jsxs(SelectTrigger, { "aria-label": "ranking_mode", children: [
							jsx(SelectValue, {}),
							jsx(SelectContent, { children: RANKING_MODES.map((mode) =>
								jsx(SelectItem, { value: mode, children: mode }, `mode-${mode}`)
							) })
						] })
					}),
					jsx("span", { className: "text-xs text-(--ui-text-tertiary)", children: t("options.rankingHint", l) })
				] })
			] }),
			rankerView(draft, catalog, handlers, l)
		] });
}

function configStringView(draft, guide, lang) {
	const l = renderLang(lang);
	const open = !!(guide && guide.open);
	const onOpenChange = (next) => {
		if (guide && typeof guide.setOpen === "function") guide.setOpen(next);
	};
	return jsxs(
		"div",
		{ className: "grid gap-2 rounded-lg border border-(--ui-stroke-tertiary) p-3", children: [
			jsxs("div", { className: "flex items-center gap-2", children: [
				jsx("span", {
					className: "text-xs font-medium text-(--ui-text-secondary)",
					children: t("configString.heading", l, WIDGET_VERSION)
				}),
				jsx(Tip, {
					label: t("configString.tip", l),
					children: jsx(Button, {
						variant: "ghost",
						size: "sm",
						onClick: () => onOpenChange(true),
						children: t("configString.guideButton", l)
					})
				})
			] }),
			jsxs(Dialog, { open, onOpenChange, children: [
				jsx(DialogContent, { "aria-label": t("configString.dialogAria", l), className: "max-w-lg", children: jsxs("div", { className: "grid gap-3", children: [
					jsx(DialogHeader, { children: jsx(DialogTitle, { children: t("configString.dialogTitle", l) }) }),
					jsx("div", { className: "text-xs text-(--ui-text-secondary)", children: t("guide.openUi", l) }),
					jsx("div", { className: "text-xs text-(--ui-text-secondary)", children: t("guide.addProvider", l) }),
					jsx(DialogFooter, { children: jsx(Button, {
						variant: "secondary",
						size: "sm",
						onClick: () => onOpenChange(false),
						children: t("configString.dialogClose", l)
					}) })
				] }) })
			] }),
			jsx(Textarea, {
				readOnly: true,
				value: draftToConfigText(draft, l),
				"aria-label": t("configString.aria", l),
				className: "font-mono text-xs",
				rows: 8
			}),
			jsx("div", {
				className: "text-xs text-(--ui-text-tertiary)",
				children: t("configString.footer", l)
			})
		] });
}

function emptyView(lang) {
	const l = renderLang(lang);
	return jsx(EmptyState, {
		title: t("empty.title", l),
		description: t("empty.description", l)
	});
}

function errorView(message, lang) {
	const l = renderLang(lang);
	return jsx(ErrorState, {
		title: t("error.title", l),
		description: message || t("error.missing", l)
	});
}

function linesView({ draft, k, handlers, note, catalog, guide, lang }) {
	const l = renderLang(lang);
	const warning = kMismatchWarning(k, draft.backends.length, l);
	return jsxs("div", { className: "grid gap-4", children: [
		note ? warningBar(note) : null,
		warning ? warningBar(warning) : null,
		draft.backends.length === 0
			? emptyView(l)
			: jsxs("div", { className: "grid gap-3", children: draft.backends.map((line, index) =>
					lineCardView(line, index, handlers, l)
				) }),
		jsx(Button, { variant: "secondary", size: "sm", onClick: handlers.onAddLine, children: t("lines.add", l) }),
		optionsView(draft, catalog, handlers, l),
		configStringView(draft, guide, l)
	] });
}

function PasteBox({ onApply, lang }) {
	const l = renderLang(lang);
	const [text, setText] = useState("");
	const [note, setNote] = useState(null);
	return jsxs("div", { className: "grid gap-2 rounded-lg border border-(--ui-stroke-tertiary) p-3", children: [
		jsx("div", {
			className: "text-xs font-medium text-(--ui-text-secondary)",
			children: t("paste.heading", l)
		}),
		jsx(Textarea, {
			value: text,
			"aria-label": t("paste.aria", l),
			className: "font-mono text-xs",
			rows: 6,
			placeholder: t("paste.placeholder", l),
			onChange: (event) => setText(event.target.value)
		}),
		jsxs("div", { className: "flex items-center gap-2", children: [
			jsx(Button, {
				variant: "secondary",
				size: "sm",
				disabled: !text.trim(),
				onClick: () => {
					const parsed = parseConfigText(text, l);
					if (!parsed.ok) {
						setNote(parsed.error);
						return;
					}
					setNote(null);
					onApply(parsed.draft);
				},
				children: t("paste.apply", l)
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
	// loaded config — and a schema-declared dict key (ranker) reports its
	// default when unset, so presence is read from the raw config too.
	// Failure = no reading, never a failed page.
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
			legacyK: legacy && typeof legacy.k === "number" && Number.isFinite(legacy.k) ? legacy.k : null,
			settingsRanker: settings.ranker ?? null,
			legacyRanker: legacy ? legacy.ranker ?? null : null
		};
	} catch {
		return { settingsK: null, legacyK: null, settingsRanker: null, legacyRanker: null };
	}
}

async function loadDraft() {
	const listed = await host.request("plugins.manage", { action: "list" });
	const rows = Array.isArray(listed && listed.plugins) ? listed.plugins : [];
	const row = rows.find((item) => item && (item.key === PLUGIN_KEY || item.name === PLUGIN_KEY));
	if (!row) throw new Error(t("load.missing", UI_LANGUAGE_DEFAULT, PLUGIN_KEY));
	const values = {};
	for (const field of row.settings_schema || []) {
		if (field && typeof field.key === "string") values[field.key] = field.value;
	}
	const extras = await readGeometryExtras();
	const { draft, problems } = draftFromValues(values, {
		settingsRanker: extras.settingsRanker,
		legacyRanker: extras.legacyRanker
	});
	draft.explicitK = extras.settingsK;
	draft.legacyK = extras.legacyK;
	return { draft, problems, rawValues: values };
}

async function loadCatalog() {
	// Read door for the desktop model directory (RPC `model.options` — the
	// plugin SDK's host.request proxy; refresh defaults to false). Failure or
	// emptiness never touches the draft: the ranker area shows the current
	// value and the string doors stay usable.
	try {
		const result = await host.request("model.options", { explicit_only: true });
		const providers = Array.isArray(result && result.providers)
			? result.providers
					.map((row) => ({
						slug: asText(row && (row.slug || row.label)),
						models: Array.isArray(row && row.models)
							? row.models
									.map((item) => asText(item && typeof item === "object" ? item.id || item.model || item.name : item))
									.filter((name) => name !== "")
							: []
					}))
					.filter((row) => row.slug !== "")
			: [];
		return { phase: providers.length ? "ok" : "empty", providers };
	} catch {
		return { phase: "error", providers: [] };
	}
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
	const [guideOpen, setGuideOpen] = useState(false);
	const [catalog, setCatalog] = useState({ phase: "loading", providers: [] });

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

	const refreshCatalog = useCallback(async () => {
		setCatalog(await loadCatalog());
	}, []);

	useEffect(() => {
		void refresh();
		void refreshCatalog();
	}, [refresh, refreshCatalog]);

	const write = useCallback(
		async (next) => {
			const lang = resolveLang(next.options.ui_language);
			const problems = validateDraft(next, lang);
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
				if (result && result.ok === false) throw new Error(result.error || t("write.rejected", lang));
				await refresh();
			} catch (error) {
				// Toast + keep the draft so the edit is not lost; the raw config
				// is never overwritten from a failed write.
				host.notifyError(error, t("toast.saveFailed", lang));
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
			onLinePool: (index, value) => {
				if (state.draft) void write(updateLineField(state.draft, index, "pool_strategy", value));
			},
			onOption: (key, value) => {
				if (state.draft) void write(updateOption(state.draft, key, value));
			},
			onLanguage: (value) => {
				if (state.draft) void write(updateOption(state.draft, "ui_language", value));
			},
			onRankerProvider: (value) => {
				if (state.draft) void write(updateRanker(state.draft, "provider", value));
			},
			onRankerModel: (value) => {
				if (state.draft) void write(updateRanker(state.draft, "model", value));
			},
			onRankerPromptApply: (value) => {
				if (state.draft) void write(updateRanker(state.draft, "prompt", value));
			},
			onRankerPromptReset: () => {
				if (state.draft) void write(updateRanker(state.draft, "prompt", ""));
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
	if (state.phase === "error") return errorView(state.saveError, UI_LANGUAGE_DEFAULT);

	const draft = state.draft || emptyDraft();
	const lang = resolveLang(draft.options.ui_language);
	const effectiveK = draft.explicitK == null ? draft.legacyK : draft.explicitK;
	const note = state.problems.length
		? t("note.damaged", lang, state.problems.join(" "))
		: null;

	return jsxs("div", { className: "grid gap-4 p-1", children: [
		jsxs("div", { className: "flex flex-wrap items-center gap-2 text-xs text-(--ui-text-tertiary)", children: [
			jsx("span", { children: t("language.label", lang) }),
			jsx(Select, {
				value: lang,
				onValueChange: handlers.onLanguage,
				children: jsxs(SelectTrigger, { "aria-label": t("language.label", lang), children: [
					jsx(SelectValue, {}),
					jsx(SelectContent, { children: UI_LANGUAGES.map((code) =>
						jsx(SelectItem, { value: code, children: t(`language.${code}`, lang) }, `ui-language-${code}`)
					) })
				] })
			})
		] }),
		jsx("div", {
			className: "text-xs text-(--ui-text-tertiary)",
			children: t("page.intro", lang)
		}),
		jsx(ScrollArea, { children: linesView({
			draft,
			k: effectiveK,
			handlers,
			note,
			catalog,
			guide: { open: guideOpen, setOpen: setGuideOpen },
			lang
		}) }),
		state.saveError ? warningBar(t("save.failed", lang, state.saveError)) : null,
		state.busy ? jsx(Loader, {}) : null,
		state.problems.length
			? jsx(Textarea, {
					readOnly: true,
					value: state.rawText,
					"aria-label": t("raw.aria", lang),
					className: "font-mono text-xs",
					rows: 8
				})
			: null,
		jsx(PasteBox, { onApply: (next) => void write(next), lang }),
		jsx(ConfirmDialog, {
			open: deleteIndex != null,
			onClose: () => setDeleteIndex(null),
			onConfirm: () => {
				if (state.draft && deleteIndex != null) void write(excludeLine(state.draft, deleteIndex));
				setDeleteIndex(null);
			},
			title: t("delete.title", lang),
			description: t("delete.description", lang),
			confirmLabel: t("common.delete", lang),
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
			// Rail label + breadcrumb. Registered once, so it is pinned to the
			// default (en) name — the host has no door to re-title a page.
			title: t("page.title", UI_LANGUAGE_DEFAULT),
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
	DEFAULT_RANKER_PROMPT,
	DEFAULT_OPTIONS,
	POOL_INHERIT,
	emptyDraft,
	emptyRanker,
	readRanker,
	rankerFromDraft,
	draftFromValues,
	settingsFromDraft,
	validateDraft,
	kMismatchWarning,
	cloneDraft,
	excludeLine,
	excludeKey,
	updateLineField,
	updateOption,
	updateRanker,
	addLine,
	addKey,
	draftToConfigText,
	parseConfigText,
	loadDraft,
	loadCatalog,
	readGeometryExtras,
	linesView,
	optionsView,
	rankerView,
	lineCardView,
	keyCardView,
	emptyView,
	errorView,
	ConfigPage
};
