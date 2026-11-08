export function mapBackendUser(payload, fallback = {}) {
  return {
    id: payload?.user_id || fallback.id || "",
    username: payload?.username || fallback.username || "",
    name: payload?.name || fallback.name || "사용자",
    email: payload?.email || fallback.email || "",
    picture: payload?.profile_image || fallback.picture || null,
    age: payload?.age ?? fallback.age ?? null,
    gender: payload?.gender || fallback.gender || null,
    phone: payload?.phone || fallback.phone || null,
    profileChunk: payload?.profile_chunk || fallback.profileChunk || null
  };
}
