import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { FormEvent } from "react";
import App from "./App.tsx";
import WorkspaceGateway from "./WorkspaceGateway.tsx";
import {
  auth, AuthError, authErrorMessage, connectAuthInvalidation, isPasskeyCancellation,
  passkeyNameProblem, passkeyUnavailableReason, passwordProblem,
} from "./auth-api.ts";
import type { AccountSession, Passkey } from "./auth-api.ts";
import Icon from "./Icon.tsx";

type Feedback = { text: string; error?: boolean };
function Message({ feedback }: { feedback: Feedback | null }) {
  return <div className={`auth-feedback${feedback?.error ? " auth-feedback-error" : ""}`}
    role="status" aria-live="polite" aria-atomic="true">{feedback && <Icon name={feedback.error ? "warning" : "info"} />}{feedback?.text}</div>;
}

function PrivacyNote() {
  return <p className="auth-hint">顔・指紋を使う場合、その情報は端末内で処理され、このサービスには送られません。
    パスキーの秘密鍵は端末やパスキープロバイダーが管理し、サーバーには公開鍵を登録します。
    パスワードでもログインできるため、必須の二要素認証ではありません。</p>;
}

function SignIn({ notice }: { notice: string }) {
  const [busy, setBusy] = useState<"password" | "passkey" | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(notice ? { text: notice } : null);
  const [showPassword, setShowPassword] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const unavailable = passkeyUnavailableReason();
  useEffect(() => { heading.current?.focus(); }, []);
  useEffect(() => { if (notice) setFeedback({ text: notice }); }, [notice]);
  useEffect(() => () => auth.cancelPasskey(), []);

  async function passwordLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const form = event.currentTarget;
    const fields = new FormData(form);
    const username = String(fields.get("username") ?? "");
    const password = String(fields.get("password") ?? "");
    setBusy("password");
    setFeedback(null);
    try { await auth.login(username, password); } catch (error) {
      setFeedback({ text: authErrorMessage(error), error: true });
    } finally {
      const field = form.elements.namedItem("password");
      if (field instanceof HTMLInputElement) field.value = "";
      setBusy(null);
    }
  }

  async function passkeyLogin() {
    if (busy) return;
    setBusy("passkey");
    setFeedback({ text: "端末に表示される案内に従ってください。" });
    try { await auth.loginWithPasskey(); } catch (error) {
      setFeedback({ text: authErrorMessage(error), error: !isPasskeyCancellation(error) });
    } finally { setBusy(null); }
  }

  return <main className="auth-ui auth-screen">
    <section className="auth-card" aria-labelledby="auth-title">
      <div className="auth-brand-symbol"><Icon name="match" /></div>
      <p className="auth-eyebrow">STAFF MATCHING · PoC</p>
      <h1 id="auth-title" tabIndex={-1} ref={heading}>会社のワークスペースへ</h1>
      <p className="auth-intro">管理会社ごとの案件・スタッフ情報を守るため、ログインしてください。
        初回は管理者から案内されたIDとパスワードを使います。</p>
      <Message feedback={feedback} />
      <form onSubmit={(event) => { void passwordLogin(event); }} aria-busy={busy === "password"}>
        <label htmlFor="auth-username">ログインID</label>
        <input id="auth-username" name="username" autoComplete="username" autoCapitalize="none"
          spellCheck={false} required disabled={!!busy} />
        <label htmlFor="auth-password">パスワード</label>
        <input id="auth-password" name="password" type={showPassword ? "text" : "password"}
          autoComplete="current-password" required disabled={!!busy} />
        <label className="auth-check">
          <input type="checkbox" checked={showPassword} onChange={(event) => setShowPassword(event.target.checked)} />
          パスワードを表示
        </label>
        <button className="auth-primary auth-full" type="submit" disabled={!!busy}>
          <Icon name="lock" />
          {busy === "password" ? "ログインしています…" : "IDとパスワードでログイン"}
        </button>
      </form>
      <div className="auth-divider"><span>登録済みの方</span></div>
      <button type="button" className="auth-full" disabled={!!busy || !!unavailable}
        aria-describedby={unavailable ? "auth-passkey-unavailable" : "auth-passkey-help"}
        onClick={() => { void passkeyLogin(); }}>
        <Icon name="key" />
        {busy === "passkey" ? "パスキーを確認しています…" : "パスキーでログイン"}
      </button>
      {busy === "passkey" && <button className="auth-full" type="button" onClick={() => auth.cancelPasskey()}>パスキー操作を中止</button>}
      {unavailable
        ? <p className="auth-hint" id="auth-passkey-unavailable">{unavailable}</p>
        : <p className="auth-hint" id="auth-passkey-help">パスキーはログイン後の「セキュリティ設定」で登録できます。</p>}
      <PrivacyNote />
      <p className="auth-hint">IDやパスワードがわからない場合は、所属する管理会社の管理者にご連絡ください。</p>
    </section>
  </main>;
}

type CredentialAction = { kind: "register"; name: string } | { kind: "remove"; passkey: Passkey };
const dateTime = (date: string) => new Intl.DateTimeFormat("ja-JP", {
  dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Tokyo",
}).format(new Date(date));

function SecuritySettings({ session, onClose }: { session: AccountSession; onClose: () => void }) {
  const [items, setItems] = useState<Passkey[] | null>(null);
  const [listError, setListError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [pending, setPending] = useState<CredentialAction | null>(null);
  const [confirmRemove, setConfirmRemove] = useState<Passkey | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [name, setName] = useState("");
  const nameProblem = passkeyNameProblem(name);
  const heading = useRef<HTMLHeadingElement>(null);
  const reauthInput = useRef<HTMLInputElement>(null);
  const cancelRemoval = useRef<HTMLButtonElement>(null);
  const addInput = useRef<HTMLInputElement>(null);
  const removeTrigger = useRef<HTMLButtonElement | null>(null);
  const mounted = useRef(true);
  const loadSequence = useRef(0);
  const unavailable = passkeyUnavailableReason();
  const locked = busy || !!pending || !!confirmRemove || uncertain || loading;

  async function loadKeys() {
    const sequence = ++loadSequence.current;
    setLoading(true);
    setListError("");
    try {
      const result = await auth.listPasskeys();
      if (mounted.current && sequence === loadSequence.current) { setItems(result); setUncertain(false); }
    } catch (error) { if (mounted.current && sequence === loadSequence.current) setListError(authErrorMessage(error)); }
    finally { if (mounted.current && sequence === loadSequence.current) setLoading(false); }
  }

  useEffect(() => {
    mounted.current = true;
    heading.current?.focus();
    void loadKeys();
    return () => { mounted.current = false; loadSequence.current += 1; auth.cancelPasskey(); };
  }, []);
  useEffect(() => { if (pending) reauthInput.current?.focus(); }, [pending]);
  useEffect(() => { if (confirmRemove) cancelRemoval.current?.focus(); }, [confirmRemove]);

  const restoreActionFocus = (action: CredentialAction) => {
    if (action.kind === "register") addInput.current?.focus();
    else if (removeTrigger.current?.isConnected) removeTrigger.current.focus();
    else heading.current?.focus();
  };

  async function execute(action: CredentialAction) {
    setBusy(true);
    setFeedback(null);
    try {
      if (action.kind === "register") await auth.registerPasskey(action.name);
      else await auth.removePasskey(action.passkey.id);
      if (!mounted.current) return;
      setPending(null);
      setConfirmRemove(null);
      if (action.kind === "register") setName("");
      setFeedback({ text: action.kind === "register" ? "パスキーを登録しました。" : "パスキーを削除しました。パスワードでのログインは引き続き使えます。" });
      await loadKeys();
      heading.current?.focus();
    } catch (error) {
      if (!mounted.current) return;
      setConfirmRemove(null);
      if (error instanceof AuthError && error.code === "reauthentication_required") {
        setPending(action);
        setFeedback({ text: authErrorMessage(error) });
      } else {
        setPending(null);
        const unknownResult = error instanceof AuthError && (error.code === "uncertain" || error.code === "invalid_response");
        setUncertain(unknownResult);
        setFeedback({ text: authErrorMessage(error), error: !isPasskeyCancellation(error) });
        if (unknownResult) heading.current?.focus();
        else window.requestAnimationFrame(() => restoreActionFocus(action));
      }
    } finally { if (mounted.current) setBusy(false); }
  }

  async function reauthenticate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!pending || busy) return;
    const action = pending;
    const form = event.currentTarget;
    const password = String(new FormData(form).get("reauth-password") ?? "");
    form.reset();
    setBusy(true);
    setFeedback(null);
    try {
      await auth.reauthenticate(password);
      // Registration starts afresh: never replay a consumed signed credential.
      await execute(action);
    } catch (error) {
      if (mounted.current) {
        setFeedback({ text: authErrorMessage(error), error: true });
        window.requestAnimationFrame(() => reauthInput.current?.focus());
      }
    } finally { if (mounted.current) setBusy(false); }
  }

  async function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (locked) return;
    const form = event.currentTarget;
    const data = new FormData(form);
    const current = String(data.get("current-password") ?? "");
    const next = String(data.get("new-password") ?? "");
    const problem = passwordProblem(next, String(data.get("confirm-password") ?? ""));
    if (problem) { setFeedback({ text: problem, error: true }); return; }
    setBusy(true);
    setFeedback(null);
    try {
      await auth.changePassword(current, next);
      setFeedback({ text: "パスワードを変更しました。他のログイン済みセッションは無効になりました。" });
    } catch (error) { setFeedback({ text: authErrorMessage(error), error: true }); }
    finally { form.reset(); if (mounted.current) setBusy(false); }
  }

  return <main className="auth-ui auth-settings" aria-labelledby="auth-security-title">
    <div className="auth-section-heading">
      <div><p className="auth-eyebrow">アカウントの保護</p>
        <h1 className="ui-heading" id="auth-security-title" ref={heading} tabIndex={-1}><Icon name="shield" />セキュリティ設定</h1></div>
      <button type="button" onClick={onClose} disabled={busy}><Icon name="left" />業務画面に戻る</button>
    </div>
    <p className="auth-hint">{session.user.display_name}（{session.user.username}）のログイン設定です。
      所属するすべての会社で共通です。</p>
    <Message feedback={feedback} />
    {pending && <section className="auth-inset" aria-labelledby="auth-reauth-title">
      <h2 id="auth-reauth-title">パスワードで本人確認</h2>
      <p>{pending.kind === "register" ? "パスキーの登録" : `「${pending.passkey.name}」の削除`}を続けます。
        パスキーの登録時は、本人確認後に端末の案内がもう一度表示されることがあります。</p>
      <form onSubmit={(event) => { void reauthenticate(event); }} aria-busy={busy}>
        <input name="username" type="hidden" autoComplete="username" value={session.user.username} />
        <label htmlFor="auth-reauth-password">現在のパスワード</label>
        <input ref={reauthInput} id="auth-reauth-password" name="reauth-password" type="password"
          autoComplete="current-password" required disabled={busy} />
        <div className="auth-actions">
          <button type="submit" className="auth-primary" disabled={busy}>{busy ? "確認しています…" : "本人確認して続ける"}</button>
          <button type="button" disabled={busy} onClick={() => {
            const action = pending; setPending(null); setFeedback({ text: "操作を取り消しました。" });
            window.requestAnimationFrame(() => restoreActionFocus(action));
          }}>取り消す</button>
        </div>
      </form>
    </section>}
    <div className="auth-settings-grid">
      <section className="auth-card" aria-labelledby="auth-keys-title">
        <h2 id="auth-keys-title" className="ui-heading"><Icon name="key" />パスキー</h2>
        <PrivacyNote />
        <p className="auth-hint">このサイトのアドレスに対して登録します。運用先のアドレスが変わる場合は再登録が必要です。</p>
        <div aria-live="polite" aria-busy={loading}>
          {loading && <p>パスキーを読み込んでいます…</p>}
          {listError && <p className="auth-feedback-error">{listError}</p>}
          {!loading && items?.length === 0 && <p className="auth-empty">登録されたパスキーはありません。</p>}
        </div>
        {!loading && items && <ul className="auth-key-list">
          {items.map((item) => <li key={item.id}>
            <h3>{item.name}</h3>
            <dl>
              <div><dt>登録日時</dt><dd>{dateTime(item.created_at)}（日本時間）</dd></div>
              <div><dt>最終利用</dt><dd>{item.last_used_at ? `${dateTime(item.last_used_at)}（日本時間）` : "まだ利用していません"}</dd></div>
              <div><dt>種類</dt><dd>{item.device_type === "multiDevice" ? "同期に対応" : "端末固定"}</dd></div>
              <div><dt>バックアップ</dt><dd>{item.backed_up ? "あり（認証器からの報告）" : "未確認／なし"}</dd></div>
            </dl>
            <button type="button" className="auth-danger" disabled={locked}
              aria-label={`パスキー「${item.name}」を削除`}
              onClick={(event) => { removeTrigger.current = event.currentTarget; setConfirmRemove(item); }}>削除…</button>
          </li>)}
        </ul>}
        {confirmRemove && <section className="auth-inset" aria-labelledby="auth-remove-title">
          <h3 id="auth-remove-title">「{confirmRemove.name}」を削除しますか？</h3>
          <p>このパスキーではログインできなくなります。パスワードでのログインは残ります。
            端末のパスキー管理画面からの削除は別途必要です。</p>
          <div className="auth-actions">
            <button ref={cancelRemoval} type="button" disabled={busy} onClick={() => {
              setConfirmRemove(null);
              window.requestAnimationFrame(() => removeTrigger.current?.focus());
            }}>取り消す</button>
            <button type="button" className="auth-danger" disabled={busy}
              onClick={() => { void execute({ kind: "remove", passkey: confirmRemove }); }}>
              {busy ? "削除しています…" : "このパスキーを削除"}
            </button>
          </div>
        </section>}
        {(listError || uncertain) && <button type="button" disabled={busy || loading}
          onClick={() => { void loadKeys(); }}>登録状態を再取得</button>}
        {uncertain && <p className="auth-hint">結果が未確認です。登録状態を再取得するまで、追加・削除はできません。</p>}
        <form onSubmit={(event) => {
          event.preventDefault();
          if (!locked && !unavailable && !nameProblem) void execute({ kind: "register", name });
        }}
          aria-busy={busy}>
          <label htmlFor="auth-passkey-name">新しいパスキーの名前</label>
          <input id="auth-passkey-name" ref={addInput} value={name}
            placeholder="例：仕事用のiPad" autoComplete="off"
            aria-invalid={name.length > 0 && !!nameProblem}
            aria-describedby="auth-key-name-hint auth-key-name-rule"
            onChange={(event) => setName(event.target.value)} disabled={locked || !!unavailable} required />
          <p className="auth-hint" id="auth-key-name-hint">あとで見分けられる名前を付けてください。機密情報は入力しないでください。</p>
          <p className="auth-hint" id="auth-key-name-rule" aria-live="polite">
            {name.length > 0 && nameProblem ? nameProblem : "1〜80文字。入力した空白も名前に含まれます。"}
          </p>
          <button type="submit" className="auth-primary" disabled={locked || !!unavailable || !!nameProblem}
            aria-describedby={unavailable ? "auth-enrollment-unavailable" : "auth-key-name-hint"}>
            <Icon name="key" />
            {busy ? "処理しています…" : "パスキーを追加"}
          </button>
        </form>
        {unavailable && <p id="auth-enrollment-unavailable" className="auth-hint">{unavailable}</p>}
      </section>
      <section className="auth-card" aria-labelledby="auth-password-title">
        <h2 id="auth-password-title" className="ui-heading"><Icon name="lock" />パスワードを変更</h2>
        <p className="auth-hint" id="auth-password-rule">15〜128文字。他のサービスと異なる長いパスワードを使ってください。
          空白も文字として扱います。変更すると他のログイン済みセッションは無効になります。</p>
        <form onSubmit={(event) => { void changePassword(event); }} aria-busy={busy}>
          <input name="username" type="hidden" autoComplete="username" value={session.user.username} />
          <label htmlFor="auth-current-password">現在のパスワード</label>
          <input id="auth-current-password" name="current-password" type="password" autoComplete="current-password" required disabled={locked} />
          <label htmlFor="auth-new-password">新しいパスワード</label>
          <input id="auth-new-password" name="new-password" type="password" autoComplete="new-password"
            aria-describedby="auth-password-rule" required disabled={locked} />
          <label htmlFor="auth-confirm-password">新しいパスワード（確認）</label>
          <input id="auth-confirm-password" name="confirm-password" type="password" autoComplete="new-password" required disabled={locked} />
          <button type="submit" className="auth-primary" disabled={locked}><Icon name="lock" />{busy ? "処理しています…" : "パスワードを変更"}</button>
        </form>
      </section>
    </div>
  </main>;
}

function AccountWorkspace({ session, notice }: {
  session: AccountSession; notice: string;
}) {
  const [settings, setSettings] = useState(false);
  const settingsButton = useRef<HTMLButtonElement>(null);
  const roleNames: Record<string, string> = {
    tenant_admin: "会社管理者", admin: "会社管理者", coordinator: "コーディネーター", viewer: "閲覧専用", readonly: "閲覧専用",
  };
  return <>
    <header className="auth-ui auth-account-bar" aria-label="会社とアカウント">
      <div className="auth-company-identity">
        <span className="auth-company-symbol"><Icon name="building" /></span>
        <div className="auth-account-company">
          <span className="auth-eyebrow">利用中の管理会社</span><strong>{session.tenant.name}</strong>
          <span className="auth-hint">{roleNames[session.tenant.role] ?? session.tenant.role}</span>
        </div>
      </div>
      {session.tenants.length > 1 && <div className="auth-company-picker">
        <label htmlFor="auth-company">管理会社を切り替え</label>
        <select id="auth-company" value={session.tenant.id} aria-describedby="auth-switch-note"
          onChange={(event) => {
            if (event.target.value !== session.tenant.id) void auth.switchTenant(event.target.value).catch(() => {});
          }}>
          {session.tenants.map((tenant) => <option value={tenant.id} key={tenant.id}>{tenant.name}</option>)}
        </select>
        <span id="auth-switch-note" className="auth-hint">切替時に業務画面の選択・未保存の入力を閉じます。</span>
      </div>}
      <div className="auth-account-controls">
        <span><Icon name="person" /> {session.user.display_name}<span className="auth-hint auth-account-id">{session.user.username}
          {" · "}{session.auth_method === "passkey" ? "パスキーでログイン中" : "パスワードでログイン中"}</span></span>
        <div className="auth-actions">
          <button type="button" ref={settingsButton} aria-expanded={settings}
            onClick={() => {
              setSettings(!settings);
              if (settings) window.requestAnimationFrame(() => settingsButton.current?.focus());
            }}><Icon name="shield" />セキュリティ設定{session.passkey_count === 0 ? " · パスキー未登録" : ""}</button>
          <button type="button" onClick={() => { void auth.logout().catch(() => {}); }}><Icon name="logout" />ログアウト</button>
        </div>
      </div>
    </header>
    {notice && <div className="auth-ui auth-account-notice"><Message feedback={{ text: notice }} /></div>}
    {settings && <SecuritySettings session={session} onClose={() => {
      setSettings(false);
      window.requestAnimationFrame(() => settingsButton.current?.focus());
    }} />}
    <div hidden={settings}>
      <WorkspaceGateway readOnly={session.tenant.role === "viewer"}
        key={JSON.stringify([session.user.id, session.tenant.id])} />
    </div>
  </>;
}

export default function AuthGate() {
  const state = useSyncExternalStore(auth.subscribe, auth.getSnapshot, auth.getSnapshot);
  const recoveryHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    const disconnect = connectAuthInvalidation();
    void auth.refresh();
    return disconnect;
  }, []);
  useEffect(() => { if (state.kind === "error") recoveryHeading.current?.focus(); }, [state.kind]);

  if (state.kind === "loading") return <main className="auth-ui auth-screen" aria-busy="true">
    <section className="auth-card"><h1>スタッフマッチング</h1>
      <p role="status" aria-live="polite">{state.message}</p></section>
  </main>;
  if (state.kind === "error") return <main className="auth-ui auth-screen">
    <section className="auth-card">
      <h1 ref={recoveryHeading} tabIndex={-1}>接続を確認してください</h1>
      <Message feedback={{ text: state.message, error: true }} />
      <button type="button" className="auth-primary" onClick={() => {
        if (state.recovery === "logout") void auth.logout().catch(() => {});
        else void auth.refresh();
      }}>{state.recovery === "logout" ? "ログアウトを完了する" : "ログイン状態を再取得"}</button>
      <p className="auth-hint">安全のため業務画面は閉じています。直前の業務操作は自動で再実行しません。</p>
    </section>
  </main>;
  if (!state.session.enabled) return <App />;
  if (!state.session.authenticated) return <SignIn notice={state.notice} />;
  return <AccountWorkspace key={JSON.stringify([state.session.user.id, state.session.tenant.id])}
    session={state.session} notice={state.notice} />;
}
