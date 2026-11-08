function createFamilyRoomsApi(client) {
  const roomPath = (roomId, suffix = "") => `/families/${encodeURIComponent(roomId)}${suffix}`;
  return {
    list: () => client.get("/families"),
    get: (roomId) => client.get(roomPath(roomId)),
    members: (roomId) => client.get(roomPath(roomId, "/members")),
    previewInvite: (inviteCode) => client.get(`/families/invite/${encodeURIComponent(inviteCode)}`),
    create: (payload, options = {}) => client.post("/families", { ...options, json: payload }),
    join: (payload) => client.post("/families/join", { json: payload }),
  };
}

function selectActiveRoomId(rooms, preferredRoomId = null) {
  const ids = rooms.map((room) => room.room_id || room.id).filter(Boolean);
  return preferredRoomId && ids.includes(preferredRoomId) ? preferredRoomId : ids[0] || null;
}

async function persistActiveRoomId(storage, key, roomId) {
  if (roomId) return storage.setItem(key, roomId);
  return storage.removeItem(key);
}

module.exports = { createFamilyRoomsApi, persistActiveRoomId, selectActiveRoomId };
