import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "./api.ts";
import { dateTime, effectiveProposalStatus, eligibilityLabels, proposalLabels, score } from "./domain.ts";
import type { Candidate, Order, Proposal, Staff } from "./models.ts";

export interface DetailSelection {
  orderId: string;
  staffId: string;
  runId: string | null;
}

interface DetailProps {
  selection: DetailSelection | null;
  currentCandidates: Candidate[];
  candidatesLoading: boolean;
  staff: Staff[];
  orders: Order[];
  proposal: Proposal | undefined;
  now: number;
  busy: boolean;
  onRespond: (proposal: Proposal, decision: "consent" | "decline") => void;
  onConfirm: (proposal: Proposal) => void;
  onClose: () => void;
}

export function EligibilityBadge({ candidate }: { candidate: Candidate }) {
  return <span className={`badge ${candidate.eligibility}`}>{eligibilityLabels[candidate.eligibility]}</span>;
}

export function ProposalBadge({ proposal, now }: { proposal: Proposal; now: number }) {
  const status = effectiveProposalStatus(proposal, now);
  return <span className={`badge ${status}`}>{proposalLabels[status]}</span>;
}

export function CandidateDetails(props: DetailProps) {
  const { selection, currentCandidates, candidatesLoading, staff, orders, proposal, now, busy, onRespond, onConfirm, onClose } = props;
  const heading = useRef<HTMLHeadingElement>(null);
  const [saved, setSaved] = useState<{ key: string; candidate: Candidate | null; error: string | null } | null>(null);
  const [retry, setRetry] = useState(0);
  const selectionKey = selection ? JSON.stringify(selection) : "";

  useEffect(() => {
    if (selectionKey) heading.current?.focus();
  }, [selection, selectionKey]);

  const runId = selection?.runId;
  const orderId = selection?.orderId;
  const staffId = selection?.staffId;
  useEffect(() => {
    if (!runId || !orderId || !staffId) return;
    const controller = new AbortController();
    setSaved(null);
    void api.explanation(runId, orderId, staffId, controller.signal).then((candidate) => {
      if (candidate.order_id !== orderId || candidate.staff_id !== staffId) {
        throw new Error("保存された説明の案件・スタッフが一致しません。");
      }
      if (!controller.signal.aborted) setSaved({ key: selectionKey, candidate, error: null });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setSaved({ key: selectionKey, candidate: null, error: errorMessage(error) });
    });
    return () => controller.abort();
  }, [runId, orderId, staffId, selectionKey, retry]);

  if (!selection) {
    return <aside className="detail-panel empty-detail" aria-labelledby="detail-placeholder">
      <span className="eyebrow">判断の根拠</span>
      <h3 id="detail-placeholder">候補の内訳を確認</h3>
      <p>候補一覧の「根拠を見る」または提案の「保存時の根拠を見る」を選ぶと、必須条件と適合スコアを分けて表示します。</p>
      <p>スコアは配置を検討するための合成値です。採用確率や本人の承諾を表すものではありません。</p>
    </aside>;
  }

  const matchingSaved = saved?.key === selectionKey ? saved : null;
  const candidate = runId ? matchingSaved?.candidate : currentCandidates.find(
    (item) => item.staff_id === staffId && item.order_id === orderId,
  );
  const loading = runId ? !matchingSaved : candidatesLoading;
  const name = staff.find((item) => item.id === staffId)?.name ?? staffId;
  const order = orders.find((item) => item.id === orderId);

  return <aside className="detail-panel" aria-labelledby="detail-heading">
    <div className="section-top">
      <span className="eyebrow">{runId ? "実行時に保存した説明" : "現在の候補評価"}</span>
      <button className="text-button" type="button" onClick={onClose} aria-label="候補の詳細を閉じる">閉じる</button>
    </div>
    <h3 id="detail-heading" ref={heading} tabIndex={-1}>{name}</h3>
    <p className="muted">{order?.title ?? orderId}</p>
    {runId && <p className="small">保存時の評価です。担当者確定後の現在の条件判定とは異なる場合があります。<br /><code>{runId}</code></p>}
    {loading && <p role="status">根拠を取得しています…</p>}
    {matchingSaved?.error && <div className="error-box" role="alert">
      <p>{matchingSaved.error}</p>
      <button type="button" onClick={() => setRetry((current) => current + 1)}>根拠を再取得</button>
    </div>}
    {!loading && !candidate && !matchingSaved?.error && <p className="empty">この候補は現在の一覧にありません。案件とデータの状態を確認してください。</p>}
    {candidate && !loading && <>
      <div className="inline-badges">
        <EligibilityBadge candidate={candidate} />
        {candidate.selected && <span className="badge selected">計算時の提案対象</span>}
      </div>
      <h4>1. 必須条件の確認</h4>
      <p className="small muted">スコアが高くても、必須条件に不適合・未確認があれば提案できません。</p>
      <ul className="reason-list">
        {candidate.reasons.map((reason, index) => <li key={`${reason.code}-${index}`} className={`reason-${reason.kind}`}>
          <span className={`reason-symbol ${reason.kind}`} aria-hidden="true">
            {reason.kind === "passed" ? "✓" : reason.kind === "violation" ? "×" : reason.kind === "unknown" ? "?" : "i"}
          </span>
          <span><span className="sr-only">{reason.kind === "passed" ? "適合" : reason.kind === "violation" ? "不適合" : reason.kind === "unknown" ? "未確認" : "参考"}：</span>{reason.message}</span>
        </li>)}
        {candidate.reasons.length === 0 && <li>判定理由の記録はありません。</li>}
      </ul>
      <h4>2. 適合スコアの内訳</h4>
      <p className="score-heading"><strong>{score(candidate.score)}</strong><span>正規化スコア・確率ではありません</span></p>
      {candidate.score_parts ? <>
        <dl className="facts score-parts">
          <div><dt>スキル適合</dt><dd>{score(candidate.score_parts.skill)}</dd></div>
          <div><dt>希望適合</dt><dd>{score(candidate.score_parts.preference)}</dd></div>
          <div><dt>通勤適合（合成値）</dt><dd>{score(candidate.score_parts.commute)}</dd></div>
          <div><dt>就業履歴</dt><dd>{score(candidate.score_parts.history)}</dd></div>
          <div><dt>評価のある就業件数</dt><dd>{candidate.score_parts.rated_assignments}件</dd></div>
          <div><dt>スコア重みの版</dt><dd><code>{candidate.score_parts.weights_version}</code></dd></div>
        </dl>
        {candidate.score_parts.history_missing && <p className="notice subtle">就業履歴が不足しています。履歴がないこと自体では減点しません。</p>}
      </> : <p className="muted">算出対象外のため、スコアの内訳はありません。</p>}
      <p className="small">通勤：{candidate.commute_minutes === null ? "未確認" : `${candidate.commute_minutes}分`}（合成値・実経路は未照会）</p>
      <p className="explanation">{candidate.explanation}</p>
    </>}
    {proposal && <ProposalActions proposal={proposal} now={now} busy={busy} onRespond={onRespond} onConfirm={onConfirm} />}
  </aside>;
}

interface ProposalActionProps {
  proposal: Proposal;
  now: number;
  busy: boolean;
  onRespond: (proposal: Proposal, decision: "consent" | "decline") => void;
  onConfirm: (proposal: Proposal) => void;
}

export function ProposalActions({ proposal, now, busy, onRespond, onConfirm }: ProposalActionProps) {
  const status = effectiveProposalStatus(proposal, now);
  const actionable = status === "proposed" || status === "consented";
  return <div className="proposal-actions">
    <div className="section-top">
      <h4>3. 提案への回答と確定</h4>
      <ProposalBadge proposal={proposal} now={now} />
    </div>
    <p className="small">提案期限：<time dateTime={proposal.expires_at}>{dateTime(proposal.expires_at)}</time></p>
    {proposal.responded_at && <p className="small">回答日時：{dateTime(proposal.responded_at)}</p>}
    <p className="small muted">提案 v{proposal.version} / スタッフ v{proposal.staff_version} / 案件 v{proposal.order_version} / 評価 v{proposal.evidence_version}</p>
    {actionable ? <>
      <fieldset disabled={busy} className="action-group">
        <legend>本人の回答をシミュレーション</legend>
        <p className="small">本人には送信されません。本人の実際の意思ではありません。</p>
        <div className="button-row">
          <button type="button" disabled={status !== "proposed"} onClick={() => onRespond(proposal, "consent")}>
            模擬本人承諾
          </button>
          <button className="danger-button" type="button" onClick={() => onRespond(proposal, "decline")}>
            {status === "consented" ? "模擬辞退（承諾撤回）" : "模擬辞退"}
          </button>
        </div>
      </fieldset>
      <fieldset disabled={busy} className="action-group coordinator">
        <legend>担当者の確定操作</legend>
        <p className="small">{status === "consented" ? "模擬承諾済みです。条件を再確認して配置を確定します。" : "模擬本人承諾の後に、別の操作で確定できます。自動では確定しません。"}</p>
        <button className="primary" type="button" disabled={status !== "consented"} onClick={() => onConfirm(proposal)}>
          担当者として確定
        </button>
      </fieldset>
    </> : <p className="notice subtle">
      {status === "confirmed" ? "担当者が確定した配置です。確定済み配置の一覧にも反映されます。"
        : status === "declined" ? "辞退済みです。この提案には回答・確定できません。"
          : "提案期限を過ぎています。回答・確定はできません。"}
    </p>}
  </div>;
}
