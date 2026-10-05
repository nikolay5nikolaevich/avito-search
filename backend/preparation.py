"""
Оркестратор фазы подготовки вариантов черновиков (ТЗ §17, Задача 4).

Фаза 1 («подготовка»): фоновая задача генерирует N вариантов текста и фото,
раскладывает их на диск в tmp/publish/prep_{prep_id}/. Позже (Задачи 5–6)
app.py навешивает HTTP-эндпоинты, publisher.py читает варианты при заливке.

Сетевых вызовов НЕТ. Зависимости: text_variation, photo_variation, publisher
(только константы / хелперы — НЕ app.py).

Хранилище:
    tmp/publish/prep_{prep_id}/
        source/photos/photo_01.ext    # копии исходников, расширение исходное
        source/title.txt              # исходное название
        source/text.txt               # исходное описание
        source/facts.json             # словарь facts
        source/seed.json              # {"seed": int, "drafts_count": int} —
                                       # пишется ДО генерации черновиков, чтобы
                                       # после рестарта сервера recover_prep_job
                                       # мог детерминированно дописать
                                       # недостающие draft_NN тем же сидом (F38)
        draft_01/{photos/, title.txt, text.txt, meta.json}
        draft_02/...

meta.json — {"preset": <имя пресета>, "seed": <int>, "notes": <строка>}

Самотесты: python backend/preparation.py
"""

import asyncio
import json
import logging
import random
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from photo_variation import (
    apply_preset,
    build_modified_presets,
    heif_available,
    output_ext_for,
)
from publisher import (
    ALLOWED_PHOTO_EXTENSIONS,
    ALLOWED_PHOTO_MIME,
    DRAFTS_MAX,
    DRAFTS_MIN,
    MAX_PHOTO_SIZE_BYTES,
    MAX_PHOTOS,
    MIN_PHOTOS,
    parse_drafts_count,
)
from text_variation import normalize_title_key, vary_listing

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Шаги фазы подготовки (для job["step"] / job["step_label"])
# ---------------------------------------------------------------------------

PREP_STEPS: list[tuple[str, str]] = [
    ("vary_texts", "Генерация текстов"),
    ("vary_photos", "Обработка фото"),
    ("done", "Готово"),
]

_PREP_STEP_LABELS: dict[str, str] = dict(PREP_STEPS)
_PREP_STEP_INDEX: dict[str, int] = {
    name: i + 1 for i, (name, _) in enumerate(PREP_STEPS)
}
PREP_TOTAL_STEPS: int = len(PREP_STEPS)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _set_prep_step(job: dict, step_name: str) -> None:
    """Переводит задачу на шаг step_name (обновляет step/step_label/done)."""
    job["step"] = step_name
    job["step_label"] = _PREP_STEP_LABELS.get(step_name, step_name)
    job["done"] = _PREP_STEP_INDEX.get(step_name, 0)
    logger.info(
        "Подготовка, шаг %d/%d: %s",
        job["done"],
        PREP_TOTAL_STEPS,
        step_name,
    )


def _draft_dir(base_dir: Path, index: int) -> Path:
    """Путь к каталогу черновика draft_NN (index начинается с 1)."""
    return base_dir / f"draft_{index:02d}"


def _source_dir(base_dir: Path) -> Path:
    return base_dir / "source"


def _count_draft_dirs(base_dir: Path) -> int:
    """Число папок draft_NN в base_dir."""
    return sum(
        1
        for p in base_dir.iterdir()
        if p.is_dir() and p.name.startswith("draft_")
    )


def _seed_info_path(base_dir: Path) -> Path:
    """Путь к файлу с сидом вариаций (source/seed.json)."""
    return _source_dir(base_dir) / "seed.json"


def _write_seed_info(base_dir: Path, *, seed: int, drafts_count: int) -> None:
    """Сохраняет сид и заявленное число вариантов ДО генерации черновиков.

    Только это на диске позволяет после рестарта сервера детерминированно
    дописать недостающие draft_NN — vary_listing/build_modified_presets
    полностью определяются (seed, title, description, facts, drafts_count),
    поэтому повторный проход с тем же сидом даёт тот же результат, что и
    непрерывный (F38). Пишем через tmp + replace, как save_publish_state.
    """
    path = _seed_info_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(
            {"seed": seed, "drafts_count": drafts_count},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _read_seed_info(base_dir: Path) -> Optional[dict]:
    """Читает сид, сохранённый _write_seed_info. None — если его нет/битый."""
    path = _seed_info_path(base_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        seed = int(data["seed"])
        drafts_count = int(data["drafts_count"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if drafts_count <= 0:
        return None
    return {"seed": seed, "drafts_count": drafts_count}


def _draft_complete(base_dir: Path, index: int) -> bool:
    """Полностью ли записан черновик index (title/text/meta + непустые photos/)."""
    d_dir = _draft_dir(base_dir, index)
    photos_dir = d_dir / "photos"
    return (
        (d_dir / "title.txt").is_file()
        and (d_dir / "text.txt").is_file()
        and (d_dir / "meta.json").is_file()
        and photos_dir.is_dir()
        and any(photos_dir.iterdir())
    )


def _write_drafts(
    base_dir: Path,
    *,
    prep_id: str,
    title: str,
    description: str,
    drafts_count: int,
    facts: dict,
    seed: int,
    source_photo_paths: list[Path],
) -> None:
    """Генерирует и раскладывает все draft_NN по (title, description, facts,
    drafts_count, seed, source_photo_paths). Детерминирована по этим входам —
    вызывается и из run_prep_job (первый проход), и из восстановления после
    рестарта с тем же сидом (F38): результат в обоих случаях одинаковый.
    """
    text_variants = vary_listing(
        title,
        description,
        drafts_count,
        seed=seed,
        facts=facts,
    )
    presets = build_modified_presets(drafts_count, seed=seed)
    source_photo_bytes = [sp.read_bytes() for sp in source_photo_paths]

    for i in range(drafts_count):
        draft_idx = i + 1  # 1-based
        tv = text_variants[i]
        preset = presets[i]

        d_dir = _draft_dir(base_dir, draft_idx)
        d_photos_dir = d_dir / "photos"
        d_photos_dir.mkdir(parents=True, exist_ok=True)

        (d_dir / "title.txt").write_text(tv.title, encoding="utf-8")
        (d_dir / "text.txt").write_text(tv.description, encoding="utf-8")

        notes = tv.notes or ""
        warnings: list[str] = []
        for j, (raw_bytes, src_photo) in enumerate(
            zip(source_photo_bytes, source_photo_paths), start=1
        ):
            out_bytes, out_ext, warn = _photo_for_draft(
                draft_idx, j, raw_bytes, src_photo.suffix, preset
            )
            if warn:
                warnings.append(warn)
                logger.warning(
                    "Подготовка %s, черновик %d: %s (пресет %r)",
                    prep_id, draft_idx, warn, preset.name,
                )
            (d_photos_dir / f"photo_{j:02d}{out_ext}").write_bytes(out_bytes)

        meta = {
            "preset": preset.name,
            "seed": seed,
            "notes": notes,
            "warnings": warnings,
        }
        (d_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "Подготовка %s: черновик %d/%d сохранён (пресет %r)",
            prep_id,
            draft_idx,
            drafts_count,
            preset.name,
        )


def _photo_for_draft(
    draft_idx: int,
    photo_idx: int,
    raw_bytes: bytes,
    src_suffix: str,
    preset,
) -> tuple[bytes, str, "str | None"]:
    """Готовит байты одного фото для черновика.

    Каждый вариант, включая №1, обрабатывается переданным пресетом.

    Возвращает (байты, расширение_с_точкой, предупреждение|None). Предупреждение
    задаётся, если обработка не изменила байты — значит вариация
    не применилась (apply_preset молча вернул оригинал), и пользователь должен это
    увидеть в превью, а не получить «клон» под видом варианта.
    """
    suffix = (src_suffix or "").lower()

    processed = apply_preset(raw_bytes, preset)
    out_ext = output_ext_for(suffix)
    warning: "str | None" = None
    if processed == raw_bytes:
        warning = f"фото {photo_idx}: вариация не применилась — использован оригинал"
    return processed, out_ext, warning


# ---------------------------------------------------------------------------
# Валидация формы подготовки
# ---------------------------------------------------------------------------


def validate_prepare_form(
    fields: dict,
    photo_meta: list[tuple[str, "str | None", int]],
) -> list[dict]:
    """Валидирует поля формы подготовки вариантов.

    Правила: title (непустое), description (непустое),
    drafts_count (через publisher.parse_drafts_count),
    фото 1–10 / разрешённые форматы / ≤25 МБ.

    Константы лимитов импортированы из publisher (не дублируем).

    Возвращает [{field, error}, ...]; пустой список = форма валидна.
    Никогда не бросает исключений.
    """
    errors: list[dict] = []

    def _text(name: str) -> str:
        return str(fields.get(name) or "").strip()

    # Обязательные текстовые поля
    if not _text("title"):
        errors.append({"field": "title", "error": "Название: поле не заполнено"})
    if not _text("description"):
        errors.append({"field": "description", "error": "Описание: поле не заполнено"})

    # Количество черновиков
    if parse_drafts_count(fields.get("drafts_count")) is None:
        errors.append({
            "field": "drafts_count",
            "error": (
                f"Сколько черновиков: целое число от {DRAFTS_MIN} до {DRAFTS_MAX}"
            ),
        })

    # Фотографии: количество, форматы, размер
    if not (MIN_PHOTOS <= len(photo_meta) <= MAX_PHOTOS):
        errors.append({
            "field": "photos",
            "error": (
                f"Фотографий должно быть от {MIN_PHOTOS} до {MAX_PHOTOS}, "
                f"передано {len(photo_meta)}"
            ),
        })
    else:
        for filename, content_type, size in photo_meta:
            ext = Path(filename or "").suffix.lower()
            mime = (content_type or "").lower()
            if ext not in ALLOWED_PHOTO_EXTENSIONS and mime not in ALLOWED_PHOTO_MIME:
                errors.append({
                    "field": "photos",
                    "error": (
                        f"Файл {filename!r}: недопустимый формат "
                        f"(разрешены jpeg/png/gif/heic)"
                    ),
                })
            if ext in (".heic", ".heif") and not heif_available():
                errors.append({
                    "field": "photos",
                    "error": (
                        f"Файл {filename!r}: HEIC-формат требует пакет pillow-heif "
                        f"(pip install pillow-heif) — установите его или "
                        f"конвертируйте фото в JPEG"
                    ),
                })
            if size > MAX_PHOTO_SIZE_BYTES:
                errors.append({
                    "field": "photos",
                    "error": f"Файл {filename!r}: больше 25 МБ",
                })

    return errors


# ---------------------------------------------------------------------------
# Фоновая задача подготовки
# ---------------------------------------------------------------------------


async def run_prep_job(
    prep_id: str,
    job: dict,
    *,
    title: str,
    description: str,
    source_photos: list[Path],
    drafts_count: int,
    facts: dict[str, str],
    base_dir: Path,
) -> None:
    """Фоновая задача подготовки N вариантов текста и фото.

    Обновляет job: status / step / step_label / done / total / error.
    Раскладывает результаты на диск (base_dir) + source/ для перегенерации.

    Алгоритм:
        1. status=running, шаг vary_texts: один вызов vary_listing на все N,
           seed случайный; изменяются все варианты, включая №1.
        2. Шаг vary_photos: build_modified_presets(n, seed), для черновика i — все фото
           через пресет i (одна технология на черновик); ошибки apply_preset
           глотает сам, заметку пишем в notes если байты не изменились.
        3. Раскладка по папкам.
        4. done.

    Любое исключение → status=failed + job["error"], наружу не выпускается.
    """
    job["status"] = "running"
    job["total"] = PREP_TOTAL_STEPS

    try:
        # ── Сохраняем исходники в source/ (для перегенерации) ────────────────
        src_dir = _source_dir(base_dir)
        src_photos_dir = src_dir / "photos"
        src_photos_dir.mkdir(parents=True, exist_ok=True)

        # Сохраняем исходные тексты и факты
        (src_dir / "title.txt").write_text(title, encoding="utf-8")
        (src_dir / "text.txt").write_text(description, encoding="utf-8")
        (src_dir / "facts.json").write_text(
            json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Копируем исходные фото в source/photos/
        copied_source_photos: list[Path] = []
        for i, src_photo in enumerate(source_photos, start=1):
            ext = src_photo.suffix.lower() or ".jpg"
            dest = src_photos_dir / f"photo_{i:02d}{ext}"
            shutil.copy2(src_photo, dest)
            copied_source_photos.append(dest)

        logger.info(
            "Подготовка %s: исходники сохранены в source/ (%d фото)",
            prep_id,
            len(copied_source_photos),
        )

        # ── Шаг 1: vary_texts ────────────────────────────────────────────────
        _set_prep_step(job, "vary_texts")

        seed = random.randrange(2**32)
        # Сид пишем на диск СРАЗУ, до генерации черновиков: если процесс
        # упадёт посреди раскладки draft_NN, после рестарта recover_prep_job
        # сможет детерминированно дописать недостающее тем же сидом (F38),
        # а не бросать всю недописанную подготовку.
        _write_seed_info(base_dir, seed=seed, drafts_count=drafts_count)
        logger.info(
            "Подготовка %s: генерация %d вариантов текста, seed=%d (сохранён на диск)",
            prep_id,
            drafts_count,
            seed,
        )

        # ── Шаг 2: vary_photos ───────────────────────────────────────────────
        _set_prep_step(job, "vary_photos")

        _write_drafts(
            base_dir,
            prep_id=prep_id,
            title=title,
            description=description,
            drafts_count=drafts_count,
            facts=facts,
            seed=seed,
            source_photo_paths=copied_source_photos,
        )

        # ── done ──────────────────────────────────────────────────────────────
        _set_prep_step(job, "done")
        job["status"] = "done"
        logger.info(
            "Подготовка %s: завершена, %d черновиков готовы",
            prep_id,
            drafts_count,
        )

    except Exception as exc:
        logger.exception(
            "Подготовка %s: непредвиденная ошибка: %s", prep_id, exc
        )
        job["status"] = "failed"
        job["error"] = str(exc)


# ---------------------------------------------------------------------------
# Перегенерация одного черновика
# ---------------------------------------------------------------------------


def regenerate_draft(prep_dir: Path, draft_index: int) -> dict:
    """Перегенерирует черновик с номером draft_index с новым случайным seed.

    Источники читает из prep_dir/source/. Перезаписывает файлы
    draft_{NN}/title.txt, draft_{NN}/text.txt, draft_{NN}/photos/*, meta.json.

    Допустим любой существующий вариант, включая №1.
    Число вариантов n берётся из числа существующих папок draft_* в prep_dir.

    Возвращает обновлённую карточку (как в build_result) для этого черновика.
    Бросает ValueError при draft_index < 1.
    """
    if draft_index < 1:
        raise ValueError(f"draft_index должен быть >= 1, получено: {draft_index}")

    src_dir = _source_dir(prep_dir)

    # Читаем исходники
    title = (src_dir / "title.txt").read_text(encoding="utf-8")
    description = (src_dir / "text.txt").read_text(encoding="utf-8")
    facts: dict[str, str] = json.loads(
        (src_dir / "facts.json").read_text(encoding="utf-8")
    )

    # Число черновиков = кол-во существующих папок draft_*
    n = _count_draft_dirs(prep_dir)
    if draft_index > n:
        raise ValueError(
            f"Черновик с индексом {draft_index} не существует "
            f"(всего черновиков: {n})"
        )

    # Новый seed
    new_seed = random.randrange(2**32)
    logger.info(
        "Перегенерация черновика %d/%d в %s, новый seed=%d",
        draft_index,
        n,
        prep_dir,
        new_seed,
    )

    # Текстовые варианты: не разрешаем новому заголовку совпасть с любым другим
    # уже подготовленным черновиком пакета.
    reserved_titles = {
        (_draft_dir(prep_dir, index) / "title.txt").read_text(encoding="utf-8")
        for index in range(1, n + 1)
        if index != draft_index
        and (_draft_dir(prep_dir, index) / "title.txt").exists()
    }
    text_variants = vary_listing(
        title,
        description,
        n,
        seed=new_seed,
        facts=facts,
        reserved_titles=reserved_titles,
    )
    tv = text_variants[draft_index - 1]

    # Фото-пресеты: генерируем все n, берём пресет с индексом draft_index-1
    presets = build_modified_presets(n, seed=new_seed)
    preset = presets[draft_index - 1]

    # Исходные фото
    src_photos_dir = src_dir / "photos"
    source_photo_paths = sorted(src_photos_dir.iterdir())
    if not source_photo_paths:
        raise ValueError(f"Исходные фото не найдены в {src_photos_dir}")

    d_dir = _draft_dir(prep_dir, draft_index)
    d_photos_dir = d_dir / "photos"
    d_photos_dir.mkdir(parents=True, exist_ok=True)

    # Перезаписываем текст
    (d_dir / "title.txt").write_text(tv.title, encoding="utf-8")
    (d_dir / "text.txt").write_text(tv.description, encoding="utf-8")

    # Перезаписываем фото выбранного варианта.
    notes = tv.notes or ""
    warnings: list[str] = []
    for j, src_photo in enumerate(source_photo_paths, start=1):
        raw_bytes = src_photo.read_bytes()
        out_bytes, out_ext, warn = _photo_for_draft(
            draft_index, j, raw_bytes, src_photo.suffix, preset
        )
        if warn:
            warnings.append(warn)
            logger.warning(
                "Перегенерация черновика %d: %s (пресет %r)",
                draft_index, warn, preset.name,
            )
        (d_photos_dir / f"photo_{j:02d}{out_ext}").write_bytes(out_bytes)

    # Перезаписываем meta.json
    meta = {
        "preset": preset.name,
        "seed": new_seed,
        "notes": notes,
        "warnings": warnings,
    }
    (d_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "Перегенерация черновика %d: завершена (пресет %r, seed=%d)",
        draft_index,
        preset.name,
        new_seed,
    )

    # Собираем карточку
    prep_id = prep_dir.name.removeprefix("prep_")
    return _build_card(prep_id, draft_index, d_dir)


def update_draft_text(
    prep_dir: Path,
    draft_index: int,
    title: str,
    description: str,
) -> dict:
    """Сохраняет ручную правку названия и описания одного варианта.

    Фотографии, preset, notes и warnings не меняются. Возвращает карточку в
    том же формате, что `build_result` и `regenerate_draft`.
    """
    n_drafts = _count_draft_dirs(prep_dir)
    if draft_index < 1 or draft_index > n_drafts:
        raise ValueError(
            f"Черновик с индексом {draft_index} не существует "
            f"(всего черновиков: {n_drafts})"
        )

    normalized_title = str(title).strip()
    normalized_description = str(description).strip()
    if not normalized_title:
        raise ValueError("Название: поле не заполнено")
    if not normalized_description:
        raise ValueError("Описание: поле не заполнено")

    title_key = normalize_title_key(normalized_title)
    for index in range(1, n_drafts + 1):
        if index == draft_index:
            continue
        other_title_path = _draft_dir(prep_dir, index) / "title.txt"
        if (
            other_title_path.exists()
            and normalize_title_key(
                other_title_path.read_text(encoding="utf-8")
            ) == title_key
        ):
            raise ValueError(
                "Название должно отличаться от остальных вариантов пакета"
            )

    d_dir = _draft_dir(prep_dir, draft_index)
    if not d_dir.is_dir():
        raise ValueError(f"Папка варианта не найдена: {d_dir}")

    (d_dir / "title.txt").write_text(normalized_title, encoding="utf-8")
    (d_dir / "text.txt").write_text(normalized_description, encoding="utf-8")

    logger.info(
        "Ручная правка текста черновика %d/%d сохранена",
        draft_index,
        n_drafts,
    )
    prep_id = prep_dir.name.removeprefix("prep_")
    return _build_card(prep_id, draft_index, d_dir)


# ---------------------------------------------------------------------------
# Построение результата
# ---------------------------------------------------------------------------


def _done_job_record(drafts_count: int) -> dict:
    return {
        "status": "done",
        "step": "done",
        "step_label": _PREP_STEP_LABELS["done"],
        "done": PREP_TOTAL_STEPS,
        "total": PREP_TOTAL_STEPS,
        "error": None,
        "drafts_count": drafts_count,
    }


def _regenerate_from_seed(prep_dir: Path, seed_info: dict) -> Optional[dict]:
    """Дописывает недостающие/битые draft_NN тем же сидом, что и до рестарта.

    Возможно только если source/ (исходники, скопированные ДО генерации
    первого черновика) цел: title.txt, text.txt, facts.json и хотя бы одно
    фото в source/photos/. Раз seed и исходники те же — vary_listing и
    build_modified_presets детерминированы, повторный проход даёт тот же
    результат, что дал бы непрерывный запуск. None — если исходники сами не
    сохранились (крах случился ещё до их копирования) — тогда пересчитывать
    нечего, а не полагаться на угадывание.
    """
    src_dir = _source_dir(prep_dir)
    title_path = src_dir / "title.txt"
    text_path = src_dir / "text.txt"
    facts_path = src_dir / "facts.json"
    src_photos_dir = src_dir / "photos"
    if not (
        title_path.is_file()
        and text_path.is_file()
        and facts_path.is_file()
        and src_photos_dir.is_dir()
    ):
        return None
    source_photo_paths = sorted(p for p in src_photos_dir.iterdir() if p.is_file())
    if not source_photo_paths:
        return None

    try:
        title = title_path.read_text(encoding="utf-8")
        description = text_path.read_text(encoding="utf-8")
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(facts, dict):
        return None

    drafts_count = seed_info["drafts_count"]
    prep_id = prep_dir.name.removeprefix("prep_")
    try:
        _write_drafts(
            prep_dir,
            prep_id=prep_id,
            title=title,
            description=description,
            drafts_count=drafts_count,
            facts=facts,
            seed=seed_info["seed"],
            source_photo_paths=source_photo_paths,
        )
    except Exception:
        logger.exception(
            "Восстановление подготовки %s: не удалось дописать черновики тем же сидом",
            prep_id,
        )
        return None

    logger.info(
        "Восстановление подготовки %s: недописанные черновики дозаписаны тем же "
        "сидом (seed=%d)", prep_id, seed_info["seed"],
    )
    return _done_job_record(drafts_count)


def recover_prep_job(prep_dir: Path) -> Optional[dict]:
    """Восстанавливает запись PREP_JOBS[prep_id] по данным на диске.

    Два случая:
    - подготовка успела ПОЛНОСТЬЮ завершиться до рестарта — все draft_NN на
      месте, просто собираем запись обратно;
    - подготовка была прервана посреди генерации (сбой во время
      vary_texts/vary_photos), но сид уже сохранён на диск (source/seed.json,
      см. _write_seed_info) — дописываем недостающее тем же сидом, результат
      детерминирован и совпадает с тем, что дал бы непрерывный запуск.

    Если ни того, ни другого нет (крах случился ещё до сохранения сида/
    исходников либо seed.json отсутствует — legacy-каталог, подготовленный до
    этой правки) — возвращаем None, вызывающий код это логирует; повторная
    публикация должна начинаться заново, а не угадывать недостающие данные.
    """
    if not prep_dir.is_dir():
        return None

    seed_info = _read_seed_info(prep_dir)
    if seed_info is not None:
        drafts_count = seed_info["drafts_count"]
        if all(_draft_complete(prep_dir, i) for i in range(1, drafts_count + 1)):
            return _done_job_record(drafts_count)
        return _regenerate_from_seed(prep_dir, seed_info)

    # Legacy-каталог без seed.json: доступно только восстановление уже
    # полностью готового набора, пересчитать недостающее нечем.
    drafts_count = _count_draft_dirs(prep_dir)
    if drafts_count == 0:
        return None
    if all(_draft_complete(prep_dir, i) for i in range(1, drafts_count + 1)):
        return _done_job_record(drafts_count)
    return None


def build_result(prep_dir: Path, drafts_count: int) -> list[dict]:
    """Собирает список карточек [{index, title, description, preset_name,
    photo_urls, notes}, ...] из файлов на диске.

    photo_urls — строки вида
    /api/publish/prepare/photo/{prep_id}/{draft_index}/{photo_index}.
    prep_id вычисляется из имени папки prep_dir.
    Индексы с 1. photo_urls — по числу фото в draft_NN/photos/.
    """
    prep_id = prep_dir.name.removeprefix("prep_")
    cards: list[dict] = []
    for i in range(1, drafts_count + 1):
        d_dir = _draft_dir(prep_dir, i)
        cards.append(_build_card(prep_id, i, d_dir))
    return cards


def _build_card(prep_id: str, draft_index: int, d_dir: Path) -> dict:
    """Строит одну карточку черновика из файлов в d_dir."""
    title = (d_dir / "title.txt").read_text(encoding="utf-8") if (d_dir / "title.txt").exists() else ""
    description = (d_dir / "text.txt").read_text(encoding="utf-8") if (d_dir / "text.txt").exists() else ""

    meta: dict = {}
    meta_path = d_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    photos_dir = d_dir / "photos"
    photo_files: list[Path] = []
    if photos_dir.exists():
        photo_files = sorted(
            p for p in photos_dir.iterdir() if p.is_file()
        )

    photo_urls = [
        f"/api/publish/prepare/photo/{prep_id}/{draft_index}/{j}"
        for j in range(1, len(photo_files) + 1)
    ]

    warnings = meta.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = []

    return {
        "index": draft_index,
        "title": title,
        "description": description,
        "preset_name": meta.get("preset", ""),
        "photo_urls": photo_urls,
        "notes": meta.get("notes", ""),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Самотесты
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import io
    import sys

    from PIL import Image

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    def _make_png_bytes(w: int = 80, h: int = 60, color: tuple = (100, 150, 200)) -> bytes:
        """Создаёт синтетическое PNG-изображение."""
        img = Image.new("RGB", (w, h), color=color)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # ── Тест 1: validate_prepare_form — пустое название ───────────────────────
    errs = validate_prepare_form(
        {"title": "", "description": "Описание", "drafts_count": "3"},
        [("photo.jpg", "image/jpeg", 1024)],
    )
    assert any(e["field"] == "title" for e in errs), f"Ожидали ошибку title: {errs}"
    print("[OK] Тест 1: пустое название → ошибка title")

    # ── Тест 2: validate_prepare_form — пустое описание ───────────────────────
    errs = validate_prepare_form(
        {"title": "Название", "description": "", "drafts_count": "3"},
        [("photo.jpg", "image/jpeg", 1024)],
    )
    assert any(e["field"] == "description" for e in errs), (
        f"Ожидали ошибку description: {errs}"
    )
    print("[OK] Тест 2: пустое описание → ошибка description")

    # ── Тест 3: validate_prepare_form — 0 фото и 11 фото ─────────────────────
    _good_fields = {"title": "Название", "description": "Описание", "drafts_count": "3"}
    errs = validate_prepare_form(_good_fields, [])
    assert any(e["field"] == "photos" for e in errs), f"Ожидали ошибку photos (0): {errs}"
    errs = validate_prepare_form(
        _good_fields,
        [("p.jpg", "image/jpeg", 1024)] * 11,
    )
    assert any(e["field"] == "photos" for e in errs), (
        f"Ожидали ошибку photos (11): {errs}"
    )
    print("[OK] Тест 3: 0 фото и 11 фото → ошибка photos")

    # ── Тест 4: validate_prepare_form — drafts_count "0"/"21"/"abc" ──────────
    for bad_dc in ("0", "21", "abc"):
        errs = validate_prepare_form(
            {"title": "Название", "description": "Описание", "drafts_count": bad_dc},
            [("p.jpg", "image/jpeg", 1024)],
        )
        assert any(e["field"] == "drafts_count" for e in errs), (
            f"drafts_count={bad_dc!r} должно отклоняться: {errs}"
        )
    print("[OK] Тест 4: drafts_count '0'/'21'/'abc' → ошибка drafts_count")

    # ── Тест 5: validate_prepare_form — валидная форма → пустой список ────────
    errs = validate_prepare_form(
        {"title": "Название", "description": "Описание", "drafts_count": "3"},
        [
            ("p1.jpg", "image/jpeg", 1024 * 1024),
            ("p2.jpg", "image/jpeg", 2048),
        ],
    )
    assert errs == [], f"Валидная форма не должна давать ошибок: {errs}"
    print("[OK] Тест 5: валидная форма → пустой список ошибок")

    # ── Тест 6: run_prep_job + build_result во временной папке ───────────────
    with tempfile.TemporaryDirectory() as _tmpdir:
        _base = Path(_tmpdir) / "prep_test001"
        _base.mkdir()

        # Создаём два синтетических PNG исходника во временной папке
        _src_photo_dir = Path(_tmpdir) / "input_photos"
        _src_photo_dir.mkdir()
        _photo1 = _src_photo_dir / "img1.png"
        _photo2 = _src_photo_dir / "img2.png"
        _photo1.write_bytes(_make_png_bytes(color=(100, 150, 200)))
        _photo2.write_bytes(_make_png_bytes(color=(200, 100, 50)))

        _job: dict = {}
        asyncio.run(run_prep_job(
            "test001",
            _job,
            title="Пиджак Hugo Boss",
            description="Отличный пиджак, без дефектов. Размер 48.",
            source_photos=[_photo1, _photo2],
            drafts_count=3,
            facts={"brand": "Hugo Boss", "size": "48 (M)"},
            base_dir=_base,
        ))

        # Статус должен быть done
        assert _job.get("status") == "done", (
            f"Ожидали status=done, получили: {_job}"
        )

        # Структура папок: source/ + draft_01..03 с photos/title.txt/text.txt/meta.json
        assert (_base / "source" / "title.txt").exists(), "source/title.txt отсутствует"
        assert (_base / "source" / "text.txt").exists(), "source/text.txt отсутствует"
        assert (_base / "source" / "facts.json").exists(), "source/facts.json отсутствует"
        assert (_base / "source" / "photos").is_dir(), "source/photos/ отсутствует"

        for _i in range(1, 4):
            _dd = _base / f"draft_{_i:02d}"
            assert _dd.is_dir(), f"Папка {_dd.name} отсутствует"
            assert (_dd / "title.txt").exists(), f"{_dd.name}/title.txt отсутствует"
            assert (_dd / "text.txt").exists(), f"{_dd.name}/text.txt отсутствует"
            assert (_dd / "meta.json").exists(), f"{_dd.name}/meta.json отсутствует"
            assert (_dd / "photos").is_dir(), f"{_dd.name}/photos/ отсутствует"
            _photos = sorted((_dd / "photos").iterdir())
            assert len(_photos) == 2, (
                f"{_dd.name}/photos/ содержит {len(_photos)} файлов, ожидалось 2"
            )

        # build_result отдаёт 3 карточки с непустыми полями и по 2 photo_urls
        _cards = build_result(_base, 3)
        assert len(_cards) == 3, f"Ожидали 3 карточки, получили {len(_cards)}"
        for _c in _cards:
            assert _c["title"].strip(), f"Пустой title в карточке: {_c}"
            assert _c["description"].strip(), f"Пустое description в карточке: {_c}"
            assert _c["preset_name"].strip(), f"Пустой preset_name в карточке: {_c}"
            assert len(_c["photo_urls"]) == 2, (
                f"Ожидали 2 photo_urls, получили {len(_c['photo_urls'])}: {_c}"
            )

        # Карточка 1 — полноценный изменённый вариант.
        _orig_title = "Пиджак Hugo Boss"
        _orig_desc = "Отличный пиджак, без дефектов. Размер 48."
        assert _cards[0]["title"] != _orig_title, (
            f"Карточка 1 сохранила исходное название: {_orig_title!r}"
        )
        assert _cards[0]["description"] != _orig_desc, (
            "Карточка 1 сохранила исходное описание"
        )

        # Все карточки несут поле warnings (список, обычно пустой)
        for _c in _cards:
            assert "warnings" in _c, f"Карточка без поля warnings: {_c}"
            assert isinstance(_c["warnings"], list), f"warnings не список: {_c}"

        # Фото каждого варианта, включая №1, обработано.
        _src_photo1 = sorted((_base / "source" / "photos").iterdir())[0].read_bytes()
        _draft1_photo1 = sorted((_base / "draft_01" / "photos").iterdir())[0].read_bytes()
        assert _draft1_photo1 != _src_photo1, (
            "Фото варианта №1 совпало с исходником — вариация не применилась"
        )
        # Фото варианта №2 — обработано, отличается от исходника
        _draft2_photo1 = sorted((_base / "draft_02" / "photos").iterdir())[0].read_bytes()
        assert _draft2_photo1 != _src_photo1, (
            "Фото варианта №2 совпало с исходником — вариация не применилась"
        )

        print("[OK] Тест 6: run_prep_job + build_result — все варианты изменены, warnings")

        # ── Тест 7: regenerate_draft — изменение черновика 2 ─────────────────
        # Запоминаем байты до перегенерации
        _title2_before = (_base / "draft_02" / "title.txt").read_text(encoding="utf-8")
        _text2_before = (_base / "draft_02" / "text.txt").read_text(encoding="utf-8")
        _photo2_before = sorted((_base / "draft_02" / "photos").iterdir())[0].read_bytes()

        _title1_before = (_base / "draft_01" / "title.txt").read_text(encoding="utf-8")
        _photo1_before = sorted((_base / "draft_01" / "photos").iterdir())[0].read_bytes()
        _title3_before = (_base / "draft_03" / "title.txt").read_text(encoding="utf-8")
        _photo3_before = sorted((_base / "draft_03" / "photos").iterdir())[0].read_bytes()

        _card2 = regenerate_draft(_base, 2)

        # draft_02 изменился — хотя бы один из файлов текста или фото поменялся
        _title2_after = (_base / "draft_02" / "title.txt").read_text(encoding="utf-8")
        _photo2_after = sorted((_base / "draft_02" / "photos").iterdir())[0].read_bytes()
        _something_changed = (
            _title2_after != _title2_before or _photo2_after != _photo2_before
        )
        assert _something_changed, (
            "После regenerate_draft(2): ни title, ни фото не изменились"
        )

        # draft_01 и draft_03 не тронуты
        _title1_after = (_base / "draft_01" / "title.txt").read_text(encoding="utf-8")
        _photo1_after = sorted((_base / "draft_01" / "photos").iterdir())[0].read_bytes()
        _title3_after = (_base / "draft_03" / "title.txt").read_text(encoding="utf-8")
        _photo3_after = sorted((_base / "draft_03" / "photos").iterdir())[0].read_bytes()
        assert _title1_after == _title1_before, "draft_01/title.txt изменился после regenerate_draft(2)"
        assert _photo1_after == _photo1_before, "draft_01 фото изменилось после regenerate_draft(2)"
        assert _title3_after == _title3_before, "draft_03/title.txt изменился после regenerate_draft(2)"
        assert _photo3_after == _photo3_before, "draft_03 фото изменилось после regenerate_draft(2)"

        # Карточка содержит index=2 и непустые поля
        assert _card2["index"] == 2, f"index карточки: {_card2['index']}"
        assert _card2["title"].strip(), "title карточки пустой"
        assert _card2["description"].strip(), "description карточки пустое"

        print("[OK] Тест 7: regenerate_draft(2) — изменены title/фото черновика 2, draft_01 и draft_03 не тронуты")

        # ── Тест 8: regenerate_draft(1) разрешён ─────────────────────────────
        _card1 = regenerate_draft(_base, 1)
        assert _card1["index"] == 1, _card1
        assert _card1["title"].strip() and _card1["description"].strip(), _card1
        assert "оригинал" not in _card1["preset_name"].lower(), _card1
        print("[OK] Тест 8: regenerate_draft(1) → обновлённый вариант")

    # ── Тест 9: ошибка задачи — несуществующая папка исходников ──────────────
    with tempfile.TemporaryDirectory() as _tmpdir2:
        _base2 = Path(_tmpdir2) / "prep_err001"
        _base2.mkdir()

        # Исходники намеренно НЕ кладём — run_prep_job упрётся в отсутствие файлов
        _nonexistent_photo = Path(_tmpdir2) / "no_such_photo.png"

        _job2: dict = {}
        _exception_escaped = False
        try:
            asyncio.run(run_prep_job(
                "err001",
                _job2,
                title="Тест",
                description="Описание",
                source_photos=[_nonexistent_photo],
                drafts_count=2,
                facts={},
                base_dir=_base2,
            ))
        except Exception:
            _exception_escaped = True

        assert not _exception_escaped, (
            "run_prep_job не должен выпускать исключения наружу"
        )
        assert _job2.get("status") == "failed", (
            f"Ожидали status=failed, получили: {_job2.get('status')!r}"
        )
        assert _job2.get("error"), (
            f"Поле error должно быть непустым: {_job2}"
        )
        print("[OK] Тест 9: несуществующая папка исходников → status=failed, error непустой, исключения нет")

    # ── Тест 10: HEIC отклоняется валидацией, когда pillow-heif недоступен ────
    import photo_variation as _pv
    _saved_flag = _pv._HEIF_REGISTERED
    _pv._HEIF_REGISTERED = False
    try:
        errs = validate_prepare_form(
            {"title": "Название", "description": "Описание", "drafts_count": "2"},
            [("photo.heic", "image/heic", 1024)],
        )
        assert any(
            e["field"] == "photos" and "pillow-heif" in e["error"] for e in errs
        ), f"HEIC без pillow-heif должен отклоняться с подсказкой: {errs}"
    finally:
        _pv._HEIF_REGISTERED = _saved_flag
    print("[OK] Тест 10: HEIC без pillow-heif → ошибка валидации с подсказкой")

    # ── Тест 11: сбой обработки фото любого варианта → warnings, status done ─
    with tempfile.TemporaryDirectory() as _tmpdir3:
        _base3 = Path(_tmpdir3) / "prep_warn001"
        _base3.mkdir()
        _bad_dir = Path(_tmpdir3) / "bad_input"
        _bad_dir.mkdir()
        # «Битый» исходник: расширение .jpg, но байты не являются изображением.
        # apply_preset не сможет открыть → молча вернёт оригинал → для №2 это сбой.
        _bad_photo = _bad_dir / "broken.jpg"
        _bad_photo.write_bytes(b"this is definitely not a valid image file")

        _job3: dict = {}
        asyncio.run(run_prep_job(
            "warn001",
            _job3,
            title="Тест",
            description="Описание",
            source_photos=[_bad_photo],
            drafts_count=2,
            facts={},
            base_dir=_base3,
        ))
        assert _job3.get("status") == "done", (
            f"Тест 11: ожидали status=done (graceful), получили {_job3}"
        )
        _cards3 = build_result(_base3, 2)
        assert _cards3[0]["warnings"], (
            f"Тест 11: №1 на битом фото должен иметь warnings: {_cards3[0]}"
        )
        # №2 — обработка сорвалась на битом файле → warnings непуст
        assert _cards3[1]["warnings"], (
            f"Тест 11: №2 на битом фото должен иметь warnings: {_cards3[1]}"
        )
    print("[OK] Тест 11: сбой обработки фото всех вариантов → warnings, статус done")

    # ── Тест 12: recover_prep_job — восстановление PREP_JOBS с диска (F38) ────
    with tempfile.TemporaryDirectory() as _tmpdir4:
        _base4 = Path(_tmpdir4) / "prep_recover001"
        _base4.mkdir()

        _photo_dir = Path(_tmpdir4) / "input_photos2"
        _photo_dir.mkdir()
        _photo3 = _photo_dir / "img.png"
        _photo3.write_bytes(_make_png_bytes())

        # До завершения задачи (папок draft_* ещё нет) — восстановить нечего.
        assert recover_prep_job(_base4) is None, (
            "Тест 12: пустая папка не должна давать восстановление"
        )

        _job4: dict = {}
        asyncio.run(run_prep_job(
            "recover001",
            _job4,
            title="Пиджак",
            description="Описание пиджака",
            source_photos=[_photo3],
            drafts_count=2,
            facts={},
            base_dir=_base4,
        ))
        assert _job4.get("status") == "done", f"Тест 12: подготовка не завершилась: {_job4}"

        _restored = recover_prep_job(_base4)
        assert _restored is not None, "Тест 12: завершённая подготовка должна восстанавливаться"
        assert _restored["status"] == "done", _restored
        assert _restored["drafts_count"] == 2, _restored

        # Сид сохранён на диске (F38) — seed.json появился ДО первого draft_NN.
        assert (_base4 / "source" / "seed.json").is_file(), (
            "Тест 12: source/seed.json должен сохраняться при run_prep_job"
        )

        # Удаляем фото одного черновика — имитируем крах посреди vary_photos.
        # Сид на диске есть → recover_prep_job обязан детерминированно
        # дописать недостающее, а не сдаться.
        _title2_before_crash = (_base4 / "draft_02" / "title.txt").read_text(encoding="utf-8")
        shutil.rmtree(_base4 / "draft_02" / "photos")
        _restored2 = recover_prep_job(_base4)
        assert _restored2 is not None, (
            "Тест 12: недописанный черновик с сохранённым сидом обязан "
            "дозаписываться детерминированно, а не отказывать"
        )
        assert _restored2["drafts_count"] == 2, _restored2
        assert (_base4 / "draft_02" / "photos").is_dir() and any(
            (_base4 / "draft_02" / "photos").iterdir()
        ), "Тест 12: фото черновика 2 должны быть дозаписаны"
        _title2_after_recover = (_base4 / "draft_02" / "title.txt").read_text(encoding="utf-8")
        assert _title2_after_recover == _title2_before_crash, (
            "Тест 12: дозапись тем же сидом должна быть детерминированной "
            "(title черновика 2 не должен измениться)"
        )

    # ── Тест 13: recover_prep_job — крах ДО первого draft_NN (F38) ───────────
    with tempfile.TemporaryDirectory() as _tmpdir5:
        _base5 = Path(_tmpdir5) / "prep_recover002"
        _base5.mkdir()
        _photo_dir5 = Path(_tmpdir5) / "input_photos3"
        _photo_dir5.mkdir()
        _photo5 = _photo_dir5 / "img.png"
        _photo5.write_bytes(_make_png_bytes())

        _job5: dict = {}
        asyncio.run(run_prep_job(
            "recover002",
            _job5,
            title="Кроссовки",
            description="Описание кроссовок",
            source_photos=[_photo5],
            drafts_count=2,
            facts={},
            base_dir=_base5,
        ))
        assert _job5.get("status") == "done", f"Тест 13: подготовка не завершилась: {_job5}"

        # Имитируем краш ДО того, как успел записаться хотя бы один draft_NN:
        # source/ и seed.json целы, а самих черновиков на диске ещё нет.
        shutil.rmtree(_base5 / "draft_01")
        shutil.rmtree(_base5 / "draft_02")
        assert _count_draft_dirs(_base5) == 0, "Тест 13: draft_NN должны отсутствовать"

        _restored3 = recover_prep_job(_base5)
        assert _restored3 is not None, (
            "Тест 13: сохранённый сид без единого draft_NN всё равно должен "
            "давать полную регенерацию"
        )
        assert _restored3["drafts_count"] == 2, _restored3
        for _i in range(1, 3):
            assert _draft_complete(_base5, _i), (
                f"Тест 13: draft_{_i:02d} должен быть дозаписан с нуля"
            )

    print(
        "[OK] Тест 12-13: recover_prep_job — полное восстановление и "
        "детерминированная дозапись тем же сидом (F38)"
    )

    print("\n=== Все самотесты preparation.py пройдены ===")
    sys.exit(0)
