// Полоса прогресса фоновой задачи.
// percent — готовое число 0..100 (страницы считают его по-своему),
// style — опциональные inline-стили обёртки (например, отступ сверху).
export default function ProgressBar({ percent, style }) {
  return (
    <div className="workspace-progress-shell" style={style}>
      <div className="progress-bar-fill" style={{ width: `${percent}%` }} />
    </div>
  );
}
