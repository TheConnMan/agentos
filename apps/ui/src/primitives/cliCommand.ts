// cliCommand: a resolver that turns an action id + live context into the exact
// `agentos ...` command string a CliHint should copy. It is typed *against the
// CLI's own command manifest* (`cli/command-manifest.json`, emitted by
// `agentos schema`), imported at build time. Because the action ids and the
// per-action flag keys are derived from the manifest's literal types, a command
// or flag that is renamed or removed in the CLI breaks `pnpm typecheck` here --
// the console/CLI parity drift is loud, not silent (issue #278, epic #145).
//
// No network, no runtime coupling: the manifest is a same-repo JSON artifact
// baked into the bundle. The resolver walks it at runtime only to render the
// string (respecting positional vs. `--flag` args and clap's arg order); the
// *typing* is what enforces parity.

import { commandManifest as manifest } from "../generated/commandManifest";

// --- manifest shape (structural, for runtime walking) ----------------------

interface ManifestArg {
  readonly id: string;
  readonly long?: string;
  readonly short?: string;
  readonly positional: boolean;
  readonly required: boolean;
  readonly possible_values?: readonly string[];
}

interface ManifestNode {
  readonly name: string;
  readonly args?: readonly ManifestArg[];
  readonly subcommands?: readonly ManifestNode[];
}

// --- type-level derivation from the imported manifest ----------------------

type Manifest = typeof manifest;

// Element (union) type of a node's `subcommands` array.
type Subs<N> = N extends { subcommands: readonly (infer S)[] } ? S : never;

// Every leaf command path under a node, dot-joined (e.g. "local.message").
// A node with no subcommands is a leaf; a group recurses under its prefix.
type LeafPaths<N, Prefix extends string = ""> =
  Subs<N> extends never
    ? never
    : Subs<N> extends infer S
      ? S extends { name: infer Name extends string }
        ? Subs<S> extends never
          ? `${Prefix}${Name}`
          : LeafPaths<S, `${Prefix}${Name}.`>
        : never
      : never;

/**
 * The set of runnable command ids, derived from the manifest. Renaming or
 * removing a command in the CLI changes this union, so a stale call site
 * (`cliCommand("local.mesage", ...)`) stops type-checking.
 */
export type ActionId = LeafPaths<Manifest>;

// Resolve the manifest node type sitting at a dotted action id.
type NodeAt<N, Path extends string> = Path extends `${infer Head}.${infer Rest}`
  ? NodeAt<Extract<Subs<N>, { name: Head }>, Rest>
  : Extract<Subs<N>, { name: Path }>;

// The `--long` keys a command accepts (non-positional args that declare `long`).
type FlagKeys<N> = Subs<N> extends never
  ? N extends { args: readonly (infer A)[] }
    ? A extends { positional: false; long: infer L extends string }
      ? L
      : never
    : never
  : never;

// The positional arg ids a command accepts.
type PositionalKeys<N> = N extends { args: readonly (infer A)[] }
  ? A extends { positional: true; id: infer I extends string }
    ? I
    : never
  : never;

/**
 * The live context for an action: a value per positional arg (by id) and per
 * flag (by `--long` name, without the dashes). Flag values may be a string
 * (rendered `--flag value`) or a boolean (`--flag` when true, omitted when
 * false/undefined). Both key sets are derived from the manifest, so a renamed
 * or removed flag breaks the call site.
 */
export type CliContext<A extends ActionId> = Partial<
  Record<PositionalKeys<NodeAt<Manifest, A>>, string> &
    Record<FlagKeys<NodeAt<Manifest, A>>, string | boolean>
>;

// --- runtime walk ----------------------------------------------------------

const ROOT = manifest as unknown as ManifestNode;

function nodeAt(path: string[]): ManifestNode {
  let node: ManifestNode = ROOT;
  for (const segment of path) {
    const next = node.subcommands?.find((s) => s.name === segment);
    if (!next) {
      throw new Error(
        `cliCommand: unknown command "${path.join(" ")}" (no "${segment}" under "${node.name}")`,
      );
    }
    node = next;
  }
  return node;
}

/**
 * Resolve an action id + context into the exact `agentos ...` command string.
 *
 * Positionals are emitted in manifest (clap) order; flags follow, `--long`
 * form, quoted when the value contains whitespace. A context key that is not a
 * declared arg of the command throws (the typing catches this at build time;
 * the runtime guard covers untyped/`any` callers).
 */
export function cliCommand<A extends ActionId>(
  action: A,
  ctx: CliContext<A> = {} as CliContext<A>,
): string {
  const path = action.split(".");
  const node = nodeAt(path);
  const args = node.args ?? [];
  const provided = ctx as Record<string, string | boolean | undefined>;

  const positionals: string[] = [];
  const flags: string[] = [];

  for (const [key, value] of Object.entries(provided)) {
    if (value === undefined || value === false) continue;
    const arg =
      args.find((a) => a.positional && a.id === key) ??
      args.find((a) => !a.positional && a.long === key);
    if (!arg) {
      throw new Error(
        `cliCommand: "${key}" is not an argument of "agentos ${path.join(" ")}"`,
      );
    }
    if (arg.positional) {
      positionals.push(quote(String(value)));
    } else if (value === true) {
      flags.push(`--${arg.long}`);
    } else {
      flags.push(`--${arg.long} ${quote(String(value))}`);
    }
  }

  return ["agentos", ...path, ...positionals, ...flags].join(" ");
}

// Shell-quote a value only when it carries whitespace, so simple ids/flags stay
// readable while a multi-word message is preserved as one token.
function quote(value: string): string {
  return /\s/.test(value) ? `"${value.replace(/"/g, '\\"')}"` : value;
}
