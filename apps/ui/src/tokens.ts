// Design tokens lifted verbatim from the design canon's `C = {...}` block.
// This is the single source of truth for color, font, radius; do not invent
// new values (per the design system: elevation via 1px borders, not shadows).

export const C = {
  page: "#121212",
  sidebar: "#171717",
  darkest: "#0f0f0f",
  card: "#1f1f1f",
  hover: "#292929",
  input: "#242424",
  sel: "#313131",
  border: "#2e2e2e",
  borderStrong: "#363636",
  borderMax: "#454545",
  text: "#fafafa",
  text2: "#b4b4b4",
  muted: "#898989",
  disabled: "#4d4d4d",
  brand: "#3ecf8e",
  link: "#00c573",
  destructive: "#e54d2e",
  success: "#2EA043",
  failure: "#CF222E",
  warn: "#BF8700",
  mutedStatus: "#8B949E",
  mono: '"SF Mono", Menlo, Monaco, Consolas, "Liberation Mono", monospace',
  sans: 'system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif',
} as const;

export type Tokens = typeof C;

// Radii used across the design: 6-8px buttons/inputs, 12-16px cards.
export const R = {
  btn: 7,
  input: 7,
  card: 14,
  cardLg: 16,
} as const;
