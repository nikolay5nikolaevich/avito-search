"""Локальный комбинаторный движок перефраза объявлений Авито (ТЗ §17.3).

Из одного названия/описания делает N различающихся вариантов БЕЗ сети и БЕЗ ИИ.
Четыре независимые «ручки» описания, комбинируемые от seed:
1) перестановка предложений (первое — смысловое ядро — фиксировано;
   абзацы через пустую строку — границы, через которые предложения не переставляются);
2) замена лексики объявлений по словарю синонимов (SYNONYMS, целые слова/фразы);
3) нейтральные шапки/подвалы (HEADERS/FOOTERS, плейсхолдеры {brand}/{size} из facts);
4) оформление: маркеры «—»/«•», переносы строк, порядок блоков.

Факты сохраняются «по построению»: движок НЕ выдумывает новые слова — только
переставляет предложения, заменяет по словарю и добавляет шаблонные фразы.
Вариант №1 — всегда нетронутый оригинал. Тот же seed → побайтно тот же результат;
перегенерация = новый seed. Контроль непохожести — Jaccard по 3-граммам слов.

Самотесты: python backend/text_variation.py
"""

import logging
import random
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Порог похожести: пары описаний с Jaccard по 3-граммам выше порога пересобираются.
SIMILARITY_MAX = 0.85
# Сколько раз пересобирать вариант с другим внутренним seed до форс-режима.
REBUILD_ATTEMPTS = 5
# Сколько комбинаций «шапка + подвал» перебирать в форс-режиме.
_FORCE_ATTEMPTS = 20
# Лимит длины названия на Авито.
TITLE_MAX_LEN = 50
# Вероятность замены найденного синонима (не 1.0 — чтобы варианты различались набором замен).
_SYNONYM_PROB = 0.85

# Разбиение на предложения: по [.!?…] с сохранением знака; абзацы (\n\n) — границы,
# через которые предложения не переставляются.
_SENT_RE = re.compile(r"[^.!?…\n]+[.!?…]?")

# Словарь синонимов лексики объявлений. Только нейтральные перефразы, ничего не
# выдумывающие про товар; замена — целых слов/фраз, регистр по первому символу.
SYNONYMS: dict[str, list[str]] = {
    # продажа и покупка
    "продаю": ["продам", "продаётся"],
    "продам": ["продаю", "продаётся"],
    "продаётся": ["продаю", "продам"],
    "куплен": ["приобретён"],
    "куплена": ["приобретена"],
    "куплено": ["приобретено"],
    "покупал": ["приобретал", "брал"],
    "покупала": ["приобретала", "брала"],
    "срочно": ["очень срочно"],
    # состояние вещи
    "отличное состояние": ["прекрасное состояние", "идеальное состояние"],
    "идеальное состояние": ["отличное состояние", "прекрасное состояние"],
    "хорошее состояние": ["достойное состояние"],
    "состояние отличное": ["состояние прекрасное", "состояние идеальное"],
    "состояние хорошее": ["состояние достойное"],
    "в отличном состоянии": ["в прекрасном состоянии", "в идеальном состоянии"],
    "в хорошем состоянии": ["в достойном состоянии"],
    "как новый": ["практически новый"],
    "как новая": ["практически новая"],
    "почти новый": ["практически новый"],
    "почти новая": ["практически новая"],
    "новый": ["абсолютно новый", "совершенно новый"],
    "новая": ["абсолютно новая", "совершенно новая"],
    "новое": ["абсолютно новое", "совершенно новое"],
    "без дефектов": ["без изъянов", "дефектов нет"],
    "без недостатков": ["без изъянов"],
    "дефектов нет": ["без дефектов", "изъянов нет"],
    "мне не подошёл": ["не подошёл мне"],
    "мне не подошла": ["не подошла мне"],
    "не подошёл": ["мне не подошёл"],
    "не подошла": ["мне не подошла"],
    "носил": ["надевал"],
    "носила": ["надевала"],
    "надевался": ["носился"],
    "надевалась": ["носилась"],
    # качество и впечатление
    "качественный": ["добротный"],
    "качественная": ["добротная"],
    "красивый": ["симпатичный"],
    "красивая": ["симпатичная"],
    "стильный": ["модный"],
    "стильная": ["модная"],
    "удобный": ["комфортный"],
    "удобная": ["комфортная"],
    "удобно": ["комфортно"],
    "практичный": ["функциональный"],
    "очень": ["весьма"],
    "отлично": ["прекрасно"],
    "прекрасно": ["отлично"],
    "идеально": ["отлично"],
    "сидит хорошо": ["хорошо сидит"],
    "хорошо сидит": ["сидит хорошо"],
    "рекомендую": ["советую"],
    # цена и торг
    "торг уместен": ["возможен торг"],
    "возможен торг": ["торг уместен"],
    "торг": ["торг уместен", "возможен торг"],
    "без торга": ["цена окончательная"],
    "недорого": ["по приятной цене", "дёшево"],
    "дешево": ["недорого"],
    "дёшево": ["недорого"],
    "цена": ["стоимость"],
    "стоимость": ["цена"],
    "хорошая цена": ["приятная цена"],
    "приятная цена": ["хорошая цена"],
    # связь и передача
    "быстро отвечу": ["отвечаю быстро"],
    "отвечу": ["отвечаю"],
    "пишите": ["напишите"],
    "звоните": ["позвоните"],
    "самовывоз": ["заберёте сами"],
    "отправлю": ["отправка возможна", "вышлю"],
    "отправка": ["пересылка"],
    "доставка": ["отправка"],
    "договоримся": ["обсудим"],
    "уточняйте": ["спрашивайте"],
    "спрашивайте": ["уточняйте"],
    "обмен не интересует": ["обмен не предлагать"],
    "обмен не предлагать": ["обмен не интересует"],
    "в наличии": ["есть в наличии"],
    "смотрите фото": ["смотрите фотографии"],
    "на фото": ["на фотографиях"],
    "фото": ["фотографии"],
    "фотографии": ["фото"],
    "быстро": ["оперативно"],
    "можно": ["есть возможность"],
    "практически": ["почти"],
    "почти": ["практически"],
}

# Нейтральные шапки и подвалы; пустая строка = «без шапки/подвала».
# Плейсхолдеры {brand}/{size} подставляются только если факт есть в facts.
HEADERS: list[str] = [
    "",
    "Отличный вариант на каждый день.",
    "Смотрите фото — всё как есть.",
    "Вещь в наличии, готова к продаже.",
    "Предлагаю к продаже хорошую вещь.",
    "Обратите внимание на это объявление.",
    "Подробности — на фото и в описании.",
    "Вещь ищет нового хозяина.",
    "Актуально, пока объявление опубликовано.",
    "Добротная вещь по разумной цене.",
    "Описание и фото отражают реальное состояние.",
    "Фирменная вещь {brand} в наличии.",
]
FOOTERS: list[str] = [
    "",
    "Отвечу на вопросы в сообщениях.",
    "Самовывоз или отправка — как удобно.",
    "Пишите — договоримся.",
    "Все вопросы — в личные сообщения.",
    "Отвечаю быстро, пишите.",
    "Покажу вещь при встрече.",
    "Договоримся об удобном времени.",
    "Смотрите другие мои объявления.",
    "По всем вопросам пишите в чат.",
    "Если есть вопросы — задавайте.",
    "Размер {size} — уточняйте детали в сообщениях.",
]

# Один регэксп на все ключи: длинные раньше коротких, чтобы «торг уместен» матчился
# прежде «торг»; один проход — заменённый текст повторно не обрабатывается.
_SYN_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in sorted(SYNONYMS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TextVariant:
    """Один вариант текста объявления."""

    title: str
    description: str
    notes: str = ""  # например: "вариант 1 — оригинал" / "форс-добавлена шапка (похожесть)"


def _jaccard_3grams(a: str, b: str) -> float:
    """Похожесть описаний: Jaccard по 3-граммам слов (нормализованных, без пунктуации)."""

    def grams(s: str) -> set[tuple[str, ...]]:
        words = re.findall(r"\w+", s.lower())
        return set(zip(words, words[1:], words[2:])) or {tuple(words)}

    ga, gb = grams(a), grams(b)
    return len(ga & gb) / max(1, len(ga | gb))


def _match_case(replacement: str, sample: str) -> str:
    """Сохраняет регистр замены по первому символу образца."""
    if sample[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _apply_synonyms(text: str, rng: random.Random) -> tuple[str, int]:
    """Заменяет целые слова/фразы по SYNONYMS; возвращает (текст, число замен)."""
    replaced = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal replaced
        options = SYNONYMS.get(match.group(0).lower())
        if not options or rng.random() > _SYNONYM_PROB:
            return match.group(0)
        replaced += 1
        return _match_case(rng.choice(options), match.group(0))

    return _SYN_RE.sub(_sub, text), replaced


def _format_template(template: str, facts: dict[str, str]) -> str | None:
    """Подставляет факты в шаблон; None — если нужного факта нет (шаблон пропускается)."""
    keys = re.findall(r"\{(\w+)\}", template)
    if any(k not in facts for k in keys):
        return None
    return template.format(**facts) if keys else template


def _split_paragraph_sentences(description: str) -> list[list[str]]:
    """Режет описание на абзацы (через пустую строку), абзацы — на предложения."""
    paragraphs = [p for p in re.split(r"\n\s*\n", description) if p.strip()]
    result: list[list[str]] = []
    for p in paragraphs:
        sents = [s.strip() for s in _SENT_RE.findall(p) if s.strip()]
        if sents:
            result.append(sents)
    return result


def _render(par_sents: list[list[str]], style: str) -> str:
    """Собирает текст из предложений в выбранном оформлении."""
    marker = {"dash": "— ", "bullet": "• "}.get(style, "")
    blocks: list[str] = []
    lead = True  # самое первое предложение — смысловое ядро, всегда без маркера
    for sents in par_sents:
        if style == "plain":
            blocks.append(" ".join(sents))
        elif style == "lines":
            blocks.append("\n".join(sents))
        else:
            lines = []
            for s in sents:
                lines.append(s if lead else marker + s)
                lead = False
            blocks.append("\n".join(lines))
        lead = False
    return "\n\n".join(blocks)


def _build_description(
    description: str,
    rng: random.Random,
    facts: dict[str, str],
    *,
    header: str | None = None,
    footer: str | None = None,
) -> tuple[str, list[str]]:
    """Один вариант описания; возвращает (текст, заметки о применённых «ручках»)."""
    notes: list[str] = []
    par_sents = _split_paragraph_sentences(description)

    # Ручка 1: перестановка предложений (первое — смысловое ядро — фиксировано).
    shuffled = False
    for idx, sents in enumerate(par_sents):
        fixed = 1 if idx == 0 else 0
        tail = sents[fixed:]
        if len(tail) > 1:
            before = list(tail)
            rng.shuffle(tail)
            sents[fixed:] = tail
            shuffled = shuffled or tail != before
    # Порядок блоков: абзацы после первого можно менять местами.
    if len(par_sents) > 2 and rng.random() < 0.5:
        rest = par_sents[1:]
        rng.shuffle(rest)
        par_sents[1:] = rest
        shuffled = True
    if shuffled:
        notes.append("перестановка предложений")

    # Ручка 2: синонимы (по предложениям, шапку/подвал не трогаем).
    replaced_total = 0
    for sents in par_sents:
        for j in range(len(sents)):
            sents[j], cnt = _apply_synonyms(sents[j], rng)
            replaced_total += cnt
    if replaced_total:
        notes.append(f"синонимы ({replaced_total})")

    # Ручка 3: шапка/подвал (пустая строка в пуле = «без шапки/подвала»).
    headers_pool = [t for t in (_format_template(h, facts) for h in HEADERS) if t is not None]
    footers_pool = [t for t in (_format_template(f, facts) for f in FOOTERS) if t is not None]
    chosen_header = header if header is not None else rng.choice(headers_pool)
    chosen_footer = footer if footer is not None else rng.choice(footers_pool)
    if chosen_header:
        notes.append("шапка")
    if chosen_footer:
        notes.append("подвал")

    # Ручка 4: оформление (plain — чаще, чтобы тексты выглядели естественно).
    style = rng.choice(("plain", "plain", "lines", "dash", "bullet"))
    body = _render(par_sents, style) or description.strip()
    if style in ("dash", "bullet") and sum(map(len, par_sents)) > 1:
        notes.append("маркеры «—»" if style == "dash" else "маркеры «•»")
    elif style == "lines":
        notes.append("построчно")

    text = "\n\n".join(part for part in (chosen_header, body, chosen_footer) if part)
    return text, notes


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
    """Кандидаты названия: перестановки слов + добавки из facts; без оригинала."""
    words = title.split()
    bases = [" ".join(words)]
    for k in range(1, len(words)):
        head, tail = list(words[k:]), list(words[:k])
        if tail[0].istitle():
            tail[0] = tail[0].lower()  # бывшее первое слово больше не в начале
        head[0] = head[0][:1].upper() + head[0][1:]
        bases.append(" ".join(head + tail))
        bases.append(" ".join(head) + ", " + " ".join(tail))

    size = facts.get("size", "")
    brand = facts.get("brand", "")
    condition = facts.get("condition", "")
    suffixes = [""]
    if size and size.lower() not in title.lower():
        suffixes += [f", {size}", f" {size}"]
    if brand and brand.lower() not in title.lower():
        suffixes.append(f", {brand}")
    if condition:
        suffixes.append(f", состояние {condition.lower()}")
    if size and condition and size.lower() not in title.lower():
        suffixes.append(f", {size}, состояние {condition.lower()}")

    candidates: list[str] = []
    for b in bases:
        for s in suffixes:
            c = (b + s).strip()
            if c and c != title and c not in candidates:
                candidates.append(c)
    return candidates


def _build_title(title: str, rng: random.Random, facts: dict[str, str], used: set[str]) -> str:
    """Название варианта: перестановки/добавки, отличное от уже использованных."""
    candidates = _title_candidates(title, facts)
    rng.shuffle(candidates)
    for cand in candidates:
        clipped = _clip_title(cand)
        if clipped and clipped not in used:
            return clipped
    # Резерв (вырожденный случай: однословное название без фактов): добивка «!».
    # Укорачиваем базу заранее, чтобы суффикс «!» не нарушил лимит 50 символов.
    fallback = _clip_title(title) or title.strip()[:TITLE_MAX_LEN]
    while fallback in used:
        # Сколько символов «!» войдёт в итог (текущий хвост + 1 новый).
        suffix_len = len(fallback) - len(fallback.rstrip("!")) + 1
        max_base = TITLE_MAX_LEN - suffix_len
        if max_base <= 0:
            # Предельный вырожденный случай: всё место занято «!» — берём только «!».
            fallback = "!" * TITLE_MAX_LEN
            break
        # База — часть без «!»-хвоста; при необходимости укорачиваем до max_base.
        base_part = fallback.rstrip("!")
        if len(base_part) > max_base:
            # Пробуем обрезать по границе слова через _clip_title.
            trimmed = _clip_title(base_part[:max_base])
            # _clip_title может вернуть строку до max_base; если пуста — жёсткая обрезка.
            base_part = (trimmed or base_part)[:max_base]
        fallback = base_part + "!" * suffix_len
    return fallback


def vary_listing(
    title: str,
    description: str,
    n: int,
    *,
    seed: int | None = None,
    facts: dict[str, str] | None = None,
) -> list[TextVariant]:
    """Делает n вариантов текста объявления; вариант №1 — нетронутый оригинал.

    Тот же seed → побайтно тот же результат; facts (ключи "size", "condition",
    "brand") идут только в добавки названия и плейсхолдеры шапок/подвалов.
    """
    if n < 1:
        raise ValueError(f"Число вариантов должно быть >= 1, получено {n}")
    clean_facts = {
        k: v.strip() for k, v in (facts or {}).items() if isinstance(v, str) and v.strip()
    }
    if seed is None:
        seed = random.randrange(2**32)

    variants = [TextVariant(title=title, description=description, notes="вариант 1 — оригинал")]
    used_titles = {title}
    # Непустые шапки/подвалы для форс-режима (доступные при данных facts).
    force_headers = [t for t in (_format_template(h, clean_facts) for h in HEADERS) if t]
    force_footers = [t for t in (_format_template(f, clean_facts) for f in FOOTERS) if t]

    for i in range(1, n):
        desc: str | None = None
        note_parts: list[str] = []
        # Обычный путь: до REBUILD_ATTEMPTS пересборок с другим внутренним seed.
        for attempt in range(REBUILD_ATTEMPTS):
            rng = random.Random(f"{seed}:desc:{i}:{attempt}")
            candidate, parts = _build_description(description, rng, clean_facts)
            if all(_jaccard_3grams(candidate, v.description) <= SIMILARITY_MAX for v in variants):
                desc, note_parts = candidate, parts
                break
        if desc is None:
            # Форс-режим: перебираем пары «шапка + подвал», пока вариант не разлепится.
            best: tuple[float, str, list[str]] | None = None
            for shift in range(_FORCE_ATTEMPTS):
                forced_header = force_headers[(i + shift) % len(force_headers)]
                forced_footer = force_footers[(i * 3 + shift) % len(force_footers)]
                rng = random.Random(f"{seed}:desc:{i}:force:{shift}")
                candidate, parts = _build_description(
                    description, rng, clean_facts, header=forced_header, footer=forced_footer
                )
                worst = max(_jaccard_3grams(candidate, v.description) for v in variants)
                parts = parts + ["форс-добавлены шапка и подвал (похожесть)"]
                if worst <= SIMILARITY_MAX:
                    desc, note_parts = candidate, parts
                    break
                if best is None or worst < best[0]:
                    best = (worst, candidate, parts)
            if desc is None and best is not None:
                logger.warning(
                    "Вариант %d: похожесть %.2f выше порога %.2f даже после форс-режима",
                    i + 1, best[0], SIMILARITY_MAX,
                )
                desc, note_parts = best[1], best[2]

        title_rng = random.Random(f"{seed}:title:{i}")
        variant_title = _build_title(title, title_rng, clean_facts, used_titles)
        used_titles.add(variant_title)
        variants.append(
            TextVariant(
                title=variant_title,
                description=desc if desc is not None else description,
                notes=f"вариант {i + 1}: " + ", ".join(note_parts),
            )
        )

    logger.info("Сгенерировано %d вариантов текста объявления (seed=%d)", n, seed)
    return variants


if __name__ == "__main__":
    from itertools import combinations

    base = ("Продаю мужской пиджак Hugo Boss. Отличное состояние, без дефектов.\n\n"
            "Размер 48. Торг.")
    base_title = "Пиджак Hugo Boss"
    base_facts = {"size": "48 (M)", "brand": "Hugo Boss"}

    # ── Тест 1: размеры словарей и пулов (требования §17.3) ──────────────────
    assert len(SYNONYMS) >= 50, f"Нужно >=50 пар синонимов, есть {len(SYNONYMS)}"
    assert all(opts for opts in SYNONYMS.values()), "Пустой список синонимов у ключа"
    assert all(k == k.lower() for k in SYNONYMS), "Ключи синонимов — в нижнем регистре"
    assert len(HEADERS) >= 10 and len(FOOTERS) >= 10, "Нужно >=10 шапок и >=10 подвалов"
    print(f"[OK] Тест 1: SYNONYMS={len(SYNONYMS)} пар, "
          f"HEADERS={len(HEADERS)}, FOOTERS={len(FOOTERS)}")

    # ── Тест 2: базовый сценарий n=5 ─────────────────────────────────────────
    vs = vary_listing(base_title, base, 5, seed=42, facts=base_facts)
    assert len(vs) == 5, f"Ожидалось 5 вариантов, получено {len(vs)}"
    assert all(v.title.strip() and v.description.strip() for v in vs), "Есть пустой вариант"
    assert vs[0].title == base_title and vs[0].description == base, "Вариант 1 не оригинал"
    assert "оригинал" in vs[0].notes, f"notes оригинала: {vs[0].notes!r}"
    print("[OK] Тест 2: n=5 — все непустые, вариант 1 — нетронутый оригинал")

    # ── Тест 3: попарная похожесть описаний не выше порога ───────────────────
    for a, b in combinations([v.description for v in vs], 2):
        sim = _jaccard_3grams(a, b)
        assert sim <= SIMILARITY_MAX, f"Похожесть {sim:.2f} > {SIMILARITY_MAX}:\n{a!r}\n{b!r}"
    print("[OK] Тест 3: похожесть всех пар описаний <= порога")

    # ── Тест 4: названия попарно различны, сгенерированные — не длиннее 50 ───
    assert len({v.title for v in vs}) == 5, [v.title for v in vs]
    for v in vs[1:]:
        assert len(v.title) <= 50, f"Название длиннее 50: {v.title!r}"
    print("[OK] Тест 4: названия различны, длина <= 50")

    # ── Тест 5: воспроизводимость seed; другой seed — другой результат ───────
    again = vary_listing(base_title, base, 5, seed=42, facts=base_facts)
    assert again == vs, "Тот же seed дал другой результат"
    other = vary_listing(base_title, base, 5, seed=43, facts=base_facts)
    assert other != vs, "Другой seed дал тот же результат"
    print("[OK] Тест 5: тот же seed воспроизводим, другой seed отличается")

    # ── Тест 6: никаких новых «фактов» — все слова из разрешённых источников ─
    def _word_set(s: str) -> set[str]:
        return set(re.findall(r"\w+", s.lower()))

    allowed = _word_set(base_title) | _word_set(base)
    for _options in SYNONYMS.values():
        for _opt in _options:
            allowed |= _word_set(_opt)
    for _tpl in HEADERS + FOOTERS:
        allowed |= _word_set(_tpl)
    for _fact in base_facts.values():
        allowed |= _word_set(_fact)
    for v in vs:
        extra = (_word_set(v.title) | _word_set(v.description)) - allowed
        assert not extra, f"Новые слова в варианте: {extra}"
    print("[OK] Тест 6: новых слов (фактов) не появилось")

    # ── Тест 7: n=1 — только оригинал; n=0 — ошибка; seed=None работает ──────
    only = vary_listing(base_title, base, 1, seed=7, facts=base_facts)
    assert len(only) == 1 and only[0].title == base_title and only[0].description == base
    try:
        vary_listing(base_title, base, 0, seed=1)
        raise AssertionError("n=0 должно вызывать ValueError")
    except ValueError:
        pass
    assert len(vary_listing(base_title, base, 2)) == 2, "seed=None должен работать"
    print("[OK] Тест 7: n=1 — оригинал; n=0 — ValueError; seed=None работает")

    # ── Тест 8: n=10 — объём, различия, похожесть ────────────────────────────
    vs10 = vary_listing(base_title, base, 10, seed=99, facts=base_facts)
    assert len(vs10) == 10
    assert all(v.title.strip() and v.description.strip() for v in vs10)
    assert len({v.title for v in vs10}) == 10, [v.title for v in vs10]
    for a, b in combinations([v.description for v in vs10], 2):
        assert _jaccard_3grams(a, b) <= SIMILARITY_MAX
    print("[OK] Тест 8: n=10 — различны и непусты")

    # ── Тест 9: описание из одного предложения ───────────────────────────────
    one = "Продаю джинсы Levis 501."
    vs1 = vary_listing("Джинсы Levis 501", one, 5, seed=5, facts={"size": "32/34"})
    assert len(vs1) == 5 and all(v.description.strip() for v in vs1)
    assert vs1[0].description == one
    for a, b in combinations([v.description for v in vs1], 2):
        assert _jaccard_3grams(a, b) <= SIMILARITY_MAX, (a, b)
    assert len({v.title for v in vs1}) == 5, [v.title for v in vs1]
    print("[OK] Тест 9: описание из одного предложения")

    # ── Тест 10: абзацы — ядро первым, абзацы не перемешиваются ──────────────
    par_desc = ("Алый шарф ручной вязки. Длина два метра. Подойдёт к пальто.\n\n"
                "Хранился в шкафу. Запахов нет.")
    par1 = ["Алый шарф ручной вязки.", "Длина два метра.", "Подойдёт к пальто."]
    par2 = ["Хранился в шкафу.", "Запахов нет."]
    vsp = vary_listing("Шарф ручной вязки", par_desc, 4, seed=11)
    assert vsp[0].description == par_desc
    for v in vsp:
        d = v.description
        pos1 = [d.find(s) for s in par1]
        pos2 = [d.find(s) for s in par2]
        assert all(p >= 0 for p in pos1 + pos2), f"Потеряно предложение:\n{d!r}"
        assert pos1[0] == min(pos1 + pos2), f"Первое предложение не первым:\n{d!r}"
        assert max(pos1) < min(pos2), f"Абзацы перемешаны:\n{d!r}"
    print("[OK] Тест 10: первое предложение фиксировано, абзацы не смешиваются")

    # ── Тест 11: длинное название обрезается по словам до 50 ─────────────────
    long_title = "Очень длинное название мужского классического костюма тёмного цвета"
    assert len(long_title) > 50
    vl = vary_listing(long_title, base, 3, seed=3)
    assert vl[0].title == long_title, "Оригинальное название тронуто"
    lt_words = {w.lower() for w in long_title.split()}
    for v in vl[1:]:
        assert 0 < len(v.title) <= 50, f"Длина названия: {len(v.title)}"
        for w in v.title.replace(",", " ").split():
            assert w.lower() in lt_words, f"Обрезано посреди слова: {w!r} в {v.title!r}"
    assert len({v.title for v in vl}) == 3
    print("[OK] Тест 11: обрезка длинного названия по словам")

    # ── Тест 12: вырожденный случай — однословное название 50 символов ──────────
    degen_title = "А" * 50
    degen_desc = "Продаю вещь. Хорошее состояние."
    vd = vary_listing(degen_title, degen_desc, 3, seed=1)
    assert vd[0].title == degen_title, "Вариант 0 тронут"
    assert len({v.title for v in vd}) == 3, f"Названия не попарно различны: {[v.title for v in vd]}"
    for v in vd:
        assert len(v.title) <= 50, f"Название длиннее 50 ({len(v.title)}): {v.title!r}"
    print("[OK] Тест 12: вырожденный случай — все названия ≤50, попарно различны, вариант 0 — оригинал")

    # ── Тест 13: обязательный базовый блок из ТЗ ─────────────────────────────
    vs = vary_listing("Пиджак Hugo Boss", base, 5, seed=42,
                      facts={"size": "48 (M)", "brand": "Hugo Boss"})
    assert len(vs) == 5 and all(v.description.strip() and v.title.strip() for v in vs)
    assert vs[0].description == base and vs[0].title == "Пиджак Hugo Boss"
    for a, b in combinations([v.description for v in vs], 2):
        assert _jaccard_3grams(a, b) <= SIMILARITY_MAX, (a, b)
    assert len({v.title for v in vs}) == 5
    assert vary_listing("Пиджак Hugo Boss", base, 5, seed=42,
                        facts={"size": "48 (M)", "brand": "Hugo Boss"}) == vs
    print("[OK] text_variation: все самотесты пройдены")

