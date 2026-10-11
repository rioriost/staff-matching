export interface Period {
  start: string;
  end: string;
}

export interface Qualification {
  verified: boolean;
  valid_until: string;
}

export interface Staff {
  id: string;
  name: string;
  skills: Record<string, number> | null;
  qualifications: Record<string, Qualification> | null;
  availability: Period | null;
  min_hourly_rate: number | null;
  commute_limit_minutes: number | null;
  matching_enabled: boolean | null;
  eligibility_verified: boolean | null;
  mandatory_verified: boolean | null;
  version: number;
}

export interface Order {
  id: string;
  title: string;
  period: Period;
  headcount: number;
  hourly_rate: number;
  required_skills: Record<string, number>;
  required_qualifications: string[];
  eligibility_verified: boolean | null;
  mandatory_verified: boolean | null;
  scheduling_mode: "full_period";
  version: number;
}

export interface PairEvidence {
  staff_id: string;
  order_id: string;
  commute_minutes: number | null;
  commute_verified: boolean;
  skill_fit: number;
  preference_fit: number;
  history_fit: number | null;
  rated_assignments: number;
  synthetic: true;
  version: number;
}

export interface Assignment {
  id: string;
  staff_id: string;
  order_id: string;
  period: Period;
  source_run_id: string | null;
  proposal_id: string | null;
  confirmed_at: string;
}

export interface Snapshot {
  tenant_id: string;
  revision: number;
  staff: Staff[];
  orders: Order[];
  evidence: PairEvidence[];
  assignments: Assignment[];
}

export interface Reason {
  code: string;
  message: string;
  kind: "passed" | "violation" | "unknown" | "information";
}

export interface ScoreParts {
  skill: number;
  preference: number;
  commute: number;
  history: number | null;
  history_missing: boolean;
  rated_assignments: number;
  weights_version: string;
  synthetic: true;
}

export interface Candidate {
  staff_id: string;
  order_id: string;
  eligibility: "eligible" | "ineligible" | "pending";
  score: number | null;
  score_parts: ScoreParts | null;
  commute_minutes: number | null;
  reasons: Reason[];
  selected: boolean;
  explanation: string;
}

export interface Pair {
  staff_id: string;
  order_id: string;
}

export interface Allocation {
  method: string;
  pairs: Pair[];
  filled_slots: number;
  total_score: number;
}

export interface SolverReport {
  status: "OPTIMAL" | "FEASIBLE" | "INFEASIBLE" | "UNKNOWN" | "MODEL_INVALID";
  count_status: string;
  score_status: string | null;
  count_optimal: boolean;
  score_optimal: boolean;
  objective_bound: number | null;
  gap: number | null;
  elapsed_ms: number;
  time_limit_seconds: number;
  engine: string;
  seed: number;
  workers: number;
  score_scale: number;
  fallback_used: boolean;
}

export interface Unfilled {
  order_id: string;
  slots: number;
  reason: string;
  eligible_candidates: number;
  pending_candidates: number;
}

export interface OptimizationResult {
  candidates: Candidate[];
  optimized: Allocation;
  greedy: Allocation;
  solver: SolverReport;
  unfilled: Unfilled[];
  candidate_count: number;
  candidate_limit: number;
  truncated: false;
  total_remaining_slots: number;
  confirmed_slots: number;
  input_revision: number;
}

export interface OptimizeRequest {
  time_limit_seconds: number;
  scheduling_mode: "full_period";
  idempotency_key: string;
}

export type RunStatus = "queued" | "running" | "completed" | "failed";
export type ProposalStatus = "proposed" | "consented" | "declined" | "expired" | "confirmed";

export interface Proposal {
  id: string;
  run_id: string;
  staff_id: string;
  order_id: string;
  status: ProposalStatus;
  version: number;
  staff_version: number;
  order_version: number;
  evidence_version: number;
  expires_at: string;
  responded_at: string | null;
  confirmed_assignment_id: string | null;
}

export interface RespondRequest {
  decision: "consent" | "decline";
  expected_version: number;
  idempotency_key: string;
}

export interface ConfirmRequest {
  expected_version: number;
  idempotency_key: string;
}

export interface RunAccepted {
  run_id: string;
  status: RunStatus;
}

export interface RunView {
  id: string;
  status: RunStatus;
  created_at: string;
  input_revision: number;
  config: OptimizeRequest;
  error: string | null;
  result: OptimizationResult | null;
  proposals: Proposal[];
}

interface HealthBase {
  status: "ok";
  tenant_id: string;
}

export type Health = HealthBase & (
  { mode: "local-mock"; persistence: "sqlite-mock" }
  | { mode: "azure-poc"; persistence: "postgresql" }
);

export interface ErrorDetail {
  code: string;
  message: string;
}

export interface ErrorResponse {
  detail: ErrorDetail;
}
