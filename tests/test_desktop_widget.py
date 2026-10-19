"""Phase-3 desktop widget checks (card editor + two-level membership + string).

The widget ships to the client machine (``desktop/plugin.js``, plain ESM) while
the agent half runs in the gateway, so these tests pin the released pair:

* ``WIDGET_VERSION`` must match ``version:`` in ``plugin.yaml`` (a build skew
  invented by accident fails here instead of shipping);
* the draft ↔ settings-payload round trip stays idempotent, and the in-page
  config string parses back to the same payload (one source of truth);
* L1/L2 membership unchecks map to removal from ``backends`` / ``credentials``
  (no private residue), ``k`` is never written, and an explicit ``k`` that
  disagrees with the line count raises the S12 warning;
* the pure view builders render the cards / empty / error states without a
  crash, and no credential VALUE shape ever appears in the rendered text;
* a damaged string is reported and nothing is written from it.

The unit under test is the released artifact, so reading ``desktop/plugin.js``
through ``tests/widget_harness.cjs`` (stub SDK/react, real source) is the point
rather than a shortcut.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "desktop" / "plugin.js"
HARNESS = ROOT / "tests" / "widget_harness.cjs"

# Credential VALUE shapes that must never reach a page, an error string or a
# log line. Spelled with character classes on purpose: the rule itself carries
# no literal vendor prefix (the same convention the agent half's rejection
# pattern follows).
VALUE_SHAPES = re.compile(r"([t][v][l][y][-]|[s][k]-[A-Za-z0-9]{8,}|[A]Iza[0-9A-Za-z_-]{10,})")


def _value_shaped_fixture() -> str:
    """A pasted-value shape, assembled at runtime so no key-shaped literal
    lands in the repository (the frozen desensitization rule wants a zero-hit)."""
    return "tv" + "ly-" + "pasted" + "-value"


def run_harness(mode: str) -> dict:
    """Run ``tests/widget_harness.cjs`` and return its JSON report."""
    completed = subprocess.run(
        ["node", str(HARNESS), mode],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"widget harness {mode!r} failed: exit={completed.returncode}, "
            f"stderr={completed.stderr.strip()}"
        )
    return json.loads(completed.stdout)


def _manifest_version() -> str:
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*['\"]?([^'\"\s#]+)", text, re.MULTILINE)
    if not match:
        raise AssertionError("plugin.yaml has no version:")
    return match.group(1)


def _widget_version() -> str:
    text = PLUGIN.read_text(encoding="utf-8")
    match = re.search(r'^const WIDGET_VERSION = "([^"]+)"', text, re.MULTILINE)
    if not match:
        raise AssertionError("desktop/plugin.js has no WIDGET_VERSION constant")
    return match.group(1)


class WidgetVersionTest(unittest.TestCase):
    def test_widget_version_matches_manifest(self):
        self.assertEqual(
            _widget_version(),
            _manifest_version(),
            "desktop/plugin.js WIDGET_VERSION and plugin.yaml version disagree — "
            "users would see a version skew on a matched install",
        )

    def test_version_looks_like_a_release(self):
        self.assertRegex(_manifest_version(), r"^\d+\.\d+\.\d+$")

    def test_default_export_matches_the_host_contract(self):
        shape = run_harness("roundtrip")["widgetShape"]
        self.assertEqual(shape["id"], "hermes-federated-search")
        self.assertEqual(shape["register"], "function")


class SerializationRoundTripTest(unittest.TestCase):
    """① 草稿→payload 序列化纯函数（round-trip：payload→再解析→等价）。"""

    @classmethod
    def setUpClass(cls):
        cls.report = run_harness("roundtrip")

    def test_payload_round_trip_is_idempotent(self):
        self.assertTrue(
            self.report["idempotent"],
            "settings payload changed after payload→draft→payload",
        )

    def test_config_string_parses_back_to_the_same_payload(self):
        self.assertTrue(self.report["parseOk"], self.report["parseError"])
        self.assertTrue(
            self.report["stringRoundTrip"],
            "draft→string→payload did not reproduce the payload",
        )

    def test_unknown_line_keys_survive_the_round_trip(self):
        minimax = self.report["payload"]["backends"][1]
        self.assertEqual(minimax.get("api_key_env"), "MINIMAX_CN_API_KEY")
        self.assertEqual(
            [line["name"] for line in self.report["payload"]["backends"]],
            ["tavily", "minimax"],
        )
        self.assertEqual(
            self.report["payload"]["backends"][0]["credentials"],
            ["TAVILY_API_KEY", "TAVILY_API_KEY_2"],
        )

    def test_two_card_structures_round_trip(self):
        """新旧两种卡片结构并存：credentials 池与 api_key_env 单卡都要活。"""
        compat = run_harness("compat")
        self.assertTrue(compat["twoCardStructures"])
        self.assertEqual(compat["problems"], [], "compat fixture must load clean")

    def test_legacy_card_keeps_api_key_env_and_unknown_keys(self):
        """旧卡（api_key_env/required/未知键）原样往返；type 不注入。"""
        compat = run_harness("compat")
        self.assertTrue(compat["legacyCardKept"])
        self.assertTrue(compat["typeNotInjected"], "a typeless line got a type injected")
        self.assertTrue(compat["registeredKept"], "a registered line lost its type")
        self.assertTrue(compat["typesRoundTrip"])

    def test_ranker_tree_round_trips_with_write_rules(self):
        """ranker 整树写回：prompt 保留；清空叶删键；无现值零自动写入。"""
        rules = run_harness("compat")["rankerRoundTrip"]
        self.assertTrue(rules["rankerKept"], "ranker provider/model/prompt did not survive")
        self.assertTrue(rules["rankerCleared"], "clearing provider dropped prompt siblings")
        self.assertTrue(rules["rankerOmitted"], "an empty draft wrote an empty ranker key")


class MembershipMappingTest(unittest.TestCase):
    """② 两级勾选→payload 映射（含取消勾选与 k 警告）。"""

    @classmethod
    def setUpClass(cls):
        cls.report = run_harness("mapping")

    def test_l1_uncheck_removes_the_line_from_backends(self):
        self.assertEqual(self.report["linesAfterExclude"], 1)
        self.assertEqual(self.report["lineNamesAfterExclude"], ["tavily"])

    def test_l2_uncheck_removes_the_key_from_credentials(self):
        self.assertEqual(self.report["credentialsAfterKeyExclude"], ["TAVILY_API_KEY"])

    def test_add_key_and_add_line_stage_new_members(self):
        self.assertEqual(self.report["credentialsAdded"], [""])
        self.assertEqual(self.report["linesAfterAdd"], 3)

    def test_k_is_never_written(self):
        self.assertTrue(self.report["kNeverWritten"], "the UI wrote an explicit k")

    def test_k_warning_fires_only_on_a_mismatch(self):
        warnings = self.report["warnings"]
        self.assertIsNone(warnings["aligned"])
        self.assertIsNone(warnings["absent"])
        self.assertIn("k=3", warnings["mismatch"])
        self.assertIn("2", warnings["mismatch"])

    def test_validation_blocks_bad_input_without_echoing_the_value(self):
        problems = self.report["validation"]
        empty = " ".join(problems["empty"])
        bad_env = " ".join(problems["badEnv"])
        self.assertIn("name", empty)
        self.assertIn("环境变量名", bad_env)
        self.assertNotIn(
            _value_shaped_fixture(),
            bad_env,
            "the validation message echoed the offending value",
        )
        self.assertRegex(bad_env, r"\^\[A-Z\]\[A-Z0-9_\]\{0,127\}\$")

    def test_per_line_pool_strategy_round_trip(self):
        """卡内 pool_strategy：选值写、清空删键、存量空串未触碰原样。"""
        pool = run_harness("compat")["perLinePoolRoundTrip"]
        self.assertTrue(pool["poolWritten"], "a chosen per-line strategy was not written")
        self.assertTrue(pool["poolDeleted"], "clearing the select kept the key")
        self.assertTrue(pool["poolEmptyKept"], "a legacy empty string was rewritten")


class RenderSmokeTest(unittest.TestCase):
    """④ 渲染 smoke（node harness，stub SDK/react，断言卡渲染与空态/错误态不崩）。"""

    @classmethod
    def setUpClass(cls):
        cls.report = run_harness("render")

    def test_lines_view_renders_cards_strings_and_string_section(self):
        text = self.report["lines"]
        for token in ("tavily", "minimax", "TAVILY_API_KEY", "参与聚合", "＋ 添加 Key", "当前配置字符串", "重排提示词"):
            self.assertIn(token, text, f"lines view lost {token!r}")
        self.assertTrue(self.report["stringSection"])

    def test_rendered_page_carries_no_credential_value_shape(self):
        for key in ("lines", "kWarning", "component"):
            self.assertIsNone(
                VALUE_SHAPES.search(self.report[key]),
                f"{key} rendered a credential value shape",
            )

    def test_empty_state_names_the_provider_error(self):
        self.assertIn("no search backends configured", self.report["empty"])

    def test_error_state_renders_the_message(self):
        self.assertIn("boom: settings unreadable", self.report["error"])

    def test_k_warning_bar_renders(self):
        self.assertIn("k=5", self.report["kWarning"])

    def test_component_mounts_without_a_crash(self):
        self.assertIn("Loader", self.report["component"])


class RerankPromptConfigTest(unittest.TestCase):
    """⑥ 重排提示词配置框（ranker.prompt）：写回/删叶/空白归一/渲染/跨语言防漂移。"""

    @classmethod
    def setUpClass(cls):
        cls.compat = run_harness("compat")
        cls.render = run_harness("render")
        cls.roundtrip = run_harness("roundtrip")

    def test_prompt_write_rules(self):
        """保存写叶（整树合并不丢兄弟键）、恢复默认删叶、无现值不发明、空白按未设置。"""
        rules = self.compat["promptRoundTrip"]
        self.assertTrue(rules["promptWritten"], "a saved prompt did not reach settings.ranker")
        self.assertTrue(rules["promptCleared"], "restore default did not delete the leaf")
        self.assertTrue(rules["promptAbsentUntouched"], "a config without prompt got one invented")
        self.assertTrue(rules["promptBlankNormalized"], "a blank prompt was written as a value")

    def test_prompt_box_shows_the_effective_value(self):
        """已设置＝当前值；未设置＝内置默认全文＋「未设置→内置默认」说明。"""
        self.assertTrue(self.render["promptBoxOnLines"], "the prompt box is missing from the page")
        self.assertTrue(self.render["promptSetShown"], "the current custom prompt is not shown")
        self.assertTrue(self.render["promptUnsetShown"], "the unset state does not explain the built-in default")
        self.assertTrue(self.render["defaultShownInUnset"], "the built-in default prompt text is not visible")
        self.assertTrue(self.render["promptAgentNoteShown"], "the agent-facing note (entry + key path) is missing")

    def test_prompt_box_hides_in_keyword_mode(self):
        """ranking_mode=keyword：提示词框随 ranker 区一起不渲染（现状 gate）。"""
        self.assertTrue(self.render["promptKeywordHidden"])

    def test_prompt_survives_the_config_string(self):
        """字符串门：draftToConfigText 含 prompt；回读等价（roundtrip 一并覆盖）。"""
        self.assertTrue(self.roundtrip["stringHasPrompt"])
        self.assertTrue(self.roundtrip["stringRoundTrip"])

    def test_default_prompt_copy_matches_the_provider_baseline(self):
        """跨语言 pin：widget 默认文本 === provider 未配置时实际发送的 system 消息。"""
        from unittest.mock import MagicMock, patch

        from provider import _rank_results

        results = [
            {"title": f"R{i}", "url": f"https://r{i}.com", "description": f"D{i}"}
            for i in range(5)
        ]
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="[1,2,3,4,5]"))]

        with patch(
            "agent.auxiliary_client.call_llm",
            return_value=mock_response,
        ) as mock_call:
            _rank_results("test", results, {"provider": "opencode-go", "model": "deepseek-v4-flash"})
            system_msg = mock_call.call_args[1]["messages"][0]["content"]

        self.assertEqual(
            self.render["defaultPrompt"],
            system_msg,
            "the widget default prompt drifts from the provider's default system message",
        )


class IllegalStateTest(unittest.TestCase):
    """⑤ 非法字符串态解析不崩且不写。"""

    @classmethod
    def setUpClass(cls):
        cls.report = run_harness("illegal")

    def test_damaged_settings_tree_reports_problems_and_loads_nothing(self):
        self.assertGreaterEqual(len(self.report["problems"]), 1)
        self.assertEqual(self.report["backendsLoaded"], 0)
        self.assertEqual(self.report["optionsFallback"]["ranking_mode"], "auto")

    def test_nothing_is_written_from_a_damaged_state(self):
        self.assertEqual(
            self.report["requestCalls"],
            0,
            "a damaged string must not reach a write door",
        )

    def test_unparsable_string_reports_an_error_and_yields_no_draft(self):
        self.assertFalse(self.report["parseOk"])
        self.assertTrue(self.report["parseError"])
        self.assertIsNone(self.report["parseDraft"])

    def test_json_of_the_wrong_shape_is_rejected(self):
        self.assertFalse(self.report["badShapeOk"])
        self.assertTrue(self.report["badShapeProblems"])


if __name__ == "__main__":
    unittest.main()
