// Thin HTTP wrapper around the real upstream PluginStorageRepository.
//
// The wrapper parses the declared query parameters, calls repository.query() once per request and
// returns what the repository returned. It does not compute cursors, change predicates or order,
// remove duplicates, add records or clamp limits. This is a test wrapper around an internal API,
// not an EmDash REST endpoint.
import { createServer } from "node:http";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";
import { pathToFileURL } from "node:url";

const { values: options } = parseArgs({
  options: {
    snapshot: { type: "string" },
    db: { type: "string" },
    fixture: { type: "string" },
    port: { type: "string", default: "0" },
    "max-requests": { type: "string", default: "2000" },
    "max-lifetime-seconds": { type: "string", default: "300" },
    "manifest-out": { type: "string" },
    "exit-on-stdin-close": { type: "boolean", default: false },
  },
});
for (const required of ["snapshot", "db", "fixture"]) {
  if (!options[required]) throw new Error(`--${required} is required`);
}
const HOST = "127.0.0.1";
const snapshot = resolve(options.snapshot);
const databasePath = resolve(options.db);
const maxRequests = Number(options["max-requests"]);
const lifetimeSeconds = Number(options["max-lifetime-seconds"]);
if (existsSync(databasePath)) throw new Error("The database file must not exist; it is disposable");

const source = (relative) => pathToFileURL(join(snapshot, "packages/core/src", relative)).href;
const { Kysely, SqliteDialect } = await import("kysely");
const { openNodeSqliteDatabase } = await import(source("db/node-sqlite-compat.ts"));
const { runMigrations } = await import(source("database/migrations/runner.ts"));
const { PluginStorageRepository } = await import(source("database/repositories/plugin-storage.ts"));

const db = new Kysely({
  dialect: new SqliteDialect({ database: openNodeSqliteDatabase(databasePath) }),
});
await runMigrations(db);

const fixture = JSON.parse(readFileSync(options.fixture, "utf8"));
const repository = new PluginStorageRepository(db, fixture.plugin_id, fixture.collection, fixture.indexes);
for (const document of fixture.documents) await repository.put(document.id, document.data);

if (options["manifest-out"]) {
  const files = Object.fromEntries(
    [...globalThis.__emdashLabExecuted].map(([path, digest]) => [path.slice(snapshot.length + 1), digest]).sort(),
  );
  writeFileSync(options["manifest-out"], JSON.stringify({ executed_upstream_files: files }, null, 1) + "\n");
}

const DECLARED = new Set(["orderBy", "direction", "limit", "cursor"]);
let requests = 0;

function send(response, status, body) {
  const text = JSON.stringify(body);
  response.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(text) });
  response.end(text);
}

const server = createServer(async (request, response) => {
  requests += 1;
  if (requests > maxRequests) {
    send(response, 503, { error: "request budget exhausted" });
    setImmediate(() => shutdown(0));
    return;
  }
  const url = new URL(request.url ?? "/", `http://${HOST}`);
  if (request.method !== "GET") return send(response, 405, { error: "GET only" });
  if (url.pathname === "/healthz") {
    return send(response, 200, { ready: true, documents: fixture.documents.length });
  }
  if (url.pathname !== "/query") return send(response, 404, { error: "not found" });

  for (const name of url.searchParams.keys()) {
    if (!DECLARED.has(name)) return send(response, 400, { error: `unsupported parameter: ${name}` });
  }
  const query = {};
  const orderBy = url.searchParams.get("orderBy");
  if (orderBy !== null) {
    const direction = url.searchParams.get("direction") ?? "asc";
    if (direction !== "asc" && direction !== "desc") return send(response, 400, { error: "invalid direction" });
    query.orderBy = { [orderBy]: direction };
  } else if (url.searchParams.has("direction")) {
    return send(response, 400, { error: "direction requires orderBy" });
  }
  if (url.searchParams.has("limit")) {
    const raw = url.searchParams.get("limit");
    if (!/^[0-9]+$/.test(raw)) return send(response, 400, { error: "invalid limit" });
    query.limit = Number(raw);
  }
  if (url.searchParams.has("cursor")) query.cursor = url.searchParams.get("cursor");

  try {
    const result = await repository.query(query);
    // JSON cannot carry `undefined`; the terminal page's absent cursor is written as null.
    send(response, 200, { items: result.items, cursor: result.cursor ?? null, hasMore: result.hasMore });
  } catch (error) {
    const known = error instanceof Error && ["StorageQueryError", "InvalidCursorError"].includes(error.name);
    send(response, known ? 400 : 500, { error: error?.name ?? "Error", message: String(error?.message ?? error) });
  }
});
server.headersTimeout = 5000;
server.requestTimeout = 10000;
server.keepAliveTimeout = 2000;

let stopping = false;
async function shutdown(code) {
  if (stopping) return;
  stopping = true;
  server.close();
  server.closeAllConnections();
  try {
    await db.destroy();
  } finally {
    process.exit(code);
  }
}
process.on("SIGTERM", () => shutdown(0));
process.on("SIGINT", () => shutdown(0));
if (options["exit-on-stdin-close"]) {
  process.stdin.on("end", () => shutdown(0));
  process.stdin.resume();
}
setTimeout(() => shutdown(0), lifetimeSeconds * 1000).unref();

server.listen(Number(options.port), HOST, () => {
  const { port } = server.address();
  console.log(JSON.stringify({ event: "listening", host: HOST, port, documents: fixture.documents.length }));
});
