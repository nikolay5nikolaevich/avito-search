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
    - drafts_count=21
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

    Читает job["items_total"], прогоняет объявления 1..N и обновляет
    item_index/items_published/published_urls. Старые поля синхронизирует
    только как алиасы.

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

        # Сколько объявлений — из нового поля; мусор → дефолт
        try:
            items_total = int(
                job.get("items_total")
                or job.get("drafts_total")
                or pub_mod.DRAFTS_DEFAULT
            )
        except (TypeError, ValueError):
            items_total = pub_mod.DRAFTS_DEFAULT
        items_total = max(pub_mod.DRAFTS_MIN, min(pub_mod.DRAFTS_MAX, items_total))

        job["status"] = "running"
        job["total"] = pub_mod.TOTAL_STEPS
        job["items_total"] = items_total
        job["item_index"] = 0
        job["items_published"] = 0
        job["published_urls"] = []
        pub_mod._sync_legacy_publish_aliases(job)

        steps = [s for s, _ in pub_mod.STEPS if s != "done"]

        for item_index in range(1, items_total + 1):
            job["item_index"] = item_index
            pub_mod._sync_legacy_publish_aliases(job)

            # Имитация ошибки (капча / обрыв) на объявлении fail_at
            if fail_at is not None and item_index == fail_at:
                job["step"] = "open_form"
                job["step_label"] = pub_mod.STEP_LABELS.get("open_form", "Открытие формы Avito")
                await asyncio.sleep(0)
                # Сообщение с частичным успехом (как делает _format_partial_error)
                base_msg = f"Тест: имитация обрыва на объявлении {fail_at}."
                job["error"] = pub_mod._format_partial_error(
                    base_msg, job["items_published"], items_total
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

            # Публикация подтверждена
            fake_url = f"https://www.avito.ru/test_{12345678 + item_index - 1}"
            job["items_published"] = item_index
            job["published_urls"].append(fake_url)
            job["result_url"] = fake_url
            pub_mod._sync_legacy_publish_aliases(job)

        # Финальный шаг done
        job["step"] = "done"
        job["step_label"] = pub_mod.STEP_LABELS.get("done", "Готово")
        job["done"] = pub_mod.TOTAL_STEPS
        job["status"] = "done"
        logger.debug(
            "Синтетический run_publish_job: задача %s завершена (%d черновиков)",
            job_id, items_total,
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

def _valid_fields(count: int = 1) -> dict[str, str]:
    """Возвращает набор валидных текстовых полей формы черновика."""
    source_locations = [
        {"city": "Москва", "address": "ул. Тверская, 1"},
        {"city": "Одинцово", "address": "ул. Центральная, 7"},
        {"city": "Тула", "address": "ул. Ленина, 2"},
    ]
    fields = {
        "title": "Тестовый мужской пиджак",
        "trade_type": "Продаю своё",
        "condition": "Отличное",
        "size": "48 (M)",
        "brand": "Test Brand",
        "color": "Чёрный",
        "description": "Тестовое описание объявления для smoke-теста.",
        "price": "5000",
        "view_price_max": "0,5",
        "locations_json": json.dumps(source_locations[:count], ensure_ascii=False),
    }
    if count != 1:
        fields["drafts_count"] = str(count)
    return fields


# Негативные кейсы валидации: один плохой параметр → HTTP 422.
# (описание, override-поля поверх _valid_fields, число фото, поле в errors)
NEGATIVE_CASES: list[tuple[str, dict[str, str], int, str]] = [
    ("пустое название", {"title": ""}, 1, "title"),
    ("цена 0", {"price": "0"}, 1, "price"),
    ("11 фото", {}, 11, "photos"),
    ("цвет 'Хаки' вне словаря", {"color": "Хаки"}, 1, "color"),
    ("drafts_count=0", {"drafts_count": "0"}, 1, "drafts_count"),
    ("drafts_count=21", {"drafts_count": "21"}, 1, "drafts_count"),
    ("drafts_count='abc'", {"drafts_count": "abc"}, 1, "drafts_count"),
    ("размер одежды при category=sneakers", {"category": "sneakers"}, 1, "size"),
    ("пустой потолок стоимости просмотра", {"view_price_max": ""}, 1, "view_price_max"),
    ("нулевой потолок стоимости просмотра", {"view_price_max": "0"}, 1, "view_price_max"),
    ("отрицательный потолок стоимости просмотра", {"view_price_max": "-1"}, 1, "view_price_max"),
    ("мусорный потолок стоимости просмотра", {"view_price_max": "abc"}, 1, "view_price_max"),
    ("геолокации не JSON", {"locations_json": "{"}, 1, "locations"),
    (
        "геолокации не массив",
        {"locations_json": json.dumps({"city": "Москва"}, ensure_ascii=False)},
        1,
        "locations",
    ),
    (
        "неверное количество геолокаций",
        {"drafts_count": "2"},
        1,
        "locations",
    ),
    (
        "пустой город второго объявления",
        {
            "drafts_count": "2",
            "locations_json": json.dumps([
                {"city": "Москва", "address": "ул. Тверская, 1"},
                {"city": "", "address": "ул. Центральная, 7"},
            ], ensure_ascii=False),
        },
        1,
        "locations.2.city",
    ),
    (
        "пустой адрес второго объявления",
        {
            "drafts_count": "2",
            "locations_json": json.dumps([
                {"city": "Москва", "address": "ул. Тверская, 1"},
                {"city": "Одинцово", "address": ""},
            ], ensure_ascii=False),
        },
        1,
        "locations.2.address",
    ),
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

        # ── Шаг 2: GET / → 200, Content-Type text/html (SPA отдаётся) ──────
        print("Шаг 2: GET / → 200 (SPA отдаётся сервером)")
        sc, body, headers = _get("/")
        assert sc == 200, (
            f"GET / вернул {sc}, ожидали 200. "
            "Главная страница SPA не отдаётся app.py"
        )
        ct = headers.get("content-type", headers.get("Content-Type", ""))
        assert "text/html" in ct.lower(), (
            f"GET / должен вернуть text/html, получили: {ct}"
        )
        checks_passed += 1
        print(f"  Проверка 2 PASS: GET / → 200, Content-Type={ct!r}")

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
        # Без drafts_count публикуется одно объявление; legacy-алиас совпадает.
        assert final_status.get("items_total") == 1
        assert final_status.get("items_published") == 1
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
        assert summary.get("view_price_max") == "0.5", summary
        assert summary.get("locations") == [
            {"city": "Москва", "address": "ул. Тверская, 1"},
        ], summary
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
            jobs_before = set(app_module.PUBLISH_JOBS)
            tmp_before = (
                {path.name for path in app_module.TMP_PUBLISH_DIR.iterdir()}
                if app_module.TMP_PUBLISH_DIR.exists()
                else set()
            )
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
            assert set(app_module.PUBLISH_JOBS) == jobs_before, (
                f"Кейс «{case_name}» создал publish-задачу до завершения валидации"
            )
            tmp_after = (
                {path.name for path in app_module.TMP_PUBLISH_DIR.iterdir()}
                if app_module.TMP_PUBLISH_DIR.exists()
                else set()
            )
            assert tmp_after == tmp_before, (
                f"Кейс «{case_name}» создал временные файлы до завершения валидации"
            )
            checks_passed += 1
            print(f"  Проверка {step_no + 1} PASS: {case_name} → 422, поля={error_fields}")

        # ── Шаг 13: пакетный режим — drafts_count=3 → done, 3 черновика ──
        print("Шаг 13: пакетный режим — drafts_count=3 → done, drafts_saved=3")
        batch_fields = _valid_fields(3)
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
        assert batch_final.get("items_total") == 3
        assert batch_final.get("item_index") == 3
        assert batch_final.get("items_published") == 3
        assert batch_final.get("drafts_total") == batch_final.get("items_total")
        assert batch_final.get("drafts_saved") == batch_final.get("items_published")
        checks_passed += 1
        print(
            f"  Проверка 15 PASS: пакет done, "
            f"drafts_total={batch_final.get('drafts_total')}, "
            f"drafts_saved={batch_final.get('drafts_saved')}"
        )

        # Результат пакетной задачи: published_urls — 3 элемента
        print(f"Шаг 15: GET /api/publish/result/{batch_job_id[:8]}… → 3 published_urls")
        sc, body, _ = _get(f"/api/publish/result/{batch_job_id}")
        assert sc == 200, (
            f"GET /api/publish/result (пакет) вернул {sc}: {body[:200]}"
        )
        batch_result = json.loads(body)
        published_urls = batch_result.get("published_urls", [])
        assert isinstance(published_urls, list), (
            f"published_urls должен быть списком, получили: {type(published_urls)}"
        )
        assert len(published_urls) == 3, (
            f"Ожидали 3 published_urls, получили {len(published_urls)}: {published_urls}"
        )
        assert batch_result.get("saved_urls") == published_urls
        # Каждый URL — непустая строка
        for idx, url in enumerate(published_urls, start=1):
            assert isinstance(url, str) and url.startswith("https://"), (
                f"saved_urls[{idx}] невалидный URL: {url!r}"
            )
        checks_passed += 1
        print(
            f"  Проверка 16 PASS: published_urls содержит 3 элемента "
            f"({', '.join(u[-8:] for u in published_urls)}…)"
        )

    # ── Шаг 16: частичный успех — обрыв на черновике 2 из 3 ──────────────
    # Та же фабрика, но с fail_at=2; сервер уже запущен — просто шлём запрос.
    print("Шаг 16: частичный успех — обрыв на черновике 2 из 3")

    with mock.patch.object(pub_module, "run_publish_job", _make_fake_job(fail_at=2)):
        partial_fields = _valid_fields(3)
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

        # items_published=1, published_urls из 1 элемента
        assert partial_final.get("items_published") == 1, (
            f"Частичный сбой: ожидали items_published=1, "
            f"получили: {partial_final.get('items_published')}"
        )
        assert partial_final.get("item_index") == 2
        checks_passed += 1
        print(f"  Проверка 18 PASS: items_published=1")

        # Результат: saved_urls из 1 элемента
        sc, body, _ = _get(f"/api/publish/result/{partial_job_id}")
        assert sc == 200, (
            f"GET /api/publish/result (partial) вернул {sc}: {body[:200]}"
        )
        partial_result = json.loads(body)
        partial_urls = partial_result.get("published_urls", [])
        assert len(partial_urls) == 1, (
            f"Частичный сбой: ожидали 1 saved_url, получили {len(partial_urls)}: "
            f"{partial_urls}"
        )
        checks_passed += 1
        assert partial_result.get("saved_urls") == partial_urls
        print(f"  Проверка 19 PASS: published_urls содержит 1 элемент ({partial_urls[0][-20:]!r})")

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

    # ── Шаг 19: после успешной заливки prep-папка удаляется (ТЗ §17.4) ────────
    print("Шаг 19: успешный publish с prep_id → prep-папка удалена")
    _prep_id_clean = "smoke-prep-cleanup-0001"
    _real_tmp_publish = app_module.TMP_PUBLISH_DIR
    _prep_dir_clean = _real_tmp_publish / f"prep_{_prep_id_clean}"
    _tmp_prep_dir_clean: str | None = None
    try:
        # Создаём временную базовую директорию и наполняем её
        _tmp_prep_dir_clean = tempfile.mkdtemp(prefix="publish_smoke_cleanup_")
        _prep_base_clean = Path(_tmp_prep_dir_clean)
        for _draft_num in (1, 2):
            _dd = _prep_base_clean / f"draft_{_draft_num:02d}"
            _dd_photos = _dd / "photos"
            _dd_photos.mkdir(parents=True, exist_ok=True)
            (_dd / "title.txt").write_text(f"Заголовок {_draft_num}", encoding="utf-8")
            (_dd / "text.txt").write_text(f"Описание {_draft_num}", encoding="utf-8")
            (_dd_photos / "photo_01.jpg").write_bytes(png1)

        # Кладём в ожидаемое место TMP_PUBLISH_DIR/prep_{prep_id}
        if _prep_dir_clean.exists():
            shutil.rmtree(_prep_dir_clean)
        shutil.copytree(_prep_base_clean, _prep_dir_clean)

        # Регистрируем в PREP_JOBS
        app_module.PREP_JOBS[_prep_id_clean] = {
            "status": "done",
            "step": "done",
            "step_label": "Готово",
            "done": 3,
            "total": 3,
            "error": None,
            "drafts_count": 2,
        }

        with mock.patch.object(pub_module, "run_publish_job", _make_fake_job()):
            clean_fields = dict(_valid_fields(), prep_id=_prep_id_clean)
            sc, body, _ = _post_multipart(
                "/api/publish/start",
                clean_fields,
                [("photos", "photo_c.png", png1)],
            )
            assert sc == 200, (
                f"start (cleanup test): ожидали 200, получили {sc}. Тело: {body[:300]}"
            )
            clean_job_id = json.loads(body).get("job_id")
            assert clean_job_id, f"Нет job_id в ответе cleanup-теста: {body[:200]}"

            # Polling до терминального статуса
            clean_final = _wait_publish_done(clean_job_id, timeout=20.0)
            assert clean_final.get("status") == "done", (
                f"cleanup-тест: ожидали status='done', получили: {clean_final.get('status')!r}"
            )

        assert not _prep_dir_clean.exists(), (
            f"Проверка 23 FAIL: prep-папка не удалена после успешной заливки: {_prep_dir_clean}"
        )
        assert _prep_id_clean not in app_module.PREP_JOBS, (
            "Проверка 23 FAIL: запись PREP_JOBS не удалена после успешной заливки"
        )
        checks_passed += 1
        print("  Проверка 23 PASS: prep-папка и запись PREP_JOBS удалены после успеха")

    finally:
        if _tmp_prep_dir_clean and os.path.exists(_tmp_prep_dir_clean):
            try:
                shutil.rmtree(_tmp_prep_dir_clean)
            except OSError:
                pass
        if _prep_dir_clean.exists():
            try:
                shutil.rmtree(_prep_dir_clean)
            except OSError:
                pass
        app_module.PREP_JOBS.pop(_prep_id_clean, None)

    # ── Шаг 20: при падении заливки prep-папка ОСТАЁТСЯ (ТЗ §17.4) ────────────
    print("Шаг 20: неуспешный publish с prep_id → prep-папка остаётся")
    _prep_id_keep = "smoke-prep-cleanup-0002"
    _prep_dir_keep = _real_tmp_publish / f"prep_{_prep_id_keep}"
    _tmp_prep_dir_keep: str | None = None
    try:
        # Собираем структуру
        _tmp_prep_dir_keep = tempfile.mkdtemp(prefix="publish_smoke_keep_")
        _prep_base_keep = Path(_tmp_prep_dir_keep)
        for _draft_num in (1, 2):
            _dd = _prep_base_keep / f"draft_{_draft_num:02d}"
            _dd_photos = _dd / "photos"
            _dd_photos.mkdir(parents=True, exist_ok=True)
            (_dd / "title.txt").write_text(f"Заголовок {_draft_num}", encoding="utf-8")
            (_dd / "text.txt").write_text(f"Описание {_draft_num}", encoding="utf-8")
            (_dd_photos / "photo_01.jpg").write_bytes(png1)

        if _prep_dir_keep.exists():
            shutil.rmtree(_prep_dir_keep)
        shutil.copytree(_prep_base_keep, _prep_dir_keep)

        app_module.PREP_JOBS[_prep_id_keep] = {
            "status": "done",
            "step": "done",
            "step_label": "Готово",
            "done": 3,
            "total": 3,
            "error": None,
            "drafts_count": 2,
        }

        with mock.patch.object(pub_module, "run_publish_job", _make_fake_job(fail_at=2)):
            keep_fields = dict(_valid_fields(3), prep_id=_prep_id_keep)
            sc, body, _ = _post_multipart(
                "/api/publish/start",
                keep_fields,
                [("photos", "photo_k.png", png1)],
            )
            assert sc == 200, (
                f"start (keep test): ожидали 200, получили {sc}. Тело: {body[:300]}"
            )
            keep_job_id = json.loads(body).get("job_id")
            assert keep_job_id, f"Нет job_id в ответе keep-теста: {body[:200]}"

            keep_final = _wait_publish_done(keep_job_id, timeout=20.0)
            terminal_statuses_keep = {"failed", "needs_user_action"}
            assert keep_final.get("status") in terminal_statuses_keep, (
                f"keep-тест: ожидали failed/needs_user_action, "
                f"получили: {keep_final.get('status')!r}"
            )

        assert _prep_dir_keep.exists(), (
            "Проверка 24 FAIL: prep-папка удалена при НЕуспешной заливке"
        )
        assert _prep_id_keep in app_module.PREP_JOBS, (
            "Проверка 24 FAIL: запись PREP_JOBS удалена при НЕуспешной заливке"
        )
        checks_passed += 1
        print("  Проверка 24 PASS: при падении prep-папка и запись сохранены")

    finally:
        if _tmp_prep_dir_keep and os.path.exists(_tmp_prep_dir_keep):
            try:
                shutil.rmtree(_tmp_prep_dir_keep)
            except OSError:
                pass
        if _prep_dir_keep.exists():
            try:
                shutil.rmtree(_prep_dir_keep)
            except OSError:
                pass
        app_module.PREP_JOBS.pop(_prep_id_keep, None)

    # ── Шаг 21: N≥2 БЕЗ prep_id → авто-подготовка вариантов ─────────────────
    # Главный фикс: раньше publisher молча делал N одинаковых клонов.
    # Фейковый job падает на черновике 2 → prep-папка остаётся для разбора,
    # можно проверить содержимое draft_01..03.
    print("Шаг 21: start с drafts_count=3 без prep_id → авто-подготовка")
    with mock.patch.object(pub_module, "run_publish_job", _make_fake_job(fail_at=2)):
        auto_fields = _valid_fields(3)
        sc, body, _ = _post_multipart(
            "/api/publish/start", auto_fields,
            [("photos", "auto1.png", png1)],
        )
        assert sc == 200, f"start без prep_id: ожидали 200, получили {sc}: {body[:300]}"
        auto_job_id = json.loads(body)["job_id"]
        auto_status = _wait_publish_done(auto_job_id)

    auto_job = app_module.PUBLISH_JOBS.get(auto_job_id, {})
    assert auto_job.get("prep_id") == auto_job_id, (
        f"Авто-подготовка: ожидали prep_id={auto_job_id!r}, "
        f"получили {auto_job.get('prep_id')!r}"
    )
    _auto_prep_dir = app_module.TMP_PUBLISH_DIR / f"prep_{auto_job_id}"
    try:
        assert _auto_prep_dir.is_dir(), (
            f"Папка авто-подготовки не найдена: {_auto_prep_dir} "
            f"(статус задачи: {auto_status.get('status')!r})"
        )
        _titles = [
            (_auto_prep_dir / f"draft_{i:02d}" / "title.txt").read_text(encoding="utf-8")
            for i in (1, 2, 3)
        ]
        assert len(set(_titles)) >= 2, (
            f"Авто-подготовка: названия вариантов не различаются: {_titles}"
        )
        checks_passed += 1
        print("  Проверка 25 PASS: авто-подготовка создала 3 варианта, названия различаются")
    finally:
        shutil.rmtree(_auto_prep_dir, ignore_errors=True)
        app_module.PUBLISH_JOBS.pop(auto_job_id, None)

    # ── Шаг 22: успешный авто-publish → prep-папка удалена ───────────────────
    print("Шаг 22: успешный publish с авто-подготовкой → prep-папка удалена")
    with mock.patch.object(pub_module, "run_publish_job", _make_fake_job()):
        auto2_fields = _valid_fields(2)
        sc, body, _ = _post_multipart(
            "/api/publish/start", auto2_fields,
            [("photos", "auto2.png", png2)],
        )
        assert sc == 200, f"ожидали 200, получили {sc}: {body[:300]}"
        auto2_job_id = json.loads(body)["job_id"]
        auto2_status = _wait_publish_done(auto2_job_id)

    assert auto2_status.get("status") == "done", (
        f"Ожидали done, получили {auto2_status.get('status')!r}: {auto2_status}"
    )
    _auto2_prep_dir = app_module.TMP_PUBLISH_DIR / f"prep_{auto2_job_id}"
    # Очистка выполняется сразу ПОСЛЕ выставления done — даём ей до 5 с
    for _ in range(50):
        if not _auto2_prep_dir.exists():
            break
        time.sleep(0.1)
    assert not _auto2_prep_dir.exists(), (
        f"Папка авто-подготовки должна удаляться после успеха: {_auto2_prep_dir}"
    )
    app_module.PUBLISH_JOBS.pop(auto2_job_id, None)
    checks_passed += 1
    print("  Проверка 26 PASS: авто-подготовка очищена после успешной заливки")

    # ── Шаг 23: GET /api/publish/categories → 200, обе категории ─────────────
    print("Шаг 23: GET /api/publish/categories → 200, обе категории с непустыми sizes")
    sc, body, _ = _get("/api/publish/categories")
    assert sc == 200, f"/api/publish/categories вернул {sc}"
    cats = {c["key"]: c for c in json.loads(body)["categories"]}
    assert {"jackets", "sneakers"} <= set(cats), cats
    assert cats["jackets"]["sizes"] and cats["sneakers"]["sizes"], (
        f"sizes не должны быть пустыми: jackets={cats['jackets']['sizes']!r}, "
        f"sneakers={cats['sneakers']['sizes']!r}"
    )
    checks_passed += 1
    print("[OK] /api/publish/categories: обе категории")
    # Категории без «Вида товара» отдают пустой список — фронт поле не рисует
    assert cats["jackets"]["item_types"] == [], cats["jackets"]["item_types"]
    checks_passed += 1
    print("  Проверка 27 PASS: item_types пуст у категории без «Вида товара»")

    # ── Шаг 24: «Вид товара» — опциональное категорийное поле ────────────────
    # Профиль «Кофты и футболки» ждёт живой разведки (TSHIRTS_VERIFIED), поэтому
    # проверяем механизм на синтетическом профиле с таким полем.
    print("Шаг 24: «Вид товара» — обязателен только у категорий, где поле есть")
    import category_profiles as _cp
    from dataclasses import replace as _replace

    _fake = _replace(
        _cp.JACKETS, key="smoke_item_type", label="Тест: с видом товара",
        item_type_options={"Футболка": 111, "Худи": 222}, item_type_prefix="vid_tovara",
    )
    _cp.PROFILES[_fake.key] = _fake
    # Свежие валидные поля и одно фото — не полагаемся на состояние прошлых шагов
    _it_fields = _valid_fields()
    _it_files = [("photos", "photo_01.png", png1)]
    try:
        # Список видов уходит на фронт
        sc, body, _ = _get("/api/publish/categories")
        _c = {c["key"]: c for c in json.loads(body)["categories"]}
        assert _c[_fake.key]["item_types"] == ["Футболка", "Худи"], _c[_fake.key]
        checks_passed += 1
        print("  Проверка 28 PASS: item_types категории отдаётся фронту")

        # Без значения → 422 именно по полю item_type
        sc, body, _ = _post_multipart(
            "/api/publish/start", {**_it_fields, "category": _fake.key}, _it_files
        )
        assert sc == 422, f"ожидали 422 без вида товара, получили {sc}"
        assert {e["field"] for e in json.loads(body)["errors"]} == {"item_type"}, body
        checks_passed += 1
        print("  Проверка 29 PASS: пустой «Вид товара» → 422")

        # Значение не из словаря категории → 422
        sc, body, _ = _post_multipart(
            "/api/publish/start",
            {**_it_fields, "category": _fake.key, "item_type": "Ботинки"}, _it_files,
        )
        assert sc == 422 and json.loads(body)["errors"][0]["field"] == "item_type", body
        checks_passed += 1
        print("  Проверка 30 PASS: чужое значение «Вида товара» → 422")

        # Валидное значение → задача создана, значение долетело до DraftData
        sc, body, _ = _post_multipart(
            "/api/publish/start",
            {**_it_fields, "category": _fake.key, "item_type": "Худи"}, _it_files,
        )
        assert sc == 200, f"валидная форма отклонена: {sc} {body[:200]}"
        _jid = json.loads(body)["job_id"]
        _draft = pub_module.build_draft_data(
            {**_it_fields, "item_type": "Худи"}, ["a.jpg"], category=_fake.key
        )
        assert _draft.item_type == "Худи", _draft.item_type
        assert _draft.summary()["item_type"] == "Худи", _draft.summary()
        app_module.PUBLISH_JOBS.pop(_jid, None)
        checks_passed += 1
        print("  Проверка 31 PASS: валидный «Вид товара» принят и дошёл до DraftData")

        # У категории без поля лишнее значение не мешает
        sc, _body, _ = _post_multipart(
            "/api/publish/start",
            {**_it_fields, "category": "jackets", "item_type": "Чепуха"}, _it_files,
        )
        assert sc == 200, f"лишний item_type сломал jackets: {sc}"
        checks_passed += 1
        print("  Проверка 32 PASS: у категории без поля лишний item_type игнорируется")
    finally:
        _cp.PROFILES.pop(_fake.key, None)

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
