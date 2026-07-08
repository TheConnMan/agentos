import { useEffect, useMemo, useState, type ReactNode } from "react";
import { C } from "../../tokens";
import { Button, Card, Chip, Dot } from "../../primitives";
import { SkillEditor } from "../../components/SkillEditor";
import { useStore } from "../../state/store";
import { useWired } from "../../state/wired";
import { useAgentVersions, useVersionFiles } from "../../api/hooks";
import {
  createVersion,
  uploadBundle,
  createDeployment,
  BundleValidationError,
  ApiError,
  type BundleFile,
  type BundleIssue,
} from "../../api/client";
import { buildBundleZipFromFiles, nextVersionLabel } from "../../api/bundle";

function Notice({ children }: { children: ReactNode }) {
  return <div style={{ padding: "34px 20px", textAlign: "center", color: C.muted, fontSize: 13 }}>{children}</div>;
}

function isSkillFile(path: string): boolean {
  return path.endsWith("/SKILL.md") || path === "SKILL.md";
}

// The wired agent-detail surface (FX2 headline). Opens from the Agents list:
// loads the agent's active version, shows its bundle's skills, lets you edit each
// skills/*/SKILL.md in the same editor as the create modal, and ships a new
// version via the create-path sequence (POST version + PUT bundle + activate
// deployment) carrying the edited content — nothing else in the bundle is lost.
export function WiredAgentDetail() {
  const { state, dispatch } = useStore();
  const { agents } = useWired();
  const agentId = state.agentDetail;
  const agent = agents.find((a) => a.id === agentId) ?? null;

  const versions = useAgentVersions(agentId);
  const activeVersion = versions.versions.find((v) => v.id === versions.activeVersionId) ?? null;
  const files = useVersionFiles(agentId, versions.activeVersionId);

  // Edited SKILL.md content keyed by bundle path, seeded from the loaded files.
  const [edited, setEdited] = useState<Record<string, string>>({});
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [deploying, setDeploying] = useState(false);
  const [deployError, setDeployError] = useState<string | null>(null);
  const [issues, setIssues] = useState<BundleIssue[]>([]);
  const [deployedLabel, setDeployedLabel] = useState<string | null>(null);
  const [confirmingPromote, setConfirmingPromote] = useState(false);
  const [promoting, setPromoting] = useState(false);
  const [promoteError, setPromoteError] = useState<string | null>(null);

  const skillFiles = useMemo(() => (files.files ?? []).filter((f) => isSkillFile(f.path)), [files.files]);

  // The version currently active in dev — the one promote-to-prod ships. Newest
  // active dev deployment whose version still exists; null when there is nothing
  // in dev to promote (the button is then hidden).
  const devActiveVersionId = useMemo(() => {
    const dev = versions.deployments
      .filter((d) => d.status === "active" && d.environment === "dev")
      .sort((a, b) => b.deployed_at.localeCompare(a.deployed_at))
      .find((d) => versions.versions.some((v) => v.id === d.version_id));
    return dev?.version_id ?? null;
  }, [versions.deployments, versions.versions]);

  useEffect(() => {
    // Reseed the editor whenever a new version's files arrive.
    const map: Record<string, string> = {};
    for (const f of files.files ?? []) map[f.path] = f.content;
    setEdited(map);
    setSelectedPath(skillFiles[0]?.path ?? null);
    setDeployError(null);
    setIssues([]);
    // skillFiles is derived from files.files, so files.files is the real dep.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [files.files]);

  // Return to the Agents list this detail was opened from. closeAgentDetail's
  // reducer lands on Overview, so navigate explicitly instead.
  const back = () => dispatch({ type: "go", nav: "agents" });

  const dirty = useMemo(
    () => (files.files ?? []).some((f) => (edited[f.path] ?? f.content) !== f.content),
    [files.files, edited],
  );

  const deploy = async () => {
    if (!agentId || !agent || deploying || !files.files) return;
    setDeploying(true);
    setDeployError(null);
    setIssues([]);
    const label = nextVersionLabel(versions.versions.map((v) => v.version_label));
    const merged: BundleFile[] = files.files.map((f) => ({ path: f.path, content: edited[f.path] ?? f.content }));
    try {
      const version = await createVersion(agentId, { version_label: label, created_by: "ui" });
      const archive = await buildBundleZipFromFiles(agent.name, merged);
      await uploadBundle(agentId, version.id, archive);
      await createDeployment({ agent_id: agentId, version_id: version.id, environment: state.env });
      setDeployedLabel(label);
      dispatch({ type: "toast", message: `Deployed ${label}` });
      versions.reload(); // refetch versions + deployments -> active version flips to the new one
    } catch (e) {
      if (e instanceof BundleValidationError) {
        setIssues(e.issues);
      } else {
        setDeployError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e));
      }
    } finally {
      setDeploying(false);
    }
  };

  // Promote the dev-active version to prod: a single createDeployment (prod gets
  // the server-default active status — no gitflow bot_identity plumbing here),
  // then refresh so the Versions/active state reflects the new prod deployment.
  const promote = async () => {
    if (!agentId || promoting || !devActiveVersionId) return;
    setPromoting(true);
    setPromoteError(null);
    try {
      await createDeployment({ agent_id: agentId, version_id: devActiveVersionId, environment: "prod" });
      dispatch({ type: "toast", message: "Promoted dev → prod" });
      setConfirmingPromote(false);
      versions.reload();
    } catch (e) {
      setPromoteError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e));
    } finally {
      setPromoting(false);
    }
  };

  const backLink = (
    <button
      type="button"
      onClick={back}
      style={{ background: "none", border: "none", color: C.muted, fontSize: 13, cursor: "pointer", marginBottom: 14, padding: 0 }}
    >
      ← Agents
    </button>
  );

  if (!agent) {
    return (
      <div>
        {backLink}
        <Notice>Agent not found.</Notice>
      </div>
    );
  }

  return (
    <div data-testid="agent-detail">
      {backLink}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 18 }}>
        <Dot color={C.brand} size={10} />
        <h1 style={{ fontSize: 22, fontWeight: 500, margin: 0, fontFamily: C.mono }} data-testid="agent-detail-name">
          {agent.name}
        </h1>
        {activeVersion ? (
          <Chip color={C.mutedStatus}>
            active {activeVersion.version_label}
          </Chip>
        ) : null}
        <span style={{ marginLeft: "auto", fontSize: 12.5, color: C.muted, fontFamily: C.mono }}>{agent.slack_channel}</span>
      </div>

      {devActiveVersionId ? (
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
          {confirmingPromote ? (
            <>
              <span style={{ fontSize: 12.5, color: C.text2, fontFamily: C.mono }}>Promote the dev-active version to prod?</span>
              <Button label="Cancel" variant="ghost" size="sm" onClick={() => setConfirmingPromote(false)} />
              {promoting ? (
                <Button label="Promoting…" variant="primary" size="sm" disabled />
              ) : (
                <Button label="Confirm promote" variant="primary" size="sm" onClick={() => void promote()} />
              )}
            </>
          ) : (
            <Button label="Promote to prod" size="sm" onClick={() => setConfirmingPromote(true)} />
          )}
          {promoteError ? (
            <span data-testid="promote-error" style={{ fontSize: 12, color: C.destructive, fontFamily: C.mono }}>
              Promote failed: {promoteError}
            </span>
          ) : null}
        </div>
      ) : null}

      {versions.loading ? (
        <Notice>Loading versions…</Notice>
      ) : versions.error ? (
        <Notice>{`Could not load versions: ${versions.error}`}</Notice>
      ) : versions.versions.length === 0 ? (
        <Notice>No versions yet for this agent.</Notice>
      ) : files.loading ? (
        <Notice>Loading skills…</Notice>
      ) : files.noBundle ? (
        <Card>
          <div data-testid="agent-detail-nobundle" style={{ padding: "8px 4px", color: C.text2, fontSize: 13.5 }}>
            <div style={{ fontWeight: 500, marginBottom: 4 }}>No bundle stored for {activeVersion?.version_label ?? "this version"}</div>
            <div style={{ color: C.muted }}>This version has no plugin bundle yet, so there are no skills to edit.</div>
          </div>
        </Card>
      ) : files.error ? (
        <Notice>{`Could not load skills: ${files.error}`}</Notice>
      ) : skillFiles.length === 0 ? (
        <Card>
          <Notice>This bundle has no skills/*/SKILL.md files.</Notice>
        </Card>
      ) : (
        <div>
          {skillFiles.length > 1 ? (
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
              {skillFiles.map((f) => (
                <button
                  key={f.path}
                  type="button"
                  data-testid="skill-tab"
                  onClick={() => setSelectedPath(f.path)}
                  style={{
                    fontFamily: C.mono,
                    fontSize: 12,
                    padding: "5px 10px",
                    borderRadius: 7,
                    cursor: "pointer",
                    background: f.path === selectedPath ? C.hover : C.card,
                    color: f.path === selectedPath ? C.text : C.text2,
                    border: "1px solid " + (f.path === selectedPath ? C.borderStrong : C.border),
                  }}
                >
                  {f.path}
                </button>
              ))}
            </div>
          ) : null}

          <div
            style={{
              border: "1px solid " + C.borderStrong,
              borderRadius: 12,
              overflow: "hidden",
              display: "flex",
              flexDirection: "column",
              marginBottom: 16,
            }}
          >
            {selectedPath ? (
              <SkillEditor
                key={selectedPath}
                path={selectedPath}
                value={edited[selectedPath] ?? ""}
                onChange={(next) => {
                  setEdited((prev) => ({ ...prev, [selectedPath]: next }));
                  setDeployedLabel(null);
                  if (deployError || issues.length) {
                    setDeployError(null);
                    setIssues([]);
                  }
                }}
                testId="skill-editor"
                height={360}
              />
            ) : null}
            {issues.length > 0 ? (
              <div
                data-testid="deploy-errors"
                style={{
                  borderTop: "1px solid rgba(229,77,46,.3)",
                  background: "rgba(229,77,46,.06)",
                  padding: "10px 16px",
                  maxHeight: 140,
                  overflow: "auto",
                }}
              >
                <div style={{ fontSize: 12, fontWeight: 600, color: C.destructive, marginBottom: 6 }}>Bundle validation failed</div>
                {issues.map((issue, i) => (
                  <div key={i} style={{ fontFamily: C.mono, fontSize: 11.5, color: C.text2, marginBottom: 3 }}>
                    <span style={{ color: C.destructive }}>{issue.code}</span>
                    {issue.location ? <span style={{ color: C.muted }}> · {issue.location}</span> : null}
                    <span style={{ color: C.text2 }}> — {issue.message}</span>
                  </div>
                ))}
              </div>
            ) : null}
            {deployError ? (
              <div
                data-testid="deploy-error"
                style={{
                  borderTop: "1px solid rgba(229,77,46,.3)",
                  background: "rgba(229,77,46,.06)",
                  padding: "10px 16px",
                  fontSize: 12.5,
                  color: C.destructive,
                  fontFamily: C.mono,
                }}
              >
                Deploy failed: {deployError}
              </div>
            ) : null}
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            {deployedLabel ? (
              <span data-testid="deploy-success" style={{ fontSize: 12.5, color: C.brand, fontFamily: C.mono }}>
                ✓ Deployed {deployedLabel}
              </span>
            ) : (
              <span style={{ fontSize: 12, color: C.muted, fontFamily: C.mono }}>
                {dirty ? "unsaved edits" : "no changes"}
              </span>
            )}
            <div style={{ marginLeft: "auto" }}>
              {deploying ? (
                <Button label="Deploying…" variant="primary" disabled />
              ) : (
                <Button label="Deploy new version" variant="primary" onClick={() => void deploy()} />
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
