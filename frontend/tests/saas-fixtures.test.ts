import type { Appointment, Candidate, Company, Introduction, Job, MatchRun, Overview, Person, PersonSummary, Site } from "../src/saas-models.ts";

// Browser fixtures are synthetic HTTP responses, never a production data fallback.
export const site: Site = {
  id: "site-1", company_id: "company-1", name: "青葉しごと研究所 日本橋事業所",
  kind: "worksite", address: "東京都中央区日本橋（架空の就業先）",
  area: { id: "nihonbashi", label: "日本橋周辺", point: { lat: 35.684, lon: 139.773 } },
  point: { lat: 35.685, lon: 139.774 }, stations: [
    { station_id: "st-1", name: "日本橋", walking_minutes: 5 },
    { station_id: "st-2", name: "三越前", walking_minutes: 8 },
  ], parking: false, accessibility: "段差のない入口あり（架空）",
};
export const company: Company = {
  id: "company-1", name: "青葉しごと研究所株式会社（架空）", industry: "業務支援",
  description: "地域の事業者の事務を支援する架空の会社です。", contact_email: "staffing@example.invalid",
};
export const job: Job = {
  id: "job-1", company_id: company.id, company_name: company.name, site_id: site.id, site,
  title: "受注内容の確認と入力を担当する一般事務スタッフ", occupation: "一般事務",
  duties: "注文票の内容を確認し、入力後に照合します。確認が必要な項目は担当者に共有します。",
  business_type: "placement", employment_type: "契約社員", headcount: 2,
  start_date: "2030-11-01", end_date: "2031-03-01",
  schedule: { weekdays: [0, 1, 2, 3, 4], start_minute: 540, end_minute: 1080, break_minutes: 60 },
  hourly_pay_min: 1600, hourly_pay_max: 1900, required_skills: { 入力照合: 3 },
  required_qualifications: [], preferred_qualifications: ["日商簿記3級"], experience_months: 6,
  renewal: "業務量と双方の希望を踏まえて更新。上限2回。", duties_change_scope: "変更なし",
  location_change_scope: "日本橋事業所内", overtime: "月5時間程度", holidays: "土日・祝日",
  allowances: "交通費支給（上限あり）", benefits: ["社会保険", "休憩室"], application: "管理会社を通じて応募",
  status: "open", version: 1, synthetic: true,
};
export const person: Person = {
  id: "person-1", name: "青山 ひかり（架空）", occupation: "一般事務",
  home_area: { id: "kameido", label: "亀戸周辺", point: { lat: 35.697, lon: 139.827 } },
  stations: [{ station_id: "st-3", name: "亀戸", walking_minutes: 10 }],
  skills: { 入力照合: 4, 表計算: 3 }, qualifications: [{ name: "日商簿記3級", verified: true, valid_until: null }],
  available_from: "2030-10-01", available_until: "2031-04-01",
  availability: { weekdays: [0, 1, 2, 3, 4], start_minute: 480, end_minute: 1140, break_minutes: 60 },
  desired_hourly_pay: 1550, preferred_occupations: ["一般事務"], commute_modes: ["transit"],
  driving_eligible: false, max_commute_minutes: 60, matching_enabled: true, version: 2, synthetic: true,
  employment_history: [{
    employer: "若葉帳票サービス（架空）", occupation: "一般事務", start_date: "2024-04-01", end_date: "2026-04-01",
    duties: "帳票の確認と入力を担当。", evaluations: [{
      recorded_at: "2026-03-10", dimension: "quality", score: 4,
      observation: "入力後の照合を続け、差異がある場合は根拠を添えて相談した。", source: "架空の担当者による記録",
    }],
  }],
};
const { employment_history: _history, ...summary } = person;
export const personSummary: PersonSummary = summary;
export const secondPerson: Person = { ...person, id: "person-2", name: "水野 つむぎ（架空）", employment_history: [] };
export const candidate: Candidate = {
  job_id: job.id, person_id: person.id, person_name: person.name, home_area: person.home_area,
  eligible: true, score: 86, scores: { skills: 90, qualifications: 100, preferences: 80, commute: 75, history: 85 },
  reasons: [], evidence: ["入力照合4/5で必須条件を満たします。", "記録された品質評価を参照しました。"],
  commute: { status: "estimated", mode: "transit", duration_seconds: 2100, eligible: true,
    summary: "亀戸から日本橋までのネットワーク概算", station_names: ["亀戸", "日本橋"],
    public_path: [{ lat: 35.697, lon: 139.826 }, { lat: 35.685, lon: 139.774 }],
    network_version: "public-test-v1", assumptions_version: "test-v1", uncertainty: "概算。時刻表・運行状況・渋滞は反映していません。" },
  recommended: true, pinned: false, job_version: job.version, person_version: person.version,
};
export const manualCandidate: Candidate = {
  ...candidate, person_id: secondPerson.id, person_name: secondPerson.name, score: 79, recommended: false,
  scores: { skills: 85, qualifications: 100, preferences: 80, commute: 70, history: null },
  evidence: ["職歴は未確認。0点として扱いません。"],
};
export const run: MatchRun = {
  id: "run-1", status: "completed", created_at: "2026-10-06T03:00:00Z", progress: 100, attempt: 1, error: null,
  coverage: { jobs: 1, people: 1000, retrieved_pairs: 30, evaluated_pairs: 30, eligible_pairs: 20,
    route_unknown: 3, truncated: true, notes: ["検索上位30組を評価しました。"] },
  recommended_count: 2, solver_status: "FEASIBLE", objective_bound: 200, model_version: "test-v1",
};
export const introduction: Introduction = {
  id: "intro-1", job_id: job.id, person_id: secondPerson.id, company_id: company.id,
  candidate_alias: "候補者A-001", status: "shortlisted", version: 1,
  created_at: "2026-10-06T03:10:00Z", expires_at: "2030-11-01T00:00:00Z", simulated: true,
};
export const appointment: Appointment = {
  id: "appointment-1", introduction_id: introduction.id, job_id: job.id, person_id: secondPerson.id,
  company_id: company.id, start_at: "2030-10-10T01:00:00Z", end_at: "2030-10-10T02:00:00Z",
  site_id: null, online: true, status: "proposed", version: 1,
};
export const overview: Overview = {
  tenant_id: "demo", dataset_version: "synthetic-test-v1", jobs: 1000, people: 1000, companies: 12,
  introductions: 0, appointments: 0, unread_notifications: 0, can_simulate: true, network_version: "public-test-v1",
};
export const account = {
  enabled: true, authenticated: true,
  user: { id: "user-a", username: "coordinator", display_name: "デモ担当者" },
  tenants: [{ id: "demo", name: "青空人材管理会社（架空）", role: "coordinator" }],
  tenant: { id: "demo", name: "青空人材管理会社（架空）", role: "coordinator" },
  auth_method: "password", passkey_count: 1,
};
export const pageOf = <T>(items: T[], total = items.length, next_cursor: string | null = null) => ({ items, total, next_cursor });
