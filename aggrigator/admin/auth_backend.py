"""SQLAdmin auth backend — session-cookie auth gated on User.role='admin'."""

from __future__ import annotations

import uuid

from sqladmin.authentication import AuthenticationBackend
from sqlalchemy import select
from starlette.requests import Request

from aggrigator.config import get_settings
from aggrigator.db import async_session_factory
from aggrigator.models.auth import User, UserRole
from aggrigator.security.passwords import verify_password


class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        email = (form.get("username") or "").strip()
        password = form.get("password") or ""
        if not email or not password:
            return False
        async with async_session_factory() as session:
            user = await session.scalar(
                select(User).where(User.email == email, User.is_active.is_(True))
            )
        if user is None or user.role != UserRole.ADMIN:
            return False
        if not verify_password(password, user.password_hash):
            return False
        request.session["admin_user_id"] = str(user.id)
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        user_id = request.session.get("admin_user_id")
        if not user_id:
            return False
        try:
            uid = uuid.UUID(user_id)
        except ValueError:
            return False
        async with async_session_factory() as session:
            user = await session.get(User, uid)
        return bool(
            user is not None
            and user.is_active
            and user.role == UserRole.ADMIN
        )


def make_admin_auth() -> AdminAuth:
    return AdminAuth(secret_key=get_settings().jwt_secret)
