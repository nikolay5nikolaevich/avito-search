"""
Сквозной тест resume: падение на шаге → checkpoint РЕАЛЬНО на диске →
resume дожимает пакет. Перенос идеи из tmp/audit/d5/test_d5_resume_findings.py
(лучшая имеющаяся основа по брифу), но не самих 13 D5-тестов: они каждый
документируют ОТДЕЛЬНУЮ находку аудита («ДЕФЕКТ D5-0N» в докстринге) —
переносить их значило бы кодировать находки как постоянную часть набора,
хотя чинить их будут другие брифы (02-06), и тест сразу же станет красным
после фикса. Этот файл — нейтральная основа для них: закрывает F39
(tests/test_publish_checkpoint.py:588 звал run_publish_job БЕЗ tmp_dir —
checkpoint на диск не писался, сквозной проверки не было).

Оба теста используют tests/publish_money_harness.py (тот же стенд, что и
денежные инварианты) — тем самым публикация идёт через настоящие
app.api_publish_start/resume и publisher.run_publish_job с реальным
tmp_dir, а не только через прямой вызов run_publish_job в памяти.

Запуск: .venv\\Scripts\\python.exe -m unittest tests.test_publish_resume_e2e -v
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
import publish_state  # noqa: E402

TRACE: list[tuple[int, str, Any]] = []


def setUpModule() -> None:
    TRACE.extend(H.clean_trace(3))


def ev(name: str, index: Any = None, occurrence: int = 1) -> int:
    return H.find_event(TRACE, name, index, occurrence)


class SafeStepFailureResumeTests(unittest.IsolatedAsyncioTestCase):
    """Сбой ДО финансового шага (upload_photos объявления №2 из 3): объявление
    №2 на Авито не создано, checkpoint на диске это отражает, resume
    повторяет №2 и доводит пакет до конца без дублей."""

    async def test_checkpoint_written_to_disk_and_resume_completes_without_duplicates(
        self,
    ) -> None:
        with H.Harness(3) as h:
            h.avito.faults[ev("upload_photos", None, 2)] = "exc"
            await h.start()
            self.assertEqual(await h.wait_task(), "ok")  # исключение поймано, job.status=failed

            job_dir = h.publish_root / h.job_id
            saved_job, saved_draft = publish_state.load_publish_state(job_dir)
            self.assertEqual(saved_job["step"], "upload_photos")
            self.assertEqual(saved_job["item_index"], 2)
            self.assertEqual(saved_job["items_published"], 1)
            self.assertEqual(saved_draft.locations[1].city, "Москва")

            plan = publish_state.resume_plan(saved_job)
            self.assertEqual(plan, {
                "mode": "retry_item",
                "start_index": 2,
                "skipped_item": None,
                "items_total": 3,
            })

            answers = await H.resume_to_end(h)
            self.assertEqual(answers[0][0], 200, f"resume отклонён: {answers[0][1]}")

        self.assertEqual(dict(h.avito.created_indices()), {1: 1, 2: 1, 3: 1})
        repeated = {k: v for k, v in h.avito.clicks.items() if v > 1}
        self.assertEqual(repeated, {}, "денежный клик повторён при resume")


class FinancialStepFailureResumeTests(unittest.IsolatedAsyncioTestCase):
    """Сбой ПОСЛЕ финансового шага (fill_view_price объявления №2 из 3):
    объявление №2 уже создано на Авито — checkpoint отражает это, resume
    пропускает №2 (без повторного денежного клика) и публикует №3."""

    async def test_checkpoint_marks_financial_step_and_resume_skips_created_item(
        self,
    ) -> None:
        with H.Harness(3) as h:
            h.avito.faults[ev("fill_view_price", 2)] = "exc"
            await h.start()
            self.assertEqual(await h.wait_task(), "ok")

            job_dir = h.publish_root / h.job_id
            saved_job, _draft = publish_state.load_publish_state(job_dir)
            self.assertEqual(saved_job["step"], "fill_view_price")
            self.assertEqual(saved_job["item_index"], 2)

            plan = publish_state.resume_plan(saved_job)
            self.assertEqual(plan, {
                "mode": "skip_item",
                "start_index": 3,
                "skipped_item": 2,
                "items_total": 3,
            })

            answers = await H.resume_to_end(h)
            self.assertEqual(answers[0][0], 200, f"resume отклонён: {answers[0][1]}")

        # №2 создан ровно один раз (сбой случился ДО оплаты, второго
        # денежного клика не было), №1 и №3 отправлены — дублей нет.
        self.assertEqual(dict(h.avito.created_indices()), {1: 1, 2: 1, 3: 1})
        repeated = {k: v for k, v in h.avito.clicks.items() if v > 1}
        self.assertEqual(repeated, {}, "денежный клик повторён при resume")


if __name__ == "__main__":
    unittest.main(verbosity=2)
