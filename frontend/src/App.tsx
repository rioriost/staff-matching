import { useRef, useState } from "react";
import { api, errorMessage } from "./api.ts";
import { CandidateDetails, EligibilityBadge } from "./CandidateDetails.tsx";
import type { DetailSelection } from "./CandidateDetails.tsx";
import { activeRun, dateTime, optimizationBlock, runLabels, score, unresolvedProposals } from "./domain.ts";
import type { Proposal } from "./models.ts";
import { SimulationResults } from "./SimulationResults.tsx";
import { useCandidates, useClock, useWorkspace } from "./useWorkspace.ts";

export default function App({ authentication = "basic" }: { authentication?: "basic" | "accounts" }) {
  const workspace = useWorkspace();
  const { data, refresh, refreshing, refreshError, pollingError, selectedOrderId, setSelectedOrderId,
    selectedRunId, setSelectedRunId, candidateVersion, retryCandidates } = workspace;
  const candidates = useCandidates(selectedOrderId, candidateVersion);
  const now = useClock();
  const [selection, setSelection] = useState<DetailSelection | null>(null);
  const [timeLimit, setTimeLimit] = useState("3");
  const [action, setAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const actionLock = useRef(false);
  const detailTrigger = useRef<HTMLElement | null>(null);
  const selectedOrder = data?.orders.find((order) => order.id === selectedOrderId);
  const selectedRun = data?.runs.find((run) => run.id === selectedRunId);
  const block = data ? optimizationBlock(data.runs, now) : "APIからデータを取得してください。";
  const seconds = Number(timeLimit);
  const validLimit = timeLimit.trim() !== "" && Number.isFinite(seconds) && seconds >= 0.05 && seconds <= 10;
  const busy = action !== null || refreshing;
  const stateUnverified = refreshError !== null || pollingError !== null;
  const staffName = (id: string) => data?.staff.find((staff) => staff.id === id)?.name ?? id;
  const orderTitle = (id: string) => data?.orders.find((order) => order.id === id)?.title ?? id;
  const selectedProposal = selection?.runId
    ? data?.runs.find((run) => run.id === selection.runId)?.proposals.find(
      (proposal) => proposal.order_id === selection.orderId && proposal.staff_id === selection.staffId,
    )
    : undefined;

  function showDetails(next: DetailSelection) {
    detailTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setSelection(next);
  }

  function closeDetails() {
    setSelection(null);
    detailTrigger.current?.focus();
  }

  async function mutate(
    label: string,
    operation: () => Promise<{ message: string; runId?: string }>,
  ) {
    if (actionLock.current) return;
    actionLock.current = true;
    setAction(label);
    setActionError(null);
    setAnnouncement("");
    try {
      const result = await operation();
      const refreshed = await refresh(result.runId);
      setAnnouncement(`${result.message}${refreshed ? "" : " 最新状態の取得に失敗しました。操作を繰り返さず、「状態を再取得」で確認してください。"}`);
    } catch (error) {
      setActionError(`${errorMessage(error)} 操作結果と最新状態を確認してください。`);
      await refresh();
    } finally {
      actionLock.current = false;
      setAction(null);
    }
  }

  function optimize() {
    if (!data || busy || stateUnverified || block || !validLimit) return;
    void mutate("配置案を作成しています…", async () => {
      const accepted = await api.optimize({
        time_limit_seconds: seconds,
        scheduling_mode: "full_period",
        idempotency_key: crypto.randomUUID(),
      });
      setSelectedRunId(accepted.run_id);
      setSelection(null);
      return { message: "計算リクエストを受け付けました。提案の作成だけでは配置は確定しません。", runId: accepted.run_id };
    });
  }

  function respond(proposal: Proposal, decision: "consent" | "decline") {
    if (busy || stateUnverified) return;
    void mutate(decision === "consent" ? "模擬承諾を登録しています…" : "模擬辞退を登録しています…", async () => {
      await api.respond(proposal.id, {
        decision,
        expected_version: proposal.version,
        idempotency_key: crypto.randomUUID(),
      });
      return {
        message: decision === "consent" ? "模擬本人承諾を登録しました。配置はまだ確定していません。" : "模擬辞退を登録しました。",
        runId: proposal.run_id,
      };
    });
  }

  function confirm(proposal: Proposal) {
    if (busy || stateUnverified) return;
    void mutate("担当者確定を登録しています…", async () => {
      await api.confirm(proposal.id, {
        expected_version: proposal.version,
        idempotency_key: crypto.randomUUID(),
      });
      return { message: "担当者として配置を確定しました。確定済み配置の一覧を更新しました。", runId: proposal.run_id };
    });
  }

  const unfinishedRun = data?.runs.find((run) => activeRun(run) || unresolvedProposals([run], now).length > 0);
  const cloud = data?.health.mode === "azure-poc";

  return <>
    <a className="skip-link" href="#main">本文へ移動</a>
    <div className="environment-banner">
      <strong>{data ? cloud ? "Azure PoC / 合成データ" : "ローカルモック / 合成データ" : "接続環境を確認中 / 合成データ"}</strong>
      <span>{data ? cloud
        ? `本人回答はシミュレーション / PostgreSQL・${authentication === "accounts" ? "管理企業別アカウント認証" : "共通担当者認証"}・本番利用不可`
        : "本人回答はシミュレーション / SQLite・認証なし・本番利用不可"
        : "本人回答はシミュレーション / 本番利用不可"}</span>
    </div>
    <header className="app-header">
      <div className="brand"><span className="brand-mark" aria-hidden="true">配</span><div><span className="eyebrow">STAFF MATCHING · PHASE 0</span><h1>スタッフ配置検討</h1></div></div>
      <div className="header-actions">
        <span className={`connection ${data && !refreshError ? "connected" : ""}`}>
          <span aria-hidden="true">●</span> {refreshError ? "API取得エラー" : data ? cloud ? "Azure API接続済み" : "ローカルAPI接続済み" : "接続を確認中"}
        </span>
        <button type="button" onClick={() => { void refresh(selectedRunId || undefined); }} disabled={busy}>
          {refreshing ? "取得中…" : "状態を再取得"}
        </button>
      </div>
    </header>

    <main id="main" className="page">
      <div className="page-intro">
        <div><span className="eyebrow">候補確認 → 配置シミュレーション → 模擬回答・担当者確定</span>
          <h2>条件を確認してから、配置を決める。</h2>
          <p>必須条件と適合スコアを分けて確認し、同じ入力で配置案を比較します。本人の意思確認と担当者の確定は別の操作です。</p>
        </div>
        <span className="tenant-label">テナント：{data?.health.tenant_id ?? "未取得"}</span>
      </div>

      <div className="metrics" aria-label="現在の登録状況">
        <div><span>募集案件</span><strong>{data ? data.orders.length : "—"}<small>件</small></strong></div>
        <div><span>登録スタッフ</span><strong>{data ? data.staff.length : "—"}<small>名</small></strong></div>
        <div><span>担当者確定済み配置</span><strong>{data ? data.assignments.length : "—"}<small>枠</small></strong><span className="small">既存配置と確定APIの登録結果</span></div>
      </div>

      {cloud && <section className="panel" aria-labelledby="environment-heading">
        <h2 id="environment-heading">Azure 接続情報</h2>
        <dl className="facts">
          <div><dt>データ保存先</dt><dd>Azure Database for PostgreSQL</dd></div>
          <div><dt>アプリ実行環境</dt><dd>Azure Container Apps / azure-poc</dd></div>
          <div><dt>認証方式</dt><dd>{authentication === "accounts" ? "ID・パスワード / パスキー" : "担当者共通の HTTP Basic 認証"}</dd></div>
        </dl>
        <p className="small muted">個人別の Entra ID 認証や本人確認ではありません。承諾・辞退は担当者による模擬操作です。</p>
        <a href="/api/extensions" target="_blank" rel="noreferrer">PostgreSQL 拡張のバージョン・使用状況を確認（JSON）</a>
      </section>}

      <nav className="section-nav" aria-label="画面内の移動">
        <a href="#candidates">01 候補の確認</a>
        <a href="#simulation">02 配置シミュレーション</a>
        <a href="#assignments">03 確定済み配置</a>
      </nav>

      <div className="status-region" aria-live="polite" aria-atomic="true">
        {action ? <p className="notice">{action}</p> : announcement ? <p className="notice success">{announcement}</p> : null}
      </div>
      {actionError && <div className="error-box" role="alert"><strong>操作結果を確認してください</strong><p>{actionError}</p></div>}
      {refreshError && <div className="error-box" role="alert"><strong>最新データを取得できませんでした</strong><p>{refreshError}</p>{data && <p>下の表示は最後に取得できた内容です。最新状態を確認するまで更新操作を停止します。</p>}</div>}
      {pollingError && <div className="error-box" role="alert"><strong>実行状況の自動更新を停止しました</strong><p>{pollingError}</p><p>更新を繰り返さず停止しています。上の「状態を再取得」で再開してください。</p></div>}
      {!data && <div className="empty initial-state" role="status">
        {refreshing ? "APIから案件・スタッフ・実行履歴を取得しています…" : "データを取得できていません。接続を確認して「状態を再取得」を選んでください。"}
      </div>}

      {data && <div className="workspace">
        <div className="main-column">
          <section className="panel" id="candidates" aria-labelledby="candidates-heading">
            <div className="section-heading"><span className="section-number">01</span><div><h2 id="candidates-heading">候補の確認</h2><p>現在の確定済み配置を反映した条件判定</p></div></div>
            <div className="notice subtle period-note"><strong>全期間を通した配置のみ</strong><span>期間は「開始日以上・終了日未満」です（終了日は含まない）。日別シフト・夜勤は対象外です。</span></div>
            <label className="field-label" htmlFor="order-select">募集案件</label>
            <select id="order-select" value={selectedOrderId} disabled={!data.orders.length} onChange={(event) => {
              setSelectedOrderId(event.target.value);
              if (selection?.runId === null) setSelection(null);
            }}>
              {!data.orders.length && <option value="">登録された案件がありません</option>}
              {data.orders.map((order) => {
                const confirmed = data.assignments.filter((item) => item.order_id === order.id).length;
                return <option key={order.id} value={order.id}>{order.title} — 募集 {order.headcount}枠 / 確定 {confirmed}枠</option>;
              })}
            </select>
            {selectedOrder && <>
              <dl className="facts order-facts">
                <div><dt>募集期間</dt><dd>{selectedOrder.period.start} 〜 {selectedOrder.period.end}<span className="small muted">（終了日を含まない）</span></dd></div>
                <div><dt>時給</dt><dd>{selectedOrder.hourly_rate.toLocaleString("ja-JP")}円</dd></div>
                <div><dt>必要スキル</dt><dd>{Object.entries(selectedOrder.required_skills).map(([name, level]) => `${name} Lv.${level}`).join(" / ") || "指定なし"}</dd></div>
                <div><dt>必要資格</dt><dd>{selectedOrder.required_qualifications.join(" / ") || "指定なし"}</dd></div>
              </dl>
              <div className="section-top candidate-heading"><h3>評価対象の候補</h3><span className="small muted">{candidates.loading ? "取得中" : `${candidates.candidates.length}件`}</span></div>
              <p className="small muted">登録済みの合成評価ペアのみ表示します。スコアは正規化した適合値で、承諾・採用の確率ではありません。</p>
              {candidates.loading && <p className="empty compact" role="status">候補を取得しています…</p>}
              {candidates.error && <div className="error-box" role="alert"><p>{candidates.error}</p><button type="button" onClick={retryCandidates}>候補を再取得</button></div>}
              {!candidates.loading && !candidates.error && (candidates.candidates.length ? <div className="table-scroll" role="region" aria-label="現在の候補一覧（横にスクロールできます）" tabIndex={0}>
                <table>
                  <caption className="sr-only">{selectedOrder.title}の現在の候補評価</caption>
                  <thead><tr><th scope="col">スタッフ</th><th scope="col">必須条件</th><th scope="col">適合スコア</th><th scope="col">通勤（合成値）</th><th scope="col">詳細</th></tr></thead>
                  <tbody>{candidates.candidates.map((candidate) => <tr key={candidate.staff_id}>
                    <th scope="row">{staffName(candidate.staff_id)}</th>
                    <td><EligibilityBadge candidate={candidate} /></td>
                    <td className="numeric">{score(candidate.score)}</td>
                    <td>{candidate.commute_minutes === null ? "—（未確認）" : `${candidate.commute_minutes}分`}</td>
                    <td><button className="text-button" type="button" onClick={() => showDetails({ orderId: candidate.order_id, staffId: candidate.staff_id, runId: null })}
                      aria-label={`${staffName(candidate.staff_id)}の現在の条件とスコアの根拠を見る`}>根拠を見る</button></td>
                  </tr>)}</tbody>
                </table>
              </div> : <p className="empty compact">この案件に登録された評価ペアはありません。データを補完して候補を作ることはありません。</p>)}
            </>}
          </section>

          <section className="panel" id="simulation" aria-labelledby="simulation-heading">
            <div className="section-heading"><span className="section-number">02</span><div><h2 id="simulation-heading" tabIndex={-1}>配置シミュレーション</h2><p>全案件をまとめて比較し、追加の配置案を作成</p></div></div>
            <form className="optimize-form" onSubmit={(event) => { event.preventDefault(); optimize(); }}>
              <div><label className="field-label" htmlFor="time-limit">計算時間の上限（秒）</label>
                <input id="time-limit" type="number" min="0.05" max="10" step="0.05" required value={timeLimit}
                  onChange={(event) => setTimeLimit(event.target.value)} disabled={busy}
                  aria-invalid={!validLimit} aria-describedby="time-limit-hint" />
                <span className={`small ${validLimit ? "muted" : "invalid"}`} id="time-limit-hint">0.05〜10秒。最適性の証明は時間内に完了しない場合があります。</span>
              </div>
              <button className="primary" type="submit" disabled={busy || stateUnverified || !!block || !validLimit}
                aria-describedby={block ? "optimization-block" : "optimize-note"}>全案件の配置案を作成</button>
            </form>
            <p className="small muted" id="optimize-note">候補確認で選択中の案件だけでなく、全案件が対象です。未確定の提案を次回へ固定して引き継ぐ機能はありません。</p>
            {block && <div className="notice warning" id="optimization-block"><p>{block}</p>
              {unfinishedRun && unfinishedRun.id !== selectedRunId && <button type="button" onClick={() => {
                setSelectedRunId(unfinishedRun.id);
                setSelection(null);
              }}>未完了の実行・提案を表示</button>}
            </div>}
            {stateUnverified && <p className="notice warning">最新状態を確認できていません。「状態を再取得」後に操作してください。</p>}
            <div className="run-selector">
              <label className="field-label" htmlFor="run-select">保存された実行履歴（直近20件）</label>
              <select id="run-select" value={selectedRunId} disabled={!data.runs.length || action !== null} onChange={(event) => {
                setSelectedRunId(event.target.value);
                if (selection?.runId) setSelection(null);
              }}>
                {!data.runs.length && <option value="">まだ実行履歴がありません</option>}
                {data.runs.map((run) => <option key={run.id} value={run.id}>{dateTime(run.created_at)} / {runLabels[run.status]} / {run.id.slice(0, 8)}</option>)}
              </select>
            </div>
            {selectedRun ? <SimulationResults run={selectedRun} staff={data.staff} orders={data.orders} now={now} pollingError={pollingError} onDetail={showDetails} />
              : <p className="empty">「全案件の配置案を作成」で比較を始めます。未実行の数値は表示しません。</p>}
          </section>

          <section className="panel" id="assignments" aria-labelledby="assignments-heading">
            <div className="section-heading"><span className="section-number">03</span><div><h2 id="assignments-heading">確定済み配置</h2><p>担当者が確定した配置のみ。未承諾・模擬承諾の提案は含みません。</p></div></div>
            {data.assignments.length ? <div className="table-scroll" role="region" aria-label="確定済み配置一覧（横にスクロールできます）" tabIndex={0}>
              <table>
                <caption className="sr-only">APIに保存された確定済み配置</caption>
                <thead><tr><th scope="col">スタッフ / 案件</th><th scope="col">配置期間（終了日を含まない）</th><th scope="col">確定日時 / 由来</th></tr></thead>
                <tbody>{data.assignments.map((assignment) => <tr key={assignment.id}>
                  <th scope="row">{staffName(assignment.staff_id)}<br /><span className="small muted">{orderTitle(assignment.order_id)}</span></th>
                  <td>{assignment.period.start}<br />〜 {assignment.period.end}</td>
                  <td><time dateTime={assignment.confirmed_at}>{dateTime(assignment.confirmed_at)}</time><br /><span className="small muted">{assignment.source_run_id ? `実行 ${assignment.source_run_id.slice(0, 8)}` : "既存の配置"}</span></td>
                </tr>)}</tbody>
              </table>
            </div> : <p className="empty compact">確定済みの配置はありません。模擬本人承諾だけでは、この一覧には追加されません。</p>}
          </section>
        </div>

        <div className="detail-column">
          <CandidateDetails selection={selection} currentCandidates={candidates.candidates} candidatesLoading={candidates.loading}
            staff={data.staff} orders={data.orders} proposal={selectedProposal} now={now} busy={busy || stateUnverified}
            onRespond={respond} onConfirm={confirm} onClose={closeDetails} />
        </div>
      </div>}
      <footer className="page-footer">
        <p>外部AI API・実際の経路情報・埋め込みモデルは使用しません。合成データによる検証専用です。</p>
        <p>{data ? cloud ? `PostgreSQL保存 / ${authentication === "accounts" ? "管理企業別アカウント認証" : "担当者共通認証"} / 本番利用不可` : "SQLite保存 / 認証なし / 本番利用不可" : "接続環境を確認中 / 本番利用不可"}</p>
      </footer>
    </main>
  </>;
}
