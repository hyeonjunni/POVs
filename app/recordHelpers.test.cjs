const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const babel = require("@babel/core");
const source = fs.readFileSync(`${__dirname}/src/features/records/recordHelpers.js`, "utf8");
const code = babel.transformSync(source, { babelrc: false, configFile: false, plugins: ["@babel/plugin-transform-modules-commonjs"] }).code;
const loaded = { exports: {} };
new Function("module", "exports", code)(loaded, loaded.exports);
const { mapUploadToRecord, formatRecordDate } = loaded.exports;

test("record dates distinguish occurrence, authorship and registration without changing media type", () => {
  const upload = { upload_id: "photo", type: "image", created_at: "2026-10-13", tags: ["가족행사"], metadata: { occurred_at: "2009-05-17", authored_at: "2026-10-04", source_record_id: "IC01" } };
  const record = mapUploadToRecord(upload);
  assert.equal(record.displayDate, "발생일 2009.05.17");
  assert.equal(record.createdAt, "2026.10.13");
  assert.equal(record.type, "image");
  assert.deepEqual(record.tags, ["가족행사"]);
  assert.equal(mapUploadToRecord({ ...upload, metadata: { authored_at: "2026-10-04" } }).displayDate, "작성일 2026.10.04");
  assert.equal(mapUploadToRecord({ ...upload, metadata: {} }).displayDate, "등록일 2026.10.13");
  assert.equal(formatRecordDate("2009-05"), "2009.05");
});

test("only explicit EVENT metadata classifies an event outside text while preserving the API type", () => {
  const event = mapUploadToRecord({ upload_id: "event", type: "text", metadata: { record_kind: "EVENT" } });
  assert.equal(event.displayType, "event");
  assert.equal(event.type, "text");
  assert.equal(mapUploadToRecord({ upload_id: "word", type: "text", title: "WORD_EVENT_C01" }).displayType, "text");
});

test("date ranges and approximate dates retain their complete original text", () => {
  for (const value of ["2022-05-07 ~ 2022-05-08", "2009-05-17 무렵", "2009년 봄", "2022-05-07 ~ 현재", "May 7, 2022"]) {
    assert.equal(formatRecordDate(value), value);
    const record = mapUploadToRecord({ upload_id: "range", type: "text", metadata: { occurred_at: value } });
    assert.equal(record.displayDate, `발생일 ${value}`);
  }
  assert.equal(formatRecordDate("2022-05-07"), "2022.05.07");
  assert.equal(formatRecordDate("2022-05-07T12:34:56"), "2022.05.07");
  assert.equal(formatRecordDate("2022-05-07Tinvalid"), "2022-05-07Tinvalid");
});
