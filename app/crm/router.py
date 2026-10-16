"""The /api routes behind the dashboard. Everything needs the admin session
cookie except POST /api/leads/ingest, which takes the shared X-Ingest-Key."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from . import service
from .models import AdminUser
from .schemas import (
    SOURCES,
    STATUSES,
    ActivityCreate,
    ActivityOut,
    IngestIn,
    IngestOut,
    LeadCreate,
    LeadDetail,
    LeadOut,
    LeadPage,
    LeadUpdate,
    LoginIn,
    Me,
    Stats,
)
from .security import (
    check_lockout,
    current_user,
    end_session,
    find_admin,
    get_db,
    login_failures,
    login_keys,
    record_failure,
    require_ingest_key,
    require_trusted_origin,
    start_session,
    verify_password,
)

router = APIRouter(prefix="/api")
authed = APIRouter(dependencies=[Depends(current_user), Depends(require_trusted_origin)])


# ------------------------------------------------------------------- auth --


@router.post("/auth/login", response_model=Me, dependencies=[Depends(require_trusted_origin)])
async def login(body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> Me:
    keys = login_keys(request, body.email)
    check_lockout(keys)
    user = await find_admin(db, body.email)
    if not verify_password(user.password_hash if user else None, body.password) or user is None:
        record_failure(keys)
        raise HTTPException(401, "Wrong email or password")
    for key in keys:
        login_failures.clear(key)
    await start_session(db, response, user)
    await db.commit()
    return Me(email=user.email)


@router.post("/auth/logout", status_code=204, dependencies=[Depends(require_trusted_origin)])
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> None:
    await end_session(db, request, response)
    await db.commit()


@router.get("/auth/me", response_model=Me)
async def me(user: AdminUser = Depends(current_user)) -> Me:
    return Me(email=user.email)


# ----------------------------------------------------------------- ingest --


@router.post("/leads/ingest", response_model=IngestOut, dependencies=[Depends(require_ingest_key)])
async def ingest(body: IngestIn, response: Response, db: AsyncSession = Depends(get_db)) -> IngestOut:
    lead, created = await service.upsert_from_ingest(db, body)
    await db.commit()
    response.status_code = 201 if created else 200
    return IngestOut(id=lead.id, created=created)


# ------------------------------------------------------------------ leads --


def filters(
    q: str | None = Query(None, max_length=100),
    status: list[Literal[STATUSES]] | None = Query(None),  # type: ignore[valid-type]
    source: Literal[SOURCES] | None = None,  # type: ignore[valid-type]
    is_hot: bool | None = None,
    follow_up: Literal["today", "overdue", "due"] | None = None,
) -> service.Filters:
    return service.Filters(q=q, status=status, source=source, is_hot=is_hot, follow_up=follow_up)


@authed.get("/leads", response_model=LeadPage)
async def list_leads(
    f: service.Filters = Depends(filters),
    sort: Literal[tuple(service.SORTABLE)] = "created_at",  # type: ignore[valid-type]
    order: Literal["asc", "desc"] = "desc",
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> LeadPage:
    items, total = await service.list_leads(db, f, sort, order == "desc", page, page_size)
    return LeadPage(items=[LeadOut.model_validate(i) for i in items], total=total, page=page, page_size=page_size)


@authed.get("/leads/stats", response_model=Stats)
async def stats(db: AsyncSession = Depends(get_db)) -> dict:
    return await service.stats(db)


@authed.get("/leads/export.csv")
async def export(f: service.Filters = Depends(filters), db: AsyncSession = Depends(get_db)) -> Response:
    return Response(
        await service.export_csv(db, f),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="scalryx-leads.csv"', "Cache-Control": "no-store"},
    )


@authed.post("/leads", response_model=LeadDetail, status_code=201)
async def create(body: LeadCreate, db: AsyncSession = Depends(get_db)) -> LeadDetail:
    lead = await service.create_lead(db, body)
    await db.commit()
    return LeadDetail.model_validate(await service.get_with_timeline(db, lead.id))


async def _lead(db: AsyncSession, lead_id: int):
    lead = await service.get_with_timeline(db, lead_id)
    if lead is None:
        raise HTTPException(404, "Lead not found")
    return lead


@authed.get("/leads/{lead_id}", response_model=LeadDetail)
async def read(lead_id: int, db: AsyncSession = Depends(get_db)) -> LeadDetail:
    return LeadDetail.model_validate(await _lead(db, lead_id))


@authed.patch("/leads/{lead_id}", response_model=LeadDetail)
async def update(lead_id: int, body: LeadUpdate, db: AsyncSession = Depends(get_db)) -> LeadDetail:
    lead = await _lead(db, lead_id)
    await service.update_lead(db, lead, body)
    await db.commit()
    return LeadDetail.model_validate(await service.get_with_timeline(db, lead_id))


@authed.delete("/leads/{lead_id}", status_code=204)
async def delete(lead_id: int, db: AsyncSession = Depends(get_db)) -> None:
    await db.delete(await _lead(db, lead_id))
    await db.commit()


@authed.post("/leads/{lead_id}/activities", response_model=ActivityOut, status_code=201)
async def add_activity(lead_id: int, body: ActivityCreate, db: AsyncSession = Depends(get_db)) -> ActivityOut:
    lead = await _lead(db, lead_id)
    activity = await service.add_activity(db, lead, body.type, body.content)
    await db.commit()
    return ActivityOut.model_validate(activity)


router.include_router(authed)
