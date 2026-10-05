// Утилиты сценария полной публикации: FormData, прогресс и разбор ошибок.
// Имена полей формы на фронте совпадают с параметрами /api/publish/start,
// поэтому маппинг имён не нужен.

// Поля формы черновика (без фото) — серверные имена /api/publish/start
const PUBLISH_FIELD_NAMES = [
  "category",
  "title",
  "trade_type",
  "condition",
  "size",
  "brand",
  "color",
  "description",
  "price",
  "drafts_count",
  // Вид товара: есть не у всех категорий. Для остальных уходит пустой строкой —
  // бэкенд такое поле игнорирует (validate_publish_form: profile.has_item_type)
  "item_type",
  // Материал основной части: есть не у всех категорий. Для остальных уходит
  // пустой строкой — бэкенд игнорирует (validate_publish_form: profile.has_material)
  "material",
  // Стиль: есть не у всех категорий. Для остальных уходит пустой строкой —
  // бэкенд игнорирует (validate_publish_form: profile.has_style)
  "style",
];

// Терминальные статусы задачи публикации (контракт /api/publish/status)
export const TERMINAL_STATUSES = new Set([
  "done",
  "failed",
  "needs_user_action",
  "interrupted",
]);
export const MAX_DRAFTS = 20;

export function normalizePublishProgress(status = {}) {
  const source = status ?? {};
  const has = (key) => Object.prototype.hasOwnProperty.call(source, key);
  const rawUrls = has("published_urls") ? source.published_urls : source.saved_urls;

  return {
    itemsTotal: has("items_total") ? source.items_total : source.drafts_total ?? null,
    itemIndex: has("item_index") ? source.item_index : source.draft_index ?? null,
    itemsPublished: has("items_published") ? source.items_published : source.drafts_saved ?? 0,
    publishedUrls: Array.isArray(rawUrls) ? rawUrls : [],
    appliedViewPrices: Array.isArray(source.applied_view_prices)
      ? source.applied_view_prices
      : [],
    addressWarnings: Array.isArray(source.address_warnings)
      ? source.address_warnings
      : [],
    brandSelected: typeof source.brand_selected === "string"
      ? source.brand_selected
      : null,
    skippedItems: Array.isArray(source.skipped_items) ? source.skipped_items : [],
  };
}

export function canResumePublish(status = {}) {
  const { itemsTotal, itemsPublished } = normalizePublishProgress(status);
  return (
    status?.resume_available === true
    && itemsTotal != null
    && itemsPublished < itemsTotal
  );
}

// Терминальные статусы, при которых возобновление в принципе рассматривается
// (совпадает с /api/publish/status: resume_available считается только для них)
const RESUMABLE_STATUSES = new Set(["failed", "needs_user_action", "interrupted"]);

// Шаги пакета начиная с денежного клика «Продолжить» (совпадает по именам с
// backend publish_state.FINANCIAL_STEPS) — используются здесь ТОЛЬКО для
// формулировок на экране. Само решение «можно ли продолжать автоматически»
// по-прежнему целиком у бэкенда (resume_plan/resume_available), тут его не
// повторяем.
const FINANCIAL_STEPS = new Set([
  "continue_listing",
  "fill_view_price",
  "continue_view_price",
  "skip_services",
  "done",
]);

// Шаги ДО подтверждения стоимости просмотра («Продолжить с минимальной
// ценой»): объявление уже может быть создано (после continue_listing), но
// показ точно ещё не оплачен.
const STEPS_BEFORE_VIEW_PRICE_CONFIRMED = new Set(["continue_listing", "fill_view_price"]);

// F26: на текущем шаге уже есть созданное, но не доведённое до конца
// объявление — его номер не входит в items_published, и его нужно явно
// вычитать из «остальных»/«оставшихся», иначе счётчик врёт.
export function hasStuckCreatedItem(status = {}) {
  return FINANCIAL_STEPS.has(status?.step);
}

// F05: один и тот же текст «создано, но не оплачено» был неверен, если
// остановка случилась уже после оплаты просмотра (на экране отказа от услуг).
export function describeStuckItemPayment(status = {}) {
  if (!hasStuckCreatedItem(status)) return null;
  return STEPS_BEFORE_VIEW_PRICE_CONFIRMED.has(status?.step)
    ? "но не оплачено"
    : "просмотр уже оплачен — проверьте, не подключились ли платные услуги, прежде чем платить снова";
}

// F26: «оставшиеся»/«ещё не начинались» без вычета застрявшего объявления
// врали — оно не входит в items_published (растёт только после полного
// успеха), но и не «ещё не начиналось».
export function countUnstartedItems(status = {}) {
  const { itemsTotal, itemsPublished } = normalizePublishProgress(status);
  if (itemsTotal == null) return 0;
  const stuck = hasStuckCreatedItem(status) ? 1 : 0;
  return Math.max(itemsTotal - itemsPublished - stuck, 0);
}

// Кнопка/пояснение возобновления зависят от режима resume_plan с бэкенда:
// retry_item — повтор текущего объявления (ничего платного ещё не случилось),
// skip_item — текущее уже создано на Авито и не трогается, едем со следующего.
// Текст собран в одном месте, чтобы кнопка и пояснение не расходились.
export function describeResumePlan(status = {}) {
  const plan = status?.resume_plan;
  if (!plan) return null;

  if (plan.mode === "skip_item") {
    const payment = describeStuckItemPayment(status) || "но оплата не подтверждена";
    return {
      buttonLabel: "Продолжить со следующего",
      description: (
        `Продолжим с объявления №${plan.start_index} из ${plan.items_total}. `
        + `Объявление №${plan.skipped_item} уже создано на Авито, ${payment} — `
        + "автоматика его повторять не будет, проверьте и завершите его вручную."
      ),
    };
  }

  return {
    buttonLabel: "Продолжить публикацию",
    description: `Продолжим с объявления №${plan.start_index} из ${plan.items_total}.`,
  };
}

// Честное объяснение отсутствия кнопки — дословно причина с бэкенда
// (resume_unavailable_reason), а не заранее заготовленный текст здесь: причины
// разные (последнее объявление пакета, задача ещё завершается, шаг помечен
// неповторяемым...), и раньше фронт их не различал (F30, F08, S2).
export function resumeUnavailableMessage(status = {}) {
  const { itemsTotal, itemsPublished } = normalizePublishProgress(status);
  if (!RESUMABLE_STATUSES.has(status?.status)) return null;
  if (itemsTotal == null || itemsPublished >= itemsTotal) return null;
  if (status?.resume_available === true) return null;
  return status?.resume_unavailable_reason
    || "Продолжение недоступно — проверьте кабинет Авито вручную.";
}

// Дата задачи для баннера незавершённой публикации (F01) — без библиотек.
export function formatJobCreatedAt(createdAt) {
  const date = createdAt ? new Date(createdAt) : null;
  if (!date || Number.isNaN(date.getTime())) return "неизвестной даты";
  return date.toLocaleString("ru-RU", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export function publishButtonLabel(count) {
  const value = Number(count) || 0;
  const mod100 = Math.abs(value) % 100;
  const mod10 = mod100 % 10;
  const noun = mod100 >= 11 && mod100 <= 14
    ? "объявлений"
    : mod10 === 1
      ? "объявление"
      : mod10 >= 2 && mod10 <= 4
        ? "объявления"
        : "объявлений";
  return `Отправить ${value} ${noun} на Авито`;
}

export function resizeLocations(locations, count) {
  const targetCount = Number.isInteger(Number(count)) && Number(count) > 0
    ? Number(count)
    : 0;
  const source = Array.isArray(locations) ? locations : [];

  return Array.from({ length: targetCount }, (_, index) => {
    const location = source[index];
    return {
      city: typeof location?.city === "string" ? location.city : "",
      address: typeof location?.address === "string" ? location.address : "",
    };
  });
}

export function normalizeViewPrice(value) {
  const text = String(value ?? "").trim();
  if (!/^\d+(?:[.,]\d+)?$/.test(text)) return null;
  if (!/[1-9]/.test(text)) return null;
  return text.replace(",", ".");
}

const ADDRESS_TYPE_PATTERN = /(?:улица|ул\.|проспект|пр-т|переулок|пер\.|шоссе|набережная|наб\.|бульвар|бул\.|площадь|пл\.|проезд|аллея|линия|микрорайон|мкр\.|тракт)/iu;

// Номер дома не обязателен: Авито принимает и адрес до улицы.
// Отсекаем только строки без типа улицы и без запятой («королева 26»),
// которые Авито сводит к городу.
export function isStructuredAddress(value) {
  const text = String(value ?? "").trim();
  return text.includes(",") || ADDRESS_TYPE_PATTERN.test(text);
}

export function validateLocations(locations, count) {
  const targetCount = Number(count);
  if (!Array.isArray(locations) || locations.length !== targetCount) {
    return {
      locations: "Количество геолокаций должно совпадать с количеством объявлений",
    };
  }

  const errors = {};
  locations.forEach((location, index) => {
    const itemNumber = index + 1;
    if (typeof location?.city !== "string" || !location.city.trim()) {
      errors[`locations.${itemNumber}.city`] = `Укажите город для объявления №${itemNumber}`;
    }
    if (typeof location?.address !== "string" || !location.address.trim()) {
      errors[`locations.${itemNumber}.address`] = `Укажите улицу для объявления №${itemNumber}`;
    } else if (!isStructuredAddress(location.address)) {
      errors[`locations.${itemNumber}.address`] = "Добавьте тип улицы (например, «Тверская улица») или отделите дом запятой";
    }
  });
  return errors;
}

export function buildPublishFormData(form, photos) {
  const formData = new FormData();

  for (const name of PUBLISH_FIELD_NAMES) {
    formData.append(name, String(form[name]).trim());
  }

  const locations = resizeLocations(form.locations, Number(form.drafts_count)).map(
    ({ city, address }) => ({ city: city.trim(), address: address.trim() }),
  );
  formData.append("locations_json", JSON.stringify(locations));
  const normalizedViewPriceMax = normalizeViewPrice(form.view_price_max);
  formData.append(
    "view_price_max",
    normalizedViewPriceMax ?? String(form.view_price_max ?? "").trim().replace(",", "."),
  );

  for (const file of photos) {
    formData.append("photos", file);
  }

  return formData;
}

// ─── Вспомогательные функции фазы превью ─────────────────────────────────────

// Собирает FormData запуска публикации из превью: бэкенд валидирует ПОЛНУЮ
// форму даже при наличии prep_id, поэтому шлём те же поля и фото, что и при
// обычном запуске (drafts_count — в форме), плюс prep_id с готовыми вариантами
export function buildLaunchFormData(form, photos, prepId) {
  const formData = buildPublishFormData(form, photos);
  formData.append("prep_id", prepId);
  return formData;
}

// Заменяет карточку черновика на новую с тем же index (1-based, как в
// контракте /api/publish/prepare/result); возвращает новый массив
export function replaceDraftCard(drafts, newCard) {
  return drafts.map((card) => (card.index === newCard.index ? newCard : card));
}

// Строит URL к фото черновика для <img src> (аналог prepPhotoUrl из api.js,
// вынесен сюда чтобы быть доступным в юнит-тестах без браузерного окружения)
export function buildPrepPhotoUrl(prepId, draftIndex, photoIndex) {
  return `/api/publish/prepare/photo/${prepId}/${draftIndex}/${photoIndex}`;
}

export function extractPublishFieldErrors(payload) {
  // Нормализуем оба формата в список пар [поле, сообщение]:
  // наш бэкенд шлёт {errors: [{field, error}]}, FastAPI — {detail: [{loc, msg}]}
  let pairs = null;

  if (Array.isArray(payload?.errors)) {
    pairs = payload.errors.map((err) => [
      typeof err?.field === "string" ? err.field : "general",
      err?.error,
    ]);
  } else if (Array.isArray(payload?.detail)) {
    pairs = payload.detail.map((err) => [
      Array.isArray(err?.loc) ? err.loc[err.loc.length - 1] : "general",
      err?.msg,
    ]);
  }

  if (pairs === null) {
    if (payload?.error || payload?.detail) {
      return { general: payload.error || payload.detail };
    }
    return {};
  }

  const fieldErrors = {};
  for (const [field, message] of pairs) {
    fieldErrors[field] = message || "Неверное значение";
  }
  return fieldErrors;
}
