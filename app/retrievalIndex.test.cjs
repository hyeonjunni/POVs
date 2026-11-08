const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createApiClient } = require('./apiClient.cjs');
const { createProcessingApi } = require('./processing.cjs');

for (const status of [200, 201, 202]) {
  test(`index requires HTTP 200, got ${status}, uses upload_id URL and original job body`, async () => {
    let request;
    const client = createApiClient({ baseUrl: 'http://localhost/api/v1', getAccessToken: () => 'session-token', fetchImpl: async (url, options) => {
      request = { url, options };
      return { ok: true, status, text: async () => JSON.stringify({ chunk_count: 2, embedding_count: 2 }) };
    } });
    const api = createProcessingApi(client);
    const pending = api.index('upload/1', 'job-original');
    if (status === 200) assert.equal((await pending).chunk_count, 2);
    else await assert.rejects(pending);
    assert.equal(request.url, 'http://localhost/api/v1/records/upload%2F1/retrieval-index');
    assert.equal(request.url.includes('file-1'), false);
    assert.equal(request.options.body, JSON.stringify({ processing_job_id: 'job-original' }));
    assert.equal(request.options.headers.Authorization, 'Bearer session-token');
  });
}

test('index deadline bounds an ambiguous publication', async () => {
  let requestSignal, calls = 0;
  const api = createProcessingApi({ post: (_path, options) => {
    calls++; requestSignal = options.signal;
    return new Promise(() => {});
  } });
  await assert.rejects(api.index('upload-1', 'job-original', {timeoutMs: 5}), { code: 'REQUEST_TIMEOUT' });
  assert.equal(requestSignal.aborted, true);
  assert.equal(calls, 1, 'an ambiguous index POST must not be retried automatically');
});

test('index can finish after the ordinary API deadline without duplicate publication', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  let calls = 0, requestSignal, settled = false;
  const api = createProcessingApi({post: (_path, options) => {
    calls++; requestSignal = options.signal;
    return new Promise(resolve => setTimeout(() => resolve({chunk_count: 1, embedding_count: 1}), 20000));
  }});
  const pending = api.index('upload-1', 'job-original');
  pending.then(() => { settled = true; });
  t.mock.timers.tick(15001);
  await Promise.resolve();
  assert.equal(settled, false);
  assert.equal(requestSignal.aborted, false);
  t.mock.timers.tick(4999);
  assert.deepEqual(await pending, {chunk_count: 1, embedding_count: 1});
  assert.equal(calls, 1);
});

test('index default deadline is bounded at 120 seconds', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  let requestSignal;
  const api = createProcessingApi({post: (_path, options) => {
    requestSignal = options.signal;
    return new Promise(() => {});
  }});
  const pending = api.index('upload-1', 'job-original');
  const rejected = assert.rejects(pending, {code: 'REQUEST_TIMEOUT'});
  t.mock.timers.tick(119999);
  assert.equal(requestSignal.aborted, false);
  t.mock.timers.tick(1);
  await rejected;
  assert.equal(requestSignal.aborted, true);
});

test('index honors explicit cancellation without making a second request', async () => {
  let calls = 0, requestSignal;
  const api = createProcessingApi({post: (_path, options) => {
    calls++; requestSignal = options.signal;
    return new Promise(() => {});
  }});
  const controller = new AbortController();
  const pending = api.index('upload-1', 'job-original', {signal: controller.signal});
  controller.abort();
  await assert.rejects(pending, {name: 'AbortError'});
  assert.equal(requestSignal.aborted, true);
  assert.equal(calls, 1);
});
