const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);

function isUiPreviewAvailable({ development, platform, hostname } = {}) {
  return development === true && platform === "web" && LOOPBACK_HOSTS.has(hostname);
}

function enterUiPreview(environment, enter) {
  if (!isUiPreviewAvailable(environment)) return false;
  enter();
  return true;
}

function createPreviewIdentity() {
  const user = {
    id: "ui-preview-user", name: "테스트 사용자", email: "preview@example.invalid",
    age: 24, gender: "기타", phone: "01000000000", picture: null,
  };
  const family = {
    id: "ui-preview-room", name: "테스트 가족방", code: "UI-PREVIEW", role: "방장",
    createdAt: "테스트 화면", ownerUserId: user.id,
    members: [{ user_id: user.id, name: user.name, email: user.email, role: "owner",
      age: user.age, gender: user.gender, phone: user.phone, profile_image: null }],
  };
  return { user, family };
}

function createPreviewRecord(draft, sequence, createdAt) {
  const type = draft?.type;
  const title = typeof draft?.title === "string" ? draft.title.trim() : "";
  if (!["text", "image", "video", "voice"].includes(type) || !title || !Number.isSafeInteger(sequence) || sequence < 1) return null;
  return {
    id: `ui-preview-record-${sequence}`, type, title,
    detail: typeof draft.detail === "string" ? draft.detail.trim() : "",
    tags: Array.isArray(draft.tags) ? [...new Set(draft.tags.filter(tag => typeof tag === "string" && tag.trim()).map(tag => tag.trim()))] : [],
    createdAt, hasFile: false, fileUrl: null, fileId: null, mimeType: null,
  };
}

module.exports = { isUiPreviewAvailable, enterUiPreview, createPreviewIdentity, createPreviewRecord };
