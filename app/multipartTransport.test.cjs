const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const babel = require('@babel/core');
const {createApiClient} = require('./apiClient.cjs');
const {createUploadsApi, createUploadWithBinary} = require('./uploads.cjs');

// Execute the installed Expo serializer, not a test reimplementation of its wire format.
const filename = require.resolve('expo/src/winter/fetch/convertFormData.ts');
const compiled = babel.transformSync(fs.readFileSync(filename, 'utf8'), {
  filename, babelrc: false, configFile: false,
  plugins: ['@babel/plugin-transform-typescript', '@babel/plugin-transform-modules-commonjs'],
}).code;
const serializer = {exports: {}};
new Function('module', 'exports', 'require', compiled)(serializer, serializer.exports,
  () => ({blobToArrayBufferAsync: blob => blob.arrayBuffer()}));

function loadInstalled(name, syntaxPlugin) {
  const filename = require.resolve(name);
  const code = babel.transformSync(fs.readFileSync(filename, 'utf8'), {
    filename, babelrc: false, configFile: false,
    plugins: [syntaxPlugin, '@babel/plugin-transform-modules-commonjs'],
  }).code;
  const module = {exports: {}};
  new Function('module', 'exports', code)(module, module.exports);
  return module.exports;
}
const NativeFormData = loadInstalled('react-native/Libraries/Network/FormData.js', '@babel/plugin-transform-flow-strip-types').default;
loadInstalled('expo/src/winter/FormData.ts', '@babel/plugin-transform-typescript').installFormDataPatch(NativeFormData);

for (const uri of ['content://media/external/7', 'file:///cache/photo.png']) {
  test(`Android image wire contract, retry and retrieval: ${uri.split(':')[0]}`, async () => {
    const {createMultipartTransport} = require('./multipartTransport.cjs');
    const bytes = new Uint8Array([137, 80, 78, 71]);
    const calls = [], reads = [];
    let binaryAttempts = 0;
    const upload = {upload_id: 'u1', has_file: false};
    const confirmed = {...upload, has_file: true, file_id: 'f1', file_url: '/uploads/u1/content'};
    const response = (status, value) => ({ok: status < 400, status, text: async () => JSON.stringify(value)});
    const fetchImpl = createMultipartTransport({
      readUriBytes: async value => { reads.push(value); return bytes; },
      createFormData: () => new NativeFormData(),
      fetchImpl: async (url, options) => {
        calls.push({url, options});
        if (url.endsWith('/content')) return response(200, {retrieved: true});
        return response(200, options.method === 'POST' ? upload : [upload]);
      },
      multipartFetch: async (url, options) => {
        calls.push({url, options});
        assert.equal(options.headers.Authorization, 'Bearer synthetic');
        assert.equal(Object.keys(options.headers).some(key => key.toLowerCase() === 'content-type'), false);
        assert.ok(options.signal instanceof AbortSignal);
        const {body, boundary} = await serializer.exports.convertFormDataAsync(options.body);
        assert.ok(boundary.length >= 1 && boundary.length <= 70);
        const prefix = Buffer.from(`--${boundary}\r\ncontent-disposition: form-data; name="file"; filename="upload.png"\r\ncontent-type: image/png\r\n\r\n`);
        assert.deepEqual(Buffer.from(body), Buffer.concat([prefix, Buffer.from(bytes), Buffer.from(`\r\n--${boundary}--\r\n`)]));
        binaryAttempts++;
        return binaryAttempts === 1 ? response(400, {detail: 'Invalid multipart upload'}) : response(200, confirmed);
      },
    });
    const client = createApiClient({baseUrl: 'https://backend.example', getAccessToken: () => 'synthetic', fetchImpl});
    const api = createUploadsApi(client, () => new NativeFormData());
    const asset = {uri, fileName: null, mimeType: 'IMAGE/PNG', fileSize: bytes.length};
    // Use a missing name with a non-filename URI for both scheme cases.
    asset.fileName = 'upload';
    let failure;
    await assert.rejects(createUploadWithBinary(api, {room_id: 'room'}, asset), error => {
      failure = error;
      return error.status === 400 && error.uploadStage === 'binary'
        && error.upload.upload_id === 'u1' && !error.upload.has_file;
    });
    const result = await createUploadWithBinary(api, {room_id: 'room'}, asset, {upload: failure.upload});
    assert.equal(result.uploadedFile.file_id, 'f1');
    assert.deepEqual(await client.get(result.uploadedFile.file_url), {retrieved: true});
    assert.equal(calls.filter(call => call.url.endsWith('/uploads')).length, 1);
    assert.deepEqual(reads, [uri, uri]);
  });
}

test('browser FormData and JSON retain the existing transport', async () => {
  const {createMultipartTransport} = require('./multipartTransport.cjs');
  const calls = [];
  const fetch = createMultipartTransport({fetchImpl: async (...args) => calls.push(args),
    multipartFetch: () => assert.fail('native transport used for browser or JSON'),
    readUriBytes: () => assert.fail('unexpected native read')});
  const form = new FormData();
  form.append('file', new Blob(['data'], {type: 'image/png'}), 'photo.png');
  await fetch('/binary', {body: form});
  await fetch('/uploads', {body: '{}', headers: {'Content-Type': 'application/json'}});
  assert.equal(calls[0][1].body, form);
  assert.equal(calls[1][1].headers['Content-Type'], 'application/json');
});

for (const fail of [null, 'copy', 'read']) {
  test(`content URI is copied through ContentResolver and temporary file is removed (${fail})`, async () => {
    const {readUploadBytes} = require('./multipartTransport.cjs');
    const calls = [];
    class File {
      constructor(...paths) { this.uri = paths.join('/'); this.exists = true; }
      async bytes() { calls.push('read'); if (fail === 'read') throw new Error('read'); return new Uint8Array([1, 2]); }
      delete() { calls.push('delete'); }
    }
    const pending = readUploadBytes('content://media/external/7', {File, cache: 'file:///cache',
      randomUUID: () => 'unique', copyAsync: async options => {
        assert.deepEqual(options, {from: 'content://media/external/7', to: 'file:///cache/multipart-unique'});
        calls.push('copy'); if (fail === 'copy') throw new Error('copy');
      }});
    if (fail) await assert.rejects(pending, new RegExp(fail));
    else assert.deepEqual(await pending, new Uint8Array([1, 2]));
    assert.deepEqual(calls, fail === 'copy' ? ['copy', 'delete'] : ['copy', 'read', 'delete']);
  });
}
