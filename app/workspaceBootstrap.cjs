async function loadWorkspacePreservingSession(loadWorkspace, onFailure = () => {}) {
  try {
    return { ok: true, value: await loadWorkspace() };
  } catch (error) {
    onFailure(error);
    return { ok: false, error };
  }
}

module.exports = { loadWorkspacePreservingSession };
