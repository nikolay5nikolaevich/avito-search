import { useEffect, useState } from "react";
import SiteFooter from "../components/SiteFooter";
import Topbar from "../components/Topbar";
import {
  fetchSellerScanResult,
  fetchSellerScanStatus,
  sellerScanExportCsvUrl,
  startSellerScan,
} from "../lib/api";
import { createSellerScanStorage, DEFAULT_SELLER_SCAN_FORM } from "../lib/sellerScan";

// Статусы, на которых опрос останавливается без результата — задача
// остановлена (см. backend/app.py: run_job/_run_seller_scan_job использует
// тот же словарь статусов, что и аналитика: blocked/error).
const STOPPED_STATUSES = new Set(["blocked", "error", "not_found"]);

function browserStorage() {
  return typeof window === "undefined" ? null : createSellerScanStorage(window.localStorage);
}

function readInitialForm() {
  try {
    return browserStorage()?.load() || { ...DEFAULT_SELLER_SCAN_FORM };
  } catch {
    return { ...DEFAULT_SELLER_SCAN_FORM };
  }
}

function formatJobProgress(status) {
  const current = status?.current;
  const total = status?.total;
  return Number.isFinite(current) && Number.isFinite(total)
    ? `${current} из ${total}`
    : "Выполняем в Chrome…";
}

function SellerScanTable({ title, rows }) {
  return (
    <div className="workspace-panel workspace-results-table-shell">
      <div className="workspace-panel-header">
        <h2 className="workspace-panel-title">{title}</h2>
      </div>
      {rows.length === 0 ? (
        <p className="workspace-body-copy">Нет объявлений для показа.</p>
      ) : (
        <div className="workspace-results-scroll">
          <table className="workspace-results-table">
            <thead>
              <tr>
                <th scope="col" className="workspace-table-sticky workspace-city-column">Название</th>
                <th scope="col">Просмотров всего</th>
                <th scope="col">Просмотров сегодня</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((item) => (
                <tr key={item.url} className="workspace-results-row">
                  <th scope="row" className="workspace-table-sticky workspace-city-cell">
                    <a
                      className="workspace-top-link"
                      href={item.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {item.title || item.url}
                    </a>
                    {item.error ? (
                      <div className="workspace-cell-empty">Нет данных: {item.error}</div>
                    ) : null}
                  </th>
                  <td className="workspace-count-cell">{item.views_total ?? "—"}</td>
                  <td className="workspace-count-cell">{item.views_today ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function SellerScanPage() {
  const [form, setForm] = useState(readInitialForm);
  const [phase, setPhase] = useState("form");
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    try {
      browserStorage()?.save(form);
    } catch {
      // Браузер без localStorage всё равно позволяет работать с текущей формой.
    }
  }, [form]);

  useEffect(() => {
    if (!jobId || phase !== "scanning") return undefined;
    let cancelled = false;
    let timeoutId;

    async function poll() {
      try {
        const status = await fetchSellerScanStatus(jobId);
        if (cancelled) return;
        setJobStatus(status);
        if (status.status === "done") {
          const completed = await fetchSellerScanResult(jobId);
          if (cancelled) return;
          setResult(completed);
          setPhase("result");
          return;
        }
        if (STOPPED_STATUSES.has(status.status)) {
          setError(status.error || "Задача остановлена. Проверьте Chrome и повторите.");
          setPhase("form");
          return;
        }
        timeoutId = window.setTimeout(poll, 1500);
      } catch (pollError) {
        if (!cancelled) {
          setError(pollError.message);
          setPhase("form");
        }
      }
    }

    poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timeoutId);
    };
  }, [jobId, phase]);

  function updateForm(event) {
    const { name, value } = event.target;
    setForm((current) => ({ ...current, [name]: value }));
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setError("");
    setResult(null);
    setJobStatus(null);
    try {
      const started = await startSellerScan({ url: form.url.trim(), limit: Number(form.limit) });
      setJobId(started.job_id);
      setPhase("scanning");
    } catch (submitError) {
      setError(submitError.message);
    }
  }

  const busy = phase === "scanning";

  return (
    <main className="workspace-page outreach-page">
      <div className="page-noise" />
      <div className="page-container workspace-page-shell">
        <header className="workspace-page-header">
          <Topbar
            ariaLabel="Навигация разбора продавца"
            links={[
              { to: "/workspace", label: "Аналитика" },
              { to: "/draft", label: "Публикация" },
              { to: "/outreach", label: "Рассылка" },
              { to: "/it-outreach", label: "Рассылка IT" },
              { to: "/resale", label: "Перепродажа" },
            ]}
          />
          <div className="workspace-header-shell outreach-header-shell">
            <div className="workspace-header-copy">
              <p className="section-kicker">Разбор продавца</p>
              <h1 className="workspace-title">Просмотры всех объявлений продавца</h1>
              <p className="workspace-intro-copy">
                Вставьте ссылку на страницу всех объявлений продавца — сервис пройдёт первые
                N по порядку показа и прочитает просмотры внизу каждой карточки.
              </p>
            </div>
          </div>
        </header>

        <section className="workspace-main-stack" aria-label="Разбор продавца">
          {error ? <p className="draft-general-error outreach-error" role="alert">{error}</p> : null}

          {phase === "form" ? (
            <form className="workspace-panel outreach-form" onSubmit={handleSubmit}>
              <div className="workspace-panel-header">
                <div>
                  <p className="section-kicker">Шаг 1</p>
                  <h2 className="workspace-panel-title">Ссылка на профиль продавца</h2>
                </div>
              </div>
              <div className="outreach-form-grid">
                <label className="field-shell">
                  <span>Ссылка на страницу «Все объявления» продавца</span>
                  <input
                    className="field-input"
                    name="url"
                    value={form.url}
                    onChange={updateForm}
                    placeholder="https://www.avito.ru/brands/.../items/all"
                    required
                  />
                </label>
                <label className="field-shell">
                  <span>Сколько объявлений разобрать</span>
                  <input
                    className="field-input"
                    type="number"
                    name="limit"
                    min="1"
                    max="300"
                    value={form.limit}
                    onChange={updateForm}
                    required
                  />
                </label>
              </div>
              <button className="submit-button outreach-collect-button" type="submit" disabled={busy}>
                Разобрать продавца
              </button>
            </form>
          ) : null}

          {phase === "scanning" ? (
            <section className="workspace-panel outreach-status-panel" aria-live="polite">
              <p className="section-kicker">Разбираем</p>
              <h2 className="workspace-panel-title">Проходим объявления по одному</h2>
              <p className="workspace-body-copy">{formatJobProgress(jobStatus)}.</p>
            </section>
          ) : null}

          {phase === "result" && result ? (
            <>
              <section className="workspace-panel outreach-status-panel">
                <p className="section-kicker">Готово</p>
                <h2 className="workspace-panel-title">
                  {typeof result.found_total === "number"
                    ? `Найдено ${result.found_total} объявлений`
                    : "Итог разбора"}
                </h2>
                <p className="workspace-body-copy">
                  Пройдено: {result.scanned ?? 0}. С ошибкой: {result.errors ?? 0}.
                </p>
                {result.duplicates ? (
                  <p className="workspace-body-copy">
                    Из них {result.duplicates} — повторы: Авито показывает часть объявлений в
                    ленте профиля дважды, поэтому уникальных карточек меньше, чем «Найдено».
                  </p>
                ) : null}
                <div className="outreach-preview-actions">
                  <a
                    className="primary-link-button"
                    href={sellerScanExportCsvUrl(jobId, "total")}
                  >
                    CSV — по просмотрам всего
                  </a>
                  <a
                    className="primary-link-button"
                    href={sellerScanExportCsvUrl(jobId, "today")}
                  >
                    CSV — по просмотрам сегодня
                  </a>
                  <button className="mini-action" type="button" onClick={() => setPhase("form")}>
                    Новый разбор
                  </button>
                </div>
              </section>

              <SellerScanTable
                title="По убыванию просмотров всего"
                rows={result.items_by_total || []}
              />
              <SellerScanTable
                title="По убыванию просмотров сегодня"
                rows={result.items_by_today || []}
              />
            </>
          ) : null}
        </section>

        <SiteFooter links={[{ to: "/workspace", label: "Аналитика" }, { to: "/draft", label: "Публикация" }]} />
      </div>
    </main>
  );
}
