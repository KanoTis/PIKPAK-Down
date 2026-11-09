from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DownloadSelectionItem(BaseModel):
    file_id: str
    kind: str = "drive#file"
    name: str = ""
    relative_path: str | None = None
    size: int = 0


class DownloadJobCreate(BaseModel):
    url: str
    pass_code: str | None = None
    items: list[DownloadSelectionItem] | None = None
    target_subdir: str | None = None


class SharePreviewRequest(BaseModel):
    url: str
    pass_code: str | None = None
    folder_id: str | None = None
    folder_name: str | None = None
    page_token: str | None = None


class PreviewItemOut(BaseModel):
    id: str
    name: str
    kind: str
    size: int
    hash: str
    mime_type: str | None = None
    is_video: bool


class SharePreviewOut(BaseModel):
    share_id: str
    share_status: str
    share_status_text: str = ""
    title: str = ""
    folder_id: str | None = None
    folder_name: str | None = None
    items: list[PreviewItemOut]
    next_page_token: str = ""
    is_root: bool = True


class DownloadJobOut(BaseModel):
    id: str
    input_url: str
    share_id: str
    root_file_id: str | None = None
    root_kind: str | None = None
    root_name: str | None = None
    target_subdir: str = ""
    status: str
    total_files: int
    completed_files: int
    failed_files: int
    total_bytes: int
    downloaded_bytes: int
    folder_total: int
    folder_done: int
    error_code: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class DownloadTaskOut(BaseModel):
    id: str
    job_id: str
    share_file_id: str
    name: str
    relative_path: str
    target_path: str | None = None
    size: int
    expected_hash: str
    mime_type: str | None = None
    is_video: bool
    route: str | None = None
    status: str
    resume_state: str | None = None
    source_share_id: str | None = None
    source_file_id: str | None = None
    source_url_expire: int | None = None
    account_id: str | None = None
    account_label: str | None = None
    drive_file_id: str | None = None
    remote_deleted_at: str | None = None
    aria2_gid: str | None = None
    bytes_done: int
    bytes_total: int
    speed: int
    attempts: int
    verify_attempts: int
    download_cycles: int
    object_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    queue_reason: str | None = None
    verified_chunk: int | None = None
    sort_index: int
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class TaskListOut(BaseModel):
    items: list[DownloadTaskOut]
    next_cursor: str | None = None


class AccountCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    email: str | None = None
    refresh_token: str = Field(min_length=8)


class AccountOut(BaseModel):
    id: str
    label: str
    email: str | None = None
    sub: str | None = None
    device_id: str
    state: str
    quota_limit: int | None = None
    quota_usage: int | None = None
    cloud_download_limit: int | None = None
    cloud_download_used: int | None = None
    cooldown_until: str | None = None
    last_used_at: str | None = None
    daily_date: str | None = None
    daily_restore_count: int
    lease_task_id: str | None = None
    lease_expires_at: str | None = None
    last_error: str | None = None
    created_at: str | None = None


class CleanupOut(BaseModel):
    id: str
    account_id: str
    drive_file_id: str
    task_id: str | None = None
    state: str
    attempts: int
    next_attempt_at: str | None = None
    last_error: str | None = None
    created_at: str | None = None


class LineageCreate(BaseModel):
    input: str | None = None
    share_id: str | None = None
    file_id: str | None = None
    pass_code: str | None = None
    hash: str | None = None
    size: int | None = None
    include_tree: bool = True
    probe_windows: bool = False
    graph: bool = False
    items: list[DownloadSelectionItem] | None = None


class LineageRunOut(BaseModel):
    id: str
    input_type: str
    status: str
    share_id: str | None = None
    file_id: str | None = None
    hash: str | None = None
    size: int | None = None
    include_tree: bool
    probe_windows: bool
    requests: int
    shares_probed: int
    items_enumerated: int
    folders_done: int
    truncated: bool
    termination: str | None = None
    passcode_unverified: bool
    error_code: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class ErrorBody(BaseModel):
    error: dict[str, Any]

class RuntimeSettingsOut(BaseModel):
    download_max_concurrency: int
    task_connections: int
    lineage_max_items: int
    lineage_max_requests: int
    lineage_max_shares: int
    lineage_max_folders: int


class RuntimeSettingsUpdate(BaseModel):
    download_max_concurrency: int | None = Field(None, ge=1, le=16)
    task_connections: int | None = Field(None, ge=1, le=16)
    lineage_max_items: int | None = Field(None, ge=1000, le=1_000_000)
    lineage_max_requests: int | None = Field(None, ge=100, le=100_000)
    lineage_max_shares: int | None = Field(None, ge=10, le=10_000)
    lineage_max_folders: int | None = Field(None, ge=100, le=100_000)
