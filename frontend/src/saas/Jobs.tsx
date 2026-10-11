import { useEffect, useRef, useState } from "react";
import type { Ref } from "react";
import { CANDIDATE_PAGE_SIZE, idempotencyKey, saasApi } from "../saas-api.ts";
import type { Candidate, Job, MatchRun } from "../saas-models.ts";
import type { Actions } from "./shared.tsx";
import { dateTime, DispatchNotice, Empty, Fact, focusSelection, LoadState, number, Pagination, scheduleLabel, StatusBadge, usePaging, useResource } from "./shared.tsx";
import Icon from "../Icon.tsx";
import { JobProfile, PersonProfile } from "./Profiles.tsx";
import PublicMap from "./PublicMap.tsx";
import Radar from "./Radar.tsx";

const runLabel = { queued: "待機中", running: "計算中", completed: "完了", failed: "失敗", cancelled: "中止" };
interface Pin { jobId: string; personId: string; name: string; title: string }
export default function Jobs({ active, actions }: { active: boolean; actions: Actions }) {
  const [query, setQuery] = useState("");
  const [occupation, setOccupation] = useState("");
  const [businessType, setBusinessType] = useState("");
  const [filters, setFilters] = useState({ q: "", occupation: "", business_type: "" });
  const paging = usePaging();
  const runPaging = usePaging();
  const [selected, setSelected] = useState("");
  const [showDetail, setShowDetail] = useState(false);
  const [runId, setRunId] = useState("");
  const [pins, setPins] = useState<Pin[]>([]);
  const heading = useRef<HTMLHeadingElement>(null);
  const candidatesHeading = useRef<HTMLHeadingElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLElement>(null);
  const jobs = useResource(`jobs:${JSON.stringify(filters)}:${paging.cursor}`,
    (signal) => saasApi.jobs({ ...filters, cursor: paging.cursor }, signal), active, actions.revision);
  const job = useResource(`job:${selected}`, (signal) => saasApi.job(selected, signal), active && !!selected, actions.revision);
  const runs = useResource(`runs:${runPaging.cursor}`, (signal) => saasApi.runs(signal, runPaging.cursor), active, actions.revision);
  const selectedRunId = runId || runs.data?.items[0]?.id || "";
  const run = useResource(`run:${selectedRunId}`, (signal) => saasApi.run(selectedRunId, signal),
    active && !!selectedRunId, actions.revision);
  useEffect(() => {
    if (!active || run.loading || !run.data || !["queued", "running"].includes(run.data.status) || run.error) return;
    const timer = setTimeout(run.reload, 2500);
    return () => clearTimeout(timer);
  }, [active, run.data, run.error, run.loading, run.reload]);
  useEffect(() => {
    if (showDetail) heading.current?.focus();
  }, [selected, showDetail]);
  async function start(all: boolean) {
    const selectedPins = all ? pins : pins.filter((pin) => pin.jobId === selected);
    const result = await actions.perform("マッチングを受け付けました。画面を離れても計算は継続します。",
      () => saasApi.startRun({ idempotency_key: idempotencyKey(), job_ids: all ? [] : [selected],
        pins: selectedPins.map((pin) => [pin.jobId, pin.personId]) }));
    if (result) { runPaging.reset(); setRunId(result.id); setShowDetail(true); }
  }
  function togglePin(candidate: Candidate, currentJob: Job) {
    setPins((old) => old.some((pin) => pin.jobId === currentJob.id && pin.personId === candidate.person_id)
      ? old.filter((pin) => !(pin.jobId === currentJob.id && pin.personId === candidate.person_id))
      : old.length < 100 ? [...old, { jobId: currentJob.id, personId: candidate.person_id, name: candidate.person_name, title: currentJob.title }] : old);
  }
  function clearFilters() {
    setQuery(""); setOccupation(""); setBusinessType("");
    setFilters({ q: "", occupation: "", business_type: "" }); paging.reset();
    searchInput.current?.focus();
  }
  return <>
    <div className="saas-section-title">
      <div><h2 className="ui-heading"><Icon name="compare" />求人と候補者の比較</h2><p className="saas-hint">条件と根拠を確認して、担当者が紹介候補を選びます。</p></div>
      <button aria-describedby="saas-batch-scope" disabled={actions.disabled || runs.loading || !!runs.error}
        onClick={() => void start(true)}><Icon name="match" />全求人のマッチングを実行</button>
    </div>
    <p id="saas-batch-scope" className="saas-hint">「全求人」は検索結果ではなく、この管理会社の全求人が対象です。候補の固定は適格性を上書きしません。</p>
    <div className={`saas-split ${showDetail ? "saas-show-detail" : ""}`}>
      <section ref={list} className="saas-list-pane" aria-label="求人の検索と一覧">
        <form className="saas-search" onSubmit={(event) => {
          event.preventDefault(); setFilters({ q: query.trim(), occupation: occupation.trim(), business_type: businessType }); paging.reset();
        }}>
          <label>求人を検索<input ref={searchInput} type="search" maxLength={200} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="職種、業務、会社名" /></label>
          <div className="saas-filter-fields">
            <label>職種で絞り込み<input maxLength={200} value={occupation} onChange={(event) => setOccupation(event.target.value)} placeholder="例：一般事務" /></label>
            <label>募集区分<select aria-label="募集区分" value={businessType} onChange={(event) => setBusinessType(event.target.value)}>
              <option value="">すべて</option><option value="placement">職業紹介</option><option value="dispatch">通常派遣</option>
            </select></label>
          </div>
          <button type="submit"><Icon name="search" />求人を検索</button>
        </form>
        {(filters.q || filters.occupation || filters.business_type) && <div className="saas-applied-filters" role="group" aria-label="適用中の求人検索条件">
          <span className="saas-hint">適用中</span>
          <div className="saas-filter-tokens">
            {filters.q && <span>検索：{filters.q}</span>}
            {filters.occupation && <span>職種：{filters.occupation}</span>}
            {filters.business_type && <span>{filters.business_type === "placement" ? "職業紹介" : "通常派遣"}</span>}
          </div>
          <button onClick={clearFilters}><Icon name="close" />検索条件をクリア</button>
        </div>}
        <LoadState resource={jobs} />
        {jobs.data && <>
          <p className="saas-count">{number(jobs.data.total)}件 · 最大50件ずつ表示</p>
          {!jobs.data.items.length && <Empty>条件に合う求人はありません。検索条件を変更してください。</Empty>}
          <ul className="saas-record-list">{jobs.data.items.map((item) => <li key={item.id}>
            <button aria-pressed={selected === item.id} onClick={() => { setSelected(item.id); setShowDetail(true); }}>
              <StatusBadge tone={item.status === "open" ? "positive" : "neutral"} icon={item.status === "open" ? "checkCircle" : "close"}>
                {item.business_type === "placement" ? "職業紹介" : "通常派遣"} · {item.status === "open" ? "募集中" : "募集終了"}</StatusBadge>
              <strong>{item.title}</strong><span className="saas-record-meta"><Icon name="building" />{item.company_name}</span>
              <span className="saas-record-meta saas-hint"><Icon name="location" />{item.site.area.label} · 時給{number(item.hourly_pay_min)}円〜</span>
              <span className="saas-record-meta saas-hint"><Icon name="clock" />{scheduleLabel(item.schedule)}</span>
            </button>
          </li>)}</ul>
          <Pagination next={jobs.data.next_cursor} previous={paging.previous} page={paging.page}
            onNext={() => paging.next(jobs.data?.next_cursor)} onPrevious={paging.back} disabled={jobs.loading} />
        </>}
      </section>
      <section className="saas-detail-pane" aria-label="求人の詳細とマッチング">
        <button className="saas-back" onClick={() => { setShowDetail(false); focusSelection(list.current); }}><Icon name="left" />求人一覧へ戻る</button>
        {selected ? <>
          <h3 ref={heading} tabIndex={-1}>{job.data?.title ?? "求人詳細"}</h3>
          <LoadState resource={job} />
          {job.data && <>
            {job.data.business_type === "dispatch" && <DispatchNotice />}
            <div className="saas-task-toolbar" aria-label="選択中の求人の操作" role="group">
              <button className="saas-primary" disabled={actions.disabled || job.loading || !!job.error || job.data.status !== "open"}
                onClick={() => void start(false)}><Icon name="match" />この求人のマッチングを実行</button>
              {run.data?.status === "completed" && <button onClick={() => {
                candidatesHeading.current?.focus({ preventScroll: true });
                candidatesHeading.current?.scrollIntoView({ block: "start", behavior: "instant" });
              }}><Icon name="people" />候補者の比較へ移動</button>}
            </div>
            <JobProfile key={job.data.id} job={job.data} />
          </>}
        </> : <><h3 ref={heading} tabIndex={-1}>マッチング</h3><Empty>求人を選ぶと、勤務条件、就業先、マッチング候補を確認できます。</Empty></>}
        <section className="saas-run-section" aria-label="マッチング実行履歴">
          <h3 className="ui-heading"><Icon name="history" />マッチングの実行状況</h3>
          <LoadState resource={runs} />
          {runs.data && (runs.data.items.length ? <label>実行履歴を選ぶ<select aria-label="実行履歴を選ぶ" value={selectedRunId}
            onChange={(event) => setRunId(event.target.value)}>
            {runs.data.items.map((item) => <option key={item.id} value={item.id}>
              {dateTime(item.created_at)} · {runLabel[item.status]} · {item.id.slice(-8)}
            </option>)}
            {runId && !runs.data.items.some((item) => item.id === runId) && <option value={runId}>選択中の実行 · {runId.slice(-8)}</option>}
          </select></label> : <Empty>まだ実行はありません。求人を選ぶか、全求人で実行してください。</Empty>)}
          {runs.data && (runPaging.previous || runs.data.next_cursor) && <Pagination next={runs.data.next_cursor}
            previous={runPaging.previous} page={runPaging.page} disabled={runs.loading}
            onNext={() => { runPaging.next(runs.data?.next_cursor); setRunId(""); }}
            onPrevious={() => { runPaging.back(); setRunId(""); }} />}
          {selectedRunId && <LoadState resource={run} />}
          {run.data && <RunSummary run={run.data} disabled={actions.disabled || run.loading || !!run.error}
            cancel={() => void actions.perform("マッチングの中止を受け付けました。", () => saasApi.cancelRun(run.data!.id))} />}
          {pins.length > 0 && <div className="saas-notice">
            <h4>次回の実行で固定する組み合わせ（{pins.length}/100）</h4>
            <ul>{pins.map((pin) => <li key={`${pin.jobId}:${pin.personId}`}>{pin.title} ↔ {pin.name}
              <button disabled={actions.disabled} onClick={() => setPins((old) => old.filter((item) => item !== pin))}
                aria-label={`${pin.name}の固定を解除`}>解除</button></li>)}</ul>
            <p>固定は次の実行でサーバーが再検証します。採用・紹介はまだ確定していません。</p>
          </div>}
        </section>
        {job.data && run.data?.status === "completed" && <Candidates key={`${job.data.id}:${run.data.id}`}
          job={job.data} run={run.data} active={active} headingRef={candidatesHeading}
          actions={{ ...actions, disabled: actions.disabled || !!job.error || job.loading || !!run.error || run.loading }}
          pins={pins} togglePin={togglePin} />}
      </section>
    </div>
  </>;
}

function RunSummary({ run, disabled, cancel }: { run: MatchRun; disabled: boolean; cancel: () => void }) {
  return <div className="saas-run">
    <div className="saas-section-title">
      <StatusBadge tone={run.status === "completed" ? "positive" : run.status === "failed" ? "danger" : run.status === "cancelled" ? "neutral" : "info"}
        icon={run.status === "completed" ? "checkCircle" : run.status === "failed" ? "warning" : run.status === "cancelled" ? "close" : "clock"}>
        {runLabel[run.status]} · {run.progress}%</StatusBadge>
      {["queued", "running"].includes(run.status) && <button disabled={disabled} onClick={cancel}><Icon name="close" />計算を中止</button>}</div>
    <progress value={run.progress} max={100} aria-label="マッチングの進捗" />
    <p className="saas-hint">{dateTime(run.created_at)} · 試行{run.attempt}回 · {run.model_version}</p>
    {run.error && <p className="saas-alert" role="alert">{run.error}</p>}
    {run.status === "cancelled" && <p>中止した実行の結果は紹介に使えません。必要に応じて新しい実行を開始してください。</p>}
    <dl className="saas-highlights saas-run-metrics">
      <Fact label="対象範囲" icon="briefcase">求人{number(run.coverage.jobs)}件・人材{number(run.coverage.people)}名</Fact>
      <Fact label="推薦" icon="star"><strong>{number(run.recommended_count)}</strong>組</Fact>
      <Fact label="経路未確認" icon="route"><strong>{number(run.coverage.route_unknown)}</strong>組</Fact>
    </dl>
    <p className="saas-notice"><Icon name="info" /> {run.coverage.truncated ? "候補探索の上限に達しました。全組み合わせの評価ではありません。" : "表示された探索範囲に対する結果です。"}
      最適性や就業の成立を保証するものではありません。推薦以外の適格な候補も選べます。</p>
    <details><summary>計算の詳細・探索範囲</summary><dl className="saas-facts">
      <Fact label="候補探索">{number(run.coverage.retrieved_pairs)}組を抽出、{number(run.coverage.evaluated_pairs)}組を評価</Fact>
      <Fact label="適格・推薦">{number(run.coverage.eligible_pairs)}組が適格、{number(run.recommended_count)}組を推薦</Fact>
      <Fact label="ソルバー">{run.solver_status ?? "未確定"} ／ 目的関数の境界：{run.objective_bound === null ? "未取得" : number(run.objective_bound)}</Fact>
    </dl>{run.coverage.notes.length > 0 && <ul>{run.coverage.notes.map((note, i) => <li key={i}>{note}</li>)}</ul>}</details>
  </div>;
}

function Candidates({ job, run, active, actions, pins, togglePin, headingRef }: {
  job: Job; run: MatchRun; active: boolean; actions: Actions; pins: Pin[];
  togglePin: (candidate: Candidate, job: Job) => void;
  headingRef: Ref<HTMLHeadingElement>;
}) {
  const paging = usePaging();
  const [chosen, setChosen] = useState("");
  const [compare, setCompare] = useState<string[]>([]);
  const [profile, setProfile] = useState("");
  const choiceHeading = useRef<HTMLHeadingElement>(null);
  const candidates = useResource(`candidates:${run.id}:${job.id}:${paging.cursor}`,
    (signal) => saasApi.candidates(run.id, job.id, paging.cursor, signal), active, actions.revision);
  const person = useResource(`candidate-person:${profile}`, (signal) => saasApi.person(profile, signal), active && !!profile, actions.revision);
  const selected = candidates.data?.items.find((item) => item.person_id === chosen);
  const compared = candidates.data?.items.filter((item) => compare.includes(item.person_id)) ?? [];
  const blocked = actions.disabled || candidates.loading || !!candidates.error;
  async function shortlist(item: Candidate) {
    await actions.perform(`${item.person_name}さんを紹介候補にしました。採用の確定ではありません。`,
      () => saasApi.shortlist({ run_id: run.id, job_id: job.id, person_id: item.person_id,
        expected_job_version: item.job_version, expected_person_version: item.person_version, idempotency_key: idempotencyKey() }));
  }
  return <section className="saas-candidates" aria-label="候補者の比較">
    <h3 ref={headingRef} tabIndex={-1} className="ui-heading"><Icon name="people" />候補者を比較する</h3>
    <p>推薦は判断材料です。適格性・評価の根拠・通勤条件を確認してください。
      {job.business_type === "placement" ? "紹介候補の確定は採用の確定ではありません。" : "通常派遣では管理会社内の条件確認に限り、派遣先の候補者選考には使いません。"}</p>
    <LoadState resource={candidates} />
    {candidates.data && <>
      <p className="saas-count">{number(candidates.data.total)}名 · 1ページ{CANDIDATE_PAGE_SIZE}名 · ページ内で最大3名を比較</p>
      {!candidates.data.items.length && <Empty>この実行・求人の候補はありません。実行対象と探索範囲をご確認ください。</Empty>}
      <ul className="saas-candidate-list">{candidates.data.items.map((item, index) => {
        const valid = item.eligible && item.job_id === job.id && item.job_version === job.version && job.status === "open";
        const pinned = pins.some((pin) => pin.jobId === job.id && pin.personId === item.person_id);
        return <li key={item.person_id} className={chosen === item.person_id ? "saas-chosen" : ""}>
          <div className="saas-section-title"><h4 className="ui-heading"><Icon name="person" />{(paging.page - 1) * CANDIDATE_PAGE_SIZE + index + 1}. {item.person_name}</h4>
            <div className="saas-badges">
              <StatusBadge tone={item.eligible ? "positive" : "warning"} icon={item.eligible ? "checkCircle" : "warning"}>{item.eligible ? "適格" : "条件不適合"}</StatusBadge>
              {item.recommended && <StatusBadge tone="recommendation" icon="star">推薦</StatusBadge>}
              {item.pinned && <StatusBadge tone="info" icon="pin">実行時に固定</StatusBadge>}
            </div></div>
          <div className="saas-candidate-summary"><p className="saas-score">総合：<strong>{item.score === null ? "評価なし" : item.score.toFixed(1)}</strong></p>
            <p className="saas-record-meta"><Icon name="location" />{item.home_area.label}</p></div>
          <p className="saas-record-meta"><Icon name="route" /><span>{item.commute.status !== "estimated" || item.commute.duration_seconds === null ? "通勤時間は不明" : `通勤概算 ${Math.ceil(item.commute.duration_seconds / 60)}分`} · {item.commute.summary}</span></p>
          {item.reasons.length > 0 && <ul>{item.reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul>}
          <div className="saas-actions">
            <button aria-pressed={chosen === item.person_id} onClick={() => setChosen(item.person_id)}>
              <Icon name={chosen === item.person_id ? "check" : "person"} />
              {job.business_type === "dispatch" ? "条件・根拠を確認"
                : chosen === item.person_id ? "選択中" : item.recommended ? "推薦候補を確認" : "この人を手動選択"}
            </button>
            {chosen === item.person_id && <button onClick={() => choiceHeading.current?.focus()}>
              <Icon name="right" />
              {job.business_type === "placement" ? "この候補の詳細・紹介操作へ" : "この候補の条件詳細へ"}
            </button>}
            <label className="saas-check"><input type="checkbox" checked={compare.includes(item.person_id)}
              disabled={!compare.includes(item.person_id) && compare.length >= 3}
              onChange={(event) => setCompare((old) => event.target.checked ? [...old, item.person_id] : old.filter((id) => id !== item.person_id))} />
              {item.person_name}を比較</label>
            {job.business_type === "placement" && <button aria-pressed={pinned} disabled={blocked || !valid || (!pinned && pins.length >= 100)}
              onClick={() => togglePin(item, job)}><Icon name="pin" />{pinned ? "次回の固定を解除" : "次回の組み合わせを固定"}</button>}
          </div>
          {item.job_version !== job.version && <p className="saas-alert">求人が更新されています。マッチングを再実行してください。</p>}
        </li>;
      })}</ul>
      <Pagination next={candidates.data.next_cursor} previous={paging.previous} page={paging.page}
        onNext={() => { paging.next(candidates.data?.next_cursor); setCompare([]); setChosen(""); }}
        onPrevious={() => { paging.back(); setCompare([]); setChosen(""); }} disabled={candidates.loading} />
    </>}
    {compared.length > 0 && <section aria-label="選択した人材の評価比較">
      <h4 className="ui-heading"><Icon name="compare" />比較表とレーダー</h4><div className="saas-compare">{compared.map((item) => <article key={item.person_id}>
        <h5>{item.person_name}</h5><Radar scores={item.scores} name={item.person_name} />
      </article>)}</div>
    </section>}
    {selected && <section className="saas-choice" aria-label="選択した候補者">
      <h4 className="ui-heading" ref={choiceHeading} tabIndex={-1}><Icon name="checkCircle" />{selected.person_name}さんを確認</h4>
      <p>{job.business_type === "dispatch" ? "管理会社による適格性・通勤条件の確認です。派遣先の候補者選考ではありません。"
        : selected.recommended ? "この実行の推薦候補です。" : "担当者が手動で選択した候補です。推薦の有無で適格性の条件は変わりません。"}</p>
      <Radar scores={selected.scores} name={selected.person_name} />
      <h5>評価の根拠</h5>
      {selected.evidence.length ? <ul>{selected.evidence.map((evidence, i) => <li key={i}>{evidence}</li>)}</ul> : <p>追加の根拠情報なし</p>}
      <PublicMap site={job.site} area={selected.home_area} commute={selected.commute} />
      <div className="saas-actions">
        <button onClick={() => setProfile(profile === selected.person_id ? "" : selected.person_id)}>
          <Icon name="history" />
          {profile === selected.person_id ? "職歴を閉じる" : "資格・職歴・評価記録を見る"}</button>
        {job.business_type === "placement" && <button className="saas-primary"
          disabled={blocked || !selected.eligible || selected.job_id !== job.id || selected.job_version !== job.version || job.status !== "open"}
          onClick={() => void shortlist(selected)}><Icon name="introduction" />紹介候補を確定</button>}
      </div>
      {job.business_type === "dispatch" ? <DispatchNotice /> : <p className="saas-hint">確定後は「紹介」で模擬依頼・本人同意を進めます。外部メールは送信しません。</p>}
      {profile === selected.person_id && <><LoadState resource={person} />
        {person.data && <PersonProfile person={person.data} showMap={false} />}</>}
    </section>}
  </section>;
}
