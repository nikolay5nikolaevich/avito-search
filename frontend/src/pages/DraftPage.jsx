import { useEffect, useRef, useState } from "react";
import { fetchPublishResult, fetchPublishStatus, startPublish } from "../lib/api";
import { buildPublishFormData, TERMINAL_STATUSES } from "../lib/publish";
import FieldShell from "../components/FieldShell";
import ProgressBar from "../components/ProgressBar";
import SiteFooter from "../components/SiteFooter";
import Topbar from "../components/Topbar";

// ─── Справочники (значения — ровно те строки, что шлём на бэкенд) ────────────
// Зеркало ключей словарей backend/avito_publish_selectors.py
// (TRADE_TYPE_OPTIONS, CONDITION_OPTIONS, SIZE_OPTIONS, COLOR_OPTIONS) —
// при изменении синхронизировать, сами значения не менять.

const AD_TYPES = [
  "Продаю своё",
  "Товар приобретён на продажу",
  "Товар от производителя",
];

const CONDITIONS = [
  "Новое с биркой",
  "Отличное",
  "Хорошее",
  "Удовлетворительное",
];

const SIZES = [
  "40 (XXS)", "42 (XS)", "44 (XS/S)", "46 (S)", "48 (M)", "50 (L)",
  "52 (L/XL)", "54 (XL)", "56 (XXL)", "58 (XXL)", "60 (3XL)", "62 (4XL)",
  "64 (5XL)", "66 (6XL)", "68 (7XL)", "70 (7XL)", "72 (8XL)", "74 (8XL)",
  "76 (9XL)", "78 (10XL)", "80 (10XL)", "82+ (10XL+)", "One size", "Без размера",
];

const COLORS = [
  "Чёрный", "Серый", "Синий", "Белый", "Бежевый", "Коричневый",
  "Бордовый", "Красный", "Розовый", "Оранжевый", "Жёлтый", "Зелёный",
  "Голубой", "Фиолетовый", "Серебряный", "Золотой", "Разноцветный",
];

// Шаги сценария (для экрана прогресса) —
// зеркало backend/publisher.py STEPS, синхронизировать при изменении
const PUBLISH_STEPS = [
  { key: "connect_chrome",   label: "Подключение к Chrome" },
  { key: "open_form",        label: "Открытие формы Avito" },
  { key: "select_category",  label: "Выбор категории" },
  { key: "check_category",   label: "Проверка категории" },
  { key: "fill_title",       label: "Название объявления" },
  { key: "upload_photos",    label: "Загрузка фотографий" },
  { key: "fill_fields",      label: "Заполнение характеристик" },
  { key: "fill_description", label: "Описание" },
  { key: "fill_price",       label: "Цена" },
  { key: "fill_address",     label: "Адрес" },
  { key: "save_draft",       label: "Сохранение черновика" },
];

// Заголовок панели прогресса по статусу задачи
// (done в пакетном режиме считается отдельно — со счётчиком черновиков)
const PANEL_TITLES = {
  done: "Черновик сохранён",
  failed: "Произошла ошибка",
  needs_user_action: "Требуется действие",
};

const ACCEPTED_MIME = "image/jpeg,image/png,image/gif,image/heic";
const MAX_PHOTOS = 10;
const MAX_FILE_SIZE_MB = 25;

// ─── Вспомогательные функции ──────────────────────────────────────────────────

function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} КБ`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
}

// ─── Форма черновика ──────────────────────────────────────────────────────────

// Имена полей — серверные (совпадают с параметрами /api/publish/start)
function buildInitialForm() {
  return {
    title: "",
    trade_type: "Продаю своё",
    condition: "Отличное",
    size: "",
    brand: "",
    color: "",
    description: "",
    price: "",
    city: "",
    address: "",
    drafts_count: 1,
  };
}

function validate(form, photos) {
  const errors = {};

  if (!form.title.trim()) errors.title = "Укажите название";
  if (!form.trade_type) errors.trade_type = "Выберите вид объявления";
  if (!form.condition) errors.condition = "Выберите состояние";
  if (!form.size) errors.size = "Выберите размер";
  if (!form.brand.trim()) errors.brand = "Укажите бренд";
  if (!form.color) errors.color = "Выберите цвет";
  if (!form.description.trim()) errors.description = "Добавьте описание";
  if (!form.city.trim()) errors.city = "Укажите город";
  if (!form.address.trim()) errors.address = "Укажите адрес";

  const priceNum = Number(form.price);
  if (!form.price || !Number.isInteger(priceNum) || priceNum <= 0) {
    errors.price = "Укажите цену — целое число больше 0";
  }

  if (photos.length === 0) errors.photos = "Добавьте хотя бы одно фото";
  if (photos.length > MAX_PHOTOS) errors.photos = `Максимум ${MAX_PHOTOS} фотографий`;

  for (const file of photos) {
    if (file.size > MAX_FILE_SIZE_MB * 1024 * 1024) {
      errors.photos = `Файл «${file.name}» превышает ${MAX_FILE_SIZE_MB} МБ`;
      break;
    }
  }

  const draftsNum = Number(form.drafts_count);
  if (!Number.isInteger(draftsNum) || draftsNum < 1 || draftsNum > 10) {
    errors.drafts_count = "Укажите число от 1 до 10";
  }

  return errors;
}

function DraftForm({ onStarted }) {
  const [form, setForm] = useState(buildInitialForm());
  const [photos, setPhotos] = useState([]);
  const [errors, setErrors] = useState({});
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [generalError, setGeneralError] = useState("");
  const fileInputRef = useRef(null);

  // Сбрасывает ошибку поля после того, как пользователь его поправил
  function clearError(field) {
    if (errors[field]) setErrors((prev) => ({ ...prev, [field]: "" }));
  }

  function updateField(e) {
    const { name, value } = e.target;
    setForm((prev) => ({ ...prev, [name]: value }));
    clearError(name);
  }

  function handleFilesChange(e) {
    const selected = Array.from(e.target.files || []);
    const combined = [...photos, ...selected].slice(0, MAX_PHOTOS);
    setPhotos(combined);
    clearError("photos");
    // Сбрасываем input, чтобы можно было добавлять одни и те же файлы повторно
    e.target.value = "";
  }

  function removePhoto(index) {
    setPhotos((prev) => prev.filter((_, i) => i !== index));
    clearError("photos");
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setGeneralError("");

    const validationErrors = validate(form, photos);
    if (Object.keys(validationErrors).length > 0) {
      setErrors(validationErrors);
      // Прокручиваем к первой ошибке
      const firstKey = Object.keys(validationErrors)[0];
      document.querySelector(`[data-field="${firstKey}"]`)?.scrollIntoView({
        behavior: "smooth",
        block: "center",
      });
      return;
    }

    setIsSubmitting(true);

    try {
      const fd = buildPublishFormData(form, photos);
      const result = await startPublish(fd);
      onStarted(result.job_id, {
        title: form.title.trim(),
        price: Number(form.price),
        city: form.city.trim(),
        photosCount: photos.length,
        draftsCount: Number(form.drafts_count),
      });
    } catch (err) {
      if (err.fieldErrors) {
        setErrors(err.fieldErrors);
      } else {
        setGeneralError(err.message || "Не удалось запустить задачу");
      }
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <form className="workspace-panel draft-form space-y-5" onSubmit={handleSubmit}>
      {/* Предупреждение о категории */}
      <div className="draft-category-notice">
        <span className="section-kicker">Категория</span>
        <p className="draft-category-text">
          Работает только категория{" "}
          <strong>«Пиджаки и костюмы»</strong> (мужская одежда).
          Перед запуском убедитесь, что в вашем Chrome на avito.ru/additem открыта именно эта категория.
        </p>
      </div>

      {/* Общая ошибка */}
      {generalError ? (
        <div className="draft-general-error">
          <span className="section-kicker" style={{ color: "rgba(255,120,120,0.8)" }}>Ошибка</span>
          <p className="workspace-body-copy" style={{ marginTop: "0.5rem" }}>{generalError}</p>
        </div>
      ) : null}

      {/* Название */}
      <div data-field="title">
        <FieldShell label="Название" error={errors.title}>
          <input
            className="field-input"
            type="text"
            name="title"
            value={form.title}
            onChange={updateField}
            placeholder="Например: Пиджак Hugo Boss классический"
            maxLength={120}
          />
        </FieldShell>
      </div>

      {/* Вид объявления */}
      <div data-field="trade_type">
        <FieldShell label="Вид объявления" error={errors.trade_type}>
          <select
            className="field-input draft-select"
            name="trade_type"
            value={form.trade_type}
            onChange={updateField}
          >
            {AD_TYPES.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </FieldShell>
      </div>

      {/* Состояние */}
      <div data-field="condition">
        <FieldShell label="Состояние" error={errors.condition}>
          <div className="draft-radio-group">
            {CONDITIONS.map((c) => (
              <label
                key={c}
                className={`draft-pill draft-radio-chip${form.condition === c ? " draft-radio-chip-active" : ""}`}
              >
                <input
                  type="radio"
                  name="condition"
                  value={c}
                  checked={form.condition === c}
                  onChange={updateField}
                />
                <span>{c}</span>
              </label>
            ))}
          </div>
        </FieldShell>
      </div>

      {/* Фотографии */}
      <div data-field="photos">
        <FieldShell label={`Фотографии (${photos.length}/${MAX_PHOTOS})`} error={errors.photos}>
          <div className="draft-photo-zone">
            <button
              type="button"
              className="draft-pill draft-photo-add-btn"
              onClick={() => fileInputRef.current?.click()}
              disabled={photos.length >= MAX_PHOTOS}
            >
              {photos.length >= MAX_PHOTOS ? "Максимум достигнут" : "+ Добавить фото"}
            </button>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ACCEPTED_MIME}
              style={{ display: "none" }}
              onChange={handleFilesChange}
            />
            <p className="draft-photo-hint">
              JPEG, PNG, GIF, HEIC · до {MAX_FILE_SIZE_MB} МБ/файл · максимум {MAX_PHOTOS} фото
            </p>
          </div>

          {photos.length > 0 ? (
            <ul className="draft-photo-list">
              {photos.map((file, i) => (
                <li key={`${file.name}-${i}`} className="draft-photo-item">
                  <span className="draft-photo-name">{file.name}</span>
                  <span className="draft-photo-size">{formatFileSize(file.size)}</span>
                  <button
                    type="button"
                    className="draft-photo-remove"
                    onClick={() => removePhoto(i)}
                    aria-label={`Удалить ${file.name}`}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </FieldShell>
      </div>

      {/* Размер + Бренд */}
      <div className="draft-two-col">
        <div data-field="size">
          <FieldShell label="Размер" error={errors.size}>
            <select
              className="field-input draft-select"
              name="size"
              value={form.size}
              onChange={updateField}
            >
              <option value="">— Выберите размер —</option>
              {SIZES.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </FieldShell>
        </div>

        <div data-field="brand">
          <FieldShell label="Бренд" error={errors.brand}>
            <input
              className="field-input"
              type="text"
              name="brand"
              value={form.brand}
              onChange={updateField}
              placeholder="Например: Hugo Boss"
            />
          </FieldShell>
        </div>
      </div>

      {/* Цвет */}
      <div data-field="color">
        <FieldShell label="Цвет" error={errors.color}>
          <select
            className="field-input draft-select"
            name="color"
            value={form.color}
            onChange={updateField}
          >
            <option value="">— Выберите цвет —</option>
            {COLORS.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </FieldShell>
      </div>

      {/* Описание */}
      <div data-field="description">
        <FieldShell label="Описание" error={errors.description}>
          <textarea
            className="field-input draft-textarea"
            name="description"
            value={form.description}
            onChange={updateField}
            placeholder="Опишите состояние, историю вещи, особенности"
            rows={5}
          />
        </FieldShell>
      </div>

      {/* Цена */}
      <div data-field="price">
        <FieldShell label="Цена, ₽" error={errors.price}>
          <input
            className="field-input"
            type="number"
            name="price"
            value={form.price}
            onChange={updateField}
            placeholder="3500"
            min="1"
            step="1"
          />
        </FieldShell>
      </div>

      {/* Город + Адрес */}
      <div className="draft-two-col">
        <div data-field="city">
          <FieldShell label="Город" error={errors.city}>
            <input
              className="field-input"
              type="text"
              name="city"
              value={form.city}
              onChange={updateField}
              placeholder="Москва"
            />
          </FieldShell>
        </div>

        <div data-field="address">
          <FieldShell label="Адрес (улица, дом)" error={errors.address}>
            <input
              className="field-input"
              type="text"
              name="address"
              value={form.address}
              onChange={updateField}
              placeholder="ул. Пушкина, д. 10"
            />
          </FieldShell>
        </div>
      </div>

      {/* Сколько черновиков */}
      <div data-field="drafts_count">
        <FieldShell label="Сколько черновиков" error={errors.drafts_count}>
          <select
            className="field-input draft-select"
            name="drafts_count"
            value={form.drafts_count}
            onChange={updateField}
          >
            {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
          <p className="draft-batch-hint">
            Будут созданы одинаковые черновики — каждый можно отредактировать на Авито перед публикацией.
          </p>
        </FieldShell>
      </div>

      {/* Кнопка запуска */}
      <div className="draft-submit-row">
        <button className="submit-button" type="submit" disabled={isSubmitting}>
          {isSubmitting ? "Запускаем..." : "Сохранить черновик на Авито"}
        </button>
      </div>
    </form>
  );
}

// ─── Экран прогресса ──────────────────────────────────────────────────────────

function PublishProgressPanel({ jobId, summary, onBack }) {
  const [status, setStatus] = useState(null);
  const [result, setResult] = useState(null);
  const [pollError, setPollError] = useState("");

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const s = await fetchPublishStatus(jobId);
        if (cancelled) return;
        setStatus(s);

        if (TERMINAL_STATUSES.has(s.status)) {
          // Терминальное состояние — грузим итог
          try {
            const r = await fetchPublishResult(jobId);
            if (!cancelled) setResult(r);
          } catch {
            // Результат может не быть ещё готов — игнорируем
          }
          return;
        }

        // Продолжаем поллинг
        setTimeout(poll, 2000);
      } catch (err) {
        if (!cancelled) setPollError(err.message || "Не удалось получить статус");
      }
    }

    poll();
    return () => { cancelled = true; };
  }, [jobId]);

  const currentStep = status?.step ?? null;
  const isTerminal = TERMINAL_STATUSES.has(status?.status);

  // Определяем индекс текущего шага
  const currentStepIndex = PUBLISH_STEPS.findIndex((s) => s.key === currentStep);

  // Пакетный режим: поля появляются в статусе только если бэкенд их прислал
  const draftsTotal = status?.drafts_total ?? null;
  const draftIndex = status?.draft_index ?? null;
  const draftsSaved = status?.drafts_saved ?? 0;
  const isBatch = draftsTotal != null && draftsTotal > 1;

  const panelTitle = status?.status === "done" && isBatch
    ? `Сохранено черновиков: ${draftsSaved} из ${draftsTotal}`
    : PANEL_TITLES[status?.status] ?? "Сохраняем черновик";

  // Каталог дампов диагностики: бэкенд шлёт debug_dir, фолбэк — путь по умолчанию
  const debugDir = status?.debug_dir || `debug/publish/${jobId}`;

  return (
    <section className="workspace-panel draft-progress-panel">
      {/* Шапка */}
      <div className="workspace-panel-header">
        <div>
          <p className="section-kicker">Публикация</p>
          <h2 className="workspace-panel-title">{panelTitle}</h2>
          {/* Счётчик текущего черновика в пакетном режиме (только во время работы) */}
          {isBatch && !isTerminal && draftIndex != null ? (
            <div className="draft-batch-counter">
              <span className="draft-batch-counter-main">
                Черновик {draftIndex} из {draftsTotal}
              </span>
              {draftsSaved > 0 ? (
                <span className="draft-batch-saved-badge">{draftsSaved} сохранено</span>
              ) : null}
            </div>
          ) : null}
        </div>
        <button type="button" className="secondary-button" onClick={onBack}>
          Новый черновик
        </button>
      </div>

      {/* Сводка объявления */}
      {summary ? (
        <div className="draft-summary-strip">
          <span className="draft-summary-item">
            <span className="draft-summary-label">Название</span>
            <span className="draft-summary-value">{summary.title}</span>
          </span>
          <span className="draft-summary-item">
            <span className="draft-summary-label">Цена</span>
            <span className="draft-summary-value">{summary.price.toLocaleString("ru-RU")} ₽</span>
          </span>
          <span className="draft-summary-item">
            <span className="draft-summary-label">Город</span>
            <span className="draft-summary-value">{summary.city}</span>
          </span>
          <span className="draft-summary-item">
            <span className="draft-summary-label">Фото</span>
            <span className="draft-summary-value">{summary.photosCount}</span>
          </span>
          {summary.draftsCount != null && summary.draftsCount > 1 ? (
            <span className="draft-summary-item">
              <span className="draft-summary-label">Черновиков</span>
              <span className="draft-summary-value">{summary.draftsCount}</span>
            </span>
          ) : null}
        </div>
      ) : null}

      {/* Список шагов */}
      <ol className="draft-steps-list">
        {PUBLISH_STEPS.map((step, index) => {
          let state = "pending";
          if (status?.status === "done") {
            // Задача завершена успешно — все шаги пройдены
            state = "done";
          } else if (index < currentStepIndex) {
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

      {/* Прогресс-бар */}
      {!isTerminal ? (
        <ProgressBar
          style={{ marginTop: "1.5rem" }}
          percent={status?.total > 0
            ? Math.min(100, Math.round(((status.done ?? 0) / status.total) * 100))
            : 0}
        />
      ) : null}

      {/* Ошибка поллинга */}
      {pollError ? (
        <div className="draft-general-error" style={{ marginTop: "1rem" }}>
          <p className="workspace-body-copy">{pollError}</p>
        </div>
      ) : null}

      {/* Терминальные состояния */}
      {status?.status === "done" ? (
        <div className="draft-terminal draft-terminal-done">
          <p className="draft-terminal-title">
            {isBatch
              ? `Все черновики сохранены: ${draftsSaved} из ${draftsTotal}`
              : "Черновик сохранён успешно"}
          </p>
          <p className="draft-terminal-copy">
            Откройте Авито → <strong>Мои объявления → Черновики</strong> — там{" "}
            {isBatch ? "появились новые черновики" : "появился новый черновик"}.
            Проверьте {isBatch ? "их" : "его"} перед публикацией.
          </p>
          {/* Список ссылок на сохранённые черновики */}
          {result?.saved_urls?.length > 0 ? (
            <ul className="draft-saved-urls-list">
              {result.saved_urls.map((url, i) => (
                <li key={url} className="draft-saved-urls-item">
                  <a
                    href={url}
                    target="_blank"
                    rel="noreferrer"
                    className="draft-saved-url-link"
                  >
                    Черновик {i + 1}
                  </a>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {status?.status === "needs_user_action" ? (
        <div className="draft-terminal draft-terminal-action">
          <p className="draft-terminal-title">Требуется ваше участие</p>
          {/* Частичный успех при пакетном режиме */}
          {isBatch && draftsSaved > 0 ? (
            <p className="draft-terminal-copy draft-terminal-partial">
              Сохранено {draftsSaved} из {draftsTotal} — остальные требуют действия.
            </p>
          ) : null}
          <p className="draft-terminal-copy">
            {status?.error || "Сервис не смог продолжить автоматически."}
          </p>
        </div>
      ) : null}

      {status?.status === "failed" ? (
        <div className="draft-terminal draft-terminal-error">
          <p className="draft-terminal-title">Не удалось сохранить черновик</p>
          {/* Частичный успех при пакетном режиме */}
          {isBatch && draftsSaved > 0 ? (
            <p className="draft-terminal-copy draft-terminal-partial">
              Сохранено {draftsSaved} из {draftsTotal} до возникновения ошибки.
            </p>
          ) : null}
          <p className="draft-terminal-copy">
            {result?.error || status?.error || "Произошла ошибка на одном из шагов."}
          </p>
          <p className="draft-terminal-copy" style={{ marginTop: "0.75rem" }}>
            Дамп сохранён в <code>{debugDir}/</code> — проверьте скриншот для диагностики.
          </p>
        </div>
      ) : null}
    </section>
  );
}

// ─── Страница черновика (корень) ──────────────────────────────────────────────

export default function DraftPage() {
  const [jobId, setJobId] = useState(null);
  const [summary, setSummary] = useState(null);

  function handleStarted(id, draftSummary) {
    setJobId(id);
    setSummary(draftSummary);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function handleBack() {
    setJobId(null);
    setSummary(null);
  }

  return (
    <main className="workspace-page">
      <div className="page-noise" />

      <div className="page-container workspace-page-shell">
        {/* Topbar */}
        <header className="workspace-page-header">
          <Topbar
            ariaLabel="Навигация черновика"
            links={[
              { to: "/workspace", label: "Аналитика" },
              { to: "/", label: "К кейсу" },
            ]}
          />

          <div className="workspace-header-shell">
            <div className="workspace-header-copy">
              <p className="section-kicker">Draft</p>
              <h1 className="workspace-title">Черновик объявления</h1>
              {!jobId ? (
                <p className="workspace-intro-copy">
                  Заполните форму — сервис откроет форму Авито в вашем Chrome и сохранит черновик
                  кнопкой «Сохранить и выйти». Для работы нужен запущенный{" "}
                  <code style={{ fontFamily: "var(--font-mono)", fontSize: "0.85em" }}>
                    start-chrome.bat
                  </code>{" "}
                  и активная сессия на avito.ru.
                </p>
              ) : (
                <p className="workspace-intro-copy">
                  Задача запущена — следите за прогрессом ниже.
                </p>
              )}
            </div>
          </div>
        </header>

        {/* Основной контент */}
        <section className="workspace-main-stack" aria-label="Форма черновика">
          {!jobId ? (
            <DraftForm onStarted={handleStarted} />
          ) : (
            <PublishProgressPanel
              jobId={jobId}
              summary={summary}
              onBack={handleBack}
            />
          )}
        </section>

        {/* Footer */}
        <SiteFooter
          links={[
            { to: "/workspace", label: "Аналитика" },
            { to: "/", label: "К кейсу" },
          ]}
        />
      </div>
    </main>
  );
}
