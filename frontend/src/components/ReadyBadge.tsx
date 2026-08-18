import type { ReadyResponse } from "../types";

type ReadyBadgeProps = {
  ready: ReadyResponse | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
};

const CHECK_LABELS: Record<string, string> = {
  duckdb: "DuckDB",
  ollama: "Ollama",
  generator_model: "Generador",
  judge_model: "Juez",
};

export default function ReadyBadge({
  ready,
  loading,
  error,
  onRetry,
}: ReadyBadgeProps) {
  if (loading && !ready) {
    return (
      <span className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-medium text-slate-500">
        Comprobando backend…
      </span>
    );
  }

  if (error && !ready) {
    return (
      <button
        type="button"
        onClick={onRetry}
        className="inline-flex items-center gap-2 rounded-full border border-red-200 bg-red-50 px-3 py-1 text-xs font-medium text-red-800"
      >
        No se pudo consultar /ready · reintentar
      </button>
    );
  }

  if (!ready) {
    return null;
  }

  const failed = Object.entries(ready.checks).filter(([, ok]) => !ok);

  return (
    <div className="flex flex-col items-end gap-1">
      <span
        className={
          ready.ready
            ? "inline-flex items-center gap-2 rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-xs font-semibold text-emerald-800"
            : "inline-flex items-center gap-2 rounded-full border border-amber-200 bg-amber-50 px-3 py-1 text-xs font-semibold text-amber-900"
        }
      >
        <span
          className={
            ready.ready
              ? "h-1.5 w-1.5 rounded-full bg-emerald-600"
              : "h-1.5 w-1.5 rounded-full bg-amber-500"
          }
        />
        {ready.ready ? "Listo" : "No listo"}
      </span>
      {!ready.ready && failed.length > 0 && (
        <ul className="max-w-xs text-right text-[11px] text-amber-900/80">
          {failed.map(([key]) => (
            <li key={key}>
              {CHECK_LABELS[key] ?? key}
              {ready.details?.[key] ? `: ${ready.details[key]}` : ""}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
