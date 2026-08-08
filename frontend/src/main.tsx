import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { installAuthFetch } from "./api/authFetch";
import "@xyflow/react/dist/style.css";
import "./styles.css";
// The `pn__*` rules started life inside giantflow and were reached only because
// `flow/FlowApp.tsx` imported them and Vite bundles every stylesheet together.
// The shell's own StudioNav uses them now, so the app states the dependency
// rather than relying on a giantflow module happening to be in the graph.
import "./flowstudio.css";

// Attach the Bearer token to /api requests + handle 401 globally (Phase 9).
installAuthFetch();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
