import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { chromium, webkit } from "playwright";
import type { Browser, BrowserContextOptions, LaunchOptions, Locator, Page } from "playwright";

export type TestBrowser = "chrome" | "webkit";
export function liveEnvironment(environment: NodeJS.ProcessEnv = process.env) {
  const subscription = environment.AZURE_SUBSCRIPTION_ID;
  const group = environment.STAFF_LIVE_RESOURCE_GROUP;
  const job = environment.STAFF_LIVE_SETUP_JOB;
  const origin = environment.STAFF_LIVE_ORIGIN;
  assert.ok(subscription && group && job && origin, "Configure AZURE_SUBSCRIPTION_ID, STAFF_LIVE_RESOURCE_GROUP, STAFF_LIVE_SETUP_JOB and STAFF_LIVE_ORIGIN before live tests.");
  return { subscription, group, job, origin };
}
export function selectBrowser(value = process.env.STAFF_BROWSER): TestBrowser {
  if (value === undefined) return "chrome";
  if (value === "chrome" || value === "webkit") return value;
  throw new Error("STAFF_BROWSER must be chrome or webkit; no browser fallback is permitted.");
}
export function browserEngine(name: TestBrowser) {
  return name === "chrome" ? chromium : webkit;
}
export function launchOptions(name: TestBrowser, runtime: string, chromeChannel = "chrome"): LaunchOptions {
  return { headless: true, downloadsPath: runtime, ...(name === "chrome" ? { channel: chromeChannel } : {}) };
}
// Touch/mobile layout emulation, not an iPadOS/Safari version or a physical-device assertion.
export function tabletOptions(viewport = { width: 1024, height: 768 }): BrowserContextOptions {
  return { viewport, hasTouch: true, isMobile: true, deviceScaleFactor: 2, colorScheme: "light", reducedMotion: "reduce" };
}
export function reportBrowser(browser: Browser, name: TestBrowser) {
  const require = createRequire(import.meta.url);
  console.info(JSON.stringify({
    stage: "browser-configuration", engine: name, browser_version: browser.version(),
    playwright_version: require("playwright/package.json").version,
  }));
}
export async function assertTouchEnvironment(page: Page) {
  const capabilities = await page.evaluate(() => ({
    touch_events: "ontouchstart" in window, coarse_pointer: matchMedia("(pointer: coarse)").matches,
    reduced_motion: matchMedia("(prefers-reduced-motion: reduce)").matches,
    max_touch_points: navigator.maxTouchPoints, device_scale_factor: devicePixelRatio,
  }));
  // macOS Playwright WebKit reports zero maxTouchPoints despite dispatching touch events.
  assert.ok(capabilities.touch_events && capabilities.coarse_pointer && capabilities.reduced_motion);
  assert.equal(capabilities.device_scale_factor, 2);
  console.info(JSON.stringify({ stage: "touch-emulation", ...capabilities }));
}
export async function assertFocused(locator: Locator) {
  await locator.waitFor({ state: "visible" });
  const element = await locator.elementHandle();
  assert.ok(element);
  try { await locator.page().waitForFunction((target) => document.activeElement === target, element); }
  finally { await element.dispose(); }
}

export async function assertVisualAccessibility(page: Page) {
  const result = await page.evaluate(() => {
    type Color = [number, number, number, number];
    function color(value: string): Color {
      if (!/^rgba?\(/.test(value)) throw new Error(`Unsupported computed color: ${value}`);
      const parts = value.match(/[\d.]+/g)?.map(Number);
      if (!parts || parts.length < 3) throw new Error(`Invalid computed color: ${value}`);
      return [parts[0]!, parts[1]!, parts[2]!, parts[3] ?? 1];
    }
    function over(front: Color, back: Color): Color {
      const channel = (i: 0 | 1 | 2) => front[i] * front[3] + back[i] * (1 - front[3]);
      return [channel(0), channel(1), channel(2), 1];
    }
    function background(element: Element): Color {
      const parents: Element[] = [];
      for (let current: Element | null = element; current; current = current.parentElement) parents.unshift(current);
      return parents.reduce<Color>((base, parent) => over(color(getComputedStyle(parent).backgroundColor), base), [255, 255, 255, 1]);
    }
    function luminance(value: Color) {
      const channels = value.slice(0, 3).map((item) => {
        const channel = item / 255;
        return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
      });
      return channels[0]! * .2126 + channels[1]! * .7152 + channels[2]! * .0722;
    }
    function contrast(front: Color, back: Color) {
      const a = luminance(over(front, back)); const b = luminance(back);
      return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
    }
    function visible(element: Element) {
      return element.getClientRects().length > 0 && getComputedStyle(element).visibility === "visible"
        && !element.closest('[hidden], .sr-only, .leaflet-container, :disabled');
    }
    const failures: string[] = [];
    let textCount = 0; let iconCount = 0; let controlCount = 0; let minimumTextContrast = Infinity;
    for (const element of document.querySelectorAll(".saas-workspace *, .auth-ui *")) {
      if (!visible(element)) continue;
      const style = getComputedStyle(element);
      if (element.classList.contains("ui-icon")) {
        iconCount++;
        if (element.getAttribute("aria-hidden") !== "true" || element.getAttribute("focusable") !== "false") {
          failures.push("A decorative icon is exposed to assistive technology or keyboard focus.");
        }
        const ratio = contrast(color(style.color), background(element));
        if (ratio < 3) failures.push(`Icon contrast ${ratio}: ${element.parentElement?.className}`);
      }
      const directText = [...element.childNodes].some((node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim());
      if (directText && !element.closest('[aria-hidden="true"]') && element.tagName !== "OPTION") {
        const ratio = contrast(color(style.color), background(element));
        textCount++; minimumTextContrast = Math.min(minimumTextContrast, ratio);
        if (ratio < 4.5) failures.push(`Text contrast ${ratio}: ${element.tagName}.${element.getAttribute("class") ?? ""}`);
      }
      if (element.matches('input:not([type="checkbox"]):not([type="radio"]), select')) {
        controlCount++;
        const ratio = contrast(color(style.borderTopColor), background(element.parentElement!));
        if (ratio < 3) failures.push(`Input boundary contrast ${ratio}: ${element.tagName}`);
        const textRatio = contrast(color(style.color), background(element));
        textCount++; minimumTextContrast = Math.min(minimumTextContrast, textRatio);
        if (textRatio < 4.5) failures.push(`Input text contrast ${textRatio}: ${element.tagName}`);
        if (element instanceof HTMLInputElement && element.placeholder && !element.value) {
          const placeholderRatio = contrast(color(getComputedStyle(element, "::placeholder").color), background(element));
          textCount++; minimumTextContrast = Math.min(minimumTextContrast, placeholderRatio);
          if (placeholderRatio < 4.5) failures.push(`Placeholder contrast ${placeholderRatio}: INPUT`);
        }
      }
      if (element.matches("button") && !element.textContent?.trim()) failures.push("A button lost its visible text label.");
      if (element.matches(":focus-visible") && style.outlineStyle !== "none") {
        const ratio = contrast(color(style.outlineColor), background(element.parentElement!));
        if (ratio < 3) failures.push(`Focus contrast ${ratio}: ${element.tagName}`);
      }
    }
    return { failures, textCount, iconCount, controlCount, minimumTextContrast };
  });
  assert.ok(result.textCount > 0 && result.iconCount > 0, "The rendered UI must contain measurable labels and icons.");
  assert.deepEqual(result.failures, []);
  console.info(JSON.stringify({ stage: "rendered-contrast-and-icons", ...result }));
}
