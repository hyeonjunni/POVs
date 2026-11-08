"""Isolated worker entrypoint; no app imports, network STT or application CLI.

Invoked by the admitted provider with a minimal environment and a hard deadline.
Only machine-readable output reaches stdout; stderr is discarded by the parent.
"""
from __future__ import annotations

import base64
import http.client
import io
import json
import sys
import wave


def visual(payload):
    connection = http.client.HTTPConnection(payload["host"], payload["port"], timeout=payload["timeout"])
    try:
        connection.request("POST", "/api/chat", json.dumps(payload["body"]), {"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(900001)
        if response.status != 200 or len(raw) > 900000:
            raise ValueError()
        return json.loads(raw)
    finally:
        connection.close()


def speech(payload):
    import numpy as np
    import torch
    from transformers import WhisperForConditionalGeneration, WhisperProcessor

    with wave.open(io.BytesIO(base64.b64decode(payload["audio"], validate=True))) as stream:
        width, channels, rate = stream.getsampwidth(), stream.getnchannels(), stream.getframerate()
        if stream.getcomptype() != "NONE" or width not in (1, 2, 3, 4) or channels not in (1, 2) or not 8000 <= rate <= 48000:
            return {"error": "UNSUPPORTED_MEDIA"}
        raw = stream.readframes(stream.getnframes())
    if width == 1:
        audio = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128
    elif width == 3:
        octets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        integers = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
        audio = ((integers ^ 0x800000) - 0x800000).astype(np.float32) / 8388608
    else:
        audio = np.frombuffer(raw, dtype="<i2" if width == 2 else "<i4").astype(np.float32) / (2 ** (width * 8 - 1))
    audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        from torchaudio import functional
        audio = functional.resample(torch.from_numpy(audio), rate, 16000).numpy()
    processor = WhisperProcessor.from_pretrained(payload["model"], local_files_only=True)
    model = WhisperForConditionalGeneration.from_pretrained(payload["model"], local_files_only=True).eval().to(payload["device"])
    inputs = processor(audio, sampling_rate=16000, return_tensors="pt", return_attention_mask=True,
                       truncation=False, padding="longest")
    with torch.inference_mode():
        generated = model.generate(**{k: v.to(payload["device"]) for k, v in inputs.items()},
            language=payload["language"] or None, task="transcribe", return_timestamps=True,
            return_segments=True)
    if not isinstance(generated, dict):
        raise TypeError()
    segments = []
    for segment in generated["segments"][0]:
        if not isinstance(segment, dict):
            raise TypeError()
        text = processor.decode(segment["tokens"], skip_special_tokens=True).strip()
        if text:
            segments.append({"start": float(segment["start"]), "end": float(segment["end"]), "text": text})
    # Unknown stays unknown; never infer event/recorded time from the filesystem.
    return {"text": " ".join(s["text"] for s in segments), "language": payload["language"] or "und", "segments": segments}


if __name__ == "__main__":
    raw = sys.stdin.buffer.read(30_000_001)
    if len(raw) > 30_000_000:
        raise ValueError()
    data = json.loads(raw)
    result = {"visual": visual, "speech": speech}[sys.argv[1]](data)
    output = json.dumps(result, allow_nan=False)
    if len(output.encode()) > 900000:
        raise ValueError()
    sys.stdout.write(output)
