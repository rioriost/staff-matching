import type {
  Allocation, Assignment, Candidate, Health, OptimizeRequest, OptimizationResult,
  Order, Pair, Period, Proposal, Qualification, Reason, RunAccepted, RunStatus,
  RunView, ScoreParts, SolverReport, Staff, Unfilled,
} from "./models.ts";

type Decoder<T> = (value: unknown) => T;

export class ContractError extends Error {
  constructor(field: string) {
    super(`API応答の形式を確認できません（${field}）。バックエンドと画面のバージョンを確認してください。`);
    this.name = "ContractError";
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function record(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) throw new ContractError("オブジェクト");
  return value;
}

function text(value: unknown): string {
  if (typeof value !== "string") throw new ContractError("文字列");
  return value;
}

function number(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new ContractError("数値");
  return value;
}

function integer(value: unknown): number {
  const result = number(value);
  if (!Number.isSafeInteger(result) || result < 0) throw new ContractError("非負整数");
  return result;
}

function positiveInteger(value: unknown): number {
  const result = integer(value);
  if (result < 1) throw new ContractError("正の整数");
  return result;
}

function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new ContractError("真偽値");
  return value;
}

function literal<const T extends string>(value: unknown, choices: readonly T[]): T {
  const result = choices.find((choice) => choice === value);
  if (result === undefined) throw new ContractError(choices.join(" / "));
  return result;
}

function nullable<T>(value: unknown, decode: Decoder<T>): T | null {
  return value === null ? null : decode(value);
}

export function list<T>(value: unknown, decode: Decoder<T>): T[] {
  if (!Array.isArray(value)) throw new ContractError("配列");
  return value.map((item: unknown) => decode(item));
}

function dictionary<T>(value: unknown, decode: Decoder<T>): Record<string, T> {
  return Object.fromEntries(Object.entries(record(value)).map(([key, item]) => [key, decode(item)]));
}

function date(value: unknown): string {
  const result = text(value);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(result)) throw new ContractError("日付");
  const parsed = new Date(`${result}T00:00:00Z`);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== result) {
    throw new ContractError("有効な日付");
  }
  return result;
}

function timestamp(value: unknown): string {
  const result = text(value);
  if (!/^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/.test(result)
      || !Number.isFinite(Date.parse(result))) throw new ContractError("タイムゾーン付き日時");
  return result;
}

function period(value: unknown): Period {
  const data = record(value);
  const start = date(data.start);
  const end = date(data.end);
  if (start >= end) throw new ContractError("開始日 < 終了日");
  return { start, end };
}

function qualification(value: unknown): Qualification {
  const data = record(value);
  return { verified: boolean(data.verified), valid_until: date(data.valid_until) };
}

export function staff(value: unknown): Staff {
  const data = record(value);
  return {
    id: text(data.id), name: text(data.name),
    skills: nullable(data.skills, (item) => dictionary(item, number)),
    qualifications: nullable(data.qualifications, (item) => dictionary(item, qualification)),
    availability: nullable(data.availability, period),
    min_hourly_rate: nullable(data.min_hourly_rate, integer),
    commute_limit_minutes: nullable(data.commute_limit_minutes, positiveInteger),
    matching_enabled: nullable(data.matching_enabled, boolean),
    eligibility_verified: nullable(data.eligibility_verified, boolean),
    mandatory_verified: nullable(data.mandatory_verified, boolean),
    version: positiveInteger(data.version),
  };
}

export function order(value: unknown): Order {
  const data = record(value);
  return {
    id: text(data.id), title: text(data.title), period: period(data.period),
    headcount: positiveInteger(data.headcount), hourly_rate: positiveInteger(data.hourly_rate),
    required_skills: dictionary(data.required_skills, number),
    required_qualifications: list(data.required_qualifications, text),
    eligibility_verified: nullable(data.eligibility_verified, boolean),
    mandatory_verified: nullable(data.mandatory_verified, boolean),
    scheduling_mode: literal(data.scheduling_mode, ["full_period"]),
    version: positiveInteger(data.version),
  };
}

export function assignment(value: unknown): Assignment {
  const data = record(value);
  return {
    id: text(data.id), staff_id: text(data.staff_id), order_id: text(data.order_id),
    period: period(data.period), source_run_id: nullable(data.source_run_id, text),
    proposal_id: nullable(data.proposal_id, text), confirmed_at: timestamp(data.confirmed_at),
  };
}

function reason(value: unknown): Reason {
  const data = record(value);
  return {
    code: text(data.code), message: text(data.message),
    kind: literal(data.kind, ["passed", "violation", "unknown", "information"]),
  };
}

function scoreParts(value: unknown): ScoreParts {
  const data = record(value);
  if (data.synthetic !== true) throw new ContractError("合成スコア");
  return {
    skill: number(data.skill), preference: number(data.preference), commute: number(data.commute),
    history: nullable(data.history, number), history_missing: boolean(data.history_missing),
    rated_assignments: integer(data.rated_assignments),
    weights_version: text(data.weights_version), synthetic: true,
  };
}

export function candidate(value: unknown): Candidate {
  const data = record(value);
  return {
    staff_id: text(data.staff_id), order_id: text(data.order_id),
    eligibility: literal(data.eligibility, ["eligible", "ineligible", "pending"]),
    score: nullable(data.score, number), score_parts: nullable(data.score_parts, scoreParts),
    commute_minutes: nullable(data.commute_minutes, integer),
    reasons: list(data.reasons, reason), selected: boolean(data.selected),
    explanation: text(data.explanation),
  };
}

function pair(value: unknown): Pair {
  const data = record(value);
  return { staff_id: text(data.staff_id), order_id: text(data.order_id) };
}

function allocation(value: unknown): Allocation {
  const data = record(value);
  return {
    method: text(data.method), pairs: list(data.pairs, pair),
    filled_slots: integer(data.filled_slots), total_score: number(data.total_score),
  };
}

function solver(value: unknown): SolverReport {
  const data = record(value);
  return {
    status: literal(data.status, ["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN", "MODEL_INVALID"]),
    count_status: text(data.count_status), score_status: nullable(data.score_status, text),
    count_optimal: boolean(data.count_optimal), score_optimal: boolean(data.score_optimal),
    objective_bound: nullable(data.objective_bound, number), gap: nullable(data.gap, number),
    elapsed_ms: number(data.elapsed_ms), time_limit_seconds: number(data.time_limit_seconds),
    engine: text(data.engine), seed: integer(data.seed), workers: positiveInteger(data.workers),
    score_scale: positiveInteger(data.score_scale), fallback_used: boolean(data.fallback_used),
  };
}

function unfilled(value: unknown): Unfilled {
  const data = record(value);
  return {
    order_id: text(data.order_id), slots: integer(data.slots), reason: text(data.reason),
    eligible_candidates: integer(data.eligible_candidates),
    pending_candidates: integer(data.pending_candidates),
  };
}

function result(value: unknown): OptimizationResult {
  const data = record(value);
  if (data.truncated !== false) throw new ContractError("候補の切り捨てなし");
  return {
    candidates: list(data.candidates, candidate),
    optimized: allocation(data.optimized), greedy: allocation(data.greedy),
    solver: solver(data.solver), unfilled: list(data.unfilled, unfilled),
    candidate_count: integer(data.candidate_count), candidate_limit: integer(data.candidate_limit),
    truncated: false, total_remaining_slots: integer(data.total_remaining_slots),
    confirmed_slots: integer(data.confirmed_slots), input_revision: integer(data.input_revision),
  };
}

function config(value: unknown): OptimizeRequest {
  const data = record(value);
  return {
    time_limit_seconds: number(data.time_limit_seconds),
    scheduling_mode: literal(data.scheduling_mode, ["full_period"]),
    idempotency_key: text(data.idempotency_key),
  };
}

export function proposal(value: unknown): Proposal {
  const data = record(value);
  return {
    id: text(data.id), run_id: text(data.run_id), staff_id: text(data.staff_id),
    order_id: text(data.order_id),
    status: literal(data.status, ["proposed", "consented", "declined", "expired", "confirmed"]),
    version: positiveInteger(data.version), staff_version: positiveInteger(data.staff_version),
    order_version: positiveInteger(data.order_version), evidence_version: positiveInteger(data.evidence_version),
    expires_at: timestamp(data.expires_at), responded_at: nullable(data.responded_at, timestamp),
    confirmed_assignment_id: nullable(data.confirmed_assignment_id, text),
  };
}

function runStatus(value: unknown): RunStatus {
  return literal(value, ["queued", "running", "completed", "failed"]);
}

export function run(value: unknown): RunView {
  const data = record(value);
  return {
    id: text(data.id), status: runStatus(data.status), created_at: timestamp(data.created_at),
    input_revision: integer(data.input_revision), config: config(data.config),
    error: nullable(data.error, text), result: nullable(data.result, result),
    proposals: list(data.proposals, proposal),
  };
}

export function accepted(value: unknown): RunAccepted {
  const data = record(value);
  return { run_id: text(data.run_id), status: runStatus(data.status) };
}

export function health(value: unknown): Health {
  const data = record(value);
  const status = literal(data.status, ["ok"]);
  const tenant_id = text(data.tenant_id);
  if (!tenant_id.trim()) throw new ContractError("テナント");
  const mode = literal(data.mode, ["local-mock", "azure-poc"]);
  if (mode === "azure-poc") {
    return { status, tenant_id, mode, persistence: literal(data.persistence, ["postgresql"]) };
  }
  return {
    status, tenant_id, mode,
    persistence: literal(data.persistence, ["sqlite-mock"]),
  };
}
