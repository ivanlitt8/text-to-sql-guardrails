import type { FinalResponse, ReadyResponse, SchemaResponse } from "./types";

export const API_BASE_URL = (
  import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000"
).replace(/\/$/, "");

const ASK_TIMEOUT_MS = Number(import.meta.env.VITE_ASK_TIMEOUT_MS ?? 900_000);

export class ApiError extends Error {
  readonly status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function readJson<T>(response: Response): Promise<T> {
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError("El servidor devolvió una respuesta inválida.", response.status);
  }
}

export async function fetchReady(): Promise<ReadyResponse> {
  const response = await fetch(`${API_BASE_URL}/v1/ready`);
  return readJson<ReadyResponse>(response);
}

export async function fetchSchema(): Promise<SchemaResponse> {
  const response = await fetch(`${API_BASE_URL}/v1/schema`);
  const data = await readJson<SchemaResponse | { detail?: string }>(response);
  if (!response.ok) {
    const detail =
      "detail" in data && typeof data.detail === "string"
        ? data.detail
        : "No se pudo leer el schema.";
    throw new ApiError(detail, response.status);
  }
  return data as SchemaResponse;
}

export async function askQuestion(question: string): Promise<FinalResponse> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), ASK_TIMEOUT_MS);
  try {
    const response = await fetch(`${API_BASE_URL}/v1/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
      signal: controller.signal,
    });
    const data = await readJson<FinalResponse | { detail?: unknown }>(response);
    if (!response.ok) {
      const detail =
        data && typeof data === "object" && "detail" in data ? data.detail : undefined;
      throw new ApiError(formatAskError(response.status, detail), response.status);
    }
    return data as FinalResponse;
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(
        "La consulta superó el tiempo de espera. El generador local puede tardar varios minutos.",
      );
    }
    throw new ApiError(
      "No se pudo contactar la API. ¿Está corriendo en " + API_BASE_URL + "?",
    );
  } finally {
    window.clearTimeout(timer);
  }
}

function formatAskError(status: number, detail: unknown): string {
  if (status === 422) {
    return "La pregunta no puede estar vacía.";
  }
  if (typeof detail === "string") {
    return detail;
  }
  return `Error HTTP ${status} al consultar /v1/ask.`;
}
