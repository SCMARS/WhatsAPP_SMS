import uuid
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, JSON,
    String, Text, UniqueConstraint, func
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# NOTE: When running against an existing DB, run migrate_telegram.py once to apply
# the schema changes (add platform column, recreate unique constraint, add new tables).


class Base(DeclarativeBase):
    pass


class WhatsAppInstance(Base):
    __tablename__ = "whatsapp_instances"
    __table_args__ = (
        Index("ix_instances_active_banned", "is_active", "is_banned"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    instance_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    api_token: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(30), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    daily_limit: Mapped[int] = mapped_column(Integer, default=150, nullable=False)
    hourly_limit: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    min_delay_sec: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    max_delay_sec: Mapped[int] = mapped_column(Integer, default=25, nullable=False)
    health_status: Mapped[str] = mapped_column(String(50), default="unknown", nullable=False)
    last_health_check: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_send_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    messages: Mapped[List["WhatsAppMessage"]] = relationship("WhatsAppMessage", back_populates="instance")


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    conversations: Mapped[List["Conversation"]] = relationship("Conversation", back_populates="campaign")


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        # Extended to include platform so WA and TG can coexist for same phone+campaign.
        # migrate_telegram.py drops the old 2-column constraint and creates this one.
        UniqueConstraint("campaign_id", "phone", "platform", name="uq_conversation_campaign_phone_platform"),
        Index("ix_conversations_phone", "phone"),
        Index("ix_conversations_campaign_id", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False)
    lead_id: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(30), nullable=False)
    lead_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    platform: Mapped[str] = mapped_column(String(20), default="whatsapp", nullable=False, server_default="whatsapp")
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active|stopped|closed
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    nudge_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    first_contact_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # ── Lead conversion tracking ──────────────────────────────────────────────
    # lead_status: new → contacted → replied → interested → converted | unsubscribed
    lead_status: Mapped[str] = mapped_column(String(30), default="new", nullable=False, server_default="new")
    outbound_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    reply_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    replied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    assigned_link_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    campaign: Mapped["Campaign"] = relationship("Campaign", back_populates="conversations")
    messages: Mapped[List["WhatsAppMessage"]] = relationship(
        "WhatsAppMessage",
        back_populates="conversation",
        order_by="WhatsAppMessage.created_at"
    )
    telegram_messages: Mapped[List["TelegramMessage"]] = relationship(
        "TelegramMessage",
        back_populates="conversation",
        order_by="TelegramMessage.created_at"
    )
    events: Mapped[List["LeadEvent"]] = relationship(
        "LeadEvent",
        back_populates="conversation",
        order_by="LeadEvent.created_at"
    )


class WhatsAppMessage(Base):
    __tablename__ = "whatsapp_messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_messages_provider_message_id", "provider_message_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False)
    instance_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("whatsapp_instances.id"), nullable=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # outbound|inbound
    body: Mapped[str] = mapped_column(Text, nullable=False)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)  # pending|sent|delivered|read|failed|received
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    conversation: Mapped["Conversation"] = relationship("Conversation", back_populates="messages")
    instance: Mapped[Optional["WhatsAppInstance"]] = relationship("WhatsAppInstance", back_populates="messages")


class Blacklist(Base):
    __tablename__ = "blacklist"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LinkPool(Base):
    __tablename__ = "links_pool"
    __table_args__ = (
        Index("ix_links_pool_country_used", "country", "used"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    country: Mapped[str] = mapped_column(String(10), nullable=False)  # "PT", "AR", etc.
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    lead_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM MODELS
# ─────────────────────────────────────────────────────────────────────────────

class TelegramInstance(Base):
    """One Telegram user-account (Telethon session) used for outreach."""
    __tablename__ = "telegram_instances"
    __table_args__ = (
        Index("ix_tg_instances_active_banned", "is_active", "is_banned"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)

    # Telethon MTProto credentials (per-instance app registration)
    api_id: Mapped[int] = mapped_column(Integer, nullable=False)
    api_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Serialized StringSession — populated by telegram_auth.py; NULL until authed.
    session_string: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # is_authorized: True only after telegram_auth.py runs successfully.
    # is_active: managed by health monitor (False during flood/cooldown).
    is_authorized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    daily_limit: Mapped[int] = mapped_column(Integer, default=200, nullable=False)
    hourly_limit: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    min_delay_sec: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    max_delay_sec: Mapped[int] = mapped_column(Integer, default=30, nullable=False)

    # 'authorized' | 'flood_wait' | 'peer_flood' | 'session_expired' | 'deactivated' | 'unknown'
    health_status: Mapped[str] = mapped_column(String(50), default="unknown", nullable=False)
    last_health_check: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_send_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Consecutive FloodWait events this cycle — instances with ≥3 are deprioritised.
    flood_wait_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    messages: Mapped[List["TelegramMessage"]] = relationship("TelegramMessage", back_populates="instance")


class TelegramMessage(Base):
    """Audit log for all Telegram messages (inbound and outbound)."""
    __tablename__ = "telegram_messages"
    __table_args__ = (
        Index("ix_tg_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_tg_messages_provider_message_id", "provider_message_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False)

    # NULL for inbound (we don't know which instance received the message)
    instance_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("telegram_instances.id"), nullable=True)

    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # outbound|inbound
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Telegram integer message_id stored as string for consistency with WA
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)

    # Resolved Telegram user_id of the lead — cached after first send to avoid re-resolving.
    telegram_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    conversation: Mapped["Conversation"] = relationship("Conversation", back_populates="telegram_messages")
    instance: Mapped[Optional["TelegramInstance"]] = relationship("TelegramInstance", back_populates="messages")


# ─────────────────────────────────────────────────────────────────────────────
# LEAD CONVERSION TRACKING
# ─────────────────────────────────────────────────────────────────────────────

class LeadEvent(Base):
    """Timeline of key events for a lead conversation (conversion funnel tracking)."""
    __tablename__ = "lead_events"
    __table_args__ = (
        Index("ix_lead_events_conversation_created", "conversation_id", "created_at"),
        Index("ix_lead_events_event_type", "event_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False
    )
    # contacted | replied | nudge_sent | link_clicked | converted | unsubscribed
    # note_added | followup_sent | status_changed
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped["Conversation"] = relationship("Conversation", back_populates="events")
