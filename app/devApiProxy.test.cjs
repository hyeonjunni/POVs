const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const {createDevApiProxy} = require("./devApiProxy.cjs");

async function listen(handler, t) {
  const server = http.createServer(handler);
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => { server.closeAllConnections(); server.close(resolve); }));
  return `http://127.0.0.1:${server.address().port}`;
}
async function fixture(t, handler, options = {}) {
  const upstream = await listen(handler, t);
  const proxy = createDevApiProxy({target: upstream + "/api/v1", ...options});
  const local = await listen((req, res) => proxy(req, res, () => { res.writeHead(404); res.end("metro"); }), t);
  return {local, upstream};
}

test("proxy is opt-in and rejects credentials, query and non-API target paths", () => {
  assert.equal(createDevApiProxy(), null);
  for (const target of ["ftp://localhost/api/v1", "http://user:pw@localhost/api/v1", "http://localhost/api/v1?x=1", "http://localhost/elsewhere"]) {
    assert.throws(() => createDevApiProxy({target}));
  }
});

test("same-origin requests preserve auth, version, idempotency, bytes, query, status and ETag", async t => {
  let seen;
  const {local} = await fixture(t, (req, res) => {
    const chunks = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => {
      seen = {method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks)};
      res.writeHead(201, {"Content-Type": "application/octet-stream", ETag: '"6"'});
      res.end(Buffer.from([0, 250, 9, 255]));
    });
  });
  const data = Buffer.from([0, 1, 255, 7]);
  const result = await fetch(local + "/api/v1/uploads/one/binary?test=1", {
    method: "POST", headers: {Origin: local, Authorization: "Bearer fixture-only", "If-Match": '"5"',
      "Idempotency-Key": "fixture-key", "Content-Type": "application/octet-stream", "X-Forwarded-Host": "evil.example"}, body: data,
  });
  assert.equal(result.status, 201);
  assert.equal(result.headers.get("etag"), '"6"');
  assert.deepEqual(Buffer.from(await result.arrayBuffer()), Buffer.from([0, 250, 9, 255]));
  assert.equal(seen.url, "/api/v1/uploads/one/binary?test=1");
  assert.equal(seen.headers.authorization, "Bearer fixture-only");
  assert.equal(seen.headers["if-match"], '"5"');
  assert.equal(seen.headers["idempotency-key"], "fixture-key");
  assert.equal(seen.headers.host, new URL(local).host);
  assert.equal(seen.headers.origin, undefined);
  assert.equal(seen.headers["x-forwarded-host"], undefined);
  assert.deepEqual(seen.body, data);
});

test("same-origin media requests preserve range and partial response", async t => {
  const {local} = await fixture(t, (req, res) => {
    assert.equal(req.headers.range, "bytes=0-3");
    res.writeHead(206, {"Content-Type": "video/mp4", "Content-Range": "bytes 0-3/10"});
    res.end("data");
  });
  const result = await fetch(local + "/api/v1/uploads/one/content", {headers: {Range: "bytes=0-3"}});
  assert.equal(result.status, 206);
  assert.equal(result.headers.get("content-range"), "bytes 0-3/10");
  assert.equal(await result.text(), "data");
});

test("cross-origin and untrusted Host requests never reach the upstream", async t => {
  let calls = 0;
  const {local} = await fixture(t, (_req, res) => { calls++; res.end("unexpected"); });
  for (const headers of [{Origin: "https://evil.example"}, {Origin: "null"}, {Host: "evil.example"}, {"Sec-Fetch-Site": "cross-site"}]) {
    const status = await new Promise((resolve, reject) => {
      http.get(local + "/api/v1/auth/me", {headers}, response => {
        response.resume();
        response.on("end", () => resolve(response.statusCode));
      }).on("error", reject);
    });
    assert.equal(status, 403);
  }
  assert.equal(calls, 0);
});

test("non-API paths continue to Metro without contacting the upstream", async t => {
  let calls = 0;
  const {local} = await fixture(t, (_req, res) => { calls++; res.end("unexpected"); });
  for (const path of ["/", "/index.bundle", "/api/v10/auth/me"]) {
    const result = await fetch(local + path);
    assert.equal(result.status, 404);
    assert.equal(await result.text(), "metro");
  }
  assert.equal(calls, 0);
});

test("upstream authorization failures stay failures", async t => {
  const {local} = await fixture(t, (_req, res) => {
    res.writeHead(403, {"Content-Type": "application/json"});
    res.end(JSON.stringify({detail: "fixture access denied"}));
  });
  const result = await fetch(local + "/api/v1/records/private");
  assert.equal(result.status, 403);
  assert.deepEqual(await result.json(), {detail: "fixture access denied"});
});

test("redirects are rejected without forwarding Location or following them", async t => {
  let calls = 0;
  const {local} = await fixture(t, (_req, res) => { calls++; res.writeHead(307, {Location: "https://evil.example"}); res.end(); });
  const result = await fetch(local + "/api/v1/auth/me");
  assert.equal(result.status, 502);
  assert.equal(result.headers.get("location"), null);
  assert.equal(calls, 1);
});

test("unresponsive upstream times out without exposing request details", async t => {
  const {local} = await fixture(t, () => {}, {timeoutMs: 30});
  const result = await fetch(local + "/api/v1/auth/me", {headers: {Authorization: "Bearer fixture-only"}});
  assert.equal(result.status, 504);
  assert.deepEqual(await result.json(), {detail: "The development API proxy timed out."});
});
