"""CRUD по таблице templates. ARCHITECTURE.md §4.3, §8.4."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Template


async def list_templates(session: AsyncSession) -> list[Template]:
    result = await session.execute(select(Template).order_by(Template.id))
    return list(result.scalars().all())


async def get_by_id(session: AsyncSession, template_id: int) -> Template | None:
    return await session.get(Template, template_id)


async def get_by_name(session: AsyncSession, name: str) -> Template | None:
    result = await session.execute(select(Template).where(Template.name == name))
    return result.scalar_one_or_none()


async def create_template(
    session: AsyncSession,
    *,
    name: str,
    body: str,
    variables: list[str],
) -> Template:
    template = Template(name=name, body=body, variables=variables)
    session.add(template)
    await session.flush()
    return template


async def update_template(
    session: AsyncSession,
    *,
    template_id: int,
    body: str,
    variables: list[str],
) -> Template | None:
    template = await session.get(Template, template_id)
    if template is None:
        return None
    template.body = body
    template.variables = variables
    template.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return template


async def delete_template(session: AsyncSession, template_id: int) -> bool:
    template = await session.get(Template, template_id)
    if template is None:
        return False
    await session.delete(template)
    return True
