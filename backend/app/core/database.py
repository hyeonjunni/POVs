import logging
import os
from pathlib import Path

from google.cloud.sql.connector import Connector, IPTypes
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.core.config import settings, validate_secure_config

connector: Connector | None = None
logger = logging.getLogger(__name__)


def _apply_google_credentials_env() -> None:
    if not settings.gcp_credentials_path:
        return

    credentials_path = Path(settings.gcp_credentials_path).expanduser()
    if credentials_path.exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(credentials_path)


def _build_engine():
    global connector
    validate_secure_config(settings)
    _apply_google_credentials_env()

    use_cloud_sql_connector = settings.use_cloud_sql_connector
    if (
        not use_cloud_sql_connector
        and settings.instance_connection_name
        and settings.db_user
        and settings.db_name
        and ("localhost" in settings.database_url or "127.0.0.1" in settings.database_url)
    ):
        use_cloud_sql_connector = True

    logger.info(
        "Database engine mode resolved: %s",
        "cloud-sql-connector" if use_cloud_sql_connector else "direct-database-url",
    )

    if use_cloud_sql_connector:
        connector = Connector(refresh_strategy="LAZY")
        ip_type = IPTypes.PRIVATE if settings.private_ip else IPTypes.PUBLIC

        def getconn():
            return connector.connect(
                settings.instance_connection_name,
                "pg8000",
                user=settings.db_user,
                password=settings.db_pass,
                db=settings.db_name,
                ip_type=ip_type,
            )

        return create_engine(
            "postgresql+pg8000://",
            creator=getconn,
            pool_pre_ping=True,
            pool_recycle=1800,
        )

    engine_kwargs = {
        "echo": False,
        "hide_parameters": settings.security_mode == "SECURE",
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }

    if settings.database_url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}

    engine = create_engine(settings.database_url, **engine_kwargs)
    if settings.database_url.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _enable_sqlite_fk(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


engine = _build_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def close_connector():
    global connector
    if connector is not None:
        connector.close()
        connector = None
