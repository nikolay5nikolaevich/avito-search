"""
Smoke-тест веб-слоя фазы подготовки вариантов черновиков (ТЗ §17, Задача 8).

Тестирует эндпоинты:
    POST /api/publish/prepare        → prep_id
    GET  /api/publish/prepare/status/{prep_id} → polling до done
    GET  /api/publish/prepare/result/{prep_id} → список карточек
    GET  /api/publish/prepare/photo/{prep_id}/{draft_index}/{photo_index}
    POST /api/publish/prepare/regenerate → обновлённая карточка
    POST /api/publish/prepare/update-text → ручное сохранение названия/описания

Позитивный сценарий:
    - 2 синтетических PNG (Pillow из памяти)
    - 3 черновика (drafts_count=3)
    - каждый черновик, включая №1, отличается от исходника
    - названия и описания всех черновиков различаются
    - фото доступны по /photo/ URL, Content-Type image/*
    - regenerate черновика №2 → описание и/или байты фото изменились
    - название и описание любого варианта можно вручную изменить без изменения фото

Негативные кейсы:
    - пустое описание → 422 (поле description)
    - drafts_count=21  → 422 (поле drafts_count)
    - 0 фото           → 422 (поле photos)
    - regenerate draft_index=1 → обновлённая карточка
    - status неизвестного prep_id → 404

Запуск:
    $env:PYTHONIOENCODING='utf-8'
    .venv\\Scripts\\python.exe tests\\prepare_smoke_test.py
"""

import io
import json
import logging
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

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
logger = logging.getLogger("prepare_smoke_test")

TEST_PORT = 8003
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"

# prep_id, созданные за прогон, — для уборки tmp/publish/prep_{id}/ в finally
CREATED_PREP_IDS: list[str] = []


# ---------------------------------------------------------------------------
# Генерация синтетических PNG через Pillow (из памяти, без файлов на диске)
# ---------------------------------------------------------------------------

def _make_png_bytes(width: int = 80, height: int = 60, color: tuple = (100, 150, 200)) -> bytes:
    """Создаёт синтетический PNG в памяти через Pillow."""
    from PIL import Image  # noqa: PLC0415
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# HTTP-хелперы
# ---------------------------------------------------------------------------

def _get(path: str) -> tuple[int, str, dict]:
    """GET к тестовому серверу."""
    return http_get(BASE_URL, path)


def _post_multipart(
    path: str,
    fields: dict[str, str],
    files: list[tuple[str, str, bytes]],
) -> tuple[int, str, dict]:
    """
    POST multipart/form-data (без внешних зависимостей).
    files: список (field_name, filename, content_bytes).
    """
    boundary = "----PrepareSmokeTestBoundary4c7d1e9f"
    body_parts: list[bytes] = []

    for name, value in fields.items():
        part = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")
        body_parts.append(part)

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


def _post_json(path: str, payload: dict) -> tuple[int, str, dict]:
    """POST с телом JSON."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = BASE_URL + path
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), dict(e.headers)


def _get_raw(path: str) -> tuple[int, bytes, dict]:
    """GET → сырые байты (для проверки фото)."""
    url = BASE_URL + path
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _wait_prep_done(prep_id: str, timeout: float = 60.0) -> dict:
    """
    Опрашивает /api/publish/prepare/status/{prep_id} до терминального статуса.
    Терминальные: done, failed. Таймаут защищает от вечного цикла.
    """
    terminal = {"done", "failed"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sc, body, _ = _get(f"/api/publish/prepare/status/{prep_id}")
        assert sc == 200, (
            f"/api/publish/prepare/status/{prep_id} вернул {sc}, ожидали 200"
        )
        data = json.loads(body)
        if data.get("status") in terminal:
            return data
        time.sleep(0.2)
    raise AssertionError(
        f"Таймаут {timeout}с: задача подготовки {prep_id} не завершилась"
    )


# ---------------------------------------------------------------------------
# Основной тест
# ---------------------------------------------------------------------------

def run_prepare_smoke_test() -> None:
    """Сквозной smoke-тест веб-слоя фазы подготовки вариантов."""
    print("=== Prepare Smoke-тест начат ===")
    checks_passed = 0

    # Генерируем два синтетических PNG через Pillow
    png1 = _make_png_bytes(color=(100, 150, 200))
    png2 = _make_png_bytes(color=(200, 100, 50))
    assert len(png1) > 0, "Генерация PNG (png1) провалилась"
    assert len(png2) > 0, "Генерация PNG (png2) провалилась"

    import app as app_module  # noqa: PLC0415

    start_server(app_module.app, TEST_PORT)
    wait_server_ready(BASE_URL, timeout=15.0)

    # ── Проверка 1: POST /api/publish/prepare → 200 + prep_id ────────────────
    print("Шаг 1: POST /api/publish/prepare → prep_id")
    orig_title = "Пиджак Hugo Boss классический"
    orig_description = (
        "Отличный пиджак в хорошем состоянии. "
        "Произведён из качественных материалов. "
        "Подойдёт для деловых встреч и торжественных мероприятий."
    )
    fields = {
        "title": orig_title,
        "description": orig_description,
        "drafts_count": "3",
    }
    files = [
        ("photos", "photo_01.png", png1),
        ("photos", "photo_02.png", png2),
    ]
    sc, body, _ = _post_multipart("/api/publish/prepare", fields, files)
    assert sc == 200, (
        f"POST /api/publish/prepare вернул {sc}, ожидали 200. Тело: {body[:300]}"
    )
    data = json.loads(body)
    prep_id = data.get("prep_id")
    assert prep_id, f"Ответ /api/publish/prepare не содержит prep_id: {data}"
    CREATED_PREP_IDS.append(prep_id)
    checks_passed += 1
    print(f"  Проверка 1 PASS: prep_id получен ({prep_id[:8]}…)")

    # ── Проверка 2: polling /api/publish/prepare/status до done ──────────────
    print(f"Шаг 2: polling /api/publish/prepare/status/{prep_id[:8]}… → done")
    final_status = _wait_prep_done(prep_id, timeout=60.0)
    assert final_status.get("status") == "done", (
        f"Ожидали status='done', получили: {final_status}"
    )
    checks_passed += 1
    print(f"  Проверка 2 PASS: status=done")

    # ── Проверка 3: GET /api/publish/prepare/result → 3 черновика ────────────
    print(f"Шаг 3: GET /api/publish/prepare/result/{prep_id[:8]}…")
    sc, body, _ = _get(f"/api/publish/prepare/result/{prep_id}")
    assert sc == 200, (
        f"GET /api/publish/prepare/result вернул {sc}: {body[:300]}"
    )
    result = json.loads(body)
    drafts = result.get("drafts")
    assert isinstance(drafts, list), (
        f"drafts должен быть списком, получили: {type(drafts)}"
    )
    assert len(drafts) == 3, (
        f"Ожидали 3 черновика, получили {len(drafts)}"
    )
    checks_passed += 1
    print(f"  Проверка 3 PASS: drafts содержит 3 элемента")

    # ── Проверка 4: каждый черновик имеет обязательные ключи и 2 photo_urls ──
    print("Шаг 4: структура карточек черновиков")
    required_keys = {"title", "description", "preset_name", "notes", "photo_urls", "warnings"}
    for i, draft in enumerate(drafts, start=1):
        missing = required_keys - set(draft.keys())
        assert not missing, (
            f"Черновик {i}: отсутствуют ключи {missing}. Карточка: {draft}"
        )
        assert len(draft["photo_urls"]) == 2, (
            f"Черновик {i}: ожидали 2 photo_urls, получили {len(draft['photo_urls'])}"
        )
    checks_passed += 1
    print(f"  Проверка 4 PASS: структура карточек корректна (ключи + 2 photo_urls у каждого)")

    # ── Проверка 5: черновик №1 — такой же изменённый вариант ────────────────
    print("Шаг 5: черновик №1 отличается от исходника")
    draft1 = drafts[0]
    assert draft1["title"] != orig_title, (
        f"Черновик 1 сохранил исходное название: {draft1['title']!r}"
    )
    assert draft1["description"] != orig_description, (
        f"Черновик 1 сохранил исходное описание: {draft1['description']!r}"
    )
    assert "артикул" in draft1.get("notes", ""), draft1
    checks_passed += 1
    print(f"  Проверка 5 PASS: черновик №1 изменён (title={draft1['title']!r})")

    # ── Проверка 6: все названия и описания попарно различны ─────────────────
    print("Шаг 6: все тексты — отдельные варианты")
    desc1 = drafts[0]["description"]
    desc2 = drafts[1]["description"]
    desc3 = drafts[2]["description"]
    assert len({desc1, desc2, desc3}) == 3, [desc1, desc2, desc3]
    assert len({draft["title"] for draft in drafts}) == 3, drafts
    checks_passed += 1
    print("  Проверка 6 PASS: названия и описания попарно различны")

    # ── Проверка 7: GET фото черновика 1, фото 1 → 200, Content-Type image/* ─
    print("Шаг 7: GET фото черновика 1, фото 1")
    photo_url = drafts[0]["photo_urls"][0]  # например: /api/publish/prepare/photo/{id}/1/1
    sc_photo, photo_bytes, photo_headers = _get_raw(photo_url)
    assert sc_photo == 200, (
        f"GET {photo_url} вернул {sc_photo}, ожидали 200"
    )
    ct = photo_headers.get("content-type", photo_headers.get("Content-Type", ""))
    assert ct.lower().startswith("image/"), (
        f"Content-Type фото должен начинаться с 'image/', получили: {ct!r}"
    )
    assert len(photo_bytes) > 0, "Тело фото пустое"
    assert photo_bytes != png1, (
        "Фото черновика №1 осталось точной копией исходника — вариация не применилась "
        f"(исходник {len(png1)} байт, получено {len(photo_bytes)} байт)"
    )
    assert "оригинал" not in draft1.get("preset_name", "").lower(), draft1
    checks_passed += 1
    print(f"  Проверка 7 PASS: GET фото → 200, Content-Type={ct!r}, {len(photo_bytes)} байт, №1 изменён")

    # ── Проверка 8: POST regenerate (draft 2) → описание И/ИЛИ байты фото изменились ──
    print("Шаг 8: POST regenerate (черновик 2)")
    # Запоминаем текущее описание и байты первого фото черновика 2
    desc2_before = drafts[1]["description"]
    photo2_url = drafts[1]["photo_urls"][0]
    _, photo2_bytes_before, _ = _get_raw(photo2_url)

    sc_regen, body_regen, _ = _post_json(
        "/api/publish/prepare/regenerate",
        {"prep_id": prep_id, "draft_index": 2},
    )
    assert sc_regen == 200, (
        f"POST regenerate вернул {sc_regen}, ожидали 200. Тело: {body_regen[:300]}"
    )
    card2 = json.loads(body_regen)
    assert card2.get("index") == 2, (
        f"Перегенерированная карточка: ожидали index=2, получили: {card2.get('index')}"
    )

    # Перечитываем фото черновика 2 после перегенерации
    _, photo2_bytes_after, _ = _get_raw(photo2_url)

    desc2_after = card2.get("description", "")
    desc_changed = desc2_after != desc2_before
    photo_changed = photo2_bytes_after != photo2_bytes_before
    assert desc_changed or photo_changed, (
        f"После regenerate ни описание, ни байты фото черновика 2 не изменились.\n"
        f"  desc_before={desc2_before!r}\n"
        f"  desc_after ={desc2_after!r}\n"
        f"  photo_bytes_before={len(photo2_bytes_before)}, photo_bytes_after={len(photo2_bytes_after)}"
    )
    checks_passed += 1
    print(
        f"  Проверка 8 PASS: regenerate черновика 2 — "
        f"desc_changed={desc_changed}, photo_changed={photo_changed}"
    )

    # ── Проверка 8а: ручная правка текста сохраняется на диск ────────────────
    print("Шаг 8а: ручное редактирование текста варианта 1")
    manual_title = "  Пиджак Hugo Boss — проверено вручную  "
    manual_description = "  Ручное описание без изменения фотографий.  "
    original_photo_urls = list(draft1["photo_urls"])
    sc_edit, body_edit, _ = _post_json(
        "/api/publish/prepare/update-text",
        {
            "prep_id": prep_id,
            "draft_index": 1,
            "title": manual_title,
            "description": manual_description,
        },
    )
    assert sc_edit == 200, (
        f"POST update-text вернул {sc_edit}, ожидали 200. Тело: {body_edit[:300]}"
    )
    edited_card = json.loads(body_edit)
    assert edited_card["title"] == manual_title.strip(), edited_card
    assert edited_card["description"] == manual_description.strip(), edited_card
    assert edited_card["photo_urls"] == original_photo_urls, (
        "Ручная правка текста не должна менять фотографии"
    )

    sc_result, body_result, _ = _get(f"/api/publish/prepare/result/{prep_id}")
    assert sc_result == 200, body_result[:300]
    persisted_card = json.loads(body_result)["drafts"][0]
    assert persisted_card["title"] == manual_title.strip(), persisted_card
    assert persisted_card["description"] == manual_description.strip(), persisted_card
    checks_passed += 1
    print("  Проверка 8а PASS: текст сохранён, фото не изменились")

    # ── Проверка 8аа: повтор названия другого варианта отклоняется ─────────
    print("Шаг 8аа: повтор названия другого варианта → 422")
    sc_duplicate, body_duplicate, _ = _post_json(
        "/api/publish/prepare/update-text",
        {
            "prep_id": prep_id,
            "draft_index": 2,
            "title": "ПИДЖАК HUGO BOSS, ПРОВЕРЕНО ВРУЧНУЮ!",
            "description": "Другое описание",
        },
    )
    assert sc_duplicate == 422, body_duplicate[:300]
    duplicate_payload = json.loads(body_duplicate)
    assert duplicate_payload.get("error"), duplicate_payload
    assert {
        error["field"] for error in duplicate_payload.get("errors", [])
    } == {"title"}, duplicate_payload
    checks_passed += 1
    print("  Проверка 8аа PASS: пунктуация и регистр не маскируют повтор")

    # ── Проверка 8б: пустой текст отклоняется и не затирает вариант ─────────
    print("Шаг 8б: пустое название при ручном редактировании → 422")
    sc_edit_bad, body_edit_bad, _ = _post_json(
        "/api/publish/prepare/update-text",
        {
            "prep_id": prep_id,
            "draft_index": 1,
            "title": "   ",
            "description": "Непустое описание",
        },
    )
    assert sc_edit_bad == 422, (
        f"Пустое название update-text: ожидали 422, получили {sc_edit_bad}: "
        f"{body_edit_bad[:300]}"
    )
    edit_errors = json.loads(body_edit_bad).get("errors", [])
    assert {error["field"] for error in edit_errors} == {"title"}, edit_errors

    _, body_after_bad, _ = _get(f"/api/publish/prepare/result/{prep_id}")
    after_bad_card = json.loads(body_after_bad)["drafts"][0]
    assert after_bad_card["title"] == manual_title.strip(), after_bad_card
    checks_passed += 1
    print("  Проверка 8б PASS: пустой текст отклонён, сохранённый вариант не затёрт")

    # ── Негативные кейсы ─────────────────────────────────────────────────────
    print("Шаг 9: негативный кейс — пустое описание → 422")
    sc_neg, body_neg, _ = _post_multipart(
        "/api/publish/prepare",
        {"title": "Название", "description": "", "drafts_count": "3"},
        [("photos", "photo_01.png", png1)],
    )
    assert sc_neg == 422, (
        f"Пустое описание: ожидали 422, получили {sc_neg}. Тело: {body_neg[:200]}"
    )
    resp_data = json.loads(body_neg)
    error_fields = {e["field"] for e in resp_data.get("errors", [])}
    assert "description" in error_fields, (
        f"В 422-ответе нет ошибки поля 'description': {resp_data}"
    )
    checks_passed += 1
    print(f"  Проверка 9 PASS: пустое описание → 422 (поля={error_fields})")

    print("Шаг 10: негативный кейс — drafts_count=21 → 422")
    sc_neg, body_neg, _ = _post_multipart(
        "/api/publish/prepare",
        {"title": "Название", "description": "Описание", "drafts_count": "21"},
        [("photos", "photo_01.png", png1)],
    )
    assert sc_neg == 422, (
        f"drafts_count=21: ожидали 422, получили {sc_neg}. Тело: {body_neg[:200]}"
    )
    resp_data = json.loads(body_neg)
    error_fields = {e["field"] for e in resp_data.get("errors", [])}
    assert "drafts_count" in error_fields, (
        f"В 422-ответе нет ошибки поля 'drafts_count': {resp_data}"
    )
    checks_passed += 1
    print(f"  Проверка 10 PASS: drafts_count=21 → 422 (поля={error_fields})")

    print("Шаг 11: негативный кейс — 0 фото → 422")
    sc_neg, body_neg, _ = _post_multipart(
        "/api/publish/prepare",
        {"title": "Название", "description": "Описание", "drafts_count": "3"},
        [],  # без фото
    )
    assert sc_neg == 422, (
        f"0 фото: ожидали 422, получили {sc_neg}. Тело: {body_neg[:200]}"
    )
    resp_data = json.loads(body_neg)
    error_fields = {e["field"] for e in resp_data.get("errors", [])}
    assert "photos" in error_fields, (
        f"В 422-ответе нет ошибки поля 'photos': {resp_data}"
    )
    checks_passed += 1
    print(f"  Проверка 11 PASS: 0 фото → 422 (поля={error_fields})")

    print("Шаг 12: regenerate draft_index=1 → 200")
    sc_regen1, body_regen1, _ = _post_json(
        "/api/publish/prepare/regenerate",
        {"prep_id": prep_id, "draft_index": 1},
    )
    assert sc_regen1 == 200, (
        f"regenerate(draft_index=1): ожидали 200, получили {sc_regen1}. Тело: {body_regen1[:200]}"
    )
    regenerated1 = json.loads(body_regen1)
    assert regenerated1.get("index") == 1, regenerated1
    assert regenerated1.get("title") != manual_title.strip(), regenerated1
    assert "оригинал" not in regenerated1.get("preset_name", "").lower(), regenerated1
    checks_passed += 1
    print("  Проверка 12 PASS: regenerate(draft_index=1) → обновлённый вариант")

    print("Шаг 13: негативный кейс — status неизвестного prep_id → 404")
    sc_neg, body_neg, _ = _get("/api/publish/prepare/status/unknown-prep-id-12345")
    assert sc_neg == 404, (
        f"Неизвестный prep_id: ожидали 404, получили {sc_neg}. Тело: {body_neg[:200]}"
    )
    checks_passed += 1
    print(f"  Проверка 13 PASS: неизвестный prep_id → 404")

    print(f"\n=== PREPARE SMOKE TEST: OK: {checks_passed} проверок ===")


# ---------------------------------------------------------------------------
# Уборка артефактов
# ---------------------------------------------------------------------------

def _cleanup_prep_dirs() -> None:
    """Подчищает tmp/publish/prep_{id}/ за прогон (best effort, как в publish_smoke_test)."""
    try:
        import app as app_module  # noqa: PLC0415
    except Exception:
        return
    for _pid in CREATED_PREP_IDS:
        _prep_path = app_module.TMP_PUBLISH_DIR / f"prep_{_pid}"
        if _prep_path.exists():
            try:
                shutil.rmtree(_prep_path)
            except OSError:
                pass
        # Убираем из PREP_JOBS (сервер in-process, реестр доступен напрямую)
        app_module.PREP_JOBS.pop(_pid, None)


# ---------------------------------------------------------------------------
# Самозапуск
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    exit_code = 0
    try:
        run_prepare_smoke_test()
    except AssertionError as e:
        print(f"\n[FAIL] {e}", file=sys.stderr)
        exit_code = 1
    except Exception as e:
        import traceback
        print(f"\n[ERROR] {e}", file=sys.stderr)
        traceback.print_exc()
        exit_code = 2
    finally:
        _cleanup_prep_dirs()
    sys.exit(exit_code)
