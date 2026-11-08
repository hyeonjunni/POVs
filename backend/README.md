# Ambient Legacy Backend

이 디렉터리는 Ambient Digital Legacy 프로젝트의 1차 백엔드 설계와 FastAPI 스캐폴드를 담는다.

## 목표

- 모바일 앱이 붙을 실제 API 서버 뼈대 구성
- 사용자, 가족방, 업로드 메타데이터를 서버 기준으로 관리
- Google Cloud Storage 및 Google Cloud SQL 연동을 위한 구조 선행 정리
- 이후 Whisper, OCR, EXAONE, RAG 파이프라인을 붙일 허브 역할 준비

## 현재 포함 내용

- `docs/erd.md`: 1차 ERD 초안
- `docs/api_spec.md`: 1차 API 명세
- `docs/provider_contracts.md`: Gemma / EXAONE provider 호출 계약
- `app/`: FastAPI 백엔드 스캐폴드
- `requirements.txt`: 초기 의존성 목록
- `.env.example`: Cloud SQL 및 GCS 환경 변수 예시

## 우선 구현 순서

1. 데이터베이스 연결 설정
2. 사용자 로그인 동기화 API
3. 가족방 생성/입장 API
4. 업로드 메타데이터 저장 API
5. Google Cloud Storage 연동

## Cloud SQL 연결 방향

현재 백엔드는 Google Cloud SQL for PostgreSQL 연결을 기준으로 설계한다.

- 로컬 개발: Cloud SQL Python Connector 또는 로컬 PostgreSQL
- 배포 환경: Cloud Run + Cloud SQL for PostgreSQL
- 인증: Application Default Credentials 또는 서비스 계정

공식 문서:

- Cloud SQL Python Connector: https://cloud.google.com/sql/docs/postgres/connect-connectors
- Cloud Run에서 Cloud SQL 연결: https://cloud.google.com/sql/docs/postgres/connect-run

## 미디어 저장 경계

LEGACY 업로드는 항상 로컬에 저장한다. 기존 `USE_GCS_MEDIA_STORAGE` 설정은
호환성을 위해 받지만 새 업로드를 GCS로 전환하지 않으며, 활성 값은 경고 로그를 남긴다.
로컬 저장 실패는 오류로 반환하고 원격 저장으로 우회하지 않는다.
GCP 상태와 AI 상태의 `gcs_media_storage_enabled`는 LEGACY 업로드에 대해 항상 false다.
기존 평문 GCS smoke-test API는 409로 거부한다.

- `USE_GCS_MEDIA_STORAGE`: 폐기된 LEGACY 쓰기 옵션. 기존 GCS 원본 읽기·삭제에만 유지.
- `GCS_BUCKET_NAME`: 기존 LEGACY GCS 원본의 locator 검증·읽기·삭제용.
- `GCP_PROJECT_ID`, `GCP_CREDENTIALS_PATH`: 기존 GCS 접근 및 DB 초기화와 공유.
- SECURE 암호화 저장소 설정과 ciphertext 어댑터의 계약은 별도로 유지한다.

SECURE GCS 경로에는 로컬에서 암호화한 ciphertext만 전달한다. 이 변경은
기존 원격 평문 객체를 이동하거나 삭제하지 않으며, 기존 locator의 보호된 읽기와
삭제를 유지한다. 새 LEGACY 업로드의 원격 평문 생성을 차단하는 변경이다.
