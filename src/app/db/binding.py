import secrets
import string
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BindingCode, UserIdentity

CODE_TTL_MINUTES = 10


class AlreadyBound(Exception):
    """這個通道帳號已綁定其他使用者；須先解除綁定才能改綁。"""


def generate_code() -> str:
    # 綁定碼等同一次性憑證，須用密碼學安全亂數（random 模組可被預測）
    return "".join(secrets.choice(string.digits) for _ in range(6))


async def _is_code_active(session: AsyncSession, code_str: str) -> bool:
    result = await session.execute(
        select(BindingCode.code_id).where(
            BindingCode.code == code_str,
            BindingCode.used_at.is_(None),
            BindingCode.expires_at > datetime.utcnow(),
        )
    )
    return result.first() is not None


async def create_binding_code(
    session: AsyncSession, created_by: uuid.UUID | None, target_user_id: uuid.UUID
) -> BindingCode:
    # 重發時舊的未使用綁定碼一併作廢，避免同一人同時有多組有效碼流落在外
    await session.execute(
        delete(BindingCode).where(BindingCode.target_user_id == target_user_id, BindingCode.used_at.is_(None))
    )
    # 不可與其他有效綁定碼重複，否則核銷時無法判斷要綁給哪位照護員
    code_str = generate_code()
    while await _is_code_active(session, code_str):
        code_str = generate_code()

    code = BindingCode(
        code=code_str,
        created_by=created_by,
        target_user_id=target_user_id,
        expires_at=datetime.utcnow() + timedelta(minutes=CODE_TTL_MINUTES),
    )
    session.add(code)
    await session.commit()
    return code


async def redeem_binding_code(
    session: AsyncSession, code_str: str, channel: str, external_user_id: str
) -> UserIdentity | None:
    """核銷綁定碼；成功回傳 UserIdentity，失敗（過期/不存在/已使用）回傳 None；
    該通道帳號仍綁著其他使用者時丟出 AlreadyBound（綁定碼不會被消耗）。"""
    result = await session.execute(
        select(BindingCode).where(
            BindingCode.code == code_str,
            BindingCode.used_at.is_(None),
            BindingCode.expires_at > datetime.utcnow(),
        )
    )
    code = result.scalar_one_or_none()
    if code is None:
        return None

    # (channel, external_user_id) 有唯一限制：已解除綁定的舊紀錄要沿用並改綁，否則停用後重新綁定會違反限制
    existing = (
        await session.execute(
            select(UserIdentity).where(
                UserIdentity.channel == channel, UserIdentity.external_user_id == external_user_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.unbound_at is None:
        raise AlreadyBound(external_user_id)
    if existing is not None:
        existing.user_id = code.target_user_id
        existing.bound_at = datetime.utcnow()
        existing.unbound_at = None
        identity = existing
    else:
        identity = UserIdentity(user_id=code.target_user_id, channel=channel, external_user_id=external_user_id)
        session.add(identity)
    await session.flush()

    code.used_at = datetime.utcnow()
    code.used_by_identity_id = identity.identity_id
    await session.commit()
    return identity


async def find_identity(session: AsyncSession, channel: str, external_user_id: str) -> UserIdentity | None:
    result = await session.execute(
        select(UserIdentity).where(
            UserIdentity.channel == channel,
            UserIdentity.external_user_id == external_user_id,
            UserIdentity.unbound_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def unbind_identity(session: AsyncSession, identity: UserIdentity) -> None:
    identity.unbound_at = datetime.utcnow()
    await session.commit()
