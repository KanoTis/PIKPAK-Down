from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import httpx
import pytest
from fake_pikpak_server import (
    FLD_FILE,
    FLD_FOLDER,
    FLD_SHARE,
    GRAPH_A_SHARE,
    GRAPH_B_SHARE,
    GRAPH_C_SHARE,
    GRAPH_D_SHARE,
    PUB_SHARE,
    REF_SHARES,
)


async def _dispatch_lineage() -> None:
    from app.db import get_session_factory
    from app.services.lineage import dispatch_lineage

    factory = get_session_factory()
    async with factory() as session:
        await dispatch_lineage(session)


async def run_lineage_until(run_id: str, timeout: float = 60.0):
    from app.db import get_session_factory
    from app.models import LineageRun

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await _dispatch_lineage()
        factory = get_session_factory()
        async with factory() as session:
            run = await session.get(LineageRun, run_id)
            if run is not None and run.status in ("DONE", "FAILED", "CANCELLED"):
                return run
        await asyncio.sleep(0.05)
    raise AssertionError(f"lineage run {run_id} did not finish in time")


async def create_lineage(**kwargs):
    from app.db import get_session_factory
    from app.services.lineage import create_run

    factory = get_session_factory()
    async with factory() as session:
        run = await create_run(session, **kwargs)
        await session.commit()
        return run.id


@pytest.mark.asyncio
async def test_direct_chain_and_window_union(env, fake_server) -> None:
    from app.db import get_session_factory
    from app.services.lineage import build_report

    run_id = await create_lineage(
        input_value=f"https://mypikpak.com/s/{PUB_SHARE}",
        include_tree=False,
        probe_windows=True,
    )
    run = await run_lineage_until(run_id)
    assert run.status == "DONE"
    assert run.truncated is False

    factory = get_session_factory()
    async with factory() as session:
        report = await build_report(session, run)

    chain = report["direct_chain"]
    assert len(chain) == 3
    assert chain[0]["share_id"] == PUB_SHARE
    assert chain[1]["share_id"] == FLD_SHARE
    assert chain[1]["file_id"] == FLD_FILE
    assert chain[2]["role"] == "terminal"
    assert chain[2]["reason"] == "global_file_id"

    windows = report["anonymous_windows"]
    assert windows["union_bytes"] == 427_914_437
    assert windows["missing"] == [[410_337_601, 444_472_334]]
    assert windows["conclusion"] == "anonymous_incomplete"
    assert len(windows["shares"]) == 2

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{fake_server}/__state")
        state = response.json()
    assert state["restore_calls"] == 0


@pytest.mark.asyncio
async def test_folder_tree_tenant_refs_and_folder_chain(env, fake_server) -> None:
    from app.db import get_session_factory
    from app.services.lineage import build_report

    run_id = await create_lineage(
        input_value=FLD_SHARE,
        file_id=FLD_FILE,
        include_tree=True,
        probe_windows=False,
    )
    run = await run_lineage_until(run_id, timeout=90)
    assert run.status == "DONE"
    assert run.truncated is False

    factory = get_session_factory()
    async with factory() as session:
        report = await build_report(session, run)

    assert report["direct_chain"][0]["file_id"] == FLD_FILE
    assert report["direct_chain"][-1]["reason"] == "global_file_id"

    folder_chain = report["folder_chain"]
    assert folder_chain[0]["file_id"] == FLD_FOLDER
    assert folder_chain[-1]["reason"] == "share_sensitive_resource"

    refs = report["tenant_refs"]
    assert len(refs) == len(REF_SHARES)
    ref_ids = {item["share_id"] for item in refs}
    assert ref_ids == set(REF_SHARES)
    assert all(item["ref_count"] == 1 for item in refs)

    direct_ids = {item.get("share_id") for item in report["direct_chain"]}
    assert not (ref_ids & direct_ids)

    copies = report["copies"]
    assert len(copies) == 1
    assert copies[0]["share_id"] == FLD_SHARE
    assert copies[0]["file_id"] == FLD_FILE


@pytest.mark.asyncio
async def test_budget_exhaustion_returns_done_truncated(env, fake_server) -> None:
    from app.db import get_session_factory
    from app.services.lineage import build_report

    env.lineage_max_requests = 1
    run_id = await create_lineage(
        input_value=f"https://mypikpak.com/s/{PUB_SHARE}",
        include_tree=False,
        probe_windows=False,
    )
    run = await run_lineage_until(run_id, timeout=30)
    assert run.status == "DONE"
    assert run.truncated is True
    assert run.termination == "budget_exhausted"
    factory = get_session_factory()
    async with factory() as session:
        report = await build_report(session, run)
    assert report["truncated"] is True


@pytest.mark.asyncio
async def test_fingerprint_mode_reads_historical_copies(env, fake_server) -> None:
    from app.db import get_session_factory
    from app.models import CopyIndex, LineageRun
    from app.services.lineage import build_report

    factory = get_session_factory()
    async with factory() as session:
        seed = LineageRun(
            id="seed-run",
            input_type="fingerprint",
            input_json="{}",
            hash="E409BE3D495A99CA8A13AF850A292244A8CDFDE5",
            size=462_049_171,
            status="DONE",
        )
        session.add(seed)
        await session.flush()
        session.add(
            CopyIndex(
                run_id=seed.id,
                share_id=FLD_SHARE,
                file_id=FLD_FILE,
                name="seed.mp4",
                kind="drive#file",
                size=462_049_171,
                hash="E409BE3D495A99CA8A13AF850A292244A8CDFDE5",
                is_video=True,
            )
        )
        await session.commit()

    run_id = await create_lineage(
        input_value="",
        hash_value="E409BE3D495A99CA8A13AF850A292244A8CDFDE5",
        size=462_049_171,
        include_tree=False,
        probe_windows=False,
    )
    run = await run_lineage_until(run_id, timeout=30)
    assert run.status == "DONE"
    async with factory() as session:
        report = await build_report(session, run)
    assert report["direct_chain"] == []
    assert len(report["copies"]) == 1
    assert report["copies"][0]["share_id"] == FLD_SHARE


@pytest.mark.asyncio
async def test_graph_bfs_dedupes_shares_and_aggregates_edges(env, fake_server) -> None:
    from app.db import get_session_factory
    from app.services.lineage import build_report
    from app.services.lineage_graph import build_share_detail

    run_id = await create_lineage(
        input_value=f"https://mypikpak.com/s/{GRAPH_A_SHARE}",
        graph=True,
    )
    run = await run_lineage_until(run_id, timeout=60)
    assert run.status == "DONE"

    factory = get_session_factory()
    async with factory() as session:
        report = await build_report(session, run)
        detail_d = await build_share_detail(session, run, GRAPH_D_SHARE)
        detail_a = await build_share_detail(session, run, GRAPH_A_SHARE)
        detail_b = await build_share_detail(session, run, GRAPH_B_SHARE)

    graph = report["graph"]
    assert graph["start"] == f"share:{GRAPH_A_SHARE}"
    assert graph["stats"]["shares"] == 4
    assert graph["stats"]["edges"] == 4
    assert graph["stats"]["files"] >= 6

    nodes = {node["share_id"]: node for node in graph["nodes"]}
    assert nodes[GRAPH_A_SHARE]["depth"] == 0
    assert nodes[GRAPH_A_SHARE]["role"] == "start"
    assert nodes[GRAPH_B_SHARE]["depth"] == 1
    assert nodes[GRAPH_C_SHARE]["depth"] == 1
    assert nodes[GRAPH_D_SHARE]["depth"] == 2
    assert nodes[GRAPH_D_SHARE]["incoming_count"] == 2

    edges = {(edge["from_share_id"], edge["to_share_id"]) for edge in graph["edges"]}
    assert edges == {
        (GRAPH_A_SHARE, GRAPH_B_SHARE),
        (GRAPH_A_SHARE, GRAPH_C_SHARE),
        (GRAPH_B_SHARE, GRAPH_D_SHARE),
        (GRAPH_C_SHARE, GRAPH_D_SHARE),
    }

    assert {item["share_id"] for item in detail_d["incoming"]} == {
        GRAPH_B_SHARE,
        GRAPH_C_SHARE,
    }
    assert detail_d["content"]["total"] == 1
    assert detail_a["content"]["total"] == 4
    assert detail_b["content"]["total"] == 1


@pytest.mark.asyncio
async def test_graph_api_creates_and_serves_node_detail(env, fake_server) -> None:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
        created = await client.post(
            "/api/lineage-runs",
            json={
                "input": f"https://mypikpak.com/s/{GRAPH_A_SHARE}",
                "graph": True,
            },
        )
        assert created.status_code == 202
        body = created.json()
        assert body["input_type"] == "share_graph"
        run_id = body["id"]

        run = await run_lineage_until(run_id, timeout=60)
        assert run.status == "DONE"

        response = await client.get(f"/api/lineage-runs/{run_id}")
        assert response.status_code == 200
        report = response.json()["report"]
        assert report["graph"]["stats"]["shares"] == 4

        detail = await client.get(
            f"/api/lineage-runs/{run_id}/graph/shares/{GRAPH_D_SHARE}",
            params={"limit": 10},
        )
        assert detail.status_code == 200
        detail_body = detail.json()
        assert detail_body["node"]["share_id"] == GRAPH_D_SHARE
        assert detail_body["content"]["total"] == 1
        assert {group["share_id"] for group in detail_body["incoming"]} == {
            GRAPH_B_SHARE,
            GRAPH_C_SHARE,
        }


@pytest.mark.asyncio
async def test_truncated_graph_marks_pending_as_budget(env, fake_server) -> None:
    from app.db import get_session_factory
    from app.models import LineageGraphShare
    from app.services.lineage import build_report
    from app.services.lineage_graph import build_share_detail

    run_id = await create_lineage(
        input_value=f"https://mypikpak.com/s/{GRAPH_A_SHARE}",
        graph=True,
    )
    run = await run_lineage_until(run_id, timeout=60)
    assert run.status == "DONE"

    factory = get_session_factory()
    async with factory() as session:
        run = await session.get(type(run), run_id)
        run.truncated = True
        run.termination = "budget_exhausted"
        session.add(
            LineageGraphShare(
                run_id=run.id,
                share_id="VPENDINGGRAPH00001",
                depth=2,
                role="parent",
                state="PENDING",
            )
        )
        await session.commit()
        report = await build_report(session, run)
        detail = await build_share_detail(session, run, "VPENDINGGRAPH00001")

    nodes = {node["share_id"]: node for node in report["graph"]["nodes"]}
    assert nodes["VPENDINGGRAPH00001"]["state"] == "BUDGET"
    assert nodes["VPENDINGGRAPH00001"]["error_code"] == "BUDGET_EXHAUSTED"
    assert detail["node"]["state"] == "BUDGET"


@pytest.mark.asyncio
async def test_graph_selection_limits_start_share(env, fake_server) -> None:
    from app.db import get_session_factory
    from app.services.lineage import build_report
    from app.services.lineage_graph import build_share_detail

    run_id = await create_lineage(
        input_value=f"https://mypikpak.com/s/{GRAPH_A_SHARE}",
        graph=True,
        items=[
            {
                "file_id": "GRAPHAFILE00000001",
                "kind": "drive#file",
                "name": "graphafile00000001.bin",
                "relative_path": "selected.bin",
                "size": 1,
            }
        ],
    )
    run = await run_lineage_until(run_id, timeout=60)
    assert run.status == "DONE"

    factory = get_session_factory()
    async with factory() as session:
        report = await build_report(session, run)
        detail_b = await build_share_detail(session, run, GRAPH_B_SHARE)

    graph = report["graph"]
    nodes = {node["share_id"]: node for node in graph["nodes"]}
    assert set(nodes) == {GRAPH_A_SHARE, GRAPH_B_SHARE, GRAPH_D_SHARE}
    assert detail_b["content"]["total"] == 1
    assert nodes[GRAPH_A_SHARE]["file_count"] == 1
    edges = {(edge["from_share_id"], edge["to_share_id"]) for edge in graph["edges"]}
    assert edges == {
        (GRAPH_A_SHARE, GRAPH_B_SHARE),
        (GRAPH_B_SHARE, GRAPH_D_SHARE),
    }

@pytest.mark.asyncio
async def test_resume_truncated_graph_run_resets_pending_work(env) -> None:
    from app.db import get_session_factory
    from app.models import LineageGraphShare, LineageRun
    from app.services.lineage import resume_run

    factory = get_session_factory()
    async with factory() as session:
        run = LineageRun(
            input_type="share_graph",
            input_json='{"input": "https://mypikpak.com/s/RESUMEGRAPH0001"}',
            share_id="RESUMEGRAPH0001",
            status="DONE",
            truncated=True,
            termination="budget_exhausted",
            finished_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        share = LineageGraphShare(
            run_id=run.id,
            share_id="RESUMEGRAPH0001",
            depth=0,
            role="start",
            state="ENUMERATING",
        )
        session.add(share)
        await session.commit()
        run_id = run.id
        share_id = share.id

    async with factory() as session:
        resumed = await resume_run(session, run_id)
        assert resumed.truncated is False
        assert resumed.termination is None
        assert resumed.finished_at is None
        assert resumed.status == "ENUMERATING"
        share = await session.get(LineageGraphShare, share_id)
        assert share.state == "PENDING"
