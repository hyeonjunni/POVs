# Ambient Legacy Backend API Spec Draft

## 공통 원칙

- 응답 포맷은 JSON
- 인증은 초기에는 Google 로그인 후 발급한 사용자 식별값 기준으로 처리
- 이후 JWT 또는 세션 토큰 방식으로 확장

## 1. Auth

### POST `/auth/google`

- 목적: Google 로그인 후 사용자 정보를 서버에 동기화
- 요청 예시

```json
{
  "google_sub": "1234567890",
  "email": "user@example.com",
  "name": "홍길동",
  "profile_image": "https://example.com/profile.jpg"
}
```

- 응답 예시

```json
{
  "user_id": "uuid",
  "email": "user@example.com",
  "name": "홍길동"
}
```

## 2. Family

### POST `/families`

- 목적: 가족방 생성

```json
{
  "owner_user_id": "uuid",
  "name": "우리 가족방"
}
```

### POST `/families/join`

- 목적: 초대 코드로 가족방 입장

```json
{
  "user_id": "uuid",
  "invite_code": "A1B2C3"
}
```

### GET `/families/{room_id}`

- 목적: 가족방 상세 조회

### GET `/families/{room_id}/members`

- 목적: 가족방 멤버 목록 조회

## 3. Upload

### POST `/uploads`

- 목적: 업로드 메타데이터 등록

```json
{
  "room_id": "uuid",
  "uploader_user_id": "uuid",
  "type": "image",
  "title": "제주도 가족여행 사진",
  "description": "2018년 여름"
}
```

### POST `/uploads/{upload_id}/file`

- 목적: 파일 업로드 연결 정보 저장 또는 업로드 URL 발급

```json
{
  "storage_bucket": "ambient-legacy",
  "storage_path": "rooms/room-id/uploads/file.jpg",
  "mime_type": "image/jpeg",
  "file_size": 123456,
  "encrypted": true
}
```

### GET `/uploads/{room_id}`

- 목적: 가족방 기준 업로드 목록 조회

### GET `/uploads/detail/{upload_id}`

- 목적: 단일 업로드 상세 조회

## 4. 이후 확장 API

### GET `/ai/models`

- 목적: 앱이 선택 가능한 모델 목록을 서버 기준으로 조회

### GET `/ai/personas`

- 목적: 앱이 선택 가능한 페르소나 목록을 서버 기준으로 조회

### GET `/ai/runtime-status`

- 목적: 현재 provider endpoint 설정 여부와 AI 런타임 상태를 확인

### POST `/ai/chat-demo`

- 목적: 선택 모델, 선택 페르소나, 가족방 기록을 기준으로 prompt package를 조립하고 provider adapter를 통해 응답 생성

```json
{
  "room_id": "uuid",
  "user_id": "uuid",
  "model_id": "gemma-4-e2b-device",
  "persona_id": "father-calm",
  "query": "할아버지가 예전에 하셨던 조언을 요약해줘."
}
```

### POST `/ai/demo-bootstrap`

- 목적: 목요일 데모 시연용 가족방과 샘플 기록을 한 번에 생성

```json
{
  "user_id": "uuid"
}
```

응답 예시:

```json
{
  "answer": "생성된 응답",
  "answer_source": "fallback",
  "inference_source": "device",
  "provider_name": "gemma",
  "provider_mode": "remote",
  "provider_output_preview": "생성된 응답 일부",
  "selected_model": {
    "id": "gemma-4-e2b-device"
  },
  "retrieved_evidence": [
    "- [image] 2026-04-28T12:00:00: 2024 가족 송년회"
  ],
  "persona_preview": "# Identity ...",
  "prompt_package": {}
}
```

### POST `/pipeline/stt`

- 목적: Whisper 기반 STT 처리

### POST `/pipeline/ocr`

- 목적: OCR 처리

### POST `/pipeline/tagging`

- 목적: EXAONE 또는 경량 모델 기반 태깅 및 요약

### POST `/chat/query`

- 목적: RAG + EXAONE 기반 질의응답

## 구현 우선순위

1. `/auth/google`
2. `/families`
3. `/families/join`
4. `/families/{room_id}`
5. `/families/{room_id}/members`
6. `/uploads`
7. `/uploads/{room_id}`
