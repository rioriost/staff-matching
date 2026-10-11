import { useState } from "react";
import { saasApi } from "../saas-api.ts";
import type { Notification } from "../saas-models.ts";
import type { Actions } from "./shared.tsx";
import { dateTime, Empty, LoadState, Pagination, StatusBadge, usePaging, useResource } from "./shared.tsx";
import Icon from "../Icon.tsx";

export default function Inbox({ active, actions }: { active: boolean; actions: Actions }) {
  const [audience, setAudience] = useState<Notification["audience"]>("management");
  const paging = usePaging();
  const notifications = useResource(`notifications:${audience}:${paging.cursor}`,
    (signal) => saasApi.notifications(audience, paging.cursor, signal), active, actions.revision);
  return <>
    <h2 className="ui-heading"><Icon name="inbox" />デモ受信箱</h2>
    <p className="saas-notice"><Icon name="info" /> この管理会社に属する模擬通知です。外部メール・SMSは送信しません。実際の企業や本人が操作した記録ではありません。</p>
    <fieldset className="saas-audience"><legend>表示する受信箱</legend>
      {([{ value: "management", label: "管理会社" }, { value: "company", label: "募集企業" }, { value: "person", label: "本人" }] satisfies
        { value: Notification["audience"]; label: string }[]).filter((item) => actions.canSimulate || item.value === "management").map((item) => <label className="saas-check" key={item.value}>
        <input type="radio" name="saas-audience" checked={audience === item.value}
          onChange={() => { setAudience(item.value); paging.reset(); }} />{item.label}
      </label>)}
    </fieldset>
    {audience === "company" && <p className="saas-hint">初期の紹介では候補者の別名を表示します。この画面で管理会社内の氏名を補完することはありません。</p>}
    <LoadState resource={notifications} />
    {notifications.data && <>
      <p className="saas-count">{notifications.data.total}件</p>
      {!notifications.data.items.length && <Empty>この受信箱に通知はありません。紹介や日程調整の操作後に記録されます。</Empty>}
      <ul className="saas-inbox">{notifications.data.items.map((item) => <li key={item.id} className={item.read ? "" : "saas-unread"}>
        <div className="saas-section-title"><h3 className="ui-heading"><Icon name="mail" />{item.title}</h3>
          <StatusBadge tone={item.read ? "neutral" : "info"} icon={item.read ? "check" : "mail"}>{item.read ? "既読" : "未読"} · 模擬通知</StatusBadge></div>
        <p className="saas-hint">{dateTime(item.created_at)}（日本時間）</p><p className="saas-message">{item.message}</p>
        <div className="saas-actions">
          {!item.read && <button disabled={actions.disabled || notifications.loading || !!notifications.error}
            onClick={() => void actions.perform("通知を既読にしました。", () => saasApi.read(item.id))}><Icon name="check" />既読にする</button>}
          {item.introduction_id && <button onClick={() => actions.navigate("introductions")}><Icon name="introduction" />管理会社の紹介一覧を開く</button>}
        </div>
      </li>)}</ul>
      <Pagination next={notifications.data.next_cursor} previous={paging.previous} page={paging.page}
        onNext={() => paging.next(notifications.data?.next_cursor)} onPrevious={paging.back} disabled={notifications.loading} />
    </>}
  </>;
}
