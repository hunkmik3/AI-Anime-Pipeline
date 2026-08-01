/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Set to "1" in frontend/.env.local to bypass the forced AI-provider setup gate. */
  readonly VITE_DISABLE_SETUP_GATE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/** Injected by vite.config.ts from package.json. */
declare const __APP_VERSION__: string;
