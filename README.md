# POVs

POVs(내부 프로젝트명 Ambient Legacy)는 가족이 사진·영상·음성·글 기록을 가족방 단위로 모으고 저장된 기록을 근거로 질문에 답하는 가족 기록 보관 앱입니다. 모바일/웹 앱(Expo)과 API 서버(FastAPI)로 구성됩니다.

> 이 저장소는 비공개 개발 저장소의 **정리된 공개용 스냅샷**입니다. 이전 커밋 이력과 데이터셋·모델·자격 증명·운영 설정은 제외했습니다(아래 "포함하지 않은 것" 참고).

## 저작권과 라이선스

이 저장소에는 라이선스 파일이 없습니다. 별도 라이선스가 명시되기 전까지 코드의 사용·수정·재배포 권한은 저작권자에게 있으며, 공개되었다는 사실만으로 오픈소스 라이선스가 부여되지는 않습니다.

- 서드파티 의존성은 각 패키지의 라이선스를 따릅니다(`app/package-lock.json`, `backend/requirements.txt`).
- `app/android/gradlew`, `gradlew.bat`, `gradle-wrapper.jar`는 Gradle 프로젝트 파일이며 원래의 Apache-2.0 고지를 유지합니다.
- 앱 아이콘의 출처와 생성 방식은 `app/assets/branding/README.md`에 있습니다.

## 기술 스택

| 영역 | 구성 |
|---|---|
| 앱 | Expo SDK 54, React Native 0.81, React 19, react-native-web, AsyncStorage, expo-image-picker / expo-video / expo-audio |
| 백엔드 | Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Pydantic Settings, PyJWT, cryptography |
| DB | PostgreSQL 및 SQLite 지원. 아래 공개본 기동 검증은 SQLite 기준입니다. pgvector 경로는 별도 검증이 필요합니다 |
| 저장소 | 로컬 파일 저장(기본). Google Cloud Storage 어댑터는 SECURE 모드의 암호문 저장과 과거 LEGACY 객체 읽기·삭제용으로 사용합니다 |
| AI(선택) | Ollama 호환 로컬 LLM(Gemma/EXAONE/Qwen 계열 모델 이름 설정), 로컬 OCR 실행 파일, 로컬 STT 실행 파일, 선택적 임베딩 엔드포인트 |

## 구조

```text
app/                    Expo 앱
  App.js                진입점
  src/features/         화면·기능별 모듈 (auth, chat, records, profile, preview 등)
  src/shared/           API 클라이언트·테마·공통 UI
  *.cjs / *.test.cjs    네트워크·업로드·세션 등 순수 로직과 node:test 테스트
  android/              네이티브 Android 프로젝트 (서명 키 미포함)
backend/
  app/main.py           FastAPI 앱 (LEGACY / SECURE 모드별 라우터 구성)
  app/api/routes/       auth, families, uploads, records, processing, retrieval, ask(근거 기반 답변) 등
  app/ai/               질의 라우팅·결정론 게이트(gates/)·근거 기반 답변·모델 레지스트리
  app/services/         기록 수명주기, 권한·공개 범위, 검색 색인, 처리 작업, SECURE 런타임
  app/storage/          로컬/GCS 저장, 암호화, 키 보관
  alembic/              DB 마이그레이션
  scripts/              마이그레이션 적용, 게이트 자가 테스트, 회귀 점검 스크립트
  tests/                pytest 테스트 (합성 픽스처만 사용)
```

흐름: 앱은 백엔드 API만 호출합니다. 백엔드가 인증(JWT), 가족방 소속·기록 공개 범위 검사, 미디어 전달을 담당합니다. 질문 답변에는 접근 가능한 기록을 검색해 근거와 원본 인용을 제공합니다. 질문 유형별 규칙 기반 경로와 LLM 생성 경로가 있으며 게이트 적용 범위는 경로·설정에 따라 다릅니다. 근거 기반으로 생성한 답변도 사실 정확성은 원본 기록과 대조해야 합니다.

## 실행 방법

### 1. 백엔드

```sh
cd backend
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env`에서 최소한 다음을 채웁니다.

- `JWT_SECRET_KEY`: 32바이트 이상의 임의 값 (예: `python -c "import secrets; print(secrets.token_urlsafe(48))"`). 비어 있거나 약하면 서버가 시작되지 않습니다.
- `DATABASE_URL`: 로컬 PostgreSQL 주소. 간단히 확인할 때는 `sqlite:///./local.db`도 동작합니다.

```sh
python scripts/adopt_alembic.py          # 마이그레이션 적용
uvicorn app.main:app --host 127.0.0.1 --port 8002
curl http://127.0.0.1:8002/api/v1/system/health/db   # {"database":"connected"}
```

실기기에서 접속하려면 서버를 기기에서 접근 가능한 주소로 띄우고(`--host 0.0.0.0` 등) 앱의 API 주소를 그 주소로 바꿉니다. 외부 네트워크에 노출할 때는 TLS와 방화벽을 직접 구성해야 합니다.

### 2. 앱

```sh
cd app
npm ci
cp .env.example .env.local
# EXPO_PUBLIC_API_BASE_URL=http://<백엔드 주소>:8002/api/v1
npx expo start          # 웹/개발 서버
npx expo run:android    # Android 개발 빌드 (Android SDK 필요)
```

- `EXPO_PUBLIC_*` 값은 앱 번들에 그대로 들어갑니다. 비밀번호 등 비밀 값은 서버에서 관리하세요.
- 프로필 바로 로그인(`EXPO_PUBLIC_ENABLE_PROFILE_LOGIN`)은 시연용 기능이며 기본값은 꺼짐입니다.
- Android 패키지 이름은 공개용 자리표시자 `com.example.povs`입니다. 배포하려면 본인 패키지 이름으로 바꾸세요.
- 릴리스 서명 키와 디버그 키스토어는 별도로 준비해야 합니다. 릴리스 빌드는 `app/android/gradle.properties`의 `MYAPP_UPLOAD_*` 값을 로컬에서 직접 설정해야 합니다. EAS 프로젝트 ID도 제거했으므로 EAS를 쓰려면 `eas init`으로 새로 연결해야 합니다.

### 3. 테스트

```sh
# 앱 (node:test)
cd app && npm test

# 백엔드 (합성 서명 키를 환경 변수로 제공)
cd backend
JWT_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" python -m pytest tests -q
python scripts/selftest_gates.py
```

## 구현된 것과 기본으로 꺼져 있는 것

구현됨(로컬에서 외부 서비스 없이 동작):

- 아이디/비밀번호 회원가입·로그인(JWT), 프로필, 계정 삭제
- 가족방 생성·초대 코드 참여·삭제, 구성원 목록과 가족 관계 정보
- 글·사진·영상·음성 기록 업로드와 조회, 기록별 공개 범위와 수명주기(보관·삭제 요청)
- 가족방 단위 기록 검색(BM25 기반 색인)과 기록 근거를 인용하는 질문 답변 API
- 인증·가족방 권한·PRIVATE 접근 검사, 질문 유형별 근거 검사와 회귀 테스트

설정해야 동작하거나 기본으로 꺼져 있음:

- LLM 답변 생성: Ollama 호환 서버와 모델이 필요합니다. 모델 레지스트리에는 이름을 설정하며 모델 파일·어댑터는 별도로 준비합니다. 모델 없이도 규칙 기반 경로와 테스트는 동작합니다.
- OCR / STT / 이미지 설명: 로컬 실행 파일·모델 경로를 `.env`에 지정해야 합니다(`OCR_EXECUTABLE_PATH`, `STT_EXECUTABLE_PATH`, `MEDIA_*`).
- 벡터 검색: 임베딩 엔드포인트(`RETRIEVAL_EMBEDDING_*`)와 PostgreSQL/pgvector가 필요합니다. 이 스냅샷의 벡터 검색은 별도 검증이 필요합니다.
- SECURE 모드(`SECURITY_MODE=SECURE`): 서버 측 암호화 저장, 신뢰된 로컬 추론 프로세스, 전용 자격 증명 디렉터리를 전제로 하는 운영 모드입니다. 운영 환경 구성·배포 스크립트는 별도로 준비해야 합니다. 기본값은 `LEGACY`입니다.
- Google Cloud SQL / GCS: 연결 코드를 포함하며 기본값은 꺼져 있습니다. 새 LEGACY 업로드는 항상 로컬에 저장합니다.
- Google 로그인: 패키지 의존성을 포함합니다. 현재 앱의 로그인 흐름은 아이디/비밀번호 방식입니다.

## 시연 버전의 한계

- 질문 이해는 한국어 가족 호칭·질문 패턴 규칙과 고정 회귀 케이스를 기준으로 맞춰져 있습니다. 케이스 밖 표현에서는 답을 거절하거나 덜 정확할 수 있습니다.
- `backend/tests/family_answer_ux_cases.json` 기준 UX 계약 점검(`scripts/check_family_answer_ux.py`)에서 기준 케이스 8개가 실패합니다.
- 성능·확장성·다중 서버 운영과 외부 보안 감사는 별도 검증 과제입니다. 보안 설계는 내부 테스트를 거쳤습니다.

## 검증 상태

- **이 스냅샷(테스트한 후보)**: 비공개 저장소 커밋 `c45e229`의 추적 파일에서 만들었습니다. 공개용 정리 후 앱 테스트, 백엔드 테스트 수집, 정리에 영향을 받는 백엔드 테스트 일부, 게이트 자가 테스트, SQLite 마이그레이션·서버 기동을 확인했습니다. 이번 정리 과정의 백엔드 검증 범위는 위 항목에 한정됩니다.
- **마지막으로 알려진 운영 배포본**: 커밋 `fd8f592` 기준입니다. 이 스냅샷은 그보다 새로운 후보이며 운영에 배포되지 않았을 수 있습니다. 현재 운영 상태는 별도 확인이 필요합니다.
- 정리 전부터 있던 알려진 실패(원본에서도 똑같이 재현됨):
  - 앱 `chatKeyboardDraft.test.cjs` 2건: 테스트 환경에 `__DEV__` 전역이 정의되지 않음
  - `tests/test_m41_providers.py::test_pcm_only_scope_and_offline_provider_mapping`: 로컬 음성 인식 런타임이 없는 환경
  - `tests/test_gcs_secure_runtime.py` 2건: 공개본과 원본에서 동일 실패
  - `scripts/check_gates.py`의 골든셋 일부 항목과 E2E 단계

## 포함하지 않은 것

- **데이터셋 전체**: 시연용 가족 데이터, 이를 가공한 학습·평가 데이터, 생성 예시, 원본 미디어와 문서. 가명 처리된 데이터도 제외했습니다. 테스트는 이 저장소의 합성 픽스처만 사용하며, 시연 데이터에 의존하는 테스트는 제외했습니다.
- 모델 가중치, 파인튜닝 어댑터, 학습 스크립트
- 자격 증명, 키 파일, 서명 키스토어, 실제 `.env`, DB 덤프, 로그
- 운영·배포 환경 설정과 내부 문서, 개발 과정 기록

시연용 미디어의 제작 도구와 공개 검토 상태는 [docs/DATA_PROVENANCE.md](docs/DATA_PROVENANCE.md)에 정리했습니다.

실제로 질문 답변을 확인하려면 앱에서 직접 가족방을 만들고 기록을 올리거나, 본인이 권한을 가진 데이터를 준비해야 합니다.

<!-- HUMANIZE-SUMMARY -->
<!-- 경로: light. 주요 수정: C-11 연결어미 뒤 쉼표, A-18 긴 관형구, A-5 관련된 표현, 반복되는 부정형 안내를 준비 사항·검증 범위로 정리. 사실·수치·코드·제목·격식체 보존. 등급 B. 자체검증 6/6 통과. 변경률은 verify_gates.py 결과를 따름. -->
