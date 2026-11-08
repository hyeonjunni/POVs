function createMultipartTransport({fetchImpl = globalThis.fetch, multipartFetch, readUriBytes,
  createFormData = () => new FormData()}) {
  return (url, options = {}) => {
    if (typeof options.body?.getParts !== 'function') return fetchImpl(url, options);

    // RN Android/OkHttp adds a per-part Content-Length, rejected by the frozen
    // backend. Expo serializes byte-backed parts with only disposition and type,
    // and generates the matching outer Content-Type/boundary itself.
    const body = createFormData();
    for (const part of options.body.getParts()) {
      if (part.uri) {
        body.append(part.fieldName, {
          name: part.name,
          type: part.type,
          bytes: () => readUriBytes(part.uri),
        });
      } else {
        body.append(part.fieldName, part.blob ?? part.string);
      }
    }
    return multipartFetch(url, {...options, body});
  };
}

async function readUploadBytes(uri, {File, cache, copyAsync, randomUUID}) {
  if (!uri.startsWith('content://')) return new File(uri).bytes();
  // File.bytes treats content URIs as SAF documents. Legacy copyAsync instead
  // opens generic MediaStore URIs through ContentResolver, preserving support.
  const temporary = new File(cache, `multipart-${randomUUID()}`);
  try {
    await copyAsync({from: uri, to: temporary.uri});
    return await temporary.bytes();
  } finally {
    if (temporary.exists) temporary.delete();
  }
}

module.exports = {createMultipartTransport, readUploadBytes};
