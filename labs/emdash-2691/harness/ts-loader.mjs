// Node preload that lets the harness import EmDash's TypeScript source unchanged.
//
// * `./x.js` specifiers inside the snapshot resolve to `./x.ts`, as upstream's bundler does.
// * `.ts` files are transpiled one by one with the TypeScript compiler (no type checking).
//   Node's own type stripping cannot handle upstream's constructor parameter properties.
// * Bare specifiers (kysely, typescript) resolve from the lab's dependency directory, never from
//   the snapshot, so the snapshot stays read-only and free of node_modules.
//
// Environment: EMDASH_LAB_DEPS is the directory containing the harness node_modules.
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { createRequire, isBuiltin, registerHooks } from "node:module";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const depsDirectory = process.env.EMDASH_LAB_DEPS;
if (!depsDirectory) throw new Error("EMDASH_LAB_DEPS is required");
const depsParent = pathToFileURL(join(depsDirectory, "package.json")).href;
const ts = createRequire(depsParent)("typescript");

const compilerOptions = {
  // Mirrors the relevant options of upstream's tsconfig.base.json.
  target: ts.ScriptTarget.ES2024,
  module: ts.ModuleKind.ESNext,
  useDefineForClassFields: true,
  verbatimModuleSyntax: false,
};

/** @type {Map<string, string>} file path to sha256 of the bytes that were executed */
export const executed = new Map();
globalThis.__emdashLabExecuted = executed;

registerHooks({
  resolve(specifier, context, nextResolve) {
    const bare =
      !specifier.startsWith(".") &&
      !specifier.startsWith("/") &&
      !specifier.startsWith("file:") &&
      !isBuiltin(specifier);
    if (bare) return nextResolve(specifier, { ...context, parentURL: depsParent });
    if (specifier.startsWith(".") && specifier.endsWith(".js") && context.parentURL?.startsWith("file:")) {
      const target = new URL(specifier, context.parentURL);
      const source = fileURLToPath(target).replace(/\.js$/, ".ts");
      if (!existsSync(fileURLToPath(target)) && existsSync(source)) {
        return { url: pathToFileURL(source).href, format: "module", shortCircuit: true };
      }
    }
    return nextResolve(specifier, context);
  },
  load(url, context, nextLoad) {
    if (!url.startsWith("file:") || !url.endsWith(".ts")) return nextLoad(url, context);
    const path = fileURLToPath(url);
    const bytes = readFileSync(path);
    executed.set(path, createHash("sha256").update(bytes).digest("hex"));
    const output = ts.transpileModule(bytes.toString("utf8"), { compilerOptions, fileName: path });
    return { format: "module", source: output.outputText, shortCircuit: true };
  },
});
