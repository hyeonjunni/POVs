function serviceMedia(query, response) {
  if (response.gate_route !== "ANSWER") return [];
  if (/몇\s*(?:개|건|장|번|회)|개수|갯수|횟수|건수/.test(query) && !/보여|들려|재생|열어/.test(query)) return [];
  const types = [];
  if (/사진|이미지|결과표.*(?:보여|찾아)|photo|image/i.test(query)) types.push("image");
  if (/음성|녹음|오디오|audio|voice/i.test(query)) types.push("voice");
  if (/영상|비디오|video/i.test(query)) types.push("video");
  const records = new Map((response.query_context?.records || []).map(record => [record.record_id, record]));
  const seen = new Set();
  return (response.citations || []).flatMap(citation => {
    const record = records.get(citation.record_id);
    if (!record || !types.includes(record.record_type) || seen.has(citation.record_id)) return [];
    seen.add(citation.record_id);
    return [{ citation, record }];
  });
}

module.exports = { serviceMedia };
