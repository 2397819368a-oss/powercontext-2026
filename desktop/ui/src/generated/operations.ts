import { readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import YAML from "yaml";
import openapiTS, { astToString } from "openapi-typescript";
const root = new URL("../", import.meta.url);
const raw = await readFile(new URL("../openapi/powercontext.yaml", root), "utf8");
const contract = YAML.parse(raw);
const wanted = ["get_liveness","get_readiness","get_capabilities","get_access_principal","list_scopes","get_scope","get_default_scope","remember_memory","search_memory","get_artifact_revision"];
const operations = {};
for (const [path, item] of Object.entries(contract.paths))
  for (const [method, op] of Object.entries(item))
    if (wanted.includes(op?.operationId)) operations[op.operationId] = { method: method.toUpperCase(), path };
if (Object.keys(operations).length !== wanted.length) throw new Error("Missing public operation");
const digest = createHash("sha256").update(raw.replaceAll("\r\n","\n")).digest("hex");
const license = (await readFile(new URL(import.meta.url), "utf8")).split(" */

// Generated from openapi/powercontext.yaml. Do not edit.
export const contractSha256 = "5878999ce9e239d6642e4da858074b281ed465629432c578b3584eec47cbce6c";
export const operations = {
  "get_liveness": {
    "method": "GET",
    "path": "/health/live"
  },
  "get_readiness": {
    "method": "GET",
    "path": "/health/ready"
  },
  "get_capabilities": {
    "method": "GET",
    "path": "/v1/capabilities"
  },
  "list_scopes": {
    "method": "GET",
    "path": "/v1/scopes"
  },
  "get_scope": {
    "method": "GET",
    "path": "/v1/scopes/{scope_id}"
  },
  "get_default_scope": {
    "method": "GET",
    "path": "/v1/scopes/default"
  },
  "remember_memory": {
    "method": "POST",
    "path": "/v1/memory/remember"
  },
  "search_memory": {
    "method": "POST",
    "path": "/v1/memory/search"
  },
  "get_artifact_revision": {
    "method": "GET",
    "path": "/v1/scopes/{scope_id}/artifacts/{family}/{artifact_id}/revisions/{revision}"
  },
  "get_access_principal": {
    "method": "GET",
    "path": "/v1/access/me"
  }
} as const;
