export const OUTREACH_STORAGE_KEY = "avito-outreach-form-v1";

export const DEFAULT_OUTREACH_FORM = {
  city: "",
  query: "",
  limit: 15,
  message: "",
};

function normalizeForm(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { ...DEFAULT_OUTREACH_FORM };
  }

  const limit = Number(value.limit);
  return {
    city: typeof value.city === "string" ? value.city : "",
    query: typeof value.query === "string" ? value.query : "",
    limit: Number.isInteger(limit) && limit >= 1 && limit <= 150 ? limit : 15,
    message: typeof value.message === "string" ? value.message : "",
  };
}

export function serializeOutreachForm(form) {
  return JSON.stringify(normalizeForm(form));
}

export function createOutreachStorage(storage) {
  return {
    load() {
      try {
        const raw = storage.getItem(OUTREACH_STORAGE_KEY);
        return raw ? normalizeForm(JSON.parse(raw)) : { ...DEFAULT_OUTREACH_FORM };
      } catch {
        return { ...DEFAULT_OUTREACH_FORM };
      }
    },
    save(form) {
      storage.setItem(OUTREACH_STORAGE_KEY, serializeOutreachForm(form));
    },
  };
}

export function buildSendPayload(candidates, message) {
  return {
    candidate_keys: candidates.filter((candidate) => candidate.selected).map((candidate) => candidate.seller_key),
    message,
    confirmed: true,
  };
}

// ─── Возобновление рассылки (/api/outreach/status, /api/outreach/resume) ──────
// Зеркало publish.js (describeResumePlan/resumeUnavailableMessage), но про
// продавцов и письма, а не про объявления и оплату.

// Терминальные статусы job'а рассылки, при которых продолжение вообще
// рассматривается (совпадает с тем, что реально шлёт бэкенд для отправки).
const RESUMABLE_STATUSES = new Set(["failed", "needs_user_action", "interrupted"]);

// Кнопка/пояснение продолжения зависят от режима resume_plan с бэкенда:
// retry_item — клика по отправке этому продавцу ещё не было, можно повторить,
// skip_item — клик уже сделан, письмо могло уйти — продавца не трогаем,
// едем со следующего. Текст собран в одном месте, чтобы кнопка и пояснение
// не расходились. Безопасность повтора целиком решает бэкенд через resume_plan.
export function describeOutreachResumePlan(status = {}) {
  const plan = status?.resume_plan;
  if (!plan) return null;

  if (plan.mode === "skip_item") {
    return {
      buttonLabel: "Продолжить со следующего",
      description: (
        `Продолжим с продавца №${plan.start_index} из ${plan.items_total}. `
        + `Продавцу №${plan.skipped_item} письмо могло уйти — `
        + "автоматика к нему больше не притронется, проверьте переписку вручную."
      ),
    };
  }

  return {
    buttonLabel: "Повторить текущего",
    description: (
      `Продолжим с продавца №${plan.start_index} из ${plan.items_total}. `
      + "Этому продавцу письмо ещё не отправлялось."
    ),
  };
}

export function outreachResumeActions(status = {}) {
  const plan = status?.resume_plan;
  const canSkip = RESUMABLE_STATUSES.has(status?.status)
    && Number.isInteger(status?.item_index)
    && Number.isInteger(status?.items_total)
    && status.item_index >= 1
    && status.item_index <= status.items_total;
  const canRetry = plan?.mode === "retry_item";

  return [
    {
      action: "retry_current",
      label: "Обновить и повторить текущего",
      disabled: !canRetry,
      description: canRetry
        ? "Страница будет обновлена, затем отправка начнётся с текущего продавца."
        : "Повтор недоступен: клик отправки уже мог пройти, возможен дубль письма.",
    },
    {
      action: "skip_current",
      label: "Пропустить и начать со следующего",
      disabled: !canSkip,
      description: "Текущий продавец будет отмечен как пропущенный, рассылка продолжится со следующего.",
    },
  ];
}

// Честное объяснение отсутствия кнопки, когда список продавцов не пройден
// до конца, но продолжить нельзя (остановка пришлась на последнего продавца —
// пропустить его было бы некуда).
export function outreachResumeUnavailableMessage(status = {}) {
  if (!RESUMABLE_STATUSES.has(status?.status)) return null;

  const itemsTotal = typeof status?.items_total === "number" ? status.items_total : null;
  const itemIndex = typeof status?.item_index === "number" ? status.item_index : null;
  if (itemsTotal == null || itemIndex == null || itemIndex >= itemsTotal) return null;
  if (status?.resume_available === true) return null;

  return (
    "Продолжение недоступно: остановка пришлась на последнего продавца — "
    + "проверьте переписку вручную."
  );
}
