import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { App } from "./App";
import { StoreProvider } from "./state/store";
import { WiredProvider } from "./state/wired";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <StoreProvider>
      <WiredProvider>
        <App />
      </WiredProvider>
    </StoreProvider>
  </StrictMode>,
);
