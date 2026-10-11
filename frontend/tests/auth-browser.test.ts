import assert from "node:assert/strict";
import test from "node:test";
import { mkdir, rm } from "node:fs/promises";
import { resolve } from "node:path";
import type { Page } from "playwright";
import { createServer } from "vite";
import { assertFocused, assertTouchEnvironment, assertVisualAccessibility, browserEngine, launchOptions, reportBrowser, selectBrowser, tabletOptions } from "./browser-support.ts";

const browserName = selectBrowser();
// Explicit opt-in: mocked-service UI checks, not real accounts or passkey interoperability evidence.
test("auth UI: mocked-service browser regression", { skip: process.env.STAFF_AUTH_BROWSER_TESTS !== "1" }, async (t) => {
  const artifacts = resolve(browserName === "chrome" ? ".auth-browser" : ".auth-browser-webkit");
  const runtime = resolve(artifacts, ".runtime");
  await mkdir(runtime, { recursive: true });
  const previousTmpDir = process.env.TMPDIR;
  process.env.TMPDIR = runtime;
  const server = await createServer({
    root: process.cwd(), server: { host: "127.0.0.1", port: 0, strictPort: false, watch: null, hmr: false },
  });
  await server.listen();
  t.after(async () => {
    await server.close();
    if (previousTmpDir === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = previousTmpDir;
  });
  const address = server.httpServer?.address();
  assert.ok(address && typeof address !== "string");
  const origin = `http://127.0.0.1:${address.port}`;
  const engine = await browserEngine(browserName).launch(launchOptions(browserName, runtime, process.env.AUTH_CHROME_CHANNEL));
  reportBrowser(engine, browserName);
  t.after(async () => {
    await engine.close();
    await rm(runtime, { recursive: true, force: true });
    if (process.env.AUTH_KEEP_ARTIFACTS !== "1") await rm(artifacts, { recursive: true, force: true });
  });
  const browser = await engine.newContext({ viewport: { width: 1024, height: 768 }, reducedMotion: "reduce" });
  browser.setDefaultTimeout(8_000);

  const firstTenant = { id: "company-a", name: "管理会社あおぞら", role: "coordinator" };
  const secondTenant = { id: "company-b", name: "管理会社さくら", role: "viewer" };
  const account = {
    enabled: true, authenticated: true, user: { id: "user-a", username: "operator-a", display_name: "デモ担当者" },
    tenants: [firstTenant, secondTenant], tenant: firstTenant, auth_method: "password", passkey_count: 1,
  };
  const key = {
    id: "key-a", name: "仕事用iPad", created_at: "2026-10-05T12:00:00Z", last_used_at: null,
    backed_up: true, device_type: "multiDevice",
  };

  async function setup(page: Page, initial: object = { enabled: true, authenticated: false }) {
    let current = initial;
    let keys = [key];
    let needsReauth = true;
    const calls: { path: string; method: string; body: Record<string, unknown> | null }[] = [];
    await page.route("**/api/**", async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const body = request.postDataJSON() as Record<string, unknown> | null;
      calls.push({ path, method: request.method(), body });
      const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
      if (path === "/api/auth/session") return json(current);
      if (path === "/api/auth/login") { current = account; return json(current); }
      if (path === "/api/auth/logout") { current = { enabled: true, authenticated: false }; return json({ ok: true }); }
      if (path === "/api/auth/tenant") {
        current = { ...account, tenant: body?.tenant_id === firstTenant.id ? firstTenant : secondTenant };
        return json(current);
      }
      if (path === "/api/auth/passkeys") return json({ items: keys });
      if (path === "/api/auth/reauth") { needsReauth = false; return json({ ok: true }); }
      if (path === "/api/auth/password") return json(current);
      if (path === "/api/auth/passkeys/key-a" && request.method() === "DELETE") {
        if (needsReauth) return json({ detail: { code: "reauthentication_required", message: "本人確認が必要です" } }, 403);
        keys = [];
        return json({ ok: true });
      }
      if (path === "/api/auth/passkeys/authentication/options") return json({
        challenge: "dGVzdC1jaGFsbGVuZ2U", rpId: "127.0.0.1", userVerification: "required",
      });
      if (path === "/api/health") return json({
        status: "ok", mode: "azure-poc", tenant_id: "company-a", persistence: "postgresql",
      });
      if (path === "/api/workspace") return json({ kind: "legacy" });
      return json([]);
    });
    return { calls, setSession: (value: object) => { current = value; } };
  }

  await t.test("sign-in has no unauthenticated business reads, supports 320px/dark/zoom and explicit unavailable notice", async () => {
    const page = await browser.newPage();
    const { calls } = await setup(page);
    await page.addInitScript(() => { Object.defineProperty(window, "isSecureContext", { value: false }); });
    await page.setViewportSize({ width: 320, height: 640 });
    await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
    await page.goto(origin);
    await page.getByRole("heading", { name: "会社のワークスペースへ" }).waitFor();
    assert.equal(calls.some((call) => !call.path.startsWith("/api/auth/")), false);
    assert.equal(await page.getByRole("button", { name: "パスキーでログイン" }).isDisabled(), true);
    assert.match(await page.locator("#auth-passkey-unavailable").textContent() ?? "", /HTTPS/);
    assert.equal(await page.locator("#auth-username").getAttribute("autocomplete"), "username");
    assert.equal(await page.locator("#auth-password").getAttribute("autocomplete"), "current-password");
    await page.getByLabel("ログインID", { exact: true }).focus();
    await page.keyboard.press("Tab");
    assert.equal(await page.locator("#auth-password").evaluate((element) => element === document.activeElement), true);
    assert.ok(await page.getByRole("button", { name: "IDとパスワードでログイン" }).evaluate((element) => element.getBoundingClientRect().height >= 44));
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.screenshot({ path: resolve(artifacts, "login-320-dark.png"), fullPage: true });
    await assertVisualAccessibility(page);
    await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.close();
  });

  await t.test("password login, inline reauthentication/deletion, password change and focus restoration", async () => {
    const page = await browser.newPage();
    const { calls } = await setup(page);
    await page.goto(origin);
    await page.getByLabel("ログインID", { exact: true }).fill("operator-a");
    await page.getByLabel("パスワード", { exact: true }).fill("  original password  ");
    await page.getByRole("button", { name: "IDとパスワードでログイン" }).click();
    await page.getByRole("banner", { name: "会社とアカウント" }).waitFor();
    await page.getByText("ID・パスワード / パスキー", { exact: true }).waitFor();
    assert.equal(await page.getByText("担当者共通の HTTP Basic 認証", { exact: true }).count(), 0);
    assert.equal(calls.find((call) => call.path.endsWith("/login"))?.body?.password, "  original password  ");
    const settingsButton = page.getByRole("button", { name: "セキュリティ設定", exact: true });
    await settingsButton.click();
    await page.getByRole("heading", { name: "仕事用iPad", exact: true }).waitFor();
    assert.equal(await page.locator("#auth-security-title").evaluate((element) => element === document.activeElement), true);
    await page.locator("#auth-passkey-name").fill("🔑".repeat(81));
    assert.equal(await page.locator("#auth-passkey-name").getAttribute("aria-invalid"), "true");
    assert.equal(await page.getByRole("button", { name: "パスキーを追加", exact: true }).isDisabled(), true);
    await page.locator("#auth-passkey-name").fill("🔑".repeat(80));
    assert.equal(await page.getByRole("button", { name: "パスキーを追加", exact: true }).isEnabled(), true);
    await page.locator("#auth-passkey-name").fill("");
    await page.screenshot({ path: resolve(artifacts, "security-ipad-light.png"), fullPage: true });
    await assertVisualAccessibility(page);
    assert.equal(await page.locator("#auth-passkey-name").evaluate((element) => element === document.activeElement), true);
    await page.setViewportSize({ width: 320, height: 640 });
    await page.emulateMedia({ colorScheme: "dark" });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.screenshot({ path: resolve(artifacts, "security-320-dark.png"), fullPage: true });
    await assertVisualAccessibility(page);
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.emulateMedia({ colorScheme: "light" });
    await page.getByRole("button", { name: "パスキー「仕事用iPad」を削除" }).click();
    assert.equal(await page.getByRole("button", { name: "取り消す", exact: true }).evaluate((element) => element === document.activeElement), true);
    assert.equal(await page.getByRole("dialog").count(), 0);
    await page.getByRole("button", { name: "このパスキーを削除", exact: true }).click();
    await page.getByRole("heading", { name: "パスワードで本人確認" }).waitFor();
    assert.equal(await page.locator("#auth-reauth-password").evaluate((element) => element === document.activeElement), true);
    await page.locator("#auth-reauth-password").fill(" reauth password ");
    await page.getByRole("button", { name: "本人確認して続ける" }).click();
    await page.getByText("登録されたパスキーはありません。", { exact: true }).waitFor();
    assert.equal(calls.filter((call) => call.method === "DELETE").length, 2);
    assert.equal(calls.find((call) => call.path.endsWith("/reauth"))?.body?.password, " reauth password ");
    await page.locator("#auth-current-password").fill(" old password ");
    await page.locator("#auth-new-password").fill(" new long password ");
    await page.locator("#auth-confirm-password").fill(" new long password ");
    await page.getByRole("button", { name: "パスワードを変更", exact: true }).click();
    await page.getByText("パスワードを変更しました。他のログイン済みセッションは無効になりました。", { exact: true }).waitFor();
    assert.deepEqual(calls.find((call) => call.path === "/api/auth/password")?.body, {
      current_password: " old password ", new_password: " new long password ",
    });
    assert.equal(await page.locator("#auth-current-password").inputValue(), "");
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    await page.getByRole("button", { name: "業務画面に戻る" }).click();
    await page.waitForFunction(() => document.activeElement?.textContent?.startsWith("セキュリティ設定"));
    await page.getByRole("button", { name: "ログアウト", exact: true }).click();
    await page.getByRole("heading", { name: "会社のワークスペースへ" }).waitFor();
    assert.equal(await page.getByRole("banner", { name: "会社とアカウント" }).count(), 0);
    await page.close();
  });

  await t.test("cancelled authenticator is not successful login", async () => {
    const page = await browser.newPage();
    const { calls } = await setup(page);
    await page.addInitScript(() => {
      navigator.credentials.get = async () => { throw new DOMException("Synthetic cancellation", "NotAllowedError"); };
    });
    await page.goto(origin);
    await page.getByRole("button", { name: "パスキーでログイン", exact: true }).click();
    await page.getByText("パスキーの操作を完了しませんでした。もう一度試すか、パスワードをご利用ください。", { exact: true }).waitFor();
    assert.equal(calls.some((call) => call.path.endsWith("/verify")), false);
    assert.equal(calls.some((call) => !call.path.startsWith("/api/auth/")), false);
    await page.close();
  });

  await t.test("offline discovery closes workspace until an explicit successful retry", async () => {
    const page = await browser.newPage();
    const { calls } = await setup(page);
    let offline = true;
    await page.route("**/api/auth/session", (route) => offline ? route.abort("internetdisconnected") : route.fallback());
    await page.goto(origin);
    await page.getByRole("heading", { name: "接続を確認してください", exact: true }).waitFor();
    assert.equal(calls.some((call) => !call.path.startsWith("/api/auth/")), false);
    offline = false;
    await page.getByRole("button", { name: "ログイン状態を再取得", exact: true }).click();
    await page.getByRole("heading", { name: "会社のワークスペースへ" }).waitFor();
    assert.equal(calls.some((call) => !call.path.startsWith("/api/auth/")), false);
    await page.close();
  });

  await t.test("workspace discovery failure never silently falls back to legacy data", async () => {
    const page = await browser.newPage();
    const { calls } = await setup(page, account);
    let unavailable = true;
    await page.route("**/api/workspace", (route) => unavailable
      ? route.fulfill({ status: 503, contentType: "application/json", body: "{}" })
      : route.fallback());
    await page.goto(origin);
    await page.getByRole("alert").filter({ hasText: "業務画面の種類を確認できません" }).waitFor();
    assert.equal(calls.some((call) => !call.path.startsWith("/api/auth/")), false);
    unavailable = false;
    await page.getByRole("button", { name: "再取得", exact: true }).click();
    await page.getByText("ID・パスワード / パスキー", { exact: true }).waitFor();
    assert.ok(calls.some((call) => call.path === "/api/workspace"));
    await page.close();
  });

  await t.test("tenant409 refreshes server state; 401 and cross-tab logout remove the workspace", async () => {
    const page = await browser.newPage();
    const fixture = await setup(page, account);
    await page.goto(origin);
    await page.getByRole("banner", { name: "会社とアカウント" }).waitFor();
    fixture.setSession({ ...account, tenant: secondTenant });
    await page.evaluate(async () => {
      const source = "/src/auth-api.ts";
      const authModule = await import(source);
      authModule.notifyAuthFailure(409, "tenant_changed");
    });
    await page.waitForFunction(() => document.querySelector<HTMLSelectElement>("#auth-company")?.value === "company-b");
    assert.equal(fixture.calls.filter((call) => !call.path.startsWith("/api/auth/") && call.method !== "GET").length, 0);
    await page.evaluate(async () => {
      const source = "/src/auth-api.ts";
      const authModule = await import(source);
      authModule.notifyAuthFailure(401, "authentication_required");
    });
    await page.getByRole("heading", { name: "会社のワークスペースへ" }).waitFor();
    assert.equal(await page.getByRole("banner", { name: "会社とアカウント" }).count(), 0);
    await page.reload();
    await page.getByRole("banner", { name: "会社とアカウント" }).waitFor();
    await page.evaluate(() => {
      const channel = new BroadcastChannel("staff-matching-auth");
      channel.postMessage({ type: "auth-invalidated", reason: "logout" });
      channel.close();
    });
    await page.getByRole("heading", { name: "会社のワークスペースへ" }).waitFor();
    await page.close();
  });

  await t.test("legacy mode renders the original App without account UI", async () => {
    const page = await browser.newPage();
    await setup(page, { enabled: false, authenticated: false });
    await page.goto(origin);
    await page.getByRole("heading", { name: "スタッフ配置検討", exact: true }).waitFor();
    await page.getByText("担当者共通の HTTP Basic 認証", { exact: true }).waitFor();
    assert.equal(await page.getByRole("banner", { name: "会社とアカウント" }).count(), 0);
    assert.equal(await page.getByRole("button", { name: /セキュリティ設定/ }).count(), 0);
    await page.close();
  });
  await t.test("touch sign-in and inline-confirmation close preserve draft, keyboard focus and tablet/narrow reflow", async () => {
    const touch = await engine.newContext(tabletOptions());
    touch.setDefaultTimeout(8_000);
    try {
      const page = await touch.newPage();
      const { calls } = await setup(page);
      await page.goto(origin);
      await assertTouchEnvironment(page);
      await page.getByLabel("ログインID", { exact: true }).tap();
      await page.getByLabel("ログインID", { exact: true }).fill("operator-a");
      await page.getByLabel("パスワード", { exact: true }).fill(" synthetic touch password ");
      await page.getByRole("button", { name: "IDとパスワードでログイン", exact: true }).tap();
      const settings = page.getByRole("button", { name: "セキュリティ設定", exact: true });
      await settings.tap();
      await assertFocused(page.locator("#auth-security-title"));
      const draft = page.locator("#auth-passkey-name");
      await draft.fill("未送信の端末名（架空）");
      const remove = page.getByRole("button", { name: "パスキー「仕事用iPad」を削除", exact: true });
      await remove.tap();
      const cancel = page.getByRole("button", { name: "取り消す", exact: true });
      await assertFocused(cancel);
      await page.setViewportSize({ width: 768, height: 1024 });
      await assertFocused(cancel);
      await page.keyboard.press("Tab");
      await assertFocused(page.getByRole("button", { name: "このパスキーを削除", exact: true }));
      await page.keyboard.press("Shift+Tab");
      await assertFocused(cancel);
      await cancel.tap();
      await assertFocused(remove);
      assert.equal(await page.getByRole("heading", { name: "「仕事用iPad」を削除しますか？", exact: true }).count(), 0);
      assert.equal(await page.getByRole("dialog").count(), 0);
      assert.equal(await draft.inputValue(), "未送信の端末名（架空）");
      await page.screenshot({ path: resolve(artifacts, "touch-security-768-portrait.png"), fullPage: true });
      await page.screenshot({ path: resolve(artifacts, "touch-security-focus-768.png") });
      await draft.tap();
      await page.setViewportSize({ width: 320, height: 740 });
      await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
      await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
      await assertFocused(draft);
      assert.equal(await draft.inputValue(), "未送信の端末名（架空）");
      assert.deepEqual(await page.evaluate(() => ({
        width: innerWidth, overflow: document.documentElement.scrollWidth > innerWidth,
      })), { width: 320, overflow: false });
      await page.screenshot({ path: resolve(artifacts, "touch-security-320-dark-200.png"), fullPage: true });
      await draft.scrollIntoViewIfNeeded();
      await page.screenshot({ path: resolve(artifacts, "touch-security-focus-320.png") });
      await page.evaluate(() => { document.documentElement.style.fontSize = ""; });
      await page.setViewportSize({ width: 1024, height: 768 });
      await page.getByRole("button", { name: "業務画面に戻る", exact: true }).tap();
      await assertFocused(settings);
      await page.keyboard.press("Tab");
      await assertFocused(page.getByRole("button", { name: "ログアウト", exact: true }));
      assert.equal(calls.filter((call) => call.method !== "GET").length, 1);
      assert.equal(calls.some((call) => call.method === "DELETE" || call.path.endsWith("/verify")), false);
    } finally { await touch.close(); }
  });
});
