import { extractPublishFieldErrors } from "./publish";

async function readJson(response) {
  const text = await response.text();
  const data = text ? JSON.parse(text) : {};

  if (!response.ok) {
    const message = data.error || `HTTP ${response.status}`;
    throw new Error(message);
  }

  return data;
}

export async function fetchBootstrap() {
  const response = await fetch("/api/bootstrap");
  return readJson(response);
}

export async function startSearch(payload) {
  const response = await fetch("/api/search", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  return readJson(response);
}

export async function fetchStatus(jobId) {
  const response = await fetch(`/api/status/${jobId}`);
  return readJson(response);
}

export async function fetchResults(jobId) {
  const response = await fetch(`/api/results/${jobId}`);
  return readJson(response);
}

// ─── Publish API ──────────────────────────────────────────────────────────────

export async function startPublish(formData) {
  // Отправляем multipart/form-data — браузер сам выставит Content-Type с boundary
  const response = await fetch("/api/publish/start", {
    method: "POST",
    body: formData,
  });

  if (response.status === 422) {
    const data = await response.json().catch(() => ({}));
    const fieldErrors = extractPublishFieldErrors(data);
    const error = new Error("Ошибка валидации");
    error.fieldErrors = fieldErrors;
    throw error;
  }

  return readJson(response);
}

export async function fetchPublishStatus(jobId) {
  const response = await fetch(`/api/publish/status/${jobId}`);
  return readJson(response);
}

export async function fetchPublishResult(jobId) {
  const response = await fetch(`/api/publish/result/${jobId}`);
  return readJson(response);
}
