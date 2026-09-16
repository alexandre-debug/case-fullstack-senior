import { useInfiniteQuery } from "@tanstack/react-query";
import { get } from "./api";

export type Job = {
  id: number;
  company_id?: number;
  kind: string;
  status: string;
  created_at: string;
  attempts: number;
  max_attempts: number;
  last_error: string | null;
  result_count: number;
};

export type JobsPage = { items: Job[]; next_cursor: string | null };

export const ACTIVE = ["queued", "running"];

// Paginação por cursor da API (o mesmo contrato de /jobs e /admin/jobs).
// O polling só roda enquanto houver job ativo: uma lista parada não gera carga nenhuma no servidor.
export function useJobsPages(path: string, auth: string, enabled = true) {
  return useInfiniteQuery({
    queryKey: [path, auth],
    enabled,
    initialPageParam: "",
    queryFn: ({ pageParam }): Promise<JobsPage> =>
      get(pageParam ? `${path}?limit=20&cursor=${encodeURIComponent(pageParam)}` : `${path}?limit=20`, auth),
    getNextPageParam: (last: JobsPage) => last.next_cursor ?? undefined,
    refetchInterval: (query) =>
      query.state.data?.pages.some((page) => page.items.some((job) => ACTIVE.includes(job.status))) ? 2000 : false,
  });
}
