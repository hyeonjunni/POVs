const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createFamilyRoomsApi, persistActiveRoomId, selectActiveRoomId } = require("./familyRooms.cjs");

test("room API uses authenticated backend routes and exact join DTO", async () => {
  const calls = [];
  const client = {
    get: async (...args) => { calls.push(["get", ...args]); return []; },
    post: async (...args) => { calls.push(["post", ...args]); return { room_id: "room-2" }; },
    delete: async (...args) => { calls.push(["delete", ...args]); return { ok: true }; },
  };
  const rooms = createFamilyRoomsApi(client);

  await rooms.list();
  await rooms.members("room/1");
  await rooms.join({ invite_code: "ABC123", related_to_user_id: null, relation_to_related_user: null });
  await rooms.create({ name: "Family" }, { headers: { Prefer: "respond-async" } });
  assert.equal(rooms.remove, undefined);

  assert.deepEqual(calls, [
    ["get", "/families"],
    ["get", "/families/room%2F1/members"],
    ["post", "/families/join", { json: { invite_code: "ABC123", related_to_user_id: null, relation_to_related_user: null } }],
    ["post", "/families", { headers: { Prefer: "respond-async" }, json: { name: "Family" } }],
  ]);
});

test("persists room selection and removes it when no room is active", async () => {
  const calls = [];
  const storage = {
    setItem: async (...args) => calls.push(["set", ...args]),
    removeItem: async (...args) => calls.push(["remove", ...args]),
  };
  await persistActiveRoomId(storage, "ambient.activeFamilyId", "room-2");
  await persistActiveRoomId(storage, "ambient.activeFamilyId", null);
  assert.deepEqual(calls, [
    ["set", "ambient.activeFamilyId", "room-2"],
    ["remove", "ambient.activeFamilyId"],
  ]);
});

test("active room restore keeps an available room and otherwise selects the first", () => {
  const rooms = [{ room_id: "room-1" }, { room_id: "room-2" }];
  assert.equal(selectActiveRoomId(rooms, "room-2"), "room-2");
  assert.equal(selectActiveRoomId(rooms, "removed-room"), "room-1");
  assert.equal(selectActiveRoomId([], "room-2"), null);
});
