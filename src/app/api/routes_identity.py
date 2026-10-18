"""照護員／督導身分（以 BE PostgreSQL 為準，需求決議 2026-10-19 第 1 項）。

只供 FE 呼叫：管理員在 LIFF 建帳號、發綁定碼，照護員在 LIFF 輸入綁定碼，FE 收到 webhook 時查身分。
FE 的 SQLite accounts 只當快取。見 2026-10-19 fe-be-integration 設計文件第 5.2 節。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_auth import require_xiaozhao_token
from app.core.rate_limit import is_rate_limited
from app.db.binding import AlreadyBound, create_binding_code, redeem_binding_code
from app.db.models import User
from app.db.session import get_session
from app.db.users import ROLES, create_user, disable_user, find_user_by_identity, get_user

router = APIRouter(prefix="/v1", dependencies=[Depends(require_xiaozhao_token)])

_LINE_CHANNEL = "line"
# 綁定碼僅 6 位數，須限制每個 LINE 帳號的核銷嘗試次數，避免被暴力猜中
_BIND_ATTEMPT_LIMIT = 5
_BIND_ATTEMPT_WINDOW_SECONDS = 600


class UserIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    role: str


class BindingCodeIn(BaseModel):
    # FE 管理員不是 biz.users 成員時留空
    created_by: uuid.UUID | None = None


class BindingIn(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")
    line_user_id: str = Field(min_length=1, max_length=128)


def _user_out(user: User) -> dict:
    return {"user_id": str(user.user_id), "name": user.name, "role": user.role, "pseudonym": user.pseudonym}


async def _existing_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await get_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user


@router.post("/users", status_code=201)
async def create_user_route(body: UserIn, session: AsyncSession = Depends(get_session)) -> dict:
    if body.role not in ROLES:
        raise HTTPException(status_code=400, detail=f"role 須為 {'／'.join(ROLES)}")
    return _user_out(await create_user(session, body.name.strip(), body.role))


@router.post("/users/{user_id}/binding-codes", status_code=201)
async def issue_binding_code(
    user_id: uuid.UUID, body: BindingCodeIn, session: AsyncSession = Depends(get_session)
) -> dict:
    await _existing_user(session, user_id)
    code = await create_binding_code(session, created_by=body.created_by, target_user_id=user_id)
    return {"code": code.code, "expires_at": code.expires_at.isoformat()}


@router.post("/bindings")
async def redeem_binding(body: BindingIn, session: AsyncSession = Depends(get_session)) -> dict:
    if await is_rate_limited(
        f"line_bind:{body.line_user_id}", _BIND_ATTEMPT_LIMIT, _BIND_ATTEMPT_WINDOW_SECONDS
    ):
        raise HTTPException(status_code=429, detail="綁定嘗試次數過多，請 10 分鐘後再試")
    try:
        identity = await redeem_binding_code(session, body.code, _LINE_CHANNEL, body.line_user_id)
    except AlreadyBound:
        await session.rollback()
        raise HTTPException(status_code=409, detail="這個 LINE 帳號已經綁定過")
    if identity is None:
        raise HTTPException(status_code=400, detail="綁定碼錯誤、已使用或已過期")
    return _user_out(await _existing_user(session, identity.user_id))


@router.get("/identities/line/{line_user_id}")
async def lookup_line_identity(line_user_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    user = await find_user_by_identity(session, _LINE_CHANNEL, line_user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="identity not found")
    return _user_out(user)


@router.post("/users/{user_id}/disable")
async def disable_user_route(user_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    user = await _existing_user(session, user_id)
    await disable_user(session, user_id)
    return _user_out(user)
