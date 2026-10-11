import { EligibilityBadge, ProposalBadge } from "./CandidateDetails.tsx";
import type { DetailSelection } from "./CandidateDetails.tsx";
import { dateTime, runLabels, score } from "./domain.ts";
import type { Order, RunView, Staff } from "./models.ts";

interface ResultProps {
  run: RunView;
  staff: Staff[];
  orders: Order[];
  now: number;
  pollingError: string | null;
  onDetail: (selection: DetailSelection) => void;
}

export function SimulationResults({ run, staff, orders, now, pollingError, onDetail }: ResultProps) {
  const result = run.result;
  const staffName = (id: string) => staff.find((item) => item.id === id)?.name ?? id;
  const orderTitle = (id: string) => orders.find((item) => item.id === id)?.title ?? id;
  const state = run.status;

  return <div className="run-results">
    <div className="run-meta" aria-live="polite" aria-atomic="true">
      <span className={`badge run-${state}`}>{runLabels[state]}</span>
      <time dateTime={run.created_at}>{dateTime(run.created_at)}</time>
      <span>入力リビジョン {run.input_revision}</span>
    </div>
    <p className="small muted">実行ID：<code>{run.id}</code></p>
    {(state === "queued" || state === "running") && <div className="notice" role="status">
      {pollingError ? "自動更新を停止しました。「状態を再取得」で実行状況を確認してください。" : "配置案を計算しています。この画面を再読み込みしても、保存された実行を表示できます。"}
    </div>}
    {state === "failed" && <div className="error-box" role="alert">
      <strong>計算に失敗しました</strong>
      <p>{run.error ?? "失敗理由は記録されていません。バックエンドの状態を確認してください。"}</p>
    </div>}
    {state === "completed" && !result && <p className="error-box" role="alert">実行は完了しましたが、計算結果がありません。状態を再取得してください。</p>}
    {result && <>
      <h3>追加充足提案の比較</h3>
      <p className="muted">同じ入力・既存配置を使った比較です。提案数であり、本人回答や確定数ではありません。</p>
      <div className="comparison">
        <div className="comparison-card optimized">
          <span>最適化案 <code>{result.optimized.method}</code></span>
          <strong>{result.optimized.filled_slots}<small>枠</small></strong>
          <span>スコア合計 {score(result.optimized.total_score)}</span>
        </div>
        <div className="comparison-card">
          <span>逐次割当 <code>GREEDY FEASIBLE</code></span>
          <strong>{result.greedy.filled_slots}<small>枠</small></strong>
          <span>スコア合計 {score(result.greedy.total_score)}</span>
        </div>
        <div className="comparison-card gain">
          <span>追加充足提案の差</span>
          <strong>{result.optimized.filled_slots - result.greedy.filled_slots > 0 ? "+" : ""}{result.optimized.filled_slots - result.greedy.filled_slots}<small>枠</small></strong>
          <span>計算時の残り {result.total_remaining_slots}枠</span>
        </div>
      </div>
      <p className="small muted">計算時の確定済み {result.confirmed_slots}枠は比較の追加提案数に含めません。逐次割当の実装識別子：{result.greedy.method}</p>

      <details className="solver-details" open>
        <summary>計算品質・対象範囲</summary>
        <dl className="facts solver-grid">
          <div><dt>ソルバーの状態</dt><dd><code>{result.solver.status}</code></dd></div>
          <div><dt>充足数の最適性</dt><dd>{result.solver.count_optimal ? "最適を証明済み" : "未証明"} <code>{result.solver.count_status}</code></dd></div>
          <div><dt>スコアの最適性</dt><dd>{result.solver.score_optimal ? "最適を証明済み" : "未証明"} <code>{result.solver.score_status ?? "未実行"}</code></dd></div>
          <div><dt>計算時間 / 設定上限</dt><dd>{(result.solver.elapsed_ms / 1000).toFixed(3)}秒 / {result.solver.time_limit_seconds}秒</dd></div>
          <div><dt>目的値の境界 / ギャップ</dt><dd>{score(result.solver.objective_bound)} / {score(result.solver.gap)}</dd></div>
          <div><dt>代替手法の使用</dt><dd>{result.solver.fallback_used ? "あり（結果を確認してください）" : "なし"}</dd></div>
          <div><dt>候補ペア数 / 上限</dt><dd>{result.candidate_count} / {result.candidate_limit}</dd></div>
          <div><dt>候補の切り捨て</dt><dd>{result.truncated === false ? "なし（truncated: false）" : "あり"}</dd></div>
          <div><dt>エンジン</dt><dd><code>{result.solver.engine}</code></dd></div>
          <div><dt>再現性の設定</dt><dd>seed {result.solver.seed} / workers {result.solver.workers} / scale {result.solver.score_scale}</dd></div>
        </dl>
        <p className="small">登録された評価ペアが対象の疎な候補集合です。未登録の組み合わせは補完せず、全スタッフ×全案件の網羅や全体最適性は保証しません。候補上限を超える場合は切り捨てず、APIがエラーを返します。</p>
        {(!result.solver.count_optimal || !result.solver.score_optimal || result.solver.fallback_used) && <p className="notice subtle">時間制限などにより最適性が未証明、または代替手法を使用しています。「最適化案」は計算方式の名称であり、最適解の保証ではありません。</p>}
      </details>

      <h3>未充足の内訳</h3>
      {result.unfilled.length ? <ul className="unfilled-list">
        {result.unfilled.map((item) => <li key={item.order_id}>
          <div><strong>{orderTitle(item.order_id)}</strong><span className="badge pending">残り {item.slots}枠</span></div>
          <p>{item.reason}</p>
          <span className="small muted">条件適合 {item.eligible_candidates}名 / 情報不足 {item.pending_candidates}名</span>
        </li>)}
      </ul> : <p className="empty compact">計算結果に未充足の記録はありません。</p>}

      <h3>提案と模擬回答</h3>
      <p className="muted">選ばれた提案だけが回答対象です。「根拠を見る」から、模擬本人回答と担当者確定を別々に操作できます。</p>
      {run.proposals.length ? <ul className="proposal-list">
        {run.proposals.map((proposal) => <li key={proposal.id}>
          <div className="proposal-summary">
            <strong>{staffName(proposal.staff_id)}</strong>
            <span>{orderTitle(proposal.order_id)}</span>
            <ProposalBadge proposal={proposal} now={now} />
          </div>
          <p className="small muted">期限 {dateTime(proposal.expires_at)}</p>
          <button type="button" onClick={() => onDetail({
            orderId: proposal.order_id, staffId: proposal.staff_id, runId: run.id,
          })} aria-label={`${staffName(proposal.staff_id)}・${orderTitle(proposal.order_id)}の保存時の根拠と回答操作を見る`}>
            保存時の根拠を見る・回答
          </button>
        </li>)}
      </ul> : <p className="empty compact">この実行で作成された提案はありません。</p>}

      <details className="all-candidates">
        <summary>計算時の候補一覧（{result.candidates.length}件）</summary>
        <p className="small muted">選択・非選択は計算時の結果です。現在の本人回答・確定状態とは別に表示します。</p>
        {result.candidates.length ? <div className="table-scroll" role="region" aria-label="計算時の候補一覧（横にスクロールできます）" tabIndex={0}>
          <table>
            <caption className="sr-only">保存された全候補と提案状態</caption>
            <thead><tr><th scope="col">案件 / スタッフ</th><th scope="col">条件</th><th scope="col">スコア</th><th scope="col">計算時の選択 / 現在の提案状態</th><th scope="col">詳細</th></tr></thead>
            <tbody>{result.candidates.map((candidate) => {
              const proposal = run.proposals.find((item) => item.staff_id === candidate.staff_id && item.order_id === candidate.order_id);
              return <tr key={JSON.stringify([candidate.order_id, candidate.staff_id])}>
                <th scope="row"><span className="small muted">{orderTitle(candidate.order_id)}</span><br />{staffName(candidate.staff_id)}</th>
                <td><EligibilityBadge candidate={candidate} /></td>
                <td className="numeric">{score(candidate.score)}</td>
                <td><div className="inline-badges"><span>{candidate.selected ? "提案対象に選択" : "非選択"}</span>{proposal && <ProposalBadge proposal={proposal} now={now} />}</div></td>
                <td><button className="text-button" type="button" onClick={() => onDetail({
                  orderId: candidate.order_id, staffId: candidate.staff_id, runId: run.id,
                })} aria-label={`${staffName(candidate.staff_id)}・${orderTitle(candidate.order_id)}の保存時の根拠を見る`}>根拠を見る</button></td>
              </tr>;
            })}</tbody>
          </table>
        </div> : <p className="empty compact">評価対象の候補ペアがありません。</p>}
      </details>
    </>}
  </div>;
}
