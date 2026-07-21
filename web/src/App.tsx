import { useState } from "react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { AuthSwitcher, FAKE_USERS, roleOf } from "./auth";
import { JobsList } from "./JobsList";
import { SubmitForm } from "./SubmitForm";
import { get } from "./api";

const queryClient = new QueryClient();

function AdminJobs({ auth }: { auth: string }) {
  const [show, setShow] = useState(false);
  const { data } = useQuery({
    queryKey: ["admin-jobs", auth],
    queryFn: () => get("/admin/jobs", auth),
    enabled: show,
  });
  return (
    <div style={{ marginTop: 24 }}>
      <button onClick={() => setShow(true)}>Ver todos (admin)</button>
      {show && (
        <ul>
          {data?.map((j: any) => (
            <li key={j.id}>
              company {j.company_id} — {j.kind} — {j.status}
            </li>
          ))}
        </ul>
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
        <JobsList auth={auth} />

        {roleOf(auth) === "admin" && <AdminJobs auth={auth} />}
      </div>
    </QueryClientProvider>
  );
}
