import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post } from "./api";

const CANCELLABLE = ["queued", "running"];

export function JobsList({ auth }: { auth: string }) {
  const queryClient = useQueryClient();
  const { data, error } = useQuery({ queryKey: ["jobs", auth], queryFn: () => get("/jobs", auth), refetchInterval: 1000 });
  const cancel = useMutation({
    mutationFn: (id: number) => post(`/jobs/${id}/cancel`, auth),
    // Tanto no sucesso quanto no 409 (o job terminou antes) a lista precisa refletir o estado real.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["jobs", auth] }),
  });

  return (
    <div>
      {error && <p role="alert">Não foi possível carregar os jobs: {error.message}</p>}
      {cancel.error && <p role="alert">Não foi possível cancelar: {cancel.error.message}</p>}
      <ul>
        {data?.items?.map((j: any) => (
          <li key={j.id}>
            #{j.id} {j.kind} — {j.status} ({j.result_count}){" "}
            {CANCELLABLE.includes(j.status) && (
              <button onClick={() => cancel.mutate(j.id)} disabled={cancel.isPending && cancel.variables === j.id}>
                Cancelar
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
