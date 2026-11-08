# Provider Contracts

## 목적

이 문서는 Ambient Legacy 백엔드가 모델별 추론 엔드포인트를 어떤 형식으로 호출하는지 정리한다.

## 1. Gemma Provider Contract

엔드포인트 예시:

```text
POST {GEMMA_ENDPOINT_URL}
```

요청 본문:

```json
{
  "model_id": "gemma-4-e2b-device",
  "input": "instructions + persona markdown + retrieved evidence + user query"
}
```

응답 예시:

```json
{
  "output_text": "생성된 답변"
}
```

허용 대체 키:

- `output_text`
- `text`
- `response`

## 2. EXAONE Provider Contract

엔드포인트 예시:

```text
POST {EXAONE_ENDPOINT_URL}
```

요청 본문:

```json
{
  "model": "exaone-family-vault",
  "messages": [
    {
      "role": "system",
      "content": "Use retrieved memories as primary evidence."
    },
    {
      "role": "user",
      "content": "Persona Markdown + Retrieved Evidence + User Query"
    }
  ]
}
```

응답 예시:

```json
{
  "choices": [
    {
      "message": {
        "content": "생성된 답변"
      }
    }
  ]
}
```

허용 대체 키:

- `output_text`
- `text`
- `response`
- `choices[0].message.content`

## 3. Mock Server

로컬 wiring 테스트용 mock server:

```bash
python3 backend/scripts/mock_model_server.py
```

추천 환경변수:

```env
GEMMA_ENDPOINT_URL=http://127.0.0.1:9001/generate
EXAONE_ENDPOINT_URL=http://127.0.0.1:9001/chat
AI_PROVIDER_TIMEOUT_SECONDS=30
```

## 4. 호출 흐름

```text
app/App.js
-> POST /api/v1/ai/chat-demo
-> prompt_package 조립
-> model_id 기준 provider 선택
-> provider endpoint 호출
-> output_text 정규화
-> app에 answer + provider 상태 + evidence 반환
```

## 5. Ollama Direct Contract

Gemma를 맥북에서 가장 빠르게 붙이는 경로는 Ollama다.

엔드포인트:

```text
POST {OLLAMA_BASE_URL}/api/chat
```

요청 본문:

```json
{
  "model": "gemma4:e2b",
  "stream": false,
  "messages": [
    {
      "role": "system",
      "content": "Use retrieved memories as primary evidence."
    },
    {
      "role": "user",
      "content": "Persona Markdown + Retrieved Evidence + User Query"
    }
  ]
}
```

응답 예시:

```json
{
  "message": {
    "content": "생성된 응답"
  }
}
```
