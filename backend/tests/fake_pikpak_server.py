from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

DATA = bytes(range(256)) * 4097
SIZE = len(DATA)
HASH = "26BFA5D48027D096B078D5BCD95C4557306612CC"

ANON_SHARE = "VANON000000000001"
ANON_FILE = "ANONFILE0000000001"
ACCOUNT_SHARE = "VACCOUNT000000000001"
ACCOUNT_FILE = "ACCFILE00000000001"
FAIL_SHARE = "VACCOUNTFAIL000001"
FAIL_FILE = "ACCFAIL00000000001"
BADLINK_SHARE = "VACCOUNTBADLINK01"
BADLINK_FILE = "ACCBAD00000000001"
GRAPH_A_SHARE = "VGRAPHA00000000001"
GRAPH_A_ROOT = "GRAPHA00000000001"
GRAPH_A_FILE1 = "GRAPHAFILE00000001"
GRAPH_A_FILE2 = "GRAPHAFILE00000002"
GRAPH_A_FILE3 = "GRAPHAFILE00000003"
GRAPH_B_SHARE = "VGRAPHB00000000001"
GRAPH_B_ROOT = "GRAPHBROOT0000001"
GRAPH_B_FILE1 = "GRAPHB00000000001"
GRAPH_B_FILE2 = "GRAPHB00000000002"
GRAPH_C_SHARE = "VGRAPHC00000000001"
GRAPH_C_ROOT = "GRAPHCROOT0000001"
GRAPH_C_FILE1 = "GRAPHC00000000001"
GRAPH_D_SHARE = "VGRAPHD00000000001"
GRAPH_D_FILE = "GRAPHD00000000001"
NESTED_SHARE = "VNESTED00000000001"
NESTED_ROOT = "NESTEDROOT0000001"
NESTED_SUB = "NESTEDSUB00000001"
NESTED_FILE = "NESTEDFILE0000001"
FOLDER_SHARE = "VFOLDER000000000001"
FOLDER_ROOT = "FOLDERROOT00000001"
FOLDER_FILE1 = "FOLDERFILE00000001"
FOLDER_FILE2 = "FOLDERFILE00000002"
SMALL_DATA = b"hello world"
SMALL_HASH = "67BECF85308ACF0261750DA1075681EE5C412F05"
SLOW_SHARE = "VSLOW000000000001"
SLOW_FILE = "SLOWFILE00000000001"
SLOW_SIZE = 32 * 1024 * 1024
SLOW_HASH = "0E8EFB62409BA8DDCC1ED30B573A279A5FA3DA0B"
SLOW_DATA = bytes(range(256)) * (SLOW_SIZE // 256)

RESTORE_FOLDER = "RESTOREFOLDER000001"
DRIVE_FILE = "DRIVEACCOUNT00001"
FAIL_DRIVE_FILE = "DRIVEFAIL00000001"
BAD_DRIVE_FILE = "DRIVEBAD000000001"
TASK_ID = "RESTORETASK000001"

STATE: dict[str, Any] = {
    "restore_calls": 0,
    "delete_calls": 0,
    "trash_calls": 0,
    "delete_failures": 0,
    "events": [],
    "cdn_requests": [],
    "captcha_calls": 0,
    "file_info_calls": 0,
}

LINEAGE_SIZE = 462_049_171
LINEAGE_HASH = "E409BE3D495A99CA8A13AF850A292244A8CDFDE5"
PUB_SHARE = "VPUB0000000000001"
PUB_FILE = "VPUBFILE000000001"
FLD_SHARE = "VPFLD0000000000001"
FLD_FILE = "VPFLDFILE00000001"
FLD_FOLDER = "VPFLDFOLDER0000001"
VOZG_SHARE = "VPSAMPLE0000000001"
VOZG_FOLDER = "VPSAMPLEFOLDER001"
VOZR_SHARE = "VPSAMPLE0000000002"
REF_SHARES = [f"VPREF{i:013d}" for i in range(1, 27)]
PUB_HEAD = 285_584_324
FLD_HEAD = 410_337_600
TAIL_START = 444_472_335


def _lineage_share(share_id: str, base: str) -> dict[str, Any] | None:
    if share_id == PUB_SHARE:
        return {
            "share_status": "OK",
            "share_status_text": "",
            "file_num": "1",
            "title": "PUB",
            "params": {"anonymous_play_seconds": "120"},
            "files": [
                {
                    "id": PUB_FILE,
                    "kind": "drive#file",
                    "name": "1712414020_159_SVID_20190824_034848_1.mp4",
                    "size": str(LINEAGE_SIZE),
                    "hash": LINEAGE_HASH,
                    "mime_type": "video/mp4",
                    "params": {
                        "anonymous_play_range": "0.40",
                        "anonymous_play_seconds": "120",
                        "original_share_id": FLD_SHARE,
                        "original_file_id": FLD_FILE,
                        "platform": "Share",
                        "platform_icon": "platform_restore",
                    },
                }
            ],
            "user_info": {"user_id": "owner-pub"},
            "pass_code_token": "",
        }
    if share_id == FLD_SHARE:
        return {
            "share_status": "OK",
            "share_status_text": "",
            "file_num": "1",
            "title": "FLD",
            "params": {"anonymous_play_seconds": "600"},
            "files": [
                {
                    "id": FLD_FOLDER,
                    "kind": "drive#folder",
                    "name": "sample-folder",
                    "size": "0",
                    "params": {
                        "original_share_id": VOZG_SHARE,
                        "original_file_id": VOZG_FOLDER,
                        "total_count": "27",
                    },
                }
            ],
            "user_info": {"user_id": "owner-fld"},
            "pass_code_token": "",
        }
    if share_id == VOZG_SHARE:
        return {
            "share_status": "OK",
            "share_status_text": "",
            "file_num": "1",
            "title": "sample-parent",
            "params": {"anonymous_play_seconds": "120"},
            "files": [
                {
                    "id": VOZG_FOLDER,
                    "kind": "drive#folder",
                    "name": "sample-parent",
                    "size": "0",
                    "params": {
                        "original_share_id": VOZR_SHARE,
                        "original_file_id": "",
                        "total_count": "1212",
                    },
                }
            ],
            "user_info": {"user_id": "owner-vozg"},
            "pass_code_token": "",
        }
    if share_id == VOZR_SHARE:
        return {
            "share_status": "SENSITIVE_RESOURCE",
            "share_status_text": "Copyrighted or harmful content, no longer valid.",
            "file_num": "1",
            "title": "VOZR",
            "params": {},
            "files": [],
            "user_info": {"user_id": "owner-vozr"},
            "pass_code_token": "",
        }
    if share_id in REF_SHARES:
        return {
            "share_status": "OK",
            "share_status_text": "",
            "file_num": "1",
            "title": f"ref-{share_id}",
            "params": {"anonymous_play_seconds": "120"},
            "files": [
                {
                    "id": f"REFROOT{share_id[-8:]}",
                    "kind": "drive#file",
                    "name": "ref.bin",
                    "size": "1",
                    "hash": f"REFHASH{share_id[-8:]}",
                    "mime_type": "application/octet-stream",
                    "params": {"original_share_id": "", "original_file_id": ""},
                }
            ],
            "user_info": {"user_id": "owner-ref"},
            "pass_code_token": "",
        }
    return None


def _lineage_file_info(share_id: str, file_id: str, base: str) -> dict[str, Any] | None:
    share = _lineage_share(share_id, base)
    if share is None:
        return None
    if share_id == PUB_SHARE and file_id == PUB_FILE:
        item = dict(share["files"][0])
        item["medias"] = [
            {
                "is_origin": True,
                "category": "category_origin",
                "media_name": "ORIGINAL",
                "link": {
                    "url": f"{base}/cdn/window/{share_id}/{file_id}?t=0&f={LINEAGE_SIZE}&expire=4102444800"
                },
            }
        ]
        return item
    for item in share["files"]:
        if item["id"] == file_id:
            return item
    if share_id == FLD_SHARE and file_id == FLD_FILE:
        return {
            "id": FLD_FILE,
            "kind": "drive#file",
            "name": "1712414020_159_SVID_20190824_034848_1.mp4",
            "size": str(LINEAGE_SIZE),
            "hash": LINEAGE_HASH,
            "mime_type": "video/mp4",
            "parent_id": "303",
            "params": {
                "anonymous_play_range": "0.67",
                "anonymous_play_seconds": "600",
                "original_share_id": "",
                "original_file_id": "G__FAKEGLOBAL0000001",
                "platform": "Offline",
                "platform_icon": "platform_offline",
            },
            "medias": [
                {
                    "is_origin": True,
                    "category": "category_origin",
                    "media_name": "ORIGINAL",
                    "link": {
                        "url": f"{base}/cdn/window/{share_id}/{file_id}?t=0&f={LINEAGE_SIZE}&expire=4102444800"
                    },
                }
            ],
        }
    return None


def _lineage_folder_items(share_id: str, parent_id: str, base: str) -> list[dict[str, Any]]:
    if share_id != FLD_SHARE or parent_id != FLD_FOLDER:
        return []
    items = [
        {
            "id": FLD_FILE,
            "kind": "drive#file",
            "name": "1712414020_159_SVID_20190824_034848_1.mp4",
            "size": str(LINEAGE_SIZE),
            "hash": LINEAGE_HASH,
            "mime_type": "video/mp4",
            "params": {
                "original_share_id": "",
                "original_file_id": "G__FAKEGLOBAL0000001",
                "platform_icon": "platform_offline",
            },
        }
    ]
    for index, share_id_ref in enumerate(REF_SHARES, start=1):
        items.append(
            {
                "id": f"TENANTFILE{index:013d}",
                "kind": "drive#file",
                "name": f"tenant-{index}.bin",
                "size": "1",
                "hash": f"TENANTHASH{index:013d}",
                "mime_type": "application/octet-stream",
                "params": {
                    "original_share_id": share_id_ref,
                    "original_file_id": f"REFROOT{share_id_ref[-8:]}",
                },
            }
        )
    return items


def _b64(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _access_token() -> str:
    return f"eyJhbGciOiJub25lIn0.{_b64({'sub': 'fake-sub-1'})}.sig"


def _share(share_id: str, base: str = "") -> dict[str, Any] | None:
    lineage = _lineage_share(share_id, base)
    if lineage is not None:
        return lineage
    if share_id == ANON_SHARE:
        return {
            "share_status": "OK",
            "share_status_text": "",
            "file_num": "1",
            "title": "anon-share",
            "params": {"anonymous_play_seconds": "0"},
            "files": [
                {
                    "id": ANON_FILE,
                    "kind": "drive#file",
                    "name": "anon.bin",
                    "size": str(SIZE),
                    "hash": HASH,
                    "mime_type": "application/octet-stream",
                    "params": {},
                }
            ],
            "user_info": {"user_id": "owner-anon"},
            "pass_code_token": "",
        }
    if share_id == GRAPH_A_SHARE:
        return {
            "share_status": "OK",
            "share_status_text": "",
            "file_num": "1",
            "title": "graph-a",
            "params": {"anonymous_play_seconds": "120"},
            "files": [
                {
                    "id": GRAPH_A_ROOT,
                    "kind": "drive#folder",
                    "name": "graph-a-root",
                    "size": "0",
                    "params": {"total_count": "3"},
                }
            ],
            "user_info": {"user_id": "owner-a"},
            "pass_code_token": "",
        }
    if share_id == GRAPH_B_SHARE:
        return {
            "share_status": "OK",
            "share_status_text": "",
            "file_num": "1",
            "title": "graph-b",
            "params": {"anonymous_play_seconds": "120"},
            "files": [
                {
                    "id": GRAPH_B_ROOT,
                    "kind": "drive#folder",
                    "name": "graph-b-root",
                    "size": "0",
                    "params": {"total_count": "2"},
                }
            ],
            "user_info": {"user_id": "owner-b"},
            "pass_code_token": "",
        }
    if share_id == GRAPH_C_SHARE:
        return {
            "share_status": "OK",
            "share_status_text": "",
            "file_num": "1",
            "title": "graph-c",
            "params": {"anonymous_play_seconds": "120"},
            "files": [
                {
                    "id": GRAPH_C_ROOT,
                    "kind": "drive#folder",
                    "name": "graph-c-root",
                    "size": "0",
                    "params": {"total_count": "1"},
                }
            ],
            "user_info": {"user_id": "owner-c"},
            "pass_code_token": "",
        }
    if share_id == GRAPH_D_SHARE:
        return {
            "share_status": "OK",
            "share_status_text": "",
            "file_num": "1",
            "title": "graph-d",
            "params": {"anonymous_play_seconds": "120"},
            "files": [
                {
                    "id": GRAPH_D_FILE,
                    "kind": "drive#file",
                    "name": "graph-d.bin",
                    "size": "1",
                    "hash": "GRAPHDHASH000000000000000000000000000000",
                    "mime_type": "application/octet-stream",
                    "params": {"original_share_id": "", "original_file_id": ""},
                }
            ],
            "user_info": {"user_id": "owner-d"},
            "pass_code_token": "",
        }
    if share_id == NESTED_SHARE:
        return {
            "share_status": "OK",
            "share_status_text": "",
            "file_num": "1",
            "title": "nested-share",
            "params": {"anonymous_play_seconds": "0"},
            "files": [
                {
                    "id": NESTED_ROOT,
                    "kind": "drive#folder",
                    "name": "root",
                    "size": "0",
                    "params": {"total_count": "2"},
                }
            ],
            "user_info": {"user_id": "owner-nested"},
            "pass_code_token": "",
        }
    if share_id == FOLDER_SHARE:
        return {
            "share_status": "OK",
            "share_status_text": "",
            "file_num": "1",
            "title": "folder-share",
            "params": {"anonymous_play_seconds": "0"},
            "files": [
                {
                    "id": FOLDER_ROOT,
                    "kind": "drive#folder",
                    "name": "folder-root",
                    "size": "0",
                    "params": {"total_count": "2"},
                }
            ],
            "user_info": {"user_id": "owner-folder"},
            "pass_code_token": "",
        }
    if share_id == SLOW_SHARE:
        return {
            "share_status": "OK",
            "share_status_text": "",
            "file_num": "1",
            "title": "slow-share",
            "params": {"anonymous_play_seconds": "0"},
            "files": [
                {
                    "id": SLOW_FILE,
                    "kind": "drive#file",
                    "name": "slow.bin",
                    "size": str(SLOW_SIZE),
                    "hash": SLOW_HASH,
                    "mime_type": "application/octet-stream",
                    "params": {},
                }
            ],
            "user_info": {"user_id": "owner-slow"},
            "pass_code_token": "",
        }
    if share_id in (ACCOUNT_SHARE, FAIL_SHARE, BADLINK_SHARE):
        file_id = {
            ACCOUNT_SHARE: ACCOUNT_FILE,
            FAIL_SHARE: FAIL_FILE,
            BADLINK_SHARE: BADLINK_FILE,
        }[share_id]
        name = {
            ACCOUNT_SHARE: "account.mp4",
            FAIL_SHARE: "fail.mp4",
            BADLINK_SHARE: "bad.mp4",
        }[share_id]
        return {
            "share_status": "OK",
            "share_status_text": "",
            "file_num": "1",
            "title": "account-share",
            "params": {"anonymous_play_seconds": "120"},
            "files": [
                {
                    "id": file_id,
                    "kind": "drive#file",
                    "name": name,
                    "size": str(SIZE),
                    "hash": HASH,
                    "mime_type": "video/mp4",
                    "params": {
                        "anonymous_play_range": "0.40",
                        "anonymous_play_seconds": "120",
                        "original_share_id": "",
                        "original_file_id": "",
                    },
                }
            ],
            "user_info": {"user_id": "owner-account"},
            "pass_code_token": "",
        }
    return None


def _file_info(share_id: str, file_id: str, base: str) -> dict[str, Any] | None:
    lineage = _lineage_file_info(share_id, file_id, base)
    if lineage is not None:
        return lineage
    share = _share(share_id, base)
    if share is None:
        return None
    if share_id == GRAPH_A_SHARE and file_id in (GRAPH_A_FILE1, GRAPH_A_FILE2, GRAPH_A_FILE3):
        refs = {
            GRAPH_A_FILE1: (GRAPH_B_SHARE, GRAPH_B_FILE1),
            GRAPH_A_FILE2: (GRAPH_C_SHARE, GRAPH_C_FILE1),
            GRAPH_A_FILE3: ("", ""),
        }
        origin_share, origin_file = refs[file_id]
        return {
            "id": file_id,
            "kind": "drive#file",
            "name": f"{file_id.lower()}.bin",
            "size": "1",
            "hash": f"HASH{file_id[-8:]}",
            "mime_type": "application/octet-stream",
            "params": {
                "original_share_id": origin_share,
                "original_file_id": origin_file,
            },
        }
    if share_id == GRAPH_B_SHARE and file_id in (GRAPH_B_FILE1, GRAPH_B_FILE2):
        origin_share = GRAPH_D_SHARE if file_id == GRAPH_B_FILE1 else GRAPH_C_SHARE
        origin_file = GRAPH_D_FILE if file_id == GRAPH_B_FILE1 else GRAPH_C_FILE1
        return {
            "id": file_id,
            "kind": "drive#file",
            "name": f"{file_id.lower()}.bin",
            "size": "1",
            "hash": f"HASH{file_id[-8:]}",
            "mime_type": "application/octet-stream",
            "params": {
                "original_share_id": origin_share,
                "original_file_id": origin_file,
            },
        }
    if share_id == GRAPH_C_SHARE and file_id == GRAPH_C_FILE1:
        return {
            "id": file_id,
            "kind": "drive#file",
            "name": "graph-c-file.bin",
            "size": "1",
            "hash": "HASHGRAPHCFILE01",
            "mime_type": "application/octet-stream",
            "params": {
                "original_share_id": GRAPH_D_SHARE,
                "original_file_id": GRAPH_D_FILE,
            },
        }
    if share_id == GRAPH_D_SHARE and file_id == GRAPH_D_FILE:
        return {
            "id": file_id,
            "kind": "drive#file",
            "name": "graph-d.bin",
            "size": "1",
            "hash": "GRAPHDHASH000000000000000000000000000000",
            "mime_type": "application/octet-stream",
            "params": {"original_share_id": "", "original_file_id": ""},
        }
    if share_id == NESTED_SHARE and file_id == NESTED_FILE:
        return {
            "id": NESTED_FILE,
            "kind": "drive#file",
            "name": "small.bin",
            "size": str(len(SMALL_DATA)),
            "hash": SMALL_HASH,
            "mime_type": "application/octet-stream",
            "web_content_link": f"{base}/cdn/anon/{NESTED_SHARE}/{NESTED_FILE}?expire=4102444800",
            "params": {},
        }
    if share_id == FOLDER_SHARE and file_id in (FOLDER_FILE1, FOLDER_FILE2):
        small = file_id == FOLDER_FILE2
        data = {
            "id": file_id,
            "kind": "drive#file",
            "name": "small.bin" if small else "anon-copy.bin",
            "size": str(len(SMALL_DATA) if small else SIZE),
            "hash": SMALL_HASH if small else HASH,
            "mime_type": "application/octet-stream",
            "web_content_link": f"{base}/cdn/anon/{FOLDER_SHARE}/{file_id}?expire=4102444800",
            "params": {},
        }
        return data
    file = next((item for item in share["files"] if item["id"] == file_id), None)
    if file is None:
        return None
    data = dict(file)
    if share_id == ANON_SHARE:
        data["web_content_link"] = f"{base}/cdn/anon/{ANON_SHARE}/{ANON_FILE}?expire=4102444800"
        data["params"] = dict(data.get("params") or {})
    elif share_id == SLOW_SHARE:
        data["web_content_link"] = f"{base}/cdn/slow/{SLOW_SHARE}/{SLOW_FILE}?expire=4102444800"
        data["params"] = dict(data.get("params") or {})
    else:
        data["web_content_link"] = ""
        data["params"] = dict(data.get("params") or {})
        data["medias"] = [
            {
                "is_origin": True,
                "category": "category_origin",
                "media_name": "ORIGINAL",
                "link": {
                    "url": f"{base}/cdn/anon-video/{share_id}/{file_id}?t=0&f={SIZE}&expire=4102444800"
                },
            }
        ]
    return data


def _drive_file(file_id: str, base: str) -> dict[str, Any] | None:
    share_id = {
        DRIVE_FILE: ACCOUNT_SHARE,
        FAIL_DRIVE_FILE: FAIL_SHARE,
        BAD_DRIVE_FILE: BADLINK_SHARE,
    }.get(file_id)
    if share_id is None:
        return None
    share = _share(share_id)
    if share is None:
        return None
    source = share["files"][0]
    name = source["name"]
    if file_id == BAD_DRIVE_FILE:
        wcl = f"{base}/cdn/broken/{file_id}?expire=4102444800"
    else:
        wcl = f"{base}/cdn/account/{file_id}?expire=4102444800"
    return {
        "id": file_id,
        "kind": "drive#file",
        "name": name,
        "size": str(SIZE),
        "hash": HASH,
        "mime_type": "video/mp4",
        "phase": "PHASE_TYPE_COMPLETE",
        "web_content_link": wcl,
        "params": {
            "original_share_id": share_id,
            "original_file_id": source["id"],
            "global_file_kind": 0,
        },
    }


def _range_response(request: Request, data: bytes, *, window_end: int | None = None) -> Response:
    range_header = request.headers.get("range")
    total = len(data)
    if range_header and range_header.startswith("bytes="):
        spec = range_header.split("=", 1)[1]
        start_s, _, end_s = spec.partition("-")
        try:
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
        except ValueError:
            return Response(status_code=416)
        if window_end is not None:
            end = min(end, window_end)
        if start >= total or start > end:
            return Response(status_code=416)
        end = min(end, total - 1)
        chunk = data[start : end + 1]
        return Response(
            content=chunk,
            status_code=206,
            headers={
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(chunk)),
            },
        )
    return Response(
        content=data,
        status_code=200,
        headers={"Accept-Ranges": "bytes", "Content-Length": str(total)},
    )


def create_app() -> FastAPI:
    app = FastAPI()

    @app.get("/__state")
    async def state() -> dict[str, Any]:
        return STATE

    @app.post("/__reset")
    async def reset() -> dict[str, Any]:
        STATE.update(
            {
                "restore_calls": 0,
                "delete_calls": 0,
                "trash_calls": 0,
                "delete_failures": 0,
                "events": [],
                "cdn_requests": [],
                "captcha_calls": 0,
                "file_info_calls": 0,
            }
        )
        return STATE

    @app.post("/v1/shield/captcha/init")
    async def captcha() -> dict[str, Any]:
        STATE["captcha_calls"] += 1
        return {"captcha_token": "fake-captcha-token", "expires_in": 300}

    @app.post("/v1/auth/token")
    async def auth_token(request: Request) -> JSONResponse:
        body = await request.json()
        if body.get("refresh_token") != "fake-refresh-token":
            return JSONResponse(
                status_code=401,
                content={"error": "invalid_grant", "error_description": "bad refresh token"},
            )
        return JSONResponse(
            {
                "access_token": _access_token(),
                "refresh_token": "fake-refresh-token",
                "expires_in": 7200,
            }
        )

    def _require_drive(request: Request) -> JSONResponse | None:
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error": "unauthenticated", "error_description": "missing token"},
            )
        if not request.headers.get("x-captcha-token"):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "captcha_invalid",
                    "error_code": 9,
                    "error_description": "captcha required",
                },
            )
        return None

    @app.get("/drive/v1/share")
    async def share(share_id: str, request: Request) -> JSONResponse:
        snapshot = _share(share_id, str(request.base_url).rstrip("/"))
        if snapshot is None:
            return JSONResponse(status_code=404, content={"error": "share_not_found"})
        return JSONResponse(content=snapshot)

    @app.get("/drive/v1/share/file_info")
    async def share_file_info(share_id: str, file_id: str, request: Request) -> JSONResponse:
        STATE["file_info_calls"] += 1
        base = str(request.base_url).rstrip("/")
        info = _file_info(share_id, file_id, base)
        if info is None:
            return JSONResponse(status_code=404, content={"error": "file_not_found"})
        return JSONResponse(
            content={"share_status": "OK", "share_status_text": "", "file_info": info}
        )

    @app.post("/drive/v1/share:batch_file_info")
    async def batch_file_info(request: Request) -> JSONResponse:
        body = await request.json()
        base = str(request.base_url).rstrip("/")
        files = [
            item
            for fid in body.get("file_ids") or []
            if (item := _file_info(body.get("share_id"), fid, base)) is not None
        ]
        return JSONResponse(content={"files": files})

    @app.get("/drive/v1/share/detail")
    async def share_detail(share_id: str, parent_id: str, request: Request) -> JSONResponse:
        base = str(request.base_url).rstrip("/")
        if share_id == GRAPH_A_SHARE and parent_id == GRAPH_A_ROOT:
            return JSONResponse(
                content={
                    "files": [
                        _file_info(GRAPH_A_SHARE, GRAPH_A_FILE1, base),
                        _file_info(GRAPH_A_SHARE, GRAPH_A_FILE2, base),
                        _file_info(GRAPH_A_SHARE, GRAPH_A_FILE3, base),
                    ],
                    "next_page_token": "",
                }
            )
        if share_id == GRAPH_B_SHARE and parent_id == GRAPH_B_ROOT:
            return JSONResponse(
                content={
                    "files": [
                        _file_info(GRAPH_B_SHARE, GRAPH_B_FILE1, base),
                        _file_info(GRAPH_B_SHARE, GRAPH_B_FILE2, base),
                    ],
                    "next_page_token": "",
                }
            )
        if share_id == GRAPH_C_SHARE and parent_id == GRAPH_C_ROOT:
            return JSONResponse(
                content={
                    "files": [_file_info(GRAPH_C_SHARE, GRAPH_C_FILE1, base)],
                    "next_page_token": "",
                }
            )
        if share_id == NESTED_SHARE and parent_id == NESTED_ROOT:
            return JSONResponse(
                content={
                    "files": [
                        {
                            "id": NESTED_SUB,
                            "kind": "drive#folder",
                            "name": "sub",
                            "size": "0",
                            "params": {"total_count": "1"},
                        },
                        _file_info(NESTED_SHARE, NESTED_FILE, base),
                    ],
                    "next_page_token": "",
                }
            )
        if share_id == NESTED_SHARE and parent_id == NESTED_SUB:
            return JSONResponse(
                content={
                    "files": [_file_info(NESTED_SHARE, NESTED_FILE, base)],
                    "next_page_token": "",
                }
            )
        if share_id == FOLDER_SHARE and parent_id == FOLDER_ROOT:
            return JSONResponse(
                content={
                    "files": [
                        _file_info(FOLDER_SHARE, FOLDER_FILE1, base),
                        _file_info(FOLDER_SHARE, FOLDER_FILE2, base),
                    ],
                    "next_page_token": "",
                }
            )
        items = _lineage_folder_items(share_id, parent_id, base)
        return JSONResponse(content={"files": items, "next_page_token": ""})

    @app.get("/drive/v1/about")
    async def about(request: Request):
        denied = _require_drive(request)
        if denied:
            return denied
        return {
            "quota": {"limit": 6 * 1024**3, "usage": 0},
            "cloud_download": {"limit": 5, "usage": 0},
        }

    @app.post("/drive/v1/share/restore")
    async def restore(request: Request):
        denied = _require_drive(request)
        if denied:
            return denied
        body = await request.json()
        STATE["restore_calls"] += 1
        STATE["events"].append({"type": "restore", "body": body, "t": time.time()})
        share_id = body.get("share_id")
        folder = RESTORE_FOLDER
        drive_id = DRIVE_FILE
        if share_id == FAIL_SHARE:
            drive_id = FAIL_DRIVE_FILE
        elif share_id == BADLINK_SHARE:
            drive_id = BAD_DRIVE_FILE
        return {
            "share_status": "OK",
            "file_id": folder,
            "restore_status": "RESTORE_START",
            "restore_task_id": TASK_ID,
            "drive_file_id": drive_id,
        }

    @app.get("/drive/v1/tasks/{task_id}")
    async def task_status(task_id: str, request: Request):
        denied = _require_drive(request)
        if denied:
            return denied
        STATE["events"].append({"type": "task", "task_id": task_id, "t": time.time()})
        return {"id": task_id, "message": "Completed", "phase": "PHASE_TYPE_COMPLETE"}

    @app.get("/drive/v1/files")
    async def list_files(request: Request, parent_id: str = ""):
        denied = _require_drive(request)
        if denied:
            return denied
        STATE["events"].append({"type": "list", "parent_id": parent_id, "t": time.time()})
        if parent_id == RESTORE_FOLDER:
            share = _share(ACCOUNT_SHARE)
            fail_share = _share(FAIL_SHARE)
            bad_share = _share(BADLINK_SHARE)
            files = []
            for share, file_id, drive_id in (
                (share, ACCOUNT_FILE, DRIVE_FILE),
                (fail_share, FAIL_FILE, FAIL_DRIVE_FILE),
                (bad_share, BADLINK_FILE, BAD_DRIVE_FILE),
            ):
                if share is None:
                    continue
                source = share["files"][0]
                files.append(
                    {
                        "id": drive_id,
                        "kind": "drive#file",
                        "name": source["name"],
                        "size": str(SIZE),
                        "hash": HASH,
                        "mime_type": "video/mp4",
                        "phase": "PHASE_TYPE_COMPLETE",
                        "params": {
                            "original_share_id": share["files"][0].get("params", {}).get(
                                "original_share_id", ""
                            )
                            or (
                                ACCOUNT_SHARE
                                if drive_id == DRIVE_FILE
                                else FAIL_SHARE
                                if drive_id == FAIL_DRIVE_FILE
                                else BADLINK_SHARE
                            ),
                            "original_file_id": source["id"],
                        },
                    }
                )
            return {"files": files, "next_page_token": ""}
        return {"files": [], "next_page_token": ""}

    @app.get("/drive/v1/files/{file_id}")
    async def get_file(file_id: str, request: Request, usage: str = "FETCH"):
        denied = _require_drive(request)
        if denied:
            return denied
        base = str(request.base_url).rstrip("/")
        data = _drive_file(file_id, base)
        if data is None:
            return JSONResponse(status_code=404, content={"error": "file_not_found"})
        STATE["events"].append({"type": "fetch", "file_id": file_id, "t": time.time()})
        return data

    @app.post("/drive/v1/files:batchTrash")
    async def batch_trash(request: Request):
        denied = _require_drive(request)
        if denied:
            return denied
        body = await request.json()
        ids = body.get("ids") or []
        STATE["trash_calls"] += len(ids)
        for file_id in ids:
            STATE["events"].append({"type": "trash", "file_id": file_id, "t": time.time()})
        return {"task_id": "TRASH0000000000001"}

    @app.post("/drive/v1/files:batchDelete")
    async def batch_delete(request: Request):
        denied = _require_drive(request)
        if denied:
            return denied
        body = await request.json()
        ids = body.get("ids") or []
        for file_id in ids:
            STATE["delete_calls"] += 1
            STATE["events"].append({"type": "delete", "file_id": file_id, "t": time.time()})
            if file_id == FAIL_DRIVE_FILE and STATE["delete_failures"] < 3:
                STATE["delete_failures"] += 1
                return JSONResponse(status_code=500, content={"error": "server_error"})
            if file_id == FAIL_DRIVE_FILE and STATE["delete_failures"] == 3:
                STATE["delete_failures"] += 1
                return JSONResponse(status_code=404, content={"error": "file_not_found"})
        return {"task_id": "DELETE0000000000001"}

    @app.delete("/drive/v1/files/{file_id}")
    async def delete_file(file_id: str, request: Request):
        denied = _require_drive(request)
        if denied:
            return denied
        STATE["delete_calls"] += 1
        STATE["events"].append({"type": "delete", "file_id": file_id, "t": time.time()})
        if file_id == FAIL_DRIVE_FILE and STATE["delete_failures"] < 3:
            STATE["delete_failures"] += 1
            return JSONResponse(status_code=500, content={"error": "server_error"})
        if file_id == FAIL_DRIVE_FILE and STATE["delete_failures"] == 3:
            STATE["delete_failures"] += 1
            return JSONResponse(status_code=404, content={"error": "file_not_found"})
        return {}

    @app.get("/cdn/slow/{token:path}")
    async def slow_cdn(token: str, request: Request):
        range_header = request.headers.get("range")
        start, end = 0, SLOW_SIZE - 1
        status = 200
        headers = {"Accept-Ranges": "bytes"}
        if range_header and range_header.startswith("bytes="):
            spec = range_header.split("=", 1)[1]
            start_s, _, end_s = spec.partition("-")
            try:
                start = int(start_s)
                end = min(int(end_s) if end_s else SLOW_SIZE - 1, SLOW_SIZE - 1)
            except ValueError:
                return Response(status_code=416)
            if start >= SLOW_SIZE or start > end:
                return Response(status_code=416)
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{SLOW_SIZE}"
        headers["Content-Length"] = str(end - start + 1)

        async def generate():
            chunk = 256 * 1024
            for offset in range(start, end + 1, chunk):
                yield SLOW_DATA[offset : min(offset + chunk, end + 1)]
                await asyncio.sleep(0.2)

        return StreamingResponse(generate(), status_code=status, headers=headers)

    @app.get("/cdn/{kind}/{token:path}")
    async def cdn(kind: str, token: str, request: Request):
        STATE["cdn_requests"].append(
            {
                "kind": kind,
                "token": token,
                "range": request.headers.get("range"),
                "ua": request.headers.get("user-agent", ""),
                "t": time.time(),
            }
        )
        if kind == "broken":
            return Response(status_code=500)
        if kind == "anon" and (FOLDER_FILE2 in token or NESTED_FILE in token):
            return _range_response(request, SMALL_DATA)
        if kind == "anon-video":
            return _range_response(request, DATA, window_end=int(SIZE * 0.40))
        if kind == "window":
            share_id = token.split("/", 1)[0]
            head_end = PUB_HEAD if share_id == PUB_SHARE else FLD_HEAD
            range_header = request.headers.get("range")
            total = LINEAGE_SIZE
            if not range_header:
                return Response(
                    status_code=206,
                    headers={"Content-Range": f"bytes 0-{head_end}/{total}", "Accept-Ranges": "bytes"},
                )
            spec = range_header.split("=", 1)[1]
            start_s, _, end_s = spec.partition("-")
            try:
                start = int(start_s)
            except ValueError:
                return Response(status_code=416)
            if start <= head_end or start >= TAIL_START:
                end = min(int(end_s) if end_s else start, total - 1)
                return Response(
                    status_code=206,
                    content=b"\x00",
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{total}",
                        "Accept-Ranges": "bytes",
                    },
                )
            return Response(status_code=416)
        return _range_response(request, DATA)

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19000)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
