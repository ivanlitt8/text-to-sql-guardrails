import type { FinalResponse } from "../types";

type AnswerViewProps = {
  answer: FinalResponse;
};

export default function AnswerView({ answer }: AnswerViewProps) {
  const { guardrail_status: guard, judge_verdict: judge } = answer;
  const writeIntent = guard.query_type === "WRITE_INTENT";
  const showResults = answer.executed && Array.isArray(answer.results);
  const columns = showResults && answer.results && answer.results.length > 0
    ? Object.keys(answer.results[0])
    : [];

  return (
    <div className="flex flex-col gap-4">
      {writeIntent && (
        <Banner tone="danger" title="Intención de escritura bloqueada">
          {guard.blocked_reason ??
            "Esta demo es de solo lectura. No se generan ni ejecutan cambios."}
        </Banner>
      )}
      {!writeIntent && !guard.is_safe && guard.blocked_reason && (
        <Banner tone="danger" title="Guardrail: consulta insegura">
          {guard.blocked_reason}
        </Banner>
      )}
      {answer.execution_error && (
        <Banner tone="warn" title="Error de ejecución (DuckDB)">
          {answer.execution_error}
        </Banner>
      )}
      {judge.is_degraded && (
        <Banner tone="warn" title="Juez degradado">
          El verificador no pudo completar la evaluación (fallback técnico). El
          score no es confiable en este turno.
        </Banner>
      )}

      {answer.sql.trim() !== "" && (
        <section className="rounded-2xl border border-slate-200/80 bg-white/90 p-4 shadow-sm">
          <h3 className="text-sm font-semibold tracking-wide text-slate-500 uppercase">
            SQL
          </h3>
          <pre className="mt-3 overflow-x-auto rounded-xl bg-slate-900 p-3 text-xs leading-relaxed text-cyan-100">
            {answer.sql}
          </pre>
        </section>
      )}

      <section className="rounded-2xl border border-slate-200/80 bg-white/90 p-4 shadow-sm">
        <h3 className="text-sm font-semibold tracking-wide text-slate-500 uppercase">
          Confianza
        </h3>
        <p className="mt-2 text-2xl font-semibold text-slate-900">
          {Math.round(answer.confidence_final * 100)}%
        </p>
        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-100">
          <div
            className="h-full rounded-full bg-cyan-700"
            style={{ width: `${Math.round(answer.confidence_final * 100)}%` }}
          />
        </div>
        <p className="mt-2 text-xs text-slate-500">
          Score compuesto (autovaloración del generador + alineación del juez).
          No es ground truth ni una métrica de eval.
        </p>
      </section>

      <section className="rounded-2xl border border-slate-200/80 bg-white/90 p-4 shadow-sm">
        <h3 className="text-sm font-semibold tracking-wide text-slate-500 uppercase">
          Guardrails
        </h3>
        <dl className="mt-3 grid gap-2 text-sm">
          <Row label="Segura" value={guard.is_safe ? "sí" : "no"} />
          <Row label="Tipo" value={guard.query_type} />
          {guard.blocked_reason && (
            <Row label="Motivo" value={guard.blocked_reason} />
          )}
        </dl>
      </section>

      <section className="rounded-2xl border border-slate-200/80 bg-white/90 p-4 shadow-sm">
        <h3 className="text-sm font-semibold tracking-wide text-slate-500 uppercase">
          Juez
        </h3>
        <p className="mt-2 text-sm text-slate-800">
          <span className="font-semibold">Pregunta inferida: </span>
          {judge.inferred_question}
        </p>
        <p className="mt-2 text-sm text-slate-700">
          Alineación {judge.alignment_score}/5
        </p>
        {judge.reasoning && (
          <p className="mt-2 text-xs leading-relaxed text-slate-600">{judge.reasoning}</p>
        )}
        {judge.concerns.length > 0 && (
          <ul className="mt-2 list-disc pl-4 text-xs text-amber-900">
            {judge.concerns.map((concern) => (
              <li key={concern}>{concern}</li>
            ))}
          </ul>
        )}
      </section>

      {showResults && (
        <section className="rounded-2xl border border-slate-200/80 bg-white/90 p-4 shadow-sm">
          <h3 className="text-sm font-semibold tracking-wide text-slate-500 uppercase">
            Resultados
          </h3>
          {answer.results && answer.results.length === 0 && (
            <p className="mt-3 text-sm text-slate-500">La consulta no devolvió filas.</p>
          )}
          {answer.results && answer.results.length > 0 && (
            <div className="mt-3 max-h-96 overflow-auto rounded-xl border border-slate-100">
              <table className="min-w-full border-collapse text-left text-xs">
                <thead className="sticky top-0 bg-slate-50">
                  <tr>
                    {columns.map((column) => (
                      <th key={column} className="px-3 py-2 font-semibold text-slate-600">
                        {column}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {answer.results.map((row, index) => (
                    <tr key={index} className="border-t border-slate-100">
                      {columns.map((column) => (
                        <td key={column} className="px-3 py-1.5 font-mono text-[11px] text-slate-800">
                          {formatCell(row[column])}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function Banner({
  tone,
  title,
  children,
}: {
  tone: "danger" | "warn";
  title: string;
  children: string;
}) {
  const styles =
    tone === "danger"
      ? "border-red-200 bg-red-50 text-red-900"
      : "border-amber-200 bg-amber-50 text-amber-950";
  return (
    <div className={`rounded-2xl border p-4 text-sm ${styles}`}>
      <p className="font-semibold">{title}</p>
      <p className="mt-1 text-xs leading-relaxed">{children}</p>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[7rem_1fr] gap-2">
      <dt className="text-slate-500">{label}</dt>
      <dd className="text-slate-800">{value}</dd>
    </div>
  );
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
