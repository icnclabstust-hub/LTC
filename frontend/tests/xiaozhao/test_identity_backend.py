"""帳號與綁定以後端為準（BACKEND_MODE=http）：以假的後端身分 API 驗證本機快取的行為。"""

import json
import uuid

import httpx
import pytest

from xiaozhao import services
from xiaozhao.identity import BackendIdentity, IdentityUnavailable


class FakeIdentityServer:
    """模擬 LTC-BE 的 /v1/users、/v1/bindings、/v1/identities（欄位與 src/app/api/routes_identity.py 相同）。"""

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.codes: dict[str, str] = {}
        self.bindings: dict[str, str] = {}
        self.seq = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        path, body = request.url.path, json.loads(request.content) if request.content else {}
        if path == "/v1/users":
            self.seq += 1
            user = {"user_id": str(uuid.uuid4()), "name": body["name"], "role": body["role"],
                    "pseudonym": f"{'SV' if body['role'] == 'supervisor' else 'CG'}-{self.seq:04d}"}
            self.users[user["user_id"]] = user
            return httpx.Response(201, json=user)
        if path.endswith("/binding-codes"):
            user_id = path.split("/")[3]
            code = f"{len(self.codes):06d}"
            self.codes[code] = user_id
            return httpx.Response(201, json={"code": code, "expires_at": "2026-10-19T10:10:00"})
        if path == "/v1/bindings":
            if body["line_user_id"] in self.bindings:
                return httpx.Response(409, json={"detail": "這個 LINE 帳號已經綁定過"})
            user_id = self.codes.pop(body["code"], None)
            if user_id is None:
                return httpx.Response(400, json={"detail": "綁定碼錯誤、已使用或已過期"})
            self.bindings[body["line_user_id"]] = user_id
            return httpx.Response(200, json=self.users[user_id])
        if path.startswith("/v1/identities/line/"):
            user_id = self.bindings.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=self.users[user_id]) if user_id else httpx.Response(404)
        if path.endswith("/disable"):
            user_id = path.split("/")[3]
            self.bindings = {k: v for k, v in self.bindings.items() if v != user_id}
            return httpx.Response(200, json=self.users[user_id])
        return httpx.Response(404)


@pytest.fixture
def server():
    return FakeIdentityServer()


@pytest.fixture
def identity(server):
    return BackendIdentity("http://be", "tok", transport=httpx.MockTransport(server))


def test_create_account_bind_and_send_backend_identity(db, identity, server):
    account, code = services.create_account(db, "陳照護員", "caregiver", identity)
    assert account["be_user_id"] in server.users and account["pseudonym"] == "CG-0001"
    assert db.all("SELECT * FROM binding_codes") == []  # 綁定碼只在後端

    bound = services.bind(db, code, "Ucare", identity)
    assert (bound["id"], bound["status"], bound["line_user_id"]) == (account["id"], "active", "Ucare")
    fields = services.caller_fields(db, "Ucare")
    assert fields == {"caregiver_name": "CG-0001", "user_id": account["be_user_id"], "role": "caregiver"}


def test_wrong_code_and_already_bound_errors(db, identity):
    _, code = services.create_account(db, "甲", "caregiver", identity)
    with pytest.raises(services.ServiceError):
        services.bind(db, "999999", "Ucare", identity)
    services.bind(db, code, "Ucare", identity)
    _, other = services.create_account(db, "乙", "caregiver", identity)
    with pytest.raises(services.Conflict):
        services.bind(db, other, "Ucare", identity)


def test_cache_miss_asks_backend(db, identity, server):
    """在別台 FE 綁定、或本機資料庫重建後：本機查不到就問後端，並寫入快取。"""
    user = identity.create_user("督導乙", "supervisor")
    server.bindings["Usup"] = user["user_id"]
    account = services.active_account(db, "Usup", identity)
    assert (account["role"], account["be_user_id"]) == ("supervisor", user["user_id"])
    assert services.active_account(db, "Unknown", identity) is None


def test_disable_then_rebind_same_line_account(db, identity):
    first, code = services.create_account(db, "甲", "caregiver", identity)
    services.bind(db, code, "Ucare", identity)
    services.disable_account(db, first["id"], identity)
    assert services.active_account(db, "Ucare", identity) is None

    second, code2 = services.create_account(db, "乙", "caregiver", identity)
    rebound = services.bind(db, code2, "Ucare", identity)
    assert rebound["id"] == second["id"]
    assert db.one("SELECT line_user_id FROM accounts WHERE id = ?", (first["id"],))["line_user_id"] is None


def test_backend_down_is_service_unavailable(db):
    def down(request):
        raise httpx.ConnectError("連不上")

    identity = BackendIdentity("http://be", "tok", transport=httpx.MockTransport(down))
    with pytest.raises(IdentityUnavailable) as exc:
        services.active_account(db, "Ucare", identity)
    assert exc.value.status_code == 503


def test_admin_api_reports_backend_code_expiry(settings, db, identity):
    """網頁顯示的綁定碼效期要跟後端一致（後端 10 分鐘，不是本機的 24 小時）。"""
    from fastapi.testclient import TestClient

    from conftest import FakeEngines
    from xiaozhao.app import State, create_app, get_principal
    from xiaozhao.auth import resolve_principal

    st = State(settings, db, engines=FakeEngines(texts={}))
    st.identity = identity
    app = create_app(st)
    app.dependency_overrides[get_principal] = lambda: resolve_principal(db, settings, "Uadmin", "管理員", None)
    created = TestClient(app).post("/api/admin/accounts", json={"display_name": "新人", "role": "caregiver"}).json()
    assert created["expires_at"] == services._iso_to_epoch("2026-10-19T10:10:00")
    assert len(created["binding_code"]) == 6
