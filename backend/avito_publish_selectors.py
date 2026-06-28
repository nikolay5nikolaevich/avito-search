"""
Селекторы формы публикации объявления Авито (avito.ru/additem).

НЕ смешивать с avito_selectors.py — там селекторы ПОИСКОВОЙ выдачи и страниц
объявлений (аналитика спроса). Здесь — только форма создания черновика.

Категорийные словари (опции, префиксы, контрольные ID) переехали в category_profiles.py.

Все data-marker и ID опций ПОДТВЕРЖДЕНЫ живой разведкой 2026-06-09
(дамп debug/additem.html, выжимка debug/additem_map.txt, категория
«Пиджаки и костюмы»). При изменении вёрстки Авито: снять новый дамп,
прогнать debug/extract_additem_map.py и поправить значения здесь.

ЖЁСТКОЕ ПРАВИЛО БЕЗОПАСНОСТИ (см. ТЗ §2):
    Кнопка «Продолжить» [data-marker='item-edit/button-next'] ведёт к ПУБЛИКАЦИИ
    и списанию реальных денег (платный тариф с оплатой просмотров).
    Её селектор НЕ объявлен в этом модуле и НЕ должен появляться в коде иначе
    как в комментарии-предупреждении. Единственная разрешённая финальная
    кнопка — «Сохранить и выйти» (SAVE_AND_EXIT_BUTTON ниже).
"""

# ---------------------------------------------------------------------------
# Категория — служебные (категория-независимые) элементы
# ---------------------------------------------------------------------------

# Хлебные крошки категории. На форме это <a data-marker="category-title" role="button">
# — КЛИКАБЕЛЬНЫ: клик открывает мастер выбора категории (миллер-колонки).
CATEGORY_TITLE = "[data-marker='category-title']"

# Hidden-поля категории (их три, различаются атрибутом name)
CATEGORY_HIDDEN_INPUTS = "[data-marker='category-hidden-input']"

# ---------------------------------------------------------------------------
# Мастер выбора категории — ПОДТВЕРЖДЁН живой CDP-разведкой 2026-06-10
# (debug/newitem*.txt, debug/recon_full.py). Это НЕ поиск, а дерево-миллер:
# каждый пункт любого уровня — <button data-marker="category-wizard/button">
# с названием категории внутри (текст может содержать &nbsp; → нормализовать).
# Пункт «Другая категория» имеет отдельный маркер another-category (не нужен).
#
# Поток (см. publisher._step_select_category):
#   1) свежий /additem открывает полноэкранный picker (есть category-wizard/button,
#      НЕТ крошек category-title) → клик по верхней «Личные вещи» уводит на форму;
#   2) на форме клик по крошкам category-title раскрывает миллер-мастер;
#   3) досверливаем полный путь кликами по category-wizard/button, ПРОПУСКАЯ
#      уровень, если его подуровень уже виден (повторный клик по выбранному
#      пункту его схлопывает);
#   4) после «Пиджаки и костюмы» грузится костюмная форма (hidden 27/748/754).
# ---------------------------------------------------------------------------
CATEGORY_WIZARD_BUTTON = "button[data-marker='category-wizard/button']"

# ---------------------------------------------------------------------------
# Поля формы
# ---------------------------------------------------------------------------

# Название. Маркер «title-field-23/input» содержит, похоже, динамический
# суффикс «-23» — опираемся на стабильный атрибут name.
TITLE_INPUT = "input[name='title']"

# Фотографии: <input type=file multiple> — заливать через set_input_files,
# кликов не нужно. accept: gif/png/jpeg/pjpeg/heic. Лимит формы: 10.
PHOTO_INPUT = "[data-marker='add/input']"

# Описание: rich-editor (contenteditable). НЕ .fill() — кликнуть и вводить
# с клавиатуры. Контроль: hidden input[name='description_html'] непустой.
DESCRIPTION_EDITOR = "[data-marker='description-html-editor']"
DESCRIPTION_HIDDEN = "input[name='description_html']"

# Цена: видимый input с маркером price; дублируется в hidden input[name='price'].
PRICE_INPUT = "input[data-marker='price']"
PRICE_INPUT_FALLBACK = "[data-marker='price'] input"  # если маркер на обёртке

# Адрес: текстовый input с гео-саджестом. Вводить посимвольно с задержкой,
# ждать саджест, кликнуть первый РЕАЛЬНЫЙ пункт-адрес. Контроль: hidden address/locationId.
GEO_SEARCH_INPUT = "[data-marker='geo/search-input']"
# GEO_SUGGEST — это КОНТЕЙНЕР выпадашки (обёртка), кликать его бесполезно:
# адрес не выберется. Используем для ожидания появления подсказок.
GEO_SUGGEST = "[data-marker='geo/field/suggest']"
# Реальные пункты-адреса — кнопки <button data-marker='geo/.../custom-option(N)'>
# (живая разведка 2026-06-28: 'geo/undefined/custom-option(0)' = «Тверская улица, 7, Москва»).
# Кликать нужно ИХ, а не контейнер GEO_SUGGEST.
GEO_SUGGEST_OPTION = "button[data-marker*='/custom-option(']"
ADDRESS_HIDDEN = "input[name='address']"
LOCATION_ID_HIDDEN = "input[name='locationId']"

# ---------------------------------------------------------------------------
# Комбобоксы Авито — РАЗВЕДАНЫ живьём 2026-06-10 (debug/recon_combobox_open.py).
# Это кастомный React-виджет <div role="combobox">, внутри:
#   - скрытый <span data-marker="{prefix}/select-text"> (НЕ кликать — невидим!);
#   - скрытый нативный <select data-marker="{prefix}/select"> (тоже невидим →
#     Playwright select_option НЕ сработает: actionability падает на visible);
#   - <input data-marker="{prefix}/search-input"> (поле поиска, видно при открытии).
# Пункты раскрытого списка — ВИДИМЫЕ <div> с ТЕКСТОМ опции и БЕЗ data-marker
# (маркеры option(ID) висят на скрытых <option> нативного select).
# Правильный алгоритм (publisher._select_combobox):
#   1) клик по КОНТЕЙНЕРУ "[data-marker='{prefix}']" (role=combobox) — раскрыть;
#   2) клик по ВИДИМОМУ элементу с точным текстом опции (по лейблу, не по ID);
#   3) верификация: нативный <select> получил value == ID опции.
# Префиксы конкретных полей (тип сделки, размер, цвет) и шаблон радио-состояния
# хранятся в CategoryProfile (category_profiles.py).
# ---------------------------------------------------------------------------

COMBOBOX_CONTAINER_TMPL = "[data-marker='{prefix}']"            # триггер (открыть)
COMBOBOX_SELECT_TMPL = "[data-marker='{prefix}/select']"        # нативный select (верификация)
COMBOBOX_SEARCH_TMPL = "[data-marker='{prefix}/search-input']"  # поле поиска (фолбэк)

# ---------------------------------------------------------------------------
# Ожидание загрузки фото: подтверждённого маркера превью в дампе нет
# (дамп снят без загруженных фото). Кандидаты проверяются по очереди;
# промах каждого логируется как SELECTOR_MISS, но не валит шаг —
# финальный fallback в publisher.py: щедрая фиксированная пауза.
# ---------------------------------------------------------------------------

PHOTO_PREVIEW_CANDIDATES: list[str] = [
    "[data-marker='image']",
    "[data-marker='thumbnail']",
    "[class*='photo-list'] img",
    "[class*='uploader'] img",
    "[class*='images'] img[src*='blob:'], [class*='images'] img[src*='avito']",
]

# ---------------------------------------------------------------------------
# Финальные кнопки
# ---------------------------------------------------------------------------

# ЕДИНСТВЕННАЯ разрешённая финальная кнопка: «Сохранить и выйти» → черновик.
# ВАЖНО (разведка 2026-06-10): эта кнопка появляется ТОЛЬКО когда в форме есть
# несохранённые данные. На пустой форме внизу стоит «Выйти»
# (item-creator/exit). Публикатор заполняет форму до шага save_draft, поэтому
# к моменту клика кнопка уже на месте — save_draft ждёт её появления.
SAVE_AND_EXIT_BUTTON = "[data-marker='item-creator/save-and-exit']"

# ВНИМАНИЕ: кнопка «Продолжить» (data-marker «item-edit/button-next») —
# ЗАПРЕЩЕНА. Клик по ней публикует объявление и списывает деньги с тарифа.
# Селектор намеренно НЕ объявлен как константа. НЕ добавлять. НЕ кликать.

# ---------------------------------------------------------------------------
# Самотесты (запуск: python backend/avito_publish_selectors.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Шаблоны комбобоксов собираются корректно (префикс «cvet» — пример)
    container = COMBOBOX_CONTAINER_TMPL.format(prefix="cvet")
    native = COMBOBOX_SELECT_TMPL.format(prefix="cvet")
    search = COMBOBOX_SEARCH_TMPL.format(prefix="cvet")
    assert container == "[data-marker='cvet']", container
    assert native == "[data-marker='cvet/select']", native
    assert search == "[data-marker='cvet/search-input']", search
    print("[OK] Шаблоны селекторов комбобоксов")

    # Запрещённый селектор не объявлен в модуле как константа
    _module_vars = {k: v for k, v in globals().items() if k.isupper()}
    for _name, _val in _module_vars.items():
        if isinstance(_val, str):
            assert "button-next" not in _val, f"ЗАПРЕЩЁННЫЙ селектор в {_name}!"
    print("[OK] Селектор кнопки «Продолжить» не объявлен")

    print("\n=== Все самотесты avito_publish_selectors.py пройдены ===")
