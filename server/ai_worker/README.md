# The hosted assistant (a Cloudflare Worker)

Holds Cygnus's Gemini key so that people need none of their own. It does one job: take a program's name, id and format plus the
public documentation Cygnus fetched, ask Gemini what else the program needs, and return only a checked list. Cygnus re-checks every
item on the person's computer. See the "AI assistant" paragraphs of `docs/security.md`.

* `src/logic.js` — everything that matters, with no Cloudflare code, so `node --test test/` tests it (Node 22.5+).
* `src/index.js` — the glue: a Worker in front, one SQLite-backed Durable Object that holds the limits, the cache and the call to Gemini.
* `src/prompt.js`, `test/golden.json` — generated from `cygnus/core/ai/prompt.py` by `python3 server/ai_worker/sync_prompt.py`
  (a test in the main suite fails when they are stale).
* `wrangler.toml` — the settings. The key is **not** in it.

## Put it online (once)

```sh
cd server/ai_worker
npm install                      # wrangler, pinned in package-lock.json
npx wrangler login               # opens the browser: sign in to the Cloudflare account that should own it
npx wrangler deploy              # prints https://cygnus-ai.<your-subdomain>.workers.dev
npx wrangler secret put GEMINI_API_KEY    # paste the key when asked; it is never written to a file
```

The key comes from https://aistudio.google.com/apikey. Create it in a project that has **no billing account**: Google's free tier
then simply refuses requests once its allowance is used up, and cannot charge anything. Cloudflare's free plan behaves the same way.

The address is built into the app (`cygnus/core/ai/config.py`, `HOSTED_URL`); `CYGNUS_AI_URL` overrides it for testing
(for example `CYGNUS_AI_URL=https://… cygnus ai test` after `cygnus ai on`). A new Worker name means a new address in the app, so
keep the name `cygnus-ai` unless a release changes it.

## Running it

* Switch it off at once: `npx wrangler secret delete GEMINI_API_KEY` (the service then answers "not set up" and the app says it is
  unavailable), or `npx wrangler deploy --var DISABLED:1` (a later plain `npx wrangler deploy` switches it back on).
* Change the key (for example after it leaked): make a new one in AI Studio, then `npx wrangler secret put GEMINI_API_KEY`.
* Change the limits in `wrangler.toml` and deploy again: `DAILY_CAP` (model calls per day for everyone), `GLOBAL_PER_MIN`,
  `PER_IP_MIN`, `PER_IP_HOUR`, `CACHE_TTL_S`. Identical questions come from the cache and cost no call.
* Change the model with `GEMINI_MODEL` (letters, digits, `.`, `_`, `-` only). `gemini-flash-lite-latest` answers in about 2 seconds;
  `gemini-flash-latest` is currently a slow "thinking" model, and the 2.5 models are closed to new keys. `GET /healthz` says
  whether the service is ready (`{"ok": true, "ready": true}`), which is what the app's "Test the connection" asks.
* A first guard in front of the Durable Object (`PER_ADDRESS` in `wrangler.toml`) turns away any address that sends more than 30
  requests a minute, answers from the cache included. Cloudflare counts it per location and approximately.
* Free-plan ceilings (Cloudflare): 100,000 requests a day and 10 ms of CPU per request (measured: about 1 ms). When they are used up the Worker answers
  with an error and the app says the assistant is unavailable: it can never produce a bill.
* No requests are logged (`observability` is off). Cloudflare itself sees the sender's internet address, as any host does.
