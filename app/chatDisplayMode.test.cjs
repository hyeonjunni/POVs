const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const babel = require("@babel/core");
const { serviceMedia } = require("./src/features/chat/serviceMedia.cjs");

function fixture(storage, allowDeveloper = true) {
  const slots = [], effects = [];
  let cursor = 0;
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initial;
      return [slots[index], value => { slots[index] = value; }];
    },
    useRef(initial) { const index = cursor++; return slots[index] ||= { current: initial }; },
    useEffect(effect) { if (!slots[cursor++]) { slots[cursor - 1] = true; effects.push(effect); } },
  };
  const filename = path.join(__dirname, "src/features/chat/useChatDisplayMode.js");
  const code = babel.transformSync(fs.readFileSync(filename, "utf8"), {
    filename, babelrc: false, configFile: false, plugins: ["@babel/plugin-transform-modules-commonjs"],
  }).code;
  const module = { exports: {} };
  new Function("require", "module", "exports", code)(id => id === "react" ? react : storage, module, module.exports);
  return { render() { cursor = 0; const value = module.exports.useChatDisplayMode(allowDeveloper); effects.splice(0).forEach(effect => effect()); return value; } };
}

test("release display ignores saved developer preference and stays in service mode", async () => {
  const h = fixture({ getItem: async () => "developer", setItem: async () => assert.fail("release must not change developer preference") }, false);
  h.render();
  await Promise.resolve();
  await h.render().changeMode("developer");
  assert.equal(h.render().mode, "service");
});

test("display defaults to service and restores a persisted developer preference", async () => {
  const values = new Map();
  const storage = { getItem: async key => values.get(key), setItem: async (key, value) => { values.set(key, value); } };
  const first = fixture(storage);
  assert.equal(first.render().mode, "service");
  await first.render().changeMode("developer");
  const second = fixture(storage);
  second.render();
  await Promise.resolve();
  assert.equal(second.render().mode, "developer");
  await second.render().changeMode("service");
  assert.equal(values.get("ambient.chatDisplayMode"), "service");
});

test("late preference read cannot override a new choice and failed persistence is visible", async () => {
  let resolveRead;
  const h = fixture({ getItem: () => new Promise(resolve => { resolveRead = resolve; }), setItem: async () => { throw new Error("disk unavailable"); } });
  await h.render().changeMode("service");
  resolveRead("developer");
  await Promise.resolve();
  assert.equal(h.render().mode, "service");
  assert.match(h.render().error, /저장하지 못했습니다/);
});

test("media filtering supports requested audio and video and requires matching response records", () => {
  const response = { gate_route: "ANSWER", citations: ["voice", "video", "image", "unknown"].map(record_id => ({ record_id })),
    query_context: { records: ["voice", "video", "image"].map(type => ({ record_id: type, record_type: type })) } };
  assert.deepEqual(serviceMedia("음성이나 영상 보여줘", response).map(item => item.record.record_type), ["voice", "video"]);
  assert.deepEqual(serviceMedia("건강검진 결과표 보여줘", response).map(item => item.record.record_type), ["image"]);
  assert.deepEqual(serviceMedia("사진 보여줘", { ...response, query_context: null }), []);
});
