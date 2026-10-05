import { useEffect, useState } from "react";
import FieldShell from "../components/FieldShell";
import ProgressBar from "../components/ProgressBar";
import SiteFooter from "../components/SiteFooter";
import Topbar from "../components/Topbar";
import {
  fetchBootstrap,
  fetchResaleResult,
  fetchResaleStatus,
  resaleExportCsvUrl,
  startResaleScan,
} from "../lib/api";
import {
  DEFAULT_RESALE_FORM,
  RESALE_STOPPED_STATUSES,
  createResaleStorage,
  resaleStageLabel,
} from "../lib/resale";

const EXCLUDED_LABELS = {
  parts: "На запчасти",
  not_laptop: "Не ноутбук",
  unrecognized: "Не распознано",
  errors: "Ошибка разбора ИИ",
};

function browserStorage() {
  return typeof window === "undefined" ? null : createResaleStorage(window.localStorage);
}

function readInitialForm() {
  try {
    return browserStorage()?.load() || { ...DEFAULT_RESALE_FORM };
  } catch {
    return { ...DEFAULT_RESALE_FORM };
  }
}

function formatJobProgress(status) {
  const current = status?.current;
  const total = status?.total;
  return Number.isFinite(current) && Number.isFinite(total)
    ? `${current} из ${total}`
    : "Выполняем в Chrome…";
}

function ConditionNote({ condition }) {
  if (condition === "unknown") {
    return <span className="resale-note resale-note-unknown">состояние не ясно, проверь</span>;
  }
  return null;
}

function DealsTable({ deals, thresholdPct }) {
  if (!deals.length) {
    return (
      <p className="workspace-body-copy">
        Выгодных лотов при пороге {thresholdPct}% нет.
      </p>
    );
  }

  return (
    <div className="workspace-results-scroll">
      <table className="workspace-results-table">
        <thead>
          <tr>
            <th scope="col" className="workspace-table-sticky workspace-city-column">Название</th>
            <th scope="col">Модель</th>
            <th scope="col">Цена</th>
            <th scope="col">Рынок</th>
            <th scope="col">Скидка</th>
            <th scope="col">Выборка</th>
            <th scope="col">Состояние</th>
          </tr>
        </thead>
        <tbody>
          {deals.map((deal) => (
            <tr key={deal.item_id} className="workspace-results-row">
              <th scope="row" className="workspace-table-sticky workspace-city-cell">
                <a className="workspace-top-link" href={deal.url} target="_blank" rel="noreferrer">
                  {deal.title || deal.url}
                </a>
                {deal.low_data ? (
                  <div className="resale-badge resale-badge-low-data">мало данных</div>
                ) : null}
              </th>
              <td>{deal.model}</td>
              <td className="workspace-count-cell">{deal.price} ₽</td>
              <td className="workspace-count-cell">{deal.market_price} ₽</td>
              <td className="workspace-average-cell">{deal.discount_pct}%</td>
              <td className="workspace-count-cell">{deal.sample}</td>
              <td>
                {deal.condition === "working" ? "рабочий" : "не заявлен продавцом"}
                {" "}
                <ConditionNote condition={deal.condition} />
                {deal.reason ? <div className="resale-reason">{deal.reason}</div> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function GroupsTable({ groups }) {
  const [expanded, setExpanded] = useState(false);

  if (!groups.length) {
    return null;
  }

  return (
    <section className="workspace-panel workspace-results-table-shell">
      <div className="workspace-panel-header">
        <h2 className="workspace-panel-title">Рынки по моделям</h2>
        <button
          type="button"
          className="mini-action"
          onClick={() => setExpanded((current) => !current)}
          aria-expanded={expanded}
        >
          {expanded ? "Скрыть" : "Показать"}
        </button>
      </div>
      <p className="workspace-body-copy">
        Рыночная цена и выборка считаются по всем объявлениям выдачи, включая другие
        города с доставкой.
      </p>
      {expanded ? (
        <div className="workspace-results-scroll">
          <table className="workspace-results-table">
            <thead>
              <tr>
                <th scope="col" className="workspace-table-sticky workspace-city-column">Модель</th>
                <th scope="col">Выборка</th>
                <th scope="col">Рынок</th>
                <th scope="col">Мин.</th>
                <th scope="col">Макс.</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((group) => (
                <tr key={group.model_key} className="workspace-results-row">
                  <th scope="row" className="workspace-table-sticky workspace-city-cell">
                    {group.model}
                    {group.low_data ? (
                      <div className="resale-badge resale-badge-low-data">мало данных</div>
                    ) : null}
                  </th>
                  <td className="workspace-count-cell">{group.sample}</td>
                  <td className="workspace-count-cell">
                    {group.market_price != null ? `${group.market_price} ₽` : "—"}
                  </td>
                  <td className="workspace-count-cell">
                    {group.min_price != null ? `${group.min_price} ₽` : "—"}
                  </td>
                  <td className="workspace-count-cell">
                    {group.max_price != null ? `${group.max_price} ₽` : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

function ExcludedSummary({ excluded }) {
  const entries = Object.entries(EXCLUDED_LABELS)
    .map(([key, label]) => ({ key, label, count: excluded?.[key] ?? 0 }))
    .filter((entry) => entry.count > 0);

  if (!entries.length) {
    return null;
  }

  return (
    <div className="draft-summary-strip">
      {entries.map((entry) => (
        <div key={entry.key} className="draft-summary-item">
          <span className="draft-summary-label">{entry.label}</span>
          <span className="draft-summary-value">{entry.count}</span>
        </div>
      ))}
    </div>
  );
}

export default function ResalePage() {
  const [bootstrap, setBootstrap] = useState(null);
  const [form, setForm] = useState(readInitialForm);
  const [fieldErrors, setFieldErrors] = useState({});
  const [phase, setPhase] = useState("form");
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchBootstrap()
      .then((data) => {
        if (cancelled) return;
        setBootstrap(data);
        setForm((current) => (
          current.city ? current : { ...current, city: data.cities?.[0]?.slug || "" }
        ));
      })
      .catch((fetchError) => {
        if (!cancelled) setError(fetchError.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

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
        const status = await fetchResaleStatus(jobId);
        if (cancelled) return;
        setJobStatus(status);
        if (status.status === "done") {
          const completed = await fetchResaleResult(jobId);
          if (cancelled) return;
          setResult(completed);
          setPhase("result");
          return;
        }
        if (RESALE_STOPPED_STATUSES.has(status.status)) {
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
    setFieldErrors({});
    setResult(null);
    setJobStatus(null);
    setIsSubmitting(true);
    try {
      const payload = {
        query: form.query.trim(),
        city: form.city,
        threshold_pct: Number(form.threshold_pct),
        max_items: Number(form.max_items),
        price_min: String(form.price_min ?? "").trim(),
        price_max: String(form.price_max ?? "").trim(),
      };
      const started = await startResaleScan(payload);
      setJobId(started.job_id);
      setPhase("scanning");
    } catch (submitError) {
      if (submitError.fieldErrors) {
        setFieldErrors(submitError.fieldErrors);
        setError(submitError.fieldErrors.general || "Проверьте поля формы.");
      } else {
        setError(submitError.message);
      }
    } finally {
      setIsSubmitting(false);
    }
  }

  function resetView() {
    setResult(null);
    setJobStatus(null);
    setJobId(null);
    setError("");
    setFieldErrors({});
    setPhase("form");
  }

  const busy = phase === "scanning" || isSubmitting;

  return (
    <main className="workspace-page outreach-page resale-page">
      <div className="page-noise" />
      <div className="page-container workspace-page-shell">
        <header className="workspace-page-header">
          <Topbar
            ariaLabel="Навигация поиска под перепродажу"
            links={[
              { to: "/workspace", label: "Аналитика" },
              { to: "/draft", label: "Публикация" },
              { to: "/outreach", label: "Рассылка" },
              { to: "/it-outreach", label: "Рассылка IT" },
              { to: "/seller", label: "Разбор продавца" },
              { to: "/agents", label: "Агенты" },
            ]}
          />
          <div className="workspace-header-shell outreach-header-shell">
            <div className="workspace-header-copy">
              <p className="section-kicker">Поиск под перепродажу</p>
              <h1 className="workspace-title">Ноутбуки заметно дешевле рынка</h1>
              <p className="workspace-intro-copy">
                Введите запрос и город — сервис соберёт объявления, распознает модель каждого
                ноутбука через ИИ и найдёт лоты, которые продаются заметно дешевле рынка своей
                модели.
              </p>
              <p className="workspace-intro-copy">
                Рыночную цену считаем по всей выдаче города, включая другие города с доставкой,
                а выгодные лоты показываем только из вашего города.
              </p>
            </div>
          </div>
        </header>

        <section className="workspace-main-stack" aria-label="Поиск под перепродажу">
          {error ? <p className="draft-general-error outreach-error" role="alert">{error}</p> : null}

          {phase === "form" ? (
            <form className="workspace-panel outreach-form" onSubmit={handleSubmit}>
              <div className="workspace-panel-header">
                <div>
                  <p className="section-kicker">Шаг 1</p>
                  <h2 className="workspace-panel-title">Что и где ищем</h2>
                </div>
              </div>
              <div className="outreach-form-grid">
                <FieldShell label="Запрос" error={fieldErrors.query}>
                  <input
                    className="field-input"
                    name="query"
                    value={form.query}
                    onChange={updateForm}
                    placeholder="Например: игровой ноутбук"
                    required
                  />
                </FieldShell>
                <FieldShell label="Город" error={fieldErrors.city}>
                  <select
                    className="field-input draft-select"
                    name="city"
                    value={form.city}
                    onChange={updateForm}
                    required
                  >
                    {!bootstrap ? <option value="">Загрузка городов…</option> : null}
                    {bootstrap?.cities?.map((city) => (
                      <option key={city.slug} value={city.slug}>
                        {city.name}
                      </option>
                    ))}
                  </select>
                </FieldShell>
                <FieldShell label="Порог скидки, %" error={fieldErrors.threshold_pct}>
                  <input
                    className="field-input"
                    type="number"
                    name="threshold_pct"
                    min="5"
                    max="80"
                    value={form.threshold_pct}
                    onChange={updateForm}
                    required
                  />
                </FieldShell>
              </div>
              <div className="outreach-form-grid">
                <FieldShell label="Цена от" error={fieldErrors.price_min}>
                  <input
                    className="field-input"
                    type="number"
                    min="0"
                    name="price_min"
                    value={form.price_min}
                    onChange={updateForm}
                    placeholder="0"
                  />
                </FieldShell>
                <FieldShell label="Цена до" error={fieldErrors.price_max}>
                  <input
                    className="field-input"
                    type="number"
                    min="0"
                    name="price_max"
                    value={form.price_max}
                    onChange={updateForm}
                    placeholder="0"
                  />
                </FieldShell>
                <FieldShell label="Максимум объявлений" error={fieldErrors.max_items}>
                  <input
                    className="field-input"
                    type="number"
                    min="20"
                    max="300"
                    name="max_items"
                    value={form.max_items}
                    onChange={updateForm}
                    required
                  />
                </FieldShell>
              </div>
              <button className="submit-button outreach-collect-button" type="submit" disabled={busy}>
                {isSubmitting ? "Запускаем…" : "Найти выгодные лоты"}
              </button>
            </form>
          ) : null}

          {phase === "scanning" ? (
            <section className="workspace-panel outreach-status-panel" aria-live="polite">
              <p className="section-kicker">{resaleStageLabel(jobStatus?.stage)}</p>
              <h2 className="workspace-panel-title">Собираем и разбираем объявления</h2>
              <p className="workspace-body-copy">{formatJobProgress(jobStatus)}.</p>
              <ProgressBar
                percent={
                  Number.isFinite(jobStatus?.current) && Number.isFinite(jobStatus?.total)
                    && jobStatus.total > 0
                    ? Math.min(100, Math.round((jobStatus.current / jobStatus.total) * 100))
                    : 0
                }
              />
            </section>
          ) : null}

          {phase === "result" && result ? (
            <>
              <section className="workspace-panel outreach-status-panel">
                <p className="section-kicker">Готово</p>
                <h2 className="workspace-panel-title">
                  {result.deals?.length
                    ? `Найдено ${result.deals.length} выгодных лотов`
                    : `Выгодных лотов при пороге ${form.threshold_pct}% нет`}
                </h2>
                <p className="workspace-body-copy">
                  Собрано объявлений: {result.total_items ?? 0}
                  {Number.isFinite(result.local_items)
                    ? `, из них в вашем городе: ${result.local_items}`
                    : ""}.
                  {result.topup_queries?.length
                    ? ` Уточняющих поисков: ${result.topup_queries.length}.`
                    : ""}
                </p>
                <ExcludedSummary excluded={result.excluded} />
                <div className="outreach-preview-actions">
                  <a className="primary-link-button" href={resaleExportCsvUrl(jobId)}>
                    CSV — выгодные лоты
                  </a>
                  <button className="mini-action" type="button" onClick={resetView}>
                    Новый поиск
                  </button>
                </div>
              </section>

              <div className="workspace-panel workspace-results-table-shell">
                <div className="workspace-panel-header">
                  <h2 className="workspace-panel-title">Выгодные лоты</h2>
                </div>
                <DealsTable deals={result.deals || []} thresholdPct={form.threshold_pct} />
              </div>

              <GroupsTable groups={result.groups || []} />
            </>
          ) : null}
        </section>

        <SiteFooter links={[{ to: "/workspace", label: "Аналитика" }, { to: "/seller", label: "Разбор продавца" }]} />
      </div>
    </main>
  );
}
