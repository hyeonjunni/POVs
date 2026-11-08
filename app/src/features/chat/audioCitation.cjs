function audioCitationRanges(citation) {
  const locator = citation?.source_locator;
  if (locator?.kind !== "multimodal" || locator.media_type !== "AUDIO" || !Array.isArray(locator.anchors)) return [];
  return locator.anchors.filter(anchor => anchor.kind === "AUDIO" &&
    Number.isSafeInteger(anchor.start_ms) && anchor.start_ms >= 0 &&
    Number.isSafeInteger(anchor.end_ms) && anchor.end_ms > anchor.start_ms)
    .map(anchor => ({ start: anchor.start_ms / 1000, end: anchor.end_ms / 1000 }));
}
function audioCitationLocation(citation) {
  const ranges = audioCitationRanges(citation);
  const clock = seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
  return ranges.length ? `음성 구간 ${ranges.slice(0, 3).map(range => `${clock(range.start)}–${clock(range.end)}`).join(", ")}${ranges.length > 3 ? " 외" : ""}` : null;
}
module.exports = { audioCitationRanges, audioCitationLocation };
