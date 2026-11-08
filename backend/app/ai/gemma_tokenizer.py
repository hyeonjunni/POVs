"""Stdlib token counter for gemma4:31b, as tokenized by the DGX runtime.

Ollama 0.30.8 serves GGUF models through llama-server (llama.cpp b9509), so this
mirrors llama.cpp's gemma4 BPE: special tokens are cut out first (longest text
first), spaces become ``▁``, raw text splits into newline / non-newline runs,
merges apply by rank (leftmost on ties) and symbols missing from the vocab fall
back to ``<0xXX>`` byte tokens.

count_tokens() excludes the BOS the runtime always prepends. Measured on DGX:
  /api/generate raw: prompt_eval_count = count_tokens(prompt) + RAW_OVERHEAD
  /api/chat, system + user, think=false (with or without format=json):
    prompt_eval_count = count_tokens(system.strip()) + count_tokens(user.strip()) + CHAT_OVERHEAD
  The 18 is "<bos><|turn>system\n<turn|>\n<|turn>user\n<turn|>\n<|turn>model\n"
  plus the empty "<|channel>thought\n<channel|>" block Ollama's gemma4 renderer
  adds when thinking is off; the renderer trims message content.

The tokenizer tables are read lazily from the GGUF header only; tensor data is
never touched.
"""
import functools
import heapq
import json
import mmap
import os
from pathlib import Path
import re
import struct

RAW_OVERHEAD = 1
CHAT_OVERHEAD = 18

_MANIFEST = "manifests/registry.ollama.ai/library/gemma4/31b"
_KEYS = ("tokenizer.ggml.tokens", "tokenizer.ggml.token_type", "tokenizer.ggml.merges")
_SCALARS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
_SPECIAL_TYPES = {2, 3, 4}  # unknown, control, user-defined
_WORDS = re.compile(r"[^\n]+|\n+")


def gguf_path():
    if override := os.environ.get("AMBIENT_GEMMA_GGUF"):
        return Path(override)
    models = Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models")
    manifest = json.loads((models / _MANIFEST).read_text())
    digest = next(layer["digest"] for layer in manifest["layers"] if layer["mediaType"].endswith(".model"))
    return models / "blobs" / digest.replace(":", "-")


def read_gguf_kv(path, wanted=_KEYS):
    """Return the wanted key/value entries from a GGUF v2/v3 header."""
    with open(path, "rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data:
        if data[:4] != b"GGUF":
            raise ValueError("not a GGUF file")
        n_kv = struct.unpack_from("<Q", data, 16)[0]
        pos, found = 24, {}

        def string():
            nonlocal pos
            n = struct.unpack_from("<Q", data, pos)[0]
            pos += 8 + n
            return data[pos - n:pos].decode("utf-8", "replace")

        def value(kind, keep):
            nonlocal pos
            if kind == 8:
                return string()
            if kind == 9:
                item, n = struct.unpack_from("<IQ", data, pos)
                pos += 12
                if item == 8:
                    return [string() for _ in range(n)]
                fmt = f"<{n}{_SCALARS[item]}"
                items = struct.unpack_from(fmt, data, pos) if keep else None
                pos += struct.calcsize(fmt)
                return items
            fmt = "<" + _SCALARS[kind]
            scalar = struct.unpack_from(fmt, data, pos)[0]
            pos += struct.calcsize(fmt)
            return scalar

        for _ in range(n_kv):
            key = string()
            kind = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            found[key] = value(kind, key in wanted)
            if all(k in found for k in wanted):
                break
        return {k: found[k] for k in wanted}


class GemmaTokenizer:
    def __init__(self, tokens, token_types, merges):
        self.vocab = set(tokens)
        self.ranks = {}
        for rank, merge in enumerate(merges):  # llama.cpp keeps the first rank of a duplicate
            self.ranks.setdefault(merge, rank)
        specials = [t for t, kind in zip(tokens, token_types) if kind in _SPECIAL_TYPES and t]
        self.specials = sorted(specials, key=len, reverse=True)
        self.byte_tokens = [f"<0x{b:02X}>" in self.vocab for b in range(256)]
        self._count_word = functools.lru_cache(maxsize=8192)(self._bpe_count)

    def count(self, text):
        parts = [text]
        for special in self.specials:
            split = []
            for part in parts:
                if part is None or special not in part:
                    split.append(part)
                    continue
                for i, piece in enumerate(part.split(special)):
                    if i:
                        split.append(None)
                    if piece:
                        split.append(piece)
            parts = split
        return sum(
            1 if part is None else sum(map(self._count_word, _WORDS.findall(part.replace(" ", "▁"))))
            for part in parts
        )

    def _bpe_count(self, word):
        if word[0] == "\n" and word in self.vocab:
            return 1
        sym = list(word)
        n = len(sym)
        nxt = list(range(1, n + 1))
        prv = list(range(-1, n - 1))
        heap = []

        def push(left, right):
            if left >= 0 and right < n:
                rank = self.ranks.get(sym[left] + " " + sym[right])
                if rank is not None:
                    heapq.heappush(heap, (rank, left, right, sym[left] + sym[right]))

        for i in range(n - 1):
            push(i, i + 1)
        while heap:
            _, left, right, merged = heapq.heappop(heap)
            if not sym[left] or not sym[right] or sym[left] + sym[right] != merged:
                continue  # stale bigram
            sym[left], sym[right] = merged, ""
            nxt[left] = nxt[right]
            if nxt[right] < n:
                prv[nxt[right]] = left
            push(prv[left], left)
            push(left, nxt[left])
        return sum(
            1 if s in self.vocab else sum(self.byte_tokens[b] for b in s.encode())
            for s in sym if s
        )


@functools.cache
def _tokenizer():
    kv = read_gguf_kv(gguf_path())
    return GemmaTokenizer(*(kv[k] for k in _KEYS))


def available():
    try:
        _tokenizer()
        return True
    except Exception:
        return False


def count_tokens(text: str) -> int:
    return _tokenizer().count(text)
