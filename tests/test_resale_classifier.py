"""
Тесты разбора ИИ для «Поиска под перепродажу» (backend/resale_classifier.py).

Runner (Callable[[str], str]) — промпт на входе, текст ответа модели на
выходе. Конверт --output-format json ({"result": "..."}) распаковывает
ТОЛЬКО реальный runner внутри себя (спека: «Реальный runner: ...,
текст модели в поле result» — это описание его внутренней механики,
единственная публичная функция модуля, classify_items, работает уже с
текстом ответа модели, без знания про конверт CLI). Инъекция
кастомного runner в тестах поэтому возвращает сразу текст ответа модели
(JSON-массив, опционально в ```-блоке) — как и раздел спеки «Ответ модели»
это описывает.

Реальный claude CLI не вызывается ни разу: runner всегда подменён фейком.
Кэш — SQLite во временной папке на каждый тест, боевой resale_cache.db
не трогаем.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import resale_classifier  # noqa: E402


# ── Фикстуры ─────────────────────────────────────────────────────────────────

def _cls_json(item_id: str, *, is_laptop=True, brand="Acer", series="Nitro 5",
              gpu="RTX 3060", condition="working", reason="исправен") -> dict:
    """Один элемент JSON-ответа модели (до превращения в Classification)."""
    return {
        "item_id": item_id, "is_laptop": is_laptop, "brand": brand,
        "series": series, "gpu": gpu, "condition": condition, "reason": reason,
    }


ITEM_1 = {"item_id": "111", "title": "Acer Nitro 5", "description": "RTX 3060, 16 Гб",
          "price": 55000, "category_slug": "noutbuki"}
ITEM_2 = {"item_id": "222", "title": "Зарядка для ноутбука", "description": "65 Вт, оригинал",
          "price": 500, "category_slug": "aksessuary"}


class ResaleClassifierTestCase(unittest.TestCase):
    """Общий temp-каталог под кэш SQLite для каждого теста."""

    def setUp(self) -> None:
        # ignore_cleanup_errors: реализация открывает sqlite3-соединения через
        # `with sqlite3.connect(...) as conn` — это коммитит/откатывает
        # транзакцию, но НЕ закрывает соединение (частая ловушка sqlite3);
        # на Windows файл кэша поэтому иногда остаётся залоченным до GC,
        # и без этого флага падает не тест, а cleanup временной папки.
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = os.path.join(self._tmpdir.name, "resale_cache_test.db")


# ── Разбор ответа модели ─────────────────────────────────────────────────────

class ParsingTests(ResaleClassifierTestCase):
    def test_parses_normal_json_response(self) -> None:
        runner = mock.Mock(return_value=json.dumps([_cls_json("111")], ensure_ascii=False))
        result = resale_classifier.classify_items([ITEM_1], runner=runner, db_path=self.db_path)
        self.assertIn("111", result)
        self.assertIsNone(result["111"]["error"])
        self.assertEqual(result["111"]["brand"], "Acer")
        self.assertEqual(result["111"]["series"], "Nitro 5")
        self.assertEqual(result["111"]["gpu"], "RTX 3060")
        self.assertEqual(result["111"]["condition"], "working")
        runner.assert_called_once()

    def test_parses_json_wrapped_in_code_block(self) -> None:
        wrapped = "```json\n" + json.dumps([_cls_json("111")], ensure_ascii=False) + "\n```"
        runner = mock.Mock(return_value=wrapped)
        result = resale_classifier.classify_items([ITEM_1], runner=runner, db_path=self.db_path)
        self.assertIsNone(result["111"]["error"])
        self.assertEqual(result["111"]["series"], "Nitro 5")

    def test_json_array_with_surrounding_text_is_extracted(self) -> None:
        raw = "Вот классификация:\n" + json.dumps([_cls_json("111")], ensure_ascii=False) + "\nГотово."
        runner = mock.Mock(return_value=raw)
        result = resale_classifier.classify_items([ITEM_1], runner=runner, db_path=self.db_path)
        self.assertIsNone(result["111"]["error"])

    def test_missing_item_id_in_model_response_gets_error(self) -> None:
        # Модель вернула классификацию только для одного из двух объявлений.
        runner = mock.Mock(return_value=json.dumps([_cls_json("111")], ensure_ascii=False))
        result = resale_classifier.classify_items(
            [ITEM_1, ITEM_2], runner=runner, db_path=self.db_path,
        )
        self.assertIsNone(result["111"]["error"])
        self.assertIsNotNone(result["222"]["error"])

    def test_garbage_response_marks_whole_batch_error_without_raising(self) -> None:
        runner = mock.Mock(return_value="это вообще не json и не массив")
        result = resale_classifier.classify_items(
            [ITEM_1, ITEM_2], runner=runner, db_path=self.db_path,
        )
        self.assertIsNotNone(result["111"]["error"])
        self.assertIsNotNone(result["222"]["error"])

    def test_runner_exception_marks_whole_batch_error_without_raising(self) -> None:
        runner = mock.Mock(side_effect=RuntimeError("таймаут CLI"))
        result = resale_classifier.classify_items(
            [ITEM_1, ITEM_2], runner=runner, db_path=self.db_path,
        )
        self.assertIsNotNone(result["111"]["error"])
        self.assertIsNotNone(result["222"]["error"])


# ── Кэш ──────────────────────────────────────────────────────────────────────

class CacheTests(ResaleClassifierTestCase):
    def test_cache_hit_skips_runner_on_second_call(self) -> None:
        runner = mock.Mock(return_value=json.dumps([_cls_json("111")], ensure_ascii=False))
        resale_classifier.classify_items([ITEM_1], runner=runner, db_path=self.db_path)
        result2 = resale_classifier.classify_items([ITEM_1], runner=runner, db_path=self.db_path)
        runner.assert_called_once()
        self.assertIsNone(result2["111"]["error"])

    def test_changed_description_triggers_reclassification(self) -> None:
        runner = mock.Mock(return_value=json.dumps([_cls_json("111")], ensure_ascii=False))
        resale_classifier.classify_items([ITEM_1], runner=runner, db_path=self.db_path)
        changed = dict(ITEM_1, description=ITEM_1["description"] + " — торг уместен")
        resale_classifier.classify_items([changed], runner=runner, db_path=self.db_path)
        self.assertEqual(runner.call_count, 2)

    def test_error_entries_are_not_cached(self) -> None:
        failing_runner = mock.Mock(return_value="мусор, не json вовсе")
        result1 = resale_classifier.classify_items(
            [ITEM_1], runner=failing_runner, db_path=self.db_path,
        )
        self.assertIsNotNone(result1["111"]["error"])

        working_runner = mock.Mock(return_value=json.dumps([_cls_json("111")], ensure_ascii=False))
        result2 = resale_classifier.classify_items(
            [ITEM_1], runner=working_runner, db_path=self.db_path,
        )
        working_runner.assert_called_once()  # не кэш-хит — реально вызван заново
        self.assertIsNone(result2["111"]["error"])


# ── Батчи и пропуск объявлений без item_id ──────────────────────────────────

class BatchingTests(ResaleClassifierTestCase):
    def test_batch_size_splits_items_into_multiple_runner_calls(self) -> None:
        items = [dict(ITEM_1, item_id=str(i), title=f"Ноутбук {i}") for i in range(1, 6)]  # 5 шт.

        # ДОПУЩЕНИЕ: спека фиксирует только верхнюю границу размера пачки
        # ("Пачками по ≤ 40 объявлений"), порядок нарезки не описан — считаю,
        # что это последовательные срезы входного списка по batch_size.
        responses = []
        for start in range(0, 5, 2):
            chunk_ids = [str(i) for i in range(start + 1, min(start + 3, 6))]
            responses.append(json.dumps([_cls_json(i) for i in chunk_ids], ensure_ascii=False))
        runner = mock.Mock(side_effect=responses)

        result = resale_classifier.classify_items(
            items, runner=runner, db_path=self.db_path, batch_size=2,
        )
        self.assertEqual(runner.call_count, 3)  # ceil(5/2)
        self.assertEqual(len(result), 5)
        for i in range(1, 6):
            self.assertIsNone(result[str(i)]["error"])

    def test_items_without_item_id_are_skipped(self) -> None:
        no_id_item = dict(ITEM_1, item_id=None)
        runner = mock.Mock(return_value=json.dumps([_cls_json("111")], ensure_ascii=False))
        result = resale_classifier.classify_items(
            [no_id_item, ITEM_1], runner=runner, db_path=self.db_path,
        )
        self.assertNotIn(None, result)
        self.assertEqual(list(result.keys()), ["111"])


# ── Реальный runner: claude не найден в PATH ────────────────────────────────

class RealRunnerNotFoundTests(ResaleClassifierTestCase):
    def test_missing_claude_cli_produces_error_not_traceback(self) -> None:
        # runner не передаём — используется реальный (shutil.which("claude"));
        # подменяем только shutil.which, subprocess вообще не должен вызываться.
        with (
            mock.patch.object(resale_classifier.shutil, "which", return_value=None),
            mock.patch.object(resale_classifier.subprocess, "run") as run_mock,
        ):
            result = resale_classifier.classify_items([ITEM_1], db_path=self.db_path)
        run_mock.assert_not_called()
        self.assertIsNotNone(result["111"]["error"])
        self.assertIn("claude", result["111"]["error"].lower())


if __name__ == "__main__":
    unittest.main()
