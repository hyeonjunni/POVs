const { test } = require("node:test");
const assert = require("node:assert/strict");
const { loadWorkspacePreservingSession } = require("./workspaceBootstrap.cjs");

test("workspace failure is reported as a workspace result", async () => {
  let reportedError;
  const error = new Error("workspace unavailable");

  const result = await loadWorkspacePreservingSession(
    async () => { throw error; },
    (failure) => { reportedError = failure; }
  );

  assert.deepEqual(result, { ok: false, error });
  assert.equal(reportedError, error);
});

test("successful workspace load returns its value", async () => {
  const result = await loadWorkspacePreservingSession(async () => ({ roomId: "room-1" }));
  assert.deepEqual(result, { ok: true, value: { roomId: "room-1" } });
});
