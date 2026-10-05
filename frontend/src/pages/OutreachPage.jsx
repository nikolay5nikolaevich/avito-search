import { useEffect, useMemo, useRef, useState } from "react";
import SiteFooter from "../components/SiteFooter";
import Topbar from "../components/Topbar";
import {
  fetchBootstrap,
  fetchOutreachHistory,
  fetchOutreachResult,
  fetchOutreachStatus,
  importOutreachContacts,
  resumeOutreach,
  sendOutreachMessages,
  startOutreachCollection,
} from "../lib/api";
import {
  buildSendPayload,
  createOutreachStorage,
  DEFAULT_OUTREACH_FORM,
  describeOutreachResumePlan,
  outreachResumeActions,
  outreachResumeUnavailableMessage,
} from "../lib/outreach";

// Шаги сценария рассылки (для ленты прогресса) — зеркало backend outreach.py/
// app.py job["step"], синхронизировать при изменении. Общий набор для сбора
// кандидатов и отправки писем: какие шаги реально пройдены, решает бэкенд
// через job["step"] — лента просто подсвечивает совпадающий ключ.
const OUTREACH_STEPS = [
  { key: "connect_chrome", label: "Подключение к Chrome" },
  { key: "open_listing",   label: "Открытие объявления" },
  { key: "check_seller",   label: "Сверка продавца" },
  { key: "open_chat",      label: "Открытие чата" },
  { key: "fill_message",   label: "Ввод письма" },
  { key: "send_click",     label: "Отправка сообщения" },
  { key: "confirm_sent",   label: "Подтверждение отправки" },
  { key: "back_to_search", label: "Возврат к выдаче" },
];

// Предел шаблона письма — maxlength поля ввода в мессенджере Авито
// (см. backend/app.py, OUTREACH_MESSAGE_MAX_LENGTH). Дублируем на фронте,
// чтобы не дать напечатать/вставить лишнее, но сервер всё равно проверяет
// заново: текст может попасть в поле и мимо формы.
const OUTREACH_MESSAGE_MAX_LENGTH = 1000;

function browserStorage() {
  return typeof window === "undefined" ? null : createOutreachStorage(window.localStorage);
}

function readInitialForm() {
  try {
    return browserStorage()?.load() || { ...DEFAULT_OUTREACH_FORM };
  } catch {
    return { ...DEFAULT_OUTREACH_FORM };
  }
}

function resultCandidates(result) {
  if (Array.isArray(result)) return result;
  if (Array.isArray(result?.candidates)) return result.candidates;
  if (Array.isArray(result?.result?.candidates)) return result.result.candidates;
  return [];
}

function historyRows(history) {
  return Array.isArray(history) ? history : history?.contacts || [];
}

function formatJobProgress(status) {
  const current = status?.current ?? status?.progress?.current;
  const total = status?.total ?? status?.progress?.total;
  return Number.isFinite(current) && Number.isFinite(total) ? `${current} из ${total}` : "Выполняем в Chrome…";
}

function candidateKey(candidate) {
  return candidate.seller_key || candidate.seller_id || candidate.profile_url;
}

// Лента шагов + счётчик текущего продавца — общий блок для панелей «Собираем»/
// «Отправляем» и для состояния после остановки. Видимость целиком определяется
// наличием status.step с бэкенда (визуально как в DraftPage.PUBLISH_STEPS).
function OutreachProgress({ status, isTerminal }) {
  const currentStepIndex = OUTREACH_STEPS.findIndex((step) => step.key === status?.step);
  const itemsTotal = typeof status?.items_total === "number" ? status.items_total : null;
  const itemIndex = typeof status?.item_index === "number" ? status.item_index : null;

  return (
    <>
      {itemsTotal != null && itemIndex != null ? (
        <div className="draft-batch-counter">
          <span className="draft-batch-counter-main">Продавец {itemIndex} из {itemsTotal}</span>
        </div>
      ) : null}

      {status?.step ? (
        <ol className="draft-steps-list">
          {OUTREACH_STEPS.map((step, index) => {
            let state = "pending";
            if (index < currentStepIndex) {
              state = "done";
            } else if (index === currentStepIndex) {
              state = isTerminal ? "error" : "active";
            }

            return (
              <li key={step.key} className={`draft-step draft-step-${state}`}>
                <span className="draft-step-icon" aria-hidden="true">
                  {state === "done" ? "✓" : state === "error" ? "✕" : state === "active" ? "●" : "○"}
                </span>
                <span className="draft-step-label">{step.label}</span>
                {state === "active" && !isTerminal ? (
                  <span className="draft-step-spinner" aria-hidden="true" />
                ) : null}
              </li>
            );
          })}
        </ol>
      ) : null}
    </>
  );
}

export default function OutreachPage() {
  const [form, setForm] = useState(readInitialForm);
  const [cities, setCities] = useState([]);
  const [phase, setPhase] = useState("form");
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState(null);
  const [candidates, setCandidates] = useState([]);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [importText, setImportText] = useState("");
  const [importNote, setImportNote] = useState("");
  const [history, setHistory] = useState([]);
  const [isResuming, setIsResuming] = useState(false);

  // Панель «Рассылка остановлена» должна быть заметна сразу — сюда прокручиваем
  // при её появлении (см. эффект ниже), ref на второй флаг не даёт скроллить
  // повторно на каждый ре-рендер статуса, только на сам переход в остановку.
  const stoppedPanelRef = useRef(null);
  const stoppedPanelShownRef = useRef(false);

  const selectedCount = useMemo(
    () => candidates.filter((candidate) => candidate.selected).length,
    [candidates],
  );

  const messageLength = form.message.length;
  const messageOverLimit = form.message.trim().length > OUTREACH_MESSAGE_MAX_LENGTH;
  const messageNearLimit = messageLength >= OUTREACH_MESSAGE_MAX_LENGTH * 0.9;

  useEffect(() => {
    fetchBootstrap()
      .then((data) => setCities(data.cities || []))
      .catch((fetchError) => setError(fetchError.message));
    fetchOutreachHistory()
      .then((data) => setHistory(historyRows(data)))
      .catch(() => {});
  }, []);

  useEffect(() => {
    try {
      browserStorage()?.save(form);
    } catch {
      // Браузер без localStorage всё равно позволяет работать с текущей формой.
    }
  }, [form]);

  useEffect(() => {
    if (!jobId || (phase !== "collecting" && phase !== "sending")) return undefined;
    let cancelled = false;
    let timeoutId;

    async function poll() {
      try {
        const status = await fetchOutreachStatus(jobId);
        if (cancelled) return;
        setJobStatus(status);
        if (status.status === "done") {
          const completed = await fetchOutreachResult(jobId);
          if (cancelled) return;
          setResult(completed);
          if (phase === "collecting") {
            setCandidates(resultCandidates(completed).map((candidate) => ({ ...candidate, selected: true })));
            setPhase("preview");
          } else {
            setPhase("result");
            fetchOutreachHistory().then((data) => setHistory(historyRows(data))).catch(() => {});
          }
          return;
        }
        if (["error", "failed", "needs_user_action", "interrupted"].includes(status.status)) {
          setError(status.error || "Задача остановлена. Проверьте Chrome и повторите после проверки.");
          setPhase("result");
          return;
        }
        timeoutId = window.setTimeout(poll, 1500);
      } catch (pollError) {
        if (!cancelled) {
          setError(pollError.message);
          setPhase("result");
        }
      }
    }

    poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timeoutId);
    };
  }, [jobId, phase]);

  // Прокрутка к панели «Рассылка остановлена» — один раз на переход в это
  // состояние, а не на каждый ре-рендер (статус обновляется по таймеру и без
  // защитного флага дёргал бы страницу при каждом опросе).
  useEffect(() => {
    const showStoppedPanel = phase === "result" && !result && Boolean(jobStatus);
    if (showStoppedPanel && !stoppedPanelShownRef.current) {
      stoppedPanelShownRef.current = true;
      stoppedPanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    if (!showStoppedPanel) {
      stoppedPanelShownRef.current = false;
    }
  }, [phase, result, jobStatus]);

  function updateForm(event) {
    const { name, value } = event.target;
    setForm((current) => ({ ...current, [name]: name === "limit" ? value : value }));
  }

  async function handleCollect(event) {
    event.preventDefault();
    setError("");
    setResult(null);
    setCandidates([]);
    setJobStatus(null);
    try {
      const started = await startOutreachCollection({
        city: form.city,
        query: form.query.trim(),
        limit: Number(form.limit),
      });
      setJobId(started.job_id);
      setPhase("collecting");
    } catch (collectError) {
      setError(collectError.message);
    }
  }

  function toggleCandidate(key) {
    setCandidates((current) => current.map((candidate) => (
      candidateKey(candidate) === key ? { ...candidate, selected: !candidate.selected } : candidate
    )));
  }

  async function handleSend() {
    if (!jobId || selectedCount === 0 || !form.message.trim() || messageOverLimit) return;
    if (!window.confirm(`Отправить одно и то же сообщение ${selectedCount} продавцам?`)) return;

    setError("");
    setJobStatus(null);
    setResult(null);
    try {
      const started = await sendOutreachMessages(jobId, buildSendPayload(candidates, form.message));
      setJobId(started.job_id || jobId);
      setPhase("sending");
    } catch (sendError) {
      setError(sendError.message);
    }
  }

  async function handleImport(event) {
    event.preventDefault();
    const urls = importText.split(/\r?\n/).map((url) => url.trim()).filter(Boolean);
    if (!urls.length) return;
    try {
      const response = await importOutreachContacts(urls);
      setImportNote(`Добавлено: ${response.imported || 0}, уже было: ${response.skipped || 0}.`);
      setImportText("");
      const updated = await fetchOutreachHistory();
      setHistory(historyRows(updated));
    } catch (importError) {
      setImportNote(importError.message);
    }
  }

  // Возобновление доступно только для отправки: единственный источник
  // правды — resume_plan с бэкенда, фронт сам не решает, безопасно ли повторять.
  const resumePlan = describeOutreachResumePlan(jobStatus ?? {});
  const resumeActions = outreachResumeActions(jobStatus ?? {});
  const resumeUnavailableReason = outreachResumeUnavailableMessage(jobStatus ?? {});
  const canResume = jobStatus?.resume_available === true;

  // Остановка на середине пакета (сбор или отправка прервались, result не
  // пришёл) — при любом терминальном статусе с остановкой (failed,
  // needs_user_action, interrupted и т.п.) панель обязана показаться, а
  // тон (красный/жёлтый) — как в DraftPage: needs_user_action/interrupted
  // мягче, остальное — ошибка.
  const showStoppedPanel = phase === "result" && !result && Boolean(jobStatus);
  const stoppedPanelTone = jobStatus?.status === "needs_user_action" || jobStatus?.status === "interrupted"
    ? "action"
    : "error";

  async function handleResumeOutreach(action) {
    setIsResuming(true);
    setError("");
    try {
      await resumeOutreach(jobId, action);
      setPhase("sending");
    } catch (resumeError) {
      setError(resumeError.message || "Не удалось продолжить рассылку");
    } finally {
      setIsResuming(false);
    }
  }

  const busy = phase === "collecting" || phase === "sending";

  return (
    <main className="workspace-page outreach-page">
      <div className="page-noise" />
      <div className="page-container workspace-page-shell">
        <header className="workspace-page-header">
          <Topbar
            ariaLabel="Навигация рассылки"
            links={[
              { to: "/workspace", label: "Аналитика" },
              { to: "/draft", label: "Публикация" },
              { to: "/seller", label: "Разбор продавца" },
              { to: "/resale", label: "Перепродажа" },
              { to: "/it-outreach", label: "Рассылка IT" },
              { to: "/agents", label: "Агенты" },
            ]}
          />
          <div className="workspace-header-shell outreach-header-shell">
            <div className="workspace-header-copy">
              <p className="section-kicker">Рассылка</p>
              <h1 className="workspace-title">Найдите магазины и напишите им</h1>
              <p className="workspace-intro-copy">
                Берём только продавцов с 50+ отзывами, исключаем тех, кому уже писали, и всегда показываем список до отправки.
              </p>
            </div>
            <p className="outreach-rule">Минимум<br /><strong>50 отзывов</strong></p>
          </div>
        </header>

        <section className="workspace-main-stack" aria-label="Поиск продавцов для рассылки">
          {error ? <p className="draft-general-error outreach-error" role="alert">{error}</p> : null}

          {/* Остановка на середине пакета: сбор или отправка прервались, result
              не пришёл. Сразу под баннером ошибки и визуально главная на экране —
              пользователь не должен искать кнопку продолжения ниже формы.
              Видимость и смысл кнопки целиком решает resume_plan с бэкенда
              (см. lib/outreach.js), тон блока — как в терминальных состояниях
              DraftPage.jsx (draft-terminal-error/-action). */}
          {showStoppedPanel ? (
            <section
              ref={stoppedPanelRef}
              className={`workspace-panel outreach-status-panel draft-terminal draft-terminal-${stoppedPanelTone}`}
            >
              <p className="section-kicker">Остановлено</p>
              <h2 className="draft-terminal-title">Рассылка остановлена</h2>
              <OutreachProgress status={jobStatus} isTerminal />

              {resumeActions.some((action) => !action.disabled) ? (
                <div style={{ marginTop: "1rem" }}>
                  {resumePlan ? <p className="draft-terminal-copy">{resumePlan.description}</p> : null}
                  {resumeActions.map((action) => (
                    <div key={action.action} style={{ marginTop: "0.5rem" }}>
                      <p className="draft-terminal-copy">{action.description}</p>
                      <button
                        className="submit-button"
                        type="button"
                        disabled={isResuming || action.disabled}
                        onClick={() => handleResumeOutreach(action.action)}
                      >
                        {isResuming ? "Продолжаем..." : action.label}
                      </button>
                    </div>
                  ))}
                </div>
              ) : null}

              {!canResume && resumeUnavailableReason ? (
                <p className="draft-terminal-copy" style={{ marginTop: "1rem" }}>
                  {resumeUnavailableReason}
                </p>
              ) : null}
            </section>
          ) : null}

          {phase === "form" || phase === "result" ? (
            <form className="workspace-panel outreach-form" onSubmit={handleCollect}>
              <div className="workspace-panel-header">
                <div>
                  <p className="section-kicker">Шаг 1</p>
                  <h2 className="workspace-panel-title">Собрать кандидатов</h2>
                </div>
                <p className="outreach-history-count">В истории: {history.length}</p>
              </div>
              <div className="outreach-form-grid">
                <label className="field-shell">
                  <span>Город</span>
                  <select className="field-input draft-select" name="city" value={form.city} onChange={updateForm} required>
                    <option value="">Выберите город</option>
                    {cities.map((city) => <option key={city.slug} value={city.slug}>{city.name}</option>)}
                  </select>
                </label>
                <label className="field-shell">
                  <span>Поисковый запрос</span>
                  <input className="field-input" name="query" value={form.query} onChange={updateForm} placeholder="Например: iPhone 17 Pro Max" required />
                </label>
                <label className="field-shell">
                  <span>Лимит новых продавцов</span>
                  <input className="field-input" type="number" name="limit" min="1" max="150" value={form.limit} onChange={updateForm} required />
                </label>
              </div>
              <label className="field-shell outreach-message-field">
                <span className="outreach-message-field-header">
                  <span>Шаблон письма</span>
                  <span className={`outreach-message-count${messageNearLimit ? " outreach-message-count-warn" : ""}`}>
                    {messageLength} / {OUTREACH_MESSAGE_MAX_LENGTH}
                  </span>
                </span>
                <textarea
                  className="field-input outreach-textarea"
                  name="message"
                  value={form.message}
                  onChange={updateForm}
                  placeholder="Один текст будет отправлен всем выбранным продавцам"
                  rows="5"
                  maxLength={OUTREACH_MESSAGE_MAX_LENGTH}
                  required
                />
              </label>
              <button className="submit-button outreach-collect-button" type="submit" disabled={busy}>Собрать кандидатов</button>
            </form>
          ) : null}

          {phase === "collecting" ? (
            <section className="workspace-panel outreach-status-panel" aria-live="polite">
              <p className="section-kicker">Собираем</p>
              <h2 className="workspace-panel-title">Проверяем выдачу и историю контактов</h2>
              <p className="workspace-body-copy">{formatJobProgress(jobStatus)}. Сообщения пока не отправляются.</p>
              <OutreachProgress status={jobStatus} isTerminal={false} />
            </section>
          ) : null}

          {phase === "preview" ? (
            <section className="workspace-panel outreach-preview">
              <div className="workspace-panel-header">
                <div>
                  <p className="section-kicker">Шаг 2</p>
                  <h2 className="workspace-panel-title">Проверьте список перед отправкой</h2>
                </div>
                <p className="outreach-selected-count">Выбрано: {selectedCount}</p>
              </div>
              {candidates.length === 0 ? (
                <p className="workspace-body-copy">Подходящих новых продавцов не найдено.</p>
              ) : (
                <div className="outreach-candidate-list">
                  {candidates.map((candidate) => {
                    const key = candidateKey(candidate);
                    return (
                      <label key={key} className="outreach-candidate">
                        <input type="checkbox" checked={Boolean(candidate.selected)} onChange={() => toggleCandidate(key)} />
                        <span className="outreach-candidate-main">
                          <strong>{candidate.seller_name || "Продавец без названия"}</strong>
                          <span>{candidate.review_count} отзывов</span>
                        </span>
                        <span className="outreach-candidate-links">
                          {candidate.profile_url ? <a href={candidate.profile_url} target="_blank" rel="noreferrer">Профиль</a> : null}
                          {candidate.listing_url ? <a href={candidate.listing_url} target="_blank" rel="noreferrer">Объявление</a> : null}
                        </span>
                      </label>
                    );
                  })}
                </div>
              )}
              <div className="outreach-preview-actions">
                <button className="mini-action" type="button" onClick={() => setPhase("form")}>Изменить поиск</button>
                <button className="submit-button outreach-send-button" type="button" disabled={!selectedCount || !form.message.trim() || messageOverLimit} onClick={handleSend}>
                  Отправить {selectedCount} сообщений
                </button>
              </div>
            </section>
          ) : null}

          {phase === "sending" ? (
            <section className="workspace-panel outreach-status-panel" aria-live="polite">
              <p className="section-kicker">Отправляем</p>
              <h2 className="workspace-panel-title">Сообщения отправляются по одному</h2>
              <p className="workspace-body-copy">{formatJobProgress(jobStatus)}. При непонятном состоянии процесс остановится.</p>
              <OutreachProgress status={jobStatus} isTerminal={false} />
            </section>
          ) : null}

          {phase === "result" && result ? (
            <section className="workspace-panel outreach-status-panel">
              <p className="section-kicker">Готово</p>
              <h2 className="workspace-panel-title">Результат рассылки</h2>
              <p className="workspace-body-copy">{result.summary || `Задача завершена: ${result.sent ?? 0} сообщений отправлено.`}</p>
            </section>
          ) : null}

          <form className="workspace-panel outreach-import" onSubmit={handleImport}>
            <div className="workspace-panel-header">
              <div>
                <p className="section-kicker">История</p>
                <h2 className="workspace-panel-title">Кому уже писали</h2>
              </div>
              <p className="outreach-history-count">{history.length} продавцов</p>
            </div>
            <label className="field-shell outreach-message-field">
              <span>Вставьте ссылки, по одной в строке</span>
              <textarea className="field-input outreach-textarea" value={importText} onChange={(event) => setImportText(event.target.value)} placeholder="https://www.avito.ru/brands/..." rows="3" />
            </label>
            <div className="outreach-preview-actions">
              <button className="mini-action" type="submit">Добавить в историю</button>
              {importNote ? <p className="outreach-import-note" role="status">{importNote}</p> : null}
            </div>
          </form>
        </section>

        <SiteFooter links={[{ to: "/workspace", label: "Аналитика" }, { to: "/draft", label: "Публикация" }]} />
      </div>
    </main>
  );
}
