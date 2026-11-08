const { test } = require("node:test");
const assert = require("node:assert/strict");
const { MAX_UPLOAD_BYTES, createUploadWithBinary, createUploadsApi, validateUploadAsset } = require("./uploads.cjs");

test("creates exact upload metadata then sends one multipart file field", async () => {
  const calls = [];
  const parts = [];
  const client = {
    post: async (...args) => { calls.push(args); return { upload_id: "upload-1", file_id: "file-1" }; },
    delete: async (...args) => { calls.push(args); return { ok: true }; },
  };
  const uploads = createUploadsApi(client, () => ({ append: (...args) => parts.push(args) }));
  const metadata = { room_id: "room-1", type: "image", title: "photo.jpg", description: null, tags: ["family"] };
  const asset = { uri: "file:///photo.jpg", fileName: "photo.jpg", mimeType: "image/jpeg", fileSize: 1234 };

  await uploads.create(metadata);
  await uploads.uploadBinary("upload-1", asset);
  await uploads.remove("upload/1");

  for (const [, options] of calls.slice(0, 2)) { assert.ok(options.signal instanceof AbortSignal); delete options.signal; }
  assert.deepEqual(calls[0], ["/uploads", { json: metadata }]);
  assert.equal(calls[1][0], "/uploads/upload-1/binary");
  assert.deepEqual(Object.keys(calls[1][1]), ["body"]);
  assert.deepEqual(parts, [["file", { uri: asset.uri, name: asset.fileName, type: asset.mimeType }]]);
  assert.deepEqual(calls[2], ["/uploads/upload%2F1"]);
});

test("rejects media over 20 MiB before sending it", async () => {
  let sent = false;
  const uploads = createUploadsApi({ post: async () => { sent = true; } });
  await assert.rejects(
    uploads.uploadBinary("upload-1", { uri: "file:///huge.jpg", fileSize: MAX_UPLOAD_BYTES + 1 }),
    /20 MiB/
  );
  assert.equal(sent, false);
});

test("requires a known acceptable size before metadata creation", () => {
  assert.throws(() => validateUploadAsset({ uri: "file:///unknown.jpg" }), /크기/);
  assert.throws(() => validateUploadAsset({ uri: "file:///huge.jpg", fileSize: MAX_UPLOAD_BYTES + 1 }), /20 MiB/);
  assert.doesNotThrow(() => validateUploadAsset({ uri: "file:///ok.jpg", fileSize: MAX_UPLOAD_BYTES }));
});

test("validates the binary before creating upload metadata", async () => {
  const calls = [];
  const uploads = {
    create: async () => { calls.push("create"); return { upload_id: "upload-1" }; },
    uploadBinary: async () => { calls.push("binary"); return { upload_id: "upload-1", has_file: true, file_id: "file-1" }; },
  };
  await assert.rejects(
    createUploadWithBinary(uploads, { room_id: "room-1" }, { uri: "file:///unknown.jpg" }),
    /크기/
  );
  assert.deepEqual(calls, []);

  await createUploadWithBinary(
    uploads,
    { room_id: "room-1" },
    { uri: "file:///ok.jpg", fileSize: 123, fileName: "ok.jpg", mimeType: "image/jpeg" }
  );
  assert.deepEqual(calls, ["create", "binary"]);
});

for (const [uri, mimeType, name] of [["content://media/123", "image/jpeg", "upload.jpg"], ["file:///cache/clip.mp4", "video/mp4", "clip.mp4"]]) {
  test(`normalizes missing picker filename for ${mimeType} without rewriting URI`, async () => {
    const { normalizeUploadAsset } = require("./uploads.cjs");
    const asset = normalizeUploadAsset({uri, mimeType, fileSize: 42});
    assert.equal(asset.uri, uri);
    assert.equal(asset.fileName, name);
    assert.equal(asset.mimeType, mimeType);
  });
}
test("derives supported MIME from filename and rejects unsupported Android asset URI before metadata", async () => {
  const { normalizeUploadAsset } = require("./uploads.cjs");
  assert.equal(normalizeUploadAsset({uri: "content://media/5", fileName: "photo.PNG", fileSize: 1}).mimeType, "image/png");
  assert.throws(() => normalizeUploadAsset({uri: "asset://photo", fileName: "photo.png", fileSize: 1}), /다시 선택/);
  assert.throws(() => normalizeUploadAsset({uri: "file:///cache/movie.mp4", mimeType: "video/quicktime", fileSize: 1}), /형식/);
});
test("binary failure preserves stage and metadata identity without claiming success", async () => {
  const failure = Object.assign(new Error("Invalid media upload"), {status: 400});
  await assert.rejects(createUploadWithBinary({
    create: async () => ({upload_id: "orphan", has_file: false}),
    uploadBinary: async () => { throw failure; },
  }, {type: "image"}, {uri: "file:///photo.jpg", fileSize: 1}), error => {
    assert.equal(error.uploadStage, "binary");
    assert.equal(error.uploadId, "orphan");
    assert.equal(error.status, 400);
    return true;
  });
});

test("content URI binary request carries one multipart file and JWT only in Authorization", async () => {
  const {createApiClient} = require('./apiClient.cjs');
  const requests = [], parts = [];
  const client = createApiClient({baseUrl: 'https://backend.example/api/v1', getAccessToken: () => 'secret', fetchImpl: async (url, options) => {
    requests.push({url, options});
    const dto = requests.length === 1 ? {upload_id: 'u1', has_file: false} : {upload_id: 'u1', has_file: true, file_id: 'f1'};
    return {ok: true, status: 200, text: async () => JSON.stringify(dto)};
  }});
  const form = {append: (...part) => parts.push(part)};
  const api = createUploadsApi(client, () => form);
  const result = await createUploadWithBinary(api, {type: 'video', room_id: 'room'}, {uri: 'content://media/external/5', mimeType: 'video/mp4', fileSize: 100});
  assert.equal(result.uploadedFile.has_file, true);
  assert.equal(requests[1].url, 'https://backend.example/api/v1/uploads/u1/binary');
  assert.equal(requests[1].options.headers.Authorization, 'Bearer secret');
  assert.equal(requests[1].options.headers['Content-Type'], undefined);
  assert.equal(requests[1].options.body, form);
  assert.deepEqual(parts, [['file', {uri: 'content://media/external/5', name: 'upload.mp4', type: 'video/mp4'}]]);
});
test('direct binary upload normalizes nullable picker metadata', async () => {
  const parts = [];
  const api = createUploadsApi({post: async () => ({has_file: true})}, () => ({append: (...part) => parts.push(part)}));
  await api.uploadBinary('u1', {uri: 'file:///photo.png', fileSize: 1});
  assert.deepEqual(parts[0], ['file', {uri: 'file:///photo.png', name: 'photo.png', type: 'image/png'}]);
});

test('web picker File is appended as a real file rather than a native URI object', async () => {
  const parts = [], file = new Blob(['data'], {type: 'image/png'});
  const api = createUploadsApi({post: async () => ({has_file: true})}, () => ({append: (...part) => parts.push(part)}));
  await api.uploadBinary('u1', {uri: 'blob:https://app/photo', fileSize: 4, mimeType: 'image/png', fileName: 'photo.png', file});
  assert.deepEqual(parts[0], ['file', file, 'photo.png']);
});

for (const type of ['image', 'video']) {
  test(`${type} success requires confirmed binary file metadata`, async () => {
    const calls = [];
    const asset = {uri: type === 'image' ? 'content://media/7' : 'file:///cache/video.mp4', mimeType: type === 'image' ? 'image/jpeg' : 'video/mp4', fileSize: 10};
    const api = {create: async () => {calls.push('metadata'); return {upload_id: 'u'};}, uploadBinary: async () => {calls.push('binary'); return {upload_id: 'u', has_file: true, file_id: 'f'};}};
    assert.equal((await createUploadWithBinary(api, {type}, asset)).uploadedFile.file_id, 'f');
    assert.deepEqual(calls, ['metadata', 'binary']);
    api.uploadBinary = async () => ({upload_id: 'u', status: 'uploaded', has_file: false});
    await assert.rejects(createUploadWithBinary(api, {type}, asset), error => error.code === 'BINARY_NOT_CONFIRMED' && error.uploadId === 'u');
  });
}

for (const alreadyStored of [false, true]) {
  test(`safe retry reuses metadata and reconciles server file first (${alreadyStored})`, async () => {
    let creates = 0, binaries = 0;
    const upload = {upload_id: 'u', has_file: false};
    const confirmed = {...upload, has_file: true, file_id: 'f'};
    const api = {
      create: async () => {creates++; return upload;},
      list: async room => {assert.equal(room, 'room'); return [alreadyStored ? confirmed : upload];},
      uploadBinary: async id => {assert.equal(id, 'u'); binaries++; if (binaries === 1) throw new Error('connection lost'); return confirmed;},
    };
    const asset = {uri: 'content://media/1', mimeType: 'image/png', fileSize: 10};
    await assert.rejects(createUploadWithBinary(api, {room_id: 'room'}, asset));
    const result = await createUploadWithBinary(api, {room_id: 'room'}, asset, {upload});
    assert.equal(result.uploadedFile.file_id, 'f');
    assert.equal(creates, 1);
    assert.equal(binaries, alreadyStored ? 1 : 2);
  });
}

test('retry does not send binary when reconciliation fails or metadata disappeared', async () => {
  const asset = {uri: 'file:///photo.jpg', fileSize: 10};
  for (const list of [async () => {throw new Error('offline');}, async () => []]) {
    await assert.rejects(createUploadWithBinary({list, create: () => assert.fail('duplicate metadata'), uploadBinary: () => assert.fail('unsafe retry')}, {room_id: 'room'}, asset, {upload: {upload_id: 'u'}}));
  }
});

test('scope change after metadata prevents binary dispatch under another session', async () => {
  let current = true;
  await assert.rejects(createUploadWithBinary({create: async () => {current = false; return {upload_id: 'u'};}, uploadBinary: () => assert.fail('stale dispatch')}, {}, {uri: 'file:///photo.jpg', fileSize: 10}, {assertCurrent: () => {if (!current) throw new Error('stale scope');}}), /stale scope/);
});
