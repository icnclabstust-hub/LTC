import sys
import uuid
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, "src")

from app.db.binding import create_binding_code, find_identity, redeem_binding_code
from app.db.models import User
from app.db.session import SessionLocal

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _unique_user_id() -> str:
    return f"U_test_{uuid.uuid4().hex}"


async def _make_users(session):
    supervisor = User(name="王主管", role="supervisor")
    caregiver = User(name="陳照護員", role="caregiver")
    session.add_all([supervisor, caregiver])
    await session.flush()
    return supervisor, caregiver


async def test_redeem_valid_code_creates_identity():
    async with SessionLocal() as session:
        supervisor, caregiver = await _make_users(session)
        code = await create_binding_code(session, created_by=supervisor.user_id, target_user_id=caregiver.user_id)

        external_user_id = _unique_user_id()
        identity = await redeem_binding_code(session, code.code, channel="line", external_user_id=external_user_id)

        assert identity is not None
        assert identity.user_id == caregiver.user_id

        found = await find_identity(session, channel="line", external_user_id=external_user_id)
        assert found is not None
        assert found.identity_id == identity.identity_id


async def test_redeem_rejects_expired_code():
    async with SessionLocal() as session:
        supervisor, caregiver = await _make_users(session)
        code = await create_binding_code(session, created_by=supervisor.user_id, target_user_id=caregiver.user_id)
        code.expires_at = datetime.utcnow() - timedelta(minutes=1)
        await session.commit()

        identity = await redeem_binding_code(session, code.code, channel="line", external_user_id=_unique_user_id())

        assert identity is None


async def test_redeem_rejects_already_used_code():
    async with SessionLocal() as session:
        supervisor, caregiver = await _make_users(session)
        code = await create_binding_code(session, created_by=supervisor.user_id, target_user_id=caregiver.user_id)

        first = await redeem_binding_code(session, code.code, channel="line", external_user_id=_unique_user_id())
        second = await redeem_binding_code(session, code.code, channel="line", external_user_id=_unique_user_id())

        assert first is not None
        assert second is None


async def test_redeem_rejects_unknown_code():
    async with SessionLocal() as session:
        identity = await redeem_binding_code(session, "000000", channel="line", external_user_id=_unique_user_id())
        assert identity is None
