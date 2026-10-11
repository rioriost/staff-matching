import { useEffect, useRef, useState } from "react";
import { ApiError, errorMessage } from "./api.ts";
import { saasApi } from "./saas-api.ts";
import type { Actions, Tab } from "./saas/shared.tsx";
import { LoadState, number, useResource } from "./saas/shared.tsx";
import Jobs from "./saas/Jobs.tsx";
import People from "./saas/People.tsx";
import { Appointments, Introductions } from "./saas/Lifecycle.tsx";
import Inbox from "./saas/Inbox.tsx";
import Icon from "./Icon.tsx";
import type { IconName } from "./Icon.tsx";
import "./saas.css";

const sections: { id: Tab; label: string; icon: IconName }[] = [
  { id: "jobs", label: "求人", icon: "briefcase" }, { id: "people", label: "人材", icon: "people" },
  { id: "introductions", label: "紹介", icon: "introduction" }, { id: "appointments", label: "面接", icon: "calendar" },
  { id: "inbox", label: "受信箱", icon: "inbox" },
];
export default function SaasWorkspace({ readOnly }: { readOnly: boolean }) {
  const [tab, setTab] = useState<Tab>("jobs");
  const [visited, setVisited] = useState<Set<Tab>>(() => new Set(["jobs"]));
  const [revision, setRevision] = useState(0);
  const [online, setOnline] = useState(navigator.onLine);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [failure, setFailure] = useState("");
  const [blocked, setBlocked] = useState(false);
  const gate = useRef(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const notice = useRef<HTMLDivElement>(null);
  const overview = useResource("overview", saasApi.overview, true, revision);
  const disabled = readOnly || !online || busy || blocked || !overview.data || !!overview.error || overview.loading;
  const disabledRef = useRef(disabled);
  disabledRef.current = disabled;
  useEffect(() => {
    const update = () => {
      setOnline(navigator.onLine);
      if (!navigator.onLine) {
        setBlocked(true);
        setFailure("接続が途切れました。再接続後に最新情報を再取得してください。");
      }
    };
    window.addEventListener("online", update); window.addEventListener("offline", update);
    return () => { window.removeEventListener("online", update); window.removeEventListener("offline", update); };
  }, []);
  function navigate(next: Tab) {
    setTab(next); setVisited((old) => new Set([...old, next]));
    requestAnimationFrame(() => heading.current?.focus());
  }
  const actions: Actions = {
    disabled, revision, canSimulate: overview.data?.can_simulate ?? false, navigate,
    async perform<T>(message: string, work: () => Promise<T>): Promise<T | undefined> {
      if (gate.current || disabledRef.current) return undefined;
      gate.current = true; setBusy(true); setFeedback(""); setFailure("");
      try {
        const result = await work();
        setFeedback(message); setRevision((value) => value + 1);
        return result;
      } catch (error) {
        const conflict = error instanceof ApiError && error.status === 409;
        const uncertain = error instanceof ApiError && (error.code === "OUTCOME_UNKNOWN" || error.status >= 500);
        setFailure(`${conflict ? "情報が更新されたか、この状態では操作できません。最新情報を確認してください。 " : ""}${errorMessage(error)}`);
        setBlocked(conflict || uncertain);
        requestAnimationFrame(() => notice.current?.focus());
        return undefined;
      } finally { gate.current = false; setBusy(false); }
    },
  };
  function refresh() {
    setRevision((value) => value + 1); setBlocked(false); setFailure("");
    setFeedback("最新情報を再取得しています。操作結果が未確認だった場合は、重複がないことをご確認ください。");
  }
  return <div className="saas-workspace">
    <a className="saas-skip" href="#saas-main">業務内容へ移動</a>
    <div className="saas-shell">
      <aside className="saas-sidebar">
        <div className="saas-brand"><span className="saas-brand-symbol"><Icon name="match" /></span><div><strong>Staff workspace</strong><span>人と仕事を、確かな条件で。</span></div></div>
        <nav aria-label="業務メニュー">{sections.map((section) => <button key={section.id}
          aria-current={tab === section.id ? "page" : undefined} onClick={() => navigate(section.id)}>
          <Icon name={section.icon} /><span>{section.label}</span>
          {tab === section.id && <Icon name="check" className="saas-nav-selected" />}
          {section.id === "inbox" && !!overview.data?.unread_notifications && <span className="saas-nav-count">{overview.data.unread_notifications}</span>}
        </button>)}</nav>
        <div className="saas-sidebar-note"><strong>架空データのデモ</strong><p>企業・人材・職歴・連絡先は架空です。実在データのアップロードは行いません。</p></div>
      </aside>
      <main id="saas-main" className="saas-main">
        <header className="saas-page-header"><div><div className="saas-page-title">
          <h1 ref={heading} tabIndex={-1}>{sections.find((section) => section.id === tab)?.label}ワークスペース</h1>
          <span className="saas-tag">架空データ</span></div>
          <p className="saas-hint">上部の管理会社に属する情報だけを表示しています。</p></div>
          <button disabled={busy || !online} onClick={refresh}><Icon name="refresh" />最新情報を再取得</button></header>
        {readOnly && <p className="saas-notice"><Icon name="eye" /> 閲覧専用アカウントです。検索・詳細確認は利用できますが、業務データの更新はできません。</p>}
        {!online && <p className="saas-alert" role="status"><Icon name="warning" /> オフラインです。表示中の情報は古い可能性があります。再接続後、最新情報を再取得してください。</p>}
        <LoadState resource={overview} />
        {overview.data && <dl className="saas-metrics" aria-label="この管理会社の登録件数">
          <div className="saas-metric-jobs"><dt><Icon name="briefcase" />求人</dt><dd>{number(overview.data.jobs)}<span>件</span></dd></div>
          <div className="saas-metric-people"><dt><Icon name="people" />人材</dt><dd>{number(overview.data.people)}<span>名</span></dd></div>
          <div className="saas-metric-companies"><dt><Icon name="building" />募集企業</dt><dd>{number(overview.data.companies)}<span>社</span></dd></div>
          <div className="saas-metric-introductions"><dt><Icon name="introduction" />紹介</dt><dd>{number(overview.data.introductions)}<span>件</span></dd></div>
        </dl>}
        <div ref={notice} tabIndex={-1} className={failure ? "saas-alert" : feedback || busy ? "saas-feedback" : undefined}
          role={failure ? "alert" : "status"} aria-live={failure ? "assertive" : "polite"}>
          {(busy || failure || feedback) && <Icon name={busy ? "refresh" : failure ? "warning" : "info"} />}
          {busy ? "操作を送信中です。結果が届くまで再送しないでください…" : failure || feedback}
          {blocked && <p>更新操作は停止しています。「最新情報を再取得」で確認してから操作してください。自動再送は行いません。</p>}
        </div>
        {visited.has("jobs") && <section hidden={tab !== "jobs"}><Jobs active={tab === "jobs"} actions={actions} /></section>}
        {visited.has("people") && <section hidden={tab !== "people"}><People active={tab === "people"} revision={revision} /></section>}
        {visited.has("introductions") && <section hidden={tab !== "introductions"}><Introductions active={tab === "introductions"} actions={actions} /></section>}
        {visited.has("appointments") && <section hidden={tab !== "appointments"}><Appointments active={tab === "appointments"} actions={actions} /></section>}
        {visited.has("inbox") && <section hidden={tab !== "inbox"}><Inbox active={tab === "inbox"} actions={actions} /></section>}
        <footer className="saas-footer">架空データによる業務デモ · 実際の採用、本人同意、メール送信を行うサービスではありません。
          {overview.data && <span>データ：{overview.data.dataset_version} ／ 地理ネットワーク：{overview.data.network_version}</span>}</footer>
      </main>
    </div>
  </div>;
}
