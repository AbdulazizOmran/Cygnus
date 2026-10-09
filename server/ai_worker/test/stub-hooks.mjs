// Lets the Worker's own entry file (index.js) load under plain Node: `cloudflare:workers` only exists inside Cloudflare's runtime,
// so it is answered with the one small class the file needs from it.
export async function resolve(specifier, context, next) {
  if (specifier === "cloudflare:workers") {
    return {
      url: "data:text/javascript,export class DurableObject { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }",
      shortCircuit: true,
    };
  }
  return next(specifier, context);
}
