import assert from "node:assert/strict";
import test from "node:test";
import { launchOptions, liveEnvironment, selectBrowser, tabletOptions } from "./browser-support.ts";

test("live tests require an explicit environment without deployed-resource defaults", () => {
  const environment = {
    AZURE_SUBSCRIPTION_ID: "test-subscription",
    STAFF_LIVE_RESOURCE_GROUP: "test-group",
    STAFF_LIVE_SETUP_JOB: "test-job",
    STAFF_LIVE_ORIGIN: "https://app.example.invalid",
  };
  assert.deepEqual(liveEnvironment(environment), {
    subscription: "test-subscription", group: "test-group",
    job: "test-job", origin: "https://app.example.invalid",
  });
  for (const key of Object.keys(environment)) {
    for (const value of [undefined, ""]) {
      assert.throws(() => liveEnvironment({ ...environment, [key]: value }), /Configure/);
    }
  }
});

test("browser selection is explicit, validated and never silently falls back from WebKit", () => {
  assert.equal(selectBrowser("chrome"), "chrome");
  assert.equal(selectBrowser("webkit"), "webkit");
  for (const value of ["", "safari", "WebKit", "firefox", " chrome"]) {
    assert.throws(() => selectBrowser(value), /STAFF_BROWSER must be chrome or webkit/);
  }
  assert.equal(launchOptions("chrome", ".runtime").channel, "chrome");
  assert.equal("channel" in launchOptions("webkit", ".runtime", "chrome"), false);
  assert.deepEqual(tabletOptions().viewport, { width: 1024, height: 768 });
  assert.equal(tabletOptions().hasTouch, true);
  assert.equal(tabletOptions().isMobile, true);
});
