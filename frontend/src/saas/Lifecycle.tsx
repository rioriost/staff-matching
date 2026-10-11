import { useEffect, useRef, useState } from "react";
import { saasApi, versioned } from "../saas-api.ts";
import type { Appointment, AppointmentRequest, Introduction, SimulationAction } from "../saas-models.ts";
import type { Actions } from "./shared.tsx";
import { AppointmentStatus, dateTime, DispatchNotice, Empty, Fact, focusSelection, IntroductionStatus, LoadState, Pagination, toTokyoInput, tokyoInputToISO, usePaging, useResource } from "./shared.tsx";
import { PersonProfile } from "./Profiles.tsx";
import Icon from "../Icon.tsx";
import type { IconName } from "../Icon.tsx";

function ConfirmAction({ label, explanation, disabled, action, danger = false }: {
  label: string; explanation: string; disabled: boolean; action: () => Promise<unknown>; danger?: boolean;
}) {
  const [confirm, setConfirm] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (confirm) cancel.current?.focus(); }, [confirm]);
  function close() {
    setConfirm(false);
    requestAnimationFrame(() => {
      if (trigger.current?.isConnected) trigger.current.focus();
      else document.querySelector<HTMLElement>("#saas-main h1")?.focus();
    });
  }
  return <div className="saas-confirm">
    <button ref={trigger} className={danger ? "saas-danger" : ""} disabled={disabled} onClick={() => setConfirm(true)}><Icon name={danger ? "close" : "checkCircle"} />{label}</button>
    {confirm && <div className="saas-notice" role="group" aria-label={`${label}の確認`}>
      <p><Icon name="warning" /> {explanation}</p><div className="saas-actions">
        <button ref={cancel} disabled={disabled} onClick={close}><Icon name="left" />戻る</button>
        <button disabled={disabled} className={danger ? "saas-danger" : "saas-primary"}
          onClick={() => void action().then((result) => { if (result !== undefined) close(); })}><Icon name={danger ? "close" : "check"} />確認して{label}</button>
      </div>
    </div>}
  </div>;
}

const introductionStages: { label: string; icon: IconName }[] = [
  { label: "紹介候補", icon: "person" }, { label: "模擬紹介依頼", icon: "mail" },
  { label: "模擬本人同意", icon: "shield" }, { label: "面接調整", icon: "calendar" },
  { label: "成約（デモ）", icon: "checkCircle" },
];
const stageForStatus: Record<Introduction["status"], number | null> = {
  shortlisted: 0, requested: 1, consented: 2, interview_proposed: 3, interview_accepted: 3, placed: 4,
  declined: null, cancelled: null, expired: null,
};
function IntroductionProgress({ status }: { status: Introduction["status"] }) {
  const current = stageForStatus[status];
  return <ol className="saas-flow" aria-label="紹介の進行状況">
    {introductionStages.map((stage, index) => {
      const complete = current !== null && (index < current || status === "placed");
      return <li key={stage.label} className={complete ? "saas-flow-complete" : ""}
        aria-current={index === current ? "step" : undefined}>
        <span className="saas-flow-symbol"><Icon name={complete ? "check" : stage.icon} /></span>
        <span>{complete && <span className="sr-only">完了：</span>}{stage.label}</span>
      </li>;
    })}
  </ol>;
}

export function Introductions({ active, actions }: { active: boolean; actions: Actions }) {
  const [selected, setSelected] = useState("");
  const [showDetail, setShowDetail] = useState(false);
  const paging = usePaging();
  const intros = useResource(`introductions:${paging.cursor}`,
    (signal) => saasApi.introductions(signal, paging.cursor), active, actions.revision);
  const chosen = intros.data?.items.find((intro) => intro.id === selected);
  const detailHeading = useRef<HTMLHeadingElement>(null);
  const list = useRef<HTMLElement>(null);
  useEffect(() => { if (showDetail) detailHeading.current?.focus(); }, [selected, showDetail]);
  return <>
    <h2 className="ui-heading"><Icon name="introduction" />紹介</h2>
    <p className="saas-hint">紹介候補から成約までの進行状況を確認します。依頼・本人同意・通知はデモ内の模擬操作です。</p>
    <div className={`saas-split ${showDetail ? "saas-show-detail" : ""}`}>
      <section ref={list} className="saas-list-pane" aria-label="紹介一覧">
        <LoadState resource={intros} />
        {intros.data && <>
          <p className="saas-count">{intros.data.total}件</p>
          {!intros.data.items.length && <Empty>紹介候補はまだありません。「求人」で候補者を比較して紹介候補を確定してください。</Empty>}
          <ul className="saas-record-list">{intros.data.items.map((intro) => <li key={intro.id}>
            <button aria-pressed={selected === intro.id} onClick={() => { setSelected(intro.id); setShowDetail(true); }}>
              <IntroductionStatus status={intro.status} /><strong className="saas-record-meta"><Icon name="person" />{intro.candidate_alias}</strong>
              <span className="saas-hint">求人 {intro.job_id}</span><span className="saas-hint">登録 {dateTime(intro.created_at)}</span>
            </button>
          </li>)}</ul>
          <Pagination next={intros.data.next_cursor} previous={paging.previous} page={paging.page} disabled={intros.loading}
            onNext={() => { paging.next(intros.data?.next_cursor); setSelected(""); setShowDetail(false); }}
            onPrevious={() => { paging.back(); setSelected(""); setShowDetail(false); }} />
        </>}
      </section>
      <section className="saas-detail-pane" aria-label="紹介詳細">
        <button className="saas-back" onClick={() => { setShowDetail(false); focusSelection(list.current); }}><Icon name="left" />紹介一覧へ戻る</button>
        <h3 ref={detailHeading} tabIndex={-1}>{chosen ? `${chosen.candidate_alias}の紹介` : "紹介を選択"}</h3>
        {chosen ? <IntroductionDetail key={chosen.id} intro={chosen} active={active}
          actions={{ ...actions, disabled: actions.disabled || intros.loading || !!intros.error }} />
          : <Empty>紹介を選ぶと、管理会社内の氏名・依頼状況・同意状況を確認できます。</Empty>}
      </section>
    </div>
  </>;
}

function IntroductionDetail({ intro, active, actions }: { intro: Introduction; active: boolean; actions: Actions }) {
  const [showProfile, setShowProfile] = useState(false);
  const job = useResource(`intro-job:${intro.job_id}`, (signal) => saasApi.job(intro.job_id, signal), active, actions.revision);
  const person = useResource(`intro-person:${intro.person_id}`, (signal) => saasApi.person(intro.person_id, signal), active, actions.revision);
  const companies = useResource("companies", saasApi.companies, active, actions.revision);
  const company = companies.data?.items.find((item) => item.id === intro.company_id);
  const disabled = actions.disabled || job.loading || person.loading || !!job.error || !!person.error;
  const closed = ["placed", "declined", "cancelled", "expired"].includes(intro.status);
  const placement = job.data?.business_type === "placement";
  const simulate = (action: SimulationAction, message: string) =>
    actions.perform(message, () => saasApi.simulate(intro.id, { ...versioned(intro.version), action, simulated: true }));
  return <>
    <IntroductionStatus status={intro.status} version={intro.version} />
    <IntroductionProgress status={intro.status} />
    <LoadState resource={job} /><LoadState resource={person} />
    <dl className="saas-facts">
      <Fact label="管理会社内の氏名">{person.data?.name ?? "確認中"}</Fact>
      <Fact label="求人">{job.data?.title ?? intro.job_id}</Fact>
      <Fact label="募集企業">{job.data?.company_name ?? intro.company_id}</Fact>
      <Fact label="紹介の有効期限">{dateTime(intro.expires_at)}（日本時間）</Fact>
      <Fact label="通知">{intro.simulated ? "アプリ内の模擬通知のみ" : "デモの記録。外部送信なし"}</Fact>
    </dl>
    {company && <details><summary>募集企業の情報（架空）</summary><p>{company.description}</p>
      <p>{company.industry} ／ 架空の連絡先：{company.contact_email}</p></details>}
    <p className="saas-notice">企業向け受信箱の初期表示は候補者の別名です。ここは管理会社内の詳細画面です。実在の個人情報や同意書は登録しないでください。</p>
    {job.data?.business_type === "dispatch" && <DispatchNotice />}
    {placement && <>
      {actions.canSimulate && !closed && <section className="saas-workflow" aria-label="デモの依頼と本人同意">
        <h4 className="ui-heading"><Icon name="shield" />デモの依頼・本人同意</h4>
        <p>担当者が企業／本人の操作を模擬します。実際の依頼・同意の証明にはなりません。</p>
        <div className="saas-actions">
          {intro.status === "shortlisted" && <button disabled={disabled} className="saas-primary"
            onClick={() => void simulate("company_request", "模擬紹介依頼を記録しました。外部メールは送信していません。")}><Icon name="mail" />模擬紹介依頼</button>}
          {intro.status === "requested" && <button disabled={disabled} className="saas-primary"
            onClick={() => void simulate("person_consent", "模擬本人同意を記録しました。実際の同意ではありません。")}><Icon name="shield" />模擬本人同意</button>}
          {["requested", "consented"].includes(intro.status) && <ConfirmAction disabled={disabled} danger label="模擬本人辞退"
            explanation="この紹介を模擬的に辞退します。実際の本人の意思表示ではありません。"
            action={() => simulate("person_decline", "模擬本人辞退を記録しました。")} />}
          {["consented", "interview_proposed", "interview_accepted"].includes(intro.status) &&
            <ConfirmAction disabled={disabled} danger label="模擬同意撤回"
              explanation="模擬同意を撤回し、この紹介と関連する面接を取り消します。"
              action={() => simulate("withdraw_consent", "模擬同意撤回を記録しました。")} />}
        </div>
      </section>}
      {intro.status === "consented" && <section className="saas-workflow" aria-label="面接日時の提案">
        <h4 className="ui-heading"><Icon name="calendar" />面接日時を提案</h4><AppointmentForm key={intro.id} companyId={intro.company_id}
          version={intro.version} disabled={disabled} revision={actions.revision}
          submit={(body) => actions.perform("面接日時を提案しました。日時の合意はまだ完了していません。",
            () => saasApi.propose(intro.id, body))} />
      </section>}
      {["interview_proposed", "interview_accepted"].includes(intro.status) &&
        <button onClick={() => actions.navigate("appointments")}><Icon name="calendar" />面接の日程と合意を確認</button>}
      {intro.status === "interview_accepted" && <ConfirmAction disabled={disabled}
        label="成約を記録" explanation="デモ上の成約を記録します。紹介候補の確定とは異なり、サーバーが同意・期間・重複配置を再検証します。実際の雇用契約を作成する操作ではありません。"
        action={() => actions.perform("デモの成約を記録しました。実際の雇用契約ではありません。", () => saasApi.place(intro.id, versioned(intro.version)))} />}
    </>}
    {!closed && <ConfirmAction disabled={disabled} danger label="紹介を取り消す"
      explanation="この紹介と関連する面接を取り消します。取消後の紹介をそのまま再開することはできません。"
      action={() => actions.perform("紹介を取り消しました。", () => saasApi.cancelIntroduction(intro.id, versioned(intro.version)))} />}
    <button onClick={() => setShowProfile(!showProfile)}><Icon name="person" />{showProfile ? "人材情報を閉じる" : "人材情報を確認"}</button>
    {showProfile && person.data && <PersonProfile person={person.data} />}
  </>;
}

function AppointmentForm({ companyId, version, disabled, revision, submit, existing }: {
  companyId: string; version: number; disabled: boolean; revision: number;
  submit: (body: AppointmentRequest) => Promise<unknown>; existing?: Appointment;
}) {
  const [start, setStart] = useState(existing ? toTokyoInput(existing.start_at) : "");
  const [end, setEnd] = useState(existing ? toTokyoInput(existing.end_at) : "");
  const [online, setOnline] = useState(existing?.online ?? true);
  const [siteId, setSiteId] = useState(existing?.site_id ?? "");
  const [problem, setProblem] = useState("");
  const sites = useResource(`appointment-sites:${companyId}`, (signal) => saasApi.sites(companyId, signal), true, revision);
  const authorizedSites = sites.data?.items.filter((site) => site.company_id === companyId) ?? [];
  return <form className="saas-appointment-form" onSubmit={(event) => {
    event.preventDefault(); setProblem("");
    try {
      const startAt = tokyoInputToISO(start);
      const endAt = tokyoInputToISO(end);
      const duration = Date.parse(endAt) - Date.parse(startAt);
      if (Date.parse(startAt) <= Date.now()) throw new Error("開始日時は現在より後にしてください。");
      if (duration <= 0 || duration > 4 * 3_600_000) throw new Error("面接時間は0分より長く、4時間以内にしてください。");
      if (!online && !authorizedSites.some((site) => site.id === siteId)) throw new Error("募集企業の拠点を選んでください。");
      void submit({ ...versioned(version), start_at: startAt, end_at: endAt, online, site_id: online ? null : siteId });
    } catch (error) { setProblem(error instanceof Error ? error.message : "入力を確認してください。"); }
  }}>
    <p className="saas-hint">入力時刻は日本時間（Asia/Tokyo・UTC+09:00）です。最大4時間。保存先はデモ内の予定のみで、外部カレンダーには登録しません。</p>
    <div className="saas-form-grid">
      <label>開始日時（日本時間）<input type="datetime-local" required value={start} disabled={disabled}
        onChange={(event) => setStart(event.target.value)} /></label>
      <label>終了日時（日本時間）<input type="datetime-local" required value={end} disabled={disabled}
        onChange={(event) => setEnd(event.target.value)} /></label>
    </div>
    <label>開催方法<select aria-label="開催方法" value={online ? "online" : "site"} disabled={disabled} onChange={(event) => setOnline(event.target.value === "online")}>
      <option value="online">オンライン（接続先は別途調整）</option><option value="site">募集企業の拠点</option>
    </select></label>
    {!online && <><LoadState resource={sites} /><label>会場<select aria-label="会場" value={siteId} required disabled={disabled || sites.loading || !!sites.error}
      onChange={(event) => setSiteId(event.target.value)}>
      <option value="">会場を選んでください</option>{authorizedSites.map((site) => <option key={site.id} value={site.id}>{site.name} — {site.address}</option>)}
    </select></label>{!sites.loading && !authorizedSites.length && <p>この企業の会場情報がありません。オンラインを選択してください。</p>}</>}
    {problem && <p className="saas-alert" role="alert">{problem}</p>}
    <button className="saas-primary" disabled={disabled || (!online && (sites.loading || !!sites.error || !siteId))} type="submit">
      <Icon name="calendar" />
      {existing ? "変更日時を再提案" : "面接日時を提案"}</button>
    {existing && <p className="saas-hint">変更後は再び「提案中」となり、再度の合意が必要です。</p>}
  </form>;
}

export function Appointments({ active, actions }: { active: boolean; actions: Actions }) {
  const [selected, setSelected] = useState("");
  const [showDetail, setShowDetail] = useState(false);
  const paging = usePaging();
  const appointments = useResource(`appointments:${paging.cursor}`,
    (signal) => saasApi.appointments(signal, paging.cursor), active, actions.revision);
  const chosen = appointments.data?.items.find((item) => item.id === selected);
  const heading = useRef<HTMLHeadingElement>(null);
  const list = useRef<HTMLElement>(null);
  useEffect(() => { if (showDetail) heading.current?.focus(); }, [selected, showDetail]);
  return <>
    <h2 className="ui-heading"><Icon name="calendar" />面接・日程調整</h2><p className="saas-hint">本人同意後の職業紹介に限ります。日時はすべて日本時間です。既存の予定を自動生成することはありません。</p>
    <div className={`saas-split ${showDetail ? "saas-show-detail" : ""}`}>
      <section ref={list} className="saas-list-pane" aria-label="面接一覧"><LoadState resource={appointments} />
        {appointments.data && <>
          <p className="saas-count">{appointments.data.total}件</p>
          {!appointments.data.items.length && <Empty>登録された面接はありません。「紹介」で模擬本人同意後に日時を提案してください。</Empty>}
          <ul className="saas-record-list">{appointments.data.items.map((item) => <li key={item.id}>
            <button aria-pressed={selected === item.id} onClick={() => { setSelected(item.id); setShowDetail(true); }}>
              <AppointmentStatus status={item.status} /><strong className="saas-record-meta"><Icon name="calendar" />{dateTime(item.start_at)}</strong>
              <span>{item.online ? "オンライン" : "企業の拠点"} ／ 求人 {item.job_id}</span>
            </button>
          </li>)}</ul>
          <Pagination next={appointments.data.next_cursor} previous={paging.previous} page={paging.page} disabled={appointments.loading}
            onNext={() => { paging.next(appointments.data?.next_cursor); setSelected(""); setShowDetail(false); }}
            onPrevious={() => { paging.back(); setSelected(""); setShowDetail(false); }} />
        </>}
      </section>
      <section className="saas-detail-pane" aria-label="面接詳細">
        <button className="saas-back" onClick={() => { setShowDetail(false); focusSelection(list.current); }}><Icon name="left" />面接一覧へ戻る</button>
        <h3 ref={heading} tabIndex={-1}>{chosen ? "面接の日時と会場" : "面接を選択"}</h3>
        {chosen ? <AppointmentDetail key={chosen.id} appointment={chosen} active={active}
          actions={{ ...actions, disabled: actions.disabled || appointments.loading || !!appointments.error }} />
          : <Empty>面接を選ぶと、日時の合意、変更、取消ができます。</Empty>}
      </section>
    </div>
  </>;
}
function AppointmentDetail({ appointment, active, actions }: { appointment: Appointment; active: boolean; actions: Actions }) {
  const [editing, setEditing] = useState(false);
  const job = useResource(`appointment-job:${appointment.job_id}`, (signal) => saasApi.job(appointment.job_id, signal), active, actions.revision);
  const person = useResource(`appointment-person:${appointment.person_id}`, (signal) => saasApi.person(appointment.person_id, signal), active, actions.revision);
  const sites = useResource(`appointment-locations:${appointment.company_id}`, (signal) => saasApi.sites(appointment.company_id, signal), active, actions.revision);
  const site = sites.data?.items.find((item) => item.id === appointment.site_id && item.company_id === appointment.company_id);
  const disabled = actions.disabled || job.loading || !!job.error;
  const placement = job.data?.business_type === "placement";
  return <>
    <LoadState resource={job} /><LoadState resource={person} />
    <AppointmentStatus status={appointment.status} version={appointment.version} />
    <dl className="saas-facts">
      <Fact label="人材">{person.data?.name ?? appointment.person_id}</Fact>
      <Fact label="求人">{job.data?.title ?? appointment.job_id}</Fact>
      <Fact label="日時">{dateTime(appointment.start_at)} – {dateTime(appointment.end_at)}（Asia/Tokyo）</Fact>
      <Fact label="開催方法">{appointment.online ? "オンライン（接続先は別途調整）" : site ? `${site.name} ／ ${site.address}` : "会場情報を確認中"}</Fact>
    </dl>
    {!appointment.online && <LoadState resource={sites} />}
    {job.data?.business_type === "dispatch" && <DispatchNotice />}
    {placement && appointment.status !== "cancelled" && <>
      <div className="saas-actions">
        {appointment.status === "proposed" && actions.canSimulate && <button className="saas-primary" disabled={disabled}
          onClick={() => void actions.perform("面接日時への合意をデモ上で記録しました。外部通知は送信していません。",
            () => saasApi.acceptAppointment(appointment.id, versioned(appointment.version)))}><Icon name="checkCircle" />提案日時に合意（デモ）</button>}
        <button disabled={disabled} aria-expanded={editing} onClick={() => setEditing(!editing)}><Icon name="calendar" />{editing ? "日時変更を閉じる" : "日時を変更"}</button>
        <ConfirmAction disabled={disabled} danger label="面接を取り消す" explanation="この面接の予定を取り消します。紹介そのものの取消とは別の操作です。"
          action={() => actions.perform("面接を取り消しました。", () => saasApi.cancelAppointment(appointment.id, versioned(appointment.version)))} />
      </div>
      {editing && <AppointmentForm companyId={appointment.company_id} existing={appointment} version={appointment.version}
        disabled={disabled} revision={actions.revision} submit={async (body) => {
          const result = await actions.perform("変更日時を再提案しました。再度の合意が必要です。", () => saasApi.reschedule(appointment.id, body));
          if (result) setEditing(false);
          return result;
        }} />}
    </>}
  </>;
}
