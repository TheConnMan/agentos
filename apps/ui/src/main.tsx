import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "./styles.css";
import { App } from "./App";
import { ConsoleGate } from "./components/ConsoleGate";
import { StoreProvider } from "./state/store";
import { WiredProvider } from "./state/wired";

// retry off so error / notFound / noBundle states surface on the first response
// (deterministic, matching the pre-react-query hooks); no refetch-on-focus.
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      {/*
        The console session gate (#630) sits above the shell so a locked console
        never mounts the providers that fetch: while it is closed, WiredProvider
        does not exist and no authenticated call is made.
      */}
      <ConsoleGate>
        <StoreProvider>
          <WiredProvider>
            <App />
          </WiredProvider>
        </StoreProvider>
      </ConsoleGate>
    </QueryClientProvider>
  </StrictMode>,
);
