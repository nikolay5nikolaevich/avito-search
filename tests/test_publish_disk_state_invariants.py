"""
Инварианты чекпоинта на диске — облегчённый перенос
tmp/audit/verify2/A/test_a_verify.py, класс A1_ReachableDiskStates.

Идея: перехватить КАЖДУЮ запись checkpoint'а, которую настоящий код
(publish_state.save_publish_state) реально делает на диск в разных точках
сбоя, и проверить на каждой записи:
  - есть items_total/items_published/item_index/step, step не None;
  - item_index <= items_published + |skipped| + 1;
  - step == "" только при item_index == 0;
  - 1 <= items_total <= DRAFTS_MAX, resume_plan не выходит за эти границы.

Урезано относительно оригинала (там же — быстрый режим без A_FULL=1 занял
~11.5 минуты живого времени, что слишком много для обычного прогона тестов):
  - только n_items=3 (не 1/2/3);
  - только сбои exc/kill, каждый раз с перезапуском сервера после сбоя
    (kind="cancel" и вариант без перезапуска не проверяются);
  - без двойных сбоев (второй сбой уже во время resume) — самая тяжёлая
    часть оригинала;
  - без сценариев без prep_id (авто-подготовка вариантов).
Итог: 72 точки сбоя × 2 вида = 144 сценария вместо ~1700+ в оригинале.
Тесты фиксируют СЕГОДНЯШНЕЕ поведение, а не находки аудита (F09-F12 и т.п.)
как «ожидаемо падает» — если инвариант нарушен уже сегодня, тест должен был
бы падать; известные находки чинят приёмочные тесты других брифов.

Запуск: .venv\\Scripts\\python.exe -m unittest tests.test_publish_disk_state_invariants -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from collections import Counter
from pathlib import Path
from typing import Any
from unittest import mock

# tests/ в sys.path — нужно для «голого» import publish_money_harness ниже
# (тот сам добавит backend/ и остальное при своём импорте).
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

import publish_money_harness as H  # noqa: E402
import publish_state  # noqa: E402
import publisher  # noqa: E402

ORIG_SAVE = H.REAL_SAVE
N_ITEMS = 3
FAULT_KINDS_LITE = ("exc", "kill")


class Recorder:
    """Обёртка над настоящей save_publish_state: пишет как обычно, затем
    читает файл с диска обратно и запоминает job."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    def __call__(self, job_dir: Path, job_id: str, job: dict[str, Any], draft: Any) -> Path:
        path = Path(job_dir) / publish_state.STATE_FILENAME
        result = ORIG_SAVE(job_dir, job_id, job, draft)
        on_disk = json.loads(path.read_text(encoding="utf-8"))["job"]
        self.jobs.append(on_disk)
        return result


def violations(job: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("items_total", "items_published", "item_index", "step"):
        if key not in job:
            out.append(f"нет ключа {key}")
    if out:
        return out
    if job["step"] is None:
        out.append("step=None")
    total = int(job["items_total"])
    pub = int(job["items_published"])
    idx = int(job["item_index"])
    # Бриф 05: skipped_items — записи {"item_index": N, ...}, старый формат —
    # числа; оба читает publish_state.skipped_numbers.
    skipped = publish_state.skipped_numbers(job.get("skipped_items"))
    pending = pub + len(skipped) + 1
    if idx > pending:
        out.append(f"item_index={idx} > pending={pending}")
    if (job["step"] or "") == "" and idx != 0:
        out.append(f"пустой step при item_index={idx}")
    if not (publisher.DRAFTS_MIN <= total <= publisher.DRAFTS_MAX):
        out.append(f"items_total={total} вне [{publisher.DRAFTS_MIN}, {publisher.DRAFTS_MAX}]")
    plan = publish_state.resume_plan(job)
    if plan is not None:
        clamped_total = max(publisher.DRAFTS_MIN, min(publisher.DRAFTS_MAX, total))
        if max(1, min(clamped_total, plan["start_index"])) != plan["start_index"]:
            out.append(f"clamp меняет start_index {plan['start_index']}")
    return out


async def _run_single_fault_scenario(n: int, event: int, kind: str) -> None:
    """Один сбой в точке event, всегда с перезапуском сервера, затем resume
    до конца (или до отказа)."""
    with H.Harness(n) as h:
        h.avito.faults[event] = kind
        await h.start()
        if h.job_id:
            await h.wait_task()
        h.restart()
        await H.resume_to_end(h, limit=n + 4)


class A1LiteReachableDiskStates(unittest.TestCase):
    """Облегчённая версия A1: если хоть одна запись нарушит инвариант —
    находка достижима кодом уже на маленькой матрице сбоев."""

    recorder: Recorder
    scenarios = 0

    @classmethod
    def setUpClass(cls) -> None:
        cls.recorder = Recorder()
        trace = H.clean_trace(N_ITEMS)
        with mock.patch.object(H, "REAL_SAVE", cls.recorder):
            for event, _name, _idx in trace:
                for kind in FAULT_KINDS_LITE:
                    asyncio.run(_run_single_fault_scenario(N_ITEMS, event, kind))
                    cls.scenarios += 1

    def test_every_written_checkpoint_satisfies_invariants(self) -> None:
        bad = Counter()
        examples: dict[str, dict[str, Any]] = {}
        for job in self.recorder.jobs:
            for v in violations(job):
                key = v[:30]
                bad[key] += 1
                examples.setdefault(key, {k: job.get(k) for k in (
                    "status", "step", "item_index", "items_published",
                    "items_total", "skipped_items")})
        print(
            f"\n[A1-lite] сценариев: {self.scenarios}; "
            f"записей checkpoint: {len(self.recorder.jobs)}; нарушений: {sum(bad.values())}"
        )
        self.assertGreater(len(self.recorder.jobs), 50, "стенд: слишком мало записей")
        self.assertFalse(bad, f"нарушения: {dict(bad)}; примеры: {examples}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
