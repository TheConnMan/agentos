import type { VersionRow } from "./types";

export const VERSION_ROWS: VersionRow[] = [
  { branch: "main", ver: "v1.4.2", dep: "2 days ago", score: "97%", by: "mara", human: true, status: "Production", state: "production" },
  { branch: "dev", ver: "4f2c91a", dep: "3h ago", score: "94%", by: "jt", human: true, status: "Preview", state: "preview" },
  { branch: "dev", ver: "b7e02d1", dep: "1h ago", score: "86%", by: "agentos-ci", human: false, status: "Eval check failed", state: "failed" },
];
