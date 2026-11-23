"""Snapshots temporales de la consulta de asistencia.

La paginacion estable exige que todas las paginas de una misma consulta
provengan del mismo conjunto de datos. El dispositivo se lee una sola vez y el
resultado filtrado y ordenado se conserva en memoria durante un TTL corto.

No hay base de datos: el snapshot es temporal, se pierde al reiniciar el
proceso y eso es aceptable porque el consumidor puede repetir la consulta.

El consumo de memoria esta acotado por dos limites configurables: el TTL y el
numero maximo de snapshots simultaneos (eviccion LRU).
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Iterable, Mapping

from app.config import get_settings

DEFAULT_TTL_SECONDS = 300.0
DEFAULT_MAX_ENTRIES = 8

#: Mensajes estables para el consumidor. El codigo va al inicio del texto.
SNAPSHOT_EXPIRED_MESSAGE = (
    "snapshot_expired: el conjunto de la consulta ya no esta disponible. "
    "Repita la consulta desde la primera pagina."
)
SNAPSHOT_FILTER_MISMATCH_MESSAGE = (
    "snapshot_filter_mismatch: los filtros enviados no coinciden con los del snapshot. "
    "Repita la consulta desde la primera pagina."
)


class SnapshotExpired(Exception):
    """El snapshot solicitado no existe, expiro o fue descartado."""


@dataclass(frozen=True)
class AttendanceSnapshot:
    """Conjunto inmutable de marcaciones de una consulta."""

    snapshot_id: str
    records: tuple[dict[str, Any], ...]
    filters: dict[str, Any]
    created_at: float
    expires_at: float

    @property
    def total(self) -> int:
        return len(self.records)

    def page(self, offset: int, limit: int) -> list[dict[str, Any]]:
        """Pagina del conjunto, sin releer el dispositivo."""
        return list(self.records[offset : offset + limit])


class SnapshotStore:
    """Almacen en memoria de snapshots con TTL y limite de entradas."""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("El TTL del snapshot debe ser mayor que cero.")
        if max_entries < 1:
            raise ValueError("El limite de snapshots debe ser al menos 1.")
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._clock = clock
        self._items: "OrderedDict[str, AttendanceSnapshot]" = OrderedDict()
        self._lock = threading.Lock()

    def create(
        self,
        records: Iterable[dict[str, Any]],
        filters: Mapping[str, Any] | None = None,
    ) -> AttendanceSnapshot:
        """Guarda el conjunto filtrado y devuelve el snapshot creado."""
        now = self._clock()
        snapshot = AttendanceSnapshot(
            snapshot_id=secrets.token_urlsafe(18),
            records=tuple(records),
            filters=dict(filters or {}),
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._purge_expired(now)
            self._items[snapshot.snapshot_id] = snapshot
            self._items.move_to_end(snapshot.snapshot_id)
            # Eviccion LRU: el conjunto mas antiguo en uso se descarta primero.
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)
        return snapshot

    def get(self, snapshot_id: str) -> AttendanceSnapshot:
        """Recupera un snapshot vigente. Si no existe o expiro, falla."""
        now = self._clock()
        with self._lock:
            snapshot = self._items.get(snapshot_id)
            if snapshot is None:
                raise SnapshotExpired(SNAPSHOT_EXPIRED_MESSAGE)
            if snapshot.expires_at <= now:
                self._items.pop(snapshot_id, None)
                raise SnapshotExpired(SNAPSHOT_EXPIRED_MESSAGE)
            self._items.move_to_end(snapshot_id)
            return snapshot

    def _purge_expired(self, now: float) -> None:
        expired = [
            key for key, item in self._items.items() if item.expires_at <= now
        ]
        for key in expired:
            self._items.pop(key, None)

    def clear(self) -> None:
        """Descarta todos los snapshots. Se usa al reiniciar y en las pruebas."""
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


@lru_cache(maxsize=1)
def get_snapshot_store() -> SnapshotStore:
    """Store compartido por el proceso, configurado desde el entorno."""
    settings = get_settings()
    return SnapshotStore(
        ttl_seconds=settings.attendance_snapshot_ttl,
        max_entries=settings.attendance_snapshot_max,
    )
