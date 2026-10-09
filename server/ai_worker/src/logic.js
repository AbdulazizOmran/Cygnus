// The hosted assistant for Cygnus, as a Cloudflare Worker: it holds the Gemini key so that people do not need their own.
//
// It can do exactly one job. It accepts structured facts (an application's name, id and format, and documents Cygnus fetched),
// builds the message itself from the shared prompt, asks Gemini for a list of companion components, filters the answer down to
// that one shape, and returns it. Cygnus checks every item again on the person's own computer, so this service is only a key
// holder, a limiter and a cache: nothing it says is acted on without those checks and the person's approval.
//
// This file has no Cloudflare-specific code, so it can be tested with plain `node --test`. index.js is the thin glue.
//
// Limits (so that a shared free key cannot be drained): per address, per minute and per hour; for everyone, per minute and per
// day; identical requests are answered from a cache and cost nothing. Requests are not logged. Settings (wrangler.toml [vars]):
// GEMINI_MODEL, DAILY_CAP, GLOBAL_PER_MIN, PER_IP_MIN, PER_IP_HOUR, CACHE_TTL_S, DISABLED=1; the secret is GEMINI_API_KEY.

import prompt from "./prompt.js";

export const MAX_BODY = 1024 * 1024; // 80,000 characters at up to 4 bytes each, plus escaping (the app sends UTF-8, not \uXXXX)
export const MAX_TOTAL_TEXT = 80_000;
export const MAX_COMPONENTS = 30;
export const KINDS = ["package", "group", "service", "extension", "info"];
const FORMAT = /^[a-z][a-z0-9-]{0,19}$/;
const ID = /^[A-Za-z0-9._:/@+-]{0,255}$/;
const MODEL = /^[A-Za-z0-9._-]{1,60}$/;
const DEFAULT_MODEL = "gemini-flash-lite-latest"; // fast and cheap; "gemini-flash-latest" is a slow thinking model (30 s for one word)
const CONTROL = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/;
const GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/";
const CACHE_ENTRIES = 2000;

export class Refused extends Error {
  constructor(status, message, retryAfter = 0) {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

// Python counted characters, JavaScript counts UTF-16 units: count code points so that both sides agree about 20,000 characters.
const length = (text) => [...text].length;
const cut = (text, limit) => (typeof text === "string" ? [...text].slice(0, limit).join("") : "");
const isObject = (value) => typeof value === "object" && value !== null && !Array.isArray(value);
const sameKeys = (object, keys) => {
  const have = Object.keys(object).sort();
  return have.length === keys.length && have.every((k, i) => k === [...keys].sort()[i]);
};

function number(env, name, fallback) {
  const raw = env?.[name];
  return typeof raw === "string" && /^\s*\d+\s*$/.test(raw) ? parseInt(raw, 10) : fallback;
}

export function settings(env) {
  return {
    perMin: number(env, "PER_IP_MIN", 4),
    perHour: number(env, "PER_IP_HOUR", 15),
    globalMin: number(env, "GLOBAL_PER_MIN", 8),
    daily: number(env, "DAILY_CAP", 300),
    ttl: number(env, "CACHE_TTL_S", 14 * 86400),
  };
}

// -- the request ------------------------------------------------------------------------------------------------------------
function text(value, low, high) {
  if (typeof value !== "string") throw new Refused(400, "the request is not in the expected form");
  const n = length(value);
  if (n < low || n > high || CONTROL.test(value)) throw new Refused(400, "the request is not in the expected form");
  return value;
}

export function parseRequest(body) {
  if (body.length > MAX_BODY) throw new Refused(413, "the request is too large");
  let data;
  try {
    data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
  } catch {
    throw new Refused(400, "the request is not valid JSON");
  }
  if (!isObject(data) || !sameKeys(data, ["v", "application", "documents"])) throw new Refused(400, "the request is not in the expected form");
  if (data.v !== prompt.version) throw new Refused(426, "this version of Cygnus is too old for the assistant");
  const { application: app, documents: docs } = data;
  if (!isObject(app) || !sameKeys(app, ["name", "id", "format"]) || !Array.isArray(docs) || docs.length < 1 || docs.length > prompt.maxDocuments) {
    throw new Refused(400, "the request is not in the expected form");
  }
  const clean = { name: text(app.name, 1, prompt.maxName), id: text(app.id, 0, 255), format: text(app.format, 1, 20) };
  if (clean.name.includes("\n") || !ID.test(clean.id) || !FORMAT.test(clean.format)) throw new Refused(400, "the request is not in the expected form");
  const documents = [];
  let total = 0;
  for (const d of docs) {
    if (!isObject(d) || !sameKeys(d, ["url", "text"])) throw new Refused(400, "the request is not in the expected form");
    const url = text(d.url, 12, 300);
    if (!url.startsWith("https://") || /[\s\x85<>]/.test(url) || url.includes('"')) throw new Refused(400, "the request is not in the expected form");
    const page = text(d.text, 1, prompt.maxDocumentChars);
    total += length(page);
    documents.push({ url, text: page });
  }
  if (total > MAX_TOTAL_TEXT) throw new Refused(413, "the request is too large");
  return { application: clean, documents };
}

// What the model is told: the application, and the documents as data (a closing tag inside one cannot end it).
export function buildUser(app, documents) {
  const docs = documents.map((d) => `<document url="${d.url}">\n${d.text.replace(/<(?=\s*\/?\s*document)/gi, "< ")}\n</document>`).join("\n\n");
  return `Application: ${app.name} (id ${app.id || "unknown"}, installed as ${app.format}).\nFind what else it needs, using only these documents:\n\n${docs}`;
}

// -- the answer -------------------------------------------------------------------------------------------------------------
// Only the one shape this service exists to produce, nothing else: anything that does not fit is refused (502).
export function cleanAnswer(answer) {
  if (!isObject(answer) || !Array.isArray(answer.components)) throw new Refused(502, "the assistant's answer was not usable");
  const out = [];
  for (const raw of answer.components.slice(0, MAX_COMPONENTS)) {
    if (!isObject(raw) || !isObject(raw.action) || !isObject(raw.citation)) continue;
    const { action } = raw;
    if (!KINDS.includes(action.kind)) continue;
    const kept = { kind: action.kind };
    for (const k of ["name", "unit", "url"]) if (typeof action[k] === "string") kept[k] = cut(action[k], 300);
    out.push({
      name: cut(raw.name, 100),
      relation: raw.relation === "required" ? "required" : "optional",
      why: cut(raw.why, 400),
      action: kept,
      citation: { url: cut(raw.citation.url, 300), quote: cut(raw.citation.quote, 400) },
    });
  }
  return { components: out };
}

// One call to Gemini with the service's own key (the key goes in a header, to this one fixed address, and nowhere else).
export function makeAsk(env, fetchImpl = (...args) => fetch(...args)) {
  return async (system, user) => {
    const key = env.GEMINI_API_KEY;
    if (!key) throw new Refused(503, "the assistant is not set up");
    const model = MODEL.test(env.GEMINI_MODEL || "") ? env.GEMINI_MODEL : DEFAULT_MODEL;
    const body = {
      systemInstruction: { parts: [{ text: system }] },
      contents: [{ role: "user", parts: [{ text: user }] }],
      generationConfig: { temperature: 0, responseMimeType: "application/json", maxOutputTokens: 4096 },
    };
    let raw;
    try {
      const response = await fetchImpl(`${GEMINI}${model}:generateContent`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-goog-api-key": key },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(60_000),
      });
      if (!response.ok) {
        throw new Refused([429, 500, 502, 503].includes(response.status) ? 503 : 502, "the assistant is busy or unavailable; try again later");
      }
      raw = await response.text();
    } catch (error) {
      if (error instanceof Refused) throw error;
      throw new Refused(503, "the assistant could not be reached; try again later");
    }
    try {
      if (raw.length > 1024 * 1024) throw new Error("too large");
      const data = JSON.parse(raw);
      if (data?.promptFeedback?.blockReason) throw new Refused(502, "the assistant declined this request");
      const parts = data.candidates[0].content.parts;
      const joined = parts.map((p) => (typeof p?.text === "string" ? p.text : "")).join("");
      return JSON.parse(joined.trim().replace(/^```(?:json)?\s*|\s*```$/g, ""));
    } catch (error) {
      if (error instanceof Refused) throw error;
      throw new Refused(502, "the assistant's answer was not usable");
    }
  };
}

// -- who is asking ----------------------------------------------------------------------------------------------------------
function ipv4(text) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(text);
  if (!m) return null;
  const parts = m.slice(1).map((p) => (p.length > 1 && p[0] === "0" ? NaN : Number(p)));
  return parts.every((n) => n >= 0 && n <= 255) ? parts : null;
}

function ipv6(text) {
  if (!/^[0-9A-Fa-f:.]+$/.test(text) || text.includes(":::")) return null;
  let s = text;
  if (s.includes(".")) {
    const colon = s.lastIndexOf(":");
    const v4 = ipv4(s.slice(colon + 1));
    if (!v4) return null;
    s = `${s.slice(0, colon + 1)}${((v4[0] << 8) | v4[1]).toString(16)}:${((v4[2] << 8) | v4[3]).toString(16)}`;
  }
  const halves = s.split("::");
  if (halves.length > 2) return null;
  const side = (part) => (part === "" ? [] : part.split(":"));
  const head = side(halves[0]);
  let groups = head;
  if (halves.length === 2) {
    const rest = side(halves[1]);
    const missing = 8 - head.length - rest.length;
    if (missing < 1) return null;
    groups = [...head, ...Array(missing).fill("0"), ...rest];
  }
  if (groups.length !== 8 || !groups.every((g) => /^[0-9a-fA-F]{1,4}$/.test(g))) return null;
  return groups.map((g) => parseInt(g, 16));
}

// The address the request really came from (Cloudflare's own header, which a caller cannot set). An IPv6 address is reduced to
// its /64, which one connection can hold entirely; an IPv4 address written inside an IPv6 one counts as that IPv4 address.
export function clientIp(raw) {
  const text = typeof raw === "string" ? raw.trim() : "";
  const v4 = ipv4(text);
  if (v4) return v4.join(".");
  const g = ipv6(text);
  if (!g) return "unknown";
  if (g.slice(0, 5).every((n) => n === 0) && g[5] === 0xffff) return [g[6] >> 8, g[6] & 255, g[7] >> 8, g[7] & 255].join(".");
  return `${g.slice(0, 4).map((n) => n.toString(16)).join(":")}::`;
}

// -- the memory: limits, the day's count and the cache, in the Durable Object's SQLite ---------------------------------------
const SCHEMA = [
  "CREATE TABLE IF NOT EXISTS hits (ip TEXT NOT NULL, ts REAL NOT NULL)",
  "CREATE INDEX IF NOT EXISTS hits_ip ON hits (ip, ts)",
  "CREATE INDEX IF NOT EXISTS hits_ts ON hits (ts)",
  "CREATE TABLE IF NOT EXISTS daily (day TEXT PRIMARY KEY, n INTEGER NOT NULL)",
  "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, ts REAL NOT NULL, answer TEXT NOT NULL)",
  "CREATE INDEX IF NOT EXISTS cache_ts ON cache (ts)",
];

export class SqlStore {
  // `sql` is the Durable Object's `ctx.storage.sql`: exec(query, ...bindings) returns a cursor with toArray().
  constructor(sql) {
    this.sql = sql;
    for (const statement of SCHEMA) sql.exec(statement).toArray(); // reading the cursor is what makes sure it has run
  }

  rows(query, ...bindings) {
    return this.sql.exec(query, ...bindings).toArray();
  }

  getCache(key, now, ttl) {
    const [row] = this.rows("SELECT ts, answer FROM cache WHERE key = ?", key);
    if (!row) return null;
    if (now - row.ts > ttl) {
      this.rows("DELETE FROM cache WHERE key = ?", key);
      return null;
    }
    try {
      return JSON.parse(row.answer);
    } catch {
      this.rows("DELETE FROM cache WHERE key = ?", key);
      return null;
    }
  }

  putCache(key, answer, now) {
    this.rows("INSERT OR REPLACE INTO cache (key, ts, answer) VALUES (?, ?, ?)", key, now, JSON.stringify(answer));
    this.rows("DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY ts DESC LIMIT -1 OFFSET ?)", CACHE_ENTRIES);
  }

  // Forgets what no limit can see any more: addresses older than an hour and cached answers past their age. Deleting nothing writes nothing.
  prune(now, ttl) {
    this.rows("DELETE FROM hits WHERE ts < ?", now - 3600);
    this.rows("DELETE FROM cache WHERE ts < ?", now - ttl);
  }

  // Counts one request against everyone's limits, or says which limit stops it. A refusal spends nothing and writes nothing.
  reserve(ip, now, today, limits) {
    const mine = this.rows("SELECT ts FROM hits WHERE ip = ? AND ts >= ?", ip, now - 3600);
    if (mine.filter((r) => r.ts >= now - 60).length >= limits.perMin) throw new Refused(429, "too many requests from your address; wait a minute", 60);
    if (mine.length >= limits.perHour) throw new Refused(429, "you have reached the hourly limit from your address", 600);
    const [{ n: everyone }] = this.rows("SELECT COUNT(*) AS n FROM hits WHERE ts >= ?", now - 60);
    if (everyone >= limits.globalMin) throw new Refused(429, "the shared assistant is busy; try again in a minute", 30);
    const [row] = this.rows("SELECT n FROM daily WHERE day = ?", today);
    if ((row?.n ?? 0) >= limits.daily) throw new Refused(429, "the shared assistant has reached its limit for today", 3600);
    this.rows("INSERT INTO hits (ip, ts) VALUES (?, ?)", ip, now);
    this.rows("INSERT INTO daily (day, n) VALUES (?, 1) ON CONFLICT (day) DO UPDATE SET n = n + 1", today);
    this.rows("DELETE FROM daily WHERE day <> ?", today);
  }
}

async function digest(value) {
  const bytes = new TextEncoder().encode(value);
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(hash)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// The Durable Object's work: the cache, then the limits, then the model, then the cache again. Everything that touches the memory
// before the single `await` is synchronous, so two requests can never both take the last place under a limit.
export class Assistant {
  constructor(store, env, ask) {
    this.store = store;
    this.env = env;
    this.ask = ask;
  }

  async handle(request, ip, now = Date.now() / 1000) {
    try {
      const limits = settings(this.env);
      this.store.prune(now, limits.ttl);
      const model = MODEL.test(this.env.GEMINI_MODEL || "") ? this.env.GEMINI_MODEL : DEFAULT_MODEL;
      // The key of the cache holds what shapes the answer, so that a new prompt or model never gets an old answer.
      const key = await digest(JSON.stringify([prompt.version, prompt.system, model, request]));
      const cached = this.store.getCache(key, now, limits.ttl);
      if (cached !== null) return { status: 200, payload: { answer: cached, cached: true }, headers: {} };
      this.store.reserve(ip, now, new Date(now * 1000).toISOString().slice(0, 10), limits);
      const answer = cleanAnswer(await this.ask(prompt.system, buildUser(request.application, request.documents)));
      this.store.putCache(key, answer, now);
      return { status: 200, payload: { answer, cached: false }, headers: {} };
    } catch (error) {
      return refusal(error);
    }
  }
}

function refusal(error) {
  if (!(error instanceof Refused)) return { status: 503, payload: { error: "the assistant is not available right now; try again later" }, headers: {} };
  return { status: error.status, payload: { error: error.message }, headers: error.retryAfter ? { "Retry-After": String(error.retryAfter) } : {} };
}

// -- the front door ----------------------------------------------------------------------------------------------------------
// Reads at most `max` bytes of a body (a caller cannot make the service hold more by leaving the length out).
export async function readLimited(stream, max) {
  if (!stream) return new Uint8Array(0);
  const reader = stream.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    chunks.push(value);
    if (total >= max) {
      await reader.cancel();
      break;
    }
  }
  const out = new Uint8Array(Math.min(total, max));
  let at = 0;
  for (const chunk of chunks) {
    const room = out.length - at;
    if (room <= 0) break;
    out.set(chunk.subarray(0, room), at);
    at += Math.min(chunk.length, room);
  }
  return out;
}

// The whole of what the Worker does with one HTTP request, as plain data in and out. `backend.handle(request, ip)` is the
// Durable Object (or a stand-in in tests); the checks that need no memory happen here, so junk never costs a Durable Object call.
export async function serve({ method, path, ip, env, readBody }, backend) {
  if (path === "/healthz") return { status: 200, payload: { ok: true, ready: Boolean(env.GEMINI_API_KEY) && env.DISABLED !== "1" }, headers: {} };
  if (path !== "/v1/needs") return { status: 404, payload: { error: "not found" }, headers: {} };
  if (method !== "POST") return { status: 405, payload: { error: "use POST" }, headers: { Allow: "POST" } };
  try {
    if (env.DISABLED === "1") throw new Refused(503, "the assistant is switched off for now");
    if (!env.GEMINI_API_KEY) throw new Refused(503, "the assistant is not set up");
    const address = clientIp(ip);
    if (env.PER_ADDRESS && !(await env.PER_ADDRESS.limit({ key: address })).success) {
      throw new Refused(429, "too many requests from your address; wait a minute", 60);
    }
    const request = parseRequest(await readBody());
    return await backend.handle(request, address);
  } catch (error) {
    return refusal(error);
  }
}

export const RESPONSE_HEADERS = {
  "Content-Type": "application/json",
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
};
