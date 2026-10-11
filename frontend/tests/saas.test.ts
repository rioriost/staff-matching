import assert from "node:assert/strict";
import test from "node:test";
import { ApiError } from "../src/api.ts";
import { auth } from "../src/auth-api.ts";
import { CANDIDATE_PAGE_SIZE, saasApi, versioned } from "../src/saas-api.ts";
import { decode, pageSchema, schemas } from "../src/saas-models.ts";
import { account, appointment, candidate, introduction, job, overview, pageOf, person, personSummary, run } from "./saas-fixtures.test.ts";

test("SaaS DTOs strictly decode every supported entity, null history and overnight schedules", () => {
  assert.equal(decode(schemas.job, job).id, job.id);
  assert.equal(decode(schemas.personSummary, personSummary).id, person.id);
  assert.equal(decode(schemas.person, person).employment_history.length, 1);
  assert.equal(decode(schemas.candidate, { ...candidate, scores: { ...candidate.scores, history: null } }).scores?.history, null);
  assert.equal(decode(schemas.introduction, introduction).version, 1);
  assert.equal(decode(schemas.appointment, appointment).online, true);
  assert.equal(decode(schemas.matchRun, run).coverage.truncated, true);
  assert.equal(decode(schemas.overview, overview).jobs, 1000);
  assert.deepEqual(decode(schemas.schedule, { weekdays: [4], start_minute: 1320, end_minute: 1800, break_minutes: 60 }).weekdays, [4]);
  assert.equal(decode(pageSchema(schemas.job), pageOf([job], 1000, "next")).next_cursor, "next");
  assert.equal(decode(schemas.basemap, {
    type: "FeatureCollection", features: [], bbox: [139.7, 35.6, 139.9, 35.8],
    network_version: "public-v1", attribution: "© OpenStreetMap contributors",
    license: "ODbL-1.0", license_url: "https://opendatacommons.org/licenses/odbl/1-0/", approximate: true,
  }).approximate, true);
});
test("public basemap accepts the published bbox contract and still rejects invalid or extra fields", () => {
  const basemap = {
    type: "FeatureCollection", features: [], bbox: [139.7, 35.6, 139.9, 35.8],
    network_version: "public-v1", attribution: "© OpenStreetMap contributors",
    license: "ODbL-1.0", license_url: "https://opendatacommons.org/licenses/odbl/1-0/", approximate: true,
  };
  assert.deepEqual(decode(schemas.basemap, basemap).bbox, basemap.bbox);
  for (const bbox of [
    undefined, [139.7, 35.6, 139.9], ["139.7", 35.6, 139.9, 35.8],
    [-181, 35.6, 139.9, 35.8], [139.7, -91, 139.9, 35.8],
    [139.7, 35.6, 181, 35.8], [139.7, 35.6, 139.9, 91], [139.7, 35.8, 139.9, 35.6],
  ]) assert.throws(() => decode(schemas.basemap, { ...basemap, bbox }), /想定外/);
  assert.throws(() => decode(schemas.basemap, { ...basemap, private_location: {} }), /想定外/);
});
test("SaaS runtime contracts reject private/extra fields, malformed dates, bad versions and cross-owned sites", () => {
  for (const value of [
    { ...person, private_location: { lat: 35, lon: 139 } }, { ...person, contact_email: "private@example.invalid" },
    { ...person, availability: { ...person.availability, weekdays: [1, 1] } },
    { ...person, available_from: "2030-02-30" }, { ...person, version: 0 },
  ]) assert.throws(() => decode(schemas.person, value), /想定外/);
  assert.throws(() => decode(schemas.personSummary, person), /想定外/);
  assert.throws(() => decode(schemas.person, personSummary), /想定外/);
  assert.throws(() => decode(schemas.job, { ...job, site: { ...job.site, company_id: "other" } }), /想定外/);
  assert.throws(() => decode(schemas.job, { ...job, hourly_pay_max: 1 }), /想定外/);
  assert.throws(() => decode(schemas.job, { ...job, synthetic: false }), /想定外/);
  assert.throws(() => decode(schemas.appointment, { ...appointment, start_at: "2030-10-10T10:00:00" }), /想定外/);
  assert.throws(() => decode(schemas.appointment, { ...appointment, online: false }), /想定外/);
  assert.throws(() => decode(schemas.schedule, { weekdays: [0], start_minute: 540, end_minute: 500, break_minutes: 0 }), /想定外/);
  assert.throws(() => decode(pageSchema(schemas.job), [job]), /想定外/);
});
test("SaaS API sends expected tenant, cookies, CSRF header and original version without retries", async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const calls: { path: string; init?: RequestInit }[] = [];
  globalThis.fetch = async (input, init) => {
    const path = String(input); calls.push({ path, init });
    return Response.json(path === "/api/auth/session" ? account : introduction);
  };
  await auth.refresh();
  const body = { ...versioned(7), action: "company_request" as const, simulated: true as const };
  await saasApi.simulate("intro/with space", body);
  const request = calls.at(-1);
  assert.equal(request?.path, "/api/saas/introductions/intro%2Fwith%20space/simulate");
  assert.equal(request?.init?.credentials, "same-origin");
  assert.equal(request?.init?.method, "POST");
  const headers = new Headers(request?.init?.headers);
  assert.equal(headers.get("X-Tenant-ID"), "demo");
  assert.equal(headers.get("X-Requested-With"), "staff-matching");
  assert.deepEqual(JSON.parse(String(request?.init?.body)), body);
  assert.match(body.idempotency_key, /^[0-9a-f-]{36}$/);
  let attempts = 0;
  globalThis.fetch = async () => { attempts++; throw new TypeError("offline"); };
  await assert.rejects(saasApi.cancelIntroduction("intro-1", versioned(1)),
    (error: unknown) => error instanceof ApiError && error.code === "OUTCOME_UNKNOWN");
  assert.equal(attempts, 1);
});
test("SaaS API reports stale and invalid successful responses honestly and closes auth on 401", async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  globalThis.fetch = async () => Response.json({ detail: { code: "stale_version", message: "再取得してください。" } }, { status: 409 });
  await assert.rejects(saasApi.cancelIntroduction("intro-1", versioned(1)),
    (error: unknown) => error instanceof ApiError && error.status === 409 && error.code === "stale_version");
  globalThis.fetch = async () => Response.json({ anything: "not a valid response" });
  await assert.rejects(saasApi.overview(), /想定外/);
  await assert.rejects(saasApi.cancelIntroduction("intro-1", versioned(1)),
    (error: unknown) => error instanceof ApiError && error.code === "OUTCOME_UNKNOWN");
  globalThis.fetch = async () => Response.json({ detail: { code: "authentication_required", message: "ログインしてください。" } }, { status: 401 });
  await assert.rejects(saasApi.overview(), (error: unknown) => error instanceof ApiError && error.status === 401);
  const state = auth.getSnapshot();
  assert.ok(state.kind === "ready" && !state.session.authenticated);
});
test("SaaS tenant_changed refreshes authoritative auth but never replays a mutation", async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  let current = account;
  const calls: { path: string; method: string }[] = [];
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    calls.push({ path, method: init?.method ?? "GET" });
    if (path === "/api/auth/session") return Response.json(current);
    return Response.json({ detail: { code: "tenant_changed", message: "管理会社が変わりました。" } }, { status: 409 });
  };
  await auth.refresh();
  const tenant = { ...account.tenant, id: "demo02" };
  current = { ...account, tenant, tenants: [tenant] };
  await assert.rejects(saasApi.cancelIntroduction("intro-1", versioned(1)),
    (error: unknown) => error instanceof ApiError && error.code === "tenant_changed");
  await new Promise((resolve) => setTimeout(resolve, 0));
  const state = auth.getSnapshot();
  assert.ok(state.kind === "ready" && state.session.authenticated && state.session.tenant.id === "demo02");
  assert.equal(calls.filter((call) => call.method === "POST").length, 1);
  assert.equal(calls.filter((call) => call.path === "/api/auth/session").length, 2);
});
test("SaaS candidates request six items, preserve opaque cursors and reject oversized pages", async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const cursor = "candidate-cursor:6/+=";
  let requested = "";
  globalThis.fetch = async (input) => {
    requested = String(input);
    return Response.json(pageOf(Array.from({ length: CANDIDATE_PAGE_SIZE }, () => candidate), 50, cursor));
  };
  const page = await saasApi.candidates("run-1", "job-1", cursor);
  const url = new URL(requested, "https://example.invalid");
  assert.equal(url.searchParams.get("limit"), "6");
  assert.equal(url.searchParams.get("cursor"), cursor);
  assert.equal(page.items.length, 6);
  assert.equal(page.next_cursor, cursor);
  globalThis.fetch = async () => Response.json(pageOf(Array.from({ length: 50 }, () => candidate), 50));
  await assert.rejects(saasApi.candidates("run-1", "job-1"), /想定外/);
});
