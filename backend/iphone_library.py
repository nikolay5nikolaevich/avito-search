"""Локальные материалы iPhone: проверка каталога и выбор одного фотопака."""

import io
import random
import re
from pathlib import Path

from PIL import Image, ImageOps

from category_profiles import IPHONE

LIBRARY_ROOT = Path(__file__).resolve().parents[1] / "library" / "iphone"
DIRECT_PACK_ID = "__memory__"
SIM_TYPES = ("eSIM", "SIM + eSIM", "SIM + SIM")
_MEMORY = re.compile(r"\b(\d+)\s*(GB|ГБ|TB|ТБ)\b", re.IGNORECASE)
_MARKER = re.compile(r"\{([^{}]*)\}")
_MARKERS = {"модель", "память", "цвет", "SIM"}


def resolve_model(title: str) -> str:
    """Определяет ровно одну модель из подтверждённого справочника."""
    text = " ".join(str(title).casefold().split())
    if len(re.findall(r"\biphone\b", text)) != 1:
        raise ValueError("Название должно содержать одну модель iPhone")
    text = _MEMORY.sub(" ", text)
    text = re.sub(r"\biphone\b", " ", text)
    candidates = list(re.finditer(r"\b\d+[a-z]*\b(?:\s+pro\b)?(?:\s+max\b)?", text))
    if len(candidates) != 1:
        raise ValueError("Неизвестная или неоднозначная модель iPhone в названии")
    candidate = candidates[0]
    model_key = "iphone " + " ".join(candidate.group().split())
    remainder = text[:candidate.start()] + text[candidate.end():]
    if re.search(r"\b(?:pro|max\w*|mini|plus|ultra|air)\b", remainder):
        raise ValueError("Неподдерживаемая модель iPhone в названии")
    for model in IPHONE.model_options:
        if model.casefold() == model_key:
            return model
    raise ValueError("Неподдерживаемая модель iPhone в названии")


def validate_title_memory(title: str, memory: str) -> str:
    """Проверяет модель и явные указания памяти, возвращает модель."""
    model = resolve_model(title)
    if memory not in IPHONE.memory_for(model):
        raise ValueError(f"Память {memory!r} недоступна для {model}")
    expected = int(memory.split()[0])
    for amount, unit in _MEMORY.findall(title):
        actual = int(amount) * (1024 if unit.casefold() in ("tb", "тб") else 1)
        if actual != expected:
            raise ValueError("Память в названии противоречит выбранной памяти")
    return model


def validate_phone_text(description: str, sim_type: str) -> None:
    """Отклоняет явные противоречия; отсутствие фактов в тексте допустимо."""
    if sim_type not in SIM_TYPES:
        raise ValueError("Выберите SIM: eSIM, SIM + eSIM или SIM + SIM")
    text = description.casefold()
    without_negated = re.sub(
        r"\bне\s*(?:активирован|вскрыт|распечатан|использовал)\w*\b", "", text
    )
    if re.search(
        r"\b(?:активирован|вскрыт|распечатан|использовал)\w*\b"
        r"|\bне\s*запечатан|\bб\s*/\s*у\b|\bбывш\w*\s+в\s+употреблении",
        without_negated,
    ):
        raise ValueError("Описание противоречит новому неактивированному телефону в запечатанной упаковке")
    mentioned = set()
    for pattern, value in (
        (r"\bsim\s*\+\s*esim\b", "SIM + eSIM"),
        (r"\bsim\s*\+\s*sim\b|\b(?:2|две)\s*(?:физическ\w*\s+)?sim\b", "SIM + SIM"),
    ):
        if re.search(pattern, text):
            mentioned.add(value)
        text = re.sub(pattern, " ", text)
    if re.search(r"\besim\b", text):
        mentioned.add("eSIM")
    if mentioned - {sim_type}:
        raise ValueError(f"SIM в описании противоречит выбранному {sim_type}")


def _inside(path: Path, root: Path) -> Path:
    """Проверяет также цели симлинков и Windows junction."""
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Путь выходит за корень библиотеки: {path}")
    return path


def _model_directory(root: Path, model: str) -> Path:
    """Имя модели допускает разный регистр и повторные пробелы в папке."""
    key = " ".join(model.casefold().split())
    if root.is_dir():
        matches = [
            path for path in root.iterdir()
            if path.is_dir() and " ".join(path.name.casefold().split()) == key
        ]
        if len(matches) > 1:
            raise ValueError(f"Найдено несколько папок модели {model}: оставьте одну")
        if matches:
            return _inside(matches[0], root)
    return _inside(root / model, root)


def _pack_directory(memory_dir: Path, pack_id: str, root: Path) -> Path:
    if pack_id == DIRECT_PACK_ID:
        return _inside(memory_dir, root)
    return _inside(memory_dir / pack_id, root)


def _templates(model_dir: Path, root: Path) -> list[tuple[str, str]]:
    shared = _inside(root / "description", root)
    directory = shared if shared.exists() else _inside(model_dir / "descriptions", root)
    if not directory.is_dir():
        raise ValueError(f"Нет описаний: добавьте UTF-8 .txt в {directory}")
    result = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() != ".txt":
            continue
        _inside(path, root)
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"Не удалось прочитать UTF-8 шаблон {path}: {exc}") from exc
        if not text.strip():
            raise ValueError(f"Пустой шаблон описания: {path}")
        unknown = set(_MARKER.findall(text)) - _MARKERS
        if unknown:
            raise ValueError(f"Неизвестный маркер в {path.name}: {', '.join(sorted(unknown))}")
        result.append((path.name, text))
    if not result:
        raise ValueError(f"Нет описаний: добавьте UTF-8 .txt в {directory}")
    if directory == shared and len(result) != 1:
        raise ValueError(f"В {directory} должен быть ровно один эталонный .txt")
    return result


def _photos(pack: Path, root: Path) -> list[Path]:
    # Ленивый импорт: publisher использует проверку текста из этого модуля.
    from publisher import ALLOWED_PHOTO_EXTENSIONS, MAX_PHOTOS, MIN_PHOTOS, MAX_PHOTO_SIZE_BYTES
    from photo_variation import heif_available

    _inside(pack, root)
    if not pack.is_dir():
        raise ValueError(f"Фотопак не найден: {pack}")
    photos = sorted(p for p in pack.iterdir() if p.suffix.lower() in ALLOWED_PHOTO_EXTENSIONS)
    if not MIN_PHOTOS <= len(photos) <= MAX_PHOTOS:
        raise ValueError(f"В паке {pack.name!r} должно быть от {MIN_PHOTOS} до {MAX_PHOTOS} фото; найдено {len(photos)}")
    for path in photos:
        _inside(path, root)
        if path.suffix.lower() in (".heic", ".heif") and not heif_available():
            raise ValueError(f"Фото {path.name}: для HEIC нужен pillow-heif или конвертация в JPEG")
        try:
            if path.stat().st_size > MAX_PHOTO_SIZE_BYTES:
                raise ValueError(f"Фото {path.name}: больше 25 МБ")
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ValueError(f"Повреждённое фото {path.name}: {exc}") from exc
    return photos


def catalog(title: str, memory: str, *, root: Path | None = None) -> dict:
    """Перечитывает каталог; недоступные паки возвращает вместе с причиной."""
    root = Path(root) if root is not None else LIBRARY_ROOT
    model = resolve_model(title)
    result = {"model": model, "packs": [], "descriptions_count": 0, "error": ""}
    if not memory:
        result["error"] = "Выберите память для просмотра фотопаков"
        return result
    try:
        validate_title_memory(title, memory)
        model_dir = _model_directory(root, model)
        result["descriptions_count"] = len(_templates(model_dir, root))
        memory_dir = _inside(model_dir / memory.replace("ГБ", "GB"), root)
        if not memory_dir.is_dir():
            raise ValueError(f"Нет фотопаков: добавьте папку пака в {memory_dir}")
        from publisher import ALLOWED_PHOTO_EXTENSIONS

        packs = []
        if any(p.is_file() and p.suffix.lower() in ALLOWED_PHOTO_EXTENSIONS for p in memory_dir.iterdir()):
            packs.append((DIRECT_PACK_ID, "Основной", memory_dir))
        for pack in sorted(memory_dir.iterdir()):
            if pack.is_dir():
                if pack.name == DIRECT_PACK_ID:
                    raise ValueError(f"Имя {DIRECT_PACK_ID} зарезервировано: переименуйте папку пака")
                packs.append((pack.name, pack.name, pack))
        for pack_id, pack_name, pack in packs:
            item = {"id": pack_id, "name": pack_name, "available": False, "error": "", "photos_count": 0}
            try:
                photos = _photos(pack, root)
                item["photos_count"] = len(photos)
                item["photos"] = [{"id": photo.name} for photo in photos]
                item["available"] = True
            except (OSError, ValueError) as exc:
                item["error"] = str(exc)
            result["packs"].append(item)
        if not any(item["available"] for item in result["packs"]):
            result["error"] = f"Нет пригодных фотопаков в {memory_dir}"
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
    return result


def select_materials(
    title: str, memory: str, color: str, sim_type: str, drafts_count: int,
    pack_id: str = "", *, root: Path | None = None, seed: int | None = None,
    photo_order: list[str] | None = None,
) -> dict:
    """Фиксирует пак и порядок фото, подставляет параметры в эталонный текст."""
    from publisher import parse_drafts_count

    root = Path(root) if root is not None else LIBRARY_ROOT
    model = validate_title_memory(title, memory)
    if color not in IPHONE.colors_for(model):
        raise ValueError(f"Цвет {color!r} недоступен для {model}")
    validate_phone_text("", sim_type)
    count = parse_drafts_count(drafts_count)
    if count is None:
        raise ValueError("Количество черновиков должно быть от 1 до 20")
    if pack_id and (pack_id in (".", "..") or any(c in pack_id for c in "/\\:")):
        raise ValueError("Недопустимый идентификатор фотопака")
    listing = catalog(title, memory, root=root)
    if listing["error"]:
        raise ValueError(listing["error"])
    rng = random.Random(seed)
    packs = listing["packs"]
    if pack_id:
        pack = next((p for p in packs if p["id"] == pack_id), None)
        if pack is None:
            raise ValueError(f"Выбранный фотопак {pack_id!r} исчез или не найден")
        if not pack["available"]:
            raise ValueError(pack["error"])
    else:
        pack = rng.choice([p for p in packs if p["available"]])
    model_dir = _model_directory(root, model)
    photos = _photos(_pack_directory(model_dir / memory.replace("ГБ", "GB"), pack["id"], root), root)
    if photo_order is not None:
        if (
            not isinstance(photo_order, list)
            or any(not isinstance(name, str) for name in photo_order)
            or len(photo_order) != len(photos)
            or set(photo_order) != {photo.name for photo in photos}
        ):
            raise ValueError(
                "Порядок фото должен содержать каждое фото выбранного пака "
                "ровно один раз. Обновите библиотеку"
            )
        by_name = {photo.name: photo for photo in photos}
        photos = [by_name[name] for name in photo_order]
    values = {"модель": model, "память": memory, "цвет": color, "SIM": sim_type}
    templates = []
    for name, text in _templates(model_dir, root):
        rendered = _MARKER.sub(lambda match: values[match[1]], text)
        validate_phone_text(rendered, sim_type)
        templates.append((name, rendered))
    chosen = [rng.choice(templates) for _ in range(count)]
    return {
        "model": model, "memory": memory, "color": color, "sim_type": sim_type,
        "pack_id": pack["id"], "pack_name": pack["name"], "source_title": title.strip(),
        "source_photos": photos, "descriptions": [text for _, text in chosen],
        "template_names": [name for name, _ in chosen],
        "photo_order": [photo.name for photo in photos],
    }


def preview_photo(
    title: str, memory: str, pack_id: str, photo_id: str,
    *, root: Path | None = None,
) -> bytes:
    """Возвращает небольшое JPEG-превью только фотографии внутри выбранного пака."""
    root = Path(root) if root is not None else LIBRARY_ROOT
    model = validate_title_memory(title, memory)
    for value in (pack_id, photo_id):
        if not value or value in (".", "..") or any(c in value for c in "/\\:"):
            raise ValueError("Недопустимый идентификатор пака или фото")
    model_dir = _model_directory(root, model)
    pack = _pack_directory(model_dir / memory.replace("ГБ", "GB"), pack_id, root)
    photos = _photos(pack, root)
    photo = next((path for path in photos if path.name == photo_id), None)
    if photo is None:
        raise ValueError("Фото не найдено в выбранном паке. Обновите библиотеку")
    with Image.open(photo) as source:
        source.thumbnail((1000, 1000))
        picture = ImageOps.exif_transpose(source).convert("RGB")
        output = io.BytesIO()
        picture.save(output, format="JPEG", quality=85)
    return output.getvalue()
