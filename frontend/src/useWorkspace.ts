import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "./api.ts";
import { activeRun, orderedRuns } from "./domain.ts";
import type { Assignment, Candidate, Health, Order, RunView, Staff } from "./models.ts";

export interface Workspace {
  health: Health;
  staff: Staff[];
  orders: Order[];
  assignments: Assignment[];
  runs: RunView[];
}

export function useWorkspace() {
  const [data, setData] = useState<Workspace | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [pollingError, setPollingError] = useState<string | null>(null);
  const [selectedOrderId, setSelectedOrderId] = useState("");
  const [selectedRunId, setSelectedRunId] = useState("");
  const [candidateVersion, setCandidateVersion] = useState(0);
  const [pollVersion, setPollVersion] = useState(0);
  const requestVersion = useRef(0);
  const mounted = useRef(false);

  const refresh = useCallback(async (focusRunId?: string, signal?: AbortSignal): Promise<boolean> => {
    if (!mounted.current) return false;
    const version = ++requestVersion.current;
    setRefreshing(true);
    setRefreshError(null);
    try {
      const [health, staff, orders, assignments, recentRuns, focusedRun] = await Promise.all([
        api.health(signal), api.staff(signal), api.orders(signal),
        api.assignments(signal), api.runs(signal),
        focusRunId ? api.run(focusRunId, signal) : Promise.resolve(null),
      ]);
      if (!mounted.current || version !== requestVersion.current || signal?.aborted) return false;
      const runs = orderedRuns(focusedRun
        ? [focusedRun, ...recentRuns.filter((run) => run.id !== focusedRun.id)]
        : recentRuns);
      setData({ health, staff, orders, assignments, runs });
      setSelectedOrderId((current) => orders.some((order) => order.id === current) ? current : orders[0]?.id ?? "");
      setSelectedRunId((current) => focusRunId ?? (runs.some((run) => run.id === current) ? current : runs[0]?.id ?? ""));
      setCandidateVersion((current) => current + 1);
      setPollingError(null);
      setPollVersion((current) => current + 1);
      return true;
    } catch (error) {
      if (mounted.current && version === requestVersion.current && !signal?.aborted) {
        setRefreshError(errorMessage(error));
      }
      return false;
    } finally {
      if (mounted.current && version === requestVersion.current) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void refresh(undefined, controller.signal);
    return () => {
      mounted.current = false;
      requestVersion.current += 1;
      controller.abort();
    };
  }, [refresh]);

  const runningId = data?.runs.find(activeRun)?.id;
  useEffect(() => {
    if (!runningId) return;
    const controller = new AbortController();
    let timer: number | undefined;
    async function poll() {
      try {
        const run = await api.run(runningId ?? "", controller.signal);
        if (controller.signal.aborted) return;
        setData((current) => current ? {
          ...current,
          runs: current.runs.map((existing) => existing.id === run.id ? run : existing),
        } : null);
        if (activeRun(run)) {
          timer = window.setTimeout(() => { void poll(); }, 900);
        } else {
          void refresh();
        }
      } catch (error) {
        if (!controller.signal.aborted) setPollingError(errorMessage(error));
      }
    }
    timer = window.setTimeout(() => { void poll(); }, 700);
    return () => {
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runningId, pollVersion, refresh]);

  return {
    data, refreshing, refreshError, pollingError, refresh,
    selectedOrderId, setSelectedOrderId, selectedRunId, setSelectedRunId,
    candidateVersion,
    retryCandidates: () => setCandidateVersion((current) => current + 1),
  };
}

export function useCandidates(orderId: string, version: number) {
  const [state, setState] = useState<{
    orderId: string;
    loading: boolean;
    error: string | null;
    candidates: Candidate[];
  }>({ orderId: "", loading: false, error: null, candidates: [] });

  useEffect(() => {
    if (!orderId) return;
    const controller = new AbortController();
    setState({ orderId, loading: true, error: null, candidates: [] });
    void api.candidates(orderId, controller.signal).then((candidates) => {
      if (!controller.signal.aborted) setState({ orderId, loading: false, error: null, candidates });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setState({ orderId, loading: false, error: errorMessage(error), candidates: [] });
    });
    return () => controller.abort();
  }, [orderId, version]);

  if (state.orderId !== orderId) return { loading: !!orderId, error: null, candidates: [] };
  return state;
}

export function useClock() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}
