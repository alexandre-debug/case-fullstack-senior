import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "./api";
import { AuthSwitcher, FAKE_USERS, roleOf } from "./auth";
import { JobsList } from "./JobsList";
import { SubmitForm } from "./SubmitForm";
import { useJobsPages } from "./useJobsPages";

const queryClient = new QueryClient({
  defaultOptions: {
    // 4xx é resposta definitiva (401, 403, 404, 422): repetir só atrasaria o erro na tela em vários segundos.
    // 5xx e falha de rede continuam com as 3 tentativas padrão.
    queries: { retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 3 },
  },
});

// O admin é da empresa (não existe papel de plataforma), então esta lista mostra os jobs da própria empresa.
function AdminJobs({ auth }: { auth: string }) {
  const [show, setShow] = useState(false);
  const jobs = useJobsPages("/admin/jobs", auth, show);
  const items = jobs.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <div style={{ marginTop: 24 }}>
      <button onClick={() => setShow((visible) => !visible)}>
        {show ? "Ocultar" : "Ver todos os jobs da empresa (admin)"}
      </button>
      {show && jobs.error && <p role="alert">{jobs.error.message}</p>}
      {show && jobs.isPending && <p>Carregando...</p>}
      {show && (
        <ul style={{ paddingLeft: 16 }}>
          {items.map((job) => (
            <li key={job.id}>
              #{job.id} {job.kind} — {job.status}
            </li>
          ))}
        </ul>
      )}
      {show && jobs.hasNextPage && (
        <button onClick={() => jobs.fetchNextPage()} disabled={jobs.isFetchingNextPage}>
          {jobs.isFetchingNextPage ? "Carregando..." : "Carregar mais"}
        </button>
      )}
    </div>
  );
}

export default function App() {
  const [auth, setAuth] = useState<string>(FAKE_USERS[0]);

  return (
    <QueryClientProvider client={queryClient}>
      <div style={{ maxWidth: 640, margin: "40px auto", fontFamily: "sans-serif" }}>
        <h1>Relay</h1>
        <AuthSwitcher auth={auth} onChange={setAuth} />

        <h2>Jobs</h2>
        <SubmitForm auth={auth} />
        {/* key por usuário: trocar de usuário no dropdown zera erros e estado da lista */}
        <JobsList key={auth} auth={auth} />

        {roleOf(auth) === "admin" && <AdminJobs key={`admin-${auth}`} auth={auth} />}
      </div>
    </QueryClientProvider>
  );
}
