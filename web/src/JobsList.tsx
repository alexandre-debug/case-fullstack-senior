import { useQuery } from "@tanstack/react-query";
import { get } from "./api";

export function JobsList({ auth }: { auth: string }) {
  const { data } = useQuery({ queryKey: ["jobs", auth], queryFn: () => get("/jobs", auth), refetchInterval: 1000 });
  return <ul>{data?.map((j: any) => <li key={j.id}>{j.kind} — {j.status} ({j.result_count})</li>)}</ul>;
}
