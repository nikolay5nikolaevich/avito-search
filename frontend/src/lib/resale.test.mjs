import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_RESALE_FORM,
  RESALE_STORAGE_KEY,
  createResaleStorage,
  extractResaleFieldErrors,
  resaleStageLabel,
} from "./resale.js";

test("resale form starts with sane defaults", () => {
  assert.deepEqual(DEFAULT_RESALE_FORM, {
    query: "",
    city: "",
    threshold_pct: 20,
    max_items: 150,
    price_min: "",
    price_max: "",
  });
});

test("resale storage ignores malformed saved form", () => {
  const storage = { getItem: () => "{", setItem() {} };
  assert.deepEqual(createResaleStorage(storage).load(), DEFAULT_RESALE_FORM);
});

test("resale storage clamps threshold_pct and max_items outside range back to default", () => {
  const store = {};
  const storage = {
    getItem: (key) => store[key],
    setItem: (key, value) => { store[key] = value; },
  };
  const persisted = createResaleStorage(storage);
  persisted.save({ query: "ноутбук", city: "moskva", threshold_pct: 999, max_items: 1 });
  assert.deepEqual(JSON.parse(store[RESALE_STORAGE_KEY]), {
    query: "ноутбук",
    city: "moskva",
    threshold_pct: 20,
    max_items: 150,
    price_min: "",
    price_max: "",
  });
});

test("resale storage round-trips a valid form", () => {
  const store = {};
  const storage = {
    getItem: (key) => store[key],
    setItem: (key, value) => { store[key] = value; },
  };
  const persisted = createResaleStorage(storage);
  const form = {
    query: "игровой ноутбук",
    city: "spb",
    threshold_pct: 25,
    max_items: 200,
    price_min: "10000",
    price_max: "80000",
  };
  persisted.save(form);
  assert.deepEqual(persisted.load(), form);
});

test("resaleStageLabel maps known stages to Russian and falls back for unknown", () => {
  assert.equal(resaleStageLabel("collecting"), "Сбор объявлений");
  assert.equal(resaleStageLabel("classifying"), "Разбор ИИ");
  assert.equal(resaleStageLabel("topup"), "Уточняющий поиск");
  assert.equal(resaleStageLabel("done"), "Готово");
  assert.equal(resaleStageLabel(undefined), "Выполняем в Chrome…");
});

test("extractResaleFieldErrors reads {errors:[{field,error}]}", () => {
  const errors = extractResaleFieldErrors({
    errors: [
      { field: "city", error: "Неизвестный город" },
      { field: "threshold_pct", error: "Должно быть от 5 до 80" },
    ],
  });
  assert.deepEqual(errors, {
    city: "Неизвестный город",
    threshold_pct: "Должно быть от 5 до 80",
  });
});

test("extractResaleFieldErrors falls back to general message", () => {
  assert.deepEqual(extractResaleFieldErrors({ error: "Что-то пошло не так" }), {
    general: "Что-то пошло не так",
  });
  assert.deepEqual(extractResaleFieldErrors({}), {});
});
