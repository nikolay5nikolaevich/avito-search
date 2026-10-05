"""
Денежные инварианты пакетной публикации — облегчённый перенос
tmp/audit/spec/test_targeted.py (полный стенд R1-R17, ~7.5 мин, не
переносился целиком — см. tests/publish_money_harness.py).

Перенесены требования, где ошибка стоит денег напрямую
(tmp/audit/spec/EXPECTATIONS.md):
  R1  — по каждому номеру пакета Авито создаёт не более одного объявления
        за всю историю пакета (сбой + перезапуск + resume до конца).
  R3  — каждый денежный клик (continue_listing/continue_view_price/
        skip_services) выполняется для номера не более одного раза.

R17 (повторный POST /api/publish/start с тем же prep_id не должен дублировать
уже созданные объявления) брифом тоже назван, но НЕ перенесён: это
подтверждённая находка аудита S6/tmp/audit/_state/findings/S6.md — на
сегодняшнем коде (backend/app.py, /api/publish/start не проверяет, что
prep_id уже занят незавершённой задачей) второй /start реально создаёт
дубли. Тест, ожидающий отсутствие дублей, был бы красным уже сегодня, а
бриф прямо запрещает кодировать находки как «ожидаемо падает» — с тестами
приёмки для S6/R17 разберётся тот бриф, который его чинит.

Тесты фиксируют СЕГОДНЯШНЕЕ поведение (все проходят на текущем коде) — они
не кодируют находки аудита (F-номера) как «ожидаемо падает»: это дело
тестов приёмки брифов, которые их чинят.

Запуск: .venv\\Scripts\\python.exe -m unittest tests.test_publish_money_invariants -v
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any

# tests/ в sys.path — нужно для «голого» import publish_money_harness ниже
# (тот сам добавит backend/ и остальное при своём импорте).
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

import publish_money_harness as H  # noqa: E402

# Трасса чистого прогона считается один раз ДО тестов (в ней самой уже есть
# asyncio.run) — тестовые методы асинхронные, вызвать clean_trace() внутри
# них нельзя (вложенный asyncio.run в уже работающем цикле).
TRACE: list[tuple[int, str, Any]] = []


def setUpModule() -> None:
    TRACE.extend(H.clean_trace(3))


def ev(name: str, index: Any = None, occurrence: int = 1) -> int:
    return H.find_event(TRACE, name, index, occurrence)


def created(h: H.Harness) -> dict[int, int]:
    return dict(h.avito.created_indices())


class KillAfterContinueListingTests(unittest.IsolatedAsyncioTestCase):
    """R1 + R3: сервер убит на экране цены просмотра объявления №2 (оно уже
    создано на Авито) — resume не должен ни задублировать №2, ни повторить
    ни один денежный клик."""

    async def test_no_duplicate_listing_and_no_repeated_money_click(self) -> None:
        with H.Harness(3) as h:
            h.avito.faults[ev("fill_view_price", 2)] = "kill"
            await h.start()
            self.assertEqual(await h.wait_task(), "killed")
            h.restart()
            await H.resume_to_end(h)

            self.assertEqual(created(h), {1: 1, 2: 1, 3: 1}, "R1: дубль объявления")
            repeated = {k: v for k, v in h.avito.clicks.items() if v > 1}
            self.assertEqual(repeated, {}, "R3: денежный клик повторён")


if __name__ == "__main__":
    unittest.main(verbosity=2)
