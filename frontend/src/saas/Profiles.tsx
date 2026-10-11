import type { Job, Person } from "../saas-models.ts";
import PublicMap from "./PublicMap.tsx";
import { Fact, number, scheduleLabel } from "./shared.tsx";
import Icon from "../Icon.tsx";

export function JobProfile({ job }: { job: Job }) {
  return <>
    <p className="saas-record-meta saas-hint"><Icon name="building" />{job.company_name} ／ {job.site.name}</p>
    <p className="saas-duties">{job.duties}</p>
    <dl className="saas-highlights">
      <Fact label="時給" icon="yen"><strong>{number(job.hourly_pay_min)}–{number(job.hourly_pay_max)}</strong>円</Fact>
      <Fact label="曜日・勤務時間" icon="clock">{scheduleLabel(job.schedule)}</Fact>
    </dl>
    <dl className="saas-facts">
      <Fact label="募集区分">{job.business_type === "placement" ? "職業紹介" : "通常派遣"} ／ {job.employment_type} ／ {job.headcount}名</Fact>
      <Fact label="勤務期間">{job.start_date} から {job.end_date} 未満（終了日は含みません）</Fact>
      <Fact label="必要な経験">{job.experience_months}か月</Fact>
      <Fact label="必須スキル">{Object.entries(job.required_skills).map(([name, level]) => `${name} ${level}/5`).join(" ／ ") || "指定なし"}</Fact>
      <Fact label="必須資格">{job.required_qualifications.join(" ／ ") || "指定なし"}</Fact>
      <Fact label="歓迎資格">{job.preferred_qualifications.join(" ／ ") || "指定なし"}</Fact>
    </dl>
    <details>
      <summary>契約条件・福利厚生を確認</summary>
      <dl className="saas-facts">
        <Fact label="契約更新">{job.renewal}</Fact>
        <Fact label="業務の変更範囲">{job.duties_change_scope}</Fact>
        <Fact label="就業場所の変更範囲">{job.location_change_scope}</Fact>
        <Fact label="時間外労働">{job.overtime}</Fact>
        <Fact label="休日">{job.holidays}</Fact>
        <Fact label="諸手当">{job.allowances}</Fact>
        <Fact label="福利厚生">{job.benefits.join(" ／ ")}</Fact>
        <Fact label="応募方法">{job.application}</Fact>
      </dl>
    </details>
    <details className="saas-location-details">
      <summary><Icon name="location" />就業先の地図・アクセスを確認</summary>
      <PublicMap site={job.site} />
    </details>
  </>;
}
const dimension = { attendance: "勤怠", quality: "品質", collaboration: "協働", improvement: "改善" };
export function PersonProfile({ person, showMap = true }: { person: Person; showMap?: boolean }) {
  return <>
    <p className="saas-record-meta saas-hint"><Icon name="location" />{person.occupation} ／ {person.home_area.label}</p>
    <dl className="saas-highlights">
      <Fact label="希望時給" icon="yen"><strong>{number(person.desired_hourly_pay)}</strong>円以上</Fact>
      <Fact label="通勤上限" icon="route">片道<strong>{person.max_commute_minutes}</strong>分まで</Fact>
    </dl>
    <dl className="saas-facts">
      <Fact label="希望職種">{person.preferred_occupations.join(" ／ ")}</Fact>
      <Fact label="就業可能期間">{person.available_from} から {person.available_until} 未満</Fact>
      <Fact label="就業可能時間">{scheduleLabel(person.availability)}</Fact>
      <Fact label="通勤条件">{person.commute_modes.map((mode) => mode === "car" ? "自動車" : "公共交通").join(" ／ ")}、
        運転要件：{person.driving_eligible ? "確認済み" : "未確認／対象外"}</Fact>
      <Fact label="利用駅">{person.stations.map((s) => `${s.name} 徒歩${s.walking_minutes}分`).join(" ／ ") || "情報なし"}</Fact>
      <Fact label="スキル">{Object.entries(person.skills).map(([name, level]) => `${name} ${level}/5`).join(" ／ ") || "未登録"}</Fact>
      <Fact label="資格">{person.qualifications.length ? <ul>{person.qualifications.map((q, i) =>
        <li key={`${q.name}-${i}`}>{q.name} — {q.verified ? "確認済み" : "未確認"}、
          有効期限：{q.valid_until ?? "登録なし"}</li>)}</ul> : "登録なし"}</Fact>
      <Fact label="マッチング対象">{person.matching_enabled ? "対象" : "停止中"}</Fact>
    </dl>
    <h4 className="ui-heading"><Icon name="history" />職歴と記録された評価</h4>
    <p className="saas-hint">架空の職歴・観察記録です。記録がない場合は「未確認」とし、能力が低いとは判断しません。</p>
    {person.employment_history.length === 0 ? <p className="saas-empty">職歴・評価は未確認です。評価点を0として扱いません。</p>
      : person.employment_history.map((employment, index) => <article className="saas-history" key={`${employment.start_date}-${index}`}>
        <h5>{employment.employer} ／ {employment.occupation}</h5>
        <p className="saas-hint">{employment.start_date} から {employment.end_date} 未満</p>
        <p>{employment.duties}</p>
        {employment.evaluations.length === 0 ? <p>評価記録なし</p> : <ul>{employment.evaluations.map((e, i) => <li key={i}>
          <strong>{e.recorded_at} · {dimension[e.dimension]}</strong>
          <p>{e.observation}</p>
          <span className="saas-hint">記録元：{e.source} ／ {e.score === null ? "点数未登録" : `${e.score}/5`}</span>
        </li>)}</ul>}
      </article>)}
    {showMap && <PublicMap area={person.home_area} />}
  </>;
}
