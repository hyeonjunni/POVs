const test = require("node:test");
const assert = require("node:assert/strict");
const { createApiClient } = require("./apiClient.cjs");
const { DEFAULT_ASK, validateCitation, validateAskResponse, createGroundedChatApi, createChatSession, chatErrorMessage } = require("./src/features/chat/groundedChat.cjs");
const { createCitationOriginal } = require("./src/features/chat/citationOriginal.cjs");
const baseUrl = "http://127.0.0.1:8001/api/v1";
const hash = "a".repeat(64);
const citation = () => ({
  fact_id: "fact-1", chunk_id: "chunk-1", record_id: "record-1", file_id: "file-1",
  chunk_char_start: 0, chunk_char_end: 24, source_locator: { kind: "ocr", page: 1 },
  extractor: "ocr", extractor_id: "local_tesseract:tesseract", review_status: "approved",
  user_confirmed: true, evidence_status: "extracted", source_object_id: "object-1", source_object_version: 1,
  source_ciphertext_sha256: hash,
  original_url: `/api/v1/uploads/record-1/content?file_id=file-1&source_object_id=object-1&source_object_version=1&expected_ciphertext_sha256=${hash}`,
});
const answer = () => ({ gate_route: "ANSWER", answer: "서버의 근거 문장", citations: [citation()], gate_action: "answer", provider_mode: "selected" });

test("general advice accepts external sources without fabricating family citations", () => {
  const response = { ...answer(), citations: [], gate_action: "general_health_reference",
    general_sources: ["https://www.nhs.uk/conditions/iron-deficiency-anaemia/"] };
  assert.equal(validateAskResponse(response, baseUrl).citations.length, 0);
  assert.throws(() => validateAskResponse({ ...response, general_sources: [] }, baseUrl));
  assert.throws(() => validateAskResponse({ ...response, general_sources: ["javascript:alert(1)"] }, baseUrl));
  assert.throws(() => validateAskResponse({ ...answer(), citations: [] }, baseUrl));
});

test("count-only questions do not show media actions", () => {
  const { serviceMedia } = require("./src/features/chat/serviceMedia.cjs");
  const response = { ...answer(), query_context: { records: [{ record_id: "record-1", record_type: "image", title: "사진" }] } };
  assert.deepEqual(serviceMedia("전체 사진은 몇 장이야?", response), []);
  assert.equal(serviceMedia("사진 보여줘", response).length, 1);
});

test("followup sends prior successful question and source IDs and clears them across account scope", async () => {
  let scope = { userId: "owner", roomId: "room", token: "token", baseUrl }, calls = [];
  const session = createChatSession({ readScope: () => scope, ask: async value => { calls.push(value); return answer(); } });
  await session.send("여행 숙소 창가에서 어떤일이 있었어?");
  await session.send("어떤 메모가 있었어?");
  assert.deepEqual(calls[1].history, [{ query: calls[0].query, record_ids: ["record-1"] }]);
  assert.equal(calls[0].contextMode, "ROOM");
  assert.equal(calls[1].contextMode, "FOLLOWUP");
  session.setContextMode("ROOM");
  await session.send("새로운 여행 기록 보여줘");
  assert.equal(calls[2].contextMode, "ROOM");
  session.reset();
  assert.deepEqual(session.getState().messages, []);
  assert.equal(session.getState().contextMode, "ROOM");
  scope = { ...scope, userId: "other", token: "other-token" };
  await session.send("어떤 메모가 있었어?");
  assert.deepEqual(calls[3].history, []);
  session.dispose();
});

test("answer markdown becomes readable plain text without visible formatting markers", () => {
  const { answerText } = require("./src/features/chat/answerText.cjs");
  assert.equal(answerText("## 메모\n* **조약돌:** 세 개\n* `창가`에 놓았다."), "메모\n• 조약돌: 세 개\n• 창가에 놓았다.");
});

test("multimodal AUDIO citation fetches exact protected bytes into disposable native audio", async () => {
  const value = { ...citation(), source_locator: { kind: "multimodal", media_type: "AUDIO", review_version: 1,
    anchors: [{ kind: "AUDIO", segment_id: "segment-1", char_start: 0, char_end: 24, start_ms: 1200, end_ms: 3400 }] } };
  let calls = 0, file;
  class File {
    constructor() { file = this; this.uri = "file:///cache/citation.wav"; this.exists = false; }
    create() { this.exists = true; }
    write(bytes) { this.bytes = bytes; }
    delete() { this.exists = false; }
  }
  const request = createCitationOriginal({ citation: value, baseUrl, token: "jwt", platform: "android",
    nativeFile: { File, cache: "cache", randomUUID: () => "unique" },
    fetchImpl: async (url, options) => {
      calls++; assert.equal(url, validateCitation(value, baseUrl));
      assert.equal(options.headers.Authorization, "Bearer jwt");
      return new Response("audio bytes", { headers: { "content-type": "audio/wav" } });
    } });
  assert.deepEqual(await request.ready, { kind: "audio", source: { uri: "file:///cache/citation.wav" } });
  assert.equal(calls, 1); assert.equal(file.exists, true);
  request.release(); assert.equal(file.exists, false);
});

test("audio citation supports disposable web playback but refuses mismatched and unsupported MIME", async () => {
  const value = { ...citation(), source_locator: { kind: "multimodal", media_type: "AUDIO", anchors: [] } };
  const request = createCitationOriginal({ citation: value, baseUrl, token: "jwt", platform: "web",
    fetchImpl: async () => new Response("audio", { headers: { "content-type": "audio/mpeg" } }) });
  const media = await request.ready;
  assert.equal(media.kind, "audio"); assert.match(media.source.uri, /^blob:/);
  request.release(); await assert.rejects(fetch(media.source.uri));
  for (const mime of ["image/png", "text/html", "application/pdf", "audio/unknown"]) {
    const rejected = createCitationOriginal({ citation: value, baseUrl, token: "jwt", platform: "web",
      fetchImpl: async () => new Response("bytes", { headers: { "content-type": mime } }) });
    await assert.rejects(rejected.ready, /안전하게 표시/);
  }
  const wrongSource = createCitationOriginal({ citation: citation(), baseUrl, token: "jwt", platform: "web",
    fetchImpl: async () => new Response("audio", { headers: { "content-type": "audio/wav" } }) });
  await assert.rejects(wrongSource.ready, /안전하게 표시/);
});

test("audio citation range labels use only valid millisecond anchors", () => {
  const { audioCitationRanges, audioCitationLocation } = require("./src/features/chat/audioCitation.cjs");
  const value = { source_locator: { kind: "multimodal", media_type: "AUDIO", anchors: [
    { kind: "AUDIO", start_ms: 1234, end_ms: 5678 }, { kind: "AUDIO", start_ms: -1, end_ms: 20 },
    { kind: "VIDEO", start_ms: 0, end_ms: 20 }, { kind: "AUDIO", start_ms: 20, end_ms: 1 },
  ] } };
  assert.deepEqual(audioCitationRanges(value), [{ start: 1.234, end: 5.678 }]);
  assert.equal(audioCitationLocation(value), "음성 구간 0:01–0:05");
  assert.equal(audioCitationLocation(citation()), null);
});

test("deterministic discovery retains strict citation validation without a provider", () => {
  const response = { ...answer(), gate_action: "record_discovery_render", provider_mode: "not_called" };
  assert.equal(validateAskResponse(response, baseUrl).gate_route, "ANSWER");
  assert.throws(() => validateAskResponse({ ...response, citations: [] }, baseUrl));
  assert.throws(() => validateAskResponse({ ...response, citations: [{ ...citation(), file_id: "wrong" }] }, baseUrl));
});
const scope = () => ({ userId: "user-1", roomId: "room-1", token: "test.jwt.token", baseUrl });
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

test("extraction review metadata alone already permits unreviewed and false", () => {
  const value = { ...citation(), review_status: "unreviewed", user_confirmed: false };
  assert.equal(validateAskResponse({ ...answer(), citations: [value] }, baseUrl).gate_route, "ANSWER");
});

test("URL-only processing tuple preserves strict protected URL and object validation", async () => {
  const value = { ...citation(), review_status: "unreviewed", user_confirmed: false };
  value.original_url += `&processing_job_id=cc9ad45c-45ea-4dfa-b3b1-073d9ffec789&source_attempt=1&source_result_sha256=${hash}`;
  assert.equal(validateCitation(value, baseUrl), new URL(value.original_url, baseUrl).toString());
  const invalid = ['http://[', value.original_url.replace('/content?', '/delete?'),
    `https://foreign.example${value.original_url}`, `${value.original_url}&token=secret`,
    `${value.original_url}&processing_job_id=duplicate`, `${value.original_url}#fragment`];
  for (const key of ['processing_job_id', 'source_attempt', 'source_result_sha256']) {
    const url = new URL(value.original_url, baseUrl);
    url.searchParams.delete(key); invalid.push(url.toString());
    url.searchParams.set(key, 'invalid'); invalid.push(url.toString());
  }
  for (const attempt of ['-1', '1.5', '01', '9007199254740992']) {
    const url = new URL(value.original_url, baseUrl);
    url.searchParams.set('source_attempt', attempt); invalid.push(url.toString());
  }
  for (const original_url of invalid) {
    let calls = 0;
    const source = createCitationOriginal({ citation: { ...value, original_url }, baseUrl, token: 'jwt', platform: 'android',
      fetchImpl: async () => { calls++; throw new Error('must not fetch'); } });
    await assert.rejects(source.ready);
    assert.equal(calls, 0);
  }
  for (const key of ['file_id', 'source_object_id', 'source_object_version', 'source_ciphertext_sha256']) {
    assert.throws(() => validateCitation({ ...value, [key]: key === 'source_object_version' ? 2 : 'changed' }, baseUrl));
  }
});

test("HTTP Ask errors expose status without echoing private server details", () => {
  for (const status of [400, 401, 403, 404, 409, 422, 500, 503]) {
    const message = chatErrorMessage(Object.assign(new Error("private server detail Bearer secret-token"), { status }));
    assert.ok(message.includes(`HTTP ${status}`));
    assert.doesNotMatch(message, /private server detail|secret-token/);
  }
});

const legacyCitation = () => {
  const value = citation();
  delete value.source_object_id; delete value.source_object_version; delete value.source_ciphertext_sha256;
  return { ...value, source_sha256: hash,
    original_url: `/api/v1/uploads/record-1/files/file-1/content?expected_sha256=${hash}` };
};

test("legacy file-content citations are rejected before any request", async () => {
  const value = legacyCitation();
  assert.throws(() => validateAskResponse({ ...answer(), citations: [value] }, baseUrl));
  let calls = 0;
  const source = createCitationOriginal({ citation: value, baseUrl, token: "test-token", platform: "web",
    fetchImpl: async () => { calls++; } });
  await assert.rejects(source.ready);
  assert.equal(calls, 0);
});

test("rejected OCR evidence is refused instead of rendered or opened", () => {
  for (const value of [citation(), legacyCitation()]) {
    assert.throws(() => validateAskResponse({ ...answer(), citations: [{ ...value, review_status: "rejected" }] }, baseUrl));
  }
});

test("secure original 409 uses dedicated STALE_SOURCE regardless of detail wording", async () => {
  let calls = 0;
  const request = createCitationOriginal({ citation: citation(), baseUrl, token: "test.jwt.token", platform: "web",
    fetchImpl: async () => { calls++; return new Response('{"detail":"Source content changed"}', { status: 409 }); } });
  await assert.rejects(request.ready, error => error.status === 409 && error.code === "STALE_SOURCE");
  assert.equal(calls, 1);
  assert.match(chatErrorMessage({ status: 409, code: "STALE_SOURCE" }), /원본이 변경/);
});

test("grounded ask uses existing bearer client and the exact room-scoped contract", async () => {
  const requests = [];
  const client = createApiClient({ baseUrl, getAccessToken: () => "test.jwt.token", fetchImpl: async (url, options) => {
    requests.push({ url, options }); return new Response(JSON.stringify(answer()), { status: 200 });
  } });
  const controller = new AbortController();
  const result = await createGroundedChatApi(client).ask({ roomId: "room-1", query: "  가족 여행은?  ", baseUrl, signal: controller.signal });
  assert.equal(requests[0].url, `${baseUrl}/families/room-1/ask`);
  assert.equal(requests[0].options.headers.Authorization, "Bearer test.jwt.token");
  assert.deepEqual(JSON.parse(requests[0].options.body), { query: "가족 여행은?", ...DEFAULT_ASK });
  assert.equal(requests[0].options.signal, controller.signal);
  assert.equal(result.gate_route, "ANSWER");
});

test("all five gates retain their meaning and only ANSWER carries immutable citations", () => {
  const transport = answer(), validated = validateAskResponse(transport, baseUrl);
  transport.citations[0].source_locator.page = 99;
  assert.equal(validated.citations[0].source_locator.page, 1);
  assert.ok(Object.isFrozen(validated.citations[0]));
  for (const gate of ["REFUSE", "CLARIFY", "CONFLICT", "NO_RECORD"]) {
    const value = { gate_route: gate, answer: "서버 안내", citations: [], gate_action: gate.toLowerCase(), provider_mode: "not_called" };
    assert.equal(validateAskResponse(value, baseUrl).gate_route, gate);
    assert.throws(() => validateAskResponse({ ...value, citations: [citation()] }, baseUrl));
  }
  assert.throws(() => validateAskResponse({ ...answer(), gate_route: "CHAT" }, baseUrl));
  assert.throws(() => validateAskResponse({ ...answer(), citations: [] }, baseUrl));
  assert.throws(() => validateAskResponse({ ...answer(), provider_mode: "not_called" }, baseUrl));
});

test("protected originals require exact origin and all immutable binding values", () => {
  assert.equal(new URL(validateCitation(citation(), baseUrl)).origin, new URL(baseUrl).origin);
  for (const key of ["file_id", "source_object_id", "source_object_version", "source_ciphertext_sha256"]) {
    assert.throws(() => validateCitation({ ...citation(), [key]: key === "source_object_version" ? 2 : "changed" }, baseUrl));
  }
  for (const url of ["https://elsewhere.example/original", "/api/v1/uploads/record-1/content", `${citation().original_url}&file_id=file-1`, `${citation().original_url}&access_token=secret`, `${citation().original_url}#fragment`]) {
    assert.throws(() => validateCitation({ ...citation(), original_url: url }, baseUrl));
  }
});

test("local preview identities cannot send and duplicate sends are blocked", async () => {
  let current = { ...scope(), token: "" }, calls = 0;
  const pending = deferred();
  const session = createChatSession({ readScope: () => current, ask: async () => { calls++; return pending.promise; } });
  session.setDraft("가족 여행은?");
  assert.equal(await session.send(), false); assert.equal(calls, 0);
  current = scope(); session.setDraft("첫 질문");
  const sent = session.send(); assert.equal(session.getState().sending, true);
  assert.equal(await session.send("둘째 질문"), false); assert.equal(calls, 1);
  pending.resolve(answer()); await sent;
  assert.equal(session.getState().messages[0].response.gate_route, "ANSWER");
  session.dispose();
});

test("room, account and token changes cancel requests and hide every old message", async () => {
  for (const change of [{ roomId: "room-2" }, { userId: "user-2", token: "another.token" }, { token: "" }]) {
    let current = scope(); const pending = deferred(); let signal;
    const session = createChatSession({ readScope: () => current, ask: args => { signal = args.signal; return pending.promise; } });
    session.setDraft("이 방의 비공개 질문"); const sent = session.send();
    current = { ...current, ...change };
    assert.deepEqual(session.getStateFor(current).messages, []);
    session.updateScope(current); assert.equal(signal.aborted, true);
    pending.resolve(answer()); await sent;
    assert.deepEqual(session.getState().messages, []); session.dispose();
  }
});

test("retry cannot replay a private question into a new room", async () => {
  let current = scope(), calls = 0;
  const session = createChatSession({ readScope: () => current, ask: async () => { calls++; throw Object.assign(new Error(), { status: 503 }); } });
  session.setDraft("이전 방의 질문"); await session.send();
  const oldMessage = session.getState().messages[0];
  current = { ...current, roomId: "different-room" };
  assert.equal(await session.retry(oldMessage), false); assert.equal(calls, 1);
  session.dispose();
});

test("hung inference is bounded even when transport ignores abort", async () => {
  let current = scope();
  const session = createChatSession({ readScope: () => current, ask: () => new Promise(() => {}), timeoutMs: 5 });
  session.setDraft("응답 확인"); assert.equal(await session.send(), false);
  assert.equal(session.getState().sending, false);
  assert.match(session.getState().messages[0].error, /시간/); session.dispose();
});

test("401 clears only its originating session while 403 keeps authentication", async () => {
  let cleared = 0;
  for (const status of [401, 403]) {
    const client = createApiClient({ baseUrl, getAccessToken: () => "test.jwt.token", onUnauthorized: token => { assert.equal(token, "test.jwt.token"); cleared++; }, fetchImpl: async () => new Response('{"detail":"denied"}', { status }) });
    await assert.rejects(createGroundedChatApi(client).ask({ roomId: "room-1", query: "질문", baseUrl }), error => error.status === status);
  }
  assert.equal(cleared, 1); assert.match(chatErrorMessage({ status: 403 }), /권한/);
});

test("STALE_SOURCE is never replaced by a latest or record-only original", async () => {
  let calls = 0;
  const source = createCitationOriginal({ citation: citation(), baseUrl, token: "test.jwt.token", platform: "web", fetchImpl: async (url, options) => {
    calls++; assert.equal(url, validateCitation(citation(), baseUrl));
    assert.equal(options.headers.Authorization, "Bearer test.jwt.token");
    assert.equal(options.redirect, "error"); assert.equal(options.credentials, "omit");
    return new Response('{"detail":"STALE_SOURCE"}', { status: 409 });
  } });
  await assert.rejects(source.ready, error => error.status === 409 && error.code === "STALE_SOURCE");
  assert.equal(calls, 1);
});

test("original blobs are revoked and late responses cannot reopen a closed viewer", async () => {
  const source = createCitationOriginal({ citation: citation(), baseUrl, token: "test.jwt.token", platform: "web", fetchImpl: async () => new Response(new Blob(["passive image"], { type: "image/png" }), { headers: { "content-type": "image/png" } }) });
  const result = await source.ready; assert.match(result.source.uri, /^blob:/);
  source.release(); await assert.rejects(fetch(result.source.uri));
  const pending = deferred();
  const late = createCitationOriginal({ citation: citation(), baseUrl, token: "test.jwt.token", platform: "web", fetchImpl: () => pending.promise });
  late.release(); pending.resolve(new Response(new Blob(["image"]), { headers: { "content-type": "image/png" } }));
  await assert.rejects(late.ready, error => error.name === "AbortError");
});

test("active originals and unsupported MIME types never become preview sources", async () => {
  for (const mime of ["text/html", "image/svg+xml", "application/octet-stream"]) {
    const source = createCitationOriginal({ citation: citation(), baseUrl, token: "test.jwt.token", platform: "web", fetchImpl: async () => new Response("unsupported", { headers: { "content-type": mime } }) });
    await assert.rejects(source.ready);
  }
});

for (const platform of ["web", "android"]) test(`${platform} secure text attachments remain plain text with the exact citation binding`, async () => {
  const body = '가족 기록 원문 <script>window.example = true</script>';
  let calls = 0;
  const source = createCitationOriginal({citation: citation(), baseUrl, token: "test.jwt.token", platform,
    fetchImpl: async (url, options) => {
      calls++;
      assert.equal(url, validateCitation(citation(), baseUrl));
      assert.equal(options.headers.Authorization, "Bearer test.jwt.token");
      assert.equal(options.redirect, "error");
      return new Response(body, {headers: {"content-type": "text/plain; charset=utf-8",
        "content-disposition": "attachment", "content-security-policy": "sandbox; default-src 'none'"}});
    }});
  assert.deepEqual(await source.ready, {kind: "text", text: body});
  assert.equal(calls, 1);
  source.release();
});

test("attachment exception never admits images, video or active content", async () => {
  for (const mime of ["image/png", "video/mp4", "text/html", "image/svg+xml"]) {
    const source = createCitationOriginal({citation: citation(), baseUrl, token: "test.jwt.token", platform: "web",
      fetchImpl: async () => new Response("attachment", {headers: {"content-type": mime, "content-disposition": "attachment"}})});
    await assert.rejects(source.ready, /안전하게 표시/);
  }
});

test("secure text attachment keeps the text display limit", async () => {
  const source = createCitationOriginal({citation: citation(), baseUrl, token: "test.jwt.token", platform: "web",
    fetchImpl: async () => new Response("x".repeat(250001), {headers: {"content-type": "text/plain; charset=utf-8", "content-disposition": "attachment"}})});
  await assert.rejects(source.ready, /표시 한도/);
});

test("native originals use checked arrayBuffer bytes, not unsupported Expo blobs or a second media request", async () => {
  for (const mime of ["image/png", "video/mp4"]) {
    let calls = 0;
    const request = createCitationOriginal({ citation: citation(), baseUrl, token: "test.jwt.token", platform: "android",
      fetchImpl: async () => {
        calls++;
        const result = new Response("checked bytes", { headers: { "content-type": mime } });
        result.blob = () => { throw new Error("Expo fetch blob is unsupported on native"); };
        return result;
      } });
    const media = await request.ready;
    assert.equal(media.source.uri, `data:${mime};base64,${Buffer.from("checked bytes").toString("base64")}`);
    assert.equal(media.source.headers, undefined);
    assert.equal(calls, 1);
    request.release();
  }
});

test('structured/claim results preserve unknown semantics and exact protected citations', () => {
  const value = citation();
  value.original_url += `&processing_job_id=cc9ad45c-45ea-4dfa-b3b1-073d9ffec789&source_attempt=1&source_result_sha256=${hash}&authorization_epoch=1`;
  assert.ok(validateCitation(value,baseUrl));
  assert.throws(()=>validateCitation({...value,original_url:value.original_url.replace('authorization_epoch=1','authorization_epoch=0')},baseUrl));
  const partial={first_used_at:{precision:'UNKNOWN'}};
  assert.deepEqual(validateAskResponse({...answer(),citations:[value],provider_mode:'not_called',gate_action:'structured_render',structured_result:partial},baseUrl).structured_result,partial);
  const claim={...value,claim_id:'claim-1',claim_version:1};
  claim.original_url+='&claim_id=claim-1&claim_version=1';
  assert.equal(validateAskResponse({...answer(),gate_route:'CONFLICT',citations:[claim],provider_mode:'not_called',gate_action:'claim_render',claim_result:{mother_memory:'unestablished'}},baseUrl).citations.length,1);
  assert.throws(()=>validateCitation({...claim,claim_version:2},baseUrl));
  assert.equal(validateAskResponse({gate_route:'NEEDS_DATA',answer:null,citations:[],provider_mode:'not_called',gate_action:'needs_data'},baseUrl).answer,null);
});
