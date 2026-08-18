type AskFormProps = {
  question: string;
  asking: boolean;
  backendReady: boolean | null;
  onChange: (value: string) => void;
  onSubmit: () => void;
};

export default function AskForm({
  question,
  asking,
  backendReady,
  onChange,
  onSubmit,
}: AskFormProps) {
  const disabled = asking || question.trim().length === 0;

  return (
    <form
      className="rounded-2xl border border-slate-200/80 bg-white/90 p-4 shadow-sm"
      onSubmit={(event) => {
        event.preventDefault();
        if (!disabled) onSubmit();
      }}
    >
      <label htmlFor="question" className="text-sm font-semibold text-slate-800">
        Pregunta
      </label>
      <p className="mt-1 text-xs text-slate-500">
        Español o inglés, sobre el schema de la izquierda. Una pregunta a la vez.
      </p>
      <textarea
        id="question"
        value={question}
        disabled={asking}
        onChange={(event) => onChange(event.target.value)}
        rows={4}
        placeholder="¿Cuántos vuelos hay con destino a …?"
        className="mt-3 w-full resize-y rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-900 outline-none ring-cyan-700/30 focus:bg-white focus:ring-2 disabled:opacity-60"
      />
      <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
        {backendReady === false && (
          <p className="text-xs text-amber-800">
            El backend no está listo (Ollama o DuckDB). Podés enviar igual, pero
            es probable que falle.
          </p>
        )}
        <button
          type="submit"
          disabled={disabled}
          className="ml-auto rounded-xl bg-cyan-800 px-4 py-2 text-sm font-semibold text-white hover:bg-cyan-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {asking ? "Generando…" : "Preguntar"}
        </button>
      </div>
    </form>
  );
}
