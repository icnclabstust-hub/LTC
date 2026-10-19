import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = {"schema": "catalog"}

    doc_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(String(16))
    source_name: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    effective_date: Mapped[date] = mapped_column(Date)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = {"schema": "catalog"}

    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    doc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("catalog.documents.doc_id"))
    content: Mapped[str] = mapped_column(Text)
    article: Mapped[str | None] = mapped_column(String(32), nullable=True)
    embedding_ref: Mapped[str] = mapped_column(String(128))


class SyncEvent(Base):
    __tablename__ = "sync_events"
    __table_args__ = {"schema": "catalog"}

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    doc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("catalog.documents.doc_id"))
    operation: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LtcPoint(Base):
    """長照 ABC 據點快取（見資料擷取層設計文件）：純直查用途，不進 ChromaDB/RAG。"""

    __tablename__ = "ltc_points"
    __table_args__ = {"schema": "catalog"}

    institution_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(16))
    address: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 2026-10-19 ltc-kb-graph：Neo4j 資源地理層用（縣市／鄉鎮為內政部行政區代碼）
    county_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    town_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    town_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    abc_level: Mapped[str | None] = mapped_column(String(4), nullable=True)
    service_names: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    service_area_codes: Mapped[list[str] | None] = mapped_column(ARRAY(String(16)), nullable=True)


class _SourcedMixin:
    """結構化長照資料共通欄位：出處與有效期間（valid_to 為 NULL 表示現行，見 2026-10-19 ltc-kb-graph 5.1 節）。"""

    source_url: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[str] = mapped_column(Text)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LtcBenefitCategory(_SourcedMixin, Base):
    __tablename__ = "ltc_benefit_categories"
    __table_args__ = {"schema": "catalog"}

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    service_code_prefixes: Mapped[str] = mapped_column(String(16))
    period: Mapped[str] = mapped_column(String(8))


class LtcQuota(_SourcedMixin, Base):
    __tablename__ = "ltc_quotas"
    __table_args__ = {"schema": "catalog"}

    quota_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(ForeignKey("catalog.ltc_benefit_categories.code"))
    need_level: Mapped[int] = mapped_column(SmallInteger)
    zone: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    item_group: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    amount: Mapped[int] = mapped_column(Integer)
    period: Mapped[str] = mapped_column(String(8))


class LtcIdentityClass(_SourcedMixin, Base):
    __tablename__ = "ltc_identity_classes"
    __table_args__ = {"schema": "catalog"}

    class_no: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    definition: Mapped[str] = mapped_column(Text)


class LtcCopayRate(_SourcedMixin, Base):
    __tablename__ = "ltc_copay_rates"
    __table_args__ = {"schema": "catalog"}

    rate_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(ForeignKey("catalog.ltc_benefit_categories.code"))
    identity_class: Mapped[int] = mapped_column(ForeignKey("catalog.ltc_identity_classes.class_no"))
    zone: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    rate_pct: Mapped[int] = mapped_column(SmallInteger)


class LtcTransportZone(_SourcedMixin, Base):
    __tablename__ = "ltc_transport_zones"
    __table_args__ = {"schema": "catalog"}

    zone_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    county: Mapped[str] = mapped_column(Text)
    town: Mapped[str | None] = mapped_column(Text, nullable=True)
    zone: Mapped[int] = mapped_column(SmallInteger)


class LtcServiceItem(_SourcedMixin, Base):
    __tablename__ = "ltc_service_items"
    __table_args__ = {"schema": "catalog"}

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(ForeignKey("catalog.ltc_benefit_categories.code"), nullable=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class LtcEligibleGroup(_SourcedMixin, Base):
    __tablename__ = "ltc_eligible_groups"
    __table_args__ = {"schema": "catalog"}

    group_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(Text)


class SyncCursor(Base):
    """各訂閱者（目前為 neo4j）的同步進度，架構圖 catalog schema 的 sync_cursor。"""

    __tablename__ = "sync_cursor"
    __table_args__ = {"schema": "catalog"}

    subscriber: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime)
    last_version: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSONB)


class User(Base):
    """小照 LINE 化階段 2：照護員／主管內部使用者（見 xiaozhao-phase2 設計文件）。"""

    __tablename__ = "users"
    __table_args__ = {"schema": "biz"}

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16))
    supervisor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("biz.users.user_id"), nullable=True)
    # 匿名代號（照護員 CG-0001、督導 SV-0002），送 LLM、統計與督導卡片只用代號不用姓名
    pseudonym: Mapped[str | None] = mapped_column(String(16), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserIdentity(Base):
    """使用者的通道身分（如 LINE userId），多通道友善設計，見設計文件第 4-2 節。"""

    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("channel", "external_user_id"), {"schema": "biz"})

    identity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("biz.users.user_id"))
    channel: Mapped[str] = mapped_column(String(32))
    external_user_id: Mapped[str] = mapped_column(String(128))
    bound_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    unbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BindingCode(Base):
    """一次性綁定碼，10 分鐘效期，見設計文件第 5 節資料流。"""

    __tablename__ = "binding_codes"
    __table_args__ = {"schema": "biz"}

    code_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(6))
    # NULL 表示由 FE 管理員（LINE_ADMIN_USER_IDS 設定，不是 biz.users 成員）產生
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("biz.users.user_id"), nullable=True)
    target_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("biz.users.user_id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    used_by_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("biz.user_identities.identity_id"), nullable=True
    )


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = {"schema": "biz"}

    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = {"schema": "biz"}

    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("biz.conversations.conversation_id"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    cited_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class InboxRequest(Base):
    """對外通道收到的請求，以 (channel, external_event_id) 去重（見小照 LINE 化
    階段 1 設計文件）。與 catalog.sync_events（資料擷取 outbox）用途不同，不可共用。"""

    __tablename__ = "inbox_requests"
    __table_args__ = (UniqueConstraint("channel", "external_event_id"), {"schema": "biz"})

    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel: Mapped[str] = mapped_column(String(32))
    external_event_id: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class XiaozhaoRequest(Base):
    """小照 FE 送來的提問與處理狀態（/v1/query、/v1/scene，見 2026-10-19 fe-be-integration 設計文件 5.3 節）。

    以 FE 產生的 request_id 為主鍵，FE 重送同一請求時不會重複執行 RAG。
    query_text 與 payload 一律為遮罩後內容；payload 不含照護員姓名與逐字稿。
    """

    __tablename__ = "xiaozhao_requests"
    # 拒答統計（P2 每週分群）會依狀態與時間查詢
    __table_args__ = (Index("ix_xiaozhao_requests_status_created_at", "status", "created_at"), {"schema": "biz"})

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    # pending → answered | refused | error；scene 為 recorded；human_answered 留給 P2 工單
    status: Mapped[str] = mapped_column(String(16), default="pending")
    channel: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("biz.users.user_id"), nullable=True)
    role: Mapped[str] = mapped_column(String(16))
    query_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    answer_full: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_spoken: Mapped[str | None] = mapped_column(Text, nullable=True)
    citations: Mapped[list] = mapped_column(JSONB, default=list)
    refusal_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    escalate: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[str | None] = mapped_column(String(8), nullable=True)
    help_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    human_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OutboxEvent(Base):
    """待送達的對外事件（如推播），狀態機 pending/sent/failed（見小照 LINE 化階段 1 設計文件）。"""

    __tablename__ = "outbox_events"
    __table_args__ = {"schema": "biz"}

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FeedbackSync(Base):
    """從 Open WebUI 匯出 API 同步進來的原始回饋（見回饋迴圈設計文件）。"""

    __tablename__ = "feedback_sync"
    __table_args__ = {"schema": "eval"}

    feedback_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    rating: Mapped[str | None] = mapped_column(String(8), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GoldenQuery(Base):
    __tablename__ = "golden_queries"
    __table_args__ = {"schema": "eval"}

    query_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    query: Mapped[str] = mapped_column(Text)
    relevant_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_feedback_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 由 eval/golden_queries.csv 匯入（2026-10-19 ltc-kb-graph 5.1 節）
    question_id: Mapped[str | None] = mapped_column(String(16), unique=True, nullable=True)
    expected_behavior: Mapped[str | None] = mapped_column(String(16), nullable=True)
    expected_refusal_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    escalate: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class EvalAlert(Base):
    """評測告警（架構圖：Recall/nDCG 下降 >10%、拒答率上升 >15%）。"""

    __tablename__ = "alerts"
    __table_args__ = {"schema": "eval"}

    alert_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    metric: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    detail: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
