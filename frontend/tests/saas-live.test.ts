import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdir, rm } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import type { APIRequestContext, APIResponse, Browser, BrowserContext, BrowserContextOptions, Locator, Page, Response } from "playwright";
import type { z } from "zod";
import { decodeAuthSession } from "../src/auth-api.ts";
import { CANDIDATE_PAGE_SIZE } from "../src/saas-api.ts";
import { pageSchema, schemas } from "../src/saas-models.ts";
import type { Appointment, Candidate, Introduction, Job, MatchRun, Notification } from "../src/saas-models.ts";
import { assertFocused, assertTouchEnvironment, assertVisualAccessibility, browserEngine, launchOptions, liveEnvironment, reportBrowser, selectBrowser, tabletOptions } from "./browser-support.ts";

const browserName = selectBrowser();
const coordinator = "coordinator08";
const tenant = "demo08";
const maxJobAttempts = 3;
const runWaitMs = 90_000;
const noticeWaitMs = 45_000;
const requestTimeoutMs = 20_000;
const privatePersonFields = new Set([
  "private_location", "private_address", "contact_email", "home_address", "home_location",
  "exact_location", "email", "phone", "telephone",
]);

function verifyPublicPersonPayload(value: unknown): void {
  if (Array.isArray(value)) {
    value.forEach(verifyPublicPersonPayload);
  } else if (value !== null && typeof value === "object") {
    for (const [key, child] of Object.entries(value)) {
      assert.ok(!privatePersonFields.has(key), "Private person field returned.");
      verifyPublicPersonPayload(child);
    }
  }
}
function isPersonPayload(path: string) {
  return path === "/api/saas/people" || path.startsWith("/api/saas/people/") || path.endsWith("/candidates");
}
function escapeRegex(value: string) { return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
function tokyoInput(value: Date) { return new Date(value.getTime() + 9 * 3_600_000).toISOString().slice(0, 16); }
function displayTime(value: string) {
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(new Date(value));
}
function activate(locator: Locator) {
  return browserName === "webkit" ? locator.tap() : locator.click();
}
async function check(locator: Locator) {
  if (!await locator.isChecked()) await activate(locator);
  assert.ok(await locator.isChecked());
}
const sleep = (milliseconds: number) => new Promise<void>((done) => setTimeout(done, milliseconds));

// Real Azure only, after the SaaS release. No credential/key changes, tracing, HAR, or request-body logs.
// At most three single-job matching runs. Successful synthetic introductions/interviews remain as evidence.
test("live Azure SaaS browser acceptance", {
  skip: process.env.STAFF_SAAS_LIVE_TESTS !== "1", timeout: 900_000,
}, async () => {
  const { subscription, group, job, origin } = liveEnvironment();
  const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
  const artifacts = resolve(root, ".azure/saas-browser-evidence");
  const acceptanceId = `live-${browserName === "webkit" ? "webkit-" : ""}${new Date().toISOString().replace(/[:.]/g, "-")}-${randomUUID().slice(0, 8)}`;
  const evidence = resolve(artifacts, acceptanceId);
  const runtime = resolve(evidence, ".browser-runtime");
  const previousTmpDir = process.env.TMPDIR;
  const csrf = { Origin: origin, "X-Requested-With": "staff-matching" };
  const initialViewport = browserName === "webkit" ? { width: 1024, height: 768 } : { width: 1024, height: 900 };
  const contextOptions: BrowserContextOptions = {
    baseURL: origin, viewport: initialViewport, colorScheme: "light", reducedMotion: "reduce", acceptDownloads: false,
    ...(browserName === "webkit" ? tabletOptions(initialViewport) : {}),
  };
  const results: { path: string; status: number }[] = [];
  let stage = "prepare";
  let failedStage = "";
  let cleanupFailedStage = "";
  let completed = false;
  let browser: Browser | undefined;
  let context: BrowserContext | undefined;
  let viewerContext: BrowserContext | undefined;
  let coordinatorPassword = "";
  let ownedIntro: Introduction | undefined;
  let ownedAppointment: Appointment | undefined;
  let creationUnconfirmed = false;
  let privacyFailed = false;
  const privacyChecks: Promise<void>[] = [];

  function record(response: APIResponse | Response) {
    const path = new URL(response.url()).pathname;
    if (path.startsWith("/api/")) {
      results.push({ path, status: response.status() });
      if (results.length > 120) results.shift();
    }
  }
  function capturePassword(username: string) {
    try {
      const value = execFileSync("uv", [
        "run", "python", "-m", "scripts.poc_credentials",
        "--subscription", subscription,
        "--resource-group", group, "--job", job,
        "--username", username,
      ], { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 90_000 }).trim();
      assert.ok(value.length >= 15);
      return value;
    } catch {
      // execFile errors can carry captured stdout/stderr; never propagate them to the test reporter.
      throw new Error("Credential capture failed; values omitted.");
    }
  }
  function observe(page: Page) {
    page.on("response", (response) => {
      if (new URL(response.url()).origin !== origin) return;
      record(response);
      const path = new URL(response.url()).pathname;
      if (response.ok() && isPersonPayload(path)) {
        privacyChecks.push(response.json().then((value: unknown) => {
          verifyPublicPersonPayload(value);
        }).catch(() => { privacyFailed = true; }));
      }
    });
  }
  function decodeResponse<T>(schema: z.ZodType<T>, value: unknown, path: string): T {
    const result = schema.safeParse(value);
    if (result.success) return result.data;
    stage = `${stage}:unexpected-response:${new URL(path, origin).pathname}`;
    throw new Error("Unexpected response contract; values omitted.");
  }
  async function get<T>(request: APIRequestContext, path: string, schema: z.ZodType<T>, selectedTenant = tenant): Promise<T> {
    const response = await request.get(path, { headers: { "X-Tenant-ID": selectedTenant }, timeout: requestTimeoutMs });
    record(response);
    assert.equal(response.status(), 200);
    const value: unknown = await response.json();
    if (isPersonPayload(new URL(path, origin).pathname)) verifyPublicPersonPayload(value);
    return decodeResponse(schema, value, path);
  }
  async function login(current: BrowserContext, username: string, password: string, expectedTenant: string) {
    const page = await current.newPage();
    page.setDefaultTimeout(30_000);
    observe(page);
    if (browserName === "webkit") {
      await page.addInitScript(() => {
        let touches = 0;
        document.addEventListener("touchstart", () => {
          document.documentElement.dataset.acceptanceTouches = String(++touches);
        }, { passive: true });
      });
    }
    await page.goto(origin, { waitUntil: "domcontentloaded", timeout: 45_000 });
    await page.getByLabel("ログインID", { exact: true }).fill(username);
    await page.getByLabel("パスワード", { exact: true }).fill(password);
    await activate(page.getByRole("button", { name: "IDとパスワードでログイン", exact: true }));
    await page.getByRole("banner", { name: "会社とアカウント" }).waitFor();
    const response = await current.request.get("/api/auth/session", { timeout: requestTimeoutMs });
    record(response);
    assert.equal(response.status(), 200);
    const session = decodeAuthSession(await response.json());
    assert.ok(session.authenticated);
    assert.equal(session.user.username, username);
    assert.equal(session.tenant.id, expectedTenant);
    assert.equal(session.auth_method, "password");
    await page.getByRole("heading", { name: "求人ワークスペース", exact: true }).waitFor();
    return { page, session };
  }
  async function navigate(page: Page, label: string) {
    await activate(page.getByRole("navigation", { name: "業務メニュー" })
      .getByRole("button", { name: new RegExp(`^${escapeRegex(label)}`) }));
  }
  async function mutation<T>(
    page: Page, path: string, status: number, schema: z.ZodType<T>, click: () => Promise<unknown>,
    receive: (value: T) => void = () => {},
  ): Promise<T> {
    const responsePromise = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.origin === origin && url.pathname === path && response.request().method() === "POST";
    }, { timeout: 30_000 }).catch(() => undefined);
    let interactionFailed = false;
    try { await click(); } catch { interactionFailed = true; }
    // Still capture an acknowledged creation if the click itself timed out after sending it.
    const response = await responsePromise;
    assert.ok(response, "Mutation outcome is unconfirmed.");
    if (response.status() >= 400 && response.status() < 500) creationUnconfirmed = false;
    assert.equal(response.status(), status);
    const result = decodeResponse(schema, await response.json(), path);
    receive(result);
    assert.ok(!interactionFailed, "UI interaction did not complete.");
    return result;
  }
  async function waitForRun(request: APIRequestContext, run: MatchRun) {
    const deadline = Date.now() + runWaitMs;
    let latest = run;
    while (Date.now() < deadline) {
      latest = await get(request, `/api/saas/matching/runs/${encodeURIComponent(run.id)}`, schemas.matchRun);
      if (latest.status === "completed") return latest;
      assert.ok(latest.status === "queued" || latest.status === "running", "Matching did not complete.");
      await sleep(2_000);
    }
    throw new Error("Matching deadline reached.");
  }
  async function waitForNotice(request: APIRequestContext, introId: string, audience: Notification["audience"], kind: string) {
    const deadline = Date.now() + noticeWaitMs;
    while (Date.now() < deadline) {
      const notices = await get(request, `/api/saas/notifications?audience=${audience}&limit=50`, pageSchema(schemas.notification));
      const notice = notices.items.find((item) => item.introduction_id === introId && item.kind === kind);
      if (notice) return notice;
      await sleep(2_000);
    }
    throw new Error("Notification deadline reached.");
  }
  async function findOwned<T extends { id: string }>(path: string, schema: z.ZodType<T>, id: string): Promise<T> {
    assert.ok(context);
    let cursor: string | null = null;
    for (let page = 0; page < 5; page++) {
      const params = new URLSearchParams({ limit: "100" });
      if (cursor) params.set("cursor", cursor);
      const result = await get(context.request, `${path}?${params}`, pageSchema(schema));
      const own = result.items.find((item) => item.id === id);
      if (own) return own;
      cursor = result.next_cursor;
      if (!cursor) break;
    }
    throw new Error("Owned acceptance record could not be located safely.");
  }
  async function cleanup() {
    if (completed || (!ownedIntro && !ownedAppointment)) return;
    assert.ok(context);
    // Reauthenticate only this isolated context, then use fresh server versions. No mutation retries.
    stage = "cleanup-authentication";
    const authenticated = await context.request.post("/api/auth/login", {
      headers: csrf, data: { username: coordinator, password: coordinatorPassword }, timeout: requestTimeoutMs,
    });
    record(authenticated);
    assert.equal(authenticated.status(), 200);
    const session = decodeAuthSession(await authenticated.json());
    assert.ok(session.authenticated && session.tenant.id === tenant);
    if (ownedAppointment) {
      stage = "cleanup-owned-appointment";
      const latest = await findOwned("/api/saas/appointments", schemas.appointment, ownedAppointment.id);
      assert.equal(latest.introduction_id, ownedIntro?.id);
      if (latest.status !== "cancelled") {
        const response = await context.request.post(`/api/saas/appointments/${encodeURIComponent(latest.id)}/cancel`, {
          headers: { ...csrf, "X-Tenant-ID": tenant },
          data: { expected_version: latest.version, idempotency_key: randomUUID() }, timeout: requestTimeoutMs,
        });
        record(response);
        assert.equal(response.status(), 200);
        assert.equal(decodeResponse(schemas.appointment, await response.json(), response.url()).status, "cancelled");
      }
    }
    if (ownedIntro) {
      stage = "cleanup-owned-introduction";
      const latest = await findOwned("/api/saas/introductions", schemas.introduction, ownedIntro.id);
      assert.equal(latest.job_id, ownedIntro.job_id);
      assert.equal(latest.person_id, ownedIntro.person_id);
      if (!["cancelled", "declined", "expired", "placed"].includes(latest.status)) {
        const response = await context.request.post(`/api/saas/introductions/${encodeURIComponent(latest.id)}/cancel`, {
          headers: { ...csrf, "X-Tenant-ID": tenant },
          data: { expected_version: latest.version, idempotency_key: randomUUID() }, timeout: requestTimeoutMs,
        });
        record(response);
        assert.equal(response.status(), 200);
        assert.equal(decodeResponse(schemas.introduction, await response.json(), response.url()).status, "cancelled");
      }
    }
  }

  try {
    await mkdir(runtime, { recursive: true });
    process.env.TMPDIR = runtime;
    stage = "browser-launch";
    browser = await browserEngine(browserName).launch({ ...launchOptions(browserName, runtime), timeout: 30_000 });
    reportBrowser(browser, browserName);
    stage = "coordinator-credential-capture";
    coordinatorPassword = capturePassword(coordinator);
    stage = "coordinator-login";
    context = await browser.newContext(contextOptions);
    const { page } = await login(context, coordinator, coordinatorPassword, tenant);
    if (browserName === "webkit") await assertTouchEnvironment(page);

    stage = "dataset-overview";
    const overview = await get(context.request, "/api/saas/overview", schemas.overview);
    assert.equal(overview.jobs, 1000);
    assert.equal(overview.people, 1000);
    assert.equal(overview.tenant_id, tenant);
    assert.equal(overview.can_simulate, true);
    stage = "dataset-jobs-page";
    const jobs = await get(context.request, "/api/saas/jobs?limit=50", pageSchema(schemas.job));
    assert.equal(jobs.total, 1000);
    assert.ok(jobs.items.length > 0 && jobs.items.length <= 50 && jobs.next_cursor);
    stage = "dataset-people-page";
    const people = await get(context.request, "/api/saas/people?limit=50", pageSchema(schemas.personSummary));
    assert.equal(people.total, 1000);
    assert.ok(people.items.length > 0 && people.items.length <= 50 && people.next_cursor);
    const person = people.items[0];
    assert.ok(person);
    stage = "person-detail-contract";
    await get(context.request, `/api/saas/people/${encodeURIComponent(person.id)}`, schemas.person);
    stage = "person-detail-ui";
    await navigate(page, "人材");
    await activate(page.getByRole("button", { name: new RegExp(escapeRegex(person.name)) }));
    await page.getByRole("heading", { name: person.name, exact: true }).waitFor();
    await page.getByRole("heading", { name: "職歴と記録された評価", exact: true }).waitFor();
    stage = "person-detail-screenshot";
    await page.screenshot({ path: resolve(evidence, "people-1024-light.png"), fullPage: false });
    stage = "jobs-workspace-ui";
    await navigate(page, "求人");
    await page.screenshot({ path: resolve(evidence, "workspace-1024-light.png"), fullPage: false });
    stage = "public-basemap-contract";
    await get(context.request, "/api/saas/geo/basemap", schemas.basemap);
    stage = "public-geography-manifest";
    const manifest = await context.request.get("/api/saas/geo/manifest", { timeout: requestTimeoutMs });
    record(manifest);
    assert.equal(manifest.status(), 200);

    stage = "placement-search";
    await page.getByLabel("募集区分", { exact: true }).selectOption("placement");
    await activate(page.getByRole("button", { name: "求人を検索", exact: true }));
    await page.getByRole("group", { name: "適用中の求人検索条件", exact: true })
      .getByText("職業紹介", { exact: true }).waitFor();
    assert.ok(await page.getByLabel("募集区分", { exact: true })
      .evaluate((element) => element.getBoundingClientRect().height >= 44));
    const placements = await get(context.request, "/api/saas/jobs?business_type=placement&limit=50", pageSchema(schemas.job));
    const existing = await get(context.request, "/api/saas/introductions?limit=100", pageSchema(schemas.introduction));
    const occupiedPeople = new Set(existing.items.filter((item) => !["cancelled", "declined", "expired"].includes(item.status)).map((item) => item.person_id));
    const previouslyUsedJobs = new Set(existing.items.map((item) => item.job_id));
    const openJobs = placements.items.filter((item) => item.business_type === "placement" && item.status === "open");
    const freshJobs = openJobs.filter((item) => !previouslyUsedJobs.has(item.id));
    const choices = (freshJobs.length ? freshJobs : openJobs)
      .sort((a, b) => a.experience_months - b.experience_months
        || a.required_qualifications.length - b.required_qualifications.length
        || Object.values(a.required_skills).reduce((sum, value) => sum + value, 0)
          - Object.values(b.required_skills).reduce((sum, value) => sum + value, 0))
      .slice(0, maxJobAttempts);
    let chosenJob: Job | undefined;
    let chosenCandidate: Candidate | undefined;
    let comparedCandidate: Candidate | undefined;
    for (const job of choices) {
      stage = "matching-submit";
      await activate(page.getByRole("button", { name: new RegExp(escapeRegex(job.title)) }));
      await page.getByRole("heading", { name: job.title, exact: true }).waitFor();
      const run = await mutation(page, "/api/saas/matching/runs", 202, schemas.matchRun,
        () => activate(page.getByRole("button", { name: "この求人のマッチングを実行", exact: true })));
      stage = "matching-poll";
      const completedRun = await waitForRun(context.request, run);
      assert.equal(completedRun.coverage.jobs, 1);
      await page.getByLabel("実行履歴を選ぶ", { exact: true }).selectOption(run.id);
      await page.getByText("完了 · 100%", { exact: true }).waitFor();
      const params = new URLSearchParams({ job_id: job.id, limit: String(CANDIDATE_PAGE_SIZE) });
      const candidates = await get(context.request, `/api/saas/matching/runs/${encodeURIComponent(run.id)}/candidates?${params}`, pageSchema(schemas.candidate));
      const eligible = candidates.items.filter((item) => item.job_id === job.id && item.eligible && item.scores !== null);
      const manual = eligible.find((item) => !item.recommended && !occupiedPeople.has(item.person_id));
      const other = eligible.find((item) => item.person_id !== manual?.person_id);
      if (manual && other) {
        const surnames = new Set(eligible.map((item) => item.person_name.split(" ")[0]));
        assert.ok(surnames.size >= 2, "The sampled demo shortlist still clusters into one surname.");
        console.info(JSON.stringify({
          check: "randomized-demo-shortlist", job_id: job.id,
          eligible_candidates: eligible.length, distinct_surnames: surnames.size,
        }));
        chosenJob = job; chosenCandidate = manual; comparedCandidate = other;
        break;
      }
    }
    assert.ok(chosenJob && chosenCandidate && comparedCandidate, "No suitable nonrecommended candidate in the bounded search.");
    const selectedJob = chosenJob;
    const selectedCandidate = chosenCandidate;
    const comparisonCandidate = comparedCandidate;

    stage = "job-map-disclosure-and-comparison-jump";
    const location = page.locator(".saas-location-details");
    assert.equal(await location.getAttribute("open"), null);
    await activate(location.locator("summary"));
    await location.locator(".leaflet-overlay-pane path:visible").first().waitFor();
    await location.screenshot({ path: resolve(evidence, "job-map-disclosed.png") });
    await activate(location.locator("summary"));
    assert.equal(await location.getAttribute("open"), null);
    await activate(page.getByRole("button", { name: "候補者の比較へ移動", exact: true }));
    await assertFocused(page.getByRole("heading", { name: "候補者を比較する", exact: true }));

    stage = "candidate-comparison-and-map";
    const card = page.locator(".saas-candidate-list > li").filter({ hasText: selectedCandidate.person_name });
    await card.waitFor();
    assert.equal(await card.count(), 1);
    await activate(card.getByRole("button", { name: "この人を手動選択", exact: true }));
    for (const candidate of [selectedCandidate, comparedCandidate]) {
      await check(page.getByLabel(`${candidate.person_name}を比較`, { exact: true }));
      const table = page.getByRole("table", { name: `${candidate.person_name}の比較評価（100点尺度）` }).first();
      assert.ok(candidate.scores);
      for (const [key, label] of [
        ["skills", "スキル"], ["qualifications", "資格"], ["preferences", "希望"], ["commute", "通勤"], ["history", "職歴"],
      ] as const) {
        const row = table.getByRole("row").filter({ has: page.getByRole("rowheader", { name: label, exact: true }) });
        const value: number | null = candidate.scores[key];
        assert.equal(await row.getByRole("cell").innerText(), value === null ? "未確認" : value.toFixed(1));
      }
    }
    stage = "candidate-version-consistency";
    const selectedPerson = await get(context.request, `/api/saas/people/${encodeURIComponent(selectedCandidate.person_id)}`, schemas.person);
    assert.equal(selectedPerson.version, selectedCandidate.person_version);
    assert.equal(selectedJob.version, selectedCandidate.job_version);
    stage = "candidate-public-map-ui";
    const map = page.locator(".saas-choice .saas-map");
    await map.locator(".leaflet-overlay-pane path").first().waitFor({ state: "attached" });
    await map.screenshot({ path: resolve(evidence, "map-1024-light.png") });
    // The first network feature can be outside the fitted candidate bounds and clipped to an empty SVG path.
    await map.locator(".leaflet-overlay-pane path:visible").first().waitFor();
    assert.equal(await page.getByRole("link", { name: "地図データの出典・ライセンスを開く／保存" }).first().getAttribute("href"), "/api/saas/geo/manifest");
    await page.locator(".saas-choice").screenshot({ path: resolve(evidence, "candidate-1024-light.png") });
    await assertVisualAccessibility(page);
    const history = page.getByLabel("実行履歴を選ぶ", { exact: true });
    const selectedRunId = await history.inputValue();
    const selectedHeading = page.getByRole("heading", { name: `${selectedCandidate.person_name}さんを確認`, exact: true });
    async function verifySelection() {
      assert.equal(await card.getByRole("button", { name: "選択中", exact: true }).getAttribute("aria-pressed"), "true");
      assert.equal(await history.inputValue(), selectedRunId);
      assert.ok(await page.getByLabel(`${selectedCandidate.person_name}を比較`, { exact: true }).isChecked());
      assert.ok(await page.getByLabel(`${comparisonCandidate.person_name}を比較`, { exact: true }).isChecked());
    }
    if (browserName === "webkit") {
      stage = "candidate-touch-portrait";
      await activate(card.getByRole("button", { name: "この候補の詳細・紹介操作へ", exact: true }));
      await assertFocused(selectedHeading);
      await page.setViewportSize({ width: 768, height: 1024 });
      await assertFocused(selectedHeading);
      await verifySelection();
      assert.deepEqual(await page.evaluate(() => ({
        width: innerWidth, overflow: document.documentElement.scrollWidth > innerWidth,
      })), { width: 768, overflow: false });
      await page.locator(".saas-choice").screenshot({ path: resolve(evidence, "candidate-768-portrait.png") });
    }
    stage = "candidate-reflow-320px";
    await page.setViewportSize({ width: 320, height: 740 });
    await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
    assert.equal(await page.evaluate(() => innerWidth), 320);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator(".saas-choice").screenshot({ path: resolve(evidence, "candidate-320-dark.png") });
    await assertVisualAccessibility(page);
    await map.screenshot({ path: resolve(evidence, "map-320-dark.png") });
    if (browserName === "webkit") {
      stage = "deployed-select-reflow-320px-200percent";
      const previousFontSize = await page.evaluate(() => document.documentElement.style.fontSize);
      await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
      await verifySelection();
      await assertFocused(selectedHeading);
      await history.locator("..").screenshot({ path: resolve(evidence, "history-320-dark-200.png") });
      assert.deepEqual(await page.evaluate(() => ({
        width: innerWidth, scroll_width: document.documentElement.scrollWidth,
      })), { width: 320, scroll_width: 320 });
      assert.ok((await history.locator("option:checked").textContent())?.includes(selectedRunId.slice(-8)));
      const shortlist = page.getByRole("button", { name: "紹介候補を確定", exact: true });
      assert.ok(await shortlist.isEnabled());
      await shortlist.scrollIntoViewIfNeeded();
      await page.screenshot({ path: resolve(evidence, "candidate-actions-320-dark-200.png") });
      await page.evaluate((fontSize) => { document.documentElement.style.fontSize = fontSize; }, previousFontSize);
    }
    await page.setViewportSize(initialViewport);
    await page.emulateMedia({ colorScheme: "light" });
    await verifySelection();
    assert.equal(await page.getByLabel("募集区分", { exact: true }).inputValue(), "placement");

    stage = "shortlist";
    creationUnconfirmed = true;
    await mutation(page, "/api/saas/introductions", 201, schemas.introduction,
      () => activate(page.getByRole("button", { name: "紹介候補を確定", exact: true })),
      (intro) => {
        assert.equal(intro.job_id, selectedJob.id);
        assert.equal(intro.person_id, selectedCandidate.person_id);
        ownedIntro = intro; creationUnconfirmed = false;
      });
    assert.ok(ownedIntro);
    const introId = ownedIntro.id;
    const alias = ownedIntro.candidate_alias;
    stage = "initial-company-inbox-privacy";
    const anonymousNotice = await waitForNotice(context.request, introId, "company", "shortlisted");
    assert.ok(anonymousNotice.message.includes(alias));
    assert.ok(!anonymousNotice.message.includes(selectedPerson.name));
    await navigate(page, "受信箱");
    await check(page.getByLabel("募集企業", { exact: true }));
    const anonymousCard = page.locator(".saas-inbox > li").filter({ hasText: alias });
    await anonymousCard.first().waitFor();
    assert.ok(!(await anonymousCard.first().innerText()).includes(selectedPerson.name));

    stage = "simulated-company-request";
    await navigate(page, "紹介");
    await activate(page.getByRole("button", { name: new RegExp(escapeRegex(alias)) }));
    const rememberIntro = (intro: Introduction) => {
      assert.equal(intro.id, introId);
      assert.equal(intro.job_id, selectedJob.id);
      assert.equal(intro.person_id, selectedCandidate.person_id);
      ownedIntro = intro;
    };
    const requested = await mutation(page, `/api/saas/introductions/${encodeURIComponent(introId)}/simulate`, 200, schemas.introduction,
      () => activate(page.getByRole("button", { name: "模擬紹介依頼", exact: true })), rememberIntro);
    assert.equal(requested.status, "requested");
    await page.getByRole("button", { name: "模擬本人同意", exact: true }).waitFor();
    assert.equal(await page.locator('.saas-flow [aria-current="step"]').textContent(), "模擬紹介依頼");
    await page.locator(".saas-flow").screenshot({ path: resolve(evidence, "introduction-progress-light.png") });
    await assertVisualAccessibility(page);
    stage = "simulated-person-consent";
    const consented = await mutation(page, `/api/saas/introductions/${encodeURIComponent(introId)}/simulate`, 200, schemas.introduction,
      () => activate(page.getByRole("button", { name: "模擬本人同意", exact: true })), rememberIntro);
    assert.equal(consented.status, "consented");
    assert.equal(consented.simulated, true);

    stage = "interview-proposal";
    const appointments = await get(context.request, "/api/saas/appointments?limit=100", pageSchema(schemas.appointment));
    assert.equal(appointments.next_cursor, null, "Appointment history exceeds the bounded slot search.");
    const tomorrow = new Date(Date.now() + 33 * 3_600_000).toISOString().slice(0, 10);
    let start: Date | undefined;
    for (let offset = 0; offset < 14; offset++) {
      const candidateStart = new Date(`${tomorrow}T10:00:00+09:00`);
      candidateStart.setTime(candidateStart.getTime() + offset * 30 * 60_000);
      const end = candidateStart.getTime() + 30 * 60_000;
      if (end < Date.parse(consented.expires_at) && !appointments.items.some((item) =>
        (item.person_id === selectedCandidate.person_id || (item.online && item.company_id === selectedJob.company_id))
        && item.status !== "cancelled"
        && Date.parse(item.start_at) < end && candidateStart.getTime() < Date.parse(item.end_at))) {
        start = candidateStart; break;
      }
    }
    assert.ok(start && start.getTime() > Date.now(), "No safe interview slot in bounded search.");
    const end = new Date(start.getTime() + 30 * 60_000);
    await page.getByLabel("開始日時（日本時間）", { exact: true }).fill(tokyoInput(start));
    await page.getByLabel("終了日時（日本時間）", { exact: true }).fill(tokyoInput(end));
    await page.getByLabel("開催方法", { exact: true }).selectOption("online");
    creationUnconfirmed = true;
    const proposed = await mutation(page, `/api/saas/introductions/${encodeURIComponent(introId)}/appointments`, 201, schemas.appointment,
      () => activate(page.getByRole("button", { name: "面接日時を提案", exact: true })),
      (appointment) => {
        assert.equal(appointment.introduction_id, introId);
        assert.equal(appointment.person_id, selectedCandidate.person_id);
        ownedAppointment = appointment; creationUnconfirmed = false;
      });
    assert.equal(Date.parse(proposed.start_at), start.getTime());
    assert.equal(Date.parse(proposed.end_at), end.getTime());
    assert.equal(proposed.online, true);
    stage = "interview-selection";
    await activate(page.getByRole("button", { name: "面接の日程と合意を確認", exact: true }));
    const appointmentButton = page.locator(".saas-record-list button")
      .filter({ hasText: selectedJob.id }).filter({ hasText: displayTime(proposed.start_at) });
    await appointmentButton.waitFor();
    assert.equal(await appointmentButton.count(), 1);
    await activate(appointmentButton);
    stage = "interview-acceptance";
    const accepted = await mutation(page, `/api/saas/appointments/${encodeURIComponent(proposed.id)}/accept`, 200, schemas.appointment,
      () => activate(page.getByRole("button", { name: "提案日時に合意（デモ）", exact: true })),
      (appointment) => { assert.equal(appointment.id, proposed.id); ownedAppointment = appointment; });
    assert.equal(accepted.status, "accepted");
    await page.getByText(`合意済み · v${accepted.version}`, { exact: true }).waitFor();
    await page.screenshot({ path: resolve(evidence, "interview-1024-light.png"), fullPage: false });

    stage = "person-inbox";
    const interviewNotice = await waitForNotice(context.request, introId, "person", "simulated_interview_accept");
    await navigate(page, "受信箱");
    await check(page.getByLabel("本人", { exact: true }));
    const ownNotices = page.locator(".saas-inbox > li").filter({ hasText: alias })
      .filter({ has: page.getByRole("heading", { name: interviewNotice.title, exact: true }) });
    await ownNotices.first().waitFor();
    await ownNotices.first().screenshot({ path: resolve(evidence, "inbox-1024-light.png") });
    await assertVisualAccessibility(page);
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    if (browserName === "webkit") {
      assert.ok(await page.evaluate(() => Number(document.documentElement.dataset.acceptanceTouches) >= 15));
    }
    await Promise.all(privacyChecks);
    assert.equal(privacyFailed, false);

    stage = "viewer-credential-capture";
    const viewerPassword = capturePassword("viewer");
    stage = "viewer-login-and-read-only-ui";
    viewerContext = await browser.newContext(contextOptions);
    const viewer = await login(viewerContext, "viewer", viewerPassword, "demo");
    assert.equal(viewer.session.tenant.role, "viewer");
    await viewer.page.getByText(/閲覧専用アカウントです。検索・詳細確認は利用できます/).waitFor();
    assert.ok(await viewer.page.getByRole("button", { name: "全求人のマッチングを実行", exact: true }).isDisabled());
    const viewerPeople = await get(viewerContext.request, "/api/saas/people?limit=1", pageSchema(schemas.personSummary), "demo");
    assert.equal(viewerPeople.total, 1000);
    await viewer.page.screenshot({ path: resolve(evidence, "viewer-1024-light.png"), fullPage: false });
    stage = "viewer-server-denial";
    // Missing entity IDs make these probes non-mutating even if the authorization gate regresses.
    const missing = `acceptance-missing-${randomUUID()}`;
    for (const [path, body] of [
      ["/api/saas/matching/runs", { idempotency_key: randomUUID(), job_ids: [missing], pins: [] }],
      [`/api/saas/notifications/${missing}/read`, { idempotency_key: randomUUID() }],
    ] as const) {
      const response: APIResponse = await viewerContext.request.post(path, {
        headers: { ...csrf, "X-Tenant-ID": "demo" }, data: body, timeout: requestTimeoutMs,
      });
      record(response);
      assert.equal(response.status(), 403);
      const bodyValue: unknown = await response.json();
      assert.ok(bodyValue && typeof bodyValue === "object" && "detail" in bodyValue);
      const detail = bodyValue.detail;
      assert.ok(detail && typeof detail === "object" && "code" in detail && detail.code === "read_only");
    }
    await Promise.all(privacyChecks);
    assert.equal(privacyFailed, false);
    completed = true;
  } catch (error) {
    const reason = error instanceof Error && error.name === "AssertionError" ? "assertion"
      : error instanceof Error && error.name === "TimeoutError" ? "browser-timeout" : "operation";
    failedStage = `${stage}:${reason}`;
  } finally {
    try { await cleanup(); } catch {
      cleanupFailedStage = stage;
      failedStage = failedStage || stage;
    }
    if (creationUnconfirmed) {
      cleanupFailedStage = cleanupFailedStage || "unconfirmed-created-record";
      failedStage = failedStage || "unconfirmed-created-record";
    }
    for (const current of [viewerContext, context]) {
      if (!current) continue;
      try {
        const response = await current.request.post("/api/auth/logout", {
          headers: csrf, data: {}, timeout: requestTimeoutMs,
        });
        record(response);
        assert.ok(response.status() === 200 || response.status() === 401);
      } catch {
        cleanupFailedStage = cleanupFailedStage || "isolated-session-logout";
        failedStage = failedStage || "isolated-session-logout";
      }
    }
    try { await browser?.close(); } catch {
      cleanupFailedStage = cleanupFailedStage || "browser-close";
      failedStage = failedStage || "browser-close";
    }
    if (previousTmpDir === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = previousTmpDir;
    try { await rm(runtime, { recursive: true, force: true }); } catch {
      cleanupFailedStage = cleanupFailedStage || "runtime-cleanup";
      failedStage = failedStage || "runtime-cleanup";
    }
  }
  if (failedStage) {
    console.error(JSON.stringify({ stage: failedStage, cleanup_stage: cleanupFailedStage || undefined, requests: results }));
    throw new Error(`Live SaaS acceptance failed at ${failedStage}${cleanupFailedStage ? `; cleanup incomplete at ${cleanupFailedStage}` : ""}; values omitted.`);
  }
});
