import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { post } from "./api";

const KINDS = ["report", "import"];

export function SubmitForm({ auth }: { auth: string }) {
  const [kind, setKind] = useState(KINDS[0]);
  const queryClient = useQueryClient();
  const submit = useMutation({
    // Uma Idempotency-Key por clique: se a resposta se perder e o cliente repetir o envio, a API devolve
    // o mesmo job em vez de criar outro (um dos motivos de "às vezes aparece mais de um job").
    mutationFn: (key: string) => post("/jobs", auth, { kind }, { "Idempotency-Key": key }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["/jobs", auth] }),
  });

  return (
    <div style={{ marginBottom: 12 }}>
      <select value={kind} onChange={(e) => setKind(e.target.value)} disabled={submit.isPending}>
        {KINDS.map((k) => (
          <option key={k} value={k}>
            {k}
          </option>
        ))}
      </select>{" "}
      <button onClick={() => submit.mutate(crypto.randomUUID())} disabled={submit.isPending}>
        {submit.isPending ? "Enviando..." : "Enviar job"}
      </button>
      {submit.error && <p role="alert">Não foi possível enviar: {submit.error.message}</p>}
    </div>
  );
}
