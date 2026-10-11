import {
  browserSupportsWebAuthn, startAuthentication, startRegistration, WebAuthnAbortService,
} from "@simplewebauthn/browser";
import type {
  AuthenticationResponseJSON, PublicKeyCredentialCreationOptionsJSON,
  PublicKeyCredentialRequestOptionsJSON, RegistrationResponseJSON,
} from "@simplewebauthn/browser";

export interface AuthTenant { id: string; name: string; role: string }
export interface AccountSession {
  enabled: true;
  authenticated: true;
  user: { id: string; username: string; display_name: string };
  tenants: AuthTenant[];
  tenant: AuthTenant;
  auth_method: "password" | "passkey";
  passkey_count: number;
}
export type AuthSession = AccountSession | { enabled: boolean; authenticated: false };
export interface Passkey {
  id: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
  backed_up: boolean;
  device_type: "singleDevice" | "multiDevice";
}
export type AuthState =
  | { kind: "loading"; message: string }
  | { kind: "error"; message: string; recovery: "refresh" | "logout" }
  | { kind: "ready"; session: AuthSession; notice: string };
export type AuthInvalidation = "session" | "logout" | "expired";

export class AuthError extends Error {
  readonly code: string;
  readonly status: number;
  constructor(code: string, status = 0) {
    super(code);
    this.name = "AuthError";
    this.code = code;
    this.status = status;
  }
}

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string" && value.length > 0;
const invalid = (): never => { throw new AuthError("invalid_response"); };

function tenant(value: unknown): AuthTenant {
  if (!record(value) || !text(value.id) || !text(value.name) || !text(value.role)) return invalid();
  return { id: value.id, name: value.name, role: value.role };
}

export function decodeAuthSession(value: unknown): AuthSession {
  if (!record(value) || typeof value.enabled !== "boolean") return invalid();
  if (value.authenticated === false) return { enabled: value.enabled, authenticated: false };
  if (!value.enabled || value.authenticated !== true || !record(value.user)) return invalid();
  const user = value.user;
  if (!text(user.id) || !text(user.username) || !text(user.display_name)) return invalid();
  if (!Array.isArray(value.tenants) || !value.tenants.length) return invalid();
  const tenants = value.tenants.map(tenant);
  const current = tenant(value.tenant);
  if (new Set(tenants.map((item) => item.id)).size !== tenants.length ||
    !tenants.some((item) => item.id === current.id && item.name === current.name && item.role === current.role)) return invalid();
  if (value.auth_method !== "password" && value.auth_method !== "passkey") return invalid();
  if (typeof value.passkey_count !== "number" || !Number.isSafeInteger(value.passkey_count) || value.passkey_count < 0) return invalid();
  return {
    enabled: true, authenticated: true,
    user: { id: user.id, username: user.username, display_name: user.display_name },
    tenants, tenant: current, auth_method: value.auth_method, passkey_count: value.passkey_count,
  };
}

function account(value: unknown): AccountSession {
  const session = decodeAuthSession(value);
  if (!session.authenticated) return invalid();
  return session;
}

export function decodePasskeys(value: unknown): Passkey[] {
  if (!record(value) || !Array.isArray(value.items)) return invalid();
  const timestamp = (input: unknown): input is string =>
    typeof input === "string" && /(?:Z|[+-]\d{2}:\d{2})$/.test(input) && Number.isFinite(Date.parse(input));
  const items = value.items.map((item): Passkey => {
    if (!record(item) || !text(item.id) || !text(item.name) || !timestamp(item.created_at) ||
      (item.last_used_at !== null && !timestamp(item.last_used_at)) || typeof item.backed_up !== "boolean") return invalid();
    const deviceType = item.device_type === "singleDevice" || item.device_type === "single_device" ? "singleDevice"
      : item.device_type === "multiDevice" || item.device_type === "multi_device" ? "multiDevice" : invalid();
    return {
      id: item.id, name: item.name, created_at: item.created_at, last_used_at: item.last_used_at,
      backed_up: item.backed_up, device_type: deviceType,
    };
  });
  if (new Set(items.map((item) => item.id)).size !== items.length) return invalid();
  return items;
}

function ok(value: unknown): void {
  if (!record(value) || value.ok !== true) invalid();
}

export function passwordProblem(value: string, confirmation: string): string | null {
  const length = [...value].length;
  if (length < 15 || length > 128) return "新しいパスワードは15〜128文字で入力してください。空白も文字として扱います。";
  if (value !== confirmation) return "確認用パスワードが一致しません。";
  return null;
}

export function passkeyNameProblem(value: string): string | null {
  const length = [...value].length;
  return length < 1 || length > 80 || /[\u0000-\u001f\u007f]/.test(value)
    ? "パスキーの名前は1〜80文字で入力してください。改行などの制御文字は使えません。"
    : null;
}

export function passkeyUnavailableReason(): string | null {
  if (typeof window === "undefined" || !window.isSecureContext) {
    return "この接続ではパスキーを使えません。正規のHTTPSアドレスで開くか、IDとパスワードでログインしてください。";
  }
  if (!browserSupportsWebAuthn()) return "このブラウザはパスキーに対応していません。IDとパスワードでログインできます。";
  return null;
}

export function isPasskeyCancellation(error: unknown): boolean {
  return error instanceof Error && (error.name === "NotAllowedError" || error.name === "AbortError" ||
    (error instanceof AuthError && error.code === "passkey_cancelled"));
}

export function authErrorMessage(error: unknown): string {
  if (isPasskeyCancellation(error)) return "パスキーの操作を完了しませんでした。もう一度試すか、パスワードをご利用ください。";
  if (error instanceof AuthError) {
    const messages: Record<string, string> = {
      reauthentication_required: "安全のため、現在のパスワードをもう一度入力してください。",
      invalid_credentials: "IDまたはパスワードを確認してください。",
      invalid_password: "現在のパスワードを確認してください。",
      password_policy: "新しいパスワードは15〜128文字で入力してください。",
      invalid_response: "認証サービスの応答を確認できません。状態を再取得してください。",
      uncertain: "通信が途切れ、操作結果を確認できません。操作を繰り返す前に状態を確認してください。",
      network: "接続できません。ネットワークを確認してから、もう一度お試しください。",
      stale_operation: "ログイン状態が変わりました。画面を確認してから操作してください。",
      passkey_unavailable: "この環境ではパスキーを使えません。IDとパスワードをご利用ください。",
      passkey_exists: "このパスキーは登録済みです。別のパスキーを使ってください。",
      passkey_limit: "パスキーは20件まで登録できます。不要なパスキーを削除してから追加してください。",
      passkey_not_found: "このパスキーは見つかりません。登録状態を再取得してください。",
      invalid_passkey: "パスキーを確認できませんでした。最初からやり直すか、パスワードをご利用ください。",
      invalid_passkey_name: "パスキーの名前は1〜80文字で入力してください。改行などの制御文字は使えません。",
      tenant_denied: "この会社を利用する権限を確認できません。ログイン状態を再取得するか、管理者にご確認ください。",
    };
    const message = messages[error.code];
    if (message) return message;
    if (error.status === 401) return "ログイン情報を確認してください。セッションが切れた場合は再度ログインしてください。";
    if (error.status === 403) return "この操作は許可されていません。管理者に確認してください。";
    if (error.status === 409) return "状態が変わりました。最新の状態を確認してから操作してください。";
    if (error.status === 429) return "試行回数が多いため一時的に制限されています。しばらく待ってからお試しください。";
    if (error.status === 422 || error.status === 400) return "入力内容を確認してください。パスキーの操作は最初からやり直してください。";
  }
  if (error instanceof Error && error.name === "InvalidStateError") return "このパスキーは登録済みです。別のパスキーを使ってください。";
  return "操作を完了できませんでした。最新の状態を確認してから、もう一度お試しください。";
}

export interface PasskeyClient {
  register: (options: PublicKeyCredentialCreationOptionsJSON) => Promise<RegistrationResponseJSON>;
  authenticate: (options: PublicKeyCredentialRequestOptionsJSON) => Promise<AuthenticationResponseJSON>;
  cancel: () => void;
}

const browserPasskeys: PasskeyClient = {
  register: (optionsJSON) => startRegistration({ optionsJSON }),
  authenticate: (optionsJSON) => startAuthentication({ optionsJSON }),
  cancel: () => WebAuthnAbortService.cancelCeremony(),
};

/** Memory-only state: cookie ownership and tenant membership stay authoritative on the server. */
export function createAuthController(dependencies: {
  fetch?: typeof fetch;
  passkeys?: PasskeyClient;
  notifyTabs?: (reason: AuthInvalidation) => void;
} = {}) {
  let state: AuthState = { kind: "loading", message: "ログイン状態を確認しています…" };
  let generation = 0;
  let ceremonyGeneration = 0;
  const listeners = new Set<() => void>();
  const activeRequests = new Set<AbortController>();
  const ceremonyRequests = new Set<AbortController>();
  const passkeys = dependencies.passkeys ?? browserPasskeys;
  const emit = (next: AuthState) => {
    state = next;
    listeners.forEach((listener) => listener());
  };
  const cancelPasskeys = () => {
    ceremonyGeneration += 1;
    for (const controller of ceremonyRequests) controller.abort();
    passkeys.cancel();
  };
  const invalidate = () => {
    generation += 1;
    for (const controller of activeRequests) controller.abort();
    cancelPasskeys();
    return generation;
  };
  const assertCurrent = (version: number) => {
    if (version !== generation) throw new AuthError("stale_operation");
  };
  const assertCeremony = (version: number | undefined) => {
    if (version !== undefined && version !== ceremonyGeneration) throw new AuthError("passkey_cancelled");
  };
  const signedOut = (notice: string) => {
    invalidate();
    emit({ kind: "ready", session: { enabled: true, authenticated: false }, notice });
  };
  const accept = (session: AuthSession, version: number, notice = "") => {
    assertCurrent(version);
    emit({ kind: "ready", session, notice });
  };

  async function request<T>(
    path: string, decoder: (body: unknown) => T,
    options: { method?: "POST" | "DELETE"; body?: unknown; protected?: boolean; ceremony?: number } = {},
  ): Promise<T> {
    const controller = new AbortController();
    const version = generation;
    activeRequests.add(controller);
    if (options.ceremony !== undefined) ceremonyRequests.add(controller);
    const timeout = globalThis.setTimeout(() => controller.abort(), 20_000);
    try {
      const response = await (dependencies.fetch ?? globalThis.fetch)(`/api/auth${path}`, {
        method: options.method ?? "GET", credentials: "same-origin", cache: "no-store",
        headers: {
          Accept: "application/json",
          ...(options.method ? { "Content-Type": "application/json", "X-Requested-With": "staff-matching" } : {}),
        },
        body: options.method ? JSON.stringify(options.body ?? {}) : undefined, signal: controller.signal,
      });
      assertCurrent(version);
      assertCeremony(options.ceremony);
      let body: unknown;
      try { body = await response.json(); } catch {
        if (response.status === 401 && options.protected) notifyFailure(401);
        throw new AuthError(options.method && response.ok ? "uncertain" : "invalid_response", response.status);
      }
      assertCurrent(version);
      assertCeremony(options.ceremony);
      if (!response.ok) {
        const code = record(body) && record(body.detail) && text(body.detail.code) ? body.detail.code : "http_error";
        if (options.protected && code !== "reauthentication_required") notifyFailure(response.status, code);
        throw new AuthError(code, response.status);
      }
      return decoder(body);
    } catch (error) {
      assertCurrent(version);
      assertCeremony(options.ceremony);
      if (error instanceof AuthError) throw error;
      throw new AuthError(options.method ? "uncertain" : "network");
    } finally {
      globalThis.clearTimeout(timeout);
      activeRequests.delete(controller);
      ceremonyRequests.delete(controller);
    }
  }

  async function refresh(notice = ""): Promise<void> {
    const version = invalidate();
    emit({ kind: "loading", message: "ログイン状態を確認しています…" });
    try {
      accept(await request("/session", decodeAuthSession), version, notice);
    } catch (error) {
      if (version === generation) emit({ kind: "error", message: authErrorMessage(error), recovery: "refresh" });
    }
  }

  function notifyFailure(status: number, code = ""): void {
    if (state.kind === "ready" && !state.session.enabled) return;
    if (status === 401) {
      signedOut("セッションが切れました。もう一度ログインしてください。");
      dependencies.notifyTabs?.("expired");
    } else if (status === 409 && /tenant/i.test(code)) {
      void refresh("会社の選択が変わりました。最新の会社で再表示しました。直前の操作は自動で再実行していません。");
    }
  }

  function checkAccount(): AccountSession {
    if (state.kind !== "ready" || !state.session.authenticated) throw new AuthError("stale_operation");
    return state.session;
  }

  const uncertain = (error: unknown, version: number, recovery: "refresh" | "logout" = "refresh") => {
    if (version === generation && error instanceof AuthError && (error.code === "uncertain" || error.code === "invalid_response")) {
      emit({ kind: "error", message: authErrorMessage(error), recovery });
    }
  };

  return {
    getSnapshot: () => state,
    subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    getTenantHeaders: (): Record<string, string> =>
      state.kind === "ready" && state.session.authenticated ? { "X-Tenant-ID": state.session.tenant.id } : {},
    refresh,
    notifyFailure,
    invalidateFromTab(reason: AuthInvalidation) {
      if (state.kind === "ready" && !state.session.enabled) return;
      if (reason === "logout" || reason === "expired") signedOut("別の画面でログアウトしたか、セッションが切れました。");
      else void refresh("別の画面でログイン状態が変わりました。最新の状態を表示しています。");
    },
    async login(username: string, password: string) {
      const version = invalidate();
      try {
        accept(await request("/login", account, { method: "POST", body: { username, password } }), version);
        dependencies.notifyTabs?.("session");
      } catch (error) { uncertain(error, version); throw error; }
    },
    async loginWithPasskey() {
      const version = invalidate();
      const ceremony = ceremonyGeneration;
      try {
        const options = await request("/passkeys/authentication/options", (body) => {
          if (!record(body) || !text(body.challenge)) return invalid();
          return body as unknown as PublicKeyCredentialRequestOptionsJSON;
        }, { method: "POST", ceremony });
        const credential = await passkeys.authenticate(options);
        assertCurrent(version);
        assertCeremony(ceremony);
        accept(await request("/passkeys/authentication/verify", account, { method: "POST", body: { credential }, ceremony }), version);
        dependencies.notifyTabs?.("session");
      } catch (error) { uncertain(error, version); throw error; }
    },
    async logout() {
      const version = invalidate();
      emit({ kind: "loading", message: "ログアウトしています…" });
      dependencies.notifyTabs?.("logout");
      try {
        await request("/logout", ok, { method: "POST" });
        accept({ enabled: true, authenticated: false }, version, "ログアウトしました。");
      } catch (error) {
        if (version === generation) emit({
          kind: "error", message: "業務画面を閉じましたが、サーバーでのログアウトは未確認です。接続を確認してログアウトを完了してください。",
          recovery: "logout",
        });
        throw error;
      } finally { dependencies.notifyTabs?.("logout"); }
    },
    async switchTenant(tenantId: string) {
      if (!checkAccount().tenants.some((item) => item.id === tenantId)) throw new AuthError("stale_operation");
      const version = invalidate();
      emit({ kind: "loading", message: "会社を切り替えています。前の会社の業務画面は閉じました…" });
      try {
        accept(await request("/tenant", account, { method: "POST", body: { tenant_id: tenantId }, protected: true }), version,
          "会社を切り替えました。");
        dependencies.notifyTabs?.("session");
      } catch (error) {
        if (version === generation) emit({ kind: "error", message: authErrorMessage(error), recovery: "refresh" });
        throw error;
      }
    },
    async changePassword(currentPassword: string, newPassword: string) {
      checkAccount();
      const version = generation;
      try {
        accept(await request("/password", account, {
          method: "POST", body: { current_password: currentPassword, new_password: newPassword }, protected: true,
        }), version);
        dependencies.notifyTabs?.("session");
      } catch (error) { uncertain(error, version); throw error; }
    },
    async reauthenticate(password: string) {
      checkAccount();
      await request("/reauth", ok, { method: "POST", body: { password }, protected: true });
    },
    async listPasskeys(): Promise<Passkey[]> {
      checkAccount();
      const version = generation;
      const items = await request("/passkeys", decodePasskeys, { protected: true });
      assertCurrent(version);
      accept({ ...checkAccount(), passkey_count: items.length }, version);
      return items;
    },
    async registerPasskey(name: string) {
      checkAccount();
      if (passkeyNameProblem(name)) throw new AuthError("invalid_passkey_name");
      const version = generation;
      cancelPasskeys();
      const ceremony = ceremonyGeneration;
      const options = await request("/passkeys/registration/options", (body) => {
        if (!record(body) || !text(body.challenge) || !record(body.user) ||
          !text(body.user.id) || !record(body.rp) || !text(body.rp.name) || !Array.isArray(body.pubKeyCredParams)) return invalid();
        return body as unknown as PublicKeyCredentialCreationOptionsJSON;
      }, { method: "POST", protected: true, ceremony });
      const credential = await passkeys.register(options);
      assertCurrent(version);
      assertCeremony(ceremony);
      await request("/passkeys/registration/verify", ok, { method: "POST", body: { credential, name }, protected: true, ceremony });
    },
    async removePasskey(id: string) {
      checkAccount();
      await request(`/passkeys/${encodeURIComponent(id)}`, ok, { method: "DELETE", protected: true });
    },
    cancelPasskey: cancelPasskeys,
  };
}

let channel: BroadcastChannel | undefined;
export const auth = createAuthController({
  notifyTabs: (reason) => channel?.postMessage({ type: "auth-invalidated", reason }),
});

/** Business requests send this expected tenant; it is not an authorization credential. */
export function getAuthTenantHeaders(): Record<string, string> { return auth.getTenantHeaders(); }

/** Call before surfacing a business 401 or tenant-specific 409. Never replays the failed request. */
export function notifyAuthFailure(status: number, code: string): void { auth.notifyFailure(status, code); }

export function connectAuthInvalidation(): () => void {
  if (typeof window === "undefined" || typeof BroadcastChannel === "undefined") return () => {};
  try {
    const connection = new BroadcastChannel("staff-matching-auth");
    channel = connection;
    connection.onmessage = (event: MessageEvent<unknown>) => {
      const message = event.data;
      if (record(message) && message.type === "auth-invalidated" &&
        (message.reason === "session" || message.reason === "logout" || message.reason === "expired")) {
        auth.invalidateFromTab(message.reason);
      }
    };
    return () => { connection.close(); if (channel === connection) channel = undefined; };
  } catch {
    // Private browsing may disable channels; API authorization still guards every request.
    return () => {};
  }
}
