import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { errorMessage } from "../api.ts";
import type { Schedule } from "../saas-models.ts";
import Icon from "../Icon.tsx";
import type { IconName } from "../Icon.tsx";

export type Tab = "jobs" | "people" | "introductions" | "appointments" | "inbox";
export interface Actions {
  disabled: boolean;
  revision: number;
  canSimulate: boolean;
  perform: <T>(message: string, work: () => Promise<T>) => Promise<T | undefined>;
  navigate: (tab: Tab) => void;
}
interface ResourceState<T> { key: string; data?: T; error?: string; loading: boolean }
export function useResource<T>(key: string, load: (signal: AbortSignal) => Promise<T>, enabled = true, revision = 0) {
  const loader = useRef(load);
  loader.current = load;
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((value) => value + 1), []);
  const [state, setState] = useState<ResourceState<T>>({ key, loading: enabled });
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setState((old) => ({ key, data: old.key === key ? old.data : undefined, loading: true }));
    loader.current(controller.signal).then((data) => {
      if (!controller.signal.aborted) setState({ key, data, loading: false });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setState((old) => ({ ...old, key, loading: false, error: errorMessage(error) }));
    });
    return () => controller.abort();
  }, [key, enabled, revision, tick]);
  return {
    data: state.key === key ? state.data : undefined,
    error: state.key === key ? state.error : undefined,
    loading: enabled && (state.key !== key || state.loading),
    reload,
  };
}
export function LoadState({ resource }: { resource: { loading: boolean; error?: string; data?: unknown; reload: () => void } }) {
  return <>
    {resource.loading && <p className="saas-hint" role="status"><Icon name="refresh" /> 最新の情報を読み込んでいます…</p>}
    {resource.error && <div className="saas-alert" role="alert">
      <p><Icon name="warning" /> {resource.error}</p>
      {resource.data !== undefined && <p>表示中の情報は古い可能性があります。更新操作はできません。</p>}
      <button onClick={resource.reload}><Icon name="refresh" />再取得</button>
    </div>}
  </>;
}
export function Empty({ children }: { children: ReactNode }) {
  return <p className="saas-empty"><Icon name="search" /><span>{children}</span></p>;
}
export function focusSelection(container: HTMLElement | null) {
  requestAnimationFrame(() => container?.querySelector<HTMLButtonElement>('button[aria-pressed="true"]')?.focus());
}
export function Fact({ label, children, icon }: { label: string; children: ReactNode; icon?: IconName }) {
  return <div><dt>{icon && <Icon name={icon} />}{label}</dt><dd>{children}</dd></div>;
}
export type StatusTone = "neutral" | "info" | "positive" | "warning" | "danger" | "recommendation";
export function StatusBadge({ tone, icon, children }: { tone: StatusTone; icon: IconName; children: ReactNode }) {
  return <span className={`saas-tag saas-status saas-status-${tone}`}><Icon name={icon} /><span>{children}</span></span>;
}
export function Pagination({ next, previous, onNext, onPrevious, disabled, page }: {
  next: string | null | undefined; previous: boolean; onNext: () => void; onPrevious: () => void; disabled: boolean; page: number;
}) {
  return <nav className="saas-pagination" aria-label="一覧のページ">
    <button disabled={!previous || disabled} onClick={onPrevious}><Icon name="left" />前へ</button>
    <span>{page}ページ</span>
    <button disabled={!next || disabled} onClick={onNext}>次へ <Icon name="right" /></button>
  </nav>;
}
export function usePaging() {
  const [cursors, setCursors] = useState<string[]>([""]);
  return { cursor: cursors.at(-1) ?? "", page: cursors.length, previous: cursors.length > 1,
    next: (cursor: string | null | undefined) => { if (cursor) setCursors((old) => [...old, cursor]); },
    back: () => setCursors((old) => old.length > 1 ? old.slice(0, -1) : old),
    reset: () => setCursors([""]) };
}
const formatNumber = new Intl.NumberFormat("ja-JP");
export const number = (value: number) => formatNumber.format(value);
export function minute(value: number) {
  const day = Math.floor(value / 1440);
  const clock = `${String(Math.floor(value % 1440 / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
  return `${day ? `翌${day > 1 ? `${day}日` : "日"} ` : ""}${clock}`;
}
export function scheduleLabel(value: Schedule) {
  return `${value.weekdays.map((day) => ["月", "火", "水", "木", "金", "土", "日"][day]).join("・")} ${minute(value.start_minute)}–${minute(value.end_minute)}（休憩${value.break_minutes}分）`;
}
export function dateTime(value: string) {
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(new Date(value));
}
export function tokyoInputToISO(value: string): string {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) throw new Error("日時を入力してください。");
  const parsed = new Date(`${value}:00+09:00`);
  if (!Number.isFinite(parsed.getTime()) || toTokyoInput(parsed.toISOString()) !== value) throw new Error("有効な日時を入力してください。");
  return parsed.toISOString();
}
export function toTokyoInput(value: string) {
  return new Date(new Date(value).getTime() + 9 * 3_600_000).toISOString().slice(0, 16);
}
export const introLabel = {
  shortlisted: "紹介候補", requested: "模擬紹介依頼済み", consented: "模擬本人同意済み", declined: "辞退",
  interview_proposed: "面接日時を提案中", interview_accepted: "面接日時に合意", placed: "成約（デモ）",
  cancelled: "取消済み", expired: "期限切れ",
};
export const appointmentLabel = { proposed: "提案中", accepted: "合意済み", cancelled: "取消済み" };
const introAppearance: Record<keyof typeof introLabel, { tone: StatusTone; icon: IconName }> = {
  shortlisted: { tone: "info", icon: "person" }, requested: { tone: "warning", icon: "mail" },
  consented: { tone: "positive", icon: "shield" }, declined: { tone: "neutral", icon: "close" },
  interview_proposed: { tone: "warning", icon: "calendar" }, interview_accepted: { tone: "positive", icon: "calendar" },
  placed: { tone: "positive", icon: "checkCircle" }, cancelled: { tone: "neutral", icon: "close" },
  expired: { tone: "warning", icon: "clock" },
};
export function IntroductionStatus({ status, version }: { status: keyof typeof introLabel; version?: number }) {
  return <StatusBadge {...introAppearance[status]}>{introLabel[status]}{version !== undefined ? ` · v${version}` : ""}</StatusBadge>;
}
export function AppointmentStatus({ status, version }: { status: keyof typeof appointmentLabel; version?: number }) {
  return <StatusBadge tone={status === "accepted" ? "positive" : status === "proposed" ? "warning" : "neutral"}
    icon={status === "accepted" ? "checkCircle" : status === "proposed" ? "clock" : "close"}>
    {appointmentLabel[status]}{version !== undefined ? ` · v${version}` : ""}</StatusBadge>;
}
export function DispatchNotice() {
  return <p className="saas-notice"><Icon name="shield" /> 通常派遣の求人です。派遣先による候補者選考・事前面接につながる紹介／面接操作は表示しません。適格性の確認と配置検討を分けて扱います。</p>;
}
