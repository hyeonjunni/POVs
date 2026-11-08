const { test, mock } = require("node:test");
const assert = require("node:assert/strict");
const { pickVoiceAsset, normalizeVoiceAsset, releaseVoiceAsset } = require("./mediaPicker.cjs");
const { MAX_UPLOAD_BYTES, createUploadWithBinary, createUploadsApi } = require("./uploads.cjs");
const { createUploadPreviewSource } = require("./mediaSource.cjs");

test("voice picker maps a cached document to the existing binary upload asset", async () => {
  const asset = await pickVoiceAsset({
    documentPicker: { getDocumentAsync: async options => {
      assert.equal(options.multiple, false);
      assert.equal(options.copyToCacheDirectory, true);
      assert.equal(options.base64, false);
      assert.deepEqual(options.type, ["audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav"]);
      return { canceled: false, assets: [{ uri: "file:///cache/memory.wav", name: "memory.wav", size: 100, mimeType: "audio/x-wav" }] };
    } },
    onError: () => assert.fail("unexpected picker failure"),
  });
  assert.equal(asset.fileName, "memory.wav");
  assert.equal(asset.fileSize, 100);
  assert.equal(asset.mimeType, "audio/wav");
  assert.equal(asset.uri, "file:///cache/memory.wav");
});

for (const result of [{ canceled: true }, { canceled: false, assets: [] }]) {
  test("voice picker cancellation does not create a record or report failure: " + JSON.stringify(result), async () => {
    assert.equal(await pickVoiceAsset({ documentPicker: { getDocumentAsync: async () => result }, onError: () => assert.fail("cancellation is not an error") }), null);
  });
}

for (const [name, mimeType, expected] of [["story.MP3", "", "audio/mpeg"], ["story.mp3", "audio/mp3", "audio/mpeg"], ["story.wav", "audio/wave", "audio/wav"], ["story.wav", "application/octet-stream", "audio/wav"]]) {
  test("voice MIME normalization supports " + name + " " + mimeType, () => {
    assert.equal(normalizeVoiceAsset({ uri: "content://provider/123", name, mimeType, size: 5 }).mimeType, expected);
  });
}

for (const [name, mimeType] of [["memo.m4a", "audio/mp4"], ["memo.ogg", "audio/ogg"], ["memo.aac", "audio/aac"], ["memo.mp3", "video/mp4"], ["memo.wav", "audio/mpeg"], ["memo", "audio/mpeg"]]) {
  test("unsupported or mismatched voice format fails before metadata creation: " + name + " " + mimeType, () => {
    assert.throws(() => normalizeVoiceAsset({ uri: "file:///memo", name, mimeType, size: 5 }), /MP3.*WAV/);
  });
}

for (const size of [undefined, 0, -1, 1.5, MAX_UPLOAD_BYTES + 1]) {
  test("voice requires nonempty measured bytes within the 20 MiB transport limit: " + size, () => {
    assert.throws(() => normalizeVoiceAsset({ uri: "file:///memo.mp3", name: "memo.mp3", mimeType: "audio/mpeg", size }), /크기|20 MiB/);
  });
}

test("invalid web voice selection releases only its object URL and reports a recoverable error", async () => {
  const revoked = [];
  mock.method(URL, "revokeObjectURL", url => revoked.push(url));
  try {
    const errors = [];
    const selected = { uri: "blob:invalid-audio", name: "memo.m4a", mimeType: "audio/mp4", size: 10, file: new Blob(["data"]) };
    assert.equal(await pickVoiceAsset({ documentPicker: { getDocumentAsync: async () => ({ canceled: false, assets: [selected] }) }, onError: error => errors.push(error) }), null);
    assert.deepEqual(revoked, [selected.uri]);
    assert.equal(errors.length, 1);
  } finally { mock.restoreAll(); }
});

test("picker failure is recoverable and a later voice selection succeeds", async () => {
  let fail = true;
  const errors = [];
  const args = { documentPicker: { getDocumentAsync: async () => {
    if (fail) throw new Error("unavailable");
    return { canceled: false, assets: [{ uri: "content://provider/123", name: "memo.mp3", mimeType: "audio/mpeg", size: 10 }] };
  } }, onError: error => errors.push(error) };
  assert.equal(await pickVoiceAsset(args), null);
  assert.equal(errors.length, 1);
  fail = false;
  assert.equal((await pickVoiceAsset(args)).mimeType, "audio/mpeg");
});

test("voice selection cleanup never deletes a native source", () => {
  const revoked = [];
  releaseVoiceAsset({ uri: "content://user/memo.mp3" }, url => revoked.push(url));
  releaseVoiceAsset({ uri: "file:///cache/memo.mp3" }, url => revoked.push(url));
  releaseVoiceAsset({ uri: "blob:selected", file: new Blob(["a"]) }, url => revoked.push(url));
  assert.deepEqual(revoked, ["blob:selected"]);
});

test("web voice uploads one actual File and requires confirmed binary metadata", async () => {
  const parts = [], calls = [];
  const file = new Blob(["sound"], { type: "audio/mpeg" });
  const asset = normalizeVoiceAsset({ uri: "blob:selected", name: "memo.mp3", mimeType: "audio/mpeg", size: 5, file });
  const uploads = createUploadsApi({ post: async (path, options) => {
    calls.push([path, options]);
    return path === "/uploads" ? { upload_id: "voice-1", has_file: false } : { upload_id: "voice-1", has_file: true, file_id: "voice-file" };
  } }, () => ({ append: (...args) => parts.push(args) }));
  const result = await createUploadWithBinary(uploads, { room_id: "room-1", type: "voice", title: "memo" }, asset);
  assert.equal(result.uploadedFile.file_id, "voice-file");
  assert.equal(calls[0][1].json.type, "voice");
  assert.deepEqual(parts, [["file", file, "memo.mp3"]]);
});

const preview = {
  baseUrl: "https://trusted.example/api/v1", token: "test-secret", platform: "web", kind: "voice", onDiagnostic: () => {},
  upload: { upload_id: "voice-1", file_id: "f1", has_file: true, file_url: "https://trusted.example/api/v1/uploads/voice-1/content" },
};
function mediaResponse(type) {
  return { ok: true, status: 200, headers: new Headers({ "content-type": type, "content-disposition": "inline" }), arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer };
}

for (const [type, extension] of [["audio/mpeg", "mp3"], ["audio/wav", "wav"]]) {
  test("protected " + type + " uses Authorization and its blob lasts until the viewer closes", async () => {
    const revoked = [];
    mock.method(URL, "createObjectURL", blob => { assert.equal(blob.type, type); return "blob:protected-audio"; });
    mock.method(URL, "revokeObjectURL", url => revoked.push(url));
    try {
      let requestOptions;
      const request = createUploadPreviewSource({ ...preview, fetchImpl: async (url, options) => {
        assert.equal(url, preview.upload.file_url);
        assert.equal(options.headers.Authorization, "Bearer test-secret");
        requestOptions = options;
        return mediaResponse(type);
      } });
      assert.deepEqual(await request.ready, { uri: "blob:protected-audio" });
      assert.deepEqual(revoked, []);
      request.release(); request.release();
      assert.equal(requestOptions.signal.aborted, true);
      assert.deepEqual(revoked, ["blob:protected-audio"]);
    } finally { mock.restoreAll(); }
  });

  test("native " + type + " receives an allowlisted cache extension and is deleted on close", async () => {
    const files = [];
    class File {
      constructor(cache, name) { this.uri = cache + name; this.exists = false; this.deletes = 0; files.push(this); }
      create() { this.exists = true; }
      write(bytes) { assert.equal(bytes.length, 3); }
      delete() { this.exists = false; this.deletes++; }
    }
    const request = createUploadPreviewSource({ ...preview, platform: "android", fetchImpl: async () => mediaResponse(type), nativeFile: { File, cache: "file:///cache/", randomUUID: () => "temporary" } });
    assert.deepEqual(await request.ready, { uri: "file:///cache/protected-media-temporary." + extension });
    request.release(); request.release();
    assert.equal(files[0].deletes, 1);
  });
}

for (const type of ["text/html", "application/octet-stream", "audio/mp4", "audio/ogg", "video/mp4", "image/png"]) {
  test("voice viewer rejects a response outside its audio allowlist: " + type, async () => {
    const request = createUploadPreviewSource({ ...preview, fetchImpl: async () => mediaResponse(type) });
    await assert.rejects(request.ready);
  });
}

test("late voice response after logout cannot publish a playable blob", async () => {
  let finish;
  const response = mediaResponse("audio/mpeg");
  response.arrayBuffer = () => new Promise(resolve => { finish = resolve; });
  const request = createUploadPreviewSource({ ...preview, fetchImpl: async () => response });
  await Promise.resolve(); await Promise.resolve();
  request.release();
  finish(new Uint8Array([1]).buffer);
  await assert.rejects(request.ready, { name: "AbortError" });
});
