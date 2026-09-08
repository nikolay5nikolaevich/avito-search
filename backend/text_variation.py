"""Локальный движок вариантов объявлений Авито (ТЗ §17.3).

Из одного названия/описания делает N вариантов БЕЗ сети и БЕЗ ИИ.
Описание НЕ перефразируется: каждый вариант получает исходное описание +
уникальный артикул в самом конце. В заголовок артикул не попадает: название
варьируется фактами и нейтральными словесными добавками.

Инварианты:
- Все варианты, включая №1, отличаются от исходника.
- Тот же seed → побайтно тот же результат; перегенерация = новый seed.
- Названия всех вариантов в одном пакете различаются.

Самотесты: python backend/text_variation.py
"""

import logging
import random
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Лимит длины названия на Авито.
TITLE_MAX_LEN = 50

# Нейтральные словесные окончания позволяют подготовить до 20 разных названий
# без артикулов и порядковых номеров. Запас нужен для перегенерации одного
# варианта без совпадения с остальными названиями уже готового пакета.
_TITLE_SUFFIXES: tuple[str, ...] = (
    "в наличии",
    "на каждый день",
    "для повседневной носки",
    "для стильного образа",
    "для базового гардероба",
    "для разных образов",
    "для современного образа",
    "для повседневного образа",
    "для универсального образа",
    "актуальная модель",
    "универсальная модель",
    "современная модель",
    "лаконичная модель",
    "комфортная модель",
    "практичная модель",
    "стильная модель",
    "выразительная модель",
    "городская модель",
    "базовая модель",
    "модная модель",
    "для удобной носки",
    "для комфортной носки",
    "для сезонного гардероба",
    "для современного гардероба",
    "для повседневного гардероба",
    "для лаконичного образа",
    "для выразительного образа",
    "для городского образа",
    "для модного образа",
    "для удобного образа",
    "практичный вариант",
    "универсальный вариант",
    "современный вариант",
    "стильный вариант",
    "комфортный вариант",
    "лаконичный вариант",
    "выразительный вариант",
    "городской вариант",
    "базовый вариант",
    "повседневный вариант",
)

_ARTICLE_TOKEN_RE = re.compile(
    r"\b(?:арт(?:икул)?|sku|код)\b\.?\s*(?:№|#|:|-)?\s*[\w./-]+",
    re.IGNORECASE,
)
_LONG_TRACKING_NUMBER_RE = re.compile(r"(?<!\w)\d{4,}(?!\w)")


@dataclass(frozen=True)
class TextVariant:
    """Один вариант текста объявления."""

    title: str
    description: str
    notes: str = ""  # например: "вариант 1: артикул арт.55453"


def _make_article(rng: random.Random, used: set[str], max_attempts: int = 50) -> str:
    """Генерирует уникальный артикул вида «арт.NNNNN» (4–6 цифр без ведущих нулей).

    При коллизии (крайне маловероятной при N<=20 и диапазоне 1000..999999)
    пересэмплирует до max_attempts раз. При исчерпании возвращает самый последний
    вариант — коллизия в таком масштабе практически невозможна.
    """
    for _ in range(max_attempts):
        article = f"арт.{rng.randint(1000, 999999)}"
        if article not in used:
            return article
    # Расширенный диапазон в крайнем случае.
    return f"арт.{rng.randint(1000000, 9999999)}"


def _clip_title(title: str, max_len: int = TITLE_MAX_LEN) -> str:
    """Обрезает название до max_len по границе слова, чистит хвостовую пунктуацию."""
    title = " ".join(title.split())
    if len(title) <= max_len:
        return title
    cut = title[: max_len + 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    else:
        cut = title[:max_len]
    return cut.rstrip(" ,.—-•")


def normalize_title_key(title: str) -> str:
    """Регистр, пробелы и одна лишь пунктуация не различают названия."""
    return " ".join(re.findall(r"\w+", str(title).casefold()))


def _clean_base_title(title: str) -> str:
    """Убирает явные артикулы и длинные трекинговые номера из базы названия."""
    cleaned = _ARTICLE_TOKEN_RE.sub(" ", " ".join(str(title).split()))
    cleaned = _LONG_TRACKING_NUMBER_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
    cleaned = re.sub(r"(?:\s*[,.;:—-]\s*)+$", "", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" ,.;:—-")
    return cleaned or "Товар"


def _title_with_suffix(title: str, suffix: str) -> str:
    """Добавляет словесное окончание, сохраняя его целиком в лимите Авито."""
    tail = f", {suffix.strip(' ,')}"
    base = _clip_title(title, TITLE_MAX_LEN - len(tail))
    return f"{base}{tail}"


def _title_candidates(title: str, facts: dict[str, str]) -> list[str]:
    """Кандидаты названия: оригинальный заголовок как база + безопасные добавки фактов
    к КОНЦУ. Порядок слов неприкосновенен. Оригинал в список не включается."""
    base = _clean_base_title(title)

    size = facts.get("size", "")
    brand = facts.get("brand", "")
    condition = facts.get("condition", "")
    # Добавки в конец — только тех фактов, которых ещё нет в заголовке.
    suffixes: list[str] = []
    if size and size.casefold() not in base.casefold():
        suffixes.append(size)
    if brand and brand.casefold() not in base.casefold():
        suffixes.append(brand)
    if condition and condition.casefold() not in base.casefold():
        suffixes.append(f"состояние {condition.lower()}")
    # Комбинация размер+состояние — отдельным кандидатом для большего разнообразия.
    if size and condition and size.casefold() not in base.casefold() \
            and condition.casefold() not in base.casefold():
        suffixes.append(f"{size}, состояние {condition.lower()}")

    suffixes.extend(_TITLE_SUFFIXES)

    candidates: list[str] = []
    for s in suffixes:
        c = _title_with_suffix(base, s)
        if c and normalize_title_key(c) != normalize_title_key(title) and c not in candidates:
            candidates.append(c)
    return candidates


def _build_title(
    title: str,
    rng: random.Random,
    facts: dict[str, str],
    used: set[str],
) -> str:
    """Название варианта: оригинал + безопасные добавки фактов к концу.

    Выдаёт ещё не использованный словесный кандидат. Артикулы и номера в
    заголовок не добавляются.
    """
    candidates = _title_candidates(title, facts)
    rng.shuffle(candidates)
    for cand in candidates:
        # Кандидат с добавкой факта берём ТОЛЬКО если он целиком влезает в лимит:
        # обрезка посреди добавки дала бы «висящий» факт («…, состояние» без значения).
        key = normalize_title_key(cand)
        if len(cand) <= TITLE_MAX_LEN and key not in used:
            return cand
    raise ValueError("Не удалось построить уникальные словесные названия пакета")


def vary_listing(
    title: str,
    description: str,
    n: int,
    *,
    seed: int | None = None,
    facts: dict[str, str] | None = None,
    reserved_titles: set[str] | None = None,
) -> list[TextVariant]:
    """Делает n изменённых вариантов текста объявления.

    Описание НЕ перефразируется. Каждый вариант получает уникальный артикул в конце
    описания (отделён пустой строкой). Заголовок варьируется только словами и
    фактами; reserved_titles исключаются при перегенерации отдельной карточки.

    Тот же seed → побайтно тот же результат; facts (ключи "size", "condition",
    "brand") идут только в добавки названия.
    """
    if n < 1:
        raise ValueError(f"Число вариантов должно быть >= 1, получено {n}")
    clean_facts = {
        k: v.strip() for k, v in (facts or {}).items() if isinstance(v, str) and v.strip()
    }
    if seed is None:
        seed = random.randrange(2**32)

    variants: list[TextVariant] = []
    used_titles = {
        normalize_title_key(value)
        for value in (reserved_titles or set())
        if normalize_title_key(value)
    }
    used_articles: set[str] = set()

    for i in range(n):
        # Один артикул используется и в названии-fallback, и в описании.
        article_rng = random.Random(f"{seed}:article:{i}")
        article = _make_article(article_rng, used_articles)
        used_articles.add(article)

        # Заголовок: вариация фактами.
        title_rng = random.Random(f"{seed}:title:{i}")
        variant_title = _build_title(title, title_rng, clean_facts, used_titles)
        used_titles.add(normalize_title_key(variant_title))

        # Описание: оригинал + уникальный артикул в конце.
        variant_description = description.strip() + "\n\n" + article

        variants.append(
            TextVariant(
                title=variant_title,
                description=variant_description,
                notes=f"вариант {i + 1}: артикул {article}",
            )
        )

    logger.info("Сгенерировано %d вариантов текста объявления (seed=%d)", n, seed)
    return variants


if __name__ == "__main__":
    import re as _re

    base_title = "Пиджак Hugo Boss"
    base = ("Продаю мужской пиджак Hugo Boss. Отличное состояние, без дефектов.\n\n"
            "Размер 48. Торг.")
    base_facts = {"size": "48 (M)", "brand": "Hugo Boss"}

    # Вспомогательное: множество слов строки.
    def _word_set(s: str) -> set[str]:
        return set(_re.findall(r"\w+", s.lower()))

    # ── Тест 1: базовый n=5 — все варианты изменены ──────────────────────────
    vs = vary_listing(base_title, base, 5, seed=42, facts=base_facts)
    assert len(vs) == 5, f"Ожидалось 5 вариантов, получено {len(vs)}"
    assert all(v.title.strip() and v.description.strip() for v in vs), "Есть пустой вариант"
    assert all(v.title != base_title for v in vs), "Один из заголовков не изменён"
    assert all(v.description != base for v in vs), "Одно из описаний не изменено"
    assert all("артикул" in v.notes for v in vs), "Не у всех вариантов есть артикул"
    print("[OK] Тест 1: n=5 — все варианты непустые и изменены")

    # ── Тест 2: формат артикула у каждого варианта ───────────────────────────
    _ARTICLE_RE = _re.compile(r"арт\.\d{4,6}$")
    for idx in range(5):
        desc = vs[idx].description
        assert _ARTICLE_RE.search(desc), \
            f"Вариант {idx + 1}: артикул не найден или не соответствует формату:\n{desc!r}"
    print("[OK] Тест 2: формат арт.<4-6 цифр> в конце каждого варианта")

    # ── Тест 3: тело описания не тронуто (без последней строки-артикула) ───────
    for idx in range(5):
        desc = vs[idx].description
        # Убираем последнюю строку артикула и хвостовую пустую строку.
        body = desc.rsplit("\n\n", 1)[0]
        assert body == base.strip(), \
            f"Вариант {idx + 1}: тело описания изменено:\n{body!r}\nожидалось:\n{base.strip()!r}"
    print("[OK] Тест 3: тело описания дословно совпадает с исходником во всех вариантах")

    # ── Тест 4: уникальность артикулов в пределах партии ────────────────────
    articles = []
    for v in vs:
        m = _ARTICLE_RE.search(v.description)
        assert m, f"Артикул не найден: {v.description!r}"
        articles.append(m.group(0))
    assert len(articles) == len(set(articles)), f"Артикулы не уникальны: {articles}"
    print("[OK] Тест 4: артикулы всех вариантов попарно различны")

    # ── Тест 5: воспроизводимость seed; другой seed — другой набор артикулов ──
    again = vary_listing(base_title, base, 5, seed=42, facts=base_facts)
    assert again == vs, "Тот же seed дал другой результат"
    other = vary_listing(base_title, base, 5, seed=43, facts=base_facts)
    other_articles = [_ARTICLE_RE.search(v.description).group(0)
                      for v in other if _ARTICLE_RE.search(v.description)]
    assert other_articles != articles, "Другой seed дал тот же набор артикулов"
    print("[OK] Тест 5: тот же seed воспроизводим; другой seed — другой набор артикулов")

    # ── Тест 6: заголовки уникальны, без артикулов и трекинговых номеров ──────
    assert all(v.title != base_title for v in vs), "Один из вариантов сохранил исходный заголовок"
    assert len({v.title.casefold() for v in vs}) == len(vs), \
        f"Названия вариантов повторяются: {[v.title for v in vs]}"
    for v in vs:
        assert len(v.title) <= 50, f"Название длиннее 50: {v.title!r}"
        assert not _ARTICLE_RE.search(v.title), f"Артикул попал в название: {v.title!r}"
        assert not _LONG_TRACKING_NUMBER_RE.search(v.title), (
            f"Трекинговый номер попал в название: {v.title!r}"
        )
    print("[OK] Тест 6: заголовки уникальны, <=50, без артикулов и длинных номеров")

    # ── Тест 7: n=1 — изменённый вариант; n=0 — ValueError; seed=None работает
    only = vary_listing(base_title, base, 1, seed=7, facts=base_facts)
    assert len(only) == 1 and only[0].title != base_title and only[0].description != base
    try:
        vary_listing(base_title, base, 0, seed=1)
        raise AssertionError("n=0 должно вызывать ValueError")
    except ValueError:
        pass
    assert len(vary_listing(base_title, base, 2)) == 2, "seed=None должен работать"
    print("[OK] Тест 7: n=1 — изменённый вариант; n=0 — ValueError; seed=None работает")

    # ── Тест 8: многострочное описание — артикул один раз в самом конце ───────
    multi = ("Стильный комплект из фактурной жатой ткани.\n\n"
             "Свободная рубашка и прямые брюки.\n\nЦвет: белый.")
    vm = vary_listing("Летний костюм", multi, 4, seed=2024, facts={"size": "48"})
    for idx in range(4):
        desc = vm[idx].description
        # Ровно одна строка артикула, в самом конце.
        all_matches = list(_ARTICLE_RE.finditer(desc))
        assert len(all_matches) == 1, \
            f"Вариант {idx + 1}: артикул встречается {len(all_matches)} раз"
        # Исходные абзацы сохранены дословно и в исходном порядке.
        body = desc.rsplit("\n\n", 1)[0]
        assert body == multi.strip(), \
            f"Вариант {idx + 1}: абзацы изменены:\n{body!r}"
    print("[OK] Тест 8: многострочное описание — абзацы дословны, артикул один раз в конце")

    print("[OK] text_variation: все самотесты пройдены")
