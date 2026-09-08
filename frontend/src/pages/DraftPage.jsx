import { useEffect, useRef, useState } from "react";
import {
  fetchPublishCategories,
  generateAddresses,
  fetchPublishResult,
  fetchPublishStatus,
  getPrepareResult,
  getPrepareStatus,
  regenerateDraft,
  resumePublish,
  startPrepare,
  startPublish,
  updateDraftText,
} from "../lib/api";
import {
  buildLaunchFormData,
  buildPublishFormData,
  canResumePublish,
  describeResumePlan,
  MAX_DRAFTS,
  normalizePublishProgress,
  normalizeViewPrice,
  publishButtonLabel,
  replaceDraftCard,
  resizeLocations,
  resumeUnavailableMessage,
  TERMINAL_STATUSES,
  validateLocations,
} from "../lib/publish";
import {
  createDraftStorage,
  createPersistenceController,
  createPhotoStorage,
} from "../lib/draftPersistence";
import FieldShell from "../components/FieldShell";
import ProgressBar from "../components/ProgressBar";
import SiteFooter from "../components/SiteFooter";
import Topbar from "../components/Topbar";

// ─── Справочники-фолбэк (значения — ровно те строки, что шлём на бэкенд) ──────
// Списки полей теперь приходят с бэкенда (GET /api/publish/categories) и зависят
// от выбранной категории. Эти константы — фолбэк под категорию «Пиджаки и
// костюмы» (jackets): форма рисуется всегда, даже если запрос категорий не
// прошёл. Зеркало словарей backend (TRADE_TYPE/CONDITION/SIZE/COLOR_OPTIONS) —
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

// Категория по умолчанию (совпадает с DEFAULT_CATEGORY на бэкенде)
const DEFAULT_CATEGORY = "jackets";

// Профиль-фолбэк под jackets: используется до загрузки категорий с бэкенда
// и при ошибке запроса — форма должна рисоваться всегда.
const FALLBACK_PROFILE = {
  key: DEFAULT_CATEGORY,
  label: "Пиджаки и костюмы",
  trade_types: AD_TYPES,
  conditions: CONDITIONS,
  sizes: SIZES,
  colors: COLORS,
};

// Возвращает профиль текущей категории из загруженного списка (или фолбэк)
function resolveProfile(categories, categoryKey) {
  if (Array.isArray(categories)) {
    const found = categories.find((c) => c.key === categoryKey);
    if (found) return found;
  }
  return FALLBACK_PROFILE;
}

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
  { key: "fill_item_price",  label: "Цена товара" },
  { key: "fill_address",     label: "Геолокация объявления" },
  { key: "continue_listing", label: "Переход к публикации" },
  { key: "fill_view_price",  label: "Стоимость просмотра" },
  { key: "continue_view_price", label: "Подтверждение стоимости просмотра" },
  { key: "skip_services",    label: "Отказ от дополнительных услуг" },
  { key: "open_next_form",   label: "Переход к следующему объявлению" },
];

// Заголовок панели прогресса по статусу задачи
const PANEL_TITLES = {
  done: "Объявление отправлено",
  failed: "Произошла ошибка",
  needs_user_action: "Требуется действие",
  interrupted: "Публикация прервана",
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

// ─── Форма публикации ─────────────────────────────────────────────────────────

// Имена полей — серверные (совпадают с параметрами /api/publish/start)
function buildInitialForm() {
  return {
    category: DEFAULT_CATEGORY,
    title: "",
    trade_type: "Продаю своё",
    condition: "Отличное",
    size: "",
    brand: "",
    color: "",
    description: "",
    price: "",
    view_price_max: "",
    drafts_count: 1,
    locations: [{ city: "", address: "" }],
    // Вид товара (футболка/поло/худи/…) — есть не у всех категорий,
    // поле рисуется только если бэкенд прислал непустой item_types
    item_type: "",
    // Материал основной части / стиль — есть не у всех категорий (напр.
    // «Жилеты»); persistence подхватит их автоматически — идёт по ключам defaults
    material: "",
    style: "",
  };
}

function validate(form, photos, profile) {
  const errors = {};

  if (!form.title.trim()) errors.title = "Укажите название";
  if (!form.trade_type) errors.trade_type = "Выберите вид объявления";
  if (!form.condition) errors.condition = "Выберите состояние";
  if (!form.size) {
    errors.size = "Выберите размер";
  } else if (
    // Размеры у категорий разные — если список загружен, значение должно в него входить
    Array.isArray(profile?.sizes) && !profile.sizes.includes(form.size)
  ) {
    errors.size = "Выберите размер из списка категории";
  }
  // Вид товара — обязателен только у категорий, где поле есть (зеркало
  // publisher.validate_publish_form: profile.has_item_type)
  const itemTypes = profile?.item_types;
  if (Array.isArray(itemTypes) && itemTypes.length > 0) {
    if (!form.item_type) {
      errors.item_type = "Выберите вид товара";
    } else if (!itemTypes.includes(form.item_type)) {
      errors.item_type = "Выберите вид товара из списка категории";
    }
  }
  const materials = profile?.materials;
  if (Array.isArray(materials) && materials.length > 0) {
    if (!form.material) {
      errors.material = "Выберите материал";
    } else if (!materials.includes(form.material)) {
      errors.material = "Выберите материал из списка категории";
    }
  }
  const styles = profile?.styles;
  if (Array.isArray(styles) && styles.length > 0) {
    if (!form.style) {
      errors.style = "Выберите стиль";
    } else if (!styles.includes(form.style)) {
      errors.style = "Выберите стиль из списка категории";
    }
  }
  if (!form.color) errors.color = "Выберите цвет";
  if (!form.description.trim()) errors.description = "Добавьте описание";

  const priceNum = Number(form.price);
  if (!form.price || !Number.isInteger(priceNum) || priceNum <= 0) {
    errors.price = "Укажите цену — целое число больше 0";
  }

  const draftsNum = Number(form.drafts_count);
  if (!Number.isInteger(draftsNum) || draftsNum < 1 || draftsNum > MAX_DRAFTS) {
    errors.drafts_count = `Укажите число от 1 до ${MAX_DRAFTS}`;
  } else {
    Object.assign(errors, validateLocations(form.locations, draftsNum));
  }

  const viewPriceMax = normalizeViewPrice(form.view_price_max);
  if (!viewPriceMax) {
    errors.view_price_max = "Укажите потолок стоимости просмотра — число больше 0";
  }

  if (photos.length === 0) errors.photos = "Добавьте хотя бы одно фото";
  if (photos.length > MAX_PHOTOS) errors.photos = `Максимум ${MAX_PHOTOS} фотографий`;

  for (const file of photos) {
    if (file.size > MAX_FILE_SIZE_MB * 1024 * 1024) {
      errors.photos = `Файл «${file.name}» превышает ${MAX_FILE_SIZE_MB} МБ`;
      break;
    }
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
  const [isHydrated, setIsHydrated] = useState(false);
  const [isClearingSaved, setIsClearingSaved] = useState(false);
  const [saveStatus, setSaveStatus] = useState("Восстанавливаем шаблон…");
  const [formPersistenceError, setFormPersistenceError] = useState("");
  const [photosPersistenceError, setPhotosPersistenceError] = useState("");
  // Профили категорий с бэкенда; null до загрузки/при ошибке — тогда фолбэк
  const [categories, setCategories] = useState(null);
  const [addressGeneration, setAddressGeneration] = useState({ loading: [], error: {}, general: "" });
  const fileInputRef = useRef(null);
  const mountedRef = useRef(true);
  const persistenceControllerRef = useRef(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // Восстанавливаем значения только один раз. До этого autosave выключен,
  // чтобы дефолты не затёрли реальный черновик до завершения гидратации.
  useEffect(() => {
    let cancelled = false;

    async function hydrateDraft() {
      const defaults = buildInitialForm();
      let restoredForm = defaults;
      let restoredPhotos = [];
      let formRestoreError = "";
      let photosRestoreError = "";
      let draftStorage;
      let photoStorage;

      try {
        draftStorage = createDraftStorage(window.localStorage);
        restoredForm = draftStorage.load(defaults);
      } catch (error) {
        formRestoreError = "Не удалось восстановить поля шаблона из локального хранилища.";
        draftStorage = {
          save() { throw error; },
          clear() { throw error; },
        };
      }

      try {
        photoStorage = createPhotoStorage(window.indexedDB);
        restoredPhotos = await photoStorage.load();
      } catch (error) {
        photosRestoreError = "Не удалось восстановить фотографии шаблона.";
        if (!photoStorage) {
          photoStorage = {
            save: async () => { throw error; },
            clear: async () => { throw error; },
          };
        }
      }

      if (cancelled) return;
      persistenceControllerRef.current = createPersistenceController({
        draftStorage,
        photoStorage,
      });
      setForm(restoredForm);
      setPhotos(restoredPhotos);
      setFormPersistenceError(formRestoreError);
      setPhotosPersistenceError(photosRestoreError);
      setSaveStatus(formRestoreError || photosRestoreError ? "Хранилище недоступно" : "Шаблон восстановлен");
      setIsHydrated(true);
    }

    hydrateDraft();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!isHydrated) return;

    let cancelled = false;
    setSaveStatus("Сохраняем…");
    persistenceControllerRef.current.saveForm(form)
      .then((saved) => {
        if (!saved || cancelled || !mountedRef.current) return;
        setFormPersistenceError("");
        setSaveStatus("Сохранено");
      })
      .catch(() => {
        if (cancelled || !mountedRef.current) return;
        setFormPersistenceError("Не удалось сохранить поля шаблона. Проверьте свободное место в браузере.");
        setSaveStatus("Ошибка сохранения");
      });

    return () => { cancelled = true; };
  }, [form, isHydrated]);

  useEffect(() => {
    if (!isHydrated) return undefined;

    let cancelled = false;
    setSaveStatus("Сохраняем…");
    persistenceControllerRef.current.savePhotos(photos)
      .then((saved) => {
        if (!saved || cancelled || !mountedRef.current) return;
        setPhotosPersistenceError("");
        setSaveStatus("Сохранено");
      })
      .catch(() => {
        if (cancelled || !mountedRef.current) return;
        setPhotosPersistenceError("Не удалось сохранить фотографии. Проверьте свободное место в браузере.");
        setSaveStatus("Ошибка сохранения");
      });

    return () => { cancelled = true; };
  }, [photos, isHydrated]);

  const persistenceError = formPersistenceError || photosPersistenceError;

  // Подтягиваем списки полей по категориям. При ошибке остаёмся на фолбэке
  // (FALLBACK_PROFILE) — форма должна рисоваться всегда.
  useEffect(() => {
    let cancelled = false;
    fetchPublishCategories()
      .then((data) => {
        if (!cancelled && Array.isArray(data?.categories)) {
          setCategories(data.categories);
        }
      })
      .catch(() => {
        // Молча остаёмся на фолбэке — категория одна, форма работает
      });
    return () => { cancelled = true; };
  }, []);

  // Текущий профиль = выбранная категория из загруженного списка (или фолбэк)
  const profile = resolveProfile(categories, form.category);
  const adTypes = profile.trade_types ?? AD_TYPES;
  const conditions = profile.conditions ?? CONDITIONS;
  const sizes = profile.sizes ?? SIZES;
  const colors = profile.colors ?? COLORS;
  // Непустой список = у категории есть «Вид товара» (фолбэк-профиль его не имеет)
  const itemTypes = Array.isArray(profile.item_types) ? profile.item_types : [];
  const materials = Array.isArray(profile.materials) ? profile.materials : [];
  const styles = Array.isArray(profile.styles) ? profile.styles : [];

  // Сбрасывает ошибку поля после того, как пользователь его поправил
  function clearError(field) {
    if (errors[field]) setErrors((prev) => ({ ...prev, [field]: "" }));
  }

  function updateField(e) {
    const { name, value } = e.target;
    setForm((prev) => ({ ...prev, [name]: value }));
    clearError(name);
  }

  function handleDraftsCountChange(e) {
    const value = e.target.value;
    const count = Number(value);
    setForm((prev) => ({
      ...prev,
      drafts_count: value,
      locations: resizeLocations(prev.locations, count),
    }));
    setErrors((prev) => Object.fromEntries(
      Object.entries(prev).filter(([key]) => (
        key !== "drafts_count"
        && key !== "locations"
        && !key.startsWith("locations.")
      )),
    ));
  }

  function updateLocation(index, field, value) {
    setForm((prev) => ({
      ...prev,
      locations: prev.locations.map((location, locationIndex) => (
        locationIndex === index
          ? { ...location, [field]: value }
          : location
      )),
    }));
    clearError(`locations.${index + 1}.${field}`);
    clearError("locations");
  }

  async function generateLocationAddresses(indices) {
    const targets = indices.filter((index) => form.locations[index]?.city.trim());
    const missing = indices.filter((index) => !form.locations[index]?.city.trim());
    const missingErrors = Object.fromEntries(missing.map((index) => [index, "Сначала укажите город."]));
    if (missing.length) {
      setAddressGeneration((prev) => ({
        ...prev,
        error: { ...prev.error, ...missingErrors },
      }));
    }
    if (!targets.length) return;
    const citySnapshot = new Map(targets.map((index) => [index, form.locations[index].city.trim()]));
    setErrors((prev) => {
      const next = { ...prev };
      targets.forEach((index) => { delete next[`locations.${index + 1}.address`]; });
      return next;
    });
    setAddressGeneration({ loading: targets, error: missingErrors, general: missing.length ? "Для части строк сначала укажите город." : "" });
    try {
      const data = await generateAddresses(
        targets.map((index) => form.locations[index].city),
        form.locations.filter((location) => location.address.trim()).map(({ city, address }) => ({ city, address })),
      );
      const nextErrors = { ...missingErrors };
      setForm((prev) => ({
        ...prev,
        locations: prev.locations.map((location, index) => {
          const position = targets.indexOf(index);
          if (position < 0) return location;
          const result = data.results?.[position];
          if (location.city.trim() !== citySnapshot.get(index)) {
            nextErrors[index] = "Город изменён во время подбора — адрес не применён.";
            return location;
          }
          if (result?.address) {
            return { ...location, address: result.address };
          }
          nextErrors[index] = result?.error || "Не удалось подобрать адрес. Введите вручную.";
          return location;
        }),
      }));
      setAddressGeneration({ loading: [], error: nextErrors, general: Object.keys(nextErrors).length ? "Некоторые адреса не удалось подобрать или не указан город." : "" });
    } catch (err) {
      setAddressGeneration({ loading: [], error: missingErrors, general: err.message || "Не удалось получить адреса. Введите их вручную." });
    }
  }

  // Смена категории: списки размеров у категорий разные — сбрасываем size,
  // чтобы не остался невалидный для новой категории
  function handleCategoryChange(e) {
    const value = e.target.value;
    // Размер и вид товара — категорийные словари: старое значение чужой
    // категории бэкенд отвергнет, поэтому сбрасываем оба
    setForm((prev) => ({ ...prev, category: value, size: "", item_type: "", material: "", style: "" }));
    clearError("category");
    clearError("size");
    clearError("item_type");
    clearError("material");
    clearError("style");
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

  async function clearSavedDraft() {
    setIsClearingSaved(true);
    setFormPersistenceError("");
    setPhotosPersistenceError("");
    setSaveStatus("Очищаем сохранённое…");

    try {
      // Контроллер закрывает очередь для новых autosave, ждёт уже поставленные
      // записи и только после них очищает оба хранилища.
      await persistenceControllerRef.current.clear();
      if (!mountedRef.current) return;
      setForm(buildInitialForm());
      setPhotos([]);
      setErrors({});
      setGeneralError("");
      setFormPersistenceError("");
      setPhotosPersistenceError("");
      setSaveStatus("Сохранённое удалено");
    } catch {
      if (!mountedRef.current) return;
      setFormPersistenceError("Не удалось полностью очистить сохранённый шаблон. Попробуйте ещё раз.");
      setSaveStatus("Ошибка очистки");
    } finally {
      if (mountedRef.current) setIsClearingSaved(false);
    }
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setGeneralError("");

    const validationErrors = validate(form, photos, profile);
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
        locationsCount: form.locations.length,
        viewPriceMax: normalizeViewPrice(form.view_price_max),
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

    const validationErrors = validate(form, photos, profile);
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
        locationsCount: form.locations.length,
        viewPriceMax: normalizeViewPrice(form.view_price_max),
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
    <form
      className="workspace-panel draft-form space-y-5"
      onSubmit={showPrepareButton ? handlePrepare : handleSubmit}
    >
      <div className="draft-persistence-bar">
        <div>
          <p id="draft-persistence-status" className="draft-persistence-status" aria-live="polite">{saveStatus}</p>
          {persistenceError ? (
            <p className="draft-persistence-error" role="alert">{persistenceError}</p>
          ) : null}
        </div>
        <button
          type="button"
          className="draft-persistence-clear"
          onClick={clearSavedDraft}
          disabled={!isHydrated || anyBusy || isClearingSaved}
        >
          Очистить сохранённое
        </button>
      </div>

      <fieldset
        className="draft-form-fields"
        disabled={!isHydrated || isClearingSaved}
        aria-describedby="draft-persistence-status"
      >

      {/* Выбор категории */}
      <div data-field="category">
        <FieldShell label="Категория" error={errors.category}>
          <select
            className="field-input draft-select"
            name="category"
            value={form.category}
            onChange={handleCategoryChange}
          >
            {(categories ?? [FALLBACK_PROFILE]).map((c) => (
              <option key={c.key} value={c.key}>{c.label}</option>
            ))}
          </select>
        </FieldShell>
      </div>

      {/* Вид товара — только у категорий, где Авито просит подтип
          (напр. «Кофты и футболки»: футболка / поло / худи / …) */}
      {itemTypes.length > 0 ? (
        <div data-field="item_type">
          <FieldShell label="Вид товара" error={errors.item_type}>
            <select
              className="field-input draft-select"
              name="item_type"
              value={form.item_type}
              onChange={updateField}
            >
              <option value="">— Выберите вид товара —</option>
              {itemTypes.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </FieldShell>
        </div>
      ) : null}

      {/* Предупреждение о категории */}
      <div className="draft-category-notice">
        <span className="section-kicker">Категория</span>
        <p className="draft-category-text">
          Работает категория{" "}
          <strong>«{profile.label}»</strong>.
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
            {adTypes.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </FieldShell>
      </div>

      {/* Состояние */}
      <div data-field="condition">
        <FieldShell label="Состояние" error={errors.condition}>
          <div className="draft-radio-group">
            {conditions.map((c) => (
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
              {sizes.map((s) => (
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
            <p className="draft-view-price-hint">
              Если такого бренда нет в подсказках Авито, сервис выберет «Без бренда».
            </p>
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
            {colors.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </FieldShell>
      </div>

      {/* Материал основной части — только у категорий, где Авито просит поле (напр. «Жилеты») */}
      {materials.length > 0 ? (
        <div data-field="material">
          <FieldShell label="Материал основной части" error={errors.material}>
            <select
              className="field-input draft-select"
              name="material"
              value={form.material}
              onChange={updateField}
            >
              <option value="">— Выберите материал —</option>
              {materials.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </FieldShell>
        </div>
      ) : null}

      {/* Стиль — только у категорий, где Авито просит поле (напр. «Жилеты») */}
      {styles.length > 0 ? (
        <div data-field="style">
          <FieldShell label="Стиль" error={errors.style}>
            <div className="draft-radio-group">
              {styles.map((s) => (
                <label
                  key={s}
                  className={`draft-pill draft-radio-chip${form.style === s ? " draft-radio-chip-active" : ""}`}
                >
                  <input
                    type="radio"
                    name="style"
                    value={s}
                    checked={form.style === s}
                    onChange={updateField}
                  />
                  <span>{s}</span>
                </label>
              ))}
            </div>
          </FieldShell>
        </div>
      ) : null}

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

      {/* Количество объявлений */}
      <div data-field="drafts_count">
        <FieldShell label="Количество объявлений" error={errors.drafts_count}>
          <select
            className="field-input draft-select"
            name="drafts_count"
            value={form.drafts_count}
            onChange={handleDraftsCountChange}
          >
            {Array.from({ length: MAX_DRAFTS }, (_, index) => index + 1).map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
          <p className="draft-batch-hint">
            {showPrepareButton
              ? "При N ≥ 2 сервис сначала готовит варианты: разные названия и описания, обработанные фото (зум/отдаление, поворот, контраст). Перед заливкой вы увидите превью. Снижает риск блокировки за дублирующийся контент, но не гарантирует защиту."
              : "Для одного объявления название, описание и фото сохраняются как есть."}
          </p>
        </FieldShell>
      </div>

      {/* Отдельная геолокация для каждого объявления */}
      <section className="draft-locations-section" data-field="locations">
        <div className="draft-locations-heading">
          <div>
            <p className="section-kicker">Геолокации</p>
            <h3 className="draft-locations-title">Геолокации объявлений</h3>
          </div>
          <span className="draft-locations-count">{form.locations.length}</span>
        </div>
        <p className="draft-locations-copy">
          Каждая строка относится к объявлению с тем же номером. Ручной ввод остаётся доступен; генератор по возможности выбирает разные реальные адреса в одном городе.
        </p>
        <div className="draft-location-actions">
          <button
            type="button"
            className="draft-address-generate"
            onClick={() => generateLocationAddresses(form.locations.map((_, index) => index))}
            disabled={addressGeneration.loading.length > 0}
          >
            {addressGeneration.loading.length > 0 ? "Подбираем адреса…" : "Заполнить все адреса"}
          </button>
          <span className="draft-osm-attribution">Адреса: <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">© OpenStreetMap contributors</a></span>
        </div>
        {addressGeneration.general ? <p className="field-error" role="alert">{addressGeneration.general}</p> : null}
        {errors.locations ? (
          <p className="field-error" role="alert">{errors.locations}</p>
        ) : null}

        <div className="draft-locations-list">
          {form.locations.map((location, index) => {
            const itemNumber = index + 1;
            const cityField = `locations.${itemNumber}.city`;
            const addressField = `locations.${itemNumber}.address`;
            return (
              <article className="draft-location-card" key={itemNumber}>
                <div className="draft-location-card-header">
                  <span className="draft-location-index">{String(itemNumber).padStart(2, "0")}</span>
                  <h4>Объявление №{itemNumber}</h4>
                </div>
                <div className="draft-location-fields">
                  <div data-field={cityField}>
                    <FieldShell label="Город" error={errors[cityField]}>
                      <input
                        className="field-input"
                        type="text"
                        name={cityField}
                        value={location.city}
                        onChange={(event) => updateLocation(index, "city", event.target.value)}
                        placeholder="Москва"
                      />
                    </FieldShell>
                  </div>
                  <div data-field={addressField}>
                    <FieldShell label="Улица и дом (дом не обязателен)" error={errors[addressField]}>
                      <div className="draft-address-input-row">
                        <input
                          className="field-input"
                          type="text"
                          name={addressField}
                          value={location.address}
                          onChange={(event) => updateLocation(index, "address", event.target.value)}
                          placeholder="ул. Пушкина, д. 10"
                        />
                        <button
                          type="button"
                          className="draft-address-generate"
                          onClick={() => generateLocationAddresses([index])}
                          disabled={addressGeneration.loading.length > 0 || !location.city.trim()}
                          aria-label={`Сгенерировать адрес для объявления №${itemNumber}`}
                        >
                          {addressGeneration.loading.includes(index) ? "Подбираем…" : "Сгенерировать"}
                        </button>
                      </div>
                      {addressGeneration.error[index] ? <p className="field-error" role="alert">{addressGeneration.error[index]}</p> : null}
                    </FieldShell>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      </section>

      {/* Единый потолок стоимости просмотра для всего пакета */}
      <div data-field="view_price_max" className="draft-view-price-field">
        <FieldShell
          label="Потолок стоимости просмотра, ₽"
          error={errors.view_price_max}
        >
          <input
            className="field-input"
            type="text"
            inputMode="decimal"
            name="view_price_max"
            value={form.view_price_max}
            onChange={updateField}
            placeholder="2,0"
            autoComplete="off"
          />
          <p className="draft-view-price-hint">
            Движок всегда выбирает минимальную цену, которую предлагает Авито.
            Если минимум окажется выше потолка — публикация остановится, деньги не спишутся.
          </p>
        </FieldShell>
      </div>

      {normalizeViewPrice(form.view_price_max) ? (
        <div className="draft-view-price-summary" aria-live="polite">
          <span>Пакет</span>
          <strong>
            {form.locations.length}{" "}
            {form.locations.length === 1
              ? "объявление"
              : form.locations.length <= 4
                ? "объявления"
                : "объявлений"}
            {" · "}не дороже {normalizeViewPrice(form.view_price_max)} ₽ за просмотр
          </strong>
        </div>
      ) : null}

      {/* При N ≥ 2 сначала готовим разные тексты и фото, затем публикуем пакет. */}
      <div className="draft-submit-row draft-submit-buttons">
        <button className="submit-button" type="submit" disabled={anyBusy}>
          {showPrepareButton
            ? (isPreparing ? "Готовим варианты..." : "Подготовить варианты")
            : (isSubmitting ? "Запускаем..." : publishButtonLabel(draftsNum))}
        </button>
      </div>
      </fieldset>
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

// ─── Карточка одного варианта в превью ────────────────────────────────────────

function DraftPreviewCard({ draft, prepId, onUpdated, onEditingChange }) {
  const [isRegenerating, setIsRegenerating] = useState(false);
  const [regenError, setRegenError] = useState("");
  const [isEditing, setIsEditing] = useState(false);
  const [isSavingText, setIsSavingText] = useState(false);
  const [editTitle, setEditTitle] = useState(draft.title);
  const [editDescription, setEditDescription] = useState(draft.description);
  const [editError, setEditError] = useState("");

  async function handleRegenerate() {
    setIsRegenerating(true);
    setRegenError("");
    try {
      // draft.index — 1-based (контракт бэкенда), карточка в ответе с тем же index
      const updated = await regenerateDraft(prepId, draft.index);
      onUpdated(updated);
    } catch (err) {
      setRegenError(err.message || "Не удалось перегенерировать вариант");
    } finally {
      setIsRegenerating(false);
    }
  }

  function handleStartEditing() {
    setEditTitle(draft.title);
    setEditDescription(draft.description);
    setEditError("");
    setIsEditing(true);
    onEditingChange(draft.index, true);
  }

  function handleCancelEditing() {
    setEditTitle(draft.title);
    setEditDescription(draft.description);
    setEditError("");
    setIsEditing(false);
    onEditingChange(draft.index, false);
  }

  async function handleSaveText() {
    const title = editTitle.trim();
    const description = editDescription.trim();
    if (!title || !description) {
      setEditError(!title ? "Название не может быть пустым" : "Описание не может быть пустым");
      return;
    }

    setIsSavingText(true);
    setEditError("");
    try {
      const updated = await updateDraftText(
        prepId,
        draft.index,
        title,
        description,
      );
      onUpdated(updated);
      setIsEditing(false);
      onEditingChange(draft.index, false);
    } catch (err) {
      setEditError(err.message || "Не удалось сохранить текст");
    } finally {
      setIsSavingText(false);
    }
  }

  return (
    <div className="draft-preview-card">
      {/* Заголовок карточки */}
      <div className="draft-preview-card-header">
        <div className="draft-preview-card-meta">
          <span className="draft-preview-index">
            {`Вариант ${draft.index}`}
          </span>
          {draft.preset_name ? (
            <span className="draft-preview-preset">{draft.preset_name}</span>
          ) : null}
        </div>

        <div className="draft-preview-actions">
          <button
            type="button"
            className="secondary-button draft-edit-btn"
            disabled={isEditing || isRegenerating || isSavingText}
            onClick={handleStartEditing}
          >
            Редактировать текст
          </button>
          <button
            type="button"
            className="secondary-button draft-regen-btn"
            disabled={isRegenerating || isEditing || isSavingText}
            onClick={handleRegenerate}
          >
            {isRegenerating ? "Обновляем..." : "Перегенерировать"}
          </button>
        </div>
      </div>

      {regenError ? (
        <div className="draft-general-error" style={{ marginTop: "0.75rem" }}>
          <p className="workspace-body-copy" style={{ fontSize: "0.85rem" }}>{regenError}</p>
        </div>
      ) : null}

      {/* Предупреждения: вариация фото не применилась — пользователь должен это
          увидеть, а не получить «клон» под видом варианта (дефект №6 аудита) */}
      {draft.warnings?.length > 0 ? (
        <div className="draft-preview-warning" role="alert">
          {draft.warnings.map((w, wi) => (
            <p key={wi}>⚠ {w}</p>
          ))}
        </div>
      ) : null}

      {isEditing ? (
        <div className="draft-preview-editor">
          <label className="draft-preview-edit-field">
            <span>Название</span>
            <input
              className="field-input"
              type="text"
              value={editTitle}
              maxLength={120}
              aria-label={`Название варианта ${draft.index}`}
              onChange={(event) => setEditTitle(event.target.value)}
            />
          </label>
          <label className="draft-preview-edit-field">
            <span>Описание</span>
            <textarea
              className="field-input draft-textarea"
              value={editDescription}
              rows={7}
              aria-label={`Описание варианта ${draft.index}`}
              onChange={(event) => setEditDescription(event.target.value)}
            />
          </label>
          {editError ? <p className="field-error" role="alert">{editError}</p> : null}
          <div className="draft-preview-editor-actions">
            <button
              type="button"
              className="secondary-button"
              disabled={isSavingText}
              onClick={handleCancelEditing}
            >
              Отмена
            </button>
            <button
              type="button"
              className="submit-button draft-save-text-btn"
              disabled={isSavingText}
              onClick={handleSaveText}
            >
              {isSavingText ? "Сохраняем..." : "Сохранить текст"}
            </button>
          </div>
        </div>
      ) : (
        <>
          {/* Название */}
          <p className="draft-preview-title">{draft.title}</p>

          {/* Описание */}
          <p className="draft-preview-description">{draft.description}</p>
        </>
      )}

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
  const [editingDraftIndexes, setEditingDraftIndexes] = useState([]);

  // Карточка из regenerate приходит с тем же 1-based index — замена по нему
  function handleUpdated(updatedCard) {
    setDrafts((prev) => replaceDraftCard(prev, updatedCard));
  }

  function handleEditingChange(draftIndex, isEditing) {
    setEditingDraftIndexes((prev) => (
      isEditing
        ? (prev.includes(draftIndex) ? prev : [...prev, draftIndex])
        : prev.filter((index) => index !== draftIndex)
    ));
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
          <h2 className="workspace-panel-title">Варианты объявлений</h2>
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
        {drafts.map((draft) => (
          <DraftPreviewCard
            key={draft.index}
            draft={draft}
            prepId={prepId}
            onUpdated={handleUpdated}
            onEditingChange={handleEditingChange}
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
          disabled={isLaunching || editingDraftIndexes.length > 0}
          onClick={handleLaunch}
        >
          {isLaunching ? "Запускаем..." : publishButtonLabel(drafts.length)}
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
  const [isResuming, setIsResuming] = useState(false);
  const [resumeNonce, setResumeNonce] = useState(0);

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
  }, [jobId, resumeNonce]);

  const currentStep = status?.step ?? null;
  const isTerminal = TERMINAL_STATUSES.has(status?.status);

  // Определяем индекс текущего шага
  const currentStepIndex = PUBLISH_STEPS.findIndex((s) => s.key === currentStep);

  const {
    itemsTotal,
    itemIndex,
    itemsPublished,
    appliedViewPrices,
    addressWarnings,
    brandSelected,
    skippedItems,
  } = normalizePublishProgress(status ?? {});
  const { publishedUrls } = normalizePublishProgress(result ?? status ?? {});
  const isBatch = itemsTotal != null && itemsTotal > 1;
  const canResume = canResumePublish(status);
  // retry_item (повтор текущего) или skip_item (уже создано на Авито, едем
  // со следующего) — режимы отличаются и текстом, и тем, что реально произойдёт
  const resumePlan = describeResumePlan(status ?? {});
  const resumeUnavailableReason = resumeUnavailableMessage(status ?? {});

  async function handleResume() {
    setIsResuming(true);
    setPollError("");
    try {
      await resumePublish(jobId);
      setResult(null);
      setStatus((current) => ({
        ...(current ?? {}),
        status: "queued",
        error: null,
        user_action: null,
      }));
      setResumeNonce((value) => value + 1);
    } catch (error) {
      setPollError(error.message || "Не удалось продолжить публикацию");
    } finally {
      setIsResuming(false);
    }
  }

  const panelTitle = status?.status === "done" && isBatch
    ? `Отправлено ${itemsPublished} из ${itemsTotal}`
    : PANEL_TITLES[status?.status] ?? "Отправляем объявления";

  // Каталог дампов диагностики: бэкенд шлёт debug_dir, фолбэк — путь по умолчанию
  const debugDir = status?.debug_dir || `debug/publish/${jobId}`;

  return (
    <section className="workspace-panel draft-progress-panel">
      {/* Шапка */}
      <div className="workspace-panel-header">
        <div>
          <p className="section-kicker">Публикация</p>
          <h2 className="workspace-panel-title">{panelTitle}</h2>
          {/* Счётчик текущего объявления в пакетном режиме */}
          {isBatch && !isTerminal && itemIndex != null ? (
            <div className="draft-batch-counter">
              <span className="draft-batch-counter-main">
                Объявление {itemIndex} из {itemsTotal}
              </span>
              {itemsPublished > 0 ? (
                <span className="draft-batch-saved-badge">Отправлено {itemsPublished}</span>
              ) : null}
            </div>
          ) : null}
        </div>
        <button type="button" className="secondary-button" onClick={onBack}>
          Новая публикация
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
            <span className="draft-summary-label">Цена товара</span>
            <span className="draft-summary-value">{summary.price?.toLocaleString("ru-RU")} ₽</span>
          </span>
          <span className="draft-summary-item">
            <span className="draft-summary-label">Геолокаций</span>
            <span className="draft-summary-value">{summary.locationsCount}</span>
          </span>
          <span className="draft-summary-item">
            <span className="draft-summary-label">Потолок просмотра</span>
            <span className="draft-summary-value">{summary.viewPriceMax} ₽</span>
          </span>
          {summary.photosCount != null ? (
            <span className="draft-summary-item">
              <span className="draft-summary-label">Фото</span>
              <span className="draft-summary-value">{summary.photosCount}</span>
            </span>
          ) : null}
          {summary.draftsCount != null && summary.draftsCount > 1 ? (
            <span className="draft-summary-item">
              <span className="draft-summary-label">Объявлений</span>
              <span className="draft-summary-value">{summary.draftsCount}</span>
            </span>
          ) : null}
        </div>
      ) : null}

      {/* Объявления, пропущенные автоматикой при skip_item-возобновлении:
          уже созданы на Авито, но не оплачены — их не трогаем, судьба на
          пользователе */}
      {skippedItems.length > 0 ? (
        <p className="workspace-body-copy" style={{ marginTop: "0.5rem" }}>
          Пропущены и требуют ручной проверки: {skippedItems.map((n) => `№${n}`).join(", ")}
        </p>
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
              ? `Отправлено ${itemsPublished} из ${itemsTotal}`
              : "Объявление отправлено"}
          </p>
          {brandSelected ? (
            <p className="draft-terminal-copy">
              Бренд в Авито: <strong>{brandSelected}</strong>.
            </p>
          ) : null}
          {appliedViewPrices.length > 0 ? (
            <p className="draft-terminal-copy">
              Фактическая стоимость: {appliedViewPrices.map((item) => (
                `№${item.item_index} — ${item.price} ₽`
              )).join("; ")}.
            </p>
          ) : null}
          {addressWarnings.length > 0 ? (
            <div className="draft-terminal-copy draft-terminal-partial">
              <strong>Авито применил только город:</strong>
              <ul>
                {addressWarnings.map((warning) => (
                  <li key={`${warning.item_index}-${warning.requested}`}>
                    №{warning.item_index}: {warning.requested} → {warning.applied}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          <p className="draft-terminal-copy">
            Данные переданы Авито. Наличие объявления во вкладке «Активные» не проверялось.
          </p>
          {/* Ссылки редактирования переданных объявлений по полученным item id */}
          {publishedUrls.length > 0 ? (
            <ul className="draft-saved-urls-list">
              {publishedUrls.map((url, i) => (
                <li key={url} className="draft-saved-urls-item">
                  <a
                    href={url}
                    target="_blank"
                    rel="noreferrer"
                    className="draft-saved-url-link"
                  >
                    Открыть объявление {i + 1}
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
          {isBatch && itemsPublished > 0 ? (
            <p className="draft-terminal-copy draft-terminal-partial">
              Отправлено {itemsPublished} из {itemsTotal}. Остальные не запускались.
            </p>
          ) : null}
          <p className="draft-terminal-copy">
            {status?.user_action?.message || status?.error || "Сервис не смог продолжить автоматически."}
          </p>
          {/* Обе остановки по цене (минимум выше потолка и отказ Авито) требуют
              одного и того же действия — поднять потолок. Но советовать
              «запустите весь пакет заново» можно ТОЛЬКО когда ещё ничего не
              отправлено: иначе первые объявления уйдут повторно и спишутся
              второй раз (дубли = двойная оплата). */}
          {status?.user_action?.type === "view_price_too_low"
            || status?.user_action?.type === "view_price_cap_exceeded" ? (
            <p className="draft-terminal-copy" style={{ marginTop: "0.75rem" }}>
              Объявление №{status.user_action.item_index}: минимум Авито — {status.user_action.minimum_view_price} ₽.
              {itemsPublished > 0 ? (
                <>
                  {" "}Увеличьте потолок стоимости просмотра и запустите ТОЛЬКО оставшиеся{" "}
                  {itemsTotal - itemsPublished}: повторный запуск всего пакета
                  отправит первые {itemsPublished} второй раз и спишет за них деньги повторно.
                </>
              ) : (
                <>{" "}Увеличьте потолок стоимости просмотра и запустите пакет заново.</>
              )}
            </p>
          ) : null}
        </div>
      ) : null}

      {status?.status === "failed" ? (
        <div className="draft-terminal draft-terminal-error">
          <p className="draft-terminal-title">Не удалось завершить публикацию</p>
          {/* Частичный успех при пакетном режиме */}
          {isBatch && itemsPublished > 0 ? (
            <p className="draft-terminal-copy draft-terminal-partial">
              Отправлено {itemsPublished} из {itemsTotal}. Остальные не запускались.
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

      {status?.status === "interrupted" ? (
        <div className="draft-terminal draft-terminal-action">
          <p className="draft-terminal-title">Сервер был перезапущен</p>
          <p className="draft-terminal-copy">
            {status?.error || "Сохранённый прогресс найден. Можно продолжить без повторной отправки уже завершённых объявлений."}
          </p>
        </div>
      ) : null}

      {/* Кнопка возобновления доступна при ЛЮБОМ терминальном статусе (в т.ч.
          красной «Не удалось завершить» и жёлтой «Требуется участие») — её
          видимость и смысл целиком определяет resume_plan с бэкенда, а не
          конкретный status/user_action.type */}
      {canResume && resumePlan ? (
        <div style={{ marginTop: "1rem" }}>
          <p className="draft-terminal-copy">{resumePlan.description}</p>
          <button
            className="submit-button"
            type="button"
            disabled={isResuming}
            onClick={handleResume}
            style={{ marginTop: "0.5rem" }}
          >
            {isResuming ? "Продолжаем..." : resumePlan.buttonLabel}
          </button>
        </div>
      ) : null}

      {/* Кнопки нет, но пакет не отправлен целиком — честно объясняем почему */}
      {!canResume && resumeUnavailableReason ? (
        <p className="draft-terminal-copy" style={{ marginTop: "1rem" }}>
          {resumeUnavailableReason}
        </p>
      ) : null}
    </section>
  );
}

// ─── Страница публикации (корень) ─────────────────────────────────────────────

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
            ariaLabel="Навигация публикации"
            links={[
              { to: "/workspace", label: "Аналитика" },
              { to: "/", label: "К кейсу" },
            ]}
          />

          <div className="workspace-header-shell">
            <div className="workspace-header-copy">
              <p className="section-kicker">Publish</p>
              <h1 className="workspace-title">Публикация объявлений</h1>
              {phase === "form" ? (
                <p className="workspace-intro-copy">
                  Заполните форму — сервис последовательно опубликует объявления в вашем Chrome,
                  задаст стоимость просмотра и откажется от дополнительных услуг. Для работы нужен запущенный{" "}
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
        <section className="workspace-main-stack" aria-label="Форма публикации">
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
