import { useEffect, useRef, useState } from "react";
import {
  fetchPublishResult,
  fetchPublishStatus,
  getPrepareResult,
  getPrepareStatus,
  regenerateDraft,
  startPrepare,
  startPublish,
} from "../lib/api";
import {
  buildLaunchFormData,
  buildPublishFormData,
  replaceDraftCard,
  TERMINAL_STATUSES,
} from "../lib/publish";
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

function DraftForm({ onStarted, onPrepared }) {
  const [form, setForm] = useState(buildInitialForm());
  const [photos, setPhotos] = useState([]);
  const [errors, setErrors] = useState({});
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isPreparing, setIsPreparing] = useState(false);
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

  // Кнопка «Подготовить варианты» (только при drafts_count >= 2)
  async function handlePrepare(e) {
    e.preventDefault();
    setGeneralError("");

    const validationErrors = validate(form, photos);
    if (Object.keys(validationErrors).length > 0) {
      setErrors(validationErrors);
      const firstKey = Object.keys(validationErrors)[0];
      document.querySelector(`[data-field="${firstKey}"]`)?.scrollIntoView({
        behavior: "smooth",
        block: "center",
      });
      return;
    }

    setIsPreparing(true);

    try {
      const fd = buildPublishFormData(form, photos);
      const result = await startPrepare(fd);
      onPrepared(result.prep_id, form, photos, {
        title: form.title.trim(),
        price: Number(form.price),
        city: form.city.trim(),
        photosCount: photos.length,
        draftsCount: Number(form.drafts_count),
      });
    } catch (err) {
      setGeneralError(err.message || "Не удалось запустить подготовку вариантов");
    } finally {
      setIsPreparing(false);
    }
  }

  const draftsNum = Number(form.drafts_count);
  const showPrepareButton = Number.isInteger(draftsNum) && draftsNum >= 2;
  const anyBusy = isSubmitting || isPreparing;

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
            {showPrepareButton
              ? "При N ≥ 2 доступна кнопка «Подготовить варианты» — сервис сгенерирует разные тексты и обработает фото. Снижает риск блокировки за дублирующийся контент, не гарантирует защиту."
              : "Будут созданы одинаковые черновики — каждый можно отредактировать на Авито перед публикацией."}
          </p>
        </FieldShell>
      </div>

      {/* Кнопки запуска */}
      <div className="draft-submit-row draft-submit-buttons">
        {showPrepareButton ? (
          <button
            className="secondary-button draft-prepare-btn"
            type="button"
            disabled={anyBusy}
            onClick={handlePrepare}
          >
            {isPreparing ? "Готовим варианты..." : "Подготовить варианты"}
          </button>
        ) : null}
        <button className="submit-button" type="submit" disabled={anyBusy}>
          {isSubmitting ? "Запускаем..." : "Сохранить черновик на Авито"}
        </button>
      </div>
    </form>
  );
}

// ─── Экран подготовки вариантов (поллинг prepare) ────────────────────────────

function PrepareProgressPanel({ prepId, onDone, onBack }) {
  const [status, setStatus] = useState(null);
  const [pollError, setPollError] = useState("");

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const s = await getPrepareStatus(prepId);
        if (cancelled) return;
        setStatus(s);

        if (s.status === "failed") {
          // Ошибка — показываем, дальше не идём
          return;
        }

        if (s.status === "done") {
          // Загружаем результат и переходим в превью
          const result = await getPrepareResult(prepId);
          if (!cancelled) onDone(result.drafts);
          return;
        }

        // Ещё работает — продолжаем поллинг
        setTimeout(poll, 2000);
      } catch (err) {
        if (!cancelled) setPollError(err.message || "Не удалось получить статус подготовки");
      }
    }

    poll();
    return () => { cancelled = true; };
  }, [prepId, onDone]);

  const percent = status?.total > 0
    ? Math.min(100, Math.round(((status.done ?? 0) / status.total) * 100))
    : 0;

  return (
    <section className="workspace-panel draft-progress-panel">
      <div className="workspace-panel-header">
        <div>
          <p className="section-kicker">Подготовка</p>
          <h2 className="workspace-panel-title">
            {status?.status === "failed" ? "Ошибка подготовки" : "Готовим варианты"}
          </h2>
        </div>
        <button type="button" className="secondary-button" onClick={onBack}>
          Назад к форме
        </button>
      </div>

      {status?.step_label ? (
        <p className="workspace-body-copy" style={{ marginTop: "1rem" }}>
          {status.step_label}
        </p>
      ) : null}

      {status?.status !== "failed" ? (
        <ProgressBar style={{ marginTop: "1.5rem" }} percent={percent} />
      ) : null}

      {pollError ? (
        <div className="draft-general-error" style={{ marginTop: "1rem" }}>
          <p className="workspace-body-copy">{pollError}</p>
        </div>
      ) : null}

      {status?.status === "failed" ? (
        <div className="draft-terminal draft-terminal-error" style={{ marginTop: "1rem" }}>
          <p className="draft-terminal-title">Не удалось подготовить варианты</p>
          <p className="draft-terminal-copy">
            {status.error || "Произошла ошибка при генерации вариантов."}
          </p>
        </div>
      ) : null}
    </section>
  );
}

// ─── Карточка одного черновика в превью ───────────────────────────────────────

function DraftPreviewCard({ draft, prepId, isOriginal, onRegenerated }) {
  const [isRegenerating, setIsRegenerating] = useState(false);
  const [regenError, setRegenError] = useState("");

  async function handleRegenerate() {
    setIsRegenerating(true);
    setRegenError("");
    try {
      // draft.index — 1-based (контракт бэкенда), карточка в ответе с тем же index
      const updated = await regenerateDraft(prepId, draft.index);
      onRegenerated(updated);
    } catch (err) {
      setRegenError(err.message || "Не удалось перегенерировать вариант");
    } finally {
      setIsRegenerating(false);
    }
  }

  return (
    <div className={`draft-preview-card${isOriginal ? " draft-preview-card-original" : ""}`}>
      {/* Заголовок карточки */}
      <div className="draft-preview-card-header">
        <div className="draft-preview-card-meta">
          <span className="draft-preview-index">
            {isOriginal ? "Вариант 1 — оригинал" : `Вариант ${draft.index}`}
          </span>
          {draft.preset_name ? (
            <span className="draft-preview-preset">{draft.preset_name}</span>
          ) : null}
        </div>

        {/* Перегенерация только для вариантов 2+ */}
        {!isOriginal ? (
          <button
            type="button"
            className="secondary-button draft-regen-btn"
            disabled={isRegenerating}
            onClick={handleRegenerate}
          >
            {isRegenerating ? "Обновляем..." : "Перегенерировать"}
          </button>
        ) : null}
      </div>

      {regenError ? (
        <div className="draft-general-error" style={{ marginTop: "0.75rem" }}>
          <p className="workspace-body-copy" style={{ fontSize: "0.85rem" }}>{regenError}</p>
        </div>
      ) : null}

      {/* Название */}
      <p className="draft-preview-title">{draft.title}</p>

      {/* Описание */}
      <p className="draft-preview-description">{draft.description}</p>

      {/* Миниатюры фото — бэкенд отдаёт готовые URL в photo_urls */}
      {draft.photo_urls?.length > 0 ? (
        <div className="draft-preview-photos">
          {draft.photo_urls.map((url, pi) => (
            <img
              key={url}
              src={url}
              alt={`Фото ${pi + 1}`}
              className="draft-preview-thumb"
            />
          ))}
        </div>
      ) : null}

      {/* Заметки */}
      {draft.notes ? (
        <p className="draft-preview-notes">{draft.notes}</p>
      ) : null}
    </div>
  );
}

// ─── Экран превью вариантов ───────────────────────────────────────────────────

function PreviewPanel({ prepId, initialDrafts, form, photos, summary, onStartPublish, onBack }) {
  const [drafts, setDrafts] = useState(initialDrafts);
  const [isLaunching, setIsLaunching] = useState(false);
  const [launchError, setLaunchError] = useState("");

  // Карточка из regenerate приходит с тем же 1-based index — замена по нему
  function handleRegenerated(updatedCard) {
    setDrafts((prev) => replaceDraftCard(prev, updatedCard));
  }

  async function handleLaunch() {
    setIsLaunching(true);
    setLaunchError("");
    try {
      // Полная форма + фото + prep_id — бэкенд валидирует форму целиком
      // даже при наличии подготовленных вариантов
      const fd = buildLaunchFormData(form, photos, prepId);
      const result = await startPublish(fd);
      onStartPublish(result.job_id, summary);
    } catch (err) {
      // 422 от startPublish несёт fieldErrors — показываем их текстом,
      // полей формы на этом экране нет
      const fieldMessages = err.fieldErrors
        ? Object.values(err.fieldErrors).filter(Boolean).join("; ")
        : "";
      setLaunchError(fieldMessages || err.message || "Не удалось запустить публикацию");
      setIsLaunching(false);
    }
  }

  return (
    <section className="workspace-panel draft-progress-panel">
      <div className="workspace-panel-header">
        <div>
          <p className="section-kicker">Превью</p>
          <h2 className="workspace-panel-title">Варианты черновиков</h2>
          <p className="workspace-intro-copy" style={{ marginTop: "0.6rem" }}>
            Проверьте варианты. Использование разных текстов и обработанных фото снижает риск
            блокировки за дублирующийся контент — но не гарантирует защиту от антиспам-систем.
          </p>
        </div>
        <button type="button" className="secondary-button" onClick={onBack}>
          Назад к форме
        </button>
      </div>

      {/* Карточки вариантов */}
      <div className="draft-preview-grid">
        {drafts.map((draft, i) => (
          <DraftPreviewCard
            key={draft.index}
            draft={draft}
            prepId={prepId}
            isOriginal={i === 0}
            onRegenerated={handleRegenerated}
          />
        ))}
      </div>

      {launchError ? (
        <div className="draft-general-error" style={{ marginTop: "1rem" }}>
          <p className="workspace-body-copy">{launchError}</p>
        </div>
      ) : null}

      {/* Запуск */}
      <div className="draft-submit-row" style={{ marginTop: "1.5rem" }}>
        <button
          className="submit-button"
          type="button"
          disabled={isLaunching}
          onClick={handleLaunch}
        >
          {isLaunching ? "Запускаем..." : `Запустить ${drafts.length} черновик${drafts.length === 1 ? "" : drafts.length < 5 ? "а" : "ов"}`}
        </button>
      </div>
    </section>
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
            <span className="draft-summary-value">{summary.price?.toLocaleString("ru-RU")} ₽</span>
          </span>
          <span className="draft-summary-item">
            <span className="draft-summary-label">Город</span>
            <span className="draft-summary-value">{summary.city}</span>
          </span>
          {summary.photosCount != null ? (
            <span className="draft-summary-item">
              <span className="draft-summary-label">Фото</span>
              <span className="draft-summary-value">{summary.photosCount}</span>
            </span>
          ) : null}
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

// Фазы: "form" | "preparing" | "preview" | "publishing"
export default function DraftPage() {
  const [phase, setPhase] = useState("form");

  // Данные формы для случая «Назад к форме» не восстанавливаем (YAGNI) —
  // пользователь просто видит чистую форму снова.

  // Для фазы preparing/preview: prepForm/prepPhotos нужны при запуске
  // публикации из превью (бэкенд валидирует полную форму даже с prep_id),
  // prepSummary — для экрана прогресса (название/город/цена)
  const [prepId, setPrepId] = useState(null);
  const [previewDrafts, setPreviewDrafts] = useState(null);
  const [prepForm, setPrepForm] = useState(null);
  const [prepPhotos, setPrepPhotos] = useState(null);
  const [prepSummary, setPrepSummary] = useState(null);

  // Для фазы publishing
  const [jobId, setJobId] = useState(null);
  const [summary, setSummary] = useState(null);

  // Форма → прямой запуск (N=1 или явный «Запустить»)
  function handleStarted(id, draftSummary) {
    setJobId(id);
    setSummary(draftSummary);
    setPhase("publishing");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // Форма → подготовка вариантов (N >= 2); сохраняем форму, фото и сводку
  // для последующего запуска публикации из превью
  function handlePrepared(pid, form, photos, draftSummary) {
    setPrepId(pid);
    setPrepForm(form);
    setPrepPhotos(photos);
    setPrepSummary(draftSummary);
    setPhase("preparing");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // Подготовка завершена → показываем превью
  function handlePrepareDone(drafts) {
    setPreviewDrafts(drafts);
    setPhase("preview");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // Превью → публикация
  function handlePreviewStartPublish(id, draftSummary) {
    setJobId(id);
    setSummary(draftSummary);
    setPhase("publishing");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // Кнопка «Назад» — всегда возвращает на форму
  function handleBack() {
    setJobId(null);
    setSummary(null);
    setPrepId(null);
    setPreviewDrafts(null);
    setPrepForm(null);
    setPrepPhotos(null);
    setPrepSummary(null);
    setPhase("form");
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
              {phase === "form" ? (
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
                  {phase === "publishing"
                    ? "Задача запущена — следите за прогрессом ниже."
                    : phase === "preview"
                    ? "Проверьте варианты и запустите публикацию."
                    : "Готовим варианты — подождите..."}
                </p>
              )}
            </div>
          </div>
        </header>

        {/* Основной контент */}
        <section className="workspace-main-stack" aria-label="Форма черновика">
          {phase === "form" ? (
            <DraftForm onStarted={handleStarted} onPrepared={handlePrepared} />
          ) : phase === "preparing" ? (
            <PrepareProgressPanel
              prepId={prepId}
              onDone={handlePrepareDone}
              onBack={handleBack}
            />
          ) : phase === "preview" ? (
            <PreviewPanel
              prepId={prepId}
              initialDrafts={previewDrafts}
              form={prepForm}
              photos={prepPhotos}
              summary={prepSummary}
              onStartPublish={handlePreviewStartPublish}
              onBack={handleBack}
            />
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
