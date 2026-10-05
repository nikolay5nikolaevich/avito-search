export const SELLER_SCAN_STORAGE_KEY = "avito-seller-scan-form-v1";

export const DEFAULT_SELLER_SCAN_FORM = {
  url: "",
  limit: 30,
};

function normalizeForm(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { ...DEFAULT_SELLER_SCAN_FORM };
  }

  const limit = Number(value.limit);
  return {
    url: typeof value.url === "string" ? value.url : "",
    limit: Number.isInteger(limit) && limit >= 1 && limit <= 300 ? limit : 30,
  };
}

export function createSellerScanStorage(storage) {
  return {
    load() {
      try {
        const raw = storage.getItem(SELLER_SCAN_STORAGE_KEY);
        return raw ? normalizeForm(JSON.parse(raw)) : { ...DEFAULT_SELLER_SCAN_FORM };
      } catch {
        return { ...DEFAULT_SELLER_SCAN_FORM };
      }
    },
    save(form) {
      storage.setItem(SELLER_SCAN_STORAGE_KEY, JSON.stringify(normalizeForm(form)));
    },
  };
}
