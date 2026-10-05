import test from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_OUTREACH_FORM,
  OUTREACH_STORAGE_KEY,
  buildSendPayload,
  createOutreachStorage,
  describeOutreachResumePlan,
  outreachResumeActions,
  outreachResumeUnavailableMessage,
  serializeOutreachForm,
} from "./outreach.js";

test("outreach form starts with the safe batch limit", () => {
  assert.deepEqual(DEFAULT_OUTREACH_FORM, {
    city: "",
    query: "",
    limit: 15,
    message: "",
  });
});

test("outreach storage ignores malformed saved form", () => {
  const storage = { getItem: () => "{", setItem() {} };

  assert.deepEqual(createOutreachStorage(storage).load(), DEFAULT_OUTREACH_FORM);
});

test("outreach storage serializes only the reusable form values", () => {
  const records = new Map();
  const storage = {
    getItem: (key) => records.get(key) ?? null,
    setItem: (key, value) => records.set(key, value),
  };
  const form = { city: "spb", query: "iPhone 17", limit: "20", message: "Здравствуйте" };

  createOutreachStorage(storage).save(form);

  assert.deepEqual(JSON.parse(records.get(OUTREACH_STORAGE_KEY)), {
    city: "spb",
    query: "iPhone 17",
    limit: 20,
    message: "Здравствуйте",
  });
  assert.deepEqual(serializeOutreachForm(form), JSON.stringify({
    city: "spb",
    query: "iPhone 17",
    limit: 20,
    message: "Здравствуйте",
  }));
});

test("send payload contains only selected candidate keys and explicit confirmation", () => {
  assert.deepEqual(buildSendPayload([
    { seller_key: "seller-a", selected: true },
    { seller_key: "seller-b", selected: false },
    { seller_key: "seller-c", selected: true },
  ], "Текст письма"), {
    candidate_keys: ["seller-a", "seller-c"],
    message: "Текст письма",
    confirmed: true,
  });
});

test("describeOutreachResumePlan renders retry_item and skip_item texts from resume_plan alone", () => {
  assert.equal(describeOutreachResumePlan({ resume_plan: null }), null);

  const retry = describeOutreachResumePlan({
    resume_plan: { mode: "retry_item", start_index: 3, skipped_item: null, items_total: 12 },
  });
  assert.equal(retry.buttonLabel, "Повторить текущего");
  assert.match(retry.description, /№3 из 12/);
  assert.match(retry.description, /ещё не отправлялось/);

  const skip = describeOutreachResumePlan({
    resume_plan: { mode: "skip_item", start_index: 4, skipped_item: 3, items_total: 12 },
  });
  assert.equal(skip.buttonLabel, "Продолжить со следующего");
  assert.match(skip.description, /№4 из 12/);
  assert.match(skip.description, /№3 письмо могло уйти/);
});

test("outreach resume actions keep both controls and block retry after send click", () => {
  const preClick = outreachResumeActions({
    status: "needs_user_action",
    items_total: 12,
    item_index: 3,
    resume_plan: { mode: "retry_item", start_index: 3, items_total: 12 },
  });
  assert.deepEqual(preClick.map(({ action, disabled }) => ({ action, disabled })), [
    { action: "retry_current", disabled: false },
    { action: "skip_current", disabled: false },
  ]);

  const afterClick = outreachResumeActions({
    status: "needs_user_action",
    items_total: 12,
    item_index: 3,
    resume_plan: { mode: "skip_item", start_index: 4, skipped_item: 3, items_total: 12 },
  });
  assert.equal(afterClick[0].label, "Обновить и повторить текущего");
  assert.equal(afterClick[0].disabled, true);
  assert.match(afterClick[0].description, /дубл/i);
  assert.equal(afterClick[1].label, "Пропустить и начать со следующего");
  assert.equal(afterClick[1].disabled, false);
});

test("outreachResumeUnavailableMessage only fires when resume is genuinely unavailable with sellers left", () => {
  assert.equal(outreachResumeUnavailableMessage({
    status: "failed",
    resume_available: false,
    items_total: 12,
    item_index: 11,
  }), "Продолжение недоступно: остановка пришлась на последнего продавца — проверьте переписку вручную.");

  // Остановка пришлась на последнего продавца — уже не тот случай (skip некуда)
  assert.equal(outreachResumeUnavailableMessage({
    status: "failed",
    resume_available: false,
    items_total: 12,
    item_index: 12,
  }), null);

  // resume_available: false, но список ещё не начат/не терминальный статус — объяснение не нужно
  assert.equal(outreachResumeUnavailableMessage({
    status: "running",
    resume_available: false,
    items_total: 12,
    item_index: 3,
  }), null);

  // Возобновление доступно — объяснение не нужно
  assert.equal(outreachResumeUnavailableMessage({
    status: "failed",
    resume_available: true,
    items_total: 12,
    item_index: 3,
  }), null);
});
