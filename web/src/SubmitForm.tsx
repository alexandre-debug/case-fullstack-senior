import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { post } from "./api";

const KINDS = ["report", "import"];

export function SubmitForm({ auth }: { auth: string }) {
  const [kind, setKind] = useState(KINDS[0]);
  const queryClient = useQueryClient();
  // Uma chave por *intenção* de envio, não por clique: ela só é trocada depois de um envio aceito.
  // Se a resposta se perder e o usuário clicar de novo, a API reconhece a repetição e devolve o mesmo
  // job em vez de criar outro (um dos motivos de "às vezes aparece mais de um job").
  const chave = useRef(crypto.randomUUID());
  const submit = useMutation({
    mutationFn: () => post("/jobs", auth, { kind }, { "Idempotency-Key": chave.current }),
    onSuccess: () => {
      chave.current = crypto.randomUUID();
      // As duas listas mostram os mesmos jobs; a de admin tem queryKey própria e não seria alcançada.
      queryClient.invalidateQueries({ queryKey: ["/jobs", auth] });
      queryClient.invalidateQueries({ queryKey: ["/admin/jobs", auth] });
    },
  });

  return (
    <div style={{ marginBottom: 12 }}>
      <select
        value={kind}
        onChange={(e) => {
          setKind(e.target.value);
          chave.current = crypto.randomUUID(); // outro tipo de job é outra intenção
        }}
        disabled={submit.isPending}
      >
        {KINDS.map((k) => (
          <option key={k} value={k}>
            {k}
          </option>
        ))}
      </select>{" "}
      <button onClick={() => submit.mutate()} disabled={submit.isPending}>
        {submit.isPending ? "Enviando..." : "Enviar job"}
      </button>
      {submit.error && <p role="alert">Não foi possível enviar: {submit.error.message}</p>}
    </div>
  );
}
