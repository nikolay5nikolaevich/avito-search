"""
Селекторы формы публикации объявления Авито (avito.ru/additem).

НЕ смешивать с avito_selectors.py — там селекторы ПОИСКОВОЙ выдачи и страниц
объявлений (аналитика спроса). Здесь — сценарий создания и публикации.

Категорийные словари (опции, префиксы, контрольные ID) переехали в category_profiles.py.

Все data-marker и ID опций ПОДТВЕРЖДЕНЫ живой разведкой 2026-06-09
(дамп debug/additem.html, выжимка debug/additem_map.txt, категория
«Пиджаки и костюмы»). При изменении вёрстки Авито: снять новый дамп,
прогнать debug/extract_additem_map.py и поправить значения здесь.

ЖЁСТКОЕ ПРАВИЛО БЕЗОПАСНОСТИ (см. ТЗ §2):
    Маркер [data-marker='item-edit/button-next'] Авито переиспользует. Сразу
    после выбора категории это промежуточная кнопка, открывающая форму; на
    заполненной форме она ведёт дальше к ПУБЛИКАЦИИ и возможному списанию денег.
    Поэтому каждый клик этого marker разрешён только отдельным шагом publisher
    после проверки контекста: сначала подтверждение категории, затем заполненная
    форма. Финансовые экраны имеют собственные точные селекторы и проверки.
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

# Промежуточное подтверждение выбранной категории. Маркер опасно переиспользуется
# Авито дальше по форме; контекстные проверки перед кликом обязательны — см.
# publisher._continue_category_confirmation.
CATEGORY_CONFIRM_CONTINUE_BUTTON = "button[data-marker='item-edit/button-next']"

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
GEO_SUGGEST_OPTION = (
    "[data-marker='geo/field/suggest'] "
    "button[data-marker*='/custom-option(']"
)
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
    "[class*='uploader'] img",
    "[data-marker='image']",
    "[data-marker='thumbnail']",
    "[class*='photo-list'] img",
    "[class*='images'] img[src*='blob:'], [class*='images'] img[src*='avito']",
]

# ---------------------------------------------------------------------------
# Контроль заполненной формы
# ---------------------------------------------------------------------------

# «Сохранить и выйти» появляется только у заполненной формы. Publisher использует
# marker как read-only контекстный признак перед «Продолжить» и НИКОГДА не кликает.
SAVE_AND_EXIT_BUTTON = "[data-marker='item-creator/save-and-exit']"

# ---------------------------------------------------------------------------
# Полная публикация: подтверждённая DOM-карта 2026-08-13
# ---------------------------------------------------------------------------

# На заполненной форме Авито повторно использует тот же marker, что и на
# промежуточном подтверждении категории. Клик разрешён только после отдельной
# контекстной проверки заполненной формы в publisher.py.
FORM_CONTINUE_BUTTON = "button[data-marker='item-edit/button-next']"

# /cpxpromo/{item_id}?vasFrom=item_add — настройка стоимости просмотра.
VIEW_PRICE_ONBOARDING_TOOLTIP = (
    "[data-marker='onboarding-tooltip/autoFromManual/1']"
)
VIEW_PRICE_ONBOARDING_CLOSE = (
    "[data-marker='onboarding-tooltip/autoFromManual/1'] "
    "button:has(svg[data-icon-name='close'])"
)
# Актуальный DOM 2026-08-21: режим задаётся двумя radio-option.
VIEW_PRICE_MANUAL_OPTION = (
    "[data-marker='budget-setting-select/option(manual)']"
)
VIEW_PRICE_MANUAL_RADIO = (
    "[data-marker='budget-setting-select/option(manual)'] "
    "input[type='radio'][value='manual']"
)
# Legacy-маркер оставлен как безопасный fallback для старой A/B-версии формы.
VIEW_PRICE_MODE_SWITCH = "[data-marker='bid-type-switcher'][role='switch']"
VIEW_PRICE_MODE_TOGGLE = "[data-marker='bid-type-switcher/toggle']"
VIEW_PRICE_CITY_INPUT = "[data-marker='geo-picker-trigger/input']"
VIEW_PRICE_INPUT_CONTAINER = "[data-marker='bid-setting-input']"
VIEW_PRICE_INPUT = "[data-marker='bid-setting-input/input']"
VIEW_PRICE_SLIDER = "[data-marker='bid-settings'] [role='slider']"
VIEW_PRICE_MINIMUM_TEXT = "[data-marker='bid-settings-caption-text']"
VIEW_PRICE_DAILY_BUDGET_INPUT = "[data-marker='limit-settings-input/input']"
# У кнопки продолжения больше нет стабильного data-marker. Publisher перебирает
# только кнопки и разрешает действие по точному тексту из финансового allowlist.
VIEW_PRICE_ACTION_BUTTONS = "button"
VIEW_PRICE_PRESETS = "[data-marker='bid-presets_cards'][role='radiogroup']"

# /pro/performance?vasFrom=avito_osp_applied — только подтверждённые платные
# услуги. Любой новый switch/widget блокирует автоматизацию до новой разведки.
SERVICE_SWITCH_MARKERS: tuple[str, ...] = (
    "pro-vas-configurator/widget/simple/switcher",
    "pro-vas-configurator/widget/xl/switcher",
    "vas-configurator/widget/sbc-union/switcher",
)
SERVICES_ALL_SWITCHES = "[role='switch'][data-marker*='widget']"
SERVICES_ALL_WIDGETS = "[data-marker*='widget']"
SERVICES_CONTINUE_WITHOUT_BUTTON = (
    "button[data-marker='pro-vas-configurator/go-home-btn']"
)

# ВНИМАНИЕ: имя CATEGORY_CONFIRM_CONTINUE_BUTTON относится только к промежуточному
# экрану сразу после проверенной категории. На заполненной форме тот же селектор
# используется отдельно как FORM_CONTINUE_BUTTON и только после полной проверки
# контекста: этот клик запускает цепочку реальной публикации и может списать деньги.

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

    # Один marker имеет два назначения; код обязан различать их контекстом.
    _module_vars = {k: v for k, v in globals().items() if k.isupper()}
    button_next_names = []
    for _name, _val in _module_vars.items():
        if isinstance(_val, str) and "button-next" in _val:
            button_next_names.append(_name)
    assert button_next_names == [
        "CATEGORY_CONFIRM_CONTINUE_BUTTON",
        "FORM_CONTINUE_BUTTON",
    ], button_next_names
    print("[OK] button-next разделён на контекст категории и заполненной формы")

    print("\n=== Все самотесты avito_publish_selectors.py пройдены ===")
