import type { z } from "zod";
import { ApiError, responseError } from "./api.ts";
import { getAuthTenantHeaders, notifyAuthFailure } from "./auth-api.ts";
import { decode, pageSchema, schemas } from "./saas-models.ts";
import type { AppointmentRequest, MatchRequest, ShortlistRequest, SimulationRequest, Versioned } from "./saas-models.ts";

type Query = Record<string, string | number | undefined>;
function query(params: Query = {}) {
  const result = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") result.set(key, String(value));
  }
  return result.size ? `?${result}` : "";
}
async function request<T>(path: string, schema: z.ZodType<T>,
  options: { signal?: AbortSignal; body?: object } = {}): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) abort();
  const timeout = setTimeout(abort, 20_000);
  const unsafe = options.body !== undefined;
  try {
    const response = await fetch(`/api/saas${path}`, {
      method: unsafe ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
      signal: controller.signal,
      headers: { Accept: "application/json", ...getAuthTenantHeaders(),
        ...(unsafe ? { "Content-Type": "application/json", "X-Requested-With": "staff-matching" } : {}) },
      body: unsafe ? JSON.stringify(options.body) : undefined,
    });
    if (response.status === 401) notifyAuthFailure(401, "authentication_required");
    let data: unknown;
    try { data = await response.json(); } catch {
      throw new ApiError(response.status, unsafe ? "OUTCOME_UNKNOWN" : "INVALID_JSON",
        unsafe ? "応答を確認できません。操作が完了した可能性があります。再送せず、状態を再取得してください。"
          : "応答を読み取れません。状態を再取得してください。");
    }
    if (!response.ok) {
      const error = responseError(response.status, data);
      if (error.status === 409 && error.code === "tenant_changed") notifyAuthFailure(409, error.code);
      throw error;
    }
    try { return decode(schema, data); } catch (error) {
      if (!unsafe) throw error;
      throw new ApiError(response.status, "OUTCOME_UNKNOWN",
        "想定外の応答のため操作結果は未確認です。再送せず、状態を再取得してください。");
    }
  } catch (error) {
    if (options.signal?.aborted) throw error;
    if (error instanceof TypeError || controller.signal.aborted) {
      throw new ApiError(0, unsafe ? "OUTCOME_UNKNOWN" : "CONNECTION_FAILED", unsafe
        ? "通信が途切れ、操作結果を確認できません。自動再送はしていません。状態を再取得し、重複がないことを確認してください。"
        : "通信できません。接続を確認して状態を再取得してください。");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abort);
  }
}
const seg = encodeURIComponent;
export const CANDIDATE_PAGE_SIZE = 6;
export const idempotencyKey = () => crypto.randomUUID();
export const versioned = (version: number): Versioned => ({ expected_version: version, idempotency_key: idempotencyKey() });
export const saasApi = {
  overview: (signal?: AbortSignal) => request("/overview", schemas.overview, { signal }),
  jobs: (params: Query = {}, signal?: AbortSignal) => request(`/jobs${query({ limit: 50, ...params })}`, pageSchema(schemas.job), { signal }),
  job: (id: string, signal?: AbortSignal) => request(`/jobs/${seg(id)}`, schemas.job, { signal }),
  people: (params: Query = {}, signal?: AbortSignal) => request(`/people${query({ limit: 50, ...params })}`, pageSchema(schemas.personSummary), { signal }),
  person: (id: string, signal?: AbortSignal) => request(`/people/${seg(id)}`, schemas.person, { signal }),
  companies: (signal?: AbortSignal) => request("/companies", pageSchema(schemas.company), { signal }),
  sites: (companyId: string, signal?: AbortSignal) => request(`/sites${query({ company_id: companyId })}`, pageSchema(schemas.site), { signal }),
  runs: (signal?: AbortSignal, cursor?: string) => request(`/matching/runs${query({ cursor, limit: 50 })}`, pageSchema(schemas.matchRun), { signal }),
  run: (id: string, signal?: AbortSignal) => request(`/matching/runs/${seg(id)}`, schemas.matchRun, { signal }),
  startRun: (body: MatchRequest) => request("/matching/runs", schemas.matchRun, { body }),
  cancelRun: (id: string) => request(`/matching/runs/${seg(id)}/cancel`, schemas.matchRun, { body: { idempotency_key: idempotencyKey() } }),
  candidates: (runId: string, jobId: string, cursor?: string, signal?: AbortSignal) =>
    request(`/matching/runs/${seg(runId)}/candidates${query({ job_id: jobId, cursor, limit: CANDIDATE_PAGE_SIZE })}`,
      pageSchema(schemas.candidate).refine((page) => page.items.length <= CANDIDATE_PAGE_SIZE), { signal }),
  introductions: (signal?: AbortSignal, cursor?: string) => request(`/introductions${query({ cursor, limit: 50 })}`, pageSchema(schemas.introduction), { signal }),
  shortlist: (body: ShortlistRequest) => request("/introductions", schemas.introduction, { body }),
  simulate: (id: string, body: SimulationRequest) => request(`/introductions/${seg(id)}/simulate`, schemas.introduction, { body }),
  cancelIntroduction: (id: string, body: Versioned) => request(`/introductions/${seg(id)}/cancel`, schemas.introduction, { body }),
  place: (id: string, body: Versioned) => request(`/introductions/${seg(id)}/place`, schemas.introduction, { body }),
  appointments: (signal?: AbortSignal, cursor?: string) => request(`/appointments${query({ cursor, limit: 50 })}`, pageSchema(schemas.appointment), { signal }),
  propose: (id: string, body: AppointmentRequest) => request(`/introductions/${seg(id)}/appointments`, schemas.appointment, { body }),
  acceptAppointment: (id: string, body: Versioned) => request(`/appointments/${seg(id)}/accept`, schemas.appointment, { body }),
  cancelAppointment: (id: string, body: Versioned) => request(`/appointments/${seg(id)}/cancel`, schemas.appointment, { body }),
  reschedule: (id: string, body: AppointmentRequest) => request(`/appointments/${seg(id)}/reschedule`, schemas.appointment, { body }),
  notifications: (audience: "management" | "company" | "person", cursor?: string, signal?: AbortSignal) =>
    request(`/notifications${query({ audience, cursor, limit: 30 })}`, pageSchema(schemas.notification), { signal }),
  read: (id: string) => request(`/notifications/${seg(id)}/read`, schemas.notification, { body: { idempotency_key: idempotencyKey() } }),
  basemap: (signal?: AbortSignal) => request("/geo/basemap", schemas.basemap, { signal }),
};
