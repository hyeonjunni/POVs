from app.ai.providers.base import InferenceRequest, InferenceResponse


class MockProvider:
    provider_name = "mock"

    def generate(self, request: InferenceRequest) -> InferenceResponse:
        evidence = request.prompt_package.get("retrieved_evidence", [])
        if evidence and evidence[0] != "- 검색된 개인 또는 가족 기록이 없습니다.":
            body = "검색된 기록을 바탕으로 요약 응답을 생성했습니다."
        else:
            body = "아직 연결된 근거 기록이 적어 구조 데모 중심으로 응답합니다."

        return InferenceResponse(
            provider=self.provider_name,
            model_id=request.model_id,
            output_text=f"{body} 질문: {request.user_query}",
            mode="mock",
        )
