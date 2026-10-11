import assert from "node:assert/strict";
import test from "node:test";
import {
  authErrorMessage, AuthError, createAuthController, decodeAuthSession, decodePasskeys,
  isPasskeyCancellation, passkeyNameProblem, passwordProblem,
} from "../src/auth-api.ts";
import type { AccountSession, AuthInvalidation, PasskeyClient } from "../src/auth-api.ts";
import type { AuthenticationResponseJSON, RegistrationResponseJSON } from "@simplewebauthn/browser";

const firstTenant = { id: "tenant-a", name: "管理会社A", role: "coordinator" };
const secondTenant = { id: "tenant-b", name: "管理会社B", role: "viewer" };
const session: AccountSession = {
  enabled: true, authenticated: true, user: { id: "user-a", username: "account-a", display_name: "担当者A" },
  tenants: [firstTenant, secondTenant], tenant: firstTenant, auth_method: "password", passkey_count: 0,
};
const signedOut = { enabled: true, authenticated: false };
const key = {
  id: "key/one", name: "仕事用端末", created_at: "2026-10-05T12:00:00Z", last_used_at: null,
  backed_up: false, device_type: "singleDevice",
};
const registration: RegistrationResponseJSON = {
  id: "credential-one", rawId: "credential-one", type: "public-key", clientExtensionResults: {},
  response: { clientDataJSON: "client", attestationObject: "attestation" },
};
const authentication: AuthenticationResponseJSON = {
  id: "credential-one", rawId: "credential-one", type: "public-key", clientExtensionResults: {},
  response: { clientDataJSON: "client", signature: "signature", authenticatorData: "authenticator" },
};
const options = {
  rp: { id: "staff.example.test", name: "Staff matching" }, user: { id: "userid", name: "account-a", displayName: "担当者A" },
  challenge: "challenge-one", pubKeyCredParams: [{ type: "public-key", alg: -7 }],
};
const errorResponse = (code: string, status: number) =>
  Response.json({ detail: { code, message: "Do not reflect untrusted credentials in UI" } }, { status });

type Call = { path: string; init?: RequestInit };
function harness(
  handle: (path: string, init?: RequestInit) => Response | Promise<Response>,
  passkeyOverrides: Partial<PasskeyClient> = {},
) {
  const calls: Call[] = [];
  const broadcasts: AuthInvalidation[] = [];
  const cancelled: number[] = [];
  const controller = createAuthController({
    fetch: async (input, init) => {
      const path = String(input);
      calls.push({ path, init });
      return handle(path, init);
    },
    passkeys: {
      register: async () => registration, authenticate: async () => authentication,
      cancel: () => { cancelled.push(1); }, ...passkeyOverrides,
    },
    notifyTabs: (reason) => { broadcasts.push(reason); },
  });
  return { controller, calls, broadcasts, cancelled };
}

test("auth DTO decoding fails closed for partial, forged tenant, or contradictory sessions", () => {
  assert.deepEqual(decodeAuthSession(session), session);
  assert.deepEqual(decodeAuthSession({ enabled: false, authenticated: false }), { enabled: false, authenticated: false });
  for (const value of [
    {}, { enabled: true }, { ...session, enabled: false }, { ...session, tenants: [] },
    { ...session, tenant: { ...firstTenant, role: "admin" } },
    { ...session, tenant: { ...firstTenant, id: "unauthorized" } },
    { ...session, tenants: [firstTenant, firstTenant] }, { ...session, auth_method: "biometric" },
    { ...session, passkey_count: -1 }, { ...session, passkey_count: 1.5 },
    { ...session, user: { ...session.user, id: "" } },
  ]) assert.throws(() => decodeAuthSession(value), AuthError);
});

test("passkey metadata decoding keeps unknown use and backup distinctions", () => {
  assert.deepEqual(decodePasskeys({ items: [key] }), [key]);
  assert.deepEqual(decodePasskeys({ items: [{ ...key, device_type: "single_device" }] }), [key]);
  assert.equal(decodePasskeys({ items: [{ ...key, device_type: "multi_device" }] })[0]?.device_type, "multiDevice");
  for (const item of [
    { ...key, backed_up: "true" }, { ...key, device_type: "FaceID" },
    { ...key, created_at: "2026-10-05T12:00:00" }, { ...key, last_used_at: "not-a-date" },
  ]) assert.throws(() => decodePasskeys({ items: [item] }), AuthError);
  assert.throws(() => decodePasskeys({ items: [key, key] }), AuthError);
});

test("password policy counts Unicode characters, does not trim, and verifies confirmation", () => {
  assert.ok(passwordProblem("short", "short"));
  assert.equal(passwordProblem(" 1234567890123 ", " 1234567890123 "), null);
  assert.equal(passwordProblem("長".repeat(15), "長".repeat(15)), null);
  assert.equal(passwordProblem("🔑".repeat(128), "🔑".repeat(128)), null);
  assert.ok(passwordProblem("x".repeat(129), "x".repeat(129)));
  assert.ok(passwordProblem("a".repeat(15), "b".repeat(15)));
});

test("passkey name policy matches the backend Unicode limit and forbids control characters without trimming", () => {
  assert.equal(passkeyNameProblem(" 仕事用端末 "), null);
  assert.equal(passkeyNameProblem("🔑".repeat(80)), null);
  assert.ok(passkeyNameProblem("🔑".repeat(81)));
  for (const name of ["", "name\n", "name\u0000", "name\u001f", "name\u007f"]) assert.ok(passkeyNameProblem(name));
});

test("legacy session never acquires account controls or expected-tenant headers on Basic failures", async () => {
  const { controller } = harness(() => Response.json({ enabled: false, authenticated: false }));
  await controller.refresh();
  controller.notifyFailure(401);
  controller.invalidateFromTab("logout");
  assert.deepEqual(controller.getSnapshot(), {
    kind: "ready", session: { enabled: false, authenticated: false }, notice: "",
  });
  assert.deepEqual(controller.getTenantHeaders(), {});
});

test("login preserves password whitespace and unsafe requests carry origin-defense header with same-origin cookies", async () => {
  const { controller, calls, broadcasts } = harness(() => Response.json(session));
  await controller.login("account-a", "  not-trimmed-secret  ");
  const call = calls[0]!;
  assert.equal(call.path, "/api/auth/login");
  assert.equal(call.init?.method, "POST");
  assert.equal(call.init?.credentials, "same-origin");
  assert.equal(call.init?.cache, "no-store");
  assert.equal(new Headers(call.init?.headers).get("X-Requested-With"), "staff-matching");
  assert.equal(new Headers(call.init?.headers).has("Authorization"), false);
  assert.deepEqual(JSON.parse(String(call.init?.body)), { username: "account-a", password: "  not-trimmed-secret  " });
  assert.deepEqual(controller.getTenantHeaders(), { "X-Tenant-ID": "tenant-a" });
  assert.deepEqual(broadcasts, ["session"]);
});

test("expected-tenant headers are fresh copies, never session tokens", async () => {
  const { controller } = harness(() => Response.json(session));
  await controller.refresh();
  const headers = controller.getTenantHeaders();
  headers["X-Tenant-ID"] = "forged";
  assert.deepEqual(controller.getTenantHeaders(), { "X-Tenant-ID": "tenant-a" });
});

test("session discovery errors fail closed and only explicit reads recover", async () => {
  let failed = true;
  const { controller, calls } = harness(() => failed ? new Response("<html>offline</html>", { status: 502 }) : Response.json(session));
  await controller.refresh();
  assert.equal(controller.getSnapshot().kind, "error");
  assert.deepEqual(controller.getTenantHeaders(), {});
  assert.equal(calls.length, 1);
  failed = false;
  await controller.refresh();
  assert.equal(controller.getSnapshot().kind, "ready");
  assert.equal(calls.length, 2);
});

test("401 invalidates authenticated workspace and cancels pending WebAuthn without fetching business data", async () => {
  const { controller, broadcasts, calls, cancelled } = harness(() => Response.json(session));
  await controller.refresh();
  controller.notifyFailure(401);
  const state = controller.getSnapshot();
  assert.equal(state.kind, "ready");
  if (state.kind === "ready") assert.equal(state.session.authenticated, false);
  assert.deepEqual(controller.getTenantHeaders(), {});
  assert.ok(cancelled.length > 0);
  assert.deepEqual(broadcasts, ["expired"]);
  assert.equal(calls.length, 1);
});

test("tenant selection removes previous workspace synchronously and waits for authoritative response", async () => {
  let finish!: (response: Response) => void;
  const { controller, calls } = harness((path) => path.endsWith("/tenant")
    ? new Promise((resolve) => { finish = resolve; }) : Response.json(session));
  await controller.refresh();
  const switching = controller.switchTenant("tenant-b");
  assert.equal(controller.getSnapshot().kind, "loading");
  assert.deepEqual(controller.getTenantHeaders(), {});
  assert.deepEqual(JSON.parse(String(calls[1]?.init?.body)), { tenant_id: "tenant-b" });
  finish(Response.json({ ...session, tenant: secondTenant }));
  await switching;
  assert.deepEqual(controller.getTenantHeaders(), { "X-Tenant-ID": "tenant-b" });
});

test("tenant conflict triggers session refresh, not replay; ordinary business conflict does not close workspace", async () => {
  let finish!: (response: Response) => void;
  let reads = 0;
  const { controller, calls } = harness(() => ++reads === 1 ? Response.json(session) : new Promise((resolve) => { finish = resolve; }));
  await controller.refresh();
  controller.notifyFailure(409, "proposal_version_conflict");
  assert.equal(controller.getSnapshot().kind, "ready");
  controller.notifyFailure(409, "tenant_changed");
  assert.equal(controller.getSnapshot().kind, "loading");
  assert.deepEqual(controller.getTenantHeaders(), {});
  const ready = new Promise<void>((resolve) => {
    const stop = controller.subscribe(() => { if (controller.getSnapshot().kind === "ready") { stop(); resolve(); } });
  });
  finish(Response.json({ ...session, tenant: secondTenant }));
  await ready;
  assert.deepEqual(calls.map((call) => call.path), ["/api/auth/session", "/api/auth/session"]);
  assert.deepEqual(controller.getTenantHeaders(), { "X-Tenant-ID": "tenant-b" });
});

test("uncertain tenant mutation is not automatically retried or restored", async () => {
  const { controller, calls } = harness((path) => {
    if (path.endsWith("/tenant")) throw new TypeError("network lost after commit");
    return Response.json(session);
  });
  await controller.refresh();
  await assert.rejects(controller.switchTenant("tenant-b"));
  assert.equal(controller.getSnapshot().kind, "error");
  assert.deepEqual(controller.getTenantHeaders(), {});
  assert.equal(calls.length, 2);
});

test("late session responses cannot reopen the workspace after logout", async () => {
  let finish!: (response: Response) => void;
  const { controller } = harness((path) => path.endsWith("/session")
    ? new Promise((resolve) => { finish = resolve; }) : Response.json({ ok: true }));
  const checking = controller.refresh();
  await controller.logout();
  finish(Response.json(session));
  await checking;
  const state = controller.getSnapshot();
  assert.equal(state.kind, "ready");
  if (state.kind === "ready") assert.equal(state.session.authenticated, false);
  assert.deepEqual(controller.getTenantHeaders(), {});
});

test("uncertain logout keeps workspace closed and offers only explicit logout completion", async () => {
  const { controller, broadcasts } = harness((path) => {
    if (path.endsWith("/logout")) throw new TypeError("offline");
    return Response.json(session);
  });
  await controller.refresh();
  await assert.rejects(controller.logout());
  const state = controller.getSnapshot();
  assert.equal(state.kind, "error");
  if (state.kind === "error") assert.equal(state.recovery, "logout");
  assert.deepEqual(controller.getTenantHeaders(), {});
  assert.deepEqual(broadcasts, ["logout", "logout"]);
});

test("wrong password errors and authenticator cancellation never authenticate", async () => {
  const cancelled = new Error("credential details must not be shown");
  cancelled.name = "NotAllowedError";
  const { controller, calls } = harness((path) => {
    if (path.endsWith("/session")) return Response.json(signedOut);
    if (path.endsWith("/login")) return errorResponse("invalid_credentials", 401);
    return Response.json({ challenge: "test-challenge" });
  }, { authenticate: async () => { throw cancelled; } });
  await controller.refresh();
  await assert.rejects(controller.login("account-a", "wrong"));
  await assert.rejects(controller.loginWithPasskey(), (error: unknown) => isPasskeyCancellation(error));
  const state = controller.getSnapshot();
  assert.equal(state.kind, "ready");
  if (state.kind === "ready") assert.equal(state.session.authenticated, false);
  assert.equal(calls.some((call) => call.path.endsWith("/verify")), false);
  assert.match(authErrorMessage(cancelled), /完了しませんでした/);
  assert.doesNotMatch(authErrorMessage(cancelled), /credential details/);
});

test("passkey authentication receives direct JSON options and accepts only server-verified session", async () => {
  let received: unknown;
  const { controller, calls } = harness((path) => path.endsWith("/options")
    ? Response.json({ challenge: "test-challenge", rpId: "staff.example.test", userVerification: "required" })
    : Response.json({ ...session, auth_method: "passkey" }),
  { authenticate: async (input) => { received = input; return authentication; } });
  await controller.loginWithPasskey();
  assert.deepEqual(received, { challenge: "test-challenge", rpId: "staff.example.test", userVerification: "required" });
  assert.deepEqual(JSON.parse(String(calls[1]?.init?.body)), { credential: authentication });
  const state = controller.getSnapshot();
  assert.equal(state.kind, "ready");
  if (state.kind === "ready" && state.session.authenticated) assert.equal(state.session.auth_method, "passkey");
});

test("logout during authenticator prompt discards a late signed response without verifying it", async () => {
  let finish!: (value: AuthenticationResponseJSON) => void;
  let prompted!: () => void;
  const promptStarted = new Promise<void>((resolve) => { prompted = resolve; });
  const { controller, calls } = harness((path) => path.endsWith("/options")
    ? Response.json({ challenge: "test-challenge" }) : Response.json({ ok: true }),
  { authenticate: () => { prompted(); return new Promise((resolve) => { finish = resolve; }); } });
  const loggingIn = controller.loginWithPasskey();
  await promptStarted;
  await controller.logout();
  finish(authentication);
  await assert.rejects(loggingIn, (error: unknown) => error instanceof AuthError && error.code === "stale_operation");
  assert.equal(calls.some((call) => call.path.endsWith("/verify")), false);
  assert.deepEqual(controller.getTenantHeaders(), {});
});

test("cancel before options arrive never opens a late authenticator prompt", async () => {
  let finish!: (response: Response) => void;
  let prompts = 0;
  const { controller, calls } = harness((path) => path.endsWith("/session")
    ? Response.json(signedOut) : new Promise((resolve) => { finish = resolve; }),
  { authenticate: async () => { prompts += 1; return authentication; } });
  await controller.refresh();
  const loggingIn = controller.loginWithPasskey();
  controller.cancelPasskey();
  finish(Response.json({ challenge: "too-late" }));
  await assert.rejects(loggingIn, (error: unknown) => isPasskeyCancellation(error));
  assert.equal(prompts, 0);
  assert.equal(calls.length, 2);
  const state = controller.getSnapshot();
  assert.equal(state.kind, "ready");
  if (state.kind === "ready") assert.equal(state.session.authenticated, false);
});

test("unmount cleanup of a passkey screen cannot cancel authoritative tenant refresh", async () => {
  let finish!: (response: Response) => void;
  const { controller } = harness(() => new Promise((resolve) => { finish = resolve; }));
  const checking = controller.refresh();
  controller.cancelPasskey();
  finish(Response.json(session));
  await checking;
  assert.deepEqual(controller.getTenantHeaders(), { "X-Tenant-ID": "tenant-a" });
});

test("reauthentication preserves password and explicit registration restart uses new options and a new signed response", async () => {
  let optionCount = 0;
  let verifyCount = 0;
  const ceremonies: string[] = [];
  const { controller, calls } = harness((path) => {
    if (path.endsWith("/session")) return Response.json(session);
    if (path.endsWith("/options")) return Response.json({ ...options, challenge: `challenge-${++optionCount}` });
    if (path.endsWith("/verify")) return ++verifyCount === 1 ? errorResponse("reauthentication_required", 403) : Response.json({ ok: true });
    return Response.json({ ok: true });
  }, { register: async (input) => {
    ceremonies.push(input.challenge);
    return { ...registration, response: { ...registration.response, clientDataJSON: input.challenge } };
  } });
  await controller.refresh();
  await assert.rejects(controller.registerPasskey("仕事用端末"),
    (error: unknown) => error instanceof AuthError && error.code === "reauthentication_required");
  assert.equal(optionCount, 1);
  assert.equal(controller.getSnapshot().kind, "ready");
  await controller.reauthenticate(" preserved whitespace ");
  assert.deepEqual(JSON.parse(String(calls.find((call) => call.path.endsWith("/reauth"))?.init?.body)), { password: " preserved whitespace " });
  await controller.registerPasskey(" 仕事用端末 ");
  assert.deepEqual(ceremonies, ["challenge-1", "challenge-2"]);
  const verificationBodies = calls.filter((call) => call.path.endsWith("/verify")).map((call) => JSON.parse(String(call.init?.body)));
  assert.notEqual(verificationBodies[0].credential.response.clientDataJSON, verificationBodies[1].credential.response.clientDataJSON);
  assert.equal(verificationBodies[1].name, " 仕事用端末 ");
});

test("invalid passkey names fail before requesting registration options", async () => {
  const { controller, calls } = harness(() => Response.json(session));
  await controller.refresh();
  await assert.rejects(controller.registerPasskey("a".repeat(81)),
    (error: unknown) => error instanceof AuthError && error.code === "invalid_passkey_name");
  assert.equal(calls.length, 1);
});

test("passkey removal encodes IDs, uses DELETE CSRF defenses, and permits removal of the last key", async () => {
  const { controller, calls } = harness((path) => {
    if (path.endsWith("/session")) return Response.json(session);
    if (path.endsWith("/passkeys")) return Response.json({ items: [key] });
    return Response.json({ ok: true });
  });
  await controller.refresh();
  await controller.listPasskeys();
  await controller.removePasskey(key.id);
  const deletion = calls[2]!;
  assert.equal(deletion.path, "/api/auth/passkeys/key%2Fone");
  assert.equal(deletion.init?.method, "DELETE");
  assert.equal(new Headers(deletion.init?.headers).get("X-Requested-With"), "staff-matching");
  assert.deepEqual(JSON.parse(String(deletion.init?.body)), {});
});

test("cross-tab logout only invalidates; it never restores cached personal or tenant data", async () => {
  const { controller, calls, broadcasts } = harness(() => Response.json(session));
  await controller.refresh();
  controller.invalidateFromTab("logout");
  assert.equal(calls.length, 1);
  assert.deepEqual(broadcasts, []);
  const state = controller.getSnapshot();
  assert.equal(state.kind, "ready");
  if (state.kind === "ready") assert.deepEqual(state.session, signedOut);
  assert.deepEqual(controller.getTenantHeaders(), {});
});

test("HTML 401 responses from protected endpoints also close workspace", async () => {
  const { controller } = harness((path) => path.endsWith("/session")
    ? Response.json(session) : new Response("<html>session expired</html>", { status: 401 }));
  await controller.refresh();
  await assert.rejects(controller.listPasskeys());
  const state = controller.getSnapshot();
  assert.equal(state.kind, "ready");
  if (state.kind === "ready") assert.equal(state.session.authenticated, false);
  assert.deepEqual(controller.getTenantHeaders(), {});
});

test("password change retains exact values, takes rotated session from server, and invalidates tabs", async () => {
  const { controller, calls, broadcasts } = harness(() => Response.json(session));
  await controller.refresh();
  await controller.changePassword(" old password ", " new password longer ");
  assert.deepEqual(JSON.parse(String(calls[1]?.init?.body)), {
    current_password: " old password ", new_password: " new password longer ",
  });
  assert.deepEqual(broadcasts, ["session"]);
});

test("error messages do not print raw server exceptions, passwords, or WebAuthn responses", () => {
  assert.doesNotMatch(authErrorMessage(new Error("secret-response")), /secret-response/);
  assert.doesNotMatch(authErrorMessage(new AuthError("secret-credential-dump", 500)), /secret-credential-dump/);
  assert.match(authErrorMessage(new AuthError("rate_limited", 429)), /制限/);
});
