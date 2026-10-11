import assert from "node:assert/strict";
import test from "node:test";
import { mkdir, rm } from "node:fs/promises";
import { resolve } from "node:path";
import type { Page } from "playwright";
import { createServer } from "vite";
import { assertFocused, assertTouchEnvironment, assertVisualAccessibility, browserEngine, launchOptions, reportBrowser, selectBrowser, tabletOptions } from "./browser-support.ts";
import type { Appointment, Introduction, MatchRun, Notification } from "../src/saas-models.ts";
import {
  account, appointment, candidate, company, introduction, job, manualCandidate, overview, pageOf,
  person, personSummary, run, secondPerson, site,
} from "./saas-fixtures.test.ts";

const browserName = selectBrowser();
// Opt-in, mock HTTP only. Neither desktop nor touch emulation is physical iPad evidence.
test("SaaS workspace: mocked-service browser flows", { skip: process.env.STAFF_SAAS_BROWSER_TESTS !== "1" }, async (t) => {
  const artifacts = resolve(browserName === "chrome" ? ".saas-browser" : ".saas-browser-webkit");
  const runtime = resolve(artifacts, ".runtime");
  await mkdir(runtime, { recursive: true });
  const prior = process.env.TMPDIR;
  process.env.TMPDIR = runtime;
  const server = await createServer({ root: process.cwd(), server: { host: "127.0.0.1", port: 0, strictPort: false, watch: null, hmr: false } });
  await server.listen();
  t.after(async () => {
    await server.close();
    if (prior === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = prior;
  });
  const address = server.httpServer?.address();
  assert.ok(address && typeof address !== "string");
  const origin = `http://127.0.0.1:${address.port}`;
  const engine = await browserEngine(browserName).launch(launchOptions(browserName, runtime, process.env.SAAS_CHROME_CHANNEL));
  reportBrowser(engine, browserName);
  t.after(async () => {
    await engine.close();
    await rm(runtime, { recursive: true, force: true });
    if (process.env.SAAS_KEEP_ARTIFACTS !== "1") await rm(artifacts, { recursive: true, force: true });
  });
  const browser = await engine.newContext({
    viewport: { width: 1024, height: 900 }, reducedMotion: "reduce",
  });
  browser.setDefaultTimeout(12_000);
  async function setup(page: Page, options: {
    readOnly?: boolean; canSimulate?: boolean; introductions?: boolean;
    introStatus?: Introduction["status"]; appointment?: boolean; holdRun?: boolean; manyCandidates?: boolean;
  } = {}) {
    const calls: { path: string; method: string; body: unknown; headers: Record<string, string> }[] = [];
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const requestProblems: string[] = [];
    page.on("request", (request) => {
      if (!request.url().startsWith(origin)) requestProblems.push(request.url());
    });
    let intros: Introduction[] = options.introductions ? [{ ...structuredClone(introduction), status: options.introStatus ?? "shortlisted" }] : [];
    let appointments: Appointment[] = options.appointment ? [structuredClone(appointment)] : [];
    let runs: MatchRun[] = [structuredClone(run)];
    let notifications: Notification[] = [];
    let polling = 0;
    let failMutation = "";
    let apiStatus = 200;
    function notify() {
      notifications = (["company", "person", "management"] as const).map((audience, index) => ({
        id: `notification-${index}`, audience, recipient_id: "recipient-1", kind: "introduction",
        title: audience === "company" ? "紹介候補のご案内" : "紹介状況が更新されました",
        message: audience === "company" ? "候補者A-001をご確認ください。これは模擬通知です。" : "デモの紹介記録を更新しました。",
        introduction_id: introduction.id, created_at: "2026-10-06T03:20:00Z", read: false, simulated: true,
      }));
    }
    await page.route("**/api/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      const method = request.method();
      const body: unknown = request.postDataJSON();
      calls.push({ path: url.pathname + url.search, method, body, headers: request.headers() });
      const json = (data: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
      const error = (code: string, status = 409) => json({ detail: { code, message: "最新の情報をご確認ください。" } }, status);
      if (path === "/api/auth/session") {
        const tenant = { ...account.tenant, role: options.readOnly ? "viewer" : "coordinator" };
        return json({ ...account, tenant, tenants: [tenant] });
      }
      if (path === "/api/workspace") return json({ kind: "saas" });
      if (path === "/api/saas/geo/basemap") return json({
        type: "FeatureCollection", network_version: "public-test-v1", attribution: "© OpenStreetMap contributors",
        bbox: [139.7, 35.6, 139.9, 35.8],
        license: "ODbL-1.0", license_url: "https://opendatacommons.org/licenses/odbl/1-0/", approximate: true, features: [{
          type: "Feature", properties: { public: true }, geometry: { type: "LineString", coordinates: [[139.72, 35.68], [139.84, 35.70]] },
        }],
      });
      if (!path.startsWith("/api/saas")) return json([]);
      if (apiStatus !== 200) return error(apiStatus === 401 ? "authentication_required" : "tenant_changed", apiStatus);
      if (method === "POST") {
        assert.equal(request.headers()["x-requested-with"], "staff-matching");
        assert.equal(request.headers()["x-tenant-id"], "demo");
        if (failMutation) {
          const mode = failMutation; failMutation = "";
          if (mode === "network") return route.abort("connectionreset");
          return error(mode, mode === "validation_error" ? 422 : 409);
        }
      }
      if (path === "/api/saas/overview") return json({ ...overview, introductions: intros.length, appointments: appointments.length,
        unread_notifications: notifications.filter((item) => !item.read).length, can_simulate: options.canSimulate ?? true });
      if (path === "/api/saas/jobs") {
        if (url.searchParams.get("q") === "該当なし") return json(pageOf([]));
        const dispatch = { ...job, id: "job-dispatch", title: "通常派遣の入力業務", business_type: "dispatch" };
        return json(url.searchParams.get("cursor") ? pageOf([dispatch], 1000) : pageOf([job], 1000, "cursor-2"));
      }
      if (path.startsWith("/api/saas/jobs/")) return json(path.endsWith("job-dispatch")
        ? { ...job, id: "job-dispatch", title: "通常派遣の入力業務", business_type: "dispatch" } : job);
      if (path === "/api/saas/people") return json(pageOf([personSummary], 1000));
      if (path.startsWith("/api/saas/people/")) return json(path.endsWith("person-2") ? secondPerson : person);
      if (path === "/api/saas/companies") return json(pageOf([company]));
      if (path === "/api/saas/sites") return json(pageOf([site]));
      if (path === "/api/saas/matching/runs") {
        if (method === "GET") return json(pageOf(runs));
        const queued: MatchRun = { ...run, id: `run-${runs.length + 1}`, status: "queued", progress: 0 };
        polling = 0; runs = [queued, ...runs]; return json(queued, 202);
      }
      if (/\/matching\/runs\/[^/]+\/candidates$/.test(path)) {
        if (!options.manyCandidates) return json(pageOf([candidate, manualCandidate]));
        const candidates = Array.from({ length: 50 }, (_, index) => ({
          ...manualCandidate, person_id: `many-person-${index + 1}`,
          person_name: `比較用候補者${String(index + 1).padStart(2, "0")}（架空）`,
          score: 99 - index, recommended: index === 0,
        }));
        const cursors = new Map<string, number>(Array.from({ length: 51 }, (_, index) => [`candidate-cursor:${index}/+=`, index] as const));
        const offset = cursors.get(url.searchParams.get("cursor") ?? "") ?? 0;
        const limit = Number(url.searchParams.get("limit") ?? 50);
        const next = offset + limit;
        return json(pageOf(candidates.slice(offset, next), candidates.length, next < candidates.length ? `candidate-cursor:${next}/+=` : null));
      }
      if (/\/matching\/runs\/[^/]+\/cancel$/.test(path)) {
        runs = runs.map((item, index) => index === 0 ? { ...item, status: "cancelled" } : item);
        return json(runs[0]);
      }
      if (/\/matching\/runs\/[^/]+$/.test(path)) {
        const match = runs.find((item) => path.endsWith(item.id));
        if (match && ["queued", "running"].includes(match.status)) {
          polling++;
          Object.assign(match, polling > 1 && !options.holdRun ? { status: "completed", progress: 100 } : { status: "running", progress: 40 });
        }
        return json(match);
      }
      if (path === "/api/saas/introductions") {
        if (method === "GET") return json(pageOf(intros));
        assert.ok(body && typeof body === "object" && "job_id" in body && "person_id" in body);
        assert.ok(typeof body.job_id === "string" && typeof body.person_id === "string");
        intros = [{ ...structuredClone(introduction), job_id: body.job_id, person_id: body.person_id }];
        notify(); return json(intros[0], 201);
      }
      if (path.endsWith("/simulate")) {
        assert.ok(body && typeof body === "object" && "action" in body && "expected_version" in body && "simulated" in body);
        assert.equal(body.expected_version, intros[0]?.version);
        assert.equal(body.simulated, true);
        const next = body.action === "company_request" ? "requested" : body.action === "person_consent" ? "consented"
          : body.action === "person_decline" ? "declined" : "cancelled";
        intros = intros.map((item) => ({ ...item, status: next, version: item.version + 1 }));
        notify(); return json(intros[0]);
      }
      if (/\/introductions\/[^/]+\/appointments$/.test(path)) {
        assert.ok(body && typeof body === "object" && "start_at" in body && "end_at" in body && "online" in body && "site_id" in body);
        assert.equal(body.start_at, "2030-10-10T01:00:00.000Z");
        appointments = [{ ...appointment, start_at: String(body.start_at), end_at: String(body.end_at),
          online: body.online === true, site_id: typeof body.site_id === "string" ? body.site_id : null }];
        intros = intros.map((item) => ({ ...item, status: "interview_proposed", version: item.version + 1 }));
        notify(); return json(appointments[0], 201);
      }
      if (path.endsWith("/place")) {
        intros = intros.map((item) => ({ ...item, status: "placed", version: item.version + 1 })); return json(intros[0]);
      }
      if (/\/introductions\/[^/]+\/cancel$/.test(path)) {
        intros = intros.map((item) => ({ ...item, status: "cancelled", version: item.version + 1 })); return json(intros[0]);
      }
      if (path === "/api/saas/appointments") return json(pageOf(appointments));
      if (/\/appointments\/[^/]+\/(accept|cancel|reschedule)$/.test(path)) {
        assert.ok(body && typeof body === "object" && "expected_version" in body);
        assert.equal(body.expected_version, appointments[0]?.version);
        appointments = appointments.map((item) => ({
          ...item, status: path.endsWith("accept") ? "accepted" : path.endsWith("cancel") ? "cancelled" : "proposed",
          version: item.version + 1,
          ...(path.endsWith("reschedule") && "start_at" in body && "end_at" in body
            ? { start_at: String(body.start_at), end_at: String(body.end_at) } : {}),
        }));
        if (path.endsWith("accept")) intros = intros.map((item) => ({ ...item, status: "interview_accepted", version: item.version + 1 }));
        notify(); return json(appointments[0]);
      }
      if (path === "/api/saas/notifications") return json(pageOf(notifications.filter((item) => item.audience === url.searchParams.get("audience"))));
      if (/\/notifications\/[^/]+\/read$/.test(path)) {
        const notification = notifications.find((item) => path.includes(item.id)); assert.ok(notification);
        notification.read = true; return json(notification);
      }
      return error(`unmocked_${path}`, 404);
    });
    return { calls, errors, requestProblems, fail: (mode: string) => { failMutation = mode; },
      expire: (status: number) => { apiStatus = status; } };
  }
  async function openJob(page: Page) {
    await page.getByRole("heading", { name: "求人ワークスペース", exact: true }).waitFor();
    await page.getByRole("button", { name: new RegExp(job.title) }).click();
    await page.getByRole("heading", { name: job.title, exact: true }).waitFor();
    await page.getByRole("button", { name: "この求人のマッチングを実行", exact: true }).waitFor();
  }
  async function navigate(page: Page, name: string) {
    await page.getByRole("navigation", { name: "業務メニュー" }).getByRole("button", { name: new RegExp(`^${name}`) }).click();
  }

  await t.test("search, resize, manual candidate, pins, durable matching and full demo lifecycle", async () => {
    const page = await browser.newPage();
    const fixture = await setup(page);
    await page.goto(origin);
    await openJob(page);
    const calculationDetails = page.getByText("計算の詳細・探索範囲", { exact: true });
    assert.equal(await page.getByText("ソルバー", { exact: true }).isVisible(), false);
    await calculationDetails.focus();
    await page.keyboard.press("Enter");
    await page.getByText("ソルバー", { exact: true }).waitFor();
    assert.ok(await page.getByText(/\d+組を抽出、\d+組を評価/).isVisible());
    await page.keyboard.press("Enter");
    assert.equal(await page.getByText("ソルバー", { exact: true }).isVisible(), false);
    assert.ok(await page.getByText(/最適性や就業の成立を保証するものではありません/).isVisible());
    await page.getByLabel("求人を検索", { exact: true }).fill("入力");
    await page.getByRole("button", { name: "求人を検索", exact: true }).click();
    await page.getByRole("button", { name: "この人を手動選択" }).click();
    await page.getByLabel(`${person.name}を比較`, { exact: true }).check();
    await page.getByLabel(`${secondPerson.name}を比較`, { exact: true }).check();
    await page.getByRole("button", { name: "次回の組み合わせを固定" }).nth(1).click();
    await page.getByRole("button", { name: "資格・職歴・評価記録を見る" }).click();
    await page.getByText("職歴・評価は未確認です。評価点を0として扱いません。", { exact: true }).waitFor();
    await page.getByRole("link", { name: "地図データの出典・ライセンスを開く／保存" }).first().waitFor();
    await page.locator(".saas-choice .saas-map .leaflet-overlay-pane path").first().waitFor();
    assert.equal(await page.getByText("地図を表示できません。以下の所在地・駅・所要時間をご確認ください。", { exact: true }).count(), 0);
    await page.screenshot({ path: resolve(artifacts, "jobs-1024-light.png"), fullPage: true });
    await page.locator(".saas-candidate-list").screenshot({ path: resolve(artifacts, "candidate-cards-light.png") });
    await page.locator(".saas-radar").first().screenshot({ path: resolve(artifacts, "score-comparison-light.png") });
    await page.evaluate(() => scrollTo(0, 0));
    await page.screenshot({ path: resolve(artifacts, "jobs-viewport-light.png") });
    await assertVisualAccessibility(page);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    assert.ok(await page.getByRole("button", { name: "紹介候補を確定", exact: true }).evaluate((element) => element.getBoundingClientRect().height >= 44));
    await page.setViewportSize({ width: 320, height: 740 });
    await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
    assert.ok(await page.getByRole("heading", { name: job.title, exact: true }).isVisible());
    await page.screenshot({ path: resolve(artifacts, "candidate-320-dark.png"), fullPage: true });
    await assertVisualAccessibility(page);
    await page.locator(".saas-choice .saas-map").screenshot({ path: resolve(artifacts, "candidate-map-320.png") });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.getByRole("button", { name: "求人一覧へ戻る", exact: true }).click();
    await page.waitForFunction(() => document.activeElement?.getAttribute("aria-pressed") === "true");
    assert.equal(await page.getByLabel("求人を検索", { exact: true }).inputValue(), "入力");
    await page.getByRole("button", { name: new RegExp(job.title) }).click();
    await page.setViewportSize({ width: 1024, height: 900 });
    await page.emulateMedia({ colorScheme: "light" });
    await page.getByRole("button", { name: "この求人のマッチングを実行", exact: true }).click();
    await page.getByText("完了 · 100%", { exact: true }).waitFor();
    const matchBody = fixture.calls.find((call) => call.path === "/api/saas/matching/runs" && call.method === "POST")?.body;
    assert.ok(matchBody && typeof matchBody === "object" && "pins" in matchBody);
    assert.deepEqual(matchBody.pins, [[job.id, secondPerson.id]]);
    await page.getByRole("button", { name: "この人を手動選択", exact: true }).click();
    await page.getByRole("button", { name: "紹介候補を確定", exact: true }).click();
    await page.getByText(/さんを紹介候補にしました/).waitFor();
    await navigate(page, "紹介");
    await page.getByRole("button", { name: /候補者A-001/ }).click();
    assert.equal(await page.locator('.saas-flow [aria-current="step"]').textContent(), "紹介候補");
    await page.getByRole("button", { name: "模擬紹介依頼", exact: true }).click();
    await page.getByRole("button", { name: "模擬本人同意", exact: true }).waitFor();
    assert.equal(await page.locator('.saas-flow [aria-current="step"]').textContent(), "模擬紹介依頼");
    await page.locator(".saas-flow").screenshot({ path: resolve(artifacts, "introduction-progress-light.png") });
    await assertVisualAccessibility(page);
    await page.getByRole("button", { name: "模擬本人同意", exact: true }).click();
    await page.getByLabel("開始日時（日本時間）", { exact: true }).fill("2030-10-10T10:00");
    await page.getByLabel("終了日時（日本時間）", { exact: true }).fill("2030-10-10T11:00");
    await page.getByLabel("開催方法", { exact: true }).selectOption("site");
    await page.getByLabel("会場", { exact: true }).selectOption(site.id);
    fixture.fail("validation_error");
    await page.getByRole("button", { name: "面接日時を提案", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "validation_error" }).waitFor();
    assert.equal(await page.getByLabel("開始日時（日本時間）", { exact: true }).inputValue(), "2030-10-10T10:00");
    await page.getByRole("button", { name: "面接日時を提案", exact: true }).click();
    await page.getByRole("button", { name: "面接の日程と合意を確認", exact: true }).click();
    await page.getByRole("button", { name: /2030\/10\/10 10:00/ }).click();
    await page.getByRole("button", { name: "提案日時に合意（デモ）", exact: true }).click();
    await page.getByText("合意済み · v2", { exact: true }).waitFor();
    await page.getByRole("button", { name: "日時を変更", exact: true }).click();
    await page.getByLabel("開始日時（日本時間）", { exact: true }).fill("2030-10-11T10:00");
    await page.getByLabel("終了日時（日本時間）", { exact: true }).fill("2030-10-11T11:00");
    await page.getByRole("button", { name: "変更日時を再提案", exact: true }).click();
    await page.getByText("提案中 · v3", { exact: true }).waitFor();
    await page.getByRole("button", { name: "提案日時に合意（デモ）", exact: true }).click();
    await page.getByText("合意済み · v4", { exact: true }).waitFor();
    await page.screenshot({ path: resolve(artifacts, "interview-1024-light.png"), fullPage: true });
    await assertVisualAccessibility(page);
    await navigate(page, "紹介");
    await page.getByRole("button", { name: "成約を記録", exact: true }).click();
    await page.getByRole("button", { name: "確認して成約を記録", exact: true }).click();
    await page.getByText("成約（デモ） · v7", { exact: true }).waitFor();
    await navigate(page, "受信箱");
    await page.getByLabel("募集企業", { exact: true }).check();
    await page.getByText("候補者A-001をご確認ください。これは模擬通知です。", { exact: true }).waitFor();
    assert.equal(await page.locator(".saas-inbox").getByText(secondPerson.name, { exact: true }).count(), 0);
    await page.getByRole("button", { name: "既読にする", exact: true }).click();
    await page.getByText("既読 · 模擬通知", { exact: true }).waitFor();
    await assertVisualAccessibility(page);
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    assert.deepEqual(fixture.errors, []);
    assert.deepEqual(fixture.requestProblems, []);
    await page.close();
  });
  await t.test("icons retain labels and status cues in light/dark, increased contrast and forced colors", async () => {
    const page = await browser.newPage();
    const fixture = await setup(page, { introductions: true, introStatus: "expired" });
    await page.goto(origin); await openJob(page);
    await page.getByRole("button", { name: "この人を手動選択", exact: true }).click();
    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme, contrast: "more", reducedMotion: "no-preference" });
      assert.ok(await page.evaluate(() => matchMedia("(prefers-contrast: more)").matches));
      assert.ok(await page.evaluate(() => {
        const style = getComputedStyle(document.querySelector(".saas-workspace")!);
        return style.getPropertyValue("--saas-line").trim() === style.getPropertyValue("--saas-ink").trim();
      }));
      const primary = page.getByRole("button", { name: "この求人のマッチングを実行", exact: true });
      assert.equal(await primary.isDisabled(), false);
      await primary.hover();
      await assertVisualAccessibility(page);
      const menu = page.getByRole("navigation", { name: "業務メニュー" });
      const selected = menu.getByRole("button", { name: "求人", exact: true });
      assert.equal(await selected.getAttribute("aria-current"), "page");
      assert.equal(await selected.locator(".saas-nav-selected").count(), 1);
      await selected.focus();
      await page.keyboard.press("Tab");
      await assertFocused(menu.getByRole("button", { name: "人材", exact: true }));
      await page.screenshot({ path: resolve(artifacts, `contrast-more-${colorScheme}.png`) });
    }
    await navigate(page, "紹介");
    await page.getByRole("button", { name: /候補者A-001/ }).click();
    await page.getByText("期限切れ · v1", { exact: true }).waitFor();
    assert.equal(await page.locator('.saas-flow [aria-current="step"]').count(), 0);
    assert.equal(await page.locator(".saas-flow-complete").count(), 0);
    await assertVisualAccessibility(page);
    await page.screenshot({ path: resolve(artifacts, "introduction-expired-dark.png"), fullPage: true });
    if (browserName === "chrome") {
      await page.emulateMedia({ forcedColors: "active", contrast: "no-preference" });
      assert.ok(await page.evaluate(() => matchMedia("(forced-colors: active)").matches));
      await assertVisualAccessibility(page);
      await page.screenshot({ path: resolve(artifacts, "forced-colors.png"), fullPage: true });
    } else {
      console.info("Forced-colors emulation is covered in Chrome, not claimed for WebKit/Safari.");
    }
    assert.deepEqual(fixture.errors, []);
    await page.close();
  });

  await t.test("compact workspace keeps actions in view, filters explicit and optional maps keyboard-accessible", async () => {
    const page = await browser.newPage();
    const fixture = await setup(page);
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto(origin);
    const listPane = page.getByRole("region", { name: "求人の検索と一覧", exact: true });
    await listPane.getByRole("button", { name: new RegExp(job.title) }).waitFor();
    const listBox = await listPane.boundingBox();
    assert.ok(listBox && listBox.y <= 450, `Search pane should start within 450 CSS px; got ${listBox?.y}`);
    const typeBox = await page.getByLabel("募集区分", { exact: true }).boundingBox();
    assert.ok(typeBox && typeBox.height >= 44, `Filter select target must remain 44 CSS px; got ${typeBox?.height}`);
    await page.screenshot({ path: resolve(artifacts, "compact-workspace-1024.png") });
    await openJob(page);
    await page.evaluate(() => scrollTo(0, 0));
    const primary = page.getByRole("button", { name: "この求人のマッチングを実行", exact: true });
    const actionBox = await primary.boundingBox();
    assert.ok(actionBox && actionBox.y >= 0 && actionBox.y + actionBox.height <= 768,
      "The selected-job action must be in the initial tablet viewport.");
    console.info(JSON.stringify({ stage: "workspace-density", width: 1024, height: 768,
      search_pane_top: listBox.y, selected_job_action_bottom: actionBox.y + actionBox.height,
      filter_select_height: typeBox.height }));
    await page.screenshot({ path: resolve(artifacts, "compact-selected-job-1024.png") });
    const jump = page.getByRole("button", { name: "候補者の比較へ移動", exact: true });
    await jump.focus(); await page.keyboard.press("Enter");
    await assertFocused(page.getByRole("heading", { name: "候補者を比較する", exact: true }));
    await page.getByRole("button", { name: "この人を手動選択", exact: true }).click();
    await page.getByLabel(`${secondPerson.name}を比較`, { exact: true }).check();
    await page.getByRole("button", { name: "次回の組み合わせを固定", exact: true }).nth(1).click();
    const disclosure = page.locator(".saas-location-details > summary");
    assert.equal(await page.locator(".saas-location-details").getAttribute("open"), null);
    await disclosure.focus(); await page.keyboard.press("Enter");
    await page.locator(".saas-location-details .leaflet-overlay-pane path:visible").first().waitFor();
    await page.locator(".saas-location-details").screenshot({ path: resolve(artifacts, "disclosed-job-map.png") });
    await page.setViewportSize({ width: 768, height: 1024 });
    await navigate(page, "人材"); await navigate(page, "求人");
    assert.notEqual(await page.locator(".saas-location-details").getAttribute("open"), null);
    await disclosure.focus(); await page.keyboard.press("Enter");
    await assertFocused(disclosure);
    assert.equal(await page.locator(".saas-location-details").getAttribute("open"), null);
    const search = page.getByLabel("求人を検索", { exact: true });
    await search.fill("入力");
    await page.getByLabel("職種で絞り込み", { exact: true }).fill("一般事務");
    await page.getByLabel("募集区分", { exact: true }).selectOption("placement");
    await page.getByRole("button", { name: "求人を検索", exact: true }).click();
    const applied = page.getByRole("group", { name: "適用中の求人検索条件", exact: true });
    await applied.waitFor();
    assert.match(await applied.innerText(), /検索：入力/);
    assert.match(await applied.innerText(), /職種：一般事務/);
    await search.fill("入力途中");
    assert.doesNotMatch(await applied.innerText(), /入力途中/);
    await page.setViewportSize({ width: 375, height: 900 });
    await page.getByRole("button", { name: "求人一覧へ戻る", exact: true }).click();
    for (const width of [375, 320]) {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme: "dark" });
      await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
      assert.ok(await applied.isVisible());
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await assertVisualAccessibility(page);
    }
    await applied.screenshot({ path: resolve(artifacts, "filters-320-dark-200.png") });
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.emulateMedia({ colorScheme: "light" });
    await page.evaluate(() => { document.documentElement.style.fontSize = ""; });
    await listPane.getByRole("button", { name: "次へ", exact: true }).click();
    await listPane.getByRole("button", { name: /通常派遣の入力業務/ }).waitFor();
    const refreshed = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === "/api/saas/jobs" && !url.searchParams.get("q")
        && !url.searchParams.get("occupation") && !url.searchParams.get("business_type") && !url.searchParams.get("cursor");
    });
    await page.getByRole("button", { name: "検索条件をクリア", exact: true }).focus();
    await page.keyboard.press("Enter"); await refreshed;
    await assertFocused(search);
    assert.equal(await search.inputValue(), "");
    assert.equal(await page.getByLabel("職種で絞り込み", { exact: true }).inputValue(), "");
    assert.equal(await page.getByLabel("募集区分", { exact: true }).inputValue(), "");
    assert.equal(await applied.count(), 0);
    assert.equal(await listPane.getByRole("button", { name: "前へ", exact: true }).isDisabled(), true);
    assert.equal(await page.getByRole("button", { name: "選択中", exact: true }).getAttribute("aria-pressed"), "true");
    assert.ok(await page.getByLabel(`${secondPerson.name}を比較`, { exact: true }).isChecked());
    await page.getByRole("heading", { name: "次回の実行で固定する組み合わせ（1/100）", exact: true }).waitFor();
    await openJob(page);
    for (const width of [1440, 1024, 768, 375, 320]) {
      await page.setViewportSize({ width, height: 900 });
      if (width <= 375) await page.emulateMedia({ colorScheme: "dark" });
      if (width === 320) await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      if (width === 1440) {
        await page.evaluate(() => scrollTo(0, 0));
        await page.screenshot({ path: resolve(artifacts, "compact-selected-job-1440.png") });
      }
      await jump.click();
      await assertFocused(page.getByRole("heading", { name: "候補者を比較する", exact: true }));
      const headingBox = await page.getByRole("heading", { name: "候補者を比較する", exact: true }).boundingBox();
      assert.ok(headingBox && headingBox.y >= 0 && headingBox.y + headingBox.height <= 900);
    }
    await assertVisualAccessibility(page);
    assert.equal(fixture.calls.filter((call) => call.method === "POST").length, 0);
    assert.deepEqual(fixture.errors, []);
    assert.deepEqual(fixture.requestProblems, []);
    await page.close();
  });

  await t.test("pagination, keyboard, people/history, empty search and 200% text reflow", async () => {
    const page = await browser.newPage();
    const fixture = await setup(page);
    await page.goto(origin);
    await page.getByLabel("求人を検索", { exact: true }).focus();
    await page.keyboard.press("Tab");
    assert.ok(await page.getByLabel("職種で絞り込み").evaluate((element) => element === document.activeElement));
    await page.getByRole("button", { name: "次へ", exact: true }).click();
    await page.getByRole("button", { name: /通常派遣の入力業務/ }).click();
    await page.getByText(/通常派遣の求人です/).first().waitFor();
    await page.getByRole("button", { name: "条件・根拠を確認", exact: true }).nth(1).click();
    assert.equal(await page.getByRole("button", { name: "この人を手動選択", exact: true }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "紹介候補を確定", exact: true }).count(), 0);
    assert.ok(fixture.calls.some((call) => call.path.includes("cursor=cursor-2")));
    await navigate(page, "人材");
    await page.getByRole("button", { name: new RegExp(person.name.replace(/[（）]/g, ".")) }).click();
    await page.getByText(person.employment_history[0]!.evaluations[0]!.observation, { exact: true }).waitFor();
    await page.setViewportSize({ width: 320, height: 740 });
    await page.emulateMedia({ colorScheme: "dark" });
    await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.screenshot({ path: resolve(artifacts, "people-320-dark-200.png"), fullPage: true });
    await page.evaluate(() => { document.documentElement.style.fontSize = ""; });
    await navigate(page, "求人");
    await page.getByRole("button", { name: "求人一覧へ戻る", exact: true }).click();
    await page.getByLabel("求人を検索", { exact: true }).fill("該当なし");
    await page.getByRole("button", { name: "求人を検索", exact: true }).click();
    await page.getByText("条件に合う求人はありません。検索条件を変更してください。", { exact: true }).waitFor();
    assert.deepEqual(fixture.errors, []);
    await page.close();
  });
  await t.test("viewer and non-simulation tenants cannot perform business mutations", async () => {
    const page = await browser.newPage();
    const fixture = await setup(page, { readOnly: true, canSimulate: false, introductions: true });
    await page.goto(origin); await openJob(page);
    assert.ok(await page.getByRole("button", { name: "全求人のマッチングを実行", exact: true }).isDisabled());
    await page.getByRole("button", { name: "この人を手動選択", exact: true }).click();
    assert.ok(await page.getByRole("button", { name: "紹介候補を確定", exact: true }).isDisabled());
    await navigate(page, "紹介");
    await page.getByRole("button", { name: /候補者A-001/ }).click();
    assert.equal(await page.getByRole("button", { name: "模擬紹介依頼", exact: true }).count(), 0);
    assert.ok(await page.getByRole("button", { name: "紹介を取り消す", exact: true }).isDisabled());
    assert.equal(fixture.calls.filter((call) => call.method === "POST").length, 0);
    await page.close();
  });
  await t.test("stale/network outcomes are not replayed; auth failure closes business views", async () => {
    const page = await browser.newPage();
    const fixture = await setup(page, { introductions: true });
    await page.goto(origin); await navigate(page, "紹介");
    await page.getByRole("button", { name: /候補者A-001/ }).click();
    fixture.fail("stale_version");
    await page.getByRole("button", { name: "模擬紹介依頼", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "stale_version" }).waitFor();
    assert.ok(await page.getByRole("button", { name: "模擬紹介依頼", exact: true }).isDisabled());
    assert.equal(fixture.calls.filter((call) => call.method === "POST").length, 1);
    await page.getByRole("button", { name: "最新情報を再取得", exact: true }).click();
    fixture.fail("network");
    await page.getByRole("button", { name: "模擬紹介依頼", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "操作結果を確認できません" }).waitFor();
    assert.equal(fixture.calls.filter((call) => call.method === "POST").length, 2);
    await page.screenshot({ path: resolve(artifacts, "uncertain-outcome.png"), fullPage: true });
    fixture.expire(401);
    await page.getByRole("button", { name: "最新情報を再取得", exact: true }).click();
    await page.getByRole("heading", { name: "会社のワークスペースへ", exact: true }).waitFor();
    assert.equal(await page.locator(".saas-workspace").count(), 0);
    assert.equal(fixture.calls.filter((call) => call.method === "POST").length, 2);
    await page.close();
  });
  await t.test("all-job run can be inspected/cancelled at 320px; consent withdrawal needs explicit confirmation", async () => {
    const page = await browser.newPage();
    const fixture = await setup(page, { introductions: true, introStatus: "consented", holdRun: true });
    await page.setViewportSize({ width: 320, height: 740 });
    await page.goto(origin);
    await page.getByRole("button", { name: "全求人のマッチングを実行", exact: true }).click();
    await page.getByText("計算中 · 40%", { exact: true }).waitFor();
    await page.getByRole("button", { name: "計算を中止", exact: true }).click();
    await page.getByText("中止 · 40%", { exact: true }).waitFor();
    const allRun = fixture.calls.find((call) => call.path === "/api/saas/matching/runs" && call.method === "POST")?.body;
    assert.ok(allRun && typeof allRun === "object" && "job_ids" in allRun);
    assert.deepEqual(allRun.job_ids, []);
    await navigate(page, "紹介");
    await page.getByRole("button", { name: /候補者A-001/ }).click();
    await page.getByRole("button", { name: "模擬同意撤回", exact: true }).click();
    assert.ok(await page.getByRole("button", { name: "戻る", exact: true }).evaluate((element) => element === document.activeElement));
    await page.getByRole("button", { name: "確認して模擬同意撤回", exact: true }).click();
    await page.getByText("取消済み · v2", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "面接日時を提案", exact: true }).count(), 0);
    assert.deepEqual(fixture.errors, []);
    await page.close();
  });
  await t.test("appointment cancellation and offline recovery preserve inspection without automatic mutations", async () => {
    const page = await browser.newPage();
    const fixture = await setup(page, { introductions: true, introStatus: "interview_proposed", appointment: true });
    await page.goto(origin); await navigate(page, "面接");
    await page.getByRole("button", { name: /2030\/10\/10 10:00/ }).click();
    await page.getByRole("button", { name: "面接を取り消す", exact: true }).click();
    await page.getByRole("button", { name: "確認して面接を取り消す", exact: true }).click();
    await page.getByText("取消済み · v2", { exact: true }).waitFor();
    await navigate(page, "求人");
    await page.getByLabel("求人を検索", { exact: true }).fill("再接続まで保持する検索");
    await browser.setOffline(true);
    await page.getByText(/オフラインです。表示中の情報は古い可能性/).waitFor();
    assert.ok(await page.getByRole("button", { name: "全求人のマッチングを実行", exact: true }).isDisabled());
    await browser.setOffline(false);
    assert.ok(await page.getByRole("button", { name: "全求人のマッチングを実行", exact: true }).isDisabled());
    await page.getByRole("button", { name: "最新情報を再取得", exact: true }).click();
    assert.equal(await page.getByLabel("求人を検索", { exact: true }).inputValue(), "再接続まで保持する検索");
    assert.equal(fixture.calls.filter((call) => call.method === "POST").length, 1);
    assert.deepEqual(fixture.errors, []);
    await page.close();
  });
  await t.test("50 candidates stay in six-card cursor pages; narrow comparison and shortlist remain reachable", async () => {
    const page = await browser.newPage();
    const fixture = await setup(page, { manyCandidates: true });
    await page.setViewportSize({ width: 320, height: 740 });
    await page.emulateMedia({ colorScheme: "dark" });
    await page.goto(origin); await openJob(page);
    const candidates = page.getByRole("region", { name: "候補者の比較", exact: true });
    await candidates.getByText("50名 · 1ページ6名 · ページ内で最大3名を比較", { exact: true }).waitFor();
    assert.equal(await candidates.locator(".saas-candidate-list > li").count(), 6);
    assert.equal(await candidates.getByRole("heading", { name: /^7\./ }).count(), 0);
    const first = "比較用候補者01（架空）";
    const second = "比較用候補者02（架空）";
    await candidates.getByLabel(`${first}を比較`, { exact: true }).check();
    await candidates.getByLabel(`${second}を比較`, { exact: true }).check();
    assert.equal(await candidates.getByRole("table", { name: `${first}の比較評価（100点尺度）` }).count(), 1);
    await candidates.locator(".saas-candidate-list > li").nth(1).getByRole("button", { name: "この人を手動選択", exact: true }).click();
    await candidates.getByRole("button", { name: "この候補の詳細・紹介操作へ", exact: true }).click();
    assert.ok(await candidates.getByRole("heading", { name: `${second}さんを確認`, exact: true })
      .evaluate((element) => element === document.activeElement && element.getBoundingClientRect().top >= 0 && element.getBoundingClientRect().bottom <= innerHeight));
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await candidates.screenshot({ path: resolve(artifacts, "many-candidates-320-dark.png") });
    await candidates.getByRole("button", { name: "次へ", exact: true }).click();
    await candidates.getByRole("heading", { name: "7. 比較用候補者07（架空）", exact: true }).waitFor();
    assert.equal(await candidates.locator(".saas-candidate-list > li").count(), 6);
    assert.equal(await candidates.getByRole("table").count(), 0);
    await candidates.locator(".saas-candidate-list > li").first().getByRole("button", { name: "この人を手動選択", exact: true }).click();
    await candidates.getByRole("button", { name: "この候補の詳細・紹介操作へ", exact: true }).click();
    await candidates.getByRole("button", { name: "紹介候補を確定", exact: true }).click();
    await page.getByText("比較用候補者07（架空）さんを紹介候補にしました。採用の確定ではありません。", { exact: true }).waitFor();
    const shortlist = fixture.calls.find((call) => call.path === "/api/saas/introductions" && call.method === "POST")?.body;
    assert.ok(shortlist && typeof shortlist === "object" && "person_id" in shortlist);
    assert.equal(shortlist.person_id, "many-person-7");
    const requests = fixture.calls.filter((call) => call.path.includes("/candidates?")).map((call) => new URL(call.path, origin));
    assert.ok(requests.length >= 2 && requests.every((url) => url.searchParams.get("limit") === "6"));
    assert.ok(requests.some((url) => url.searchParams.get("cursor") === "candidate-cursor:6/+="));
    assert.deepEqual(fixture.errors, []);
    await page.close();
  });
  await t.test("touch primary flow preserves filters, comparisons, pins, drafts and focus across tablet and narrow layouts", async () => {
    const touch = await engine.newContext(tabletOptions());
    touch.setDefaultTimeout(12_000);
    try {
      const page = await touch.newPage();
      const fixture = await setup(page);
      await page.addInitScript(() => {
        let touches = 0;
        document.addEventListener("touchstart", () => { document.documentElement.dataset.touchStarts = String(++touches); }, { passive: true });
      });
      const menu = page.getByRole("navigation", { name: "業務メニュー" });
      const tapNavigate = (name: string) => menu.getByRole("button", { name: new RegExp(`^${name}`) }).tap();
      const reflow = async (width: number) => {
        if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) {
          await page.screenshot({ path: resolve(artifacts, `touch-reflow-failure-${width}.png`), fullPage: true });
          await page.screenshot({ path: resolve(artifacts, `touch-reflow-viewport-${width}.png`) });
          console.info(JSON.stringify({ stage: "mocked-touch-reflow", ...await page.evaluate(() => ({
            width: innerWidth, scroll_width: document.documentElement.scrollWidth, scroll_x: scrollX,
            visual_width: visualViewport?.width, visual_scale: visualViewport?.scale,
            elements: [...document.querySelectorAll("body *")].flatMap((element) => {
              const box = element.getBoundingClientRect();
              return box.width > 0 && (box.right + scrollX > innerWidth || element.scrollWidth > element.clientWidth + 1) ? [{
                tag: element.tagName, classes: element.getAttribute("class"), left: box.left, right: box.right,
                client: element.clientWidth, scroll: element.scrollWidth,
              }] : [];
            }).slice(0, 24),
          })) }));
        }
        assert.deepEqual(await page.evaluate(() => ({
          width: innerWidth, overflow: document.documentElement.scrollWidth > innerWidth,
        })), { width, overflow: false });
      };
      await page.goto(origin);
      await assertTouchEnvironment(page);
      const search = page.getByLabel("求人を検索", { exact: true });
      await search.tap();
      await search.fill("入力");
      await page.getByRole("button", { name: "求人を検索", exact: true }).tap();
      const jobRow = page.getByRole("button", { name: new RegExp(job.title) });
      await jobRow.tap();
      await assertFocused(page.getByRole("heading", { name: job.title, exact: true }));
      const manual = page.locator(".saas-candidate-list > li").filter({ hasText: secondPerson.name })
        .getByRole("button", { name: /^(この人を手動選択|選択中)$/ });
      await manual.tap();
      await page.getByLabel(`${person.name}を比較`, { exact: true }).tap();
      await page.getByLabel(`${secondPerson.name}を比較`, { exact: true }).tap();
      await page.getByRole("button", { name: "次回の組み合わせを固定", exact: true }).nth(1).tap();
      const selectedRun = await page.getByLabel("実行履歴を選ぶ", { exact: true }).inputValue();
      const detail = page.getByRole("heading", { name: `${secondPerson.name}さんを確認`, exact: true });
      await page.getByRole("button", { name: "この候補の詳細・紹介操作へ", exact: true }).tap();
      await assertFocused(detail);
      await reflow(1024);
      await page.locator(".saas-choice").screenshot({ path: resolve(artifacts, "touch-candidate-1024-light.png") });
      await page.setViewportSize({ width: 768, height: 1024 });
      await assertFocused(detail);
      assert.equal(await manual.getAttribute("aria-pressed"), "true");
      assert.ok(await page.getByLabel(`${person.name}を比較`, { exact: true }).isChecked());
      assert.ok(await page.getByLabel(`${secondPerson.name}を比較`, { exact: true }).isChecked());
      assert.equal(await page.getByLabel("実行履歴を選ぶ", { exact: true }).inputValue(), selectedRun);
      const map = page.locator(".saas-choice .saas-map");
      const geometry = map.locator(".leaflet-overlay-pane path").first();
      await geometry.waitFor();
      const beforeZoom = await geometry.getAttribute("d");
      await map.getByTitle("地図を拡大", { exact: true }).tap();
      assert.notEqual(await geometry.getAttribute("d"), beforeZoom);
      await page.locator(".saas-choice").getByRole("button", { name: "表示範囲を戻す", exact: true }).tap();
      await reflow(768);
      await page.locator(".saas-choice").screenshot({ path: resolve(artifacts, "touch-candidate-768-portrait.png") });
      await page.getByRole("button", { name: "この候補の詳細・紹介操作へ", exact: true }).tap();
      await page.setViewportSize({ width: 320, height: 740 });
      await page.emulateMedia({ colorScheme: "dark" });
      await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
      await assertFocused(detail);
      assert.equal(await manual.getAttribute("aria-pressed"), "true");
      await reflow(320);
      const history = page.getByLabel("実行履歴を選ぶ", { exact: true });
      assert.equal(await history.inputValue(), selectedRun);
      assert.ok((await history.locator("option:checked").textContent())?.includes(selectedRun.slice(-8)));
      await history.locator("..").screenshot({ path: resolve(artifacts, "touch-history-320-dark-200.png") });
      const shortlist = page.getByRole("button", { name: "紹介候補を確定", exact: true });
      assert.ok(await shortlist.evaluate((element) => element.getBoundingClientRect().height >= 44));
      await page.locator(".saas-choice").screenshot({ path: resolve(artifacts, "touch-candidate-320-dark-200.png") });
      await shortlist.scrollIntoViewIfNeeded();
      await page.screenshot({ path: resolve(artifacts, "touch-actions-320-dark-200.png") });
      await page.evaluate(() => { document.documentElement.style.fontSize = ""; });
      await page.getByRole("button", { name: "求人一覧へ戻る", exact: true }).tap();
      await assertFocused(jobRow);
      assert.equal(await search.inputValue(), "入力");
      await jobRow.tap();
      await tapNavigate("人材");
      await assertFocused(page.getByRole("heading", { name: "人材ワークスペース", exact: true }));
      await tapNavigate("求人");
      await assertFocused(page.getByRole("heading", { name: "求人ワークスペース", exact: true }));
      assert.equal(await manual.getAttribute("aria-pressed"), "true");
      assert.ok(await page.getByLabel(`${secondPerson.name}を比較`, { exact: true }).isChecked());
      await page.getByRole("heading", { name: "次回の実行で固定する組み合わせ（1/100）", exact: true }).waitFor();
      await page.setViewportSize({ width: 1024, height: 768 });
      await page.emulateMedia({ colorScheme: "light" });
      assert.equal(await search.inputValue(), "入力");
      await page.getByRole("button", { name: "この求人のマッチングを実行", exact: true }).tap();
      await page.getByText("完了 · 100%", { exact: true }).waitFor();
      await manual.tap();
      await shortlist.tap();
      await page.getByText(/さんを紹介候補にしました/).waitFor();
      await tapNavigate("紹介");
      await page.getByRole("button", { name: /候補者A-001/ }).tap();
      await page.getByRole("button", { name: "模擬紹介依頼", exact: true }).tap();
      await page.getByRole("button", { name: "模擬本人同意", exact: true }).tap();
      const start = page.getByLabel("開始日時（日本時間）", { exact: true });
      const end = page.getByLabel("終了日時（日本時間）", { exact: true });
      await start.fill("2030-10-10T10:00");
      await end.fill("2030-10-10T11:00");
      const withdrawal = page.getByRole("button", { name: "模擬同意撤回", exact: true });
      const postCount = () => fixture.calls.filter((call) => call.method === "POST").length;
      const beforeCancel = postCount();
      await withdrawal.tap();
      const back = page.getByRole("button", { name: "戻る", exact: true });
      await assertFocused(back);
      await page.setViewportSize({ width: 768, height: 1024 });
      await assertFocused(back);
      await back.tap();
      await assertFocused(withdrawal);
      assert.equal(await page.getByRole("group", { name: "模擬同意撤回の確認", exact: true }).count(), 0);
      assert.equal(postCount(), beforeCancel);
      assert.equal(await start.inputValue(), "2030-10-10T10:00");
      assert.equal(await end.inputValue(), "2030-10-10T11:00");
      await page.getByRole("button", { name: "面接日時を提案", exact: true }).tap();
      await page.getByRole("button", { name: "面接の日程と合意を確認", exact: true }).tap();
      await page.getByRole("button", { name: /2030\/10\/10 10:00/ }).tap();
      await page.getByRole("button", { name: "提案日時に合意（デモ）", exact: true }).tap();
      await page.getByText("合意済み · v2", { exact: true }).waitFor();
      await reflow(768);
      await page.screenshot({ path: resolve(artifacts, "touch-interview-768-portrait.png"), fullPage: true });
      assert.ok(await page.evaluate(() => Number(document.documentElement.dataset.touchStarts) >= 20));
      const queued = fixture.calls.find((call) => call.path === "/api/saas/matching/runs" && call.method === "POST")?.body;
      assert.ok(queued && typeof queued === "object" && "pins" in queued);
      assert.deepEqual(queued.pins, [[job.id, secondPerson.id]]);
      const shortlisted = fixture.calls.find((call) => call.path === "/api/saas/introductions" && call.method === "POST")?.body;
      assert.ok(shortlisted && typeof shortlisted === "object" && "person_id" in shortlisted);
      assert.equal(shortlisted.person_id, secondPerson.id);
      assert.deepEqual(fixture.errors, []);
      assert.deepEqual(fixture.requestProblems, []);
    } finally { await touch.close(); }
  });
});
