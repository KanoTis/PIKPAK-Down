from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class JobStatus:
    ENUMERATING = "ENUMERATING"
    READY = "READY"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    TERMINAL = {COMPLETED, COMPLETED_WITH_ERRORS, FAILED, CANCELLED}


class TaskStatus:
    DISCOVERED = "DISCOVERED"
    PLANNING = "PLANNING"
    ANON_DOWNLOADING = "ANON_DOWNLOADING"
    ACCOUNT_QUEUED = "ACCOUNT_QUEUED"
    RESTORING = "RESTORING"
    WCL_READY = "WCL_READY"
    ACCOUNT_DOWNLOADING = "ACCOUNT_DOWNLOADING"
    VERIFYING = "VERIFYING"
    PAUSED = "PAUSED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    TERMINAL = {DONE, FAILED, CANCELLED}


class FolderStatus:
    PENDING = "PENDING"
    ENUMERATING = "ENUMERATING"
    DONE = "DONE"
    FAILED = "FAILED"


class AccountState:
    ACTIVE = "ACTIVE"
    COOLDOWN = "COOLDOWN"
    INVALID = "INVALID"
    DISABLED = "DISABLED"


class CleanupState:
    PENDING = "PENDING"
    RETRYING = "RETRYING"
    DONE = "DONE"
    FAILED = "FAILED"


class LineageStatus:
    QUEUED = "QUEUED"
    RESOLVING = "RESOLVING"
    DIRECT_CHAIN = "DIRECT_CHAIN"
    ENUMERATING = "ENUMERATING"
    SWEEPING = "SWEEPING"
    PROBING = "PROBING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    TERMINAL = {DONE, FAILED, CANCELLED}


class DownloadJob(Base):
    __tablename__ = "download_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    input_url: Mapped[str] = mapped_column(Text)
    share_id: Mapped[str] = mapped_column(String(64), index=True)
    pass_code_enc: Mapped[str | None] = mapped_column(Text)
    pass_code_token_enc: Mapped[str | None] = mapped_column(Text)
    root_file_id: Mapped[str | None] = mapped_column(String(64))
    root_kind: Mapped[str | None] = mapped_column(String(32))
    root_name: Mapped[str | None] = mapped_column(Text)
    target_subdir: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.ENUMERATING, index=True)
    total_files: Mapped[int] = mapped_column(Integer, default=0)
    completed_files: Mapped[int] = mapped_column(Integer, default=0)
    failed_files: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    downloaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    folder_total: Mapped[int] = mapped_column(Integer, default=0)
    folder_done: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DownloadFolder(Base):
    __tablename__ = "download_folders"
    __table_args__ = (UniqueConstraint("job_id", "folder_id", name="uq_download_folder"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("download_jobs.id", ondelete="CASCADE"), index=True)
    folder_id: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text, default="")
    page_token: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default=FolderStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DownloadJobItem(Base):
    __tablename__ = "download_job_items"
    __table_args__ = (
        UniqueConstraint("job_id", "share_file_id", name="uq_download_job_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("download_jobs.id", ondelete="CASCADE"), index=True
    )
    share_file_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32), default="drive#file")
    name: Mapped[str] = mapped_column(Text, default="")
    relative_path: Mapped[str] = mapped_column(Text, default="")
    sort_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DownloadTask(Base):
    __tablename__ = "download_tasks"
    __table_args__ = (
        UniqueConstraint("job_id", "share_file_id", "expected_hash", name="uq_download_task"),
        Index("ix_download_tasks_status", "status"),
        Index("ix_download_tasks_job_status", "job_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("download_jobs.id", ondelete="CASCADE"), index=True)
    share_file_id: Mapped[str] = mapped_column(String(64))
    expected_hash: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(Text, default="")
    relative_path: Mapped[str] = mapped_column(Text, default="")
    target_path: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    kind: Mapped[str | None] = mapped_column(String(32))
    is_video: Mapped[bool] = mapped_column(Boolean, default=False)
    route: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.DISCOVERED)
    resume_state: Mapped[str | None] = mapped_column(String(32))
    source_share_id: Mapped[str | None] = mapped_column(String(64))
    source_file_id: Mapped[str | None] = mapped_column(String(64))
    source_url_enc: Mapped[str | None] = mapped_column(Text)
    source_url_expire: Mapped[int | None] = mapped_column(BigInteger)
    account_id: Mapped[str | None] = mapped_column(String(36), index=True)
    account_label: Mapped[str | None] = mapped_column(Text)
    drive_file_id: Mapped[str | None] = mapped_column(String(64))
    restore_task_id: Mapped[str | None] = mapped_column(String(64))
    restore_folder_id: Mapped[str | None] = mapped_column(String(64))
    restore_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tar_size: Mapped[int] = mapped_column(BigInteger, default=0)
    tar_folder_id: Mapped[str | None] = mapped_column(String(64))
    remote_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    aria2_gid: Mapped[str | None] = mapped_column(String(64))
    bytes_done: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_total: Mapped[int] = mapped_column(BigInteger, default=0)
    speed: Mapped[int] = mapped_column(BigInteger, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    verify_attempts: Mapped[int] = mapped_column(Integer, default=0)
    download_cycles: Mapped[int] = mapped_column(Integer, default=0)
    object_id: Mapped[str | None] = mapped_column(String(36))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    verified_chunk: Mapped[int | None] = mapped_column(Integer)
    queue_reason: Mapped[str | None] = mapped_column(String(64))
    sort_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FileObject(Base):
    __tablename__ = "file_objects"
    __table_args__ = (UniqueConstraint("hash", "size", name="uq_file_object"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    hash: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    rel_path: Mapped[str] = mapped_column(Text)
    merkle_chunk: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    label: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(320))
    sub: Mapped[str | None] = mapped_column(String(128), unique=True)
    device_id: Mapped[str] = mapped_column(String(32))
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    refresh_token_enc: Mapped[str] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quota_limit: Mapped[int | None] = mapped_column(BigInteger)
    quota_usage: Mapped[int | None] = mapped_column(BigInteger)
    cloud_download_limit: Mapped[int | None] = mapped_column(Integer)
    cloud_download_used: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default=AccountState.ACTIVE, index=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    daily_date: Mapped[str | None] = mapped_column(String(10))
    daily_restore_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_task_id: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CleanupTask(Base):
    __tablename__ = "cleanup_tasks"
    __table_args__ = (
        UniqueConstraint("account_id", "drive_file_id", name="uq_cleanup_file"),
        Index("ix_cleanup_state", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    drive_file_id: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(String(16), default=CleanupState.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class LineageRun(Base):
    __tablename__ = "lineage_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    input_type: Mapped[str] = mapped_column(String(24))
    input_json: Mapped[str] = mapped_column(Text)
    pass_code_enc: Mapped[str | None] = mapped_column(Text)
    share_id: Mapped[str | None] = mapped_column(String(64), index=True)
    file_id: Mapped[str | None] = mapped_column(String(64))
    hash: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[int | None] = mapped_column(BigInteger)
    include_tree: Mapped[bool] = mapped_column(Boolean, default=True)
    probe_windows: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default=LineageStatus.QUEUED, index=True)
    progress: Mapped[str | None] = mapped_column(Text)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    shares_probed: Mapped[int] = mapped_column(Integer, default=0)
    items_enumerated: Mapped[int] = mapped_column(Integer, default=0)
    folders_done: Mapped[int] = mapped_column(Integer, default=0)
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    termination: Mapped[str | None] = mapped_column(String(64))
    passcode_unverified: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LineageNode(Base):
    __tablename__ = "lineage_nodes"
    __table_args__ = (
        UniqueConstraint("run_id", "node_key", name="uq_lineage_node"),
        Index("ix_lineage_node_ref", "run_id", "original_share_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("lineage_runs.id", ondelete="CASCADE"), index=True)
    node_key: Mapped[str] = mapped_column(Text)
    node_type: Mapped[str] = mapped_column(String(16))
    share_id: Mapped[str | None] = mapped_column(String(64))
    file_id: Mapped[str | None] = mapped_column(String(64))
    parent_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(String(32))
    size: Mapped[int | None] = mapped_column(BigInteger)
    hash: Mapped[str | None] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    file_extension: Mapped[str | None] = mapped_column(String(32))
    user_id: Mapped[str | None] = mapped_column(String(128))
    created_time: Mapped[str | None] = mapped_column(String(64))
    modified_time: Mapped[str | None] = mapped_column(String(64))
    platform: Mapped[str | None] = mapped_column(String(64))
    platform_icon: Mapped[str | None] = mapped_column(Text)
    original_share_id: Mapped[str | None] = mapped_column(String(64))
    original_file_id: Mapped[str | None] = mapped_column(String(64))
    anonymous_play_seconds: Mapped[int | None] = mapped_column(Integer)
    anonymous_play_range: Mapped[str | None] = mapped_column(String(16))
    share_status: Mapped[str | None] = mapped_column(String(32))
    share_status_text: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(String(24), index=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(64))
    raw_json: Mapped[str | None] = mapped_column(Text)


class LineageEdge(Base):
    __tablename__ = "lineage_edges"
    __table_args__ = (
        UniqueConstraint("run_id", "from_key", "to_key", "edge_type", name="uq_lineage_edge"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("lineage_runs.id", ondelete="CASCADE"), index=True)
    from_key: Mapped[str] = mapped_column(Text)
    to_key: Mapped[str] = mapped_column(Text)
    edge_type: Mapped[str] = mapped_column(String(24))
    via_field: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[str] = mapped_column(String(16), default="observed")


class ShareSnapshot(Base):
    __tablename__ = "share_snapshots"
    __table_args__ = (UniqueConstraint("run_id", "share_id", name="uq_share_snapshot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("lineage_runs.id", ondelete="CASCADE"), index=True)
    share_id: Mapped[str] = mapped_column(String(64))
    share_status: Mapped[str | None] = mapped_column(String(32))
    share_status_text: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    file_num: Mapped[str | None] = mapped_column(String(32))
    user_id: Mapped[str | None] = mapped_column(String(128))
    anonymous_play_seconds: Mapped[int | None] = mapped_column(Integer)
    root_json: Mapped[str | None] = mapped_column(Text)
    pass_code_token_enc: Mapped[str | None] = mapped_column(Text)
    probed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_json: Mapped[str | None] = mapped_column(Text)


class CopyIndex(Base):
    __tablename__ = "copy_index"
    __table_args__ = (
        UniqueConstraint("run_id", "share_id", "file_id", name="uq_copy_index"),
        Index("ix_copy_fp", "run_id", "hash", "size"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("lineage_runs.id", ondelete="CASCADE"), index=True)
    share_id: Mapped[str] = mapped_column(String(64))
    file_id: Mapped[str] = mapped_column(String(64))
    parent_path: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(String(32))
    size: Mapped[int | None] = mapped_column(BigInteger)
    hash: Mapped[str | None] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    is_video: Mapped[bool] = mapped_column(Boolean, default=False)


class WindowProbe(Base):
    __tablename__ = "window_probes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("lineage_runs.id", ondelete="CASCADE"), index=True)
    share_id: Mapped[str] = mapped_column(String(64))
    file_id: Mapped[str] = mapped_column(String(64))
    media_name: Mapped[str | None] = mapped_column(String(64))
    url_host: Mapped[str | None] = mapped_column(String(255))
    size: Mapped[int | None] = mapped_column(BigInteger)
    no_range_status: Mapped[int | None] = mapped_column(Integer)
    head_end: Mapped[int | None] = mapped_column(BigInteger)
    tail_start: Mapped[int | None] = mapped_column(BigInteger)
    tail_end: Mapped[int | None] = mapped_column(BigInteger)
    full_200: Mapped[bool] = mapped_column(Boolean, default=False)
    supported: Mapped[bool] = mapped_column(Boolean, default=True)
    raw_json: Mapped[str | None] = mapped_column(Text)
    probed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RawResponse(Base):
    __tablename__ = "raw_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("lineage_runs.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(8))
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[int | None] = mapped_column(Integer)
    file_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LineageFolder(Base):
    __tablename__ = "lineage_folders"
    __table_args__ = (UniqueConstraint("run_id", "folder_id", name="uq_lineage_folder"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("lineage_runs.id", ondelete="CASCADE"), index=True)
    folder_id: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text, default="")
    page_token: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

class LineageGraphShare(Base):
    __tablename__ = "lineage_graph_shares"
    __table_args__ = (
        UniqueConstraint("run_id", "share_id", name="uq_lineage_graph_share"),
        Index("ix_lineage_graph_share_state", "run_id", "state"),
        Index("ix_lineage_graph_share_depth", "run_id", "depth"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("lineage_runs.id", ondelete="CASCADE"), index=True
    )
    share_id: Mapped[str] = mapped_column(String(64))
    depth: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(16), default="parent")
    state: Mapped[str] = mapped_column(String(24), default="PENDING")
    share_status: Mapped[str | None] = mapped_column(String(32))
    share_status_text: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str | None] = mapped_column(String(128))
    file_num: Mapped[str | None] = mapped_column(String(32))
    anonymous_play_seconds: Mapped[int | None] = mapped_column(Integer)
    root_kind: Mapped[str | None] = mapped_column(String(32))
    root_file_id: Mapped[str | None] = mapped_column(String(64))
    root_name: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class LineageGraphItem(Base):
    __tablename__ = "lineage_graph_items"
    __table_args__ = (
        UniqueConstraint("run_id", "share_id", "file_id", name="uq_lineage_graph_item"),
        Index("ix_lineage_graph_item_origin", "run_id", "original_share_id"),
        Index("ix_lineage_graph_item_share", "run_id", "share_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("lineage_runs.id", ondelete="CASCADE"), index=True
    )
    share_id: Mapped[str] = mapped_column(String(64))
    file_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32), default="drive#file")
    name: Mapped[str] = mapped_column(Text, default="")
    path: Mapped[str] = mapped_column(Text, default="")
    parent_id: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    hash: Mapped[str | None] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    is_video: Mapped[bool] = mapped_column(Boolean, default=False)
    original_share_id: Mapped[str | None] = mapped_column(String(64))
    original_file_id: Mapped[str | None] = mapped_column(String(64))
    depth: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LineageGraphFolder(Base):
    __tablename__ = "lineage_graph_folders"
    __table_args__ = (
        UniqueConstraint("run_id", "share_id", "folder_id", name="uq_lineage_graph_folder"),
        Index("ix_lineage_graph_folder_state", "run_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("lineage_runs.id", ondelete="CASCADE"), index=True
    )
    share_id: Mapped[str] = mapped_column(String(64))
    folder_id: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text, default="")
    page_token: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default="PENDING")
    depth: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
