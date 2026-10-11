import { useEffect, useRef, useState } from "react";
import { saasApi } from "../saas-api.ts";
import { PersonProfile } from "./Profiles.tsx";
import { Empty, focusSelection, LoadState, number, Pagination, usePaging, useResource } from "./shared.tsx";
import Icon from "../Icon.tsx";

export default function People({ active, revision }: { active: boolean; revision: number }) {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState("");
  const [showDetail, setShowDetail] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const list = useRef<HTMLElement>(null);
  const paging = usePaging();
  const people = useResource(`people:${query}:${paging.cursor}`,
    (signal) => saasApi.people({ q: query, cursor: paging.cursor }, signal), active, revision);
  const person = useResource(`person:${selected}`, (signal) => saasApi.person(selected, signal), active && !!selected, revision);
  useEffect(() => { if (showDetail) heading.current?.focus(); }, [selected, showDetail]);
  return <>
    <h2 className="ui-heading"><Icon name="people" />登録人材</h2><p className="saas-hint">管理会社内の架空の人材情報です。居住地はエリア単位で表示します。</p>
    <div className={`saas-split ${showDetail ? "saas-show-detail" : ""}`}>
      <section ref={list} className="saas-list-pane" aria-label="人材の検索と一覧">
        <form className="saas-search" onSubmit={(event) => { event.preventDefault(); setQuery(draft.trim()); paging.reset(); }}>
          <label>人材を検索<input type="search" maxLength={200} value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="氏名、職種、スキル" /></label>
          <button type="submit"><Icon name="search" />人材を検索</button>
        </form>
        <LoadState resource={people} />
        {people.data && <>
          <p className="saas-count">{number(people.data.total)}名 · 最大50名ずつ表示</p>
          {people.data.items.length === 0 && <Empty>条件に合う人材はありません。</Empty>}
          <ul className="saas-record-list">{people.data.items.map((item) => <li key={item.id}>
            <button aria-pressed={item.id === selected} onClick={() => { setSelected(item.id); setShowDetail(true); }}>
              <strong className="saas-record-meta"><Icon name="person" />{item.name}</strong><span>{item.occupation}</span>
              <span className="saas-record-meta saas-hint"><Icon name="location" />{item.home_area.label} ／ 希望時給{number(item.desired_hourly_pay)}円</span>
              <span className="saas-record-meta saas-hint"><Icon name="shield" />{item.qualifications.map((q) => q.name).join("・") || "資格登録なし"}</span>
            </button>
          </li>)}</ul>
          <Pagination next={people.data.next_cursor} previous={paging.previous} page={paging.page}
            onNext={() => paging.next(people.data?.next_cursor)} onPrevious={paging.back} disabled={people.loading} />
        </>}
      </section>
      <section className="saas-detail-pane" aria-label="人材詳細">
        {selected ? <>
          <button className="saas-back" onClick={() => { setShowDetail(false); focusSelection(list.current); }}><Icon name="left" />人材一覧へ戻る</button>
          <h3 ref={heading} tabIndex={-1}>{person.data?.name ?? "人材詳細"}</h3>
          <LoadState resource={person} />
          {person.data && <PersonProfile person={person.data} />}
        </> : <Empty>人材を選んで、資格・勤務希望・過去の職歴と評価をご確認ください。</Empty>}
      </section>
    </div>
  </>;
}
