import { extractPublishFieldErrors } from "./publish";
import { extractResaleFieldErrors } from "./resale";

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

export async function fetchPublishCategories() {
  const response = await fetch("/api/publish/categories");
  return readJson(response);
}

export async function generateAddresses(cities, usedLocations = []) {
  const response = await fetch("/api/addresses/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cities, used_locations: usedLocations }),
  });
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

export async function startItOutreach(limit) {
  const response = await fetch("/api/it-outreach/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ limit }),
  });
  return readJson(response);
}

export async function fetchItOutreachStatus(jobId) {
  const response = await fetch(`/api/it-outreach/status/${jobId}`);
  if (response.status === 404) return { status: "not_found" };
  return readJson(response);
}

export async function fetchItOutreachResults() {
  const response = await fetch("/api/it-outreach/results");
  return readJson(response);
}

export const itOutreachExportCsvUrl = "/api/it-outreach/export.csv";

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

export async function resumePublish(jobId) {
  const response = await fetch(`/api/publish/resume/${jobId}`, {
    method: "POST",
  });
  return readJson(response);
}

// Незавершённые publish-задачи (F01) — баннер «Есть незавершённая публикация»
// на экране формы, чтобы пользователь не запускал второй пакет поверх первого.
export async function fetchPendingPublishJobs() {
  const response = await fetch("/api/publish/pending");
  return readJson(response);
}

export async function closePublishJob(jobId) {
  const response = await fetch(`/api/publish/close/${jobId}`, {
    method: "POST",
  });
  return readJson(response);
}

// ─── Prepare API (фаза превью вариантов) ─────────────────────────────────────

export async function startPrepare(formData) {
  // Отправляем multipart/form-data — браузер сам выставит Content-Type с boundary
  const response = await fetch("/api/publish/prepare", {
    method: "POST",
    body: formData,
  });
  return readJson(response);
}

export async function getPrepareStatus(prepId) {
  const response = await fetch(`/api/publish/prepare/status/${prepId}`);
  return readJson(response);
}

export async function getPrepareResult(prepId) {
  const response = await fetch(`/api/publish/prepare/result/${prepId}`);
  return readJson(response);
}

export async function regenerateDraft(prepId, draftIndex) {
  const response = await fetch("/api/publish/prepare/regenerate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prep_id: prepId, draft_index: draftIndex }),
  });
  return readJson(response);
}

export async function updateDraftText(prepId, draftIndex, title, description) {
  const response = await fetch("/api/publish/prepare/update-text", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prep_id: prepId,
      draft_index: draftIndex,
      title: title.trim(),
      description: description.trim(),
    }),
  });
  return readJson(response);
}

// Строит URL к миниатюре фото черновика (не fetch — просто строка для <img src>)
export function prepPhotoUrl(prepId, draftIndex, photoIndex) {
  return `/api/publish/prepare/photo/${prepId}/${draftIndex}/${photoIndex}`;
}

// ─── Agents registry API (реестр агентов и общий журнал событий) ─────────────

export async function fetchAgents() {
  const response = await fetch("/api/agents");
  return readJson(response);
}

export async function fetchJournal({ actor, limit = 100 } = {}) {
  const params = new URLSearchParams();
  if (actor) {
    params.set("actor", actor);
  }
  if (limit) {
    params.set("limit", String(limit));
  }

  const query = params.toString();
  const response = await fetch(`/api/journal${query ? `?${query}` : ""}`);
  return readJson(response);
}

// ─── Сборщик статистики (docs/specs/agents-registry.md, Этап 2) ───────────────

export async function startStatsCollectorRun(periodDays) {
  const response = await fetch("/api/agents/stats_collector/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ period_days: periodDays }),
  });
  return readJson(response);
}

export async function fetchAgentRunStatus(runId) {
  const response = await fetch(`/api/agents/runs/${runId}`);
  return readJson(response);
}

// null, если снимков ещё не было (404 — это не ошибка, а обычное состояние).
export async function fetchStatsLatest() {
  const response = await fetch("/api/stats/latest");
  if (response.status === 404) {
    return null;
  }
  return readJson(response);
}

// ─── Стратег (docs/specs/agents-registry.md, Этап 3) ──────────────────────────

export async function fetchScoutReports() {
  const response = await fetch("/api/scout/reports");
  return readJson(response);
}

export async function runStrategist(scoutReportKey) {
  const response = await fetch("/api/agents/strategist/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scout_report_key: scoutReportKey }),
  });
  return readJson(response);
}

export async function fetchHypotheses() {
  const response = await fetch("/api/hypotheses");
  return readJson(response);
}

export async function decideHypothesis(hypothesisId, decision) {
  const response = await fetch(`/api/hypotheses/${hypothesisId}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision }),
  });
  return readJson(response);
}

// ─── Outreach API ──────────────────────────────────────────────────────────

export async function importOutreachContacts(urls) {
  const response = await fetch("/api/outreach/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ urls }),
  });
  return readJson(response);
}

export async function fetchOutreachHistory() {
  const response = await fetch("/api/outreach/history");
  return readJson(response);
}

export async function startOutreachCollection(payload) {
  const response = await fetch("/api/outreach/collect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return readJson(response);
}

export async function fetchOutreachStatus(jobId) {
  const response = await fetch(`/api/outreach/status/${jobId}`);
  return readJson(response);
}

export async function fetchOutreachResult(jobId) {
  const response = await fetch(`/api/outreach/result/${jobId}`);
  return readJson(response);
}

export async function sendOutreachMessages(jobId, payload) {
  const response = await fetch(`/api/outreach/send/${jobId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return readJson(response);
}

export async function resumeOutreach(jobId, action) {
  const response = await fetch(`/api/outreach/resume/${jobId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  return readJson(response);
}

// ─── Seller scan API (Разбор продавца) ───────────────────────────────────────

export async function startSellerScan(payload) {
  const response = await fetch("/api/seller/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return readJson(response);
}

export async function fetchSellerScanStatus(jobId) {
  const response = await fetch(`/api/seller/status/${jobId}`);
  return readJson(response);
}

export async function fetchSellerScanResult(jobId) {
  const response = await fetch(`/api/seller/result/${jobId}`);
  return readJson(response);
}

// Строит URL к CSV одной из двух таблиц — не fetch, просто ссылка для <a href>.
export function sellerScanExportCsvUrl(jobId, sort) {
  return `/api/seller/export.csv?job_id=${encodeURIComponent(jobId)}&sort=${encodeURIComponent(sort)}`;
}

// ─── Resale finder API (Поиск под перепродажу, docs/specs/resale-finder.md) ──

export async function startResaleScan(payload) {
  const response = await fetch("/api/resale/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (response.status === 422) {
    const data = await response.json().catch(() => ({}));
    const error = new Error("Ошибка валидации");
    error.fieldErrors = extractResaleFieldErrors(data);
    throw error;
  }

  return readJson(response);
}

export async function fetchResaleStatus(jobId) {
  const response = await fetch(`/api/resale/status/${jobId}`);
  if (response.status === 404) return { status: "not_found" };
  return readJson(response);
}

export async function fetchResaleResult(jobId) {
  const response = await fetch(`/api/resale/result/${jobId}`);
  return readJson(response);
}

// Не fetch — просто ссылка для <a href>.
export function resaleExportCsvUrl(jobId) {
  return `/api/resale/export.csv?job_id=${encodeURIComponent(jobId)}`;
}
