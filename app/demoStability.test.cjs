const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createProcessingApi, createOcrReviewFlow } = require('./processing.cjs');
const { createUploadsApi } = require('./uploads.cjs');

for (const operation of ['create', 'getReview', 'review']) {
  test(`OCR ${operation} deadline releases a waiting caller`, async () => {
    const api = createProcessingApi({ get: () => new Promise(() => {}), post: () => new Promise(() => {}) }, { timeoutMs: 5 });
    let loading = true;
    const pending = api[operation]('job-1', operation === 'review' ? 'approve' : {});
    await assert.rejects(pending.finally(() => { loading = false; }), { code: 'REQUEST_TIMEOUT' });
    assert.equal(loading, false);
  });
}
for (const operation of ['create', 'list', 'uploadBinary']) {
  test(`upload ${operation} has a bounded deadline`, async () => {
    const api = createUploadsApi({ get: () => new Promise(() => {}), post: () => new Promise(() => {}) }, () => ({ append() {} }), { timeoutMs: 5 });
    await assert.rejects(api[operation]('upload-1', { fileSize: 1, uri: 'file:///a.jpg' }), { code: 'REQUEST_TIMEOUT' });
  });
}
test('review timeout recovery reloads same job before any further write', async () => {
  const calls = [];
  const flow = createOcrReviewFlow({
    getReview: async id => { calls.push(['get', id]); return { review_status: 'confirmed' }; },
    review: async id => { calls.push(['post', id]); throw Object.assign(new Error('uncertain'), { code: 'REQUEST_TIMEOUT' }); },
  });
  flow.setContext({ job_id: 'existing-job' });
  await assert.rejects(flow.decide('approve'));
  const recovered = await flow.decide('reject');
  assert.equal(recovered.review.review_status, 'confirmed');
  assert.deepEqual(calls, [['post', 'existing-job'], ['get', 'existing-job']]);
  assert.equal(flow.getContext(), null);
});
test('failed authoritative reload prevents a repeated review write', async () => {
  let writes = 0;
  const flow = createOcrReviewFlow({
    getReview: async () => { throw new Error('offline'); },
    review: async () => { writes++; throw Object.assign(new Error('uncertain'), { code: 'REQUEST_TIMEOUT' }); },
  });
  flow.setContext({ job_id: 'existing-job' });
  await assert.rejects(flow.decide('approve'));
  await assert.rejects(flow.decide('approve'), /offline/);
  assert.equal(writes, 1);
});
