from sqlalchemy.dialects import postgresql

from conftest import postgres_url_or_skip
from app.services.retrieval import vector_statement


def test_postgres_vector_statement_is_exact_room_and_model_scoped():
    postgres_url_or_skip()
    statement = vector_statement(room_id="room-1", model_key="model-1", query_vector=[1.0] * 1024, k=5)
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    assert "record_chunks.room_id" in sql
    assert "record_embeddings.model_key" in sql
    assert "<=>" in sql
    assert "record_chunks.id" in sql
