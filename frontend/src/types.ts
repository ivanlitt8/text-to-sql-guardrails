export type SchemaForeignKey = {
  table: string;
  column: string;
};

export type SchemaColumn = {
  name: string;
  data_type: string;
  nullable: boolean;
  is_primary_key: boolean;
  foreign_key: SchemaForeignKey | null;
  description: string | null;
};

export type SchemaTable = {
  name: string;
  description: string | null;
  columns: SchemaColumn[];
};

export type SchemaHint = {
  title: string;
  body: string;
};

export type SchemaResponse = {
  tables: SchemaTable[];
  hints: SchemaHint[];
  prompt_suggestions: string[];
  limitations: string[];
};

export type ReadyResponse = {
  ready: boolean;
  checks: Record<string, boolean>;
  details: Record<string, string> | null;
};

export type GuardrailStatus = {
  is_safe: boolean;
  blocked_reason: string | null;
  query_type: string;
  sanitized_sql: string | null;
};

export type JudgeVerdict = {
  inferred_question: string;
  reasoning: string;
  alignment_score: number;
  concerns: string[];
  is_degraded: boolean;
};

export type FinalResponse = {
  question: string;
  sql: string;
  executed: boolean;
  results: Record<string, unknown>[] | null;
  confidence_final: number;
  guardrail_status: GuardrailStatus;
  judge_verdict: JudgeVerdict;
  execution_error: string | null;
};
