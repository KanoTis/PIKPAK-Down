from __future__ import annotations

import asyncio

from ..pikpak.client import FileNode
from ..pikpak.errors import NotFound, PikPakError


async def wait_remote_task(client, task_id: str, *, timeout: float = 60.0) -> bool:
    if not task_id:
        return True
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            state = await client.get_restore_task(task_id)
        except NotFound:
            return True
        except PikPakError:
            raise
        phase = str(state.get("phase") or "").upper()
        message = str(state.get("message") or "").lower()
        if phase in ("PHASE_TYPE_COMPLETE", "COMPLETE") or message == "completed":
            return True
        if phase in ("PHASE_TYPE_ERROR", "ERROR") or message in ("error", "failed"):
            return False
        await asyncio.sleep(1.5)
    return False


async def delete_remote_file(client, file_id: str) -> int:
    if not file_id:
        return 0
    try:
        task_id = await client.delete_files([file_id])
    except NotFound:
        return 0
    if not await wait_remote_task(client, task_id):
        raise PikPakError(
            "delete task did not finish",
            code="CLEANUP_TASK_TIMEOUT",
            retryable=True,
        )
    return 1


async def delete_folder_tree(client, folder_id: str, *, depth: int = 0) -> int:
    """Delete a folder bottom-up with permanent delete tasks.

    Do not use batchTrash here: trashed items keep occupying PIKPAK quota, and
    batchDelete does not reliably purge them. Enumerate children and delete
    every file/folder directly instead.
    """
    if not folder_id or depth > 8:
        return 0
    files: list[str] = []
    folders: list[str] = []
    page_token: str | None = None
    for _ in range(200):
        try:
            data = await client.list_files(folder_id, page_token)
        except NotFound:
            return 0
        for item in data.get("files") or []:
            node = FileNode.from_json(item)
            if node.trashed:
                continue
            if node.is_folder:
                folders.append(node.id)
            else:
                files.append(node.id)
        page_token = data.get("next_page_token") or None
        if not page_token:
            break
    for child in folders:
        await delete_folder_tree(client, child, depth=depth + 1)
    for index in range(0, len(files), 100):
        chunk = files[index : index + 100]
        task_id = await client.delete_files(chunk)
        if not await wait_remote_task(client, task_id):
            raise PikPakError(
                "delete task did not finish",
                code="CLEANUP_TASK_TIMEOUT",
                retryable=True,
            )
    task_id = await client.delete_files([folder_id])
    if not await wait_remote_task(client, task_id):
        raise PikPakError(
            "delete task did not finish",
            code="CLEANUP_TASK_TIMEOUT",
            retryable=True,
        )
    return 1


