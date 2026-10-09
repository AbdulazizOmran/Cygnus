// The Cloudflare glue around logic.js: one Worker in front, one Durable Object behind it that holds the limits, the cache and
// the call to Gemini (so that every call to Gemini leaves from the same place, and the counts are exact).
import { DurableObject } from "cloudflare:workers";
import { Assistant, MAX_BODY, RESPONSE_HEADERS, SqlStore, makeAsk, readLimited, serve } from "./logic.js";

export class Limiter extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.assistant = new Assistant(new SqlStore(ctx.storage.sql), env, makeAsk(env));
  }

  handle(request, ip) {
    return this.assistant.handle(request, ip);
  }
}

const respond = ({ status, payload, headers }) =>
  new Response(JSON.stringify(payload), { status, headers: { ...RESPONSE_HEADERS, ...headers } });

export default {
  async fetch(request, env) {
    // `locationHint` only matters the first time: Gemini refuses some countries, so its calls should leave from North America.
    const backend = {
      handle: (parsed, ip) => env.LIMITER.get(env.LIMITER.idFromName("assistant"), { locationHint: "enam" }).handle(parsed, ip),
    };
    try {
      return respond(
        await serve(
          {
            method: request.method,
            path: new URL(request.url).pathname,
            ip: request.headers.get("CF-Connecting-IP"),
            env,
            readBody: () => readLimited(request.body, MAX_BODY + 1),
          },
          backend,
        ),
      );
    } catch {
      return respond({ status: 503, payload: { error: "the assistant is not available right now; try again later" }, headers: {} });
    }
  },
};
