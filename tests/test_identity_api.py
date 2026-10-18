"""身分 API（BE 為準）：建帳號 → 綁定碼 → 核銷 → 查詢 → 停用 → 重新綁定。需要本機 PostgreSQL 與 Redis。"""

import re
import uuid

import httpx
import pytest
from fastapi import FastAPI

from app.api import routes_identity
from app.config import settings

pytestmark = pytest.mark.asyncio(loop_scope="session")

_TOKEN = "test-identity-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "xiaozhao_api_token", _TOKEN)


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(routes_identity.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://be")


def _line_user_id() -> str:
    return f"U_test_{uuid.uuid4().hex}"


async def _create(client, role="caregiver") -> dict:
    response = await client.post("/v1/users", json={"name": "陳照護員", "role": role}, headers=_AUTH)
    assert response.status_code == 201
    return response.json()


async def _code(client, user_id: str) -> str:
    response = await client.post(f"/v1/users/{user_id}/binding-codes", json={}, headers=_AUTH)
    assert response.status_code == 201
    return response.json()["code"]


async def test_full_binding_flow_returns_pseudonym_and_role():
    line_user_id = _line_user_id()
    async with _client() as client:
        user = await _create(client)
        assert re.fullmatch(r"CG-\d{4}", user["pseudonym"])

        code = await _code(client, user["user_id"])
        bound = await client.post("/v1/bindings", json={"code": code, "line_user_id": line_user_id}, headers=_AUTH)
        assert bound.status_code == 200
        assert bound.json()["user_id"] == user["user_id"]

        found = await client.get(f"/v1/identities/line/{line_user_id}", headers=_AUTH)
        assert found.status_code == 200
        assert found.json() == user


async def test_supervisor_gets_sv_pseudonym():
    async with _client() as client:
        assert (await _create(client, "supervisor"))["pseudonym"].startswith("SV-")


async def test_rejects_unknown_role():
    async with _client() as client:
        response = await client.post("/v1/users", json={"name": "x", "role": "admin"}, headers=_AUTH)
    assert response.status_code == 400


async def test_reissuing_code_voids_previous_one():
    async with _client() as client:
        user = await _create(client)
        old = await _code(client, user["user_id"])
        new = await _code(client, user["user_id"])
        if old == new:
            pytest.skip("兩次亂數碰巧相同")
        response = await client.post("/v1/bindings", json={"code": old, "line_user_id": _line_user_id()}, headers=_AUTH)
    assert response.status_code == 400


async def test_line_account_already_bound_is_conflict_and_code_not_consumed():
    line_user_id = _line_user_id()
    async with _client() as client:
        first = await _create(client)
        await client.post(
            "/v1/bindings", json={"code": await _code(client, first["user_id"]), "line_user_id": line_user_id},
            headers=_AUTH,
        )
        second = await _create(client)
        code = await _code(client, second["user_id"])
        conflict = await client.post("/v1/bindings", json={"code": code, "line_user_id": line_user_id}, headers=_AUTH)
        assert conflict.status_code == 409
        other = await client.post("/v1/bindings", json={"code": code, "line_user_id": _line_user_id()}, headers=_AUTH)
        assert other.status_code == 200


async def test_disable_unbinds_and_allows_rebinding_same_line_account():
    line_user_id = _line_user_id()
    async with _client() as client:
        user = await _create(client)
        await client.post(
            "/v1/bindings", json={"code": await _code(client, user["user_id"]), "line_user_id": line_user_id},
            headers=_AUTH,
        )
        assert (await client.post(f"/v1/users/{user['user_id']}/disable", headers=_AUTH)).status_code == 200
        assert (await client.get(f"/v1/identities/line/{line_user_id}", headers=_AUTH)).status_code == 404

        replacement = await _create(client)
        rebound = await client.post(
            "/v1/bindings", json={"code": await _code(client, replacement["user_id"]), "line_user_id": line_user_id},
            headers=_AUTH,
        )
        assert rebound.status_code == 200
        found = await client.get(f"/v1/identities/line/{line_user_id}", headers=_AUTH)
        assert found.json()["user_id"] == replacement["user_id"]


async def test_binding_attempts_are_rate_limited():
    line_user_id = _line_user_id()
    async with _client() as client:
        statuses = [
            (await client.post("/v1/bindings", json={"code": "000000", "line_user_id": line_user_id}, headers=_AUTH)).status_code
            for _ in range(6)
        ]
    assert statuses[:5] == [400] * 5
    assert statuses[5] == 429


async def test_requires_token():
    async with _client() as client:
        assert (await client.get(f"/v1/identities/line/{_line_user_id()}")).status_code == 401
