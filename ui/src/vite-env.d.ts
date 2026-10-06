/// <reference types="vite/client" />
interface ImportMetaEnv {
  readonly VITE_CONTROLLER_URL?: string;
  readonly VITE_CONTROLLER_TOKEN?: string;
  readonly VITE_MOCK?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
