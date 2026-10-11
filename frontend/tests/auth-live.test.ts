import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import test from "node:test";
import { chromium } from "playwright";
import { assertVisualAccessibility, liveEnvironment, selectBrowser } from "./browser-support.ts";

// Explicit operator opt-in: registers and removes only its own synthetic test passkey.
test("live Azure password and signed virtual-authenticator UI", {
  skip: process.env.STAFF_AUTH_LIVE_TESTS !== "1", timeout: 180_000,
}, async () => {
  assert.equal(selectBrowser(), "chrome", "Signed virtual-authenticator acceptance requires Chrome CDP; WebKit is not substituted.");
  const root = resolve("..");
  const { subscription, group, job, origin } = liveEnvironment();
  const username = "coordinator10";
  const password = execFileSync("uv", [
    "run", "python", "-m", "scripts.poc_credentials", "--subscription", subscription,
    "--resource-group", group, "--job", job, "--username", username,
  ], { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  const artifacts = resolve(root, ".azure", "auth-browser-evidence");
  await mkdir(artifacts, { recursive: true });
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const context = await browser.newContext({
    baseURL: origin, viewport: { width: 1024, height: 768 }, reducedMotion: "reduce",
  });
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);
  const requestResults: { path: string; status: number }[] = [];
  page.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith("/api/auth/")) requestResults.push({ path, status: response.status() });
  });
  const cdp = await context.newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  const { authenticatorId } = await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2", transport: "internal", hasResidentKey: true,
      hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true,
    },
  });
  const name = `acceptance-${randomUUID()}`;
  const csrf = { Origin: origin, "X-Requested-With": "staff-matching" };
  let stage = "login";
  let completed = false;
  try {
    await page.goto("/");
    await page.getByRole("heading", { name: "会社のワークスペースへ" }).waitFor();
    await page.screenshot({ path: resolve(artifacts, "login-ipad.png"), fullPage: true });
    await assertVisualAccessibility(page);
    await page.setViewportSize({ width: 320, height: 640 });
    await page.emulateMedia({ colorScheme: "dark" });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: resolve(artifacts, "login-320-dark.png"), fullPage: true });
    await assertVisualAccessibility(page);
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.emulateMedia({ colorScheme: "light" });
    await page.getByLabel("ログインID", { exact: true }).fill(username);
    await page.getByLabel("パスワード", { exact: true }).fill(password);
    await page.getByRole("button", { name: "IDとパスワードでログイン", exact: true }).click();
    await page.getByRole("banner", { name: "会社とアカウント" }).waitFor();
    let session = await (await context.request.get("/api/auth/session")).json();
    assert.equal(session.tenant.id, "demo10");
    assert.equal(session.auth_method, "password");
    assert.equal((await context.request.get("/api/staff", {
      headers: { "X-Tenant-ID": "demo" },
    })).status(), 409);
    assert.equal((await context.request.post("/api/auth/passkeys/registration/options", {
      headers: { ...csrf, Origin: "https://untrusted.example" }, data: {},
    })).status(), 403);

    stage = "registration";
    await page.getByRole("button", { name: /^セキュリティ設定/ }).click();
    await page.locator("#auth-passkey-name").fill(name);
    await page.getByRole("button", { name: "パスキーを追加", exact: true }).click();
    await page.getByText("パスキーを登録しました。", { exact: true }).waitFor();
    await page.getByRole("heading", { name, exact: true }).waitFor();
    await page.screenshot({ path: resolve(artifacts, "security-ipad.png"), fullPage: true });
    await assertVisualAccessibility(page);
    await page.setViewportSize({ width: 320, height: 640 });
    await page.emulateMedia({ colorScheme: "dark" });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: resolve(artifacts, "security-320-dark.png"), fullPage: true });
    await assertVisualAccessibility(page);
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.emulateMedia({ colorScheme: "light" });
    await page.getByRole("button", { name: "ログアウト", exact: true }).click();
    await page.getByRole("heading", { name: "会社のワークスペースへ" }).waitFor();
    assert.equal((await context.request.get("/api/staff")).status(), 401);

    stage = "signed-passkey-login";
    await page.getByRole("button", { name: "パスキーでログイン", exact: true }).click();
    await page.getByRole("banner", { name: "会社とアカウント" }).waitFor();
    session = await (await context.request.get("/api/auth/session")).json();
    assert.equal(session.user.username, username);
    assert.equal(session.tenant.id, "demo10");
    assert.equal(session.auth_method, "passkey");
    const cookies = await context.cookies();
    const sessionCookie = cookies.find((cookie) => cookie.name === "__Host-staff-session");
    assert.ok(sessionCookie?.secure && sessionCookie.httpOnly);
    assert.equal(sessionCookie.sameSite, "Lax");
    assert.equal(await page.evaluate(() => localStorage.length), 0);

    stage = "remove-test-key";
    await page.getByRole("button", { name: /^セキュリティ設定/ }).click();
    await page.getByRole("button", { name: `パスキー「${name}」を削除`, exact: true }).click();
    await page.getByRole("button", { name: "このパスキーを削除", exact: true }).click();
    await page.getByText("パスキーを削除しました。パスワードでのログインは引き続き使えます。", {
      exact: true,
    }).waitFor();
    const keys = await (await context.request.get("/api/auth/passkeys")).json();
    assert.equal(keys.items.some((key: { name: string }) => key.name === name), false);
    completed = true;
  } catch {
    console.error(JSON.stringify({
      stage, requests: requestResults,
      feedback: await page.locator(".auth-feedback").allTextContents(),
    }));
    throw new Error(`Live authentication acceptance failed at ${stage}; credentials omitted.`);
  } finally {
    try {
      if (!completed) {
        const login = await context.request.post("/api/auth/login", {
          headers: csrf, data: { username, password },
        });
        if (login.ok()) {
          const keys = await (await context.request.get("/api/auth/passkeys")).json();
          for (const key of keys.items.filter((key: { name: string }) => key.name === name)) {
            const removed = await context.request.delete(`/api/auth/passkeys/${encodeURIComponent(key.id)}`, {
              headers: csrf,
            });
            assert.equal(removed.status(), 200, "Could not remove isolated acceptance passkey.");
          }
        } else {
          throw new Error("Acceptance cleanup could not authenticate.");
        }
      }
      const logout = await context.request.post("/api/auth/logout", { headers: csrf, data: {} });
      assert.equal(logout.status(), 200);
    } catch {
      throw new Error("Acceptance cleanup failed; inspect its named test key. Credentials omitted.");
    } finally {
      try { await cdp.send("WebAuthn.removeVirtualAuthenticator", { authenticatorId }); }
      finally { await browser.close(); }
    }
  }
});
