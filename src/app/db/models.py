import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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


class User(Base):
    """小照 LINE 化階段 2：照護員／主管內部使用者（見 xiaozhao-phase2 設計文件）。"""

    __tablename__ = "users"
    __table_args__ = {"schema": "biz"}

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16))
    supervisor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("biz.users.user_id"), nullable=True)
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
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("biz.users.user_id"))
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
