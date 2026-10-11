import assert from "node:assert/strict";
import test from "node:test";
import { ContractError, assignment, candidate, health, order, proposal, run } from "../src/decoders.ts";
import { activeRun, effectiveProposalStatus, optimizationBlock, orderedRuns, score, unresolvedProposals } from "../src/domain.ts";
import { api, ApiError, errorMessage, responseError } from "../src/api.ts";
import type { Candidate, OptimizationResult, Proposal, RunView } from "../src/models.ts";

const now = Date.parse("2026-10-05T12:00:00Z");

function makeProposal(overrides: Partial<Proposal> = {}): Proposal {
  return {
    id: "proposal-test", run_id: "run-test", staff_id: "staff-test", order_id: "order-test",
    status: "proposed", version: 1, staff_version: 1, order_version: 1, evidence_version: 1,
    expires_at: "2026-10-06T12:00:00Z", responded_at: null, confirmed_assignment_id: null,
    ...overrides,
  };
}

function makeRun(overrides: Partial<RunView> = {}): RunView {
  return {
    id: "run-test", status: "completed", created_at: "2026-10-05T12:00:00Z", input_revision: 1,
    config: { time_limit_seconds: 3, scheduling_mode: "full_period", idempotency_key: "test-request-key" },
    error: null, result: null, proposals: [],
    ...overrides,
  };
}

function makeCandidate(overrides: Partial<Candidate> = {}): Candidate {
  return {
    staff_id: "staff-test", order_id: "order-test", eligibility: "eligible", score: 0.8,
    score_parts: {
      skill: 0.9, preference: 0.8, commute: 0.6, history: null,
      history_missing: true, rated_assignments: 0, weights_version: "test-weights-v1", synthetic: true,
    },
    commute_minutes: 20,
    reasons: [{ code: "test-passed", message: "テスト条件に適合", kind: "passed" }],
    selected: false, explanation: "合成データのテスト",
    ...overrides,
  };
}

function makeResult(): OptimizationResult {
  return {
    candidates: [makeCandidate({ selected: true })],
    optimized: { method: "TEST_OPTIMIZED", pairs: [{ order_id: "order-test", staff_id: "staff-test" }], filled_slots: 1, total_score: 0.8 },
    greedy: { method: "GREEDY FEASIBLE", pairs: [], filled_slots: 0, total_score: 0 },
    solver: {
      status: "FEASIBLE", count_status: "FEASIBLE", score_status: null,
      count_optimal: false, score_optimal: false, objective_bound: 2, gap: 1,
      elapsed_ms: 3000, time_limit_seconds: 3, engine: "test-engine",
      seed: 0, workers: 1, score_scale: 10000, fallback_used: true,
    },
    unfilled: [{ order_id: "unfilled-test", slots: 1, reason: "対象候補なし", eligible_candidates: 0, pending_candidates: 0 }],
    candidate_count: 1, candidate_limit: 2000, truncated: false,
    total_remaining_slots: 2, confirmed_slots: 1, input_revision: 1,
  };
}

test("runtime contracts preserve nullable scores and missing history without fabricating a score", () => {
  assert.deepEqual(candidate(makeCandidate()), makeCandidate());
  const missing = candidate(makeCandidate({ eligibility: "pending", score: null, score_parts: null, commute_minutes: null }));
  assert.equal(missing.score, null);
  assert.equal(score(missing.score), "—");
  assert.equal(makeCandidate().score_parts?.history_missing, true);
  assert.equal(score(0), "0.000");
});

test("runtime contracts reject malformed values and unknown lifecycle states", () => {
  assert.throws(() => candidate({ ...makeCandidate(), score: "0.8" }), ContractError);
  assert.throws(() => candidate({ ...makeCandidate(), eligibility: "approved" }), ContractError);
  assert.throws(() => candidate({ ...makeCandidate(), score: Number.NaN }), ContractError);
  assert.throws(() => candidate({ ...makeCandidate(), selected: undefined }), ContractError);
  assert.throws(() => proposal({ ...makeProposal(), version: 0 }), ContractError);
  assert.throws(() => proposal({ ...makeProposal(), status: "approved" }), ContractError);
  assert.throws(() => run({ ...makeRun(), status: "done" }), ContractError);
  assert.throws(() => health({ status: "ok", mode: "production", tenant_id: "demo", persistence: "sqlite-mock" }), ContractError);
});

test("health distinguishes Azure PostgreSQL from local SQLite without inventing a tenant", () => {
  const local = { status: "ok", mode: "local-mock", tenant_id: "demo", persistence: "sqlite-mock" };
  const cloud = { status: "ok", mode: "azure-poc", tenant_id: "demo", persistence: "postgresql" };
  assert.deepEqual(health(local), local);
  assert.deepEqual(health(cloud), cloud);
  assert.equal(health({ ...local, tenant_id: "tenantB" }).tenant_id, "tenantB");
  assert.throws(() => health({ ...cloud, persistence: "sqlite-mock" }), ContractError);
  assert.throws(() => health({ ...local, persistence: "postgresql" }), ContractError);
  assert.throws(() => health({ ...cloud, tenant_id: "" }), ContractError);
});

test("complete run results retain solver uncertainty, sparse coverage, and actual comparisons", () => {
  const value = makeRun({ result: makeResult(), proposals: [makeProposal()] });
  const decoded = run(value);
  assert.deepEqual(decoded, value);
  assert.equal(decoded.result?.solver.count_optimal, false);
  assert.equal(decoded.result?.solver.score_optimal, false);
  assert.equal(decoded.result?.solver.fallback_used, true);
  assert.equal(decoded.result?.candidate_limit, 2000);
  assert.equal(decoded.result?.truncated, false);
  assert.throws(() => run({ ...value, result: { ...makeResult(), truncated: true } }), ContractError);
});

test("runtime contracts validate full-period half-open dates and timezone-aware timestamps", () => {
  const value = {
    id: "order-test", title: "テスト案件", period: { start: "2026-10-05", end: "2026-11-01" },
    headcount: 2, hourly_rate: 1500, required_skills: {}, required_qualifications: [],
    eligibility_verified: true, mandatory_verified: true, scheduling_mode: "full_period", version: 1,
  };
  assert.equal(order(value).period.end, "2026-11-01");
  assert.throws(() => order({ ...value, period: { start: "2026-11-01", end: "2026-11-01" } }), ContractError);
  assert.throws(() => order({ ...value, period: { start: "2026-02-30", end: "2026-11-01" } }), ContractError);
  assert.throws(() => proposal({ ...makeProposal(), expires_at: "2026-10-06T12:00:00" }), ContractError);
  assert.equal(proposal({ ...makeProposal(), expires_at: "2026-10-06T21:00:00+09:00" }).expires_at, "2026-10-06T21:00:00+09:00");
});

test("queued/running jobs block optimization independently of selected history", () => {
  for (const status of ["queued", "running"]) {
    const pending = run({ ...makeRun(), status });
    assert.equal(activeRun(pending), true);
    assert.match(optimizationBlock([makeRun({ id: "past" }), pending], now) ?? "", /計算中/);
  }
  assert.equal(optimizationBlock([makeRun({ status: "failed", error: "テストエラー" })], now), null);
});

test("all live proposed/consented proposals must resolve before rerunning", () => {
  const proposed = makeProposal();
  const consented = makeProposal({ id: "consented-test", status: "consented" });
  const runs = [makeRun({ proposals: [proposed, consented] })];
  assert.equal(unresolvedProposals(runs, now).length, 2);
  assert.match(optimizationBlock(runs, now) ?? "", /2件/);
  assert.equal(optimizationBlock([makeRun({ proposals: [
    makeProposal({ status: "confirmed" }), makeProposal({ id: "declined", status: "declined" }),
  ] })], now), null);
});

test("expiration closes unresolved proposals at the exact deadline but does not rewrite terminal states", () => {
  const proposal = makeProposal({ expires_at: "2026-10-05T12:00:00Z" });
  assert.equal(effectiveProposalStatus(proposal, now - 1), "proposed");
  assert.equal(effectiveProposalStatus(proposal, now), "expired");
  assert.equal(optimizationBlock([makeRun({ proposals: [proposal] })], now), null);
  assert.equal(effectiveProposalStatus({ ...proposal, status: "confirmed" }, now), "confirmed");
  assert.equal(effectiveProposalStatus({ ...proposal, status: "declined" }, now), "declined");
});

test("reload history is ordered newest first without mutating API records", () => {
  const old = makeRun({ id: "old", created_at: "2026-10-04T12:00:00Z" });
  const recent = makeRun({ id: "recent" });
  const records = [old, recent];
  assert.deepEqual(orderedRuns(records).map((item) => item.id), ["recent", "old"]);
  assert.equal(records[0]?.id, "old");
});

test("structured conflicts and validation arrays surface server messages", () => {
  const conflict = responseError(409, { detail: { code: "proposal_version_conflict", message: "提案が更新されました" } });
  assert.ok(conflict instanceof ApiError);
  assert.equal(conflict.status, 409);
  assert.match(errorMessage(conflict), /提案が更新されました/);
  const validation = responseError(422, { detail: [{ loc: ["body", "time_limit_seconds"], msg: "上限は10秒です", type: "less_than_equal" }] });
  assert.match(validation.message, /body.time_limit_seconds: 上限は10秒です/);
  assert.equal(responseError(500, "<html>error</html>").code, "HTTP_ERROR");
});

test("assignment records retain provenance and remain separate from proposals", () => {
  const saved = assignment({
    id: "assignment-test", staff_id: "staff-test", order_id: "order-test",
    period: { start: "2026-10-05", end: "2026-11-01" },
    source_run_id: "run-test", proposal_id: "proposal-test", confirmed_at: "2026-10-05T12:00:00Z",
  });
  assert.equal(saved.source_run_id, "run-test");
  assert.equal(saved.proposal_id, "proposal-test");
  assert.equal(proposal(makeProposal({ status: "consented" })).confirmed_assignment_id, null);
});

test("API uses relative proxy URLs, encodes identifiers, and sends versioned idempotent POST bodies", async (context) => {
  const calls: { path: string; options: RequestInit | undefined }[] = [];
  context.mock.method(globalThis, "fetch", async (input: string | URL | Request, options?: RequestInit) => {
    calls.push({ path: String(input), options });
    return Response.json(makeProposal({ status: "consented", version: 2 }));
  });
  const received = await api.respond("proposal/with space", {
    decision: "consent", expected_version: 1, idempotency_key: "test-request-key",
  });
  assert.equal(received.status, "consented");
  assert.equal(calls.length, 1);
  assert.equal(calls[0]?.path, "/api/proposals/proposal%2Fwith%20space/respond");
  assert.equal(calls[0]?.options?.method, "POST");
  assert.equal(calls[0]?.options?.credentials, "same-origin");
  assert.equal(new Headers(calls[0]?.options?.headers).get("X-Requested-With"), "staff-matching");
  assert.equal(new Headers(calls[0]?.options?.headers).has("Authorization"), false);
  assert.deepEqual(JSON.parse(String(calls[0]?.options?.body)), {
    decision: "consent", expected_version: 1, idempotency_key: "test-request-key",
  });
});

test("API rejects server errors and invalid JSON instead of presenting fabricated data", async (context) => {
  const fetchMock = context.mock.method(globalThis, "fetch", async () =>
    Response.json({ detail: { code: "unresolved_batch", message: "未完了の提案があります" } }, { status: 409 }));
  await assert.rejects(api.runs(), (error: unknown) => error instanceof ApiError && error.status === 409);
  fetchMock.mock.mockImplementation(async () => new Response("<html>unavailable</html>", { status: 502 }));
  await assert.rejects(api.runs(), /API応答を読み取れません/);
  fetchMock.mock.mockImplementation(async () => Response.json({ runs: [] }));
  await assert.rejects(api.runs(), ContractError);
});

test("network failures do not send cloud users to localhost", async (context) => {
  context.mock.method(globalThis, "fetch", async () => { throw new TypeError("Failed to fetch"); });
  await assert.rejects(api.health(), (error: unknown) => {
    assert.ok(error instanceof Error);
    assert.match(error.message, /ネットワーク/);
    assert.doesNotMatch(error.message, /127\.0\.0\.1|localhost|ローカル/);
    return true;
  });
});

test("GET candidate/explanation endpoints preserve arrays and saved candidate identity", async (context) => {
  const paths: string[] = [];
  context.mock.method(globalThis, "fetch", async (input: string | URL | Request) => {
    const path = String(input);
    paths.push(path);
    return Response.json(path.endsWith("/candidates") ? [makeCandidate()] : makeCandidate({ selected: true }));
  });
  const current = await api.candidates("order-test");
  const historical = await api.explanation("run-test", "order-test", "staff-test");
  assert.equal(current[0]?.selected, false);
  assert.equal(historical.selected, true);
  assert.deepEqual(paths, [
    "/api/orders/order-test/candidates",
    "/api/matching/runs/run-test/explanations/order-test/staff-test",
  ]);
});
