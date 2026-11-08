// Development-server middleware only. Never imported by the app bundle.
const http = require("node:http");
const https = require("node:https");

const REQUEST_HEADERS = new Set([
  "accept", "accept-encoding", "authorization", "content-type", "content-length",
  "idempotency-key", "if-match", "if-none-match", "range", "if-range", "cache-control",
]);
const HOP_HEADERS = new Set([
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "te", "trailer", "transfer-encoding", "upgrade",
]);
const METHODS = new Set(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]);

function isLoopbackAddress(value) {
  return value === "127.0.0.1" || value === "::1" || value === "::ffff:127.0.0.1";
}

function isLocalRequest(req) {
  if (!isLoopbackAddress(req.socket.remoteAddress)) return false;
  try {
    const local = new URL(`http://${req.headers.host}`);
    if (!["localhost", "127.0.0.1", "[::1]"].includes(local.hostname) ||
        local.username || local.password || local.pathname !== "/" || local.search || local.hash) return false;
    if (req.headers.origin) {
      const origin = new URL(req.headers.origin);
      if (origin.protocol !== (req.socket.encrypted ? "https:" : "http:") || origin.host !== local.host ||
          origin.username || origin.password || origin.pathname !== "/" || origin.search || origin.hash) return false;
    }
    const site = req.headers["sec-fetch-site"];
    return !site || site === "same-origin" || site === "none";
  } catch (_) {
    return false;
  }
}

function createDevApiProxy({target, timeoutMs = 120000} = {}) {
  if (!target) return null;
  const upstream = new URL(target);
  if (!["http:", "https:"].includes(upstream.protocol) || upstream.username || upstream.password ||
      upstream.search || upstream.hash || upstream.pathname.replace(/\/+$/, "") !== "/api/v1") {
    throw new Error("AMBIENT_DEV_API_PROXY_TARGET must be an HTTP(S) /api/v1 URL without credentials or a query.");
  }
  const transport = upstream.protocol === "https:" ? https : http;

  return function devApiProxy(req, res, next) {
    const rawPath = String(req.url || "");
    if (!/^\/api\/v1(?:[/?]|$)/.test(rawPath)) return next();
    function fail(status, detail) {
      if (res.headersSent) { res.destroy(); return; }
      res.writeHead(status, {"Content-Type": "application/json", "Cache-Control": "no-store"});
      res.end(JSON.stringify({detail}));
    }
    if (!isLocalRequest(req)) return fail(403, "Local development proxy only accepts same-origin loopback requests.");
    if (!METHODS.has(req.method)) return fail(405, "Unsupported development proxy method.");
    let incoming;
    try {
      incoming = new URL(rawPath, "http://localhost");
      if (!/^\/api\/v1(?:\/|$)/.test(incoming.pathname) || incoming.hash ||
          /[\\\u0000-\u001f]/.test(rawPath)) throw new Error("Invalid path");
    } catch (_) {
      return fail(400, "Invalid API path.");
    }
    const headers = {};
    for (const [key, value] of Object.entries(req.headers)) {
      if (REQUEST_HEADERS.has(key)) headers[key] = value;
    }
    // The socket always connects to the configured target. Keep the validated
    // localhost Host so backend-generated protected media URLs stay same-origin.
    headers.host = req.headers.host;
    const proxy = transport.request({
      protocol: upstream.protocol,
      hostname: upstream.hostname.replace(/^\[|\]$/g, ""),
      port: upstream.port || undefined,
      method: req.method,
      path: incoming.pathname + incoming.search,
      headers,
    }, response => {
      // Never follow or relay a redirect that could move a bearer-authenticated
      // browser request away from the configured origin.
      if (response.statusCode >= 300 && response.statusCode < 400 && response.statusCode !== 304) {
        response.resume();
        return fail(502, "The development API proxy does not allow redirects.");
      }
      const blockedHeaders = new Set(HOP_HEADERS);
      for (const name of String(response.headers.connection || "").split(",")) blockedHeaders.add(name.trim().toLowerCase());
      const responseHeaders = {};
      for (const [name, value] of Object.entries(response.headers)) {
        if (!blockedHeaders.has(name) && value !== undefined) responseHeaders[name] = value;
      }
      res.writeHead(response.statusCode || 502, responseHeaders);
      response.on("error", () => res.destroy());
      response.pipe(res);
    });
    proxy.setTimeout(timeoutMs, () => {
      fail(504, "The development API proxy timed out.");
      proxy.destroy();
    });
    proxy.on("error", () => fail(502, "The configured API server could not be reached."));
    req.on("aborted", () => proxy.destroy());
    res.on("close", () => { if (!res.writableEnded) proxy.destroy(); });
    req.pipe(proxy);
  };
}

module.exports = {createDevApiProxy};
