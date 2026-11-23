"""Filtro, orden y paginacion estable de las marcaciones.

El equipo no soporta consultas por rango ni por usuario en el protocolo, asi que
el historico se descarga completo y se filtra en memoria.

Para que la paginacion sea estable, la primera peticion de una consulta lee el
dispositivo una sola vez y guarda el conjunto filtrado y ordenado en un snapshot
temporal. Las paginas siguientes reutilizan ese snapshot: no vuelven a leer el
dispositivo y mantienen exactamente el mismo `total`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as DateType
from typing import Any, Sequence

from app.services.snapshots import AttendanceSnapshot, SnapshotStore, get_snapshot_store

logger = logging.getLogger("app.attendance")


@dataclass(frozen=True)
class AttendanceQuery:
    """Filtros validados de la consulta de asistencia."""

    date: DateType | None = None
    from_date: DateType | None = None
    to_date: DateType | None = None
    ci: str | None = None
    limit: int = 500
    offset: int = 0

    @property
    def start(self) -> DateType | None:
        return self.date or self.from_date

    @property
    def end(self) -> DateType | None:
        return self.date or self.to_date


@dataclass(frozen=True)
class AttendancePage:
    """Pagina de resultados con los totales del conjunto filtrado."""

    total: int
    offset: int
    limit: int
    records: list[dict[str, Any]]
    snapshot_id: str | None = None
    filters: dict[str, Any] | None = None

    @property
    def count(self) -> int:
        return len(self.records)


def sort_key(record: dict[str, Any]) -> tuple[str, str, int, str]:
    """Orden ascendente estable: timestamp, user_id, uid y record_id."""
    uid = record.get("uid")
    return (
        str(record.get("timestamp") or ""),
        str(record.get("user_id") or ""),
        int(uid) if isinstance(uid, int) else 0,
        str(record.get("record_id") or ""),
    )


def matches(record: dict[str, Any], query: AttendanceQuery) -> bool:
    """Aplica los filtros de fecha y CI a una marcacion normalizada.

    El CI se compara como texto contra `user_id`; nunca se convierte a entero.
    """
    record_date = str(record.get("date") or "")
    if query.date is not None:
        if record_date != query.date.isoformat():
            return False
    else:
        if query.from_date is not None and record_date < query.from_date.isoformat():
            return False
        if query.to_date is not None and record_date > query.to_date.isoformat():
            return False
    if query.ci is not None:
        if str(record.get("user_id") or "").strip() != query.ci:
            return False
    return True


def describe_filters(query: AttendanceQuery) -> dict[str, Any]:
    """Filtros de la consulta en forma serializable, para el snapshot."""
    return {
        "date": query.date.isoformat() if query.date else None,
        "from": query.from_date.isoformat() if query.from_date else None,
        "to": query.to_date.isoformat() if query.to_date else None,
        "ci": query.ci,
    }


class AttendanceService:
    """Filtra, ordena y pagina las marcaciones con snapshots estables."""

    def __init__(self, reader: Any, store: SnapshotStore | None = None) -> None:
        self.reader = reader
        self.store = store if store is not None else get_snapshot_store()

    def query(self, query: AttendanceQuery) -> AttendancePage:
        """Primera pagina: lee el dispositivo una vez y crea el snapshot.

        Las paginas siguientes deben reutilizar el `snapshot_id` devuelto.
        """
        downloaded = self.reader.attendance()
        filtered = [record for record in downloaded if matches(record, query)]
        filtered.sort(key=sort_key)
        filters = describe_filters(query)
        snapshot = self.store.create(filtered, filters=filters)
        page = snapshot.page(query.offset, query.limit)
        logger.info(
            "attendance_query device_id=%s date=%s from=%s to=%s ci=%s downloaded=%s filtered=%s returned=%s offset=%s limit=%s snapshot_id=%s",
            getattr(self.reader.settings, "biometric_device_id", None),
            query.date,
            query.from_date,
            query.to_date,
            query.ci,
            len(downloaded),
            snapshot.total,
            len(page),
            query.offset,
            query.limit,
            snapshot.snapshot_id,
        )
        return AttendancePage(
            total=snapshot.total,
            offset=query.offset,
            limit=query.limit,
            records=page,
            snapshot_id=snapshot.snapshot_id,
            filters=snapshot.filters,
        )

    def page(self, snapshot: AttendanceSnapshot, offset: int, limit: int) -> AttendancePage:
        """Pagina siguiente: reutiliza el snapshot sin leer el dispositivo."""
        page = snapshot.page(offset, limit)
        logger.info(
            "attendance_snapshot_page snapshot_id=%s total=%s returned=%s offset=%s limit=%s",
            snapshot.snapshot_id,
            snapshot.total,
            len(page),
            offset,
            limit,
        )
        return AttendancePage(
            total=snapshot.total,
            offset=offset,
            limit=limit,
            records=page,
            snapshot_id=snapshot.snapshot_id,
            filters=snapshot.filters,
        )

    def users(self) -> Sequence[dict[str, Any]]:
        return self.reader.users()
