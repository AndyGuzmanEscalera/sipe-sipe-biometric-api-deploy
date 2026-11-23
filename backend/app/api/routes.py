"""Rutas HTTP de solo lectura.

Solo se expone GET. No existe ninguna ruta POST/PUT/PATCH/DELETE en esta API.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.schemas import (
    AttendanceFilters,
    AttendanceQueryParams,
    AttendanceResponse,
    BiometricUser,
    DeviceResponse,
    HealthResponse,
    UsersResponse,
    first_error_message,
    to_query,
)
from app.security.auth import require_api_token
from app.services.attendance_service import AttendanceService
from app.services.biometric_reader import BiometricReader, get_reader
from app.services.snapshots import (
    SNAPSHOT_FILTER_MISMATCH_MESSAGE,
    AttendanceSnapshot,
    SnapshotExpired,
    SnapshotStore,
    get_snapshot_store,
)

logger = logging.getLogger("app.api")

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
    summary="Estado del servicio (no requiere token ni dispositivo)",
)
def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        version=settings.service_version,
    )


@router.get(
    "/device",
    response_model=DeviceResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_api_token)],
    tags=["device"],
    summary="Informacion disponible del dispositivo biometrico",
)
def device(
    reader: Annotated[BiometricReader, Depends(get_reader)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DeviceResponse:
    info: dict[str, Any] = reader.device_info()
    return DeviceResponse(
        device_id=settings.biometric_device_id,
        connected=True,
        **info,
    )


@router.get(
    "/users",
    response_model=UsersResponse,
    dependencies=[Depends(require_api_token)],
    tags=["device"],
    summary="Usuarios registrados en el dispositivo",
)
def users(
    reader: Annotated[BiometricReader, Depends(get_reader)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UsersResponse:
    items = reader.users()
    return UsersResponse(
        device_id=settings.biometric_device_id,
        total=len(items),
        users=[BiometricUser(**item) for item in items],
    )


@router.get(
    "/attendance",
    response_model=AttendanceResponse,
    dependencies=[Depends(require_api_token)],
    tags=["attendance"],
    summary="Marcaciones del dispositivo con filtros opcionales",
)
def attendance(
    reader: Annotated[BiometricReader, Depends(get_reader)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[SnapshotStore, Depends(get_snapshot_store)],
    date_: Annotated[date | None, Query(alias="date")] = None,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
    ci: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    snapshot_id: Annotated[str | None, Query(max_length=64)] = None,
) -> AttendanceResponse:
    """Pagina de marcaciones.

    Sin `snapshot_id` se lee el dispositivo y se crea un snapshot con el conjunto
    filtrado. Con `snapshot_id` se reutiliza ese conjunto: no se vuelve a leer el
    dispositivo y el `total` permanece estable durante todas las paginas.
    """
    try:
        params = AttendanceQueryParams(date=date_, from_=from_, to=to, ci=ci)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=first_error_message(exc),
        ) from exc

    if limit is not None and limit > settings.attendance_max_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'limit' supera el maximo permitido ({settings.attendance_max_limit}).",
        )

    effective_limit = settings.effective_limit(limit)
    service = AttendanceService(reader, store)

    if snapshot_id:
        try:
            snapshot = store.get(snapshot_id)
        except SnapshotExpired as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from None
        _check_snapshot_filters(snapshot, params)
        page = service.page(snapshot, offset, effective_limit)
    else:
        query = to_query(params, effective_limit, offset)
        page = service.query(query)

    return AttendanceResponse(
        device_id=settings.biometric_device_id,
        filters=_filters_from(page.filters),
        total=page.total,
        offset=page.offset,
        limit=page.limit,
        count=page.count,
        records=page.records,
        snapshot_id=page.snapshot_id,
    )


def _check_snapshot_filters(snapshot: AttendanceSnapshot, params: AttendanceQueryParams) -> None:
    """Los filtros enviados deben coincidir con los del snapshot.

    Si el consumidor no envia filtros, se usan los del snapshot. Si envia otros
    distintos, es un error controlado: el conjunto ya no corresponde.
    """
    if not any((params.date, params.from_, params.to, params.ci)):
        return
    requested = {
        "date": params.date.isoformat() if params.date else None,
        "from": params.from_.isoformat() if params.from_ else None,
        "to": params.to.isoformat() if params.to else None,
        "ci": params.ci,
    }
    if requested != snapshot.filters:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=SNAPSHOT_FILTER_MISMATCH_MESSAGE,
        )


def _filters_from(filters: dict[str, Any] | None) -> AttendanceFilters:
    """Reconstruye los filtros reportados a partir del snapshot."""
    data = filters or {}
    return AttendanceFilters(
        date=date.fromisoformat(data["date"]) if data.get("date") else None,
        from_=date.fromisoformat(data["from"]) if data.get("from") else None,
        to=date.fromisoformat(data["to"]) if data.get("to") else None,
        ci=data.get("ci"),
    )
