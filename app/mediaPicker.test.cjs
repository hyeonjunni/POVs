const { test } = require("node:test");
const assert = require("node:assert/strict");

for (const stage of ["permission", "picker"]) {
  test(`${stage} exception is recoverable and a later selection succeeds`, async () => {
    const { pickMediaAsset } = require("./mediaPicker.cjs");
    const failure = new Error("Native picker unavailable");
    const errors = [];
    let fail = true;
    const asset = { uri: "file:///photo.jpg" };
    const options = {
      imagePicker: {
        requestMediaLibraryPermissionsAsync: async () => {
          if (fail && stage === "permission") throw failure;
          return { granted: true };
        },
        launchImageLibraryAsync: async () => {
          if (fail && stage === "picker") throw failure;
          return { canceled: false, assets: [asset] };
        },
      },
      typeKey: "image",
      onPermissionDenied: () => assert.fail("unexpected permission notice"),
      onError: error => errors.push(error),
    };
    assert.equal(await pickMediaAsset(options), null);
    assert.deepEqual(errors, [failure]);
    fail = false;
    assert.equal(await pickMediaAsset(options), asset);
  });
}

test("permission denial uses the notice without launching the picker", async () => {
  const { pickMediaAsset } = require("./mediaPicker.cjs");
  let notices = 0;
  assert.equal(await pickMediaAsset({
    imagePicker: {
      requestMediaLibraryPermissionsAsync: async () => ({ granted: false }),
      launchImageLibraryAsync: () => assert.fail("picker must not launch"),
    },
    typeKey: "image",
    onPermissionDenied: () => notices++,
    onError: () => assert.fail("unexpected error"),
  }), null);
  assert.equal(notices, 1);
});

for (const result of [{ canceled: true }, { canceled: false, assets: [] }]) {
  test(`cancel or empty selection is silent: ${JSON.stringify(result)}`, async () => {
    const { pickMediaAsset } = require("./mediaPicker.cjs");
    assert.equal(await pickMediaAsset({
      imagePicker: {
        requestMediaLibraryPermissionsAsync: async () => ({ granted: true }),
        launchImageLibraryAsync: async () => result,
      },
      typeKey: "image",
      onPermissionDenied: () => assert.fail("unexpected permission notice"),
      onError: () => assert.fail("unexpected error"),
    }), null);
  });
}

for (const [typeKey, mediaTypes] of [["image", ["images"]], ["video", ["videos"]]]) {
  test(`${typeKey} selection preserves options and returns the existing asset`, async () => {
    const { pickMediaAsset } = require("./mediaPicker.cjs");
    const asset = { uri: "file:///selected", fileName: "selected" };
    assert.equal(await pickMediaAsset({
      imagePicker: {
        requestMediaLibraryPermissionsAsync: async () => ({ granted: true }),
        launchImageLibraryAsync: async options => {
          assert.deepEqual(options, { mediaTypes, allowsEditing: false, quality: 1 });
          return { canceled: false, assets: [asset] };
        },
      },
      typeKey,
      onPermissionDenied: () => assert.fail("unexpected permission notice"),
      onError: () => assert.fail("unexpected error"),
    }), asset);
  });
}

test("runtime network guidance renders real newlines", () => {
  const fs = require("node:fs");
  const vm = require("node:vm");
  const source = fs.readFileSync(require.resolve("./src/shared/api/runtime.js"), "utf8");
  const start = source.indexOf("export function getNetworkGuidanceMessage()");
  const end = source.indexOf("export function formatApiErrorDetail");
  const context = { getRuntimeApiBaseUrl: () => "http://localhost:8000" };
  vm.runInNewContext(source.slice(start, end).replaceAll("export function", "function"), context);
  const message = context.getReadableErrorMessage(new Error("Network request failed"), "저장 실패");
  assert.ok(message.startsWith("저장 실패\n\n"));
  assert.equal(message.includes("\\n"), false);
  assert.ok(message.includes("http://localhost:8000"));
  assert.equal(context.getReadableErrorMessage(new Error("Other error"), "fallback"), "Other error");
});
