const {test, mock} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const {createUploadPreviewSource} = require("./mediaSource.cjs");
const uploadId = "id/../?other";
const originalUrl = "https://trusted.example/api/v1/uploads/id%2F..%2F%3Fother/content?file_id=file-1&source_object_id=object-1&source_object_version=1&expected_ciphertext_sha256=" + "a".repeat(64);
const args = {baseUrl: "https://trusted.example/api/v1", upload: {upload_id: uploadId, file_id: "file-1", file_url: originalUrl, has_file: true}, token: "synthetic+/?=&token", platform: "web", kind: "image", onDiagnostic: () => {}};
function stub(response) {
  const calls = [], revoked = [];
  mock.method(globalThis, "fetch", async (...values) => { calls.push(values); return response; });
  mock.method(URL, "createObjectURL", () => "blob:synthetic");
  mock.method(URL, "revokeObjectURL", (url) => revoked.push(url));
  return {calls, revoked};
}
function response(type="image/png", disposition="inline") {
  return {ok: true, headers: new Headers({"content-type": type, "content-disposition": disposition}),
    status: 200, arrayBuffer: async () => new TextEncoder().encode("synthetic").buffer};
}
test("web uses only trusted configured URL and Authorization, then disposable passive blob", async () => {
  const {calls, revoked} = stub(response());
  try {
    const request = createUploadPreviewSource(args);
    assert.deepEqual(await request.ready, {uri: "blob:synthetic"});
    assert.equal(calls[0][0], originalUrl);
    const options = calls[0][1];
    assert.equal(options.headers.Authorization, "Bearer " + args.token);
    assert.equal(options.redirect, "error");
    assert.equal(options.credentials, "omit");
    assert.equal(options.cache, "no-store");
    assert.ok(!calls[0][0].includes(args.token) && !calls[0][0].includes("access_token"));
    request.release(); request.release();
    assert.equal(options.signal.aborted, true);
    assert.deepEqual(revoked, ["blob:synthetic"]);
  } finally { mock.restoreAll(); }
});
for (const [type, disposition] of [["text/html","inline"], ["image/svg+xml","inline"], ["application/octet-stream","attachment"], ["image/png","attachment"], ["video/mp4","inline"]]) {
  test("web refuses active, unknown, attachment, or wrong media family: " + type + disposition, async () => {
    const {calls, revoked} = stub(response(type, disposition));
    try {
      const request = createUploadPreviewSource(args);
      await assert.rejects(request.ready);
      assert.equal(calls[0][1].signal.aborted, true);
      request.release();
      assert.deepEqual(revoked, []);
      assert.equal(URL.createObjectURL.mock.callCount(), 0);
    } finally { mock.restoreAll(); }
  });
}
test("late fetch after close/logout cannot create or publish a blob", async () => {
  let finish;
  const res = response();
  res.arrayBuffer = () => new Promise(resolve => { finish = resolve; });
  stub(res);
  try {
    const request = createUploadPreviewSource(args);
    await Promise.resolve(); await Promise.resolve();
    request.release();
    finish(new TextEncoder().encode("late").buffer);
    await assert.rejects(request.ready, {name: "AbortError"});
    assert.equal(URL.createObjectURL.mock.callCount(), 0);
  } finally { mock.restoreAll(); }
});
test("upload preview helper accepts the upload DTO shape only", async () => {
  const media = require("./mediaSource.cjs");
  assert.equal(typeof media.createUploadPreviewSource, "function");
  assert.equal(media.createMediaSource, undefined);
  const {calls} = stub(response());
  try {
    const citation = createUploadPreviewSource({...args, upload: {record_id: uploadId, original_url: originalUrl}});
    await assert.rejects(citation.ready);
    assert.equal(calls.length, 0);
  } finally { mock.restoreAll(); }
});
test("upload preview rejects invalid bound-source or JWT query URLs", async () => {
  const {calls} = stub(response());
  try {
    const invalidUploads = [
      {...args.upload, file_url: "https://trusted.example/api/v1/uploads/" + encodeURIComponent(uploadId) + "/content?file_id=file-1"},
      {...args.upload, file_url: originalUrl.replace("https://trusted.example", "https://other.example")},
      {...args.upload, file_url: originalUrl + "&access_token=jwt"},
      {...args.upload, file_id: "different-file"},
      {...args.upload, has_file: false},
    ];
    for (const upload of invalidUploads) {
      const request = createUploadPreviewSource({...args, upload});
      await assert.rejects(request.ready);
      request.release();
    }
    assert.equal(calls.length, 0);
  } finally { mock.restoreAll(); }
});
test("invalid credentials or configured URL fail before fetching", async () => {
  const {calls} = stub(response());
  try {
    for (const change of [{token: ""}, {baseUrl: "javascript:alert(1)"}, {baseUrl: "https://a:b@evil.example/api/v1"}, {baseUrl: "https://trusted.example/api/v1?access_token=x"}]) {
      const request = createUploadPreviewSource({...args, ...change});
      await assert.rejects(request.ready);
      request.release();
    }
    assert.equal(calls.length, 0);
  } finally { mock.restoreAll(); }
});
test("modular UI has no URL JWT or external uploaded-media navigation", () => {
  const collectSources = (directory) => fs.readdirSync(directory, {withFileTypes: true}).flatMap((entry) => {
    const filename = directory + "/" + entry.name;
    if (entry.isDirectory()) return collectSources(filename);
    return entry.name.endsWith(".js") && !entry.name.endsWith(".test.js") ? [filename] : [];
  });
  const source = [__dirname + "/App.js", ...collectSources(__dirname + "/src")]
    .map((filename) => fs.readFileSync(filename, "utf8")).join("\n");
  assert.ok(!source.includes("access_token="));
  assert.ok(!source.includes("Linking.openURL"));
  assert.ok(!source.includes("uri: previewUrl"));
  assert.ok(!source.includes("/ai/chat-demo"));
  assert.ok(!source.includes("/ai/demo-bootstrap"));
  assert.ok(!source.includes("fetchAIDemoChat"));
  assert.ok(!source.includes("bootstrapAIDemo"));
});

test("passive video blob remains usable until viewer disposal", async () => {
  const {revoked} = stub(response("video/mp4"));
  try {
    const request = createUploadPreviewSource({...args, kind: "video"});
    assert.deepEqual(await request.ready, {uri: "blob:synthetic"});
    assert.deepEqual(revoked, []);
    request.release();
    assert.deepEqual(revoked, ["blob:synthetic"]);
  } finally { mock.restoreAll(); }
});

for (const kind of ['image', 'video']) {
  test(`plain backend ${kind} content is fetched with header auth and never a JWT query`, async () => {
    const {calls} = stub(response(kind === 'image' ? 'image/png' : 'video/mp4'));
    const uri = 'https://trusted.example/api/v1/uploads/' + encodeURIComponent(uploadId) + '/content';
    try {
      const request = createUploadPreviewSource({...args, kind, upload: {...args.upload, file_url: uri}});
      assert.deepEqual(await request.ready, {uri: 'blob:synthetic'});
      assert.equal(calls[0][0], uri);
      assert.equal(calls[0][1].headers.Authorization, 'Bearer ' + args.token);
      request.release();
    } finally { mock.restoreAll(); }
  });
}

function nativeFiles() {
  const files = [];
  class File {
    constructor(cache, name) { this.uri = cache + name; this.exists = false; this.deletes = 0; files.push(this); }
    create() { this.exists = true; }
    write(bytes) { this.bytes = bytes; }
    delete() { this.exists = false; this.deletes++; }
  }
  return {files, nativeFile: {File, cache: "file:///cache/", randomUUID: () => "random"}};
}
for (const [kind, type, extension] of [["image", "image/png", "png"], ["video", "video/mp4", "mp4"]]) {
  test("native " + kind + " with NULL list MIME uses validated content and disposable local source", async () => {
    const {calls} = stub(response(type));
    const {files, nativeFile} = nativeFiles();
    const diagnostics = [];
    try {
      const request = createUploadPreviewSource({...args, platform: "android", kind,
        upload: {...args.upload, mime_type: null}, nativeFile, onDiagnostic: value => diagnostics.push(value)});
      assert.deepEqual(await request.ready, {uri: "file:///cache/protected-media-random." + extension});
      assert.equal(calls.length, 1);
      assert.equal(calls[0][1].headers.Authorization, "Bearer " + args.token);
      assert.ok(!calls[0][0].includes(args.token));
      assert.deepEqual([...files[0].bytes], [...new TextEncoder().encode("synthetic")]);
      assert.deepEqual(diagnostics.at(-1), {upload_id: uploadId, status: 200, media_type: type, byte_length: 9, source_kind: "temp file", event: "ready"});
      assert.ok(!JSON.stringify(diagnostics).includes(args.token));
      request.release(); request.release();
      assert.equal(files[0].deletes, 1);
    } finally { mock.restoreAll(); }
  });
}
for (const type of ["text/html", "image/svg+xml", "application/octet-stream", "video/unknown"]) {
  test("native rejects unsupported response " + type + " despite jpg filename and list MIME", async () => {
    stub(response(type));
    const {files, nativeFile} = nativeFiles();
    try {
      const request = createUploadPreviewSource({...args, platform: "android", nativeFile,
        upload: {...args.upload, mime_type: "image/jpeg", file_name: "photo.jpg"}});
      await assert.rejects(request.ready);
      assert.equal(files.length, 0);
    } finally { mock.restoreAll(); }
  });
}
test("native late body after close cannot materialize a plaintext file", async () => {
  let finish;
  const res = response(); res.arrayBuffer = () => new Promise(resolve => { finish = resolve; });
  stub(res);
  const {files, nativeFile} = nativeFiles();
  try {
    const request = createUploadPreviewSource({...args, platform: "android", nativeFile});
    await Promise.resolve(); await Promise.resolve();
    request.release(); finish(new Uint8Array([1]).buffer);
    await assert.rejects(request.ready, {name: "AbortError"});
    assert.equal(files.length, 0);
  } finally { mock.restoreAll(); }
});
test("partial native write failure deletes its plaintext file", async () => {
  stub(response());
  const {files, nativeFile} = nativeFiles();
  nativeFile.File.prototype.write = () => { throw new Error("disk full"); };
  try {
    const request = createUploadPreviewSource({...args, platform: "android", nativeFile});
    await assert.rejects(request.ready, /disk full/);
    assert.equal(files[0].deletes, 1);
  } finally { mock.restoreAll(); }
});
for (const change of [res => {res.ok = false; res.status = 401;}, res => res.headers.set("content-length", "10"), res => res.headers.set("content-length", "9e0"),
  res => res.headers.set("content-length", String(21 * 1024 * 1024)), res => {res.arrayBuffer = async () => new ArrayBuffer(0);}]) {
  test("failed status, truncated, oversized or empty content cannot produce a source", async () => {
    const res = response(); change(res); stub(res);
    try { await assert.rejects(createUploadPreviewSource(args).ready); }
    finally { mock.restoreAll(); }
  });
}

test("native attachment or mismatched media family is blocked", async () => {
  const {files, nativeFile} = nativeFiles();
  for (const res of [response("image/png", "attachment"), response("video/mp4")]) {
    stub(res);
    try { await assert.rejects(createUploadPreviewSource({...args, platform: "android", nativeFile}).ready); }
    finally { mock.restoreAll(); }
  }
  assert.equal(files.length, 0);
});
test("native cleanup failure does not throw through unmount and retries without secret diagnostics", async () => {
  stub(response());
  const {files, nativeFile} = nativeFiles(), diagnostics = [];
  try {
    const request = createUploadPreviewSource({...args, platform: "android", nativeFile, onDiagnostic: value => diagnostics.push(value)});
    await request.ready;
    const originalDelete = files[0].delete.bind(files[0]);
    files[0].delete = () => {throw new Error("file:///private/plaintext");};
    assert.doesNotThrow(() => request.release());
    assert.equal(diagnostics.at(-1).event, "cleanup_failed");
    assert.ok(!JSON.stringify(diagnostics).includes("file:///"));
    files[0].delete = originalDelete;
    request.release();
    assert.equal(files[0].deletes, 1);
  } finally { mock.restoreAll(); }
});

test("media HTTP 503 reports server unavailability without reading or decoding an error body", async () => {
  const res = response("application/json");
  res.ok = false; res.status = 503;
  let bodyReads = 0;
  res.arrayBuffer = async () => { bodyReads++; throw new Error("must not read a server error as media"); };
  const {calls} = stub(res);
  try {
    await assert.rejects(createUploadPreviewSource(args).ready, error => {
      assert.equal(error.status, 503);
      assert.match(error.message, /서버.*최신 상태를 확인/);
      return true;
    });
    assert.equal(calls.length, 1, "media errors are not automatically retried");
    assert.equal(bodyReads, 0);
    assert.equal(URL.createObjectURL.mock.callCount(), 0);
  } finally { mock.restoreAll(); }
});

for (const status of [401, 404]) test(`media HTTP ${status} retains generic protected-media failure`, async () => {
  const res = response("application/json");
  res.ok = false; res.status = status;
  stub(res);
  try {
    await assert.rejects(createUploadPreviewSource(args).ready, error => {
      assert.equal(error.message, "This file cannot be displayed safely");
      assert.notEqual(error.status, 503);
      return true;
    });
  } finally { mock.restoreAll(); }
});
