import { useEffect, useState } from "react";
import {
  fetchAgents,
  fetchJournal,
  startStatsCollectorRun,
  fetchAgentRunStatus,
  fetchStatsLatest,
  fetchScoutReports,
  runStrategist,
  fetchHypotheses,
  decideHypothesis,
} from "../lib/api";
import Topbar from "../components/Topbar";
import SiteFooter from "../components/SiteFooter";

const KIND_LABELS = {
  code: "код",
  code_llm: "код + LLM",
};

const STATUS_LABELS = {
  active: "работает",
  planned: "план",
};

const RUN_STATUS_LABELS = {
  queued: "В очереди…",
  running: "Собираем статистику…",
  done: "Готово",
  error: "Ошибка",
};

const DECISION_LABELS = {
  pending: "Ожидает решения",
  accepted: "Принята",
  rejected: "Отклонена",
};

// Итоги снимка для карточки: дата, число объявлений, среднее просмотров/день
// по всем объявлениям снимка. Пусто (нет объявлений) — null, а не 0.
function summarizeSnapshot(snapshot) {
  if (!snapshot) {
    return null;
  }
  const items = snapshot.data?.items || [];
  const avgViewsPerDay =
    items.length > 0
      ? items.reduce((sum, item) => sum + (item.views_per_day || 0), 0) / items.length
      : null;

  return {
    createdAt: snapshot.created_at,
    periodDays: snapshot.period_days,
    itemsCount: items.length,
    avgViewsPerDay,
  };
}

// Просм./день — 1 знак после запятой; null (объявлений нет) — тире.
function formatViewsPerDay(value) {
  return value === null || value === undefined ? "—" : value.toFixed(1);
}

// Доля контактов — проценты без знаков после запятой; null (просмотров 0) — тире.
function formatContactRate(value) {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(0)}%`;
}

function formatEventTime(ts) {
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) {
    return ts;
  }

  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Итог запуска Стратега для панели гипотез: сколько всего, сколько
// принято/отклонено/ждёт решения, по какому отчёту Разведчика и когда.
// Всё — из уже загруженных ответов API (hypotheses, scoutReports), ничего
// не досчитываем и не запрашиваем отдельно.
function summarizeHypotheses(hypotheses, scoutReports) {
  if (!hypotheses || hypotheses.length === 0) {
    return null;
  }

  const total = hypotheses.length;
  const accepted = hypotheses.filter((h) => h.status === "accepted").length;
  const rejected = hypotheses.filter((h) => h.status === "rejected").length;
  const pending = total - accepted - rejected;

  const reportKey = hypotheses[0].scout_report_key;
  const report = (scoutReports || []).find((r) => r.cache_key === reportKey);
  const reportLabel = report
    ? `«${report.query}» · ${report.cities_count} гор.`
    : reportKey || "—";

  return { total, accepted, rejected, pending, reportLabel, createdAt: hypotheses[0].created_at };
}

// Общая заглушка состояния — тот же паттерн, что WorkspaceStatePanel в WorkspacePage.jsx.
function AgentsStatePanel({ kicker, title, copy, tone = "default" }) {
  const className =
    tone === "error" ? "error-panel workspace-state-panel" : "workspace-panel workspace-state-panel";

  return (
    <section className={className}>
      <p className="section-kicker">{kicker}</p>
      <h2 className="workspace-panel-title">{title}</h2>
      <p className="workspace-body-copy">{copy}</p>
    </section>
  );
}

// Переключатель периода + кнопка запуска + статус + итоги последнего снимка.
// Живёт внутри карточки-кнопки (AgentCard), поэтому все клики внутри должны
// останавливать всплытие — иначе они переключали бы фильтр журнала по карточке.
function StatsCollectorControls({
  periodDays,
  onPeriodChange,
  isRunning,
  runStatus,
  runError,
  onRun,
  latest,
  latestError,
}) {
  const summary = summarizeSnapshot(latest);

  return (
    <div className="agent-run-panel" onClick={(event) => event.stopPropagation()}>
      <div className="draft-radio-group">
        {[7, 30].map((days) => (
          <label
            key={days}
            className={`draft-pill draft-radio-chip${periodDays === days ? " draft-radio-chip-active" : ""}`}
          >
            <input
              type="radio"
              name="stats-period"
              value={days}
              checked={periodDays === days}
              disabled={isRunning}
              onChange={() => onPeriodChange(days)}
            />
            {days} дней
          </label>
        ))}
      </div>

      <button type="button" className="mini-action" disabled={isRunning} onClick={onRun}>
        {isRunning ? "Собираем…" : "Запустить"}
      </button>

      {runStatus ? (
        <p className={`agent-run-status${runStatus === "error" ? " agent-run-status-error" : ""}`}>
          {RUN_STATUS_LABELS[runStatus] || runStatus}
          {runStatus === "error" && runError ? `: ${runError}` : ""}
        </p>
      ) : null}

      {latestError ? (
        <p className="agent-run-status agent-run-status-error">Снимок недоступен: {latestError}</p>
      ) : summary ? (
        <p className="agent-run-summary">
          Снимок за {summary.periodDays} дн. от {formatEventTime(summary.createdAt)}:{" "}
          {summary.itemsCount} объявл., в среднем{" "}
          {summary.avgViewsPerDay === null ? "—" : summary.avgViewsPerDay.toFixed(1)} просм./день
        </p>
      ) : (
        <p className="agent-run-summary">Снимков ещё нет — запустите сбор.</p>
      )}
    </div>
  );
}

// Содержимое последнего снимка Сборщика — раскрывается кликом по карточке
// (activeAgent === "stats_collector"). Полная ширина сетки, а не карточка:
// семь колонок таблицы объявлений в треть экрана не влезают.
function SnapshotDetail({ snapshot, snapshotError }) {
  const data = snapshot?.data;
  const cities = data?.cities || [];
  const variants = data?.variants || [];
  const items = [...(data?.items || [])].sort((a, b) => b.views - a.views);

  return (
    <section
      className="workspace-panel agent-snapshot-panel"
      aria-label="Содержимое последнего снимка"
      onClick={(event) => event.stopPropagation()}
    >
      <div className="workspace-panel-header">
        <div>
          <p className="section-kicker">Снимок</p>
          <h2 className="workspace-panel-title">Содержимое последнего снимка</h2>
        </div>
      </div>

      {snapshotError ? (
        <p className="agents-journal-state agents-journal-state-error">
          Снимок недоступен: {snapshotError}
        </p>
      ) : !snapshot ? (
        <p className="agents-journal-state">Снимков ещё нет — запустите сбор.</p>
      ) : (
        <div className="agent-snapshot-tables">
          <div>
            <h3 className="agent-snapshot-subtitle">По городам</h3>
            <div className="workspace-results-scroll">
              <table className="workspace-results-table agent-snapshot-table">
                <thead>
                  <tr>
                    <th>Город</th>
                    <th>Объявлений</th>
                    <th>Просм./день (среднее)</th>
                    <th>Доля контактов</th>
                  </tr>
                </thead>
                <tbody>
                  {cities.map((city) => (
                    <tr key={city.city_slug} className="workspace-results-row">
                      <td>{city.city_name || city.city_slug}</td>
                      <td>{city.items}</td>
                      <td>{formatViewsPerDay(city.views_per_day_avg)}</td>
                      <td>{formatContactRate(city.contact_rate)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {variants.length > 0 ? (
            <div>
              <h3 className="agent-snapshot-subtitle">По вариантам</h3>
              <div className="workspace-results-scroll">
                <table className="workspace-results-table agent-snapshot-table">
                  <thead>
                    <tr>
                      <th>Пакет</th>
                      <th>Вариант</th>
                      <th>Объявлений</th>
                      <th>Просм./день (среднее)</th>
                      <th>Доля контактов</th>
                    </tr>
                  </thead>
                  <tbody>
                    {variants.map((variant) => (
                      <tr key={`${variant.prep_id}-${variant.variant_index}`} className="workspace-results-row">
                        <td className="agents-journal-time">{variant.prep_id}</td>
                        <td>№{variant.variant_index + 1}</td>
                        <td>{variant.items}</td>
                        <td>{formatViewsPerDay(variant.views_per_day_avg)}</td>
                        <td>{formatContactRate(variant.contact_rate)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}

          <div>
            <h3 className="agent-snapshot-subtitle">По объявлениям</h3>
            <div className="workspace-results-scroll">
              <table className="workspace-results-table agent-snapshot-table">
                <thead>
                  <tr>
                    <th>Название</th>
                    <th>Город</th>
                    <th>Вариант</th>
                    <th>Просмотры</th>
                    <th>Контакты</th>
                    <th>Избранное</th>
                    <th>Просм./день</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <tr key={item.item_id} className="workspace-results-row">
                      <td>{item.title || "—"}</td>
                      <td>{item.city_name || item.city_slug || "—"}</td>
                      <td>
                        {item.variant_index !== null && item.variant_index !== undefined
                          ? `№${item.variant_index + 1}`
                          : "—"}
                      </td>
                      <td>{item.views}</td>
                      <td>{item.contacts}</td>
                      <td>{item.favorites}</td>
                      <td>{formatViewsPerDay(item.views_per_day)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

// Переключатель отчёта Разведчика + кнопка запуска + статус. Живёт внутри
// карточки-кнопки (AgentCard), поэтому клики внутри останавливают всплытие —
// как у StatsCollectorControls.
function StrategistControls({
  reports,
  reportsError,
  selectedReportKey,
  onSelectReport,
  isRunning,
  runError,
  onRun,
}) {
  return (
    <div className="agent-run-panel" onClick={(event) => event.stopPropagation()}>
      {reportsError ? (
        <p className="agent-run-status agent-run-status-error">
          Отчёты Разведчика недоступны: {reportsError}
        </p>
      ) : !reports ? (
        <p className="agent-run-summary">Загружаем отчёты Разведчика…</p>
      ) : reports.length === 0 ? (
        <p className="agent-run-summary">
          Отчётов ещё нет — сначала запустите поиск на странице «Аналитика».
        </p>
      ) : (
        <select
          className="field-input draft-select agent-report-select"
          value={selectedReportKey}
          disabled={isRunning}
          onChange={(event) => onSelectReport(event.target.value)}
        >
          <option value="">Выберите отчёт Разведчика…</option>
          {reports.map((report) => (
            <option key={report.cache_key} value={report.cache_key}>
              {report.query} · {report.cities_count} гор. · {formatEventTime(report.created_at)}
              {report.is_expired ? " (устарел)" : ""}
            </option>
          ))}
        </select>
      )}

      <button
        type="button"
        className="mini-action"
        disabled={isRunning || !selectedReportKey}
        onClick={onRun}
      >
        {isRunning ? "Формулируем гипотезы… (до 3 мин)" : "Запустить"}
      </button>

      {runError ? <p className="agent-run-status agent-run-status-error">{runError}</p> : null}
    </div>
  );
}

// Гипотезы последнего запуска Стратега — раскрывается кликом по карточке
// (activeAgent === "strategist"), тот же паттерн, что SnapshotDetail.
function HypothesesPanel({ hypotheses, hypothesesError, onDecide, decisionPendingId, scoutReports }) {
  const summary = summarizeHypotheses(hypotheses, scoutReports);

  return (
    <section
      className="workspace-panel agent-snapshot-panel"
      aria-label="Гипотезы Стратега"
      onClick={(event) => event.stopPropagation()}
    >
      <div className="workspace-panel-header">
        <div>
          <p className="section-kicker">Гипотезы</p>
          <h2 className="workspace-panel-title">Гипотезы последнего запуска</h2>
        </div>
      </div>

      {hypothesesError ? (
        <p className="agents-journal-state agents-journal-state-error">
          Гипотезы недоступны: {hypothesesError}
        </p>
      ) : !hypotheses || hypotheses.length === 0 ? (
        <p className="agents-journal-state">Гипотез ещё нет — выберите отчёт и запустите Стратега.</p>
      ) : (
        <>
          <div className="agent-hypotheses-summary">
            <div className="agent-hypotheses-summary-stats">
              <div className="agent-hypotheses-summary-stat">
                <span className="agent-hypotheses-summary-value">{summary.total}</span>
                <span className="agent-hypotheses-summary-label">Всего гипотез</span>
              </div>
              <div className="agent-hypotheses-summary-stat agent-hypotheses-summary-stat-accepted">
                <span className="agent-hypotheses-summary-value">{summary.accepted}</span>
                <span className="agent-hypotheses-summary-label">Принято</span>
              </div>
              <div className="agent-hypotheses-summary-stat agent-hypotheses-summary-stat-rejected">
                <span className="agent-hypotheses-summary-value">{summary.rejected}</span>
                <span className="agent-hypotheses-summary-label">Отклонено</span>
              </div>
              <div className="agent-hypotheses-summary-stat agent-hypotheses-summary-stat-pending">
                <span className="agent-hypotheses-summary-value">{summary.pending}</span>
                <span className="agent-hypotheses-summary-label">Ждут решения</span>
              </div>
            </div>
            <p className="agent-hypotheses-summary-meta">
              По отчёту {summary.reportLabel}
              {summary.createdAt ? ` · запуск ${formatEventTime(summary.createdAt)}` : ""}
            </p>
          </div>

          <div className="agent-hypotheses-list">
            {hypotheses.map((hypothesis) => (
              <article
                key={hypothesis.id}
                className={`agent-hypothesis-card agent-hypothesis-card-${hypothesis.status}`}
              >
                <div className="agent-hypothesis-card-head">
                  <h3 className="agent-hypothesis-change">{hypothesis.change}</h3>
                  <span className={`agent-hypothesis-badge agent-hypothesis-badge-${hypothesis.status}`}>
                    {DECISION_LABELS[hypothesis.status] || hypothesis.status}
                  </span>
                </div>

                <div className="agent-hypothesis-body">
                  <p className="agent-hypothesis-field">
                    <span className="agent-hypothesis-field-label">Основание</span>
                    <span className="agent-hypothesis-field-value">{hypothesis.basis}</span>
                  </p>
                  <p className="agent-hypothesis-field">
                    <span className="agent-hypothesis-field-label">Метрика</span>
                    <span className="agent-hypothesis-field-value">{hypothesis.metric}</span>
                  </p>
                  <p className="agent-hypothesis-field">
                    <span className="agent-hypothesis-field-label">Ожидаемый эффект</span>
                    <span className="agent-hypothesis-field-value">{hypothesis.expected_effect}</span>
                  </p>
                  <p className="agent-hypothesis-field">
                    <span className="agent-hypothesis-field-label">Проверить через</span>
                    <span className="agent-hypothesis-field-value">{hypothesis.check_days} дн.</span>
                  </p>
                </div>

                {hypothesis.status === "pending" ? (
                  <div className="agent-hypothesis-actions">
                    <button
                      type="button"
                      className="mini-action mini-action-primary"
                      disabled={decisionPendingId === hypothesis.id}
                      onClick={() => onDecide(hypothesis.id, "accepted")}
                    >
                      Принять
                    </button>
                    <button
                      type="button"
                      className="mini-action"
                      disabled={decisionPendingId === hypothesis.id}
                      onClick={() => onDecide(hypothesis.id, "rejected")}
                    >
                      Отклонить
                    </button>
                  </div>
                ) : null}
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function AgentCard({ agent, isActive, onToggle, extra, expandable, expandLabel }) {
  const inputs = agent.inputs?.join(", ") || "—";
  const outputs = agent.outputs?.join(", ") || "—";

  function handleKeyDown(event) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onToggle();
    }
  }

  return (
    // div, не button: внутри (extra) может быть своя интерактивная разметка
    // (radio, button) — вложенный <button> в <button> невалиден и ломает клики.
    <div
      role="button"
      tabIndex={0}
      className={`agent-card${isActive ? " agent-card-active" : ""}`}
      onClick={onToggle}
      onKeyDown={handleKeyDown}
      aria-pressed={isActive}
    >
      <div className="agent-card-header">
        <h3 className="agent-card-name">{agent.name}</h3>
        <div className="agent-card-badges">
          {expandable ? (
            <span className="agent-badge agent-expand-hint">
              {isActive ? `▾ ${expandLabel}` : `▸ ${expandLabel}`}
            </span>
          ) : null}
          <span className={`agent-badge agent-badge-kind-${agent.kind}`}>
            {KIND_LABELS[agent.kind] || agent.kind}
          </span>
          <span className={`agent-badge agent-badge-status-${agent.status}`}>
            {STATUS_LABELS[agent.status] || agent.status}
          </span>
        </div>
      </div>

      <p className="agent-card-role">{agent.role}</p>

      <p className="agent-io-line">
        <span className="agent-io-label">Вход</span> {inputs}
        <span className="agent-io-arrow"> → </span>
        <span className="agent-io-label">Выход</span> {outputs}
      </p>

      {agent.allowed_tools?.length ? (
        <div className="agent-tools">
          {agent.allowed_tools.map((tool) => (
            <span key={tool} className="agent-tool-chip">
              {tool}
            </span>
          ))}
        </div>
      ) : null}

      {agent.modules?.length ? (
        <p className="agent-modules">{agent.modules.join(" · ")}</p>
      ) : null}

      {extra}

      <div className="agent-card-footer">
        <p className="agent-handoff">
          {agent.hands_off_to?.length ? `передаёт: ${agent.hands_off_to.join(", ")}` : "конечное звено"}
        </p>
      </div>
    </div>
  );
}

export default function AgentsPage() {
  const [agents, setAgents] = useState(null);
  const [agentsError, setAgentsError] = useState(null);
  const [activeAgent, setActiveAgent] = useState(null);
  const [journal, setJournal] = useState(null);
  const [journalError, setJournalError] = useState(null);

  // Сборщик статистики: период, статус текущего запуска, последний снимок.
  const [statsPeriod, setStatsPeriod] = useState(7);
  const [statsRunId, setStatsRunId] = useState(null);
  const [statsRunStatus, setStatsRunStatus] = useState(null);
  const [statsRunError, setStatsRunError] = useState(null);
  const [statsLatest, setStatsLatest] = useState(null);
  const [statsLatestError, setStatsLatestError] = useState(null);

  // Стратег: отчёты Разведчика, выбранный отчёт, статус запуска, гипотезы.
  const [scoutReports, setScoutReports] = useState(null);
  const [scoutReportsError, setScoutReportsError] = useState(null);
  const [selectedReportKey, setSelectedReportKey] = useState("");
  const [strategistRunning, setStrategistRunning] = useState(false);
  const [strategistError, setStrategistError] = useState(null);
  const [hypotheses, setHypotheses] = useState(null);
  const [hypothesesError, setHypothesesError] = useState(null);
  const [decisionPendingId, setDecisionPendingId] = useState(null);

  function loadStatsLatest() {
    fetchStatsLatest()
      .then((data) => {
        setStatsLatest(data);
        setStatsLatestError(null);
      })
      .catch((fetchError) => setStatsLatestError(fetchError.message));
  }

  // Последний снимок — грузим один раз при открытии страницы.
  useEffect(() => {
    loadStatsLatest();
  }, []);

  // Опрос статуса запуска Сборщика, пока он не завершится (done/error).
  useEffect(() => {
    if (!statsRunId) {
      return undefined;
    }
    let cancelled = false;

    async function poll() {
      try {
        const status = await fetchAgentRunStatus(statsRunId);
        if (cancelled) {
          return;
        }
        setStatsRunStatus(status.status);
        if (status.status === "done" || status.status === "error") {
          setStatsRunError(status.error);
          if (status.status === "done") {
            loadStatsLatest();
          }
          return;
        }
        setTimeout(poll, 2000);
      } catch (fetchError) {
        if (!cancelled) {
          setStatsRunStatus("error");
          setStatsRunError(fetchError.message);
        }
      }
    }

    poll();
    return () => {
      cancelled = true;
    };
  }, [statsRunId]);

  function handleRunStatsCollector() {
    setStatsRunError(null);
    startStatsCollectorRun(statsPeriod)
      .then((data) => {
        setStatsRunId(data.run_id);
        setStatsRunStatus("queued");
      })
      .catch((fetchError) => {
        setStatsRunStatus("error");
        setStatsRunError(fetchError.message);
      });
  }

  // Отчёты Разведчика и гипотезы последнего запуска — грузим один раз при
  // открытии страницы, как и последний снимок Сборщика.
  useEffect(() => {
    fetchScoutReports()
      .then((data) => {
        setScoutReports(Array.isArray(data) ? data : []);
        setScoutReportsError(null);
      })
      .catch((fetchError) => setScoutReportsError(fetchError.message));

    fetchHypotheses()
      .then((data) => {
        setHypotheses(Array.isArray(data) ? data : []);
        setHypothesesError(null);
      })
      .catch((fetchError) => setHypothesesError(fetchError.message));
  }, []);

  function handleRunStrategist() {
    if (!selectedReportKey || strategistRunning) {
      return;
    }
    setStrategistError(null);
    setStrategistRunning(true);
    runStrategist(selectedReportKey)
      .then((data) => {
        setHypotheses(data.hypotheses || []);
        setHypothesesError(null);
      })
      .catch((fetchError) => setStrategistError(fetchError.message))
      .finally(() => setStrategistRunning(false));
  }

  function handleDecideHypothesis(hypothesisId, decision) {
    setDecisionPendingId(hypothesisId);
    decideHypothesis(hypothesisId, decision)
      .then((updated) => {
        setHypotheses((current) =>
          (current || []).map((h) => (h.id === updated.id ? updated : h))
        );
        setHypothesesError(null);
      })
      .catch((fetchError) => setHypothesesError(fetchError.message))
      .finally(() => setDecisionPendingId(null));
  }

  // Реестр агентов грузим один раз — он не меняется на лету.
  useEffect(() => {
    let cancelled = false;

    fetchAgents()
      .then((data) => {
        if (cancelled) {
          return;
        }
        // /api/agents отдаёт голый массив (list_agents()) — без обёртки.
        setAgents(Array.isArray(data) ? data : []);
      })
      .catch((fetchError) => {
        if (cancelled) {
          return;
        }
        setAgentsError(fetchError.message);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  // Журнал: опрос раз в 5 секунд. Пауза, пока вкладка скрыта, и без наложения
  // запросов — следующий тик планируется только после того, как предыдущий
  // ответ (успех или ошибка) уже обработан.
  useEffect(() => {
    let cancelled = false;
    let timeoutId = null;

    async function poll() {
      if (cancelled) {
        return;
      }

      if (document.visibilityState !== "hidden") {
        try {
          const data = await fetchJournal({ actor: activeAgent, limit: 100 });
          if (!cancelled) {
            // /api/journal отдаёт голый массив (list_events()) — без обёртки.
            setJournal(Array.isArray(data) ? data : []);
            setJournalError(null);
          }
        } catch (fetchError) {
          if (!cancelled) {
            setJournalError(fetchError.message);
          }
        }
      }

      if (!cancelled) {
        timeoutId = window.setTimeout(poll, 5000);
      }
    }

    function handleVisibilityChange() {
      // Возврат на вкладку — обновляем сразу, не дожидаясь следующего тика.
      if (document.visibilityState === "visible") {
        window.clearTimeout(timeoutId);
        poll();
      }
    }

    document.addEventListener("visibilitychange", handleVisibilityChange);
    poll();

    return () => {
      cancelled = true;
      window.clearTimeout(timeoutId);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [activeAgent]);

  function handleCardClick(agentId) {
    setActiveAgent((current) => (current === agentId ? null : agentId));
  }

  return (
    <main className="workspace-page agents-page">
      <div className="page-noise" />

      <div className="page-container workspace-page-shell">
        <header className="workspace-page-header">
          <Topbar
            ariaLabel="Навигация реестра агентов"
            links={[
              { to: "/workspace", label: "Аналитика" },
              { to: "/draft", label: "Черновик объявления" },
              { to: "/it-outreach", label: "Рассылка IT" },
            ]}
          />

          <div className="workspace-header-shell">
            <div className="workspace-header-copy">
              <p className="section-kicker">Agents</p>
              <h1 className="workspace-title">Реестр агентов</h1>
              <p className="workspace-intro-copy">
                Пять агентов системы — роли, входы и выходы, разрешённые инструменты — и общий
                журнал того, что они делали. Цифры считает код, решения принимает человек.
              </p>
            </div>
          </div>
        </header>

        <section className="workspace-main-stack" aria-label="Реестр и журнал агентов">
          {agentsError ? (
            <AgentsStatePanel
              kicker="Error state"
              title="Реестр агентов недоступен"
              copy={agentsError}
              tone="error"
            />
          ) : !agents ? (
            <AgentsStatePanel
              kicker="Agents"
              title="Загружаем реестр"
              copy="Подтягиваем список агентов и их инструменты."
            />
          ) : (
            <div className="agents-grid">
              {agents.map((agent) => (
                <AgentCard
                  key={agent.id}
                  agent={agent}
                  isActive={activeAgent === agent.id}
                  onToggle={() => handleCardClick(agent.id)}
                  expandable={agent.id === "stats_collector" || agent.id === "strategist"}
                  expandLabel={agent.id === "stats_collector" ? "снимок" : "гипотезы"}
                  extra={
                    agent.id === "stats_collector" ? (
                      <StatsCollectorControls
                        periodDays={statsPeriod}
                        onPeriodChange={setStatsPeriod}
                        isRunning={statsRunStatus === "queued" || statsRunStatus === "running"}
                        runStatus={statsRunStatus}
                        runError={statsRunError}
                        onRun={handleRunStatsCollector}
                        latest={statsLatest}
                        latestError={statsLatestError}
                      />
                    ) : agent.id === "strategist" ? (
                      <StrategistControls
                        reports={scoutReports}
                        reportsError={scoutReportsError}
                        selectedReportKey={selectedReportKey}
                        onSelectReport={setSelectedReportKey}
                        isRunning={strategistRunning}
                        runError={strategistError}
                        onRun={handleRunStrategist}
                      />
                    ) : null
                  }
                />
              ))}
            </div>
          )}

          {activeAgent === "stats_collector" ? (
            <SnapshotDetail snapshot={statsLatest} snapshotError={statsLatestError} />
          ) : null}

          {activeAgent === "strategist" ? (
            <HypothesesPanel
              hypotheses={hypotheses}
              hypothesesError={hypothesesError}
              onDecide={handleDecideHypothesis}
              decisionPendingId={decisionPendingId}
              scoutReports={scoutReports}
            />
          ) : null}

          <section className="workspace-panel agents-journal-panel" aria-label="Журнал событий">
            <div className="workspace-panel-header">
              <div>
                <p className="section-kicker">Журнал</p>
                <h2 className="workspace-panel-title">Что делали агенты</h2>
              </div>
              {activeAgent ? (
                <button type="button" className="mini-action" onClick={() => setActiveAgent(null)}>
                  Фильтр: {activeAgent} · сбросить
                </button>
              ) : null}
            </div>

            {journalError ? (
              <p className="agents-journal-state agents-journal-state-error">
                Журнал недоступен: {journalError}
              </p>
            ) : journal === null ? (
              <p className="agents-journal-state">Загружаем события…</p>
            ) : journal.length === 0 ? (
              <p className="agents-journal-state">
                {activeAgent
                  ? `Для «${activeAgent}» пока нет событий в журнале.`
                  : "Журнал пуст — агенты ещё не запускались."}
              </p>
            ) : (
              <div className="workspace-results-scroll">
                <table className="workspace-results-table agents-journal-table">
                  <thead>
                    <tr>
                      <th>Время</th>
                      <th>Агент</th>
                      <th>Тип</th>
                      <th>Событие</th>
                    </tr>
                  </thead>
                  <tbody>
                    {journal.map((event, index) => (
                      <tr key={event.id ?? `${event.run_id ?? "no-run"}-${event.ts}-${index}`} className="workspace-results-row">
                        <td className="agents-journal-time">{formatEventTime(event.ts)}</td>
                        <td>{event.actor}</td>
                        <td>
                          <span className={`agent-badge agent-badge-event-type-${event.type}`}>{event.type}</span>
                        </td>
                        <td>{event.summary}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </section>

        <SiteFooter
          links={[
            { to: "/workspace", label: "Аналитика" },
            { to: "/draft", label: "Черновик объявления" },
          ]}
        />
      </div>
    </main>
  );
}
