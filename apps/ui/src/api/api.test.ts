import { afterEach, describe, expect, it, vi } from "vitest";
import { bundleFileTree, buildBundleZip } from "./bundle";
import { createAgent, uploadBundle, BundleValidationError, ApiError } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("bundleFileTree", () => {
  it("lays out the canonical plugin bundle tree with a name-bearing manifest", () => {
    const tree = bundleFileTree({
      agentName: "deal-desk",
      versionLabel: "v0.1.0",
      skillMd: "---\nname: deal-desk\ndescription: Approves deals\n---\n# body",
    });
    expect(Object.keys(tree)).toContain(".claude-plugin/plugin.json");
    expect(Object.keys(tree)).toContain("skills/deal-desk/SKILL.md");
    const manifest = JSON.parse(tree[".claude-plugin/plugin.json"]);
    expect(manifest.name).toBe("deal-desk");
    expect(manifest.version).toBe("v0.1.0");
    expect(manifest.description).toBe("Approves deals");
    expect(tree["skills/deal-desk/SKILL.md"]).toContain("# body");
  });

  it("produces a real zip Blob", async () => {
    const blob = await buildBundleZip({ agentName: "x", versionLabel: "v0", skillMd: "ok" });
    expect(blob.size).toBeGreaterThan(0);
  });
});

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api client", () => {
  it("sends the API key and returns the parsed agent", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(201, { id: "a1", name: "deal-desk", slack_channel: "#revenue-ops", created_at: "now" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const agent = await createAgent({ name: "deal-desk", slack_channel: "#revenue-ops" });
    expect(agent.id).toBe("a1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/agents");
    expect((init.headers as Record<string, string>)["X-API-Key"]).toBeTruthy();
  });

  it("surfaces validator issues from a 422 as BundleValidationError", async () => {
    const body = {
      detail: {
        detail: "bundle failed validation",
        errors: [
          { code: "skill.frontmatter_invalid", message: "description is required", location: "skills/x/SKILL.md" },
        ],
      },
    };
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(jsonResponse(422, body))));
    const archive = await buildBundleZip({ agentName: "x", versionLabel: "v0", skillMd: "bad" });
    const err = await uploadBundle("a1", "v1", archive).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(BundleValidationError);
    expect((err as BundleValidationError).issues[0].code).toBe("skill.frontmatter_invalid");
  });

  it("throws ApiError for a non-validation failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(409, { detail: "already stored" })));
    const archive = await buildBundleZip({ agentName: "x", versionLabel: "v0", skillMd: "ok" });
    await expect(uploadBundle("a1", "v1", archive)).rejects.toBeInstanceOf(ApiError);
  });
});
