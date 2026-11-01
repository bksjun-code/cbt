from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


DEV_SECRET = "dev-only-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/cbt.db"
    secret_key: SecretStr = SecretStr(DEV_SECRET)
    cookie_secure: bool = True  # 로컬 HTTP 개발 시 COOKIE_SECURE=false

    scheduler_enabled: bool = True  # 만료 시험 자동 제출 작업
    session_max_age_sec: int = 8 * 3600

    login_max_failures: int = 5
    login_lock_minutes: int = 10

    # 메일(SMTP): 가입 승인·비밀번호 재설정 메일. 587=STARTTLS, 465=SSL
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_timeout_sec: int = 15
    # 메일 속 링크의 기준 주소(예: https://cbt.example.com). 요청의 Host 헤더로 만들지 않는다(재설정 링크 위조 방지)
    public_base_url: str | None = None
    password_reset_minutes: int = 30
    password_reset_max_per_hour: int = 3

    # 합격증
    certificate_issuer: str = "Rockplace DI팀"


@lru_cache
def get_settings() -> Settings:
    return Settings()
