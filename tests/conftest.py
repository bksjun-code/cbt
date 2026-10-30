import pytest
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db import Base, make_engine


@pytest.fixture(autouse=True)
def _isolate_settings_from_local_env(monkeypatch):
    """테스트가 개발자 PC 의 .env 나 환경변수(SECRET_KEY 등)에 따라 결과가 달라지지 않게 한다."""
    from app.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in ("SECRET_KEY", "COOKIE_SECURE", "DATABASE_URL", "SCHEDULER_ENABLED"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch):
    from app.services import security

    monkeypatch.setattr(security, "ROUNDS", 4)


@pytest.fixture()
def db():
    engine = make_engine("sqlite://")  # 인메모리 (스레드당 단일 연결 풀)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def seeded(db):
    from app.seed import run_seed

    run_seed(db, samples=True)
    return db
