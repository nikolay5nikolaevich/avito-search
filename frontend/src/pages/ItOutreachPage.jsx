import { useEffect, useMemo, useState } from "react";
import SiteFooter from "../components/SiteFooter";
import Topbar from "../components/Topbar";
import {
  fetchItOutreachResults,
  fetchItOutreachStatus,
  itOutreachExportCsvUrl,
  startItOutreach,
} from "../lib/api";
import { filterItOutreachCompanies, isItOutreachTerminalStatus, itOutreachPollDelay } from "../lib/itOutreach";

const JOB_KEY = "avito.it-outreach.job-id";

function companyName(company) {
  return company.company_name || company.company || company.domain || "Не указана";
}

function LinkCell({ href, children }) {
  return href ? <a href={href} target="_blank" rel="noreferrer">{children}</a> : "—";
}

function WorkLinks({ company }) {
  if (!company.vacancy_url && !company.internship_url) return "—";
  return <>{company.vacancy_url ? <LinkCell href={company.vacancy_url}>Вакансии</LinkCell> : null}{company.vacancy_url && company.internship_url ? " / " : null}{company.internship_url ? <LinkCell href={company.internship_url}>Стажировка</LinkCell> : null}</>;
}

export default function ItOutreachPage() {
  const [limit, setLimit] = useState(20);
  const [filter, setFilter] = useState("all");
  const [companies, setCompanies] = useState([]);
  const [jobId, setJobId] = useState(() => localStorage.getItem(JOB_KEY));
  const [status, setStatus] = useState(null);
  const [error, setError] = useState("");

  const filteredCompanies = useMemo(
    () => filterItOutreachCompanies(companies, filter),
    [companies, filter],
  );

  function loadResults() {
    return fetchItOutreachResults().then((data) => setCompanies(data.companies || []));
  }

  useEffect(() => {
    loadResults().catch((fetchError) => setError(fetchError.message));
  }, []);

  useEffect(() => {
    if (!jobId) return undefined;
    let cancelled = false;
    let timer;
    async function poll() {
      try {
        const nextStatus = await fetchItOutreachStatus(jobId);
        if (cancelled) return;
        setError("");
        setStatus(nextStatus);
        if (isItOutreachTerminalStatus(nextStatus.status)) {
          localStorage.removeItem(JOB_KEY);
          setJobId(null);
          if (nextStatus.status === "not_found") setStatus(null);
          await loadResults();
          return;
        }
        timer = window.setTimeout(poll, itOutreachPollDelay());
      } catch (fetchError) {
        if (!cancelled) {
          setError(fetchError.message);
          timer = window.setTimeout(poll, itOutreachPollDelay(true));
        }
      }
    }
    poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [jobId]);

  async function handleStart(event) {
    event.preventDefault();
    setError("");
    setStatus(null);
    const safeLimit = Number(limit);
    if (!Number.isInteger(safeLimit) || safeLimit < 1 || safeLimit > 100) {
      setError("Укажите целое число сайтов от 1 до 100.");
      return;
    }
    try {
      const data = await startItOutreach(safeLimit);
      localStorage.setItem(JOB_KEY, data.job_id);
      setJobId(data.job_id);
    } catch (startError) {
      setError(startError.message);
    }
  }

  const isRunning = status?.status === "running" || Boolean(jobId && !status);
  const isBlocked = status?.status === "blocked";

  return (
    <main className="workspace-page outreach-page it-outreach-page">
      <div className="page-noise" />
      <div className="page-container workspace-page-shell">
        <header className="workspace-page-header">
          <Topbar ariaLabel="Навигация IT-рассылки" links={[
            { to: "/workspace", label: "Аналитика" },
            { to: "/draft", label: "Публикация" },
            { to: "/outreach", label: "Рассылка" },
            { to: "/seller", label: "Разбор продавца" },
            { to: "/resale", label: "Перепродажа" },
            { to: "/agents", label: "Агенты" },
          ]} />
          <div className="workspace-header-shell outreach-header-shell">
            <div className="workspace-header-copy">
              <p className="section-kicker">Рассылка IT</p>
              <h1 className="workspace-title">Рассылка IT</h1>
              <p className="workspace-intro-copy">Ищет сайты компаний в открытых данных по России и собирает опубликованные почты; письма не отправляет.</p>
            </div>
          </div>
        </header>

        <section className="workspace-main-stack" aria-label="Поиск IT-контактов">
          {error ? <p className="draft-general-error outreach-error" role="alert">{error}</p> : null}
          <form className="workspace-panel it-outreach-form" onSubmit={handleStart}>
            <label className="field-shell">
              <span>Сайтов для проверки</span>
              <input className="field-input" type="number" min="1" max="100" value={limit} onChange={(event) => setLimit(event.target.value)} disabled={isRunning} />
            </label>
            <button className="primary-button" type="submit" disabled={isRunning}>{isRunning ? "Идёт поиск…" : "Найти компании"}</button>
          </form>

          {status ? <div className={`workspace-panel it-outreach-status ${isBlocked ? "error-panel" : ""}`} aria-live="polite">
            <p className="workspace-panel-title">{isBlocked ? "Поиск остановлен" : status.status === "error" ? "Поиск завершился с ошибкой" : status.status === "done" ? "Поиск завершён" : "Идёт поиск"}</p>
            <p className="workspace-body-copy">Проверено: {status.scanned} / {status.total || "…"}. Найдено email: {status.found_emails}.</p>
            {status.current_site ? <p className="workspace-body-copy">Текущий сайт: {status.current_site}</p> : null}
            {status.error ? <p className="workspace-body-copy">{status.error}</p> : null}
          </div> : null}

          <div className="workspace-panel workspace-results-table-shell">
            <div className="it-outreach-results-head">
              <div>
                <p className="workspace-panel-title">Результаты</p>
                <p className="workspace-body-copy">{filteredCompanies.length} из {companies.length}</p>
              </div>
              <div className="it-outreach-actions">
                <label>Фильтр <select className="field-input" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">Все</option><option value="with_email">С почтой</option><option value="without_email">Без почты</option></select></label>
                {companies.length ? <a className="secondary-button" href={itOutreachExportCsvUrl}>CSV</a> : null}
              </div>
            </div>
            {filteredCompanies.length ? <div className="workspace-results-scroll"><table className="workspace-results-table"><thead><tr><th>Компания</th><th>Город</th><th>Сайт</th><th>Вакансии / стажировки</th><th>Email</th><th>Тип</th><th>Страница-источник</th><th>Проверено</th></tr></thead><tbody>{filteredCompanies.map((company) => <tr className="workspace-results-row" key={company.domain || company.site_url}><th scope="row">{companyName(company)}{company.error ? <small className="it-outreach-row-error">Ошибка: {company.error}</small> : null}</th><td>{company.city || "не указан"}</td><td><LinkCell href={company.site_url}>Сайт</LinkCell></td><td><WorkLinks company={company} /></td><td>{company.email ? <a href={`mailto:${company.email}`}>{company.email}</a> : "—"}</td><td>{company.email_kind || "—"}</td><td><LinkCell href={company.source_url}>Источник</LinkCell></td><td>{company.checked_at ? new Date(company.checked_at).toLocaleString("ru-RU") : "—"}</td></tr>)}</tbody></table></div> : <p className="workspace-body-copy it-outreach-empty">{companies.length ? "По этому фильтру записей нет." : "Пока нет результатов. Запустите поиск компаний."}</p>}
          </div>
          <p className="it-outreach-attribution">Данные сайтов: <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">© OpenStreetMap contributors (ODbL)</a></p>
        </section>
        <SiteFooter links={[{ to: "/workspace", label: "Аналитика" }, { to: "/outreach", label: "Рассылка" }]} />
      </div>
    </main>
  );
}
