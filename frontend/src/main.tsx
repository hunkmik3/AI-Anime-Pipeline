import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { installAuthFetch } from "./api/authFetch";
import "@xyflow/react/dist/style.css";
import "./styles.css";

// Attach the Bearer token to /api requests + handle 401 globally (Phase 9).
installAuthFetch();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
