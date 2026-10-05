import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_SELLER_SCAN_FORM,
  SELLER_SCAN_STORAGE_KEY,
  createSellerScanStorage,
} from "./sellerScan.js";

test("seller scan form starts with a sane default limit", () => {
  assert.deepEqual(DEFAULT_SELLER_SCAN_FORM, { url: "", limit: 30 });
});

test("seller scan storage ignores malformed saved form", () => {
  const storage = { getItem: () => "{", setItem() {} };
  assert.deepEqual(createSellerScanStorage(storage).load(), DEFAULT_SELLER_SCAN_FORM);
});

test("seller scan storage clamps limit outside 1..300 back to default", () => {
  const store = {};
  const storage = {
    getItem: (key) => store[key],
    setItem: (key, value) => { store[key] = value; },
  };
  const persisted = createSellerScanStorage(storage);
  persisted.save({ url: "https://www.avito.ru/brands/abc", limit: 999 });
  assert.equal(store[SELLER_SCAN_STORAGE_KEY], JSON.stringify({ url: "https://www.avito.ru/brands/abc", limit: 30 }));
});

test("seller scan storage round-trips a valid form", () => {
  const store = {};
  const storage = {
    getItem: (key) => store[key],
    setItem: (key, value) => { store[key] = value; },
  };
  const persisted = createSellerScanStorage(storage);
  const form = { url: "https://www.avito.ru/brands/abc123/items/all", limit: 50 };
  persisted.save(form);
  assert.deepEqual(persisted.load(), form);
});
