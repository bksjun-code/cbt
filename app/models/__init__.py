"""ORM 모델. 모든 JSON 컬럼은 SQLAlchemy JSON 타입(SQLite/PostgreSQL 공용)."""
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UtcDateTime(TypeDecorator):
    """항상 timezone-aware(UTC) 로 읽고 쓴다 (SQLite 는 tz 를 저장하지 않음)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime 은 저장할 수 없습니다(UTC aware 사용)")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def _enum(e: type[enum.Enum]) -> Enum:
    # 문자열 컬럼 + CHECK 제약 (SQLite/PG 이식성)
    return Enum(e, native_enum=False, length=20, validate_strings=True)


class Role(str, enum.Enum):
    ADMIN = "ADMIN"
    CANDIDATE = "CANDIDATE"


class Difficulty(str, enum.Enum):
    LOW = "LOW"
    MID = "MID"
    HIGH = "HIGH"


class ItemType(str, enum.Enum):
    SINGLE = "SINGLE"
    MULTI = "MULTI"
    SCENARIO = "SCENARIO"


class Architecture(str, enum.Enum):
    CLASSIC = "CLASSIC"
    MA = "MA"
    BOTH = "BOTH"


class ItemStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    REVIEWED = "REVIEWED"
    APPROVED = "APPROVED"
    RETIRED = "RETIRED"


class AttemptStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    EXPIRED = "EXPIRED"


class ReviewAction(str, enum.Enum):
    SUBMIT_REVIEW = "SUBMIT_REVIEW"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    RETIRE = "RETIRE"


class OverrideMode(str, enum.Enum):
    CREDIT_ALL = "CREDIT_ALL"  # 해당 문항을 모든 응시자에게 정답 인정(무응답 포함)
    EXCLUDE = "EXCLUDE"        # 채점 제외(배점·만점에서 모두 제외, 100점 환산 유지)


class UserStatus(str, enum.Enum):
    PENDING = "PENDING"    # 가입 신청 후 관리자 승인 대기(로그인 불가)
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"  # 관리자 반려(로그인 불가, 다시 신청 가능)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    login_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # 응시자는 이메일(소문자)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(100))
    role: Mapped[Role] = mapped_column(_enum(Role), default=Role.CANDIDATE)
    status: Mapped[UserStatus] = mapped_column(_enum(UserStatus), default=UserStatus.ACTIVE, server_default="ACTIVE")
    # 한 계정 한 곳 로그인: 로그인·로그아웃·비밀번호 변경 때 1 증가. 세션 쿠키의 값과 다르면 그 세션은 무효
    session_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    # 로그인 잠금(5회 실패 → 10분)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime)


class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        Index("ix_items_cell", "domain_id", "difficulty", "status"),
        Index("ix_items_variant_group", "variant_group"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id"))
    difficulty: Mapped[Difficulty] = mapped_column(_enum(Difficulty))
    item_type: Mapped[ItemType] = mapped_column(_enum(ItemType))
    stem: Mapped[str] = mapped_column(Text)
    options: Mapped[dict] = mapped_column(JSON)  # {"A": "...", ...}
    correct_keys: Mapped[list] = mapped_column(JSON)  # ["B"]
    explanation: Mapped[str] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(String(300))
    ogg_version: Mapped[str] = mapped_column(String(20), default="19c")
    architecture: Mapped[Architecture] = mapped_column(_enum(Architecture), default=Architecture.BOTH)
    status: Mapped[ItemStatus] = mapped_column(_enum(ItemStatus), default=ItemStatus.DRAFT)
    variant_group: Mapped[str] = mapped_column(String(100))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)
    # 버전 관리: 출제 이력 문항은 RETIRED 후 신규 버전 생성
    version: Mapped[int] = mapped_column(Integer, default=1)
    previous_version_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"))
    # 개발용 샘플(운영 환경에서 일괄 식별/삭제용)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    domain: Mapped[Domain] = relationship()


class ItemReview(Base):
    """검수 워크플로 이력(반려 사유 포함)."""

    __tablename__ = "item_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    action: Mapped[ReviewAction] = mapped_column(_enum(ReviewAction))
    reason: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Blueprint(Base):
    __tablename__ = "blueprints"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    total_items: Mapped[int] = mapped_column(Integer)
    time_limit_min: Mapped[int] = mapped_column(Integer)
    pass_score: Mapped[float] = mapped_column(Float, default=70.0)
    domain_min_ratio: Mapped[float] = mapped_column(Float, default=0.4)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    # 출제·채점 옵션
    recent_exclude_n: Mapped[int] = mapped_column(Integer, default=2)
    sort_by_difficulty: Mapped[bool] = mapped_column(Boolean, default=True)
    multi_partial_credit: Mapped[bool] = mapped_column(Boolean, default=False)

    rules: Mapped[list["BlueprintRule"]] = relationship(
        back_populates="blueprint", cascade="all, delete-orphan"
    )


class BlueprintRule(Base):
    __tablename__ = "blueprint_rules"
    __table_args__ = (CheckConstraint("item_count >= 0", name="ck_rule_count_nonneg"),)

    blueprint_id: Mapped[int] = mapped_column(ForeignKey("blueprints.id"), primary_key=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id"), primary_key=True)
    difficulty: Mapped[Difficulty] = mapped_column(_enum(Difficulty), primary_key=True)
    item_count: Mapped[int] = mapped_column(Integer)
    points_per_item: Mapped[float] = mapped_column(Float)

    blueprint: Mapped[Blueprint] = relationship(back_populates="rules")
    domain: Mapped[Domain] = relationship()


class ExamAttempt(Base):
    __tablename__ = "exam_attempts"
    __table_args__ = (
        # 1인 1회차 동시 응시 1건: IN_PROGRESS 는 사용자당 최대 1개
        Index(
            "uq_one_active_attempt",
            "user_id",
            unique=True,
            sqlite_where=text("status = 'IN_PROGRESS'"),
            postgresql_where=text("status = 'IN_PROGRESS'"),
        ),
        Index("uq_attempt_certificate_no", "certificate_no", unique=True),  # NULL 은 여러 개 허용
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    blueprint_id: Mapped[int] = mapped_column(ForeignKey("blueprints.id"))
    seed: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    total_score: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[AttemptStatus] = mapped_column(_enum(AttemptStatus), default=AttemptStatus.IN_PROGRESS)
    # 채점 시점 스냅샷(Blueprint가 이후 바뀌어도 결과 유지)
    domain_scores: Mapped[dict | None] = mapped_column(JSON)
    pass_score: Mapped[float | None] = mapped_column(Float)
    domain_min_ratio: Mapped[float | None] = mapped_column(Float)
    # 합격증 인증번호(처음 내려받을 때 발급). 진위 확인 페이지에서 이 번호로 조회한다
    certificate_no: Mapped[str | None] = mapped_column(String(40))

    items: Mapped[list["AttemptItem"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan", order_by="AttemptItem.seq"
    )


class AttemptItem(Base):
    __tablename__ = "attempt_items"
    __table_args__ = (UniqueConstraint("attempt_id", "item_id", name="uq_attempt_item"),)

    attempt_id: Mapped[int] = mapped_column(ForeignKey("exam_attempts.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    option_order: Mapped[list] = mapped_column(JSON)  # 표시 순서의 원본 key 목록 ["C","A",...]
    points: Mapped[float] = mapped_column(Float)
    selected_keys: Mapped[list | None] = mapped_column(JSON)  # 원본 key 기준
    is_correct: Mapped[bool | None] = mapped_column(Boolean)
    earned_points: Mapped[float | None] = mapped_column(Float)  # 부분 점수 설정 대비
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    answered_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    attempt: Mapped[ExamAttempt] = relationship(back_populates="items")
    item: Mapped[Item] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(50), index=True)
    target: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class AppSetting(Base):
    """관리자 설정(해설 공개 여부 등). key/value(JSON)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)


class ItemOverride(Base):
    """문항 단위 채점 보정(문항 오류 발견 시). 문항당 활성 보정은 1건, 이력은 audit_logs 에 남는다."""

    __tablename__ = "item_overrides"

    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), primary_key=True)
    mode: Mapped[OverrideMode] = mapped_column(_enum(OverrideMode))
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class PasswordResetToken(Base):
    """비밀번호 재설정 링크. 토큰 원문은 메일로만 보내고 DB 에는 SHA-256 해시만 저장한다(1회용, 만료 있음)."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
