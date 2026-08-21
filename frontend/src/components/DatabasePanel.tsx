import type { SchemaResponse } from "../types";
import SchemaDeck from "./SchemaDeck";

type DatabasePanelProps = {
  schema: SchemaResponse | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onPickSuggestion: (question: string) => void;
};

export default function DatabasePanel({
  schema,
  loading,
  error,
  onRetry,
  onPickSuggestion,
}: DatabasePanelProps) {
  return (
    <aside className="flex flex-col gap-4">
      <section className="rounded-2xl border border-slate-200/80 bg-white/90 p-4 shadow-sm">
        <h2 className="text-sm font-semibold tracking-wide text-slate-500 uppercase">
          Base de datos
        </h2>
        <p className="mt-1 text-sm text-slate-600">
          Cada capa es una entidad del schema.
        </p>

        {loading && (
          <p className="mt-4 text-sm text-slate-500">Cargando tablas…</p>
        )}
        {error && (
          <div className="mt-4 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800">
            <p>{error}</p>
            <button
              type="button"
              onClick={onRetry}
              className="mt-2 text-xs font-semibold underline"
            >
              Reintentar
            </button>
          </div>
        )}
        {schema && <SchemaDeck tables={schema.tables} />}
      </section>

      {/* {schema && schema.hints.length > 0 && (
        <section className="rounded-2xl border border-slate-200/80 bg-white/90 p-4 shadow-sm">
          <h2 className="text-sm font-semibold tracking-wide text-slate-500 uppercase">
            Hints
          </h2>
          <ul className="mt-3 space-y-3">
            {schema.hints.map((hint) => (
              <li key={hint.title}>
                <p className="text-sm font-semibold text-slate-800">{hint.title}</p>
                <p className="mt-1 text-xs leading-relaxed text-slate-600">{hint.body}</p>
              </li>
            ))}
          </ul>
        </section>
      )} */}

      {/* {schema && schema.limitations.length > 0 && (
        <section className="rounded-2xl border border-slate-200/80 bg-white/90 p-4 shadow-sm">
          <h2 className="text-sm font-semibold tracking-wide text-slate-500 uppercase">
            Limitaciones
          </h2>
          <ul className="mt-3 list-disc space-y-1 pl-4 text-xs leading-relaxed text-slate-600">
            {schema.limitations.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
      )} */}

      {schema && schema.prompt_suggestions.length > 0 && (
        <section className="rounded-2xl border border-cyan-200/80 bg-cyan-50/60 p-4 shadow-sm">
          <h2 className="text-sm font-semibold tracking-wide text-cyan-900 uppercase">
            Ideas para preguntar
          </h2>
          <p className="mt-1 text-xs text-cyan-900/70">
            Rellenan el cuadro. No ejecutan la consulta.
          </p>
          <div className="mt-3 flex flex-col gap-2">
            {schema.prompt_suggestions.map((suggestion) => (
              <button
                key={suggestion}
                type="button"
                onClick={() => onPickSuggestion(suggestion)}
                className="rounded-lg border border-cyan-200 bg-white px-3 py-2 text-left text-sm text-slate-800 hover:border-cyan-400 hover:bg-cyan-50"
              >
                {suggestion}
              </button>
            ))}
          </div>
        </section>
      )}
    </aside>
  );
}
