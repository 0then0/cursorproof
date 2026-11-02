// Walks pages by calling the upstream repository directly, without HTTP. harness_check.py compares
// its output with the same walk through the HTTP wrapper on the same database file.
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";
import { pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";

const { values: o } = parseArgs({
  options: {
    snapshot: { type: "string" },
    db: { type: "string" },
    fixture: { type: "string" },
    orderBy: { type: "string" },
    direction: { type: "string", default: "asc" },
    limit: { type: "string" },
  },
});
const source = (relative) => pathToFileURL(join(resolve(o.snapshot), "packages/core/src", relative)).href;
const { Kysely, SqliteDialect } = await import("kysely");
const { openNodeSqliteDatabase } = await import(source("db/node-sqlite-compat.ts"));
const { PluginStorageRepository } = await import(source("database/repositories/plugin-storage.ts"));
const db = new Kysely({ dialect: new SqliteDialect({ database: openNodeSqliteDatabase(resolve(o.db)) }) });
const fixture = JSON.parse(readFileSync(o.fixture, "utf8"));
const repository = new PluginStorageRepository(db, fixture.plugin_id, fixture.collection, fixture.indexes);

const pages = [];
let cursor;
for (let page = 0; page < 60; page++) {
  const query = { limit: Number(o.limit), cursor };
  if (o.orderBy) query.orderBy = { [o.orderBy]: o.direction };
  const result = await repository.query(query);
  pages.push({ items: result.items, cursor: result.cursor ?? null, hasMore: result.hasMore });
  if (!result.hasMore || !result.cursor) break;
  cursor = result.cursor;
}
console.log(JSON.stringify(pages));
await db.destroy();
