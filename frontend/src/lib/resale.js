// Утилиты страницы «Поиск под перепродажу» (docs/specs/resale-finder.md):
// хранение формы в браузере, подписи стадий прогресса и разбор ошибок 422.

export const RESALE_STORAGE_KEY = "avito-resale-form-v1";

export const DEFAULT_RESALE_FORM = {
  query: "",
  city: "",
  threshold_pct: 20,
  max_items: 150,
  price_min: "",
  price_max: "",
};

function normalizeForm(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { ...DEFAULT_RESALE_FORM };
  }

  const thresholdPct = Number(value.threshold_pct);
  const maxItems = Number(value.max_items);

  return {
    query: typeof value.query === "string" ? value.query : "",
    city: typeof value.city === "string" ? value.city : "",
    threshold_pct:
      Number.isFinite(thresholdPct) && thresholdPct >= 5 && thresholdPct <= 80
        ? thresholdPct
        : DEFAULT_RESALE_FORM.threshold_pct,
    max_items:
      Number.isInteger(maxItems) && maxItems >= 20 && maxItems <= 300
        ? maxItems
        : DEFAULT_RESALE_FORM.max_items,
    price_min: typeof value.price_min === "string" ? value.price_min : "",
    price_max: typeof value.price_max === "string" ? value.price_max : "",
  };
}

export function createResaleStorage(storage) {
  return {
    load() {
      try {
        const raw = storage.getItem(RESALE_STORAGE_KEY);
        return raw ? normalizeForm(JSON.parse(raw)) : { ...DEFAULT_RESALE_FORM };
      } catch {
        return { ...DEFAULT_RESALE_FORM };
      }
    },
    save(form) {
      storage.setItem(RESALE_STORAGE_KEY, JSON.stringify(normalizeForm(form)));
    },
  };
}

// Подписи стадий прогресса run_resale_scan (collecting/classifying/topup/done)
const RESALE_STAGE_LABELS = {
  collecting: "Сбор объявлений",
  classifying: "Разбор ИИ",
  topup: "Уточняющий поиск",
  done: "Готово",
};

export function resaleStageLabel(stage) {
  return RESALE_STAGE_LABELS[stage] || "Выполняем в Chrome…";
}

// Статусы, на которых опрос останавливается без результата (как в разборе
// продавца — см. STOPPED_STATUSES в SellerScanPage.jsx)
export const RESALE_STOPPED_STATUSES = new Set(["blocked", "error", "not_found"]);

// Разбор ошибок валидации 422 — формат {errors: [{field, error}]}, как у
// публикации (extractPublishFieldErrors в lib/publish.js). Продублировано
// здесь маленькой функцией, чтобы страница перепродажи не зависела от
// модуля публикации (разные, не связанные сценарии).
export function extractResaleFieldErrors(payload) {
  if (!Array.isArray(payload?.errors)) {
    if (payload?.error) {
      return { general: payload.error };
    }
    return {};
  }

  const fieldErrors = {};
  for (const err of payload.errors) {
    const field = typeof err?.field === "string" ? err.field : "general";
    fieldErrors[field] = err?.error || "Неверное значение";
  }
  return fieldErrors;
}
