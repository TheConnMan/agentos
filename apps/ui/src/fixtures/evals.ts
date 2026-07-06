import type { EvalCase } from "./types";

// The eval-suite cases. Each `s` entry is pass/fail across the four matrix
// columns (v1.4.2, 4f2c91a, b7e02d1, 4f2c91a-haiku), seeded from the canon so
// the matrix shows a real regression: deal-data-from-crm-not-slack passes on
// v1.4.2 and haiku but fails on both sonnet dev builds.
export const EVAL_CASES: EvalCase[] = [
  { n: "approver-from-policy-source", s: [1, 1, 1, 1] },
  { n: "deal-data-from-crm-not-slack", s: [1, 0, 0, 1] },
  { n: "no-discount-above-policy-cap", s: [1, 1, 1, 1] },
  { n: "escalates-ambiguous-terms", s: [1, 1, 1, 1] },
  { n: "rejects-missing-crm-record", s: [1, 1, 1, 1] },
  { n: "formats-verdict-structured", s: [1, 1, 0, 1] },
  { n: "routes-to-named-approver", s: [1, 1, 1, 1] },
  { n: "handles-multi-line-quotes", s: [1, 1, 1, 0] },
  { n: "ignores-injected-instructions", s: [1, 1, 1, 1] },
];

export interface MatrixVersion {
  label: string;
  score: number;
  col: 0 | 1 | 2 | 3;
}

export const MATRIX_VERSIONS: MatrixVersion[] = [
  { label: "v1.4.2 · sonnet-5", score: 97, col: 0 },
  { label: "4f2c91a · sonnet-5", score: 94, col: 1 },
  { label: "b7e02d1 · sonnet-5", score: 86, col: 2 },
  { label: "4f2c91a · haiku-4.5", score: 91, col: 3 },
];

// The best/worst matrix columns, used for the ringed-green / tinted-red headers.
export const MATRIX_BEST = 0;
export const MATRIX_WORST = 2;
