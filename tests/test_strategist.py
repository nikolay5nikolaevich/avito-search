"""
Тесты Стратега (backend/strategist.py, брифа 06).

build_facts — чистая функция на фикстурах (без HTTP/CLI). CLI не вызывается:
subprocess.run и shutil.which замоканы — один вызов LLM стоит денег и должен
делаться только пользователем вручную (см. brief 06, «Можно без спроса»).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import strategist  # noqa: E402


# ── Фикстуры build_facts ─────────────────────────────────────────────────

SNAPSHOT = {
    "items": [],
    "cities": [
        {"city_slug": "moskva", "city_name": "Москва", "items": 2,
         "views_per_day_avg": 5.0, "contact_rate": 0.2},
        {"city_slug": "kazan", "city_name": "Казань", "items": 1,
         "views_per_day_avg": 3.0, "contact_rate": None},
    ],
    "variants": [
        {"prep_id": "prep-a", "variant_index": 1, "items": 1, "views_per_day_avg": 2.0, "contact_rate": None},
        {"prep_id": "prep-a", "variant_index": 2, "items": 1, "views_per_day_avg": 6.0, "contact_rate": None},
        {"prep_id": "prep-single", "variant_index": 1, "items": 1, "views_per_day_avg": 4.0, "contact_rate": None},
    ],
}

# Казани в отчёте Разведчика нет — город без рыночных данных (спека, Этап 3).
SCOUT_REPORT = [
    {"city_slug": "moskva", "city_name": "Москва", "avg_views_today": 10.0, "local_count": 100, "top3": []},
]


class BuildFactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = strategist.build_facts(SNAPSHOT, SCOUT_REPORT)

    def test_city_with_market_data_has_ratio(self) -> None:
        moskva = next(c for c in self.facts["cities"] if c["city_slug"] == "moskva")
        self.assertIsNotNone(moskva["market"])
        self.assertEqual(moskva["market"]["avg_views_today"], 10.0)
        self.assertAlmostEqual(moskva["market"]["our_vs_market_ratio"], 0.5)

    def test_city_without_market_data_is_null(self) -> None:
        kazan = next(c for c in self.facts["cities"] if c["city_slug"] == "kazan")
        self.assertIsNone(kazan["market"])

    def test_package_of_two_variants_has_spread(self) -> None:
        prep_a = next(v for v in self.facts["variants"] if v["prep_id"] == "prep-a")
        self.assertEqual(prep_a["variants_count"], 2)
        self.assertEqual(prep_a["min_views_per_day_avg"], 2.0)
        self.assertEqual(prep_a["max_views_per_day_avg"], 6.0)
        self.assertEqual(prep_a["spread"], 4.0)

    def test_package_of_one_variant_is_skipped(self) -> None:
        prep_ids = {v["prep_id"] for v in self.facts["variants"]}
        self.assertNotIn("prep-single", prep_ids)

    def test_caveats_are_present(self) -> None:
        self.assertEqual(len(self.facts["caveats"]), 2)

    def test_facts_work_without_any_variants(self) -> None:
        # Пакет опубликован до журнала: у всех объявлений prep_id/variant_index
        # = null, variants в снимке пуст — build_facts должен давать
        # осмысленный результат и без вариантов (условие брифа 06).
        snapshot_no_variants = {**SNAPSHOT, "variants": []}
        facts = strategist.build_facts(snapshot_no_variants, SCOUT_REPORT)
        self.assertEqual(facts["variants"], [])
        self.assertEqual(len(facts["cities"]), 2)


# ── parse_hypotheses ──────────────────────────────────────────────────────

VALID_HYPOTHESIS = {
    "change": "Поднять цену в Москве",
    "basis": "our_vs_market_ratio 0.5 в Москве",
    "metric": "views_per_day_avg",
    "expected_effect": "рост просмотров на 20%",
    "check_days": 7,
}


def _envelope(result_text: str) -> str:
    """Конверт --output-format json: {"result": "<текст ответа модели>", ...}."""
    return json.dumps({"result": result_text, "type": "result"}, ensure_ascii=False)


class ParseHypothesesTests(unittest.TestCase):
    def test_valid_response(self) -> None:
        raw = _envelope(json.dumps([VALID_HYPOTHESIS]))
        hypotheses = strategist.parse_hypotheses(raw)
        self.assertEqual(len(hypotheses), 1)
        self.assertEqual(hypotheses[0]["check_days"], 7)
        self.assertEqual(hypotheses[0]["change"], VALID_HYPOTHESIS["change"])

    def test_event_list_envelope_from_real_cli(self) -> None:
        # Формат живого CLI: список событий, конверт с result — последний.
        raw = json.dumps([
            {"type": "system", "subtype": "init", "tools": []},
            {"type": "assistant", "message": {}},
            {"type": "result", "subtype": "success", "is_error": False,
             "result": "```json\n" + json.dumps([VALID_HYPOTHESIS], ensure_ascii=False) + "\n```"},
        ], ensure_ascii=False)
        hypotheses = strategist.parse_hypotheses(raw)
        self.assertEqual(len(hypotheses), 1)
        self.assertEqual(hypotheses[0]["change"], VALID_HYPOTHESIS["change"])

    def test_event_list_with_error_result_raises(self) -> None:
        raw = json.dumps([
            {"type": "system", "subtype": "init"},
            {"type": "result", "subtype": "error", "is_error": True, "result": "лимит исчерпан"},
        ], ensure_ascii=False)
        with self.assertRaises(strategist.StrategistError):
            strategist.parse_hypotheses(raw)

    def test_json_with_surrounding_text(self) -> None:
        raw = _envelope(f"Вот гипотезы:\n{json.dumps([VALID_HYPOTHESIS])}\nСпасибо.")
        hypotheses = strategist.parse_hypotheses(raw)
        self.assertEqual(len(hypotheses), 1)

    def test_check_days_30_is_valid(self) -> None:
        h = dict(VALID_HYPOTHESIS, check_days=30)
        raw = _envelope(json.dumps([h]))
        hypotheses = strategist.parse_hypotheses(raw)
        self.assertEqual(hypotheses[0]["check_days"], 30)

    def test_missing_field_raises(self) -> None:
        broken = dict(VALID_HYPOTHESIS)
        del broken["metric"]
        raw = _envelope(json.dumps([broken]))
        with self.assertRaises(strategist.StrategistError):
            strategist.parse_hypotheses(raw)

    def test_invalid_check_days_raises(self) -> None:
        broken = dict(VALID_HYPOTHESIS, check_days=14)
        raw = _envelope(json.dumps([broken]))
        with self.assertRaises(strategist.StrategistError):
            strategist.parse_hypotheses(raw)

    def test_envelope_not_json_raises(self) -> None:
        with self.assertRaises(strategist.StrategistError):
            strategist.parse_hypotheses("не json вовсе")

    def test_no_result_field_raises(self) -> None:
        raw = json.dumps({"type": "result"})
        with self.assertRaises(strategist.StrategistError):
            strategist.parse_hypotheses(raw)

    def test_result_without_array_raises(self) -> None:
        raw = _envelope("просто текст без массива")
        with self.assertRaises(strategist.StrategistError):
            strategist.parse_hypotheses(raw)

    def test_empty_array_raises(self) -> None:
        raw = _envelope(json.dumps([]))
        with self.assertRaises(strategist.StrategistError):
            strategist.parse_hypotheses(raw)

    def test_hypothesis_not_an_object_raises(self) -> None:
        raw = _envelope(json.dumps(["просто строка"]))
        with self.assertRaises(strategist.StrategistError):
            strategist.parse_hypotheses(raw)


# ── call_claude_cli (subprocess и shutil.which замоканы) ──────────────────

class CallClaudeCliTests(unittest.TestCase):
    def test_claude_not_found_raises(self) -> None:
        with mock.patch.object(strategist.shutil, "which", return_value=None):
            with self.assertRaises(strategist.StrategistError):
                strategist.call_claude_cli("промпт")

    def test_timeout_raises(self) -> None:
        with (
            mock.patch.object(strategist.shutil, "which", return_value="claude.cmd"),
            mock.patch.object(
                strategist.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=180),
            ),
        ):
            with self.assertRaises(strategist.StrategistError):
                strategist.call_claude_cli("промпт")

    def test_nonzero_exit_code_raises(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["claude"], returncode=1, stdout=b"", stderr=b"boom",
        )
        with (
            mock.patch.object(strategist.shutil, "which", return_value="claude.cmd"),
            mock.patch.object(strategist.subprocess, "run", return_value=completed),
        ):
            with self.assertRaises(strategist.StrategistError):
                strategist.call_claude_cli("промпт")

    def test_os_error_on_launch_raises(self) -> None:
        with (
            mock.patch.object(strategist.shutil, "which", return_value="claude.cmd"),
            mock.patch.object(strategist.subprocess, "run", side_effect=OSError("no exec")),
        ):
            with self.assertRaises(strategist.StrategistError):
                strategist.call_claude_cli("промпт")

    def test_success_returns_stdout_and_disables_tools(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout=b'{"result": "[]"}', stderr=b"",
        )
        with (
            mock.patch.object(strategist.shutil, "which", return_value="claude.cmd"),
            mock.patch.object(strategist.subprocess, "run", return_value=completed) as run_mock,
        ):
            raw = strategist.call_claude_cli("промпт")

        self.assertEqual(raw, '{"result": "[]"}')
        # Инструменты отключены явно (--tools "") — модель не должна иметь
        # доступа к файлам, ей незачем видеть проект (спека, Этап 3).
        args = run_mock.call_args.args[0]
        self.assertIn("--tools", args)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(run_mock.call_args.kwargs["timeout"], strategist.CLI_TIMEOUT_S)


class StrategistRunGuardTests(unittest.TestCase):
    """Второй запуск, пока идёт первый, получает 409 и не вызывает платный CLI."""

    def setUp(self) -> None:
        import app as app_module

        self.app_module = app_module
        # Обработчик вызывается напрямую (httpx для TestClient в проекте нет),
        # startup не выполняется — боевые cache.db/agents.db не трогаются.
        patches = [
            mock.patch.object(app_module.cache_mod, "get_entry_by_key", return_value={"cities": []}),
            mock.patch.object(app_module.journal, "get_latest_stats_snapshot", return_value={"id": 1, "data": {}}),
            mock.patch.object(app_module.journal, "DB_PATH", None),
            mock.patch.object(strategist, "build_facts", return_value={}),
            mock.patch.object(strategist, "build_prompt", return_value="промпт"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(setattr, app_module, "_STRATEGIST_RUNNING", False)

    def _post_run(self):
        import asyncio

        class _FakeRequest:
            async def json(self) -> dict:
                return {"scout_report_key": "ключ"}

        return asyncio.run(self.app_module.api_strategist_run(_FakeRequest()))

    def test_second_run_while_busy_is_rejected_without_cli_call(self) -> None:
        self.app_module._STRATEGIST_RUNNING = True
        with mock.patch.object(strategist, "call_claude_cli") as cli_mock:
            response = self._post_run()
        self.assertEqual(response.status_code, 409)
        cli_mock.assert_not_called()

    def test_flag_is_released_after_failed_run(self) -> None:
        with mock.patch.object(
            strategist, "call_claude_cli", side_effect=strategist.StrategistError("сбой"),
        ) as cli_mock:
            response = self._post_run()
        self.assertEqual(response.status_code, 502)
        cli_mock.assert_called_once()
        self.assertFalse(self.app_module._STRATEGIST_RUNNING)


if __name__ == "__main__":
    unittest.main()
