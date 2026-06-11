// Утилиты сценария публикации черновика: сборка FormData и разбор ошибок.
// Имена полей формы на фронте совпадают с параметрами /api/publish/start,
// поэтому маппинг имён не нужен.

// Поля формы черновика (без фото) — серверные имена /api/publish/start
const PUBLISH_FIELD_NAMES = [
  "title",
  "trade_type",
  "condition",
  "size",
  "brand",
  "color",
  "description",
  "price",
  "city",
  "address",
  "drafts_count",
];

// Терминальные статусы задачи публикации (контракт /api/publish/status)
export const TERMINAL_STATUSES = new Set(["done", "failed", "needs_user_action"]);

export function buildPublishFormData(form, photos) {
  const formData = new FormData();

  for (const name of PUBLISH_FIELD_NAMES) {
    formData.append(name, String(form[name]).trim());
  }

  for (const file of photos) {
    formData.append("photos", file);
  }

  return formData;
}

// ─── Вспомогательные функции фазы превью ─────────────────────────────────────

// Заменяет одну карточку черновика в массиве (возвращает новый массив)
export function replaceDraftCard(drafts, index, newCard) {
  return drafts.map((card, i) => (i === index ? newCard : card));
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
