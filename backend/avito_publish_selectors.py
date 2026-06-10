"""
Селекторы формы публикации объявления Авито (avito.ru/additem).

НЕ смешивать с avito_selectors.py — там селекторы ПОИСКОВОЙ выдачи и страниц
объявлений (аналитика спроса). Здесь — только форма создания черновика.

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
# Категория «Пиджаки и костюмы» — контрольные значения hidden-полей.
# Заполнять их НЕ нужно — только сверять перед заполнением формы.
# ---------------------------------------------------------------------------

# <input data-marker="category-hidden-input" name="..." value="...">
EXPECTED_CATEGORY: dict[str, str] = {
    "category_id": "27",     # Товары
    "params[175]": "748",    # Мужская одежда
    "params[176]": "754",    # Пиджаки и костюмы
}

# Хлебные крошки категории. На форме это <a data-marker="category-title" role="button">
# — КЛИКАБЕЛЬНЫ: клик открывает мастер выбора категории (миллер-колонки).
# Также служат дублирующим текстовым контролем выбранной категории.
CATEGORY_TITLE = "[data-marker='category-title']"
CATEGORY_TITLE_EXPECTED_TEXT = "Пиджаки и костюмы"

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
# Заголовок полноэкранного picker: «Новое объявление».
CATEGORY_FULL_PATH: tuple[str, ...] = (
    "Личные вещи",
    "Одежда, обувь, аксессуары",
    "Мужская одежда",
    "Пиджаки и костюмы",
)

# ---------------------------------------------------------------------------
# Поля формы
# ---------------------------------------------------------------------------

# Название. Маркер «title-field-23/input» содержит, похоже, динамический
# суффикс «-23» — опираемся на стабильный атрибут name.
TITLE_INPUT = "input[name='title']"

# Фотографии: <input type=file multiple> — заливать через set_input_files,
# кликов не нужно. accept: gif/png/jpeg/pjpeg/heic. Лимит формы: 10.
PHOTO_INPUT = "[data-marker='add/input']"

# Бренд: текстовый input с автокомплитом; достаточно ввести текст.
BRAND_INPUT = "[data-marker='params[115634]/input']"

# Описание: rich-editor (contenteditable). НЕ .fill() — кликнуть и вводить
# с клавиатуры. Контроль: hidden input[name='description_html'] непустой.
DESCRIPTION_EDITOR = "[data-marker='description-html-editor']"
DESCRIPTION_HIDDEN = "input[name='description_html']"

# Цена: видимый input с маркером price; дублируется в hidden input[name='price'].
PRICE_INPUT = "input[data-marker='price']"
PRICE_INPUT_FALLBACK = "[data-marker='price'] input"  # если маркер на обёртке

# Адрес: текстовый input с гео-саджестом. Вводить посимвольно с задержкой,
# ждать саджест, кликнуть первый пункт. Контроль: hidden address/locationId.
GEO_SEARCH_INPUT = "[data-marker='geo/search-input']"
GEO_SUGGEST = "[data-marker='geo/field/suggest']"
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
# ---------------------------------------------------------------------------

COMBOBOX_CONTAINER_TMPL = "[data-marker='{prefix}']"            # триггер (открыть)
COMBOBOX_SELECT_TMPL = "[data-marker='{prefix}/select']"        # нативный select (верификация)
COMBOBOX_SEARCH_TMPL = "[data-marker='{prefix}/search-input']"  # поле поиска (фолбэк)

# Префиксы комбобоксов
TRADE_TYPE_PREFIX = "type_of_trade"   # Вид объявления
SIZE_PREFIX = "razmer"                # Размер
COLOR_PREFIX = "cvet"                 # Цвет
# material_osnovnoi_chasti (Материал) — в v1 НЕ заполняем (поле необязательное)

# Радио «Состояние»: кликать по label с маркером params[110385]/{ID}
CONDITION_RADIO_TMPL = "[data-marker='params[110385]/{option_id}']"

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
# Словари опций: человекочитаемый лейбл → ID опции Авито.
# Лейблы — это же допустимые значения полей формы проекта (валидация по ключам).
# Источник: debug/additem_map.txt (живой дамп 2026-06-09).
# ---------------------------------------------------------------------------

# Вид объявления (комбобокс type_of_trade) — 3 опции
TRADE_TYPE_OPTIONS: dict[str, int] = {
    "Продаю своё": 20029,
    "Товар приобретён на продажу": 20030,
    "Товар от производителя": 20031,
}

# Состояние (радио params[110385]) — 4 опции
CONDITION_OPTIONS: dict[str, int] = {
    "Новое с биркой": 2804445,
    "Отличное": 431223,
    "Хорошее": 431224,
    "Удовлетворительное": 2804446,
}

# Размер (комбобокс razmer) — 24 опции
SIZE_OPTIONS: dict[str, int] = {
    "40 (XXS)": 1364250,
    "42 (XS)": 1364251,
    "44 (XS/S)": 1364252,
    "46 (S)": 1364253,
    "48 (M)": 1364254,
    "50 (L)": 1364255,
    "52 (L/XL)": 1364256,
    "54 (XL)": 1364257,
    "56 (XXL)": 1364258,
    "58 (XXL)": 1364259,
    "60 (3XL)": 1364260,
    "62 (4XL)": 1364261,
    "64 (5XL)": 1364262,
    "66 (6XL)": 1364263,
    "68 (7XL)": 1364264,
    "70 (7XL)": 1364265,
    "72 (8XL)": 1364266,
    "74 (8XL)": 1364267,
    "76 (9XL)": 1364268,
    "78 (10XL)": 1364269,
    "80 (10XL)": 1364270,
    "82+ (10XL+)": 1364271,
    "One size": 3263777,
    "Без размера": 1364272,
}

# Цвет (комбобокс cvet) — 17 опций
COLOR_OPTIONS: dict[str, int] = {
    "Красный": 754149,
    "Белый": 754146,
    "Розовый": 754160,
    "Бордовый": 2849885,
    "Синий": 754151,
    "Жёлтый": 754156,
    "Голубой": 754158,
    "Фиолетовый": 754159,
    "Оранжевый": 754154,
    "Разноцветный": 754161,
    "Серый": 754148,
    "Бежевый": 754150,
    "Чёрный": 754147,
    "Коричневый": 754153,
    "Зелёный": 754157,
    "Серебряный": 754152,
    "Золотой": 754155,
}


# ---------------------------------------------------------------------------
# Самотесты (запуск: python backend/avito_publish_selectors.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Размеры словарей соответствуют живому дампу
    assert len(TRADE_TYPE_OPTIONS) == 3, f"Вид объявления: 3, есть {len(TRADE_TYPE_OPTIONS)}"
    assert len(CONDITION_OPTIONS) == 4, f"Состояние: 4, есть {len(CONDITION_OPTIONS)}"
    assert len(SIZE_OPTIONS) == 24, f"Размер: 24, есть {len(SIZE_OPTIONS)}"
    assert len(COLOR_OPTIONS) == 17, f"Цвет: 17, есть {len(COLOR_OPTIONS)}"
    print("[OK] Размеры словарей: 3 / 4 / 24 / 17")

    # ID уникальны внутри каждого словаря
    for name, options in (
        ("TRADE_TYPE_OPTIONS", TRADE_TYPE_OPTIONS),
        ("CONDITION_OPTIONS", CONDITION_OPTIONS),
        ("SIZE_OPTIONS", SIZE_OPTIONS),
        ("COLOR_OPTIONS", COLOR_OPTIONS),
    ):
        ids = list(options.values())
        assert len(ids) == len(set(ids)), f"{name}: дублирующиеся ID"
    print("[OK] ID опций уникальны")

    # Согласованность данных категории (сами значения — разведданные,
    # их «копии в assert'ах» не держим: правка была бы двойной)
    assert len(EXPECTED_CATEGORY) == 3 and all(EXPECTED_CATEGORY.values())
    assert CATEGORY_FULL_PATH and all(CATEGORY_FULL_PATH)
    assert CATEGORY_FULL_PATH[-1] == CATEGORY_TITLE_EXPECTED_TEXT
    print("[OK] Мастер выбора категории: путь согласован с контролем заголовка")

    # Шаблоны комбобоксов собираются корректно
    container = COMBOBOX_CONTAINER_TMPL.format(prefix=COLOR_PREFIX)
    native = COMBOBOX_SELECT_TMPL.format(prefix=COLOR_PREFIX)
    search = COMBOBOX_SEARCH_TMPL.format(prefix=COLOR_PREFIX)
    assert container == "[data-marker='cvet']", container
    assert native == "[data-marker='cvet/select']", native
    assert search == "[data-marker='cvet/search-input']", search
    radio = CONDITION_RADIO_TMPL.format(option_id=CONDITION_OPTIONS["Отличное"])
    assert radio == "[data-marker='params[110385]/431223']", radio
    print("[OK] Шаблоны селекторов комбобоксов и радио")

    # Запрещённый селектор не объявлен в модуле как константа
    _module_vars = {k: v for k, v in globals().items() if k.isupper()}
    for _name, _val in _module_vars.items():
        if isinstance(_val, str):
            assert "button-next" not in _val, f"ЗАПРЕЩЁННЫЙ селектор в {_name}!"
    print("[OK] Селектор кнопки «Продолжить» не объявлен")

    print("\n=== Все самотесты avito_publish_selectors.py пройдены ===")
