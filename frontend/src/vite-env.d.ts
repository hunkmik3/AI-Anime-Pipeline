/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Set to "1" in frontend/.env.local to bypass the forced AI-provider setup gate. */
  readonly VITE_DISABLE_SETUP_GATE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
