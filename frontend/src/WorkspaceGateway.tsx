import { useEffect, useState } from "react";
import App from "./App.tsx";
import { getAuthTenantHeaders, notifyAuthFailure } from "./auth-api.ts";
import SaasWorkspace from "./SaasWorkspace.tsx";

export default function WorkspaceGateway({ readOnly }: { readOnly: boolean }) {
  const [kind, setKind] = useState<"loading" | "error" | "legacy" | "saas">("loading");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    const timeout = window.setTimeout(() => controller.abort(), 15_000);
    setKind("loading");
    void (async () => {
      try {
        const response = await fetch("/api/workspace", {
          credentials: "same-origin", cache: "no-store",
          headers: getAuthTenantHeaders(), signal: controller.signal,
        });
        if (response.status === 401) notifyAuthFailure(401, "authentication_required");
        if (response.status === 409) notifyAuthFailure(409, "tenant_changed");
        if (!response.ok) throw new Error("workspace_unavailable");
        const value: unknown = await response.json();
        if (typeof value !== "object" || value === null || !("kind" in value)
          || (value.kind !== "legacy" && value.kind !== "saas")) throw new Error("invalid_workspace");
        if (!controller.signal.aborted) setKind(value.kind);
      } catch {
        if (!cancelled) setKind("error");
      } finally { window.clearTimeout(timeout); }
    })();
    return () => { cancelled = true; controller.abort(); window.clearTimeout(timeout); };
  }, [attempt]);
  if (kind === "loading") return <main className="auth-ui auth-settings" aria-busy="true">
    <p role="status">業務画面を読み込んでいます…</p>
  </main>;
  if (kind === "error") return <main className="auth-ui auth-settings">
    <p role="alert">業務画面の種類を確認できません。接続を確認して再取得してください。</p>
    <button type="button" onClick={() => setAttempt((value) => value + 1)}>再取得</button>
  </main>;
  return kind === "saas" ? <SaasWorkspace readOnly={readOnly} /> : <App authentication="accounts" />;
}
