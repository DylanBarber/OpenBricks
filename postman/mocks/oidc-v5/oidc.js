// Stateful local OIDC stand-in. Synthetic identities only; private keys stay in memory.
// Based on contracts/oidc-provider.openapi.yaml and OpenID Connect Core code flow.
const http = require("node:http");
const crypto = require("node:crypto");
const BASE = process.env.OIDC_MOCK_BASE_URL || "http://127.0.0.1:4612/oidc";
const CLIENT = "openbricks-smoke";
const REDIRECT = process.env.OIDC_MOCK_REDIRECT_URI || "http://127.0.0.1:3009/api/auth/oidc/callback";
const codes = new Map();
const counts = {};
let mode = "valid";
let pair = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
let kid = "oidc-v5-initial";
const badPair = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
const b64 = (value) => Buffer.from(value).toString("base64url");
const hash = (value) => crypto.createHash("sha256").update(value).digest("base64url");

function send(res, status, value) {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  res.end(JSON.stringify(value));
}
async function body(req) {
  if (req.body && typeof req.body === "object") return req.body;
  let text = typeof req.body === "string" ? req.body : "";
  if (!text) {
    for await (const chunk of req) {
      text += chunk;
      if (Buffer.byteLength(text) > 16384) throw new Error("request_too_large");
    }
  }
  return String(req.headers["content-type"] || "").includes("application/json")
    ? JSON.parse(text || "{}") : Object.fromEntries(new URLSearchParams(text));
}
function sign(claims) {
  const head = b64(JSON.stringify({ alg: "RS256", typ: "JWT", kid }));
  const payload = b64(JSON.stringify(claims));
  const signature = crypto.sign("RSA-SHA256", Buffer.from(`${head}.${payload}`), mode === "bad_signature" ? badPair.privateKey : pair.privateKey);
  return `${head}.${payload}.${signature.toString("base64url")}`;
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, BASE);
    const path = url.pathname.replace(/^\/oidc(?=\/|$)/, "");
    counts[`${req.method} ${path}`] = (counts[`${req.method} ${path}`] || 0) + 1;
    if (req.method === "GET" && path === "/health") return send(res, 200, { status: "ok", synthetic_provider: true });
    if (req.method === "GET" && path === "/__test/requests") return send(res, 200, counts);
    if (req.method === "POST" && path === "/__test/mode") {
      const input = await body(req);
      if (!["valid", "bad_nonce", "bad_signature", "bad_audience", "expired", "bad_groups", "provider_error"].includes(input.mode)) return send(res, 400, { error: "unknown_scenario" });
      mode = input.mode;
      return send(res, 200, { mode });
    }
    if (req.method === "POST" && path === "/__test/rotate-key") {
      pair = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
      kid = `oidc-v5-${crypto.randomBytes(8).toString("hex")}`;
      return send(res, 200, { rotated: true });
    }
    if (req.method === "GET" && path === "/.well-known/openid-configuration") return send(res, 200, {
      issuer: BASE, authorization_endpoint: `${BASE}/authorize`, token_endpoint: `${BASE}/token`, jwks_uri: `${BASE}/jwks`,
      response_types_supported: ["code"], subject_types_supported: ["public"], id_token_signing_alg_values_supported: ["RS256"],
      code_challenge_methods_supported: ["S256"], token_endpoint_auth_methods_supported: ["none"],
    });
    if (req.method === "GET" && path === "/jwks") return send(res, 200, {
      keys: [{ ...pair.publicKey.export({ format: "jwk" }), kid, alg: "RS256", use: "sig" }],
    });
    if (req.method === "GET" && path === "/authorize") {
      const input = Object.fromEntries(url.searchParams);
      if (input.client_id !== CLIENT || input.redirect_uri !== REDIRECT || input.response_type !== "code"
          || input.code_challenge_method !== "S256" || !/^[A-Za-z0-9_-]{43}$/.test(input.code_challenge || "")
          || !/^[A-Za-z0-9_-]{20,200}$/.test(input.nonce || "") || !/^[A-Za-z0-9_-]{20,200}$/.test(input.state || "")
          || !(input.scope || "").split(" ").includes("openid")) return send(res, 400, { error: "invalid_request" });
      for (const [code, value] of codes) if (value.expires < Date.now()) codes.delete(code);
      if (codes.size >= 256) return send(res, 503, { error: "temporarily_unavailable" });
      const code = crypto.randomBytes(32).toString("base64url");
      codes.set(code, { ...input, expires: Date.now() + 300000 });
      res.writeHead(302, { Location: `${REDIRECT}?${new URLSearchParams({ code, state: input.state })}`, "Cache-Control": "no-store" });
      return res.end();
    }
    if (req.method === "POST" && path === "/token") {
      const input = await body(req);
      if (mode === "provider_error") return send(res, 500, { error: "fixture_provider_unavailable" });
      if (input.client_id !== CLIENT || input.redirect_uri !== REDIRECT || input.grant_type !== "authorization_code") return send(res, 400, { error: "invalid_client" });
      const original = codes.get(input.code);
      codes.delete(input.code);
      if (!original || original.expires < Date.now() || !/^[A-Za-z0-9._~-]{43,128}$/.test(input.code_verifier || "")
          || hash(input.code_verifier) !== original.code_challenge) return send(res, 400, { error: "invalid_grant" });
      const instant = Math.floor(Date.now() / 1000);
      const claims = { iss: BASE, sub: "postman-synthetic-alice", aud: mode === "bad_audience" ? "wrong-client" : CLIENT,
        iat: instant, exp: mode === "expired" ? instant - 300 : instant + 180,
        nonce: mode === "bad_nonce" ? "wrong-nonce" : original.nonce,
        groups: mode === "bad_groups" ? [] : ["openbricks-smoke-users"], name: "Postman synthetic Alice" };
      return send(res, 200, { id_token: sign(claims), access_token: crypto.randomBytes(32).toString("base64url"), token_type: "Bearer", expires_in: 180 });
    }
    return send(res, 501, { error: "dependency_endpoint_not_modeled" });
  } catch {
    return send(res, 400, { error: "invalid_request" });
  }
});
server.listen(process.env.PORT || 4613);
