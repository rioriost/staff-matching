import * as decode from "./decoders.ts";
import { getAuthTenantHeaders, notifyAuthFailure } from "./auth-api.ts";
import type { ConfirmRequest, OptimizeRequest, RespondRequest } from "./models.ts";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}（${error.code} / HTTP ${error.status}）`;
  if (error instanceof Error) return error.message;
  return "処理に失敗しました。状態を再取得してください。";
}

export function responseError(status: number, body: unknown): ApiError {
  if (decode.isRecord(body)) {
    const detail = body.detail;
    if (decode.isRecord(detail) && typeof detail.message === "string" && typeof detail.code === "string") {
      return new ApiError(status, detail.code, detail.message);
    }
    if (Array.isArray(detail)) {
      const messages = detail.flatMap((item: unknown) => {
        if (!decode.isRecord(item) || typeof item.msg !== "string") return [];
        const location = Array.isArray(item.loc)
          ? item.loc.filter((part: unknown) => typeof part === "string" || typeof part === "number").join(".")
          : "入力";
        return [`${location}: ${item.msg}`];
      });
      return new ApiError(status, "VALIDATION_ERROR", messages.join(" / ") || "入力内容を確認してください。");
    }
    if (typeof detail === "string") return new ApiError(status, "HTTP_ERROR", detail);
  }
  return new ApiError(status, "HTTP_ERROR", "APIから正常な応答がありません。バックエンドの状態を確認してください。");
}

async function request<T>(
  path: string,
  decoder: (value: unknown) => T,
  options: { signal?: AbortSignal; body?: OptimizeRequest | RespondRequest | ConfirmRequest } = {},
): Promise<T> {
  const controller = new AbortController();
  const forwardAbort = () => controller.abort();
  options.signal?.addEventListener("abort", forwardAbort, { once: true });
  if (options.signal?.aborted) controller.abort();
  const timeout = globalThis.setTimeout(() => controller.abort(), 15_000);
  try {
    const response = await fetch(`/api${path}`, {
      method: options.body ? "POST" : "GET",
      headers: options.body
        ? { "Content-Type": "application/json", Accept: "application/json", "X-Requested-With": "staff-matching", ...getAuthTenantHeaders() }
        : { Accept: "application/json", ...getAuthTenantHeaders() },
      body: options.body ? JSON.stringify(options.body) : undefined,
      signal: controller.signal,
      cache: "no-store",
      credentials: "same-origin",
    });
    if (response.status === 401) notifyAuthFailure(401, "authentication_required");
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      throw new ApiError(response.status, "INVALID_JSON", "API応答を読み取れません。サービスの稼働状況を確認してください。");
    }
    if (!response.ok) {
      const error = responseError(response.status, body);
      if (error.status === 409 && error.code === "tenant_changed") {
        notifyAuthFailure(error.status, error.code);
      }
      throw error;
    }
    return decoder(body);
  } catch (error) {
    if (options.signal?.aborted) throw error;
    if (controller.signal.aborted) {
      throw new Error("API応答が15秒以内に届きませんでした。操作結果が未確認のため、状態を再取得してください。");
    }
    if (error instanceof TypeError) {
      throw new Error("APIに接続できません。ネットワークとサービスの稼働状況を確認して、状態を再取得してください。");
    }
    throw error;
  } finally {
    globalThis.clearTimeout(timeout);
    options.signal?.removeEventListener("abort", forwardAbort);
  }
}

const segment = encodeURIComponent;

export const api = {
  health: (signal?: AbortSignal) => request("/health", decode.health, { signal }),
  staff: (signal?: AbortSignal) => request("/staff", (data) => decode.list(data, decode.staff), { signal }),
  orders: (signal?: AbortSignal) => request("/orders", (data) => decode.list(data, decode.order), { signal }),
  assignments: (signal?: AbortSignal) => request("/assignments", (data) => decode.list(data, decode.assignment), { signal }),
  candidates: (orderId: string, signal?: AbortSignal) =>
    request(`/orders/${segment(orderId)}/candidates`, (data) => decode.list(data, decode.candidate), { signal }),
  runs: (signal?: AbortSignal) => request("/matching/runs", (data) => decode.list(data, decode.run), { signal }),
  run: (runId: string, signal?: AbortSignal) => request(`/matching/runs/${segment(runId)}`, decode.run, { signal }),
  explanation: (runId: string, orderId: string, staffId: string, signal?: AbortSignal) =>
    request(`/matching/runs/${segment(runId)}/explanations/${segment(orderId)}/${segment(staffId)}`, decode.candidate, { signal }),
  optimize: (body: OptimizeRequest) => request("/matching/optimize", decode.accepted, { body }),
  respond: (proposalId: string, body: RespondRequest) =>
    request(`/proposals/${segment(proposalId)}/respond`, decode.proposal, { body }),
  confirm: (proposalId: string, body: ConfirmRequest) =>
    request(`/proposals/${segment(proposalId)}/confirm`, decode.assignment, { body }),
};
