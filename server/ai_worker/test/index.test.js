// index.js, the glue Cloudflare runs: every answer carries the safe headers, the address is the one Cloudflare reports (never one the
// caller wrote), a Durable Object that fails is a plain 503, and a body is bounded however it is sent.
import assert from "node:assert/strict";
import { register } from "node:module";
import test from "node:test";

register("./stub-hooks.mjs", import.meta.url);

let DatabaseSync;
try {
  ({ DatabaseSync } = await import("node:sqlite"));
} catch {
  // skipped below
}
const skip = DatabaseSync ? false : "node:sqlite is not available in this Node";
const { default: worker, Limiter } = await import("../src/index.js");
const { MAX_BODY } = await import("../src/logic.js");

const GOOD = {
  v: 1,
  application: { name: "WhatPulse", id: "org.whatpulse.WhatPulse", format: "flatpak" },
  documents: [{ url: "https://whatpulse.org/help", text: "WhatPulse needs the input group to see your keyboard." }],
};
const ANSWER = { components: [] };

function environment(vars = {}, { fail = false } = {}) {
  const db = new DatabaseSync(":memory:");
  const sql = {
    exec(query, ...bindings) {
      const rows = db.prepare(query).all(...bindings);
      return { toArray: () => rows };
    },
  };
  const hints = [];
  const env = { GEMINI_API_KEY: "test-key", ...vars };
  const limiter = new Limiter({ storage: { sql } }, env);
  limiter.assistant.ask = async () => ANSWER; // never the network
  env.LIMITER = {
    idFromName: (name) => name,
    get: (id, options) => {
      hints.push({ id, options });
      return { handle: async (request, ip) => (fail ? Promise.reject(new Error("the object is overloaded: secret detail")) : limiter.handle(request, ip)) };
    },
  };
  return { env, hints };
}

const call = (env, { method = "POST", path = "/v1/needs", body = JSON.stringify(GOOD), headers = {} } = {}) =>
  worker.fetch(new Request(`https://cygnus-ai.example${path}`, { method, body: method === "POST" ? body : undefined, headers }), env);

test("every answer carries headers that stop it being cached or sniffed", { skip }, async () => {
  const { env } = environment();
  for (const request of [{}, { method: "GET" }, { path: "/healthz", method: "GET" }, { path: "/nowhere", method: "GET" }, { body: "junk" }, { body: JSON.stringify({ ...GOOD, v: 7 }) }]) {
    const response = await call(env, request);
    assert.equal(response.headers.get("cache-control"), "no-store", JSON.stringify(request));
    assert.equal(response.headers.get("x-content-type-options"), "nosniff");
    assert.equal(response.headers.get("content-type"), "application/json");
    assert.ok(response.status >= 200 && response.status < 600);
    JSON.parse(await response.text()); // always JSON, whatever happened
  }
  const quiet = environment({ DISABLED: "1" }).env;
  assert.equal((await call(quiet)).headers.get("cache-control"), "no-store");
  const limited = await call(environment().env, { method: "GET" });
  assert.equal(limited.headers.get("allow"), "POST");
});

test("a good request gets its answer through the whole chain", { skip }, async () => {
  const { env } = environment();
  const response = await call(env);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { answer: ANSWER, cached: false });
});

test("the address is the one Cloudflare reports, and what the caller writes in other headers is ignored", { skip }, async () => {
  const { env } = environment({ PER_IP_MIN: "1" });
  const bodyFor = (i) => JSON.stringify({ ...GOOD, documents: [{ ...GOOD.documents[0], text: `${GOOD.documents[0].text} ${i}` }] });
  const first = await call(env, { body: bodyFor(1), headers: { "CF-Connecting-IP": "203.0.113.1", "X-Forwarded-For": "198.51.100.1" } });
  assert.equal(first.status, 200);
  // the same address behind a different forged header: the same person, so the second is refused
  const same = await call(env, { body: bodyFor(2), headers: { "CF-Connecting-IP": "203.0.113.1", "X-Forwarded-For": "198.51.100.99", "X-Real-IP": "192.0.2.5" } });
  assert.equal(same.status, 429);
  // another address Cloudflare reports: another person, whatever the forged headers say
  const other = await call(env, { body: bodyFor(3), headers: { "CF-Connecting-IP": "203.0.113.2", "X-Forwarded-For": "203.0.113.1" } });
  assert.equal(other.status, 200);
});

test("without a reported address everyone shares one bucket, and an address that cannot be read cannot choose another one", { skip }, async () => {
  const { env } = environment({ PER_IP_MIN: "1" });
  const body = (i) => JSON.stringify({ ...GOOD, documents: [{ ...GOOD.documents[0], text: `x ${i}` }] });
  assert.equal((await call(env, { body: body(1) })).status, 200);
  assert.equal((await call(env, { body: body(2), headers: { "CF-Connecting-IP": "not an address" } })).status, 429);
});

test("the Durable Object is asked from North America, and one that fails is a plain 503 without its details", { skip }, async () => {
  const good = environment();
  await call(good.env);
  assert.deepEqual(good.hints, [{ id: "assistant", options: { locationHint: "enam" } }]);
  const broken = environment({}, { fail: true });
  const response = await call(broken.env);
  assert.equal(response.status, 503);
  const text = await response.text();
  assert.ok(!text.includes("secret detail") && !text.includes("overloaded"));
  assert.match(JSON.parse(text).error, /not available/);
  assert.equal(response.headers.get("cache-control"), "no-store");
});

test("a body bigger than the limit is refused however it is sent, and never held whole", { skip }, async () => {
  const { env } = environment();
  const big = JSON.stringify({ ...GOOD, padding: "x".repeat(MAX_BODY + 10) });
  assert.equal((await call(env, { body: big })).status, 413);
  // sent in pieces, with no length given: the reader stops at the limit
  let produced = 0;
  const stream = new ReadableStream({
    pull(controller) {
      produced += 64 * 1024;
      controller.enqueue(new Uint8Array(64 * 1024).fill(120));
      if (produced > 50 * MAX_BODY) controller.close(); // would be 50 MiB if it were all read
    },
  });
  const response = await worker.fetch(new Request("https://cygnus-ai.example/v1/needs", { method: "POST", body: stream, duplex: "half" }), env);
  assert.equal(response.status, 413);
  assert.ok(produced < 4 * MAX_BODY, `the service pulled ${produced} bytes`);
});

test("the service answers its health check, and says when it is not ready", { skip }, async () => {
  assert.deepEqual(await (await call(environment().env, { path: "/healthz", method: "GET" })).json(), { ok: true, ready: true });
  assert.deepEqual(await (await call(environment({ GEMINI_API_KEY: "" }).env, { path: "/healthz", method: "GET" })).json(), { ok: true, ready: false });
});

test("the flood guard sits in front of the Durable Object", { skip }, async () => {
  const { env, hints } = environment({ PER_ADDRESS: { limit: async () => ({ success: false }) } });
  const response = await call(env, { headers: { "CF-Connecting-IP": "203.0.113.9" } });
  assert.equal(response.status, 429);
  assert.equal(response.headers.get("retry-after"), "60");
  assert.deepEqual(hints, []); // the Durable Object was never called
});

test("anything that goes wrong outside the service logic is a plain 503 too, with nothing of the error in it", { skip }, async () => {
  const { env } = environment();
  const odd = { get url() { throw new Error("boom with a secret inside"); }, method: "POST", headers: new Headers(), body: null };
  const response = await worker.fetch(odd, env);
  assert.equal(response.status, 503);
  const text = await response.text();
  assert.ok(!text.includes("boom") && !text.includes("secret"));
  assert.equal(response.headers.get("cache-control"), "no-store");
});
