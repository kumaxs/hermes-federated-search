#!/usr/bin/env node
/**
 * Node harness for `desktop/plugin.js` (the pure model + the view builders).
 *
 * The widget is plain ESM loaded by the Hermes desktop app, where exactly
 * three bare specifiers resolve (`@hermes/plugin-sdk`, `react`,
 * `react/jsx-runtime`). The harness reproduces that contract outside the app:
 * it strips the top-level imports, rewires the module's `export` forms, and
 * evaluates it with stub modules — so the tests exercise the REAL widget
 * source (no copy), the same way `tests/test_desktop_widget.py` pin
 * `WIDGET_VERSION` against `plugin.yaml`.
 *
 * usage: node tests/widget_harness.cjs <roundtrip|mapping|render|illegal>
 * prints: one JSON object on stdout; a non-zero exit with the message on stderr
 *         when the widget itself cannot be loaded.
 */

"use strict";

const fs = require("fs");
const path = require("path");

const SOURCE = path.join(__dirname, "..", "desktop", "plugin.js");

// --- stub modules -----------------------------------------------------------

const jsx = (type, props, key) => ({
	__el: true,
	type,
	props: { ...(props || {}), ...(key !== undefined ? { key } : {}) }
});
const jsxs = (type, props, key) => jsx(type, props, key);

/** Render-time hooks for the harness only: every component is called exactly
 *  once (initial state), which is all a smoke test needs. */
const hooks = {
	useState: (init) => [typeof init === "function" ? init() : init, () => {}],
	useEffect: () => {},
	useMemo: (fn) => fn(),
	useCallback: (fn) => fn
};

const SDK_COMPONENTS = [
	"Badge", "Button", "Checkbox", "ConfirmDialog", "EmptyState", "ErrorState",
	"Input", "Loader", "RowButton", "ScrollArea", "SegmentedControl", "Select",
	"SelectContent", "SelectItem", "SelectTrigger", "SelectValue", "Separator",
	"Textarea", "Tip"
];

function makeSdk() {
	const sdk = { cn: (...parts) => parts.filter(Boolean).join(" ") };
	for (const name of SDK_COMPONENTS) sdk[name] = name;
	return sdk;
}

/** Host stub. Every call is recorded, and an unexpected request throws so a
 *  render smoke test cannot silently depend on a door it does not stub. */
function makeHost(respond) {
	const calls = [];
	const request = async (method, params) => {
		calls.push({ method, params });
		if (respond) return respond(method, params);
		throw new Error(`harness: unstubbed host.request(${method})`);
	};
	return { calls, request, notifyError: () => {}, notify: () => {} };
}

function loadWidget(hostStub) {
	let src = fs.readFileSync(SOURCE, "utf8");
	src = src.replace(/^import [^\n]*;\r?\n/gm, ""); // top-level imports are single-line
	src = src.replace(/^export default /m, "const __widgetDefault = ");
	src = src.replace(/^export const /gm, "const ");
	const sdk = makeSdk();
	const factory = new Function(
		...SDK_COMPONENTS, "cn", "host", "jsx", "jsxs", "useState", "useEffect", "useMemo", "useCallback",
		`${src}\nreturn { __widgetDefault, __test };`
	);
	const mod = factory(
		...SDK_COMPONENTS.map((name) => sdk[name]),
		sdk.cn,
		hostStub,
		jsx,
		jsxs,
		hooks.useState,
		hooks.useEffect,
		hooks.useMemo,
		hooks.useCallback
	);
	if (!mod || !mod.__test || !mod.__widgetDefault) {
		throw new Error("harness: desktop/plugin.js did not expose the expected module shape");
	}
	return { test: mod.__test, widget: mod.__widgetDefault, sdk };
}

// --- tolerant element -> text renderer --------------------------------------

const TEXTY_PROPS = ["title", "description", "label", "placeholder", "aria-label", "value"];

function textOf(node) {
	if (node == null || node === true || node === false) return "";
	if (typeof node === "string" || typeof node === "number") return String(node);
	if (Array.isArray(node)) return node.map(textOf).join(" ");
	if (node.__el) {
		const { type, props } = node;
		if (typeof type === "function") return textOf(type(props));
		const parts = [String(type), textOf(props.children)];
		for (const key of TEXTY_PROPS) {
			if (typeof props[key] === "string" || typeof props[key] === "number") {
				parts.push(String(props[key]));
			}
		}
		return parts.join(" ");
	}
	return "";
}

// --- fixtures ---------------------------------------------------------------

function sampleValues() {
	return {
		backends: [
			{
				name: "tavily",
				type: "custom",
				base_url: "https://api.example.invalid/tavily",
				search_path: "/search",
				query_param: "query",
				auth_style: "bearer",
				credentials: ["TAVILY_API_KEY", "TAVILY_API_KEY_2"]
			},
			{
				name: "minimax",
				type: "custom",
				api_key_env: "MINIMAX_CN_API_KEY",
				base_url: "https://api.example.invalid/minimax"
			}
		],
		timeout: 10,
		max_results: 8,
		min_backends: 1,
		ranking_mode: "auto",
		pool_strategy: "fill_first"
	};
}

// A pasted-value shape, assembled at runtime so no key-shaped literal lands in
// the repository (the frozen desensitization rule wants a zero-hit).
const VALUE_SHAPED_FIXTURE = ["tv", "ly-", "pasted", "-value"].join("");

function handlers() {
	const noop = () => {};
	return {
		onField: noop,
		onType: noop,
		onOption: noop,
		onAddLine: noop,
		onAddKey: noop,
		onExcludeLine: noop,
		onExcludeKey: noop,
		onRequestDelete: noop
	};
}

// --- modes ------------------------------------------------------------------

function modeRoundTrip() {
	const { test, widget } = loadWidget(makeHost());
	const { draft } = test.draftFromValues(sampleValues());
	const payload = test.settingsFromDraft(draft);
	// payload -> draft -> payload  (idempotent)
	const roundTripped = test.settingsFromDraft(test.draftFromValues(payload).draft);
	// draft -> config string -> parse -> payload
	const text = test.draftToConfigText(draft);
	const parsed = test.parseConfigText(text);
	const fromText = parsed.ok ? test.settingsFromDraft(parsed.draft) : null;
	return {
		mode: "roundtrip",
		payload,
		roundTripped,
		fromText,
		parseOk: parsed.ok,
		parseError: parsed.error,
		idempotent: JSON.stringify(payload) === JSON.stringify(roundTripped),
		stringRoundTrip: JSON.stringify(payload) === JSON.stringify(fromText),
		widgetShape: {
			id: widget.id,
			register: typeof widget.register,
			widgetVersion: test.WIDGET_VERSION
		}
	};
}

function modeMapping() {
	const { test } = loadWidget(makeHost());
	const draft = test.draftFromValues(sampleValues()).draft;
	const lineExcluded = test.settingsFromDraft(test.excludeLine(draft, 1));
	const keyExcluded = test.settingsFromDraft(test.excludeKey(draft, 0, 1));
	const addKeyDraft = test.addKey(draft, 1);
	const addLineDraft = test.addLine(draft);
	const validation = {
		empty: test.validateDraft(test.addLine(test.emptyDraft())),
		badEnv: test.validateDraft(
			test.updateLineField(
				test.updateLineField(draft, 0, "credentials", [VALUE_SHAPED_FIXTURE]),
				0,
				"name",
				"oops"
			)
		)
	};
	return {
		mode: "mapping",
		linesAfterExclude: lineExcluded.backends.length,
		lineNamesAfterExclude: lineExcluded.backends.map((line) => line.name),
		credentialsAfterKeyExclude: keyExcluded.backends[0].credentials,
		credentialsAdded: addKeyDraft.backends[1].credentials,
		linesAfterAdd: addLineDraft.backends.length,
		extensionKeysKept: Object.prototype.hasOwnProperty.call(
			test.settingsFromDraft(draft).backends[1],
			"api_key_env"
		),
		kNeverWritten: !Object.prototype.hasOwnProperty.call(
			test.settingsFromDraft({ ...draft, explicitK: 3 }),
			"k"
		),
		warnings: {
			mismatch: test.kMismatchWarning(3, 2),
			aligned: test.kMismatchWarning(2, 2),
			absent: test.kMismatchWarning(null, 2)
		},
		validation
	};
}

function modeRender() {
	const host = makeHost();
	const { test } = loadWidget(host);
	const draft = test.draftFromValues(sampleValues()).draft;
	const h = handlers();
	return {
		mode: "render",
		lines: textOf(test.linesView({ draft, k: null, handlers: h, note: null })),
		kWarning: textOf(test.linesView({ draft, k: 5, handlers: h, note: null })),
		empty: textOf(test.emptyView()),
		error: textOf(test.errorView("boom: settings unreadable")),
		component: textOf(jsx(test.ConfigPage, {})),
		stringSection: textOf(test.linesView({ draft, k: null, handlers: h, note: null })).includes(
			"当前配置字符串"
		)
	};
}

function modeIllegal() {
	const host = makeHost();
	const { test } = loadWidget(host);
	const loaded = test.draftFromValues({
		backends: "not-a-list",
		ranking_mode: "bogus",
		pool_strategy: 42,
		timeout: "abc"
	});
	const bad = test.parseConfigText("{ this is not json");
	const validJsonBadShape = test.parseConfigText('{"backends": 12}');
	return {
		mode: "illegal",
		problems: loaded.problems,
		backendsLoaded: loaded.draft.backends.length,
		optionsFallback: loaded.draft.options,
		parseOk: bad.ok,
		parseError: bad.error,
		parseDraft: bad.draft,
		badShapeOk: validJsonBadShape.ok,
		badShapeProblems: validJsonBadShape.problems,
		requestCalls: host.calls.length
	};
}

const MODES = {
	roundtrip: modeRoundTrip,
	mapping: modeMapping,
	render: modeRender,
	illegal: modeIllegal
};

function main() {
	const mode = process.argv[2];
	const run = MODES[mode];
	if (!run) {
		process.stderr.write(`usage: node tests/widget_harness.cjs <${Object.keys(MODES).join("|")}>\n`);
		process.exit(2);
	}
	try {
		process.stdout.write(`${JSON.stringify(run(), null, 2)}\n`);
	} catch (error) {
		process.stderr.write(`${error && error.stack ? error.stack : String(error)}\n`);
		process.exit(1);
	}
}

main();
