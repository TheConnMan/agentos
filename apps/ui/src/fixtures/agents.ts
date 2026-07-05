import type { FixtureLevel } from "../state/types";
import type { Agent } from "./types";

// The full fleet, seeded to match the design canon's ALL_AGENTS().
// rev-analytics carries the deliberately-degrading eval trend (amber health).
export const ALL_AGENTS: Agent[] = [
  { id: "deal-desk", ch: "#revenue-ops", prodV: "v1.4.2", devV: "4f2c91a", score: 94, runs: 128, cost: "$4.20", health: "green", trend: [88, 90, 91, 92, 93, 94, 94, 95, 94, 94] },
  { id: "sre-triage", ch: "#incidents", prodV: "v0.9.1", devV: "a1c33e8", plugin: true, score: 91, runs: 64, cost: "$2.80", health: "green", trend: [85, 88, 89, 90, 91, 91, 92, 91, 91, 91] },
  { id: "rev-analytics", ch: "#analytics", prodV: "v2.1.0", devV: "c90ffb2", score: 82, runs: 212, cost: "$9.40", health: "amber", trend: [96, 95, 95, 94, 93, 90, 87, 84, 83, 82] },
  { id: "onboarding-faq", ch: "#help", prodV: "v1.1.0", devV: "d02aa71", score: 97, runs: 340, cost: "$1.90", health: "green", trend: [95, 96, 96, 97, 97, 97, 98, 97, 97, 97] },
  { id: "contract-review", ch: "#legal", prodV: "v1.0.3", devV: "e7712bb", score: 95, runs: 41, cost: "$3.10", health: "green", trend: [92, 93, 94, 94, 95, 95, 95, 96, 95, 95] },
];

// How many agents are visible at each fixture level, matching the canon's
// agents() slice: none pre-deploy, one at 3-4, two at 5, the full fleet at 6.
export function agentsForLevel(level: FixtureLevel): Agent[] {
  if (level < 3) return [];
  if (level === 3 || level === 4) return [ALL_AGENTS[0]];
  if (level === 5) return [ALL_AGENTS[0], ALL_AGENTS[1]];
  return ALL_AGENTS;
}
