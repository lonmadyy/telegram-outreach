"""CRUD по таблице templates. ARCHITECTURE.md §4.3, §8.4."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Template


async def list_templates(session: AsyncSession, *, kind: str = "dm") -> list[Template]:
    """Шаблоны одного вида: 'dm' — для лички (§8), 'group' — для групп (§22.2)."""
    result = await session.execute(
        select(Template).where(Template.kind == kind).order_by(Template.id)
    )
    return list(result.scalars().all())


async def get_by_id(session: AsyncSession, template_id: int) -> Template | None:
    return await session.get(Template, template_id)


async def all_names(session: AsyncSession) -> set[str]:
    """Имена всех шаблонов (уникальны на всю таблицу, обоих видов)."""
    result = await session.execute(select(Template.name))
    return {r[0] for r in result.all()}


async def get_by_name(session: AsyncSession, name: str) -> Template | None:
    result = await session.execute(select(Template).where(Template.name == name))
    return result.scalar_one_or_none()


async def create_template(
    session: AsyncSession,
    *,
    name: str,
    body: str,
    variables: list[str],
    kind: str = "dm",
) -> Template:
    template = Template(name=name, body=body, variables=variables, kind=kind)
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
