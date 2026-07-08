import type { DeploymentOut, Environment } from "../api/client";

// Client-side env scoping: the agent_ids that have an ACTIVE deployment in the
// given environment. A deployment only counts when it is live (status "active")
// — superseded/stopped deployments do not place an agent in an environment.
export function agentIdsForEnv(deployments: DeploymentOut[], env: Environment): Set<string> {
  const ids = new Set<string>();
  for (const d of deployments) {
    if (d.status === "active" && d.environment === env) ids.add(d.agent_id);
  }
  return ids;
}

// Client-side env visibility: the agent_ids that should be HIDDEN in `env`
// because they are deployed EXCLUSIVELY to the other env(s). An agent is hidden
// only when it has >=1 ACTIVE deployment but NONE active in `env`. Agents with
// no active deployments anywhere (e.g. just created, never deployed) are never
// hidden — they remain visible in every environment.
export function hiddenAgentIdsForEnv(deployments: DeploymentOut[], env: Environment): Set<string> {
  const activeAnywhere = new Set<string>();
  const activeHere = new Set<string>();
  for (const d of deployments) {
    if (d.status !== "active") continue;
    activeAnywhere.add(d.agent_id);
    if (d.environment === env) activeHere.add(d.agent_id);
  }
  const hidden = new Set<string>();
  for (const id of activeAnywhere) {
    if (!activeHere.has(id)) hidden.add(id);
  }
  return hidden;
}
