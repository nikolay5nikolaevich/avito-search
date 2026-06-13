"""
Пресеты лёгкой обработки фото через Pillow для вариативности черновиков.

Назначение: каждый черновик объявления получает слегка отличающуюся версию
фотографий — меняется перцептивный хэш, но не суть товара. Только Pillow,
без нейросетей, без сети.

ЗАПРЕТ: зеркалирование по горизонтали (борт пиджака, бирки/логотипы) — поля
mirror/flip в Preset отсутствуют намеренно.

Правило ошибок: apply_preset на конкретном фото при любом исключении → лог +
вернуть оригинальные байты, наружу не выпускать.

HEIC: Pillow не открывает → лог + вернуть оригинал.

Каждый пресет (кроме №1-оригинала) гарантированно содержит геометрию
(зум/отдаление/поворот) + тон (яркость/контраст/насыщенность/температура).
"""

import io
import logging
import math
import random
from dataclasses import dataclass, fields as dc_fields
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclass пресета
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    """Набор параметров лёгкой обработки одного фото.

    Все значения — «мягкие»: фото остаётся узнаваемым, меняется лишь
    перцептивный хэш (антиспам Авито).

    ЗЕРКАЛИРОВАНИЕ ЗАПРЕЩЕНО: поля mirror/flip в dataclass'е нет.
    """

    name: str                 # Человекочитаемо, по-русски: "зум 4% + теплее"
    zoom_pct: float = 0.0     # 0, +2..6 — кроп (приближение); -2..-4 — отдаление
    rotate_deg: float = 0.0   # 0 или ±1..2 — с обрезкой полей (без чёрных углов)
    brightness: float = 1.0   # 0.92..1.08
    contrast: float = 1.0     # 0.92..1.08
    saturation: float = 1.0   # 0.92..1.08
    temp_shift: int = 0       # -8..+8 — сдвиг каналов R/B (теплее/холоднее)
    noise_alpha: float = 0.0  # 0 или 0.01..0.03 — Image.blend с Image.effect_noise
    jpeg_quality: int = 92    # 85..95


# ---------------------------------------------------------------------------
# Семейства вариации и их параметры
# ---------------------------------------------------------------------------

# Возможные значения для выборки (не включают нейтральные «дефолтные» значения,
# кроме случаев, где нейтральное значение — тоже осмысленный вариант)
_ZOOM_VALUES: list[float] = [-4.0, -3.0, -2.0, 2.0, 3.0, 4.0, 5.0, 6.0]
_ROTATE_VALUES: list[float] = [-2.0, -1.5, -1.0, 1.0, 1.5, 2.0]
_BRIGHTNESS_VALUES: list[float] = [0.93, 0.95, 0.97, 1.03, 1.05, 1.07]
_CONTRAST_VALUES: list[float] = [0.93, 0.95, 0.97, 1.03, 1.05, 1.07]
_SATURATION_VALUES: list[float] = [0.93, 0.95, 0.97, 1.03, 1.05, 1.07]
_TEMP_VALUES: list[int] = [-7, -5, -3, 3, 5, 7]
_NOISE_VALUES: list[float] = [0.01, 0.015, 0.02, 0.025, 0.03]
_QUALITY_VALUES: list[int] = [86, 88, 90, 91, 93, 94, 95]


# ---------------------------------------------------------------------------
# Вспомогательная функция — максимальный вписанный прямоугольник после поворота
# ---------------------------------------------------------------------------


def _max_inscribed_rect(w: int, h: int, angle_deg: float) -> tuple[int, int]:
    """Размер максимального прямоугольника, вписанного в w×h после поворота на angle_deg.

    Используется для обрезки чёрных угловых полей после rotate без expand.
    Возвращает (crop_w, crop_h) — целые числа, оба не меньше 1.
    """
    angle_rad = math.radians(abs(angle_deg))
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    if cos_a < 1e-9:
        # 90°: вписанный прямоугольник — квадрат со стороной min(w, h)
        side = min(w, h)
        return side, side

    # Формула максимального вписанного прямоугольника с тем же соотношением сторон
    # при повороте изображения w×h на угол a.
    if w <= h:
        new_w = w / (cos_a + (float(h) / w) * sin_a)
    else:
        new_w = w / (cos_a + (float(w) / h) * sin_a)
    new_h = new_w * h / w

    crop_w = max(1, int(new_w))
    crop_h = max(1, int(new_h))
    return crop_w, crop_h


# ---------------------------------------------------------------------------
# Построение набора пресетов
# ---------------------------------------------------------------------------


def build_presets(n: int, seed: Optional[int] = None) -> list[Preset]:
    """Вернуть список из n пресетов.

    Пресет №1 — нейтральный («оригинал (только EXIF)»), все параметры
    дефолтные. Каждый следующий — уникальная комбинация 2–3 семейств,
    гарантированно отличающаяся от предыдущих (попарно различны как кортежи
    параметров). При одинаковом seed результат воспроизводим.

    :param n: количество пресетов (1..10).
    :param seed: инициализация RNG (None → случайный).
    :raises ValueError: если n < 1 или n > 10.
    """
    if n < 1 or n > 10:
        raise ValueError(f"n должно быть в диапазоне 1..10, получено: {n}")

    rng = random.Random(seed)

    presets: list[Preset] = []
    # Нейтральный: только чистка EXIF + пересохранение
    presets.append(Preset(name="оригинал (только EXIF)"))

    if n == 1:
        return presets

    # Ключ уникальности — кортеж числовых параметров (без name)
    seen_keys: set[tuple] = {_preset_key(presets[0])}

    # Гарантия заметности: каждый пресет обязан содержать минимум одно
    # ГЕОМЕТРИЧЕСКОЕ семейство (зум/отдаление или поворот) и минимум одно
    # ТОНАЛЬНОЕ (яркость/контраст/насыщенность/температура); шум и
    # jpeg-качество — только необязательная добавка. Иначе вариант может
    # быть визуально неотличим от оригинала.
    geometry_families = [
        ("zoom",        "zoom_pct",    _ZOOM_VALUES),
        ("rotate",      "rotate_deg",  _ROTATE_VALUES),
    ]
    tone_families = [
        ("brightness",  "brightness",  _BRIGHTNESS_VALUES),
        ("contrast",    "contrast",    _CONTRAST_VALUES),
        ("saturation",  "saturation",  _SATURATION_VALUES),
        ("temp",        "temp_shift",  _TEMP_VALUES),
    ]
    extra_families = [
        ("noise",       "noise_alpha", _NOISE_VALUES),
        ("quality",     "jpeg_quality",_QUALITY_VALUES),
    ]

    max_attempts = 200
    attempts = 0

    while len(presets) < n and attempts < max_attempts:
        attempts += 1

        # 1 геометрическое + 1 тональное (+ иногда 1 добавка) = 2–3 семейства
        chosen = [rng.choice(geometry_families), rng.choice(tone_families)]
        if rng.random() < 0.5:
            chosen.append(rng.choice(extra_families))

        kwargs: dict = {}
        name_parts: list[str] = []

        for fam_name, field_name, values in chosen:
            val = rng.choice(values)
            kwargs[field_name] = val

            # Человекочитаемая часть названия
            if field_name == "zoom_pct":
                if val > 0:
                    name_parts.append(f"зум {int(val)}%")
                else:
                    name_parts.append(f"отдаление {int(abs(val))}%")
            elif field_name == "rotate_deg":
                sign = "+" if val > 0 else ""
                name_parts.append(f"поворот {sign}{val:.1f}°")
            elif field_name == "brightness":
                label = "ярче" if val >= 1.0 else "темнее"
                name_parts.append(f"{label} {abs(val - 1.0) * 100:.0f}%")
            elif field_name == "contrast":
                label = "контраст+" if val >= 1.0 else "контраст-"
                name_parts.append(f"{label}{abs(val - 1.0) * 100:.0f}%")
            elif field_name == "saturation":
                label = "насыщ+" if val >= 1.0 else "насыщ-"
                name_parts.append(f"{label}{abs(val - 1.0) * 100:.0f}%")
            elif field_name == "temp_shift":
                label = "теплее" if val > 0 else "холоднее"
                name_parts.append(f"{label} {abs(val)}")
            elif field_name == "noise_alpha":
                name_parts.append(f"шум {val:.2f}")
            elif field_name == "jpeg_quality":
                name_parts.append(f"jpeg {val}")

        preset_name = " + ".join(name_parts)
        preset = Preset(name=preset_name, **kwargs)
        key = _preset_key(preset)

        if key not in seen_keys:
            seen_keys.add(key)
            presets.append(preset)

    if len(presets) < n:
        logger.warning(
            "build_presets: не удалось набрать %d уникальных пресетов за %d попыток "
            "(получено %d). Используем дубли.",
            n, max_attempts, len(presets),
        )
        # Заполняем дублями при крайнем случае (n > 10 запрещено выше)
        while len(presets) < n:
            presets.append(presets[-1])

    return presets


def _preset_key(p: Preset) -> tuple:
    """Кортеж числовых параметров пресета (без name) — ключ уникальности."""
    return (
        p.zoom_pct,
        p.rotate_deg,
        p.brightness,
        p.contrast,
        p.saturation,
        p.temp_shift,
        p.noise_alpha,
        p.jpeg_quality,
    )


# ---------------------------------------------------------------------------
# Применение пресета к байтам фото
# ---------------------------------------------------------------------------


def apply_preset(image_bytes: bytes, preset: Preset) -> bytes:
    """Применить пресет к фотографии, вернуть байты обработанного изображения.

    Формат выхода = формат входа (JPEG→JPEG, PNG→PNG).
    EXIF чистится у всех форматов.
    При любой ошибке → лог + вернуть оригинальные байты image_bytes.

    :param image_bytes: исходные байты изображения.
    :param preset: набор параметров обработки.
    :return: байты обработанного изображения (или оригинала при ошибке).
    """
    try:
        return _apply_preset_impl(image_bytes, preset)
    except Exception as exc:
        logger.error(
            "apply_preset: ошибка при обработке фото пресетом %r: %s — возвращаем оригинал",
            preset.name,
            exc,
        )
        return image_bytes


def _apply_preset_impl(image_bytes: bytes, preset: Preset) -> bytes:
    """Внутренняя реализация apply_preset (может бросать исключения)."""
    from PIL import Image, ImageEnhance  # импорт внутри — не требуется на уровне модуля

    # Определяем формат из заголовка байтов
    src_buf = io.BytesIO(image_bytes)
    try:
        img = Image.open(src_buf)
        img.load()
    except Exception as exc:
        # HEIC или повреждённые данные — Pillow не открывает
        logger.warning(
            "apply_preset: не удалось открыть изображение (%s) — возвращаем оригинал. "
            "Примечание: HEIC не поддерживается Pillow без плагина.",
            exc,
        )
        return image_bytes

    fmt = (img.format or "JPEG").upper()
    # Нормируем JPEG-алиасы
    if fmt in ("JPG", "JPEG", "MPO"):
        fmt = "JPEG"

    # CMYK → RGB (JPEG может быть CMYK)
    if img.mode == "CMYK":
        logger.debug("apply_preset: конвертируем CMYK → RGB")
        img = img.convert("RGB")
    elif img.mode == "P":
        # Палитровые изображения: ImageEnhance не поддерживает P-mode.
        # P с прозрачностью (transparency) → RGBA, иначе → RGB.
        if "transparency" in img.info:
            logger.debug("apply_preset: конвертируем P (с прозрачностью) → RGBA")
            img = img.convert("RGBA")
        else:
            logger.debug("apply_preset: конвертируем P → RGB")
            img = img.convert("RGB")
    elif img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")

    # --- Чистка EXIF: пересоздание без метаданных ---
    # Используем paste на новый пустой Image, либо просто не передаём exif при сохранении.
    # Для надёжности — пересоздаём через новый Image + paste.
    clean = Image.new(img.mode, img.size)
    clean.paste(img)
    img = clean

    # --- Зум (кроп) или отдаление (уменьшение с подложкой) ---
    if preset.zoom_pct != 0:
        img = _apply_zoom(img, preset.zoom_pct)

    # --- Поворот с обрезкой чёрных углов ---
    if preset.rotate_deg != 0.0:
        img = _apply_rotate(img, preset.rotate_deg)

    # --- Яркость, контраст, насыщенность ---
    if preset.brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(preset.brightness)
    if preset.contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(preset.contrast)
    if preset.saturation != 1.0:
        img = ImageEnhance.Color(img).enhance(preset.saturation)

    # --- Температурный сдвиг (R/B каналы) ---
    if preset.temp_shift != 0:
        img = _apply_temp_shift(img, preset.temp_shift)

    # --- Наложение шума ---
    if preset.noise_alpha > 0.0:
        img = _apply_noise(img, preset.noise_alpha)

    # --- Сохранение в байты ---
    out_buf = io.BytesIO()
    if fmt == "JPEG":
        # PNG с прозрачностью → нельзя сохранить как JPEG
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(out_buf, format="JPEG", quality=preset.jpeg_quality, exif=b"")
    elif fmt == "PNG":
        if img.mode == "P":
            img = img.convert("RGBA")
        img.save(out_buf, format="PNG", optimize=True)
    else:
        # Для прочих форматов — сохраняем как JPEG
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(out_buf, format="JPEG", quality=preset.jpeg_quality, exif=b"")

    return out_buf.getvalue()


# ---------------------------------------------------------------------------
# Вспомогательные функции обработки
# ---------------------------------------------------------------------------


def _apply_zoom(img, zoom_pct: float):
    """zoom_pct > 0 — центральный кроп с увеличением; zoom_pct < 0 — отдаление:
    кадр уменьшается, поля заполняются размытой растяжкой самого фото
    (информации за кадром нет — дорисовываем правдоподобную подложку)."""
    from PIL import ImageFilter

    w, h = img.size
    if zoom_pct > 0:
        # Размер обрезаемого поля с каждой стороны
        dw = int(w * zoom_pct / 100 / 2)
        dh = int(h * zoom_pct / 100 / 2)
        # Гарантируем минимальный размер 1×1; координаты — неотрицательные
        left = max(0, min(dw, w // 2 - 1))
        top = max(0, min(dh, h // 2 - 1))
        cropped = img.crop((left, top, w - left, h - top))
        # Масштабируем обратно до оригинального размера
        return cropped.resize((w, h), resample=3)  # 3 = BICUBIC

    # Отдаление: подложка — само фото, размытое; сверху — уменьшенный кадр
    shrink = 1.0 + zoom_pct / 100  # zoom_pct < 0 → коэффициент < 1
    new_w = max(1, int(w * shrink))
    new_h = max(1, int(h * shrink))
    background = img.resize((w, h), resample=3).filter(
        ImageFilter.GaussianBlur(radius=max(w, h) // 50 + 2)
    )
    small = img.resize((new_w, new_h), resample=3)
    background.paste(small, ((w - new_w) // 2, (h - new_h) // 2))
    return background


def _apply_rotate(img, angle_deg: float):
    """Поворот с обрезкой чёрных угловых полей — результат без expand."""
    w, h = img.size
    # Поворачиваем без expand (чёрные углы заполняются по краям)
    rotated = img.rotate(angle_deg, expand=False, resample=2)  # 2 = BILINEAR

    # Вычисляем максимальный вписанный прямоугольник
    cw, ch = _max_inscribed_rect(w, h, angle_deg)

    # Центральный кроп до вписанного прямоугольника
    cx, cy = w // 2, h // 2
    left = cx - cw // 2
    top = cy - ch // 2
    right = left + cw
    bottom = top + ch

    # Защита от выхода за границы
    left = max(0, left)
    top = max(0, top)
    right = min(w, right)
    bottom = min(h, bottom)

    cropped = rotated.crop((left, top, right, bottom))
    # Масштабируем обратно к оригинальному размеру
    return cropped.resize((w, h), resample=3)  # BICUBIC


def _apply_temp_shift(img, temp_shift: int):
    """Сдвиг температуры: temp_shift > 0 — теплее (R+, B-), < 0 — холоднее (R-, B+)."""
    from PIL import Image

    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    has_alpha = img.mode == "RGBA"
    if has_alpha:
        r, g, b, a = img.split()
    else:
        channels = img.split()
        if len(channels) == 3:
            r, g, b = channels
        else:
            return img  # неизвестный формат — пропускаем

    def _shift_channel(channel, delta: int):
        """Сдвигаем значения канала на delta, с клиппингом 0..255."""
        from PIL import ImageChops, ImageOps
        if delta == 0:
            return channel
        # Используем point() для поканального сдвига
        return channel.point(lambda v: max(0, min(255, v + delta)))

    r = _shift_channel(r, temp_shift)
    b = _shift_channel(b, -temp_shift)

    if has_alpha:
        return Image.merge("RGBA", (r, g, b, a))
    return Image.merge("RGB", (r, g, b))


def _apply_noise(img, noise_alpha: float):
    """Добавляем лёгкий шум через Image.blend + Image.effect_noise (без numpy)."""
    from PIL import Image

    noise = Image.effect_noise(img.size, 12).convert(img.mode)
    return Image.blend(img, noise, noise_alpha)


# ---------------------------------------------------------------------------
# Самотесты (python backend/photo_variation.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import dataclasses
    import sys

    from PIL import Image

    print("Запуск самотестов photo_variation.py ...")

    # ── Тест 1: build_presets(10, seed=1) → 10 пресетов, попарно различных, №1 нейтральный ──

    presets_10 = build_presets(10, seed=1)
    assert len(presets_10) == 10, f"Ожидали 10 пресетов, получили {len(presets_10)}"
    assert presets_10[0].name == "оригинал (только EXIF)", (
        f"Пресет №1 должен быть нейтральным, получили: {presets_10[0].name!r}"
    )
    neutral_key = _preset_key(presets_10[0])
    assert neutral_key == (0.0, 0.0, 1.0, 1.0, 1.0, 0, 0.0, 92), (
        f"Нейтральный пресет имеет неверные параметры: {neutral_key}"
    )
    # Попарная уникальность
    keys_10 = [_preset_key(p) for p in presets_10]
    assert len(set(keys_10)) == 10, (
        f"Пресеты не все уникальны: {len(set(keys_10))} уникальных из 10"
    )
    print("[OK] Тест 1: build_presets(10, seed=1) — 10 уникальных, №1 нейтральный")

    # ── Тест 2: зеркала нет — у Preset нет поля mirror/flip ──────────────────────────────

    field_names = {f.name for f in dataclasses.fields(Preset)}
    forbidden = {"mirror", "flip", "mirror_h", "flip_h", "horizontal_flip"}
    overlap = field_names & forbidden
    assert not overlap, f"Preset содержит запрещённые поля зеркалирования: {overlap}"
    print(f"[OK] Тест 2: Preset не имеет полей зеркалирования (поля: {sorted(field_names)})")

    # ── Тест 3: apply_preset на синтетическом JPEG 100×80 ────────────────────────────────

    def _make_jpeg(w: int, h: int) -> bytes:
        img = Image.new("RGB", (w, h), color=(120, 80, 60))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()

    def _make_png(w: int, h: int) -> bytes:
        img = Image.new("RGB", (w, h), color=(60, 120, 180))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _make_cmyk_jpeg(w: int, h: int) -> bytes:
        img = Image.new("CMYK", (w, h), color=(10, 20, 30, 40))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()

    jpeg_100x80 = _make_jpeg(100, 80)
    for i, p in enumerate(presets_10):
        result = apply_preset(jpeg_100x80, p)
        assert isinstance(result, bytes) and len(result) > 0, (
            f"Пресет {i+1} ({p.name!r}): результат пустой или не bytes"
        )
        out_img = Image.open(io.BytesIO(result))
        out_img.load()
        ow, oh = out_img.size
        assert 0.9 <= ow / 100 <= 1.1 and 0.9 <= oh / 80 <= 1.1, (
            f"Пресет {i+1} ({p.name!r}): размер {ow}×{oh} отличается > 10% от 100×80"
        )
    print("[OK] Тест 3: apply_preset JPEG 100×80 — все 10 пресетов, размер ≤ 10% от оригинала")

    # ── Тест 4: apply_preset на PNG 8×9 (нечётные размеры) ──────────────────────────────

    png_8x9 = _make_png(8, 9)
    for i, p in enumerate(presets_10):
        result = apply_preset(png_8x9, p)
        assert isinstance(result, bytes) and len(result) > 0, (
            f"Пресет {i+1}: PNG 8×9 → пустой результат"
        )
        out_img = Image.open(io.BytesIO(result))
        out_img.load()
        ow, oh = out_img.size
        assert 0.9 <= ow / 8 <= 1.1 and 0.9 <= oh / 9 <= 1.1, (
            f"Пресет {i+1} ({p.name!r}): PNG 8×9 → размер {ow}×{oh}, отклонение > 10%"
        )
    print("[OK] Тест 4: apply_preset PNG 8×9 (нечётные размеры) — все 10 пресетов")

    # ── Тест 5: apply_preset на CMYK JPEG ────────────────────────────────────────────────

    cmyk_jpeg = _make_cmyk_jpeg(60, 60)
    result = apply_preset(cmyk_jpeg, presets_10[1])
    assert isinstance(result, bytes) and len(result) > 0, "CMYK JPEG → пустой результат"
    out_img = Image.open(io.BytesIO(result))
    out_img.load()
    assert out_img.mode in ("RGB", "L"), f"CMYK должен стать RGB, получили {out_img.mode}"
    print(f"[OK] Тест 5: apply_preset CMYK JPEG → RGB ({out_img.mode})")

    # ── Тест 6: мусорные байты b"not an image" → вернулся оригинал ──────────────────────

    garbage = b"not an image"
    result = apply_preset(garbage, presets_10[0])
    assert result == garbage, (
        f"Мусорные байты должны вернуться без изменений, получили: {result[:30]!r}"
    )
    print("[OK] Тест 6: мусорные байты b'not an image' → оригинал без исключений")

    # ── Тест 7: воспроизводимость по seed ────────────────────────────────────────────────

    presets_a = build_presets(5, seed=42)
    presets_b = build_presets(5, seed=42)
    assert [_preset_key(p) for p in presets_a] == [_preset_key(p) for p in presets_b], (
        "Два вызова build_presets с одним seed дали разные результаты"
    )
    presets_c = build_presets(5, seed=99)
    assert [_preset_key(p) for p in presets_a] != [_preset_key(p) for p in presets_c], (
        "Разные seed дали одинаковые пресеты — RNG не работает"
    )
    print("[OK] Тест 7: воспроизводимость по seed — одинаковые seed равны, разные seed отличаются")

    # ── Тест 8: build_presets для разных n ───────────────────────────────────────────────

    for n_test in (1, 2, 5, 10):
        ps = build_presets(n_test, seed=7)
        assert len(ps) == n_test, f"build_presets({n_test}) вернул {len(ps)} пресетов"
        assert ps[0].name == "оригинал (только EXIF)", (
            f"build_presets({n_test}): пресет №1 не нейтральный"
        )
        keys_n = [_preset_key(p) for p in ps]
        assert len(set(keys_n)) == n_test, (
            f"build_presets({n_test}): не все пресеты уникальны ({len(set(keys_n))} из {n_test})"
        )
    print("[OK] Тест 8: build_presets для n=1,2,5,10 — верный размер и уникальность")

    # ── Тест 9: результат JPEG открывается Pillow и является валидным JPEG ────────────────

    jpeg_bytes = _make_jpeg(200, 150)
    result = apply_preset(jpeg_bytes, presets_10[3])
    out_img = Image.open(io.BytesIO(result))
    assert out_img.format == "JPEG", f"Ожидали JPEG, получили {out_img.format}"
    print(f"[OK] Тест 9: результат обработки JPEG → валидный JPEG ({out_img.size})")

    # ── Тест 10: результат PNG открывается Pillow и является валидным PNG ────────────────

    png_bytes = _make_png(50, 60)
    result = apply_preset(png_bytes, presets_10[2])
    out_img = Image.open(io.BytesIO(result))
    assert out_img.format == "PNG", f"Ожидали PNG, получили {out_img.format}"
    print(f"[OK] Тест 10: результат обработки PNG → валидный PNG ({out_img.size})")

    # ── Тест 11: палитровый (P-mode) PNG — обработка применяется, формат PNG сохранён ──────

    def _make_palette_png(w: int, h: int) -> bytes:
        """Создаём RGB-изображение с градиентом, конвертируем в P, сохраняем как PNG."""
        base = Image.new("RGB", (w, h))
        pixels = base.load()
        for x in range(w):
            for y in range(h):
                pixels[x, y] = (x * 255 // max(w - 1, 1), y * 255 // max(h - 1, 1), 128)
        palette_img = base.convert("P")
        buf = io.BytesIO()
        palette_img.save(buf, format="PNG")
        return buf.getvalue()

    # Проверяем, что входной файл действительно P-mode
    palette_png_bytes = _make_palette_png(60, 40)
    _check_mode = Image.open(io.BytesIO(palette_png_bytes))
    assert _check_mode.mode == "P", (
        f"Тест 11: входной PNG должен быть P-mode, получили {_check_mode.mode!r}"
    )

    presets_5 = build_presets(5, seed=3)
    neutral_preset = presets_5[0]
    non_neutral_presets = [p for p in presets_5[1:] if _preset_key(p) != _preset_key(neutral_preset)]

    # Нейтральный пресет — просто пересохранение, байты могут отличаться от P-PNG
    neutral_result = apply_preset(palette_png_bytes, neutral_preset)
    assert isinstance(neutral_result, bytes) and len(neutral_result) > 0, (
        "Тест 11: нейтральный пресет на P-PNG → пустой результат"
    )
    out_neutral = Image.open(io.BytesIO(neutral_result))
    out_neutral.load()
    assert out_neutral.format == "PNG", (
        f"Тест 11: нейтральный пресет должен вернуть PNG, получили {out_neutral.format}"
    )

    # Проверяем пресет с brightness — это именно тот случай, где ImageEnhance
    # упадёт с "image has wrong mode" при P-mode изображении без конверсии.
    # Создаём такой пресет явно.
    brightness_preset = Preset(name="тест-ярче", brightness=1.05)
    result_bright = apply_preset(palette_png_bytes, brightness_preset)
    assert isinstance(result_bright, bytes) and len(result_bright) > 0, (
        "Тест 11: brightness-пресет на P-PNG → пустой результат"
    )
    out_bright = Image.open(io.BytesIO(result_bright))
    out_bright.load()
    assert out_bright.format == "PNG", (
        f"Тест 11: brightness-пресет → ожидали PNG, получили {out_bright.format}"
    )
    # Если обработка не применилась (P-mode не конвертировали), apply_preset вернёт
    # оригинальные байты palette_png_bytes без изменений.
    # Правильная обработка должна изменить пиксели (яркость +5%), байты будут другими.
    assert result_bright != palette_png_bytes, (
        "Тест 11: brightness-пресет на P-PNG вернул оригинал — обработка не применилась "
        "(вероятно, ImageEnhance упал с 'image has wrong mode', фикс не внесён)"
    )

    # Все пресеты на P-PNG не должны падать и должны возвращать PNG
    for i, p in enumerate(presets_5):
        result = apply_preset(palette_png_bytes, p)
        assert isinstance(result, bytes) and len(result) > 0, (
            f"Тест 11: пресет {p.name!r} на P-PNG → пустой результат"
        )
        out_img = Image.open(io.BytesIO(result))
        out_img.load()
        assert out_img.format == "PNG", (
            f"Тест 11: пресет {p.name!r} → ожидали PNG, получили {out_img.format}"
        )

    print("[OK] Тест 11: палитровый (P-mode) PNG — обработка применяется, формат PNG сохранён")

    # ── Тест 12: JPEG 1×1 — не падает, размер результата ≥ 1×1 ──────────────────────────

    def _make_jpeg_1x1() -> bytes:
        img = Image.new("RGB", (1, 1), color=(200, 100, 50))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()

    jpeg_1x1 = _make_jpeg_1x1()
    presets_all = build_presets(5, seed=3)
    for i, p in enumerate(presets_all):
        result = apply_preset(jpeg_1x1, p)
        assert isinstance(result, bytes) and len(result) > 0, (
            f"Тест 12: пресет {p.name!r} на JPEG 1×1 → пустой результат"
        )
        out_img = Image.open(io.BytesIO(result))
        out_img.load()
        ow, oh = out_img.size
        assert ow >= 1 and oh >= 1, (
            f"Тест 12: пресет {p.name!r} → размер {ow}×{oh} < 1×1"
        )
    print("[OK] Тест 12: JPEG 1×1 — не падает, размер ≥ 1×1 для всех пресетов")

    # ── Тест 13: каждый пресет 2..N содержит геометрию И тон ─────────────────
    for _seed in (1, 7, 42, 99):
        _ps = build_presets(10, seed=_seed)
        for _i, _p in enumerate(_ps[1:], start=2):
            _has_geometry = _p.zoom_pct != 0.0 or _p.rotate_deg != 0.0
            _has_tone = (
                _p.brightness != 1.0 or _p.contrast != 1.0
                or _p.saturation != 1.0 or _p.temp_shift != 0
            )
            assert _has_geometry, (
                f"seed={_seed}, пресет {_i} ({_p.name!r}): нет геометрии "
                f"(зум/отдаление/поворот)"
            )
            assert _has_tone, (
                f"seed={_seed}, пресет {_i} ({_p.name!r}): нет тона "
                f"(яркость/контраст/насыщенность/температура)"
            )
    print("[OK] Тест 13: пресеты 2..10 всегда содержат геометрию + тон (4 seed)")

    # ── Тест 14: отдаление (zoom_pct < 0) — размер сохранён, байты изменены ──
    def _make_gradient_jpeg(w: int, h: int) -> bytes:
        img = Image.new("RGB", (w, h))
        px = img.load()
        for x in range(w):
            for y in range(h):
                px[x, y] = (x * 255 // max(w - 1, 1), y * 255 // max(h - 1, 1), 90)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()

    _grad = _make_gradient_jpeg(120, 90)
    _zoom_out = Preset(name="тест-отдаление", zoom_pct=-4.0)
    _res = apply_preset(_grad, _zoom_out)
    assert _res != _grad, "Отдаление: байты не изменились — операция не применилась"
    _out = Image.open(io.BytesIO(_res))
    _out.load()
    assert _out.size == (120, 90), f"Отдаление: размер {_out.size}, ожидали (120, 90)"
    print("[OK] Тест 14: отдаление -4% — размер сохранён, изображение изменено")

    print("\nВсе самотесты пройдены успешно.")
    sys.exit(0)
