export function buildProfileDraft(user) {
  return {
    name: user?.name || "",
    age: user?.age ? String(user.age) : "",
    gender: user?.gender || "",
    phone: user?.phone || "",
    email: user?.email || ""
  };
}
export function isProfileComplete(user) {
  return Boolean(user?.age && user?.gender && user?.phone);
}
export function isProfileFormComplete(draft, mode = "complete") {
  const normalizedAge = draft?.age?.trim() ? Number.parseInt(draft.age.trim(), 10) : null;
  const hasCoreFields = Boolean(Number.isFinite(normalizedAge) && normalizedAge > 0 && draft?.gender?.trim() && draft?.phone?.trim());
  if (mode === "edit") {
    return Boolean(hasCoreFields && draft?.name?.trim() && draft?.email?.trim());
  }
  return hasCoreFields;
}
