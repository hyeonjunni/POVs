const { test } = require("node:test");
const assert = require("node:assert/strict");
const { isUiPreviewAvailable, enterUiPreview, createPreviewIdentity, createPreviewRecord } = require("./src/features/preview/previewModel.cjs");

test("test login is restricted to development web on exact loopback hosts", () => {
  for (const hostname of ["localhost", "127.0.0.1", "::1", "[::1]"]) {
    assert.equal(isUiPreviewAvailable({ development: true, platform: "web", hostname }), true);
  }
  for (const development of [false, undefined, "true", 1]) {
    assert.equal(isUiPreviewAvailable({ development, platform: "web", hostname: "localhost" }), false);
  }
  for (const platform of ["android", "ios", undefined]) {
    assert.equal(isUiPreviewAvailable({ development: true, platform, hostname: "localhost" }), false);
  }
  for (const hostname of ["localhost.evil.example", "127.0.0.1.evil.example", "example.com", "192.168.1.2", "", undefined]) {
    assert.equal(isUiPreviewAvailable({ development: true, platform: "web", hostname }), false);
  }
});

test("test login handler rechecks its gate and cannot enter a release build", () => {
  let entries = 0;
  assert.equal(enterUiPreview({ development: false, platform: "web", hostname: "localhost" }, () => entries++), false);
  assert.equal(entries, 0);
  assert.equal(enterUiPreview({ development: true, platform: "web", hostname: "localhost" }, () => entries++), true);
  assert.equal(entries, 1);
});

test("preview identity is isolated and recreated on each entry", () => {
  const first = createPreviewIdentity();
  first.user.name = "임시 변경";
  first.family.members[0].name = "임시 변경";
  const second = createPreviewIdentity();
  assert.equal(second.user.name, "테스트 사용자");
  assert.equal(second.family.members[0].name, "테스트 사용자");
  assert.equal(second.user.id, "ui-preview-user");
  assert.equal(second.family.id, "ui-preview-room");
  assert.equal(second.user.accessToken, undefined);
});

test("preview records cannot carry protected media or authentication data", () => {
  for (const type of ["text", "image", "video", "voice"]) {
    const record = createPreviewRecord({ type, title: " 테스트 ", detail: " 내용 ", tags: ["a", "a", " b "], hasFile: true, fileUrl: "https://real.example/file", fileId: "real-file", token: "secret" }, 1, "2026.10.02");
    assert.equal(record.id, "ui-preview-record-1");
    assert.equal(record.title, "테스트");
    assert.equal(record.hasFile, false);
    assert.equal(record.fileUrl, null);
    assert.equal(record.fileId, null);
    assert.equal(record.token, undefined);
    assert.deepEqual(record.tags, ["a", "b"]);
  }
  assert.equal(createPreviewRecord({ type: "text", title: " " }, 1, ""), null);
  assert.equal(createPreviewRecord({ type: "unknown", title: "기록" }, 1, ""), null);
});
