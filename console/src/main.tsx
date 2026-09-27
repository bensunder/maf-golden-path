import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { MODE } from "./lib/api";
import { FleetApp } from "./pages/Fleet";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {MODE === "fleet" ? <FleetApp /> : <App />}
  </StrictMode>,
);
