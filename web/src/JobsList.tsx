import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { get, post } from "./api";
import { ACTIVE, Job, useJobsPages } from "./useJobsPages";

export function JobsList({ auth }: { auth: string }) {
  const queryClient = useQueryClient();
  const jobs = useJobsPages("/jobs", auth);
  const [result, setResult] = useState<{ id: number; payload: string } | null>(null);
  // Erro por linha: some quando a mesma ação é tentada de novo, e não fica preso na tela descrevendo
  // um job que já mudou de estado.
  const [erros, setErros] = useState<Record<number, string>>({});
  const [emVoo, setEmVoo] = useState<number[]>([]);

  // Handlers compartilhados: marcam a linha como ocupada, guardam o erro nela e, no fim, recarregam a lista
  // (o 409 de um job que mudou de estado também precisa refletir na tela).
  const acao = {
    onMutate: (id: number) => {
      setEmVoo((atual) => [...atual, id]);
      setErros(({ [id]: _removido, ...resto }) => resto);
    },
    onError: (error: Error, id: number) => setErros((atual) => ({ ...atual, [id]: error.message })),
    onSettled: (_data: unknown, _error: unknown, id: number) => {
      setEmVoo((atual) => atual.filter((emAndamento) => emAndamento !== id));
      // As duas listas mostram os mesmos jobs; a de admin tem queryKey própria e não seria alcançada.
      queryClient.invalidateQueries({ queryKey: ["/jobs", auth] });
      queryClient.invalidateQueries({ queryKey: ["/admin/jobs", auth] });
    },
  };

  const cancel = useMutation({ mutationFn: (id: number) => post(`/jobs/${id}/cancel`, auth), ...acao });
  const retry = useMutation({ mutationFn: (id: number) => post(`/jobs/${id}/retry`, auth), ...acao });
  const verResultado = useMutation({
    mutationFn: async (id: number) => ({ id, payload: (await get(`/jobs/${id}/result`, auth)).payload as string }),
    ...acao,
    onSuccess: setResult,
  });

  const items: Job[] = jobs.data?.pages.flatMap((page) => page.items) ?? [];

  if (jobs.isPending) return <p>Carregando jobs...</p>;

  return (
    <div>
      {jobs.error && <p role="alert">Não foi possível carregar os jobs: {jobs.error.message}</p>}
      {items.length === 0 && <p>Nenhum job ainda. Envie o primeiro acima.</p>}
      <ul style={{ paddingLeft: 16 }}>
        {items.map((job) => (
          <li key={job.id} style={{ marginBottom: 6 }}>
            #{job.id} {job.kind} — <strong>{job.status}</strong>{" "}
            <small>
              {new Date(job.created_at).toLocaleString()}
              {job.attempts > 0 && ` · tentativa ${job.attempts} de ${job.max_attempts}`}
            </small>{" "}
            {ACTIVE.includes(job.status) && (
              <button onClick={() => cancel.mutate(job.id)} disabled={emVoo.includes(job.id)}>
                Cancelar
              </button>
            )}
            {job.status === "failed" && job.attempts < job.max_attempts && (
              <button onClick={() => retry.mutate(job.id)} disabled={emVoo.includes(job.id)}>
                Reprocessar
              </button>
            )}
            {job.result_count > 0 && (
              <button onClick={() => verResultado.mutate(job.id)} disabled={emVoo.includes(job.id)}>
                Ver resultado
              </button>
            )}
            {job.last_error && <div style={{ fontSize: 12, color: "#a00" }}>{job.last_error}</div>}
            {erros[job.id] && (
              <div role="alert" style={{ fontSize: 12, color: "#a00" }}>
                {erros[job.id]}
              </div>
            )}
          </li>
        ))}
      </ul>
      {jobs.hasNextPage && (
        <button onClick={() => jobs.fetchNextPage()} disabled={jobs.isFetchingNextPage}>
          {jobs.isFetchingNextPage ? "Carregando..." : "Carregar mais"}
        </button>
      )}
      {result && (
        <div style={{ marginTop: 12, padding: 8, border: "1px solid #ccc" }}>
          <strong>Resultado do job #{result.id}</strong>
          <pre style={{ whiteSpace: "pre-wrap", margin: "8px 0" }}>{result.payload}</pre>
          <button onClick={() => setResult(null)}>Fechar</button>
        </div>
      )}
    </div>
  );
}
