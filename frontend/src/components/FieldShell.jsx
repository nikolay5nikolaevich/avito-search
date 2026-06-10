// Обёртка поля формы черновика: рамка field-shell + подпись + ошибка валидации.
// В WorkspacePage похожий паттерн построен на <label> без ошибок — это другой
// компонент по семантике, сюда его не объединяем.

function FieldError({ message }) {
  if (!message) return null;
  return <p className="draft-field-error">{message}</p>;
}

export default function FieldShell({ label, error, children }) {
  return (
    <div className={`field-shell${error ? " field-shell-invalid" : ""}`}>
      <span>{label}</span>
      {children}
      <FieldError message={error} />
    </div>
  );
}
