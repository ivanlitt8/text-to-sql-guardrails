import { useCallback, useEffect, useState } from "react";
import { ApiError, askQuestion, fetchReady, fetchSchema } from "./api";
import AskForm from "./components/AskForm";
import AnswerView from "./components/AnswerView";
import DatabasePanel from "./components/DatabasePanel";
import ReadyBadge from "./components/ReadyBadge";
import type { FinalResponse, ReadyResponse, SchemaResponse } from "./types";

type MobileTab = "db" | "ask";

export default function App() {
  const [mobileTab, setMobileTab] = useState<MobileTab>("ask");
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [schemaLoading, setSchemaLoading] = useState(true);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [ready, setReady] = useState<ReadyResponse | null>(null);
  const [readyLoading, setReadyLoading] = useState(true);
  const [readyError, setReadyError] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [answer, setAnswer] = useState<FinalResponse | null>(null);
  const [askError, setAskError] = useState<string | null>(null);

  const loadSchema = useCallback(async () => {
    setSchemaLoading(true);
    setSchemaError(null);
    try {
      setSchema(await fetchSchema());
    } catch (error) {
      setSchema(null);
      setSchemaError(error instanceof ApiError ? error.message : "No se pudo cargar el schema.");
    } finally {
      setSchemaLoading(false);
    }
  }, []);

  const loadReady = useCallback(async () => {
    setReadyLoading(true);
    setReadyError(null);
    try {
      setReady(await fetchReady());
    } catch (error) {
      setReady(null);
      setReadyError(
        error instanceof ApiError ? error.message : "No se pudo consultar /v1/ready.",
      );
    } finally {
      setReadyLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSchema();
    void loadReady();
  }, [loadSchema, loadReady]);

  async function handleAsk() {
    const trimmed = question.trim();
    if (!trimmed || asking) return;
    setAsking(true);
    setAskError(null);
    setAnswer(null);
    try {
      setAnswer(await askQuestion(trimmed));
    } catch (error) {
      setAskError(
        error instanceof ApiError
          ? error.message
          : "Falló la consulta. Revisá que la API esté en marcha.",
      );
    } finally {
      setAsking(false);
      void loadReady();
    }
  }

  function handleSuggestion(text: string) {
    setQuestion(text);
    setMobileTab("ask");
  }

  return (
    <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
      <header className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs font-semibold tracking-[0.2em] text-cyan-800 uppercase">
            Demo local
          </p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-900">
            Text-to-SQL Vuelos
          </h1>
          <p className="mt-1 max-w-xl text-sm text-slate-600">
            Pregunta en lenguaje natural. Un generador arma SQL, los guardrails
            bloquean escrituras, DuckDB ejecuta en solo lectura y un juez verifica
            alineación.
          </p>
          <ol className="mt-3 flex flex-wrap gap-2 text-[11px] font-medium text-slate-500">
            <li className="rounded-full bg-white px-2.5 py-1 ring-1 ring-slate-200">
              1. Generador
            </li>
            <li className="rounded-full bg-white px-2.5 py-1 ring-1 ring-slate-200">
              2. Guardrails
            </li>
            <li className="rounded-full bg-white px-2.5 py-1 ring-1 ring-slate-200">
              3. Ejecución
            </li>
            <li className="rounded-full bg-white px-2.5 py-1 ring-1 ring-slate-200">
              4. Juez
            </li>
          </ol>
        </div>
        <ReadyBadge
          ready={ready}
          loading={readyLoading}
          error={readyError}
          onRetry={() => void loadReady()}
        />
      </header>

      <div className="mb-4 flex gap-2 lg:hidden">
        <TabButton active={mobileTab === "ask"} onClick={() => setMobileTab("ask")}>
          Preguntar
        </TabButton>
        <TabButton active={mobileTab === "db"} onClick={() => setMobileTab("db")}>
          Base de datos
        </TabButton>
      </div>

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
        <div className={mobileTab === "db" ? "block" : "hidden lg:block"}>
          <DatabasePanel
            schema={schema}
            loading={schemaLoading}
            error={schemaError}
            onRetry={() => void loadSchema()}
            onPickSuggestion={handleSuggestion}
          />
        </div>

        <main className={mobileTab === "ask" ? "flex flex-col gap-4" : "hidden lg:flex lg:flex-col lg:gap-4"}>
          <AskForm
            question={question}
            asking={asking}
            backendReady={ready ? ready.ready : null}
            onChange={setQuestion}
            onSubmit={() => void handleAsk()}
          />

          {asking && (
            <div className="rounded-2xl border border-cyan-200 bg-cyan-50 px-4 py-6 text-center">
              <p className="text-sm font-semibold text-cyan-950">Generando SQL…</p>
              <p className="mt-1 text-xs text-cyan-900/70">
                Puede tardar varios minutos: sqlcoder y el juez corren en Ollama
                local.
              </p>
            </div>
          )}

          {askError && (
            <div className="rounded-2xl border border-red-200 bg-red-50 p-4 text-sm text-red-900">
              {askError}
            </div>
          )}

          {answer && !asking && <AnswerView answer={answer} />}
        </main>
      </div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        active
          ? "flex-1 rounded-xl bg-cyan-800 px-3 py-2 text-sm font-semibold text-white"
          : "flex-1 rounded-xl bg-white px-3 py-2 text-sm font-medium text-slate-600 ring-1 ring-slate-200"
      }
    >
      {children}
    </button>
  );
}
