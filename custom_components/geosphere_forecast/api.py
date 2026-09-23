"""Schlanker async Client für die GeoSphere Dataset API und die Warn-API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiohttp

from .const import API_BASE, WARNINGS_URL

TIMEOUT = aiohttp.ClientTimeout(total=30)


class GeoSphereError(Exception):
    """Fehler beim Abruf von GeoSphere-Daten."""


@dataclass
class TimeSeries:
    """Zeitreihe eines Punktes: timestamps + Werte pro Parameter."""

    reference_time: datetime | None
    timestamps: list[datetime]
    values: dict[str, list[float | None]] = field(default_factory=dict)

    def get(self, param: str, index: int) -> float | None:
        """Wert eines Parameters an Position index (oder None)."""
        series = self.values.get(param)
        if series is None or not 0 <= index < len(series):
            return None
        return series[index]

    def index_at(self, moment: datetime) -> int:
        """Index des letzten Zeitschritts <= moment (mind. 0)."""
        idx = 0
        for i, ts in enumerate(self.timestamps):
            if ts <= moment:
                idx = i
            else:
                break
        return idx


class GeoSphereClient:
    """Zugriff auf Timeseries-Forecast-Datensätze und Warnungen."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _get_json(self, url: str, params: list[tuple[str, str]]) -> Any:
        try:
            async with self._session.get(url, params=params, timeout=TIMEOUT) as resp:
                if resp.status == 400:
                    detail = (await resp.json(content_type=None)).get("detail")
                    raise GeoSphereError(f"Ungültige Anfrage: {detail}")
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise GeoSphereError(f"Fehler beim Abruf von {url}: {err}") from err

    async def forecast(
        self, resource: str, parameters: list[str], lat: float, lon: float
    ) -> TimeSeries:
        """Punkt-Zeitreihe eines Forecast-Datensatzes abrufen."""
        return await self._timeseries(f"forecast/{resource}", parameters, lat, lon, [])

    async def historical(
        self,
        resource: str,
        parameters: list[str],
        lat: float,
        lon: float,
        start: datetime,
        end: datetime,
    ) -> TimeSeries:
        """Punkt-Zeitreihe eines historischen/Analyse-Datensatzes abrufen."""
        fmt = "%Y-%m-%dT%H:%M"
        extra = [("start", start.strftime(fmt)), ("end", end.strftime(fmt))]
        return await self._timeseries(f"historical/{resource}", parameters, lat, lon, extra)

    async def _timeseries(
        self,
        path: str,
        parameters: list[str],
        lat: float,
        lon: float,
        extra: list[tuple[str, str]],
    ) -> TimeSeries:
        params = [("parameters", p) for p in parameters]
        params.append(("lat_lon", f"{lat},{lon}"))
        params.extend(extra)
        data = await self._get_json(f"{API_BASE}/{path}", params)

        features = data.get("features") or []
        if not features:
            raise GeoSphereError(f"Keine Daten für {path}")
        raw = features[0]["properties"]["parameters"]
        ref = data.get("reference_time")
        return TimeSeries(
            reference_time=datetime.fromisoformat(ref) if ref else None,
            timestamps=[datetime.fromisoformat(t) for t in data["timestamps"]],
            values={name: p.get("data", []) for name, p in raw.items()},
        )

    async def warnings(self, lat: float, lon: float) -> list[dict[str, Any]]:
        """Aktive Wetterwarnungen für einen Punkt."""
        data = await self._get_json(
            WARNINGS_URL, [("lon", str(lon)), ("lat", str(lat)), ("lang", "de")]
        )
        result = []
        for w in data.get("properties", {}).get("warnings", []):
            raw = w.get("rawinfo", {})
            result.append(
                {
                    "id": w.get("warnid"),
                    "type_id": w.get("warntypid"),
                    "level": int(w.get("warnstufeid") or 0),
                    "start": _ts(raw.get("start")),
                    "end": _ts(raw.get("end")),
                    "text": w.get("text", ""),
                    "effects": w.get("auswirkungen", ""),
                    "recommendations": w.get("empfehlungen", ""),
                }
            )
        return result


def _ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(int(value), UTC)
