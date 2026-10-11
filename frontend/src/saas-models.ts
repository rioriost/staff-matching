import { z } from "zod";

const text = z.string();
const id = text.min(1);
const count = z.number().int().nonnegative();
const version = z.number().int().positive();
const date = z.iso.date();
const timestamp = z.iso.datetime({ offset: true });
const score = z.number().min(0).max(100);
const skills = z.record(text, z.number().int().min(1).max(5));
const strings = z.array(text);
const point = z.strictObject({ lat: z.number().min(-90).max(90), lon: z.number().min(-180).max(180) });
const area = z.strictObject({ id, label: text, point });
const station = z.strictObject({ station_id: id, name: text, walking_minutes: count });
const schedule = z.strictObject({
  weekdays: z.array(z.number().int().min(0).max(6)).min(1).max(7),
  start_minute: z.number().int().min(0).max(1439),
  end_minute: z.number().int().min(1).max(2880),
  break_minutes: count,
}).refine((s) => new Set(s.weekdays).size === s.weekdays.length
  && s.end_minute > s.start_minute && s.end_minute - s.start_minute <= 1440
  && s.break_minutes < s.end_minute - s.start_minute);

const company = z.strictObject({
  id, name: text, industry: text, description: text, contact_email: text,
});
const site = z.strictObject({
  id, company_id: id, name: text, kind: z.enum(["headquarters", "branch", "worksite"]),
  address: text, area, point, stations: z.array(station), parking: z.boolean(), accessibility: text,
});
const job = z.strictObject({
  id, company_id: id, company_name: text, site_id: id, site, title: text, occupation: text,
  duties: text, business_type: z.enum(["placement", "dispatch"]), employment_type: text,
  headcount: z.number().int().min(1).max(20), start_date: date, end_date: date, schedule,
  hourly_pay_min: version, hourly_pay_max: version, required_skills: skills,
  required_qualifications: strings, preferred_qualifications: strings, experience_months: count,
  renewal: text, duties_change_scope: text, location_change_scope: text, overtime: text, holidays: text,
  allowances: text, benefits: strings, application: text, status: z.enum(["open", "closed"]),
  version, synthetic: z.literal(true),
}).refine((j) => j.end_date > j.start_date && j.hourly_pay_max >= j.hourly_pay_min
  && j.site.id === j.site_id && j.site.company_id === j.company_id);
const credential = z.strictObject({ name: text, verified: z.boolean(), valid_until: date.nullable() });
const evaluation = z.strictObject({
  recorded_at: date, observation: text, dimension: z.enum(["attendance", "quality", "collaboration", "improvement"]),
  score: z.number().int().min(1).max(5).nullable(), source: text,
});
const employment = z.strictObject({
  employer: text, occupation: text, start_date: date, end_date: date,
  duties: text, evaluations: z.array(evaluation),
});
const personFields = {
  id, name: text, occupation: text, home_area: area, stations: z.array(station),
  skills, qualifications: z.array(credential), available_from: date, available_until: date,
  availability: schedule, desired_hourly_pay: version, preferred_occupations: strings,
  commute_modes: z.array(z.enum(["transit", "car"])), driving_eligible: z.boolean(),
  max_commute_minutes: z.number().int().min(1).max(240),
  matching_enabled: z.boolean(), version, synthetic: z.literal(true),
};
const personSummary = z.strictObject(personFields);
const person = z.strictObject({ ...personFields, employment_history: z.array(employment) });
const commute = z.strictObject({
  status: z.enum(["estimated", "unknown", "unreachable"]), mode: z.enum(["transit", "car"]).nullable(),
  duration_seconds: count.nullable(), eligible: z.boolean(), summary: text, station_names: strings,
  public_path: z.array(point), network_version: text, assumptions_version: text, uncertainty: text,
});
const scores = z.strictObject({
  skills: score, qualifications: score, preferences: score, commute: score, history: score.nullable(),
});
const candidate = z.strictObject({
  job_id: id, person_id: id, person_name: text, home_area: area, eligible: z.boolean(),
  score: z.number().finite().nullable(), scores: scores.nullable(), reasons: strings, evidence: strings, commute,
  recommended: z.boolean(), pinned: z.boolean(), job_version: version, person_version: version,
});
const coverage = z.strictObject({
  jobs: count, people: count, retrieved_pairs: count, evaluated_pairs: count, eligible_pairs: count,
  route_unknown: count, truncated: z.boolean(), notes: strings,
});
const matchRun = z.strictObject({
  id, status: z.enum(["queued", "running", "completed", "failed", "cancelled"]),
  created_at: timestamp, progress: z.number().int().min(0).max(100), attempt: count,
  error: text.nullable(), coverage, recommended_count: count, solver_status: text.nullable(),
  objective_bound: z.number().finite().nullable(), model_version: text,
});
const introduction = z.strictObject({
  id, job_id: id, person_id: id, company_id: id, candidate_alias: text,
  status: z.enum(["shortlisted", "requested", "consented", "declined", "interview_proposed",
    "interview_accepted", "placed", "cancelled", "expired"]),
  version, created_at: timestamp, expires_at: timestamp, simulated: z.boolean(),
});
const appointment = z.strictObject({
  id, introduction_id: id, job_id: id, person_id: id, company_id: id,
  start_at: timestamp, end_at: timestamp, site_id: id.nullable(), online: z.boolean(),
  status: z.enum(["proposed", "accepted", "cancelled"]), version,
}).refine((a) => Date.parse(a.end_at) > Date.parse(a.start_at) && a.online === (a.site_id === null));
const notification = z.strictObject({
  id, audience: z.enum(["management", "company", "person"]), recipient_id: id,
  kind: text, title: text, message: text, introduction_id: id.nullable(), created_at: timestamp,
  read: z.boolean(), simulated: z.literal(true),
});
const overview = z.strictObject({
  tenant_id: id, dataset_version: text, jobs: count, people: count, companies: count,
  introductions: count, appointments: count, unread_notifications: count,
  can_simulate: z.boolean(), network_version: text,
});

const longitude = z.number().min(-180).max(180);
const latitude = z.number().min(-90).max(90);
const position = z.tuple([longitude, latitude]);
const boundingBox = z.tuple([longitude, latitude, longitude, latitude])
  .refine(([, south, , north]) => south <= north);
const geometry = z.discriminatedUnion("type", [
  z.strictObject({ type: z.literal("Point"), coordinates: position }),
  z.strictObject({ type: z.literal("MultiPoint"), coordinates: z.array(position) }),
  z.strictObject({ type: z.literal("LineString"), coordinates: z.array(position) }),
  z.strictObject({ type: z.literal("MultiLineString"), coordinates: z.array(z.array(position)) }),
  z.strictObject({ type: z.literal("Polygon"), coordinates: z.array(z.array(position)) }),
  z.strictObject({ type: z.literal("MultiPolygon"), coordinates: z.array(z.array(z.array(position))) }),
]);
const basemap = z.strictObject({
  type: z.literal("FeatureCollection"),
  bbox: boundingBox,
  network_version: text, attribution: text, license: text, license_url: z.url(), approximate: z.literal(true),
  features: z.array(z.strictObject({
    type: z.literal("Feature"), geometry,
    properties: z.record(text, z.unknown()).nullable(),
    id: z.union([text, z.number()]).optional(),
  })),
});

export const schemas = { point, area, schedule, company, site, job, personSummary, person, candidate,
  commute, scores, matchRun, introduction, appointment, notification, overview, basemap };
export function pageSchema<T extends z.ZodType>(item: T) {
  return z.strictObject({ items: z.array(item), next_cursor: id.nullable(), total: count });
}
export function decode<T>(schema: z.ZodType<T>, value: unknown): T {
  const result = schema.safeParse(value);
  if (!result.success) {
    throw new Error("サーバーから想定外の形式の応答を受信しました。データは表示・更新していません。状態を再取得してください。");
  }
  return result.data;
}

export type Point = z.infer<typeof point>;
export type Area = z.infer<typeof area>;
export type Schedule = z.infer<typeof schedule>;
export type Company = z.infer<typeof company>;
export type Site = z.infer<typeof site>;
export type Job = z.infer<typeof job>;
export type PersonSummary = z.infer<typeof personSummary>;
export type Person = z.infer<typeof person>;
export type Commute = z.infer<typeof commute>;
export type Scores = z.infer<typeof scores>;
export type Candidate = z.infer<typeof candidate>;
export type MatchRun = z.infer<typeof matchRun>;
export type Introduction = z.infer<typeof introduction>;
export type Appointment = z.infer<typeof appointment>;
export type Notification = z.infer<typeof notification>;
export type Overview = z.infer<typeof overview>;
export type Basemap = z.infer<typeof basemap>;
export interface Page<T> { items: T[]; next_cursor: string | null; total: number }
export interface Versioned { expected_version: number; idempotency_key: string }
export interface MatchRequest { idempotency_key: string; job_ids: string[]; pins: [string, string][] }
export interface ShortlistRequest {
  run_id: string; job_id: string; person_id: string;
  expected_job_version: number; expected_person_version: number; idempotency_key: string;
}
export type SimulationAction = "company_request" | "person_consent" | "person_decline" | "withdraw_consent";
export interface SimulationRequest extends Versioned { action: SimulationAction; simulated: true }
export interface AppointmentRequest extends Versioned {
  start_at: string; end_at: string; site_id: string | null; online: boolean;
}
