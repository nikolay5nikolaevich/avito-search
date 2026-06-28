"""Локальный движок вариантов объявлений Авито (ТЗ §17.3).

Из одного названия/описания делает N вариантов БЕЗ сети и БЕЗ ИИ.
Описание НЕ перефразируется: вариант №1 — дословный оригинал (без артикула),
варианты 2..N — оригинальное описание + уникальный артикул в самом конце.
Заголовок вариируется добавками фактов к концу (size/condition/brand).

Инварианты:
- Вариант №1 — нетронутый оригинал (title и description без изменений, без артикула).
- Тот же seed → побайтно тот же результат; перегенерация = новый seed.
- Авито не требует уникальных заголовков → при нехватке безопасных кандидатов
  заголовок может повторять оригинал (повтор лучше, чем нонсенс).

Самотесты: python backend/text_variation.py
"""

import logging
import random
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Лимит длины названия на Авито.
TITLE_MAX_LEN = 50


@dataclass(frozen=True)
class TextVariant:
    """Один вариант текста объявления."""

    title: str
    description: str
    notes: str = ""  # например: "вариант 1 — оригинал" / "вариант 2: артикул арт.55453"


def _make_article(rng: random.Random, used: set[str], max_attempts: int = 50) -> str:
    """Генерирует уникальный артикул вида «арт.NNNNN» (4–6 цифр без ведущих нулей).

    При коллизии (крайне маловероятной при N<=10 и диапазоне 1000..999999)
    пересэмплирует до max_attempts раз. При исчерпании возвращает самый последний
    вариант — коллизия в таком масштабе практически невозможна.
    """
    for _ in range(max_attempts):
        article = f"арт.{rng.randint(1000, 999999)}"
        if article not in used:
            return article
    # Расширенный диапазон в крайнем случае.
    return f"арт.{rng.randint(1000000, 9999999)}"


def _clip_title(title: str) -> str:
    """Обрезает название до TITLE_MAX_LEN по границе слова, чистит хвостовую пунктуацию."""
    title = " ".join(title.split())
    if len(title) <= TITLE_MAX_LEN:
        return title
    cut = title[: TITLE_MAX_LEN + 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    else:
        cut = title[:TITLE_MAX_LEN]
    return cut.rstrip(" ,.—-•")


def _title_candidates(title: str, facts: dict[str, str]) -> list[str]:
    """Кандидаты названия: оригинальный заголовок как база + безопасные добавки фактов
    к КОНЦУ. Порядок слов неприкосновенен. Оригинал в список не включается — его кладёт
    вызывающий код как базу первого варианта."""
    base = " ".join(title.split())

    size = facts.get("size", "")
    brand = facts.get("brand", "")
    condition = facts.get("condition", "")
    # Добавки в конец — только тех фактов, которых ещё нет в заголовке.
    suffixes: list[str] = []
    if size and size.lower() not in title.lower():
        suffixes.append(f", {size}")
    if brand and brand.lower() not in title.lower():
        suffixes.append(f", {brand}")
    if condition and condition.lower() not in title.lower():
        suffixes.append(f", состояние {condition.lower()}")
    # Комбинация размер+состояние — отдельным кандидатом для большего разнообразия.
    if size and condition and size.lower() not in title.lower() \
            and condition.lower() not in title.lower():
        suffixes.append(f", {size}, состояние {condition.lower()}")

    candidates: list[str] = []
    for s in suffixes:
        c = (base + s).strip()
        if c and c != title and c not in candidates:
            candidates.append(c)
    return candidates


def _build_title(title: str, rng: random.Random, facts: dict[str, str], used: set[str]) -> str:
    """Название варианта: оригинал + безопасные добавки фактов к концу.

    Сначала пытаемся выдать ещё не использованный безопасный кандидат. Если все
    кандидаты исчерпаны (фактов нет / уже добавлены), РАЗРЕШАЕМ повтор исходного
    заголовка: Авито не требует уникальных названий, а повтор корректнее, чем
    разрыв словосочетания или добивка мусором.
    """
    candidates = _title_candidates(title, facts)
    rng.shuffle(candidates)
    for cand in candidates:
        # Кандидат с добавкой факта берём ТОЛЬКО если он целиком влезает в лимит:
        # обрезка посреди добавки дала бы «висящий» факт («…, состояние» без значения).
        if len(cand) <= TITLE_MAX_LEN and cand not in used:
            return cand
    # Безопасных уникальных кандидатов не осталось — повторяем оригинал (обрезанный).
    return _clip_title(title) or title.strip()[:TITLE_MAX_LEN]


def vary_listing(
    title: str,
    description: str,
    n: int,
    *,
    seed: int | None = None,
    facts: dict[str, str] | None = None,
) -> list[TextVariant]:
    """Делает n вариантов текста объявления; вариант №1 — нетронутый оригинал.

    Описание НЕ перефразируется. Варианты 2..N получают уникальный артикул в конце
    описания (отделён пустой строкой). Заголовок варьируется добавками фактов.

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

    # Вариант №1 — нетронутый оригинал, без артикула.
    variants = [TextVariant(title=title, description=description, notes="вариант 1 — оригинал")]
    used_titles: set[str] = {title}
    used_articles: set[str] = set()

    for i in range(1, n):
        # Заголовок: вариация фактами.
        title_rng = random.Random(f"{seed}:title:{i}")
        variant_title = _build_title(title, title_rng, clean_facts, used_titles)
        used_titles.add(variant_title)

        # Описание: оригинал + уникальный артикул в конце.
        article_rng = random.Random(f"{seed}:article:{i}")
        article = _make_article(article_rng, used_articles)
        used_articles.add(article)
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

    # ── Тест 1: базовый n=5 — все непустые, вариант №1 — нетронутый оригинал ──
    vs = vary_listing(base_title, base, 5, seed=42, facts=base_facts)
    assert len(vs) == 5, f"Ожидалось 5 вариантов, получено {len(vs)}"
    assert all(v.title.strip() and v.description.strip() for v in vs), "Есть пустой вариант"
    assert vs[0].title == base_title, f"Вариант 1: заголовок изменён: {vs[0].title!r}"
    assert vs[0].description == base, f"Вариант 1: описание изменён: {vs[0].description!r}"
    assert "оригинал" in vs[0].notes, f"notes оригинала: {vs[0].notes!r}"
    print("[OK] Тест 1: n=5 — все непустые, вариант 1 — нетронутый оригинал")

    # ── Тест 2: формат артикула и его отсутствие у варианта №1 ────────────────
    _ARTICLE_RE = _re.compile(r"арт\.\d{4,6}$")
    assert not _ARTICLE_RE.search(vs[0].description), \
        f"Вариант 1 содержит артикул: {vs[0].description!r}"
    for idx in range(1, 5):
        desc = vs[idx].description
        assert _ARTICLE_RE.search(desc), \
            f"Вариант {idx + 1}: артикул не найден или не соответствует формату:\n{desc!r}"
    print("[OK] Тест 2: формат арт.<4-6 цифр> в конце вариантов 2..N; у №1 артикула нет")

    # ── Тест 3: тело описания не тронуто (без последней строки-артикула) ───────
    for idx in range(1, 5):
        desc = vs[idx].description
        # Убираем последнюю строку артикула и хвостовую пустую строку.
        body = desc.rsplit("\n\n", 1)[0]
        assert body == base.strip(), \
            f"Вариант {idx + 1}: тело описания изменено:\n{body!r}\nожидалось:\n{base.strip()!r}"
    print("[OK] Тест 3: тело описания дословно совпадает с оригиналом во всех вариантах 2..N")

    # ── Тест 4: уникальность артикулов в пределах партии ────────────────────
    articles = []
    for v in vs[1:]:
        m = _ARTICLE_RE.search(v.description)
        assert m, f"Артикул не найден: {v.description!r}"
        articles.append(m.group(0))
    assert len(articles) == len(set(articles)), f"Артикулы не уникальны: {articles}"
    print("[OK] Тест 4: артикулы вариантов 2..N попарно различны")

    # ── Тест 5: воспроизводимость seed; другой seed — другой набор артикулов ──
    again = vary_listing(base_title, base, 5, seed=42, facts=base_facts)
    assert again == vs, "Тот же seed дал другой результат"
    other = vary_listing(base_title, base, 5, seed=43, facts=base_facts)
    other_articles = [_ARTICLE_RE.search(v.description).group(0)
                      for v in other[1:] if _ARTICLE_RE.search(v.description)]
    assert other_articles != articles, "Другой seed дал тот же набор артикулов"
    print("[OK] Тест 5: тот же seed воспроизводим; другой seed — другой набор артикулов")

    # ── Тест 6: заголовки — вариация фактами, длина <=50 ─────────────────────
    assert vs[0].title == base_title, "Вариант 1 — не исходный заголовок"
    base_title_words = _word_set(base_title)
    facts_words: set[str] = set()
    for _fv in base_facts.values():
        facts_words |= _word_set(_fv)
    for v in vs:
        assert len(v.title) <= 50, f"Название длиннее 50: {v.title!r}"
        # Каждое слово заголовка — либо из базы, либо из фактов (без выдумок).
        for w in _word_set(v.title):
            assert w in base_title_words or w in facts_words or w == "состояние", \
                f"Лишнее слово в заголовке: {w!r} в {v.title!r}"
    print("[OK] Тест 6: заголовки <=50, слова только из оригинала и фактов")

    # ── Тест 7: n=1 — только оригинал; n=0 — ValueError; seed=None работает ──
    only = vary_listing(base_title, base, 1, seed=7, facts=base_facts)
    assert len(only) == 1 and only[0].title == base_title and only[0].description == base
    try:
        vary_listing(base_title, base, 0, seed=1)
        raise AssertionError("n=0 должно вызывать ValueError")
    except ValueError:
        pass
    assert len(vary_listing(base_title, base, 2)) == 2, "seed=None должен работать"
    print("[OK] Тест 7: n=1 — оригинал; n=0 — ValueError; seed=None работает")

    # ── Тест 8: многострочное описание — артикул один раз в самом конце ───────
    multi = ("Стильный комплект из фактурной жатой ткани.\n\n"
             "Свободная рубашка и прямые брюки.\n\nЦвет: белый.")
    vm = vary_listing("Летний костюм", multi, 4, seed=2024, facts={"size": "48"})
    assert vm[0].description == multi, "Вариант 1: многострочное описание изменено"
    for idx in range(1, 4):
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
