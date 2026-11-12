from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DownloadJob, DownloadJobItem, DownloadTask
from ..pikpak.client import AnonymousPikPakClient
from ..pikpak.errors import PikPakError
from ..security import decrypt

log = structlog.get_logger(__name__)

MAX_AUDIT_FILES = 100_000
MAX_FOLDERS = 20_000
SAMPLE_LIMIT = 100


def _join(base: str, name: str) -> str:
    return f"{base}/{name}".strip("/") if base else name


async def audit_job(
    session: AsyncSession,
    job: DownloadJob,
    client: AnonymousPikPakClient,
) -> dict:
    """Re-enumerate the share and compare it with the job's task rows."""
    token = decrypt(job.pass_code_token_enc)
    expected: dict[str, str] = {}
    errors: list[dict] = []
    folders_scanned = 0
    truncated = False

    async def walk(folder_id: str, base: str) -> None:
        nonlocal folders_scanned, truncated
        if truncated or folders_scanned >= MAX_FOLDERS:
            truncated = True
            return
        folders_scanned += 1
        page_token: str | None = None
        for _ in range(200):
            if len(expected) >= MAX_AUDIT_FILES:
                truncated = True
                return
            try:
                page = await client.list_folder(
                    job.share_id, folder_id, token, page_token
                )
            except PikPakError as exc:
                errors.append(
                    {
                        "folder_id": folder_id,
                        "path": base,
                        "error": str(exc),
                        "code": exc.code,
                    }
                )
                return
            for node in page.files:
                relative = _join(base, node.name)
                if node.is_folder:
                    await walk(node.id, relative)
                else:
                    expected[node.id] = relative
            page_token = page.next_page_token or None
            if not page_token:
                return

    if job.root_kind == "selection":
        items = (
            await session.execute(
                select(DownloadJobItem)
                .where(DownloadJobItem.job_id == job.id)
                .order_by(DownloadJobItem.sort_index.asc())
            )
        ).scalars().all()
        if not items:
            errors.append({"folder_id": "", "path": "", "error": "没有选择项"})
        for item in items:
            base = item.relative_path or item.name
            if item.kind == "drive#folder":
                await walk(item.share_file_id, base)
            else:
                expected[item.share_file_id] = base
    elif job.root_kind == "drive#folder" and job.root_file_id:
        await walk(job.root_file_id, job.root_name or "")
    elif job.root_file_id:
        expected[job.root_file_id] = job.root_name or ""

    task_ids = set(
        (
            await session.execute(
                select(DownloadTask.share_file_id).where(
                    DownloadTask.job_id == job.id
                )
            )
        ).scalars().all()
    )
    missing = [
        {"file_id": file_id, "path": path}
        for file_id, path in expected.items()
        if file_id not in task_ids
    ]
    extra = sorted(task_ids - set(expected))
    return {
        "job_id": job.id,
        "expected_files": len(expected),
        "task_files": len(task_ids),
        "missing_count": len(missing),
        "missing": missing[:SAMPLE_LIMIT],
        "extra_count": len(extra),
        "extra": extra[:SAMPLE_LIMIT],
        "folders_scanned": folders_scanned,
        "truncated": truncated,
        "errors": errors,
    }
