import type { Candidate, Proposal, ProposalStatus, RunStatus, RunView } from "./models.ts";

export const proposalLabels: Record<ProposalStatus, string> = {
  proposed: "提案",
  consented: "本人承諾（模擬）",
  declined: "辞退",
  expired: "期限切れ",
  confirmed: "担当者確定",
};

export const runLabels: Record<RunStatus, string> = {
  queued: "実行待ち",
  running: "計算中",
  completed: "計算完了",
  failed: "計算失敗",
};

export const eligibilityLabels: Record<Candidate["eligibility"], string> = {
  eligible: "条件適合",
  ineligible: "条件不適合",
  pending: "情報不足・要確認",
};

export function activeRun(run: RunView): boolean {
  return run.status === "queued" || run.status === "running";
}

export function effectiveProposalStatus(proposal: Proposal, now: number): ProposalStatus {
  if ((proposal.status === "proposed" || proposal.status === "consented") && Date.parse(proposal.expires_at) <= now) {
    return "expired";
  }
  return proposal.status;
}

export function unresolvedProposals(runs: RunView[], now: number): Proposal[] {
  return runs.flatMap((run) => run.proposals).filter((proposal) => {
    const status = effectiveProposalStatus(proposal, now);
    return status === "proposed" || status === "consented";
  });
}

export function optimizationBlock(runs: RunView[], now: number): string | null {
  if (runs.some(activeRun)) return "実行待ち・計算中の処理があります。完了を待ってください。";
  const unresolved = unresolvedProposals(runs, now);
  if (unresolved.length > 0) {
    return `未完了の提案が${unresolved.length}件あります。全件が担当者確定・辞退・期限切れになるまで再計算できません。`;
  }
  return null;
}

export function orderedRuns(runs: RunView[]): RunView[] {
  return [...runs].sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at));
}

export function score(value: number | null): string {
  return value === null ? "—" : value.toLocaleString("ja-JP", { maximumFractionDigits: 3, minimumFractionDigits: 3 });
}

export function dateTime(value: string): string {
  return new Date(value).toLocaleString("ja-JP", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    timeZoneName: "short",
  });
}
