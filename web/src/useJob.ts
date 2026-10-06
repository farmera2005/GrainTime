import { useEffect, useState } from "react";
import { api, Job } from "./api";

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

export const isDone = (job: Job | null) => !!job && TERMINAL.has(job.status);

/** Polls a collector job once a second until it finishes. */
export function useJob(jobId: number | null): Job | null {
  const [job, setJob] = useState<Job | null>(null);
  useEffect(() => {
    setJob(null);
    if (jobId == null) return;
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const j = await api.getJob(jobId);
        if (stopped) return;
        setJob(j);
        if (TERMINAL.has(j.status)) return;
      } catch {
        // network blip: keep polling
      }
      if (!stopped) timer = window.setTimeout(poll, 1000);
    };
    poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [jobId]);
  return job;
}
