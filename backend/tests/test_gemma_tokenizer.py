import struct

import pytest

from app.ai import gemma_tokenizer as gt

BYTES = [f"<0x{b:02X}>" for b in range(256)]
SPECIALS = ["<pad>", "<eos>", "<bos>", "<unk>", "<|turn>", "<turn|>"]
NORMAL = ["a", "b", "c", "ab", "abc", "aa", "▁", "\n", "\n\n", "a\n"]
MERGES = ["a b", "ab c", "a a", "a aa", "a \n"]


def tiny():
    tokens = SPECIALS + BYTES + NORMAL
    types = [3] * len(SPECIALS) + [6] * len(BYTES) + [1] * len(NORMAL)
    return tokens, types, MERGES


def test_bpe_merges_byte_fallback_and_specials_on_tiny_vocab():
    tok = gt.GemmaTokenizer(*tiny())
    assert tok.count("") == 0
    assert tok.count("abc") == 1
    assert tok.count("ab abc") == 3  # ab ▁ abc: rank order, not left-to-right greed
    assert tok.count("aaa") == 2  # leftmost tie wins -> aa a; "a aa" never fires
    assert tok.count("d") == 1 and tok.count("é") == 2  # <0xXX> per UTF-8 byte
    assert tok.count("a<|turn>b<bos>") == 4
    assert tok.count("\n\n") == 1 and tok.count("\n\n\n") == 3
    assert tok.count("a\n") == 2  # merges never cross the newline split


def write_gguf(path, kv):
    def string(s):
        raw = s.encode()
        return struct.pack("<Q", len(raw)) + raw

    out = b"GGUF" + struct.pack("<IQQ", 3, 0, len(kv))
    for key, value in kv.items():
        out += string(key)
        if isinstance(value, str):
            out += struct.pack("<I", 8) + string(value)
        elif isinstance(value[0], str):
            out += struct.pack("<IIQ", 9, 8, len(value)) + b"".join(map(string, value))
        else:
            out += struct.pack(f"<IIQ{len(value)}i", 9, 5, len(value), *value)
    path.write_bytes(out)


def test_gguf_header_reader_and_env_override(tmp_path, monkeypatch):
    tokens, types, merges = tiny()
    path = tmp_path / "tiny.gguf"
    write_gguf(path, {"general.architecture": "gemma4", "tokenizer.ggml.tokens": tokens,
        "tokenizer.ggml.scores_unused": [0, 1], "tokenizer.ggml.token_type": types,
        "tokenizer.ggml.merges": merges})
    monkeypatch.setenv("AMBIENT_GEMMA_GGUF", str(path))
    kv = gt.read_gguf_kv(gt.gguf_path())
    assert kv["tokenizer.ggml.tokens"] == tokens and list(kv["tokenizer.ggml.token_type"]) == types
    assert gt.GemmaTokenizer(*(kv[k] for k in gt._KEYS)).count("ab abc") == 3


# Expected values are DGX prompt_eval_count (raw /api/generate) minus the BOS.
@pytest.mark.skipif(not gt.available(), reason="gemma4:31b GGUF not present")
@pytest.mark.parametrize("text, expected", [
    ("할머니께서는 어제 시장에서 사과를 사셨고, 저는 동생과 함께 공원에 갔습니다.", 25),
    ("여행 기간: 2025-08-15 ~ 2025-08-17 (2박 3일)", 33),
    ("헤모글로빈 12.7 g/dL, 공복혈당 98 mg/dL, LDL 131 mg/dL", 33),
    ("생일 축하해 🎂🎉 사랑해 ❤️ 👨‍👩‍👧‍👦 가족 최고 👍🏽", 23),
    ("텍스트 중간에 <|turn> 특수토큰과 <turn|> 그리고 <|\"|> 가 있다", 18),
    ("\n" * 36, 2),
    ("<bos><|turn>system\n<turn|>\n<|turn>user\n<turn|>\n<|turn>model\n<|channel>thought\n<channel|>",
     gt.CHAT_OVERHEAD),
])
def test_matches_runtime_counts(text, expected):
    assert gt.count_tokens(text) == expected
