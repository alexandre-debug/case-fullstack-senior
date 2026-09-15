import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post } from "./api";

const CANCELLABLE = ["queued", "running"];

export function JobsList({ auth }: { auth: string }) {
  const queryClient = useQueryClient();
  const { data, error } = useQuery({ queryKey: ["jobs", auth], queryFn: () => get("/jobs", auth), refetchInterval: 1000 });
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["jobs", auth] });
  // Tanto no sucesso quanto no 409 (o job mudou de estado antes do clique) a lista precisa refletir o estado real.
  const cancel = useMutation({ mutationFn: (id: number) => post(`/jobs/${id}/cancel`, auth), onSettled: invalidate });
  const retry = useMutation({ mutationFn: (id: number) => post(`/jobs/${id}/retry`, auth), onSettled: invalidate });

  return (
    <div>
      {error && <p role="alert">Não foi possível carregar os jobs: {error.message}</p>}
      {cancel.error && <p role="alert">Não foi possível cancelar: {cancel.error.message}</p>}
      {retry.error && <p role="alert">Não foi possível reprocessar: {retry.error.message}</p>}
      <ul>
        {data?.items?.map((j: any) => (
          <li key={j.id}>
            #{j.id} {j.kind} — {j.status} ({j.result_count}){" "}
            {j.attempts > 0 && (
              <small>
                tentativa {j.attempts} de {j.max_attempts}{" "}
              </small>
            )}
            {CANCELLABLE.includes(j.status) && (
              <button onClick={() => cancel.mutate(j.id)} disabled={cancel.isPending && cancel.variables === j.id}>
                Cancelar
              </button>
            )}
            {j.status === "failed" && j.attempts < j.max_attempts && (
              <button onClick={() => retry.mutate(j.id)} disabled={retry.isPending && retry.variables === j.id}>
                Reprocessar
              </button>
            )}
            {j.last_error && <div style={{ fontSize: 12, color: "#a00" }}>{j.last_error}</div>}
          </li>
        ))}
      </ul>
    </div>
  );
}
