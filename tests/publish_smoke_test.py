"""
Smoke-тест веб-слоя publish (JSON/multipart API): POST /api/publish/start →
GET /api/publish/status → GET /api/publish/result.

Запуск (без новых зависимостей):
    .venv\\Scripts\\python.exe tests\\publish_smoke_test.py

Сервер запускается в фоновом потоке через uvicorn (хелперы — tests/smoke_helpers.py).
HTTP-запросы делаются через встроенный urllib — httpx/requests не нужны.
Реальный publisher.run_publish_job НЕ вызывается — используется синтетическая
async-заглушка из фабрики _make_fake_job(), которая прогоняет статусы
queued → running (несколько шагов) → done (или обрыв на черновике fail_at).

Позитивный сценарий (одиночный черновик, обратная совместимость):
    POST /api/publish/start (multipart: все обязательные поля + 2 PNG-фото, без drafts_count)
    → получаем job_id
    → опрашиваем /api/publish/status/{job_id} до терминала
    → статус done, drafts_total=1
    → GET /api/publish/result/{job_id} → есть сводка.

Позитивный сценарий (пакетный режим, ТЗ §16):
    POST /api/publish/start (drafts_count=3)
    → polling → done; drafts_total=3, drafts_saved=3, saved_urls — 3 элемента.

Негативные кейсы (HTTP 422) — таблица NEGATIVE_CASES:
    - пустое название
    - цена 0
    - 11 фото
    - цвет не из словаря
    - drafts_count=0
    - drafts_count=11
    - drafts_count="abc"

Частичный успех (пакетный режим):
    Заглушка имитирует обрыв на черновике 2 из 3: терминальный статус failed/needs_user_action,
    drafts_saved=1, saved_urls из 1 элемента, в error упоминание «1 из 3».

Проверка фикса маршрутов:
    - GET /draft → 200, Content-Type text/html
    - GET / → 200 (регрессия не сломана)
"""

import asyncio
import json
import logging
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest.mock as mock
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any, Awaitable, Callable

# Каталог tests/, корень проекта и backend/ в sys.path
_tests_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_tests_dir)
_backend_dir = os.path.join(_project_root, "backend")
for _p in (_tests_dir, _project_root, _backend_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from smoke_helpers import http_get, start_server, wait_server_ready  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)-8s %(name)s %(message)s",
)
logger = logging.getLogger("publish_smoke_test")

TEST_PORT = 18766  # порт, не конфликтующий ни с продакшном, ни с smoke_test.py

BASE_URL = f"http://127.0.0.1:{TEST_PORT}"

# ---------------------------------------------------------------------------
# Генерация маленьких настоящих PNG-байтов (без внешних зависимостей)
# ---------------------------------------------------------------------------

def _make_tiny_png(width: int = 2, height: int = 2) -> bytes:
    """
    Генерирует минимальный валидный PNG (width×height пикселей, RGB).
    Использует только stdlib: struct + zlib.
    """
    def _chunk(name: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)
        return length + name + data + crc

    # IHDR: 8 байт
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = _chunk(b"IHDR", ihdr_data)

    # IDAT: сырые строки + filter byte
    raw_rows = b""
    for _row in range(height):
        # filter-byte 0 (None) + RGB-пиксели
        raw_rows += b"\x00" + b"\xFF\x00\x00" * width  # красный ряд

    compressed = zlib.compress(raw_rows)
    idat = _chunk(b"IDAT", compressed)

    iend = _chunk(b"IEND", b"")

    signature = b"\x89PNG\r\n\x1a\n"
    return signature + ihdr + idat + iend


# ---------------------------------------------------------------------------
# Фабрика синтетической заглушки publisher.run_publish_job
# ---------------------------------------------------------------------------

def _make_fake_job(fail_at: int | None = None) -> Callable[..., Awaitable[None]]:
    """
    Возвращает async-заглушку publisher.run_publish_job (без реального браузера).

    Поддерживает пакетный режим (ТЗ §16): читает job["drafts_total"],
    прогоняет черновики 1..N, обновляет draft_index/drafts_saved/saved_urls.

    fail_at=None — успешный прогон всех черновиков, финальный статус done.
    fail_at=N   — имитация обрыва (StepError/капча) на черновике N:
                  черновики 1..N-1 сохранены, статус failed,
                  error через publisher._format_partial_error.
    """
    async def _fake_run_publish_job(
        job_id: str,
        job: dict[str, Any],
        data: Any,
        *,
        cdp_url: Any = None,
        tmp_dir: Any = None,
    ) -> None:
        import publisher as pub_mod

        # Сколько черновиков — из состояния задачи; мусор → дефолт
        try:
            drafts_total = int(job.get("drafts_total") or pub_mod.DRAFTS_DEFAULT)
        except (TypeError, ValueError):
            drafts_total = pub_mod.DRAFTS_DEFAULT
        drafts_total = max(pub_mod.DRAFTS_MIN, min(pub_mod.DRAFTS_MAX, drafts_total))

        job["status"] = "running"
        job["total"] = pub_mod.TOTAL_STEPS
        job["drafts_total"] = drafts_total
        job["draft_index"] = 0
        job["drafts_saved"] = 0
        job["saved_urls"] = []

        steps = [s for s, _ in pub_mod.STEPS if s != "done"]

        for draft_index in range(1, drafts_total + 1):
            job["draft_index"] = draft_index

            # Имитация ошибки (капча / обрыв) на черновике fail_at
            if fail_at is not None and draft_index == fail_at:
                job["step"] = "open_form"
                job["step_label"] = pub_mod.STEP_LABELS.get("open_form", "Открытие формы Avito")
                await asyncio.sleep(0)
                # Сообщение с частичным успехом (как делает _format_partial_error)
                base_msg = f"Тест: имитация обрыва на черновике {fail_at}."
                job["error"] = pub_mod._format_partial_error(
                    base_msg, job["drafts_saved"], drafts_total
                )
                job["status"] = "failed"
                logger.debug(
                    "Синтетический run_publish_job: задача %s — обрыв на черновике %d",
                    job_id, fail_at,
                )
                return

            # Прогоняем шаги текущего черновика
            for i, step_name in enumerate(steps, start=1):
                job["step"] = step_name
                job["step_label"] = pub_mod.STEP_LABELS.get(step_name, step_name)
                job["done"] = i
                await asyncio.sleep(0)  # отдаём event loop

            # Черновик сохранён
            fake_url = f"https://www.avito.ru/profile/ad/{12345678 + draft_index - 1}"
            job["drafts_saved"] = draft_index
            job["saved_urls"].append(fake_url)
            job["result_url"] = fake_url

        # Финальный шаг done
        job["step"] = "done"
        job["step_label"] = pub_mod.STEP_LABELS.get("done", "Готово")
        job["done"] = pub_mod.TOTAL_STEPS
        job["status"] = "done"
        logger.debug(
            "Синтетический run_publish_job: задача %s завершена (%d черновиков)",
            job_id, drafts_total,
        )

    return _fake_run_publish_job


# ---------------------------------------------------------------------------
# HTTP-хелперы (общие — в smoke_helpers; здесь только специфичные)
# ---------------------------------------------------------------------------

def _get(path: str) -> tuple[int, str, dict]:
    """GET к тестовому серверу (обёртка над smoke_helpers.http_get)."""
    return http_get(BASE_URL, path)


def _post_multipart(
    path: str,
    fields: dict[str, str],
    files: list[tuple[str, str, bytes]],   # (field_name, filename, content)
) -> tuple[int, str, dict]:
    """
    POST multipart/form-data (без внешних зависимостей).
    files: список (field_name, filename, content_bytes).
    """
    boundary = "----SmokeTestBoundary7a3f9e2b"
    body_parts: list[bytes] = []

    # Текстовые поля
    for name, value in fields.items():
        part = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")
        body_parts.append(part)

    # Файловые поля
    for field_name, filename, content in files:
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode("utf-8")
        body_parts.append(header + content + b"\r\n")

    body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(body_parts)

    url = BASE_URL + path
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), dict(e.headers)


def _wait_publish_done(job_id: str, timeout: float = 30.0) -> dict:
    """
    Опрашивает /api/publish/status/{job_id} до получения терминального статуса
    (status != 'running' и status != 'queued').
    """
    terminal = {"done", "failed", "needs_user_action", "not_found"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sc, body, _ = _get(f"/api/publish/status/{job_id}")
        assert sc == 200, f"/api/publish/status/{job_id} вернул {sc}"
        data = json.loads(body)
        if data.get("status") in terminal:
            return data
        time.sleep(0.15)
    raise AssertionError(
        f"Таймаут {timeout}с: задача {job_id} не завершилась"
    )


# ---------------------------------------------------------------------------
# Вспомогательные поля: валидные значения для позитивного сценария
# ---------------------------------------------------------------------------

def _valid_fields() -> dict[str, str]:
    """Возвращает набор валидных текстовых полей формы черновика."""
    return {
        "title": "Тестовый мужской пиджак",
        "trade_type": "Продаю своё",
        "condition": "Отличное",
        "size": "48 (M)",
        "brand": "Test Brand",
        "color": "Чёрный",
        "description": "Тестовое описание объявления для smoke-теста.",
        "price": "5000",
        "city": "Москва",
        "address": "ул. Тверская, 1",
    }


# Негативные кейсы валидации: один плохой параметр → HTTP 422.
# (описание, override-поля поверх _valid_fields, число фото, поле в errors)
NEGATIVE_CASES: list[tuple[str, dict[str, str], int, str]] = [
    ("пустое название", {"title": ""}, 1, "title"),
    ("цена 0", {"price": "0"}, 1, "price"),
    ("11 фото", {}, 11, "photos"),
    ("цвет 'Хаки' вне словаря", {"color": "Хаки"}, 1, "color"),
    ("drafts_count=0", {"drafts_count": "0"}, 1, "drafts_count"),
    ("drafts_count=11", {"drafts_count": "11"}, 1, "drafts_count"),
    ("drafts_count='abc'", {"drafts_count": "abc"}, 1, "drafts_count"),
]


# ---------------------------------------------------------------------------
# Основной тест
# ---------------------------------------------------------------------------

def run_publish_smoke_test() -> None:
    """Сквозной smoke-тест веб-слоя publish на синтетических данных."""
    print("=== Publish Smoke-тест начат ===")
    checks_passed = 0

    # Генерируем маленькие PNG для фото-полей
    png1 = _make_tiny_png(2, 2)
    png2 = _make_tiny_png(3, 3)
    assert len(png1) > 0, "Генерация PNG провалилась"

    # Импортируем app и publisher до патчинга.
    # app.py делает «import publisher» — это ТОТ ЖЕ объект модуля, что
    # pub_module, поэтому одного патча run_publish_job достаточно.
    import app as app_module
    import publisher as pub_module

    with mock.patch.object(pub_module, "run_publish_job", _make_fake_job()):
        start_server(app_module.app, TEST_PORT)
        wait_server_ready(BASE_URL, timeout=15.0)

        # ── Шаг 1: GET / → 200 (регрессия не сломана) ────────────────────
        print("Шаг 1: GET / → 200")
        sc, body, headers = _get("/")
        assert sc == 200, f"GET / вернул {sc}, ожидали 200"
        ct = headers.get("content-type", headers.get("Content-Type", ""))
        assert "text/html" in ct.lower(), (
            f"GET / должен вернуть text/html, получили: {ct}"
        )
        checks_passed += 1
        print(f"  Проверка 1 PASS: GET / → 200, Content-Type={ct!r}")

        # ── Шаг 2: GET /draft → 200, Content-Type text/html ──────────────
        print("Шаг 2: GET /draft → 200 (проверка нового маршрута)")
        sc, body, headers = _get("/draft")
        assert sc == 200, (
            f"GET /draft вернул {sc}, ожидали 200. "
            "Маршрут /draft не зарегистрирован в app.py"
        )
        ct = headers.get("content-type", headers.get("Content-Type", ""))
        assert "text/html" in ct.lower(), (
            f"GET /draft должен вернуть text/html, получили: {ct}"
        )
        checks_passed += 1
        print(f"  Проверка 2 PASS: GET /draft → 200, Content-Type={ct!r}")

        # ── Шаг 3: позитивный сценарий — обратная совместимость (без drafts_count) ──
        print("Шаг 3: POST /api/publish/start (без drafts_count — обратная совместимость)")
        fields = _valid_fields()  # drafts_count не передаём → дефолт 1
        files = [
            ("photos", "photo_01.png", png1),
            ("photos", "photo_02.png", png2),
        ]
        sc, body, _ = _post_multipart("/api/publish/start", fields, files)
        assert sc == 200, (
            f"POST /api/publish/start вернул {sc}, ожидали 200. Тело: {body[:300]}"
        )
        data = json.loads(body)
        job_id = data.get("job_id")
        assert job_id, f"Ответ /api/publish/start не содержит job_id: {data}"
        checks_passed += 1
        print(f"  Проверка 3 PASS: job_id получен ({job_id[:8]}…)")

        # ── Шаг 4: polling /api/publish/status до терминала ───────────────
        print(f"Шаг 4: polling /api/publish/status/{job_id[:8]}…")
        final_status = _wait_publish_done(job_id, timeout=20.0)
        assert final_status.get("status") == "done", (
            f"Ожидали status='done', получили: {final_status}"
        )
        checks_passed += 1
        print(f"  Проверка 4 PASS: status=done")

        # Структура ответа статуса
        for key in ("job_id", "status", "step", "step_label", "done", "total"):
            assert key in final_status, (
                f"В /api/publish/status нет обязательного поля '{key}': {final_status}"
            )
        assert final_status["done"] == final_status["total"], (
            f"done={final_status['done']} != total={final_status['total']}"
        )
        # Обратная совместимость: без drafts_count → drafts_total=1
        assert final_status.get("drafts_total") == 1, (
            f"Без drafts_count drafts_total должен быть 1, получили: "
            f"{final_status.get('drafts_total')}"
        )
        checks_passed += 1
        print(f"  Проверка 5 PASS: структура статуса корректна "
              f"(done={final_status['done']}/{final_status['total']}, "
              f"drafts_total={final_status.get('drafts_total')})")

        # ── Шаг 5: GET /api/publish/result/{job_id} ──────────────────────
        print(f"Шаг 5: GET /api/publish/result/{job_id[:8]}…")
        sc, body, _ = _get(f"/api/publish/result/{job_id}")
        assert sc == 200, (
            f"GET /api/publish/result/{job_id} вернул {sc}: {body[:200]}"
        )
        result = json.loads(body)
        assert result.get("status") == "done", (
            f"В результате ожидали status='done': {result}"
        )
        summary = result.get("summary")
        assert isinstance(summary, dict) and summary, (
            f"В результате нет сводки (summary): {result}"
        )
        assert summary.get("title") == fields["title"], (
            f"Сводка содержит неверный заголовок: {summary.get('title')!r}"
        )
        assert summary.get("price") == int(fields["price"]), (
            f"Сводка содержит неверную цену: {summary.get('price')!r}"
        )
        assert "photo_paths" not in summary, (
            "В сводке не должно быть путей к фото (персональные данные)"
        )
        checks_passed += 1
        print(f"  Проверка 6 PASS: результат содержит корректную сводку "
              f"(title={summary.get('title')!r}, price={summary.get('price')})")

        # ── Шаги 6–12: негативные кейсы по таблице NEGATIVE_CASES → 422 ───
        for step_no, (case_name, overrides, n_photos, expected_field) in enumerate(
            NEGATIVE_CASES, start=6
        ):
            print(f"Шаг {step_no}: негативный кейс — {case_name} → 422")
            bad_fields = dict(_valid_fields(), **overrides)
            photos = [("photos", f"photo_{i:02d}.png", png1) for i in range(n_photos)]
            sc, body, _ = _post_multipart("/api/publish/start", bad_fields, photos)
            assert sc == 422, (
                f"Кейс «{case_name}»: ожидали 422, получили {sc}. Тело: {body[:300]}"
            )
            resp_data = json.loads(body)
            error_fields = {e["field"] for e in resp_data.get("errors", [])}
            assert expected_field in error_fields, (
                f"Кейс «{case_name}»: в 422-ответе нет ошибки поля "
                f"{expected_field!r}: {resp_data}"
            )
            checks_passed += 1
            print(f"  Проверка {step_no + 1} PASS: {case_name} → 422, поля={error_fields}")

        # ── Шаг 13: пакетный режим — drafts_count=3 → done, 3 черновика ──
        print("Шаг 13: пакетный режим — drafts_count=3 → done, drafts_saved=3")
        batch_fields = dict(_valid_fields(), drafts_count="3")
        sc, body, _ = _post_multipart(
            "/api/publish/start",
            batch_fields,
            [("photos", "photo_01.png", png1), ("photos", "photo_02.png", png2)],
        )
        assert sc == 200, (
            f"POST /api/publish/start (drafts_count=3) вернул {sc}. Тело: {body[:300]}"
        )
        data = json.loads(body)
        batch_job_id = data.get("job_id")
        assert batch_job_id, (
            f"Ответ /api/publish/start не содержит job_id: {data}"
        )
        checks_passed += 1
        print(f"  Проверка 14 PASS: job_id для пакетного режима получен ({batch_job_id[:8]}…)")

        # Polling до терминала
        print(f"Шаг 14: polling пакетной задачи {batch_job_id[:8]}…")
        batch_final = _wait_publish_done(batch_job_id, timeout=20.0)
        assert batch_final.get("status") == "done", (
            f"Пакетная задача: ожидали status='done', получили: {batch_final}"
        )
        assert batch_final.get("drafts_total") == 3, (
            f"Пакетная задача: ожидали drafts_total=3, получили: "
            f"{batch_final.get('drafts_total')}"
        )
        assert batch_final.get("drafts_saved") == 3, (
            f"Пакетная задача: ожидали drafts_saved=3, получили: "
            f"{batch_final.get('drafts_saved')}"
        )
        checks_passed += 1
        print(
            f"  Проверка 15 PASS: пакет done, "
            f"drafts_total={batch_final.get('drafts_total')}, "
            f"drafts_saved={batch_final.get('drafts_saved')}"
        )

        # Результат пакетной задачи: saved_urls — 3 элемента
        print(f"Шаг 15: GET /api/publish/result/{batch_job_id[:8]}… → 3 saved_urls")
        sc, body, _ = _get(f"/api/publish/result/{batch_job_id}")
        assert sc == 200, (
            f"GET /api/publish/result (пакет) вернул {sc}: {body[:200]}"
        )
        batch_result = json.loads(body)
        saved_urls = batch_result.get("saved_urls", [])
        assert isinstance(saved_urls, list), (
            f"saved_urls должен быть списком, получили: {type(saved_urls)}"
        )
        assert len(saved_urls) == 3, (
            f"Ожидали 3 saved_urls, получили {len(saved_urls)}: {saved_urls}"
        )
        # Каждый URL — непустая строка
        for idx, url in enumerate(saved_urls, start=1):
            assert isinstance(url, str) and url.startswith("https://"), (
                f"saved_urls[{idx}] невалидный URL: {url!r}"
            )
        checks_passed += 1
        print(
            f"  Проверка 16 PASS: saved_urls содержит 3 элемента "
            f"({', '.join(u[-8:] for u in saved_urls)}…)"
        )

    # ── Шаг 16: частичный успех — обрыв на черновике 2 из 3 ──────────────
    # Та же фабрика, но с fail_at=2; сервер уже запущен — просто шлём запрос.
    print("Шаг 16: частичный успех — обрыв на черновике 2 из 3")

    with mock.patch.object(pub_module, "run_publish_job", _make_fake_job(fail_at=2)):
        partial_fields = dict(_valid_fields(), drafts_count="3")
        sc, body, _ = _post_multipart(
            "/api/publish/start",
            partial_fields,
            [("photos", "photo_p.png", png1)],
        )
        assert sc == 200, (
            f"POST /api/publish/start (partial) вернул {sc}. Тело: {body[:300]}"
        )
        partial_job_id = json.loads(body).get("job_id")
        assert partial_job_id, f"Нет job_id в ответе partial: {body[:200]}"

        # Polling
        partial_final = _wait_publish_done(partial_job_id, timeout=20.0)
        terminal_statuses = {"failed", "needs_user_action"}
        assert partial_final.get("status") in terminal_statuses, (
            f"Частичный сбой: ожидали failed/needs_user_action, "
            f"получили: {partial_final.get('status')}"
        )
        checks_passed += 1
        print(
            f"  Проверка 17 PASS: частичный сбой — "
            f"статус={partial_final.get('status')!r}"
        )

        # drafts_saved=1, saved_urls из 1 элемента
        assert partial_final.get("drafts_saved") == 1, (
            f"Частичный сбой: ожидали drafts_saved=1, "
            f"получили: {partial_final.get('drafts_saved')}"
        )
        checks_passed += 1
        print(f"  Проверка 18 PASS: drafts_saved=1")

        # Результат: saved_urls из 1 элемента
        sc, body, _ = _get(f"/api/publish/result/{partial_job_id}")
        assert sc == 200, (
            f"GET /api/publish/result (partial) вернул {sc}: {body[:200]}"
        )
        partial_result = json.loads(body)
        partial_urls = partial_result.get("saved_urls", [])
        assert len(partial_urls) == 1, (
            f"Частичный сбой: ожидали 1 saved_url, получили {len(partial_urls)}: "
            f"{partial_urls}"
        )
        checks_passed += 1
        print(f"  Проверка 19 PASS: saved_urls содержит 1 элемент ({partial_urls[0][-20:]!r})")

        # В error упоминается «1 из 3»
        error_text = partial_final.get("error") or ""
        assert "1 из 3" in error_text, (
            f"Частичный сбой: в поле error ожидали упоминание '1 из 3', "
            f"получили: {error_text!r}"
        )
        checks_passed += 1
        print(f"  Проверка 20 PASS: error содержит '1 из 3' ({error_text!r})")

    # ── Шаги 17–18: интеграция publish с prep_id (ТЗ §17, Задача 8.3) ────────
    # Сборка минимального prep вручную: две папки draft_01/draft_02 с текстами
    # и фото, запись в PREP_JOBS со status="done", POST start с prep_id.
    print("Шаг 17: start с валидным prep_id → job создан, job[prep_id] сохранён")

    _tmp_prep_dir: str | None = None
    _prep_id_integ = "smoke-prep-integration-0001"
    try:
        # Создаём tmp-директорию под prep
        _tmp_prep_dir = tempfile.mkdtemp(prefix="publish_smoke_prep_")
        _prep_base = Path(_tmp_prep_dir)

        # Создаём структуру draft_01 и draft_02
        for _draft_num in (1, 2):
            _dd = _prep_base / f"draft_{_draft_num:02d}"
            _dd_photos = _dd / "photos"
            _dd_photos.mkdir(parents=True, exist_ok=True)
            (_dd / "title.txt").write_text(f"Заголовок {_draft_num}", encoding="utf-8")
            (_dd / "text.txt").write_text(f"Описание {_draft_num}", encoding="utf-8")
            # Синтетическое JPEG-фото (минимальный валидный файл)
            (_dd_photos / "photo_01.jpg").write_bytes(png1)

        # Патчим TMP_PUBLISH_DIR в app_module так, чтобы сервер нашёл prep_dir
        # Проще: регистрируем prep_id напрямую в PREP_JOBS и кладём папку
        # туда, куда app.py ожидает: TMP_PUBLISH_DIR / f"prep_{prep_id}"
        _real_tmp_publish = app_module.TMP_PUBLISH_DIR
        _expected_prep_dir = _real_tmp_publish / f"prep_{_prep_id_integ}"
        if _expected_prep_dir.exists():
            shutil.rmtree(_expected_prep_dir)
        # Копируем нашу заглушку в ожидаемое место
        shutil.copytree(_prep_base, _expected_prep_dir)

        # Регистрируем в PREP_JOBS
        app_module.PREP_JOBS[_prep_id_integ] = {
            "status": "done",
            "step": "done",
            "step_label": "Готово",
            "done": 3,
            "total": 3,
            "error": None,
            "drafts_count": 2,
        }

        # Теперь POST /api/publish/start с prep_id (и валидными остальными полями)
        with mock.patch.object(pub_module, "run_publish_job", _make_fake_job()):
            integ_fields = dict(_valid_fields(), prep_id=_prep_id_integ)
            sc, body, _ = _post_multipart(
                "/api/publish/start",
                integ_fields,
                [("photos", "photo_01.png", png1)],
            )
            assert sc == 200, (
                f"start с prep_id: ожидали 200, получили {sc}. Тело: {body[:300]}"
            )
            integ_data = json.loads(body)
            integ_job_id = integ_data.get("job_id")
            assert integ_job_id, f"start с prep_id: нет job_id в ответе: {integ_data}"

            # Проверяем что PUBLISH_JOBS содержит prep_id
            integ_job = app_module.PUBLISH_JOBS.get(integ_job_id)
            assert integ_job is not None, (
                f"start с prep_id: задача {integ_job_id} не найдена в PUBLISH_JOBS"
            )
            assert integ_job.get("prep_id") == _prep_id_integ, (
                f"start с prep_id: ожидали prep_id={_prep_id_integ!r}, "
                f"получили: {integ_job.get('prep_id')!r}"
            )
            checks_passed += 1
            print(
                f"  Проверка 21 PASS: start с prep_id → job_id={integ_job_id[:8]}…, "
                f"PUBLISH_JOBS[job_id]['prep_id']={integ_job.get('prep_id')[:8]}…"
            )

        # ── Шаг 18: start с мусорным prep_id → 422 ──────────────────────────
        print("Шаг 18: start с мусорным prep_id → 422")
        garbage_fields = dict(_valid_fields(), prep_id="мусор-неизвестный-prep")
        sc_bad, body_bad, _ = _post_multipart(
            "/api/publish/start",
            garbage_fields,
            [("photos", "photo_g.png", png1)],
        )
        assert sc_bad == 422, (
            f"start с мусорным prep_id: ожидали 422, получили {sc_bad}. "
            f"Тело: {body_bad[:300]}"
        )
        bad_data = json.loads(body_bad)
        bad_fields = {e["field"] for e in bad_data.get("errors", [])}
        assert "prep_id" in bad_fields, (
            f"start с мусорным prep_id: в 422-ответе нет поля 'prep_id': {bad_data}"
        )
        checks_passed += 1
        print(f"  Проверка 22 PASS: start с мусорным prep_id → 422 (поля={bad_fields})")

    finally:
        # Подчищаем tmp-артефакты (best effort)
        if _tmp_prep_dir and os.path.exists(_tmp_prep_dir):
            try:
                shutil.rmtree(_tmp_prep_dir)
            except OSError:
                pass
        _cleanup_path = app_module.TMP_PUBLISH_DIR / f"prep_{_prep_id_integ}"
        if _cleanup_path.exists():
            try:
                shutil.rmtree(_cleanup_path)
            except OSError:
                pass
        # Убираем из PREP_JOBS
        app_module.PREP_JOBS.pop(_prep_id_integ, None)

    print(f"\n=== PUBLISH SMOKE TEST: OK: {checks_passed} проверок ===")


# ---------------------------------------------------------------------------
# Самозапуск
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        run_publish_smoke_test()
        sys.exit(0)
    except AssertionError as e:
        print(f"\n[FAIL] {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\n[ERROR] {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(2)
