"""
Профили категорий формы публикации Авито.

Профиль — самодостаточный набор данных формы для одной категории: путь в мастере,
контрольные hidden-ID, словари полей и префиксы комбобоксов. Движок publisher.py
один на все категории; меняются только данные профиля.

JACKETS — «Пиджаки и костюмы» (Мужская одежда), значения подтверждены разведкой
2026-06-09 (исторически жили в avito_publish_selectors.py).
SNEAKERS — «Кроссовки» (Мужская обувь): путь известен, option_id размеров и
hidden-ID заполняются из debug/sneakers_map.txt (recon, см. план Task 1/8).

Самотесты: python backend/category_profiles.py
"""
from dataclasses import dataclass


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

    def condition_radio(self, option_id: int) -> str:
        """Селектор label радио «Состояние» для опции option_id."""
        return f"[data-marker='params[{self.condition_param}]/{option_id}']"

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
# ПЛЕЙСХОЛДЕРЫ до разведки (Task 1/8). Реальные option_id — из debug/sneakers_map.txt.
SNEAKERS_VERIFIED = False
_SNEAKERS_SIZE_OPTIONS: dict[str, int] = {
    # TODO(recon): заменить на реальные пары «размер → option_id» из sneakers_map.txt
    "41": -41, "42": -42, "43": -43, "44": -44, "45": -45, "46": -46,
}

SNEAKERS = CategoryProfile(
    key="sneakers",
    label="Кроссовки",
    full_path=("Личные вещи", "Одежда, обувь, аксессуары", "Мужская обувь", "Кроссовки"),
    category_title_text="Кроссовки",
    expected_category={},  # TODO(recon): hidden-ID из sneakers_map.txt; пусто → проверка по крошке
    size_options=_SNEAKERS_SIZE_OPTIONS,
    color_options=_COLOR_OPTIONS,
    trade_type_options=_TRADE_TYPE_OPTIONS,
    condition_options=_CONDITION_OPTIONS,
    size_prefix="razmer",  # TODO(recon): подтвердить префикс комбобокса размера обуви
    color_prefix="cvet",
    trade_type_prefix="type_of_trade",
    condition_param=_CONDITION_PARAM,
    brand_param=_BRAND_PARAM,
)

PROFILES: dict[str, CategoryProfile] = {"jackets": JACKETS, "sneakers": SNEAKERS}
DEFAULT_CATEGORY = "jackets"


def get_profile(key: str | None) -> CategoryProfile:
    """Профиль по ключу; неизвестный/пустой ключ → дефолтная категория."""
    return PROFILES.get((key or "").strip(), PROFILES[DEFAULT_CATEGORY])


if __name__ == "__main__":
    # Реестр и дефолт
    assert set(PROFILES) == {"jackets", "sneakers"}
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

    # SNEAKERS: путь и крошка известны; пометка о неподтверждённости
    assert SNEAKERS.full_path[-1] == "Кроссовки" == SNEAKERS.category_title_text
    assert SNEAKERS_VERIFIED is False  # снимется в Task 8 после разведки
    print("[OK] SNEAKERS: путь известен, помечен как неподтверждённый")

    print("\n=== Все самотесты category_profiles.py пройдены ===")
