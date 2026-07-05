import JSZip from "jszip";

// Packages the editor's skill.md into a Claude Code plugin bundle, client-side.
// jszip is the packaging choice: it is the de-facto browser zip library, ships
// its own types, and produces an archive that apps/api's zip intake accepts
// (a flat zip whose root holds .claude-plugin/plugin.json). The API validates
// the bytes via the frozen plugin_format validator, so this only has to lay out
// the canonical file tree:
//
//   .claude-plugin/plugin.json      (manifest: name required)
//   skills/<name>/SKILL.md          (the editor content; frontmatter name+description)

export interface BundleInput {
  agentName: string;
  versionLabel: string;
  skillMd: string;
}

// Pull `description:` out of the SKILL.md frontmatter for the manifest, so the
// bundle carries a real description rather than a placeholder.
function descriptionFrom(skillMd: string): string {
  const match = skillMd.match(/^\s*description:\s*(.+)$/m);
  return match ? match[1].trim() : "";
}

// The path -> content map for the bundle. Exposed as a seam so the layout can be
// unit-tested without a Blob round-trip (jsdom Blobs are not reliably readable).
export function bundleFileTree(input: BundleInput): Record<string, string> {
  const manifest = {
    name: input.agentName,
    version: input.versionLabel,
    description: descriptionFrom(input.skillMd) || `Agent ${input.agentName}`,
  };
  return {
    ".claude-plugin/plugin.json": JSON.stringify(manifest, null, 2),
    [`skills/${input.agentName}/SKILL.md`]: input.skillMd,
  };
}

export async function buildBundleZip(input: BundleInput): Promise<Blob> {
  const zip = new JSZip();
  for (const [path, content] of Object.entries(bundleFileTree(input))) {
    zip.file(path, content);
  }
  return zip.generateAsync({ type: "blob", compression: "DEFLATE" });
}
