"""
Профили категорий формы публикации Авито.

Профиль — самодостаточный набор данных формы для одной категории: путь в мастере,
контрольные hidden-ID, словари полей и префиксы комбобоксов. Движок publisher.py
один на все категории; меняются только данные профиля.

JACKETS — «Пиджаки и костюмы» (Мужская одежда), значения подтверждены разведкой
2026-06-09 (исторически жили в avito_publish_selectors.py).
SNEAKERS — «Кроссовки» (Мужская обувь), значения подтверждены разведкой 2026-06-29.
TSHIRTS — «Кофты и футболки» (Мужская одежда). У категории есть ДОПОЛНИТЕЛЬНОЕ
поле «Вид товара» (футболка/поло/худи/…), которого нет у пиджаков и кроссовок:
оно описано опциональными item_type_* и заполняется движком только если задано.
Значения ждут живой разведки: python debug/recon_tshirts.py → debug/tshirts_map.txt.
VESTS — «Жилеты» (Мужская одежда → Верхняя одежда). Значения подтверждены
разведкой 2026-08-25.

Самотесты: python backend/category_profiles.py
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CategoryProfile:
    key: str
    label: str
    full_path: tuple[str, ...]
    category_title_text: str
    expected_category: dict[str, str]
    size_options: dict[str, int]
    color_options: dict[str, int]
    trade_type_options: dict[str, int]
    condition_options: dict[str, int]
    size_prefix: str
    color_prefix: str
    trade_type_prefix: str
    condition_param: int
    brand_param: int
    # «Вид товара» — есть только у части категорий (напр. «Кофты и футболки»).
    # Пустой словарь = поля нет: движок и валидация его молча пропускают,
    # фронт не рисует. Оба поля должны быть заданы вместе.
    item_type_options: dict[str, int] = field(default_factory=dict)
    item_type_prefix: str = ""
    # «Материал основной части» — комбобокс с мультивыбором (Авито разрешает
    # до 5 значений, движок выбирает одно). Пустая пара = поля у категории нет.
    material_options: dict[str, int] = field(default_factory=dict)
    material_prefix: str = ""
    # «Стиль» — радиогруппа, как «Состояние». Пустая пара = поля у категории нет.
    style_options: dict[str, int] = field(default_factory=dict)
    style_param: int = 0

    @property
    def has_item_type(self) -> bool:
        """Есть ли у категории поле «Вид товара» (подтип: футболка/поло/…)."""
        return bool(self.item_type_options and self.item_type_prefix)

    @property
    def has_material(self) -> bool:
        """Есть ли у категории поле «Материал основной части» (мультикомбобокс)."""
        return bool(self.material_options and self.material_prefix)

    @property
    def has_style(self) -> bool:
        """Есть ли у категории поле «Стиль» (радио)."""
        return bool(self.style_options and self.style_param)

    def condition_radio(self, option_id: int) -> str:
        """Селектор label радио «Состояние» для опции option_id."""
        return f"[data-marker='params[{self.condition_param}]/{option_id}']"

    def style_radio(self, option_id: int) -> str:
        """Селектор label радио «Стиль» для опции option_id."""
        return f"[data-marker='params[{self.style_param}]/{option_id}']"

    @property
    def brand_input(self) -> str:
        return f"[data-marker='params[{self.brand_param}]/input']"

    @property
    def brand_option(self) -> str:
        return f"[data-marker='params[{self.brand_param}]/option']"


# --- Общие для дерева «Товары» словари/параметры (одинаковы у обеих категорий) ---
_TRADE_TYPE_OPTIONS: dict[str, int] = {
    "Продаю своё": 20029,
    "Товар приобретён на продажу": 20030,
    "Товар от производителя": 20031,
}
_CONDITION_OPTIONS: dict[str, int] = {
    "Новое с биркой": 2804445,
    "Отличное": 431223,
    "Хорошее": 431224,
    "Удовлетворительное": 2804446,
}
_COLOR_OPTIONS: dict[str, int] = {
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
_CONDITION_PARAM = 110385
_BRAND_PARAM = 115634

# --- «Пиджаки и костюмы» ---
_JACKETS_SIZE_OPTIONS: dict[str, int] = {
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

JACKETS = CategoryProfile(
    key="jackets",
    label="Пиджаки и костюмы",
    full_path=("Личные вещи", "Одежда, обувь, аксессуары", "Мужская одежда", "Пиджаки и костюмы"),
    category_title_text="Пиджаки и костюмы",
    expected_category={"category_id": "27", "params[175]": "748", "params[176]": "754"},
    size_options=_JACKETS_SIZE_OPTIONS,
    color_options=_COLOR_OPTIONS,
    trade_type_options=_TRADE_TYPE_OPTIONS,
    condition_options=_CONDITION_OPTIONS,
    size_prefix="razmer",
    color_prefix="cvet",
    trade_type_prefix="type_of_trade",
    condition_param=_CONDITION_PARAM,
    brand_param=_BRAND_PARAM,
)

# --- «Кроссовки» (Мужская обувь) ---
# Значения подтверждены разведкой HTML-дампа 2026-06-29.
# Вид объявления, Состояние, Цвет, Бренд — те же ID, что у пиджаков; общие словари.
SNEAKERS_VERIFIED = True
_SNEAKERS_SIZE_OPTIONS: dict[str, int] = {
    "36": 1211551, "36,5": 1211552, "37": 1211553, "37,5": 1211554,
    "38": 1211555, "38,5": 1211556, "39": 1211557, "39,5": 1211558,
    "40": 1211559, "40,5": 1211560, "41": 1211561, "41,5": 1211562,
    "42": 1211563, "42,5": 1211564, "43": 1211565, "43,5": 1211566,
    "44": 1211567, "44,5": 1211568, "45": 1211569, "45,5": 1211570,
    "46": 1211571, "46,5": 1211572, "47": 1211573, "47,5": 1211574,
    "48+": 1211575,
}

SNEAKERS = CategoryProfile(
    key="sneakers",
    label="Кроссовки",
    full_path=("Личные вещи", "Одежда, обувь, аксессуары", "Мужская обувь", "Кроссовки"),
    category_title_text="Кроссовки",
    expected_category={"category_id": "27", "params[175]": "2804317", "params[118631]": "2262814"},
    size_options=_SNEAKERS_SIZE_OPTIONS,
    color_options=_COLOR_OPTIONS,
    trade_type_options=_TRADE_TYPE_OPTIONS,
    condition_options=_CONDITION_OPTIONS,
    size_prefix="razmer_muzhskaya_obuv",
    color_prefix="cvet",
    trade_type_prefix="type_of_trade",
    condition_param=_CONDITION_PARAM,
    brand_param=_BRAND_PARAM,
)

# --- «Кофты и футболки» (Мужская одежда) ---
# Значения ПОДТВЕРЖДЕНЫ живой разведкой 2026-08-10 (debug/recon_tshirts.py →
# debug/tshirts_map.txt, крошки «Товары / Личные вещи / Одежда, обувь,
# аксессуары / Мужская одежда / Кофты и футболки»).
# Сверено с общими словарями: цвет, вид сделки и состояние — те же ID, что у
# пиджаков; размерная сетка ПОЛНОСТЬЮ совпала с _JACKETS_SIZE_OPTIONS, поэтому
# переиспользуем её, а не дублируем.
# Отличие категории — обязательное поле «Предмет одежды» (комбобокс
# tip_tolstovki_i_svitshoty): футболка/поло/…/кофта.
TSHIRTS_VERIFIED = True

# «Вид товара» на форме Авито подписан «Предмет одежды» (10 подтипов).
_TSHIRTS_ITEM_TYPE_OPTIONS: dict[str, int] = {
    "Футболка": 3255283,
    "Поло": 3255284,
    "Майка": 3255285,
    "Свитшот": 3255286,
    "Толстовка": 3255287,
    "Худи": 3345903,
    "Джемпер": 3255288,
    "Свитер": 3255289,
    "Кардиган": 3255290,
    "Кофта": 3263775,
}

# category_id=27, params[175]=748 — как у пиджаков (Мужская одежда),
# отличается params[176]: 756 вместо 754 (подкатегория «Кофты и футболки»).
_TSHIRTS_EXPECTED_CATEGORY: dict[str, str] = {
    "category_id": "27", "params[175]": "748", "params[176]": "756",
}

TSHIRTS = CategoryProfile(
    key="tshirts",
    label="Кофты и футболки",
    full_path=("Личные вещи", "Одежда, обувь, аксессуары", "Мужская одежда", "Кофты и футболки"),
    category_title_text="Кофты и футболки",
    expected_category=_TSHIRTS_EXPECTED_CATEGORY,
    size_options=_JACKETS_SIZE_OPTIONS,  # сетка идентична пиджакам (разведка 2026-08-10)
    color_options=_COLOR_OPTIONS,
    trade_type_options=_TRADE_TYPE_OPTIONS,
    condition_options=_CONDITION_OPTIONS,
    size_prefix="razmer",
    color_prefix="cvet",
    trade_type_prefix="type_of_trade",
    condition_param=_CONDITION_PARAM,
    brand_param=_BRAND_PARAM,
    item_type_options=_TSHIRTS_ITEM_TYPE_OPTIONS,
    item_type_prefix="tip_tolstovki_i_svitshoty",
)

# --- «Демисезонные куртки» (Мужская одежда → Верхняя одежда) ---
# Путь и DOM-данные подтверждены безопасной read-only разведкой 2026-08-22.
# У категории есть необязательный combobox material_osnovnoi_chasti
# (params[164867][]); текущая модель профиля не заполняет необязательные поля.
DEMI_JACKETS_VERIFIED = True
DEMI_JACKETS = CategoryProfile(
    key="demi_jackets",
    label="Демисезонные куртки",
    full_path=(
        "Личные вещи",
        "Одежда, обувь, аксессуары",
        "Мужская одежда",
        "Верхняя одежда",
        "Демисезонные куртки",
    ),
    category_title_text="Демисезонные куртки",
    expected_category={
        "category_id": "27",
        "params[175]": "748",
        "params[176]": "751",
        "params[114145]": "908138",
    },
    size_options=_JACKETS_SIZE_OPTIONS,
    color_options=_COLOR_OPTIONS,
    trade_type_options=_TRADE_TYPE_OPTIONS,
    condition_options=_CONDITION_OPTIONS,
    size_prefix="razmer",
    color_prefix="cvet",
    trade_type_prefix="type_of_trade",
    condition_param=_CONDITION_PARAM,
    brand_param=_BRAND_PARAM,
)

# --- «Жилеты» (Мужская одежда → Верхняя одежда) ---
# Путь и DOM-данные подтверждены живой разведкой 2026-08-25
# (debug/recon_vests.py → debug/vests_map.txt). Размерная сетка, цвета,
# вид сделки, состояние и бренд полностью совпали с общими словарями.
# У категории есть два ДОПОЛНИТЕЛЬНЫХ обязательных поля, которых нет у
# пиджаков/кроссовок/футболок/демисезонных курток:
#   - combobox material_osnovnoi_chasti («Материал основной части», 54
#     варианта, Авито разрешает до 5 значений — движок выбирает одно);
#   - радио params[192478] «Стиль» (Деловой/Оверсайз/Повседневный/…).
# Сознательно НЕ заполняется свободнотекстовое поле «Цвет от производителя».
VESTS_VERIFIED = True

# «Материал основной части» — снят разведкой 2026-08-25 из debug/vests_map.txt.
_VESTS_MATERIAL_OPTIONS: dict[str, int] = {
    "Акрил": 3263862,
    "Ангора": 3263859,
    "Атлас": 3263849,
    "Ацетат": 3263870,
    "Байка": 3263874,
    "Бамбук": 3263869,
    "Бархат": 3263852,
    "Бифлекс": 3263857,
    "Болонь": 3263858,
    "Вельвет": 3263851,
    "Велюр": 3263853,
    "Вискоза": 3263850,
    "Джинса/деним": 3263844,
    "Драп": 3263856,
    "Жаккард": 3263878,
    "Искусственная замша": 3263863,
    "Искусственный мех": 3263842,
    "Кашемир": 3263848,
    "Креп": 3263865,
    "Лайкра": 3263864,
    "Лён": 3263833,
    "Люрекс": 3263881,
    "Микрофибра": 3263876,
    "Модал": 3263880,
    "Мохер": 3263883,
    "Муслин": 3263882,
    "Натуральная замша": 3263866,
    "Натуральная кожа": 3263838,
    "Натуральный": 3263885,
    "Натуральный мех": 3263841,
    "Нейлон": 3263861,
    "Неопрен": 3263879,
    "Норка": 3263871,
    "Овчина": 3263860,
    "Плащевка": 3263875,
    "Плюш": 3263847,
    "Полиамид": 3343296,
    "Полиэстер": 3263843,
    "Сатин": 3263877,
    "Синтетический": 3263884,
    "Спандекс": 3263836,
    "Таслан": 3263872,
    "Твид": 3263854,
    "Тинсулейт": 3263868,
    "Флис": 3263837,
    "Футер": 3263855,
    "Хлопок": 3263834,
    "Шёлк": 3263845,
    "Шерпа": 3263873,
    "Шерсть": 3263840,
    "Шифон": 3263846,
    "Экокожа": 3263839,
    "Экомех": 3263867,
    "Эластан": 3263835,
}

# «Стиль» — снят разведкой 2026-08-25 из debug/vests_map.txt.
_VESTS_STYLE_OPTIONS: dict[str, int] = {
    "Деловой": 3360003,
    "Оверсайз": 3360004,
    "Повседневный": 3360005,
    "Спортивный": 3360006,
    "Другой": 3360007,
}

VESTS = CategoryProfile(
    key="vests",
    label="Жилеты",
    full_path=(
        "Личные вещи",
        "Одежда, обувь, аксессуары",
        "Мужская одежда",
        "Верхняя одежда",
        "Жилеты",
    ),
    category_title_text="Жилеты",
    expected_category={
        "category_id": "27",
        "params[175]": "748",
        "params[176]": "751",
        "params[114145]": "908141",
    },
    size_options=_JACKETS_SIZE_OPTIONS,  # сетка идентична пиджакам (разведка 2026-08-25)
    color_options=_COLOR_OPTIONS,
    trade_type_options=_TRADE_TYPE_OPTIONS,
    condition_options=_CONDITION_OPTIONS,
    size_prefix="razmer",
    color_prefix="cvet",
    trade_type_prefix="type_of_trade",
    condition_param=_CONDITION_PARAM,
    brand_param=_BRAND_PARAM,
    material_options=_VESTS_MATERIAL_OPTIONS,
    material_prefix="material_osnovnoi_chasti",
    style_options=_VESTS_STYLE_OPTIONS,
    style_param=192478,
)

PROFILES: dict[str, CategoryProfile] = {"jackets": JACKETS, "sneakers": SNEAKERS}
if TSHIRTS_VERIFIED:
    PROFILES["tshirts"] = TSHIRTS
if DEMI_JACKETS_VERIFIED:
    PROFILES["demi_jackets"] = DEMI_JACKETS
if VESTS_VERIFIED:
    PROFILES["vests"] = VESTS
DEFAULT_CATEGORY = "jackets"


def get_profile(key: str | None) -> CategoryProfile:
    """Профиль по ключу; неизвестный/пустой ключ → дефолтная категория."""
    return PROFILES.get((key or "").strip(), PROFILES[DEFAULT_CATEGORY])


if __name__ == "__main__":
    # Реестр и дефолт: tshirts появляется в реестре только после разведки
    _expected_registry = (
        {"jackets", "sneakers"}
        | ({"tshirts"} if TSHIRTS_VERIFIED else set())
        | ({"demi_jackets"} if DEMI_JACKETS_VERIFIED else set())
        | ({"vests"} if VESTS_VERIFIED else set())
    )
    assert set(PROFILES) == _expected_registry, set(PROFILES)
    assert get_profile(None) is JACKETS
    assert get_profile("") is JACKETS
    assert get_profile("нет такой") is JACKETS
    assert get_profile("sneakers") is SNEAKERS
    print("[OK] Реестр и get_profile")

    # У каждого профиля словари непусты, option_id уникальны
    for p in PROFILES.values():
        for nm, d in (("size", p.size_options), ("color", p.color_options),
                      ("trade_type", p.trade_type_options), ("condition", p.condition_options)):
            assert d, f"{p.key}: пустой словарь {nm}"
            ids = list(d.values())
            assert len(ids) == len(set(ids)), f"{p.key}/{nm}: дубли ID"
    print("[OK] Словари профилей непусты и без дублей ID")

    # Регрессия JACKETS — исторические значения «Пиджаков»
    assert JACKETS.expected_category == {"category_id": "27", "params[175]": "748", "params[176]": "754"}
    assert len(JACKETS.size_options) == 24 and len(JACKETS.color_options) == 17
    assert len(JACKETS.trade_type_options) == 3 and len(JACKETS.condition_options) == 4
    assert JACKETS.condition_radio(431223) == "[data-marker='params[110385]/431223']"
    assert JACKETS.brand_input == "[data-marker='params[115634]/input']"
    assert JACKETS.brand_option == "[data-marker='params[115634]/option']"
    print("[OK] JACKETS: исторические значения сохранены")

    # SNEAKERS: путь, категория и размеры подтверждены разведкой 2026-06-29
    assert SNEAKERS.full_path[-1] == "Кроссовки" == SNEAKERS.category_title_text
    assert SNEAKERS_VERIFIED is True
    assert all(v > 0 for v in SNEAKERS.size_options.values()), "остались плейсхолдеры размеров"
    assert len(SNEAKERS.size_options) == 25, f"ожидали 25 размеров, получили {len(SNEAKERS.size_options)}"
    assert SNEAKERS.expected_category, "expected_category у SNEAKERS пусто"
    assert SNEAKERS.size_prefix == "razmer_muzhskaya_obuv"
    print("[OK] SNEAKERS: данные разведки вписаны, плейсхолдеров нет")

    # У пиджаков и кроссовок «Вида товара» нет — движок не должен его искать
    assert not JACKETS.has_item_type and not SNEAKERS.has_item_type
    print("[OK] has_item_type: у jackets/sneakers поля нет")

    # TSHIRTS: пока не подтверждён — не в реестре; подтверждён — данные полные.
    assert TSHIRTS.full_path[-1] == "Кофты и футболки" == TSHIRTS.category_title_text
    assert TSHIRTS.full_path[2] == "Мужская одежда", TSHIRTS.full_path
    if not TSHIRTS_VERIFIED:
        assert "tshirts" not in PROFILES, "непроверенный профиль просочился в реестр"
        assert get_profile("tshirts") is JACKETS, "неизвестный ключ → дефолт"
        print("[OK] TSHIRTS: ждёт разведки (debug/recon_tshirts.py), в реестр не попал")
    else:
        assert PROFILES.get("tshirts") is TSHIRTS
        assert TSHIRTS.expected_category, "expected_category пусто"
        assert TSHIRTS.size_prefix, "size_prefix пуст"
        assert TSHIRTS.size_options and all(v > 0 for v in TSHIRTS.size_options.values())
        # «Вид товара» задаётся парой префикс+словарь: половина данных = ошибка
        assert TSHIRTS.has_item_type, "у футболок ожидается «Вид товара»"
        assert all(v > 0 for v in TSHIRTS.item_type_options.values())
        # Регрессия значений разведки 2026-08-10
        assert TSHIRTS.expected_category == {
            "category_id": "27", "params[175]": "748", "params[176]": "756",
        }, TSHIRTS.expected_category
        assert TSHIRTS.item_type_prefix == "tip_tolstovki_i_svitshoty"
        assert len(TSHIRTS.item_type_options) == 10, len(TSHIRTS.item_type_options)
        assert TSHIRTS.item_type_options["Футболка"] == 3255283
        assert TSHIRTS.item_type_options["Худи"] == 3345903
        # Размерная сетка переиспользована от пиджаков — не копия, тот же объект
        assert TSHIRTS.size_options == JACKETS.size_options
        # Категория отличается от пиджаков только подкатегорией params[176]
        assert TSHIRTS.expected_category["params[176]"] != JACKETS.expected_category["params[176]"]
        print(f"[OK] TSHIRTS: разведка вписана ({len(TSHIRTS.size_options)} размеров, "
              f"{len(TSHIRTS.item_type_options)} видов товара)")

    # DEMI_JACKETS: DOM-данные сняты безопасной read-only разведкой 2026-08-22.
    assert DEMI_JACKETS.full_path == (
        "Личные вещи", "Одежда, обувь, аксессуары", "Мужская одежда",
        "Верхняя одежда", "Демисезонные куртки",
    )
    assert PROFILES.get("demi_jackets") is DEMI_JACKETS
    assert DEMI_JACKETS.expected_category == {
        "category_id": "27", "params[175]": "748", "params[176]": "751",
        "params[114145]": "908138",
    }
    assert DEMI_JACKETS.size_options == JACKETS.size_options
    assert DEMI_JACKETS.color_options == _COLOR_OPTIONS
    assert DEMI_JACKETS.trade_type_options == _TRADE_TYPE_OPTIONS
    assert DEMI_JACKETS.condition_options == _CONDITION_OPTIONS
    assert DEMI_JACKETS.size_prefix == "razmer"
    assert DEMI_JACKETS.color_prefix == "cvet"
    assert DEMI_JACKETS.trade_type_prefix == "type_of_trade"
    print("[OK] DEMI_JACKETS: данные разведки вписаны и профиль включён")

    # VESTS: пока не подтверждён — не в реестре; подтверждён — данные полные.
    assert VESTS.full_path[-1] == "Жилеты" == VESTS.category_title_text
    assert VESTS.full_path[3] == "Верхняя одежда"
    assert not VESTS.has_item_type
    if not VESTS_VERIFIED:
        assert "vests" not in PROFILES, "непроверенный профиль просочился в реестр"
        assert get_profile("vests") is JACKETS, "неизвестный ключ → дефолт"
        print("[OK] VESTS: ждёт разведки (debug/recon_vests.py), в реестр не попал")
    else:
        assert PROFILES.get("vests") is VESTS
        assert VESTS.expected_category == {
            "category_id": "27", "params[175]": "748", "params[176]": "751",
            "params[114145]": "908141",
        }
        assert VESTS.size_options == JACKETS.size_options
        assert (VESTS.color_options == _COLOR_OPTIONS
                and VESTS.trade_type_options == _TRADE_TYPE_OPTIONS
                and VESTS.condition_options == _CONDITION_OPTIONS)
        assert (VESTS.size_prefix == "razmer" and VESTS.color_prefix == "cvet"
                and VESTS.trade_type_prefix == "type_of_trade")
        # Жилеты отличаются от демисезонных курток только подтипом верхней одежды
        assert VESTS.expected_category["params[114145]"] != DEMI_JACKETS.expected_category["params[114145]"]
        # Материал и стиль — дополнительные поля, специфичные для жилетов
        assert len(VESTS.material_options) == 54, len(VESTS.material_options)
        assert len(VESTS.style_options) == 5, len(VESTS.style_options)
        assert VESTS.has_material and VESTS.has_style
        assert VESTS.material_prefix == "material_osnovnoi_chasti"
        assert VESTS.style_param == 192478
        assert VESTS.style_radio(3360005) == "[data-marker='params[192478]/3360005']"
        assert VESTS.material_options["Нейлон"] == 3263861
        assert VESTS.style_options["Повседневный"] == 3360005
        assert len(set(VESTS.material_options.values())) == len(VESTS.material_options), "дубли ID материалов"
        assert len(set(VESTS.style_options.values())) == len(VESTS.style_options), "дубли ID стилей"
        print("[OK] VESTS: данные разведки вписаны и профиль включён")

    # Инвариант для всех профилей: item_type задан парой (префикс + словарь)
    for p in list(PROFILES.values()) + [TSHIRTS, VESTS]:
        assert bool(p.item_type_options) == bool(p.item_type_prefix), (
            f"{p.key}: item_type_options и item_type_prefix должны быть заданы вместе"
        )
    print("[OK] Инвариант item_type (префикс и словарь — вместе)")

    # Инвариант для всех профилей: material/style тоже задаются парой
    for p in list(PROFILES.values()) + [TSHIRTS, VESTS]:
        assert bool(p.material_options) == bool(p.material_prefix), (
            f"{p.key}: material_options и material_prefix должны быть заданы вместе"
        )
        assert bool(p.style_options) == bool(p.style_param), (
            f"{p.key}: style_options и style_param должны быть заданы вместе"
        )
    assert not JACKETS.has_material and not JACKETS.has_style
    assert not DEMI_JACKETS.has_material and not DEMI_JACKETS.has_style
    print("[OK] Инвариант material/style (парой) + jackets/demi_jackets без этих полей")

    print("\n=== Все самотесты category_profiles.py пройдены ===")
