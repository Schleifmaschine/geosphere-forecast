"""Schlanker async Client für die GeoSphere Dataset API und die Warn-API."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiohttp

from .const import (
    API_BASE,
    OPEN_METEO_DAILY,
    OPEN_METEO_MODEL,
    OPEN_METEO_URL,
    STATION_URL,
    WARNINGS_URL,
)

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=30)


class GeoSphereError(Exception):
    """Fehler beim Abruf von GeoSphere-Daten."""


class GeoSphereParamError(GeoSphereError):
    """Die API kennt einzelne Parameter nicht (mehr)."""

    def __init__(self, detail: str, params: set[str]) -> None:
        super().__init__(f"Ungültige Parameter: {detail}")
        self.params = params


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
        # Parameter, die die API abgelehnt hat (pro Datensatz), werden nicht mehr angefragt
        self._dropped: dict[str, set[str]] = {}

    async def _get_json(self, url: str, params: list[tuple[str, str]]) -> Any:
        try:
            async with self._session.get(url, params=params, timeout=TIMEOUT) as resp:
                if resp.status == 400:
                    detail = str((await resp.json(content_type=None)).get("detail"))
                    # z. B. "Parameters {'foo', 'bar'} do not exist or access is denied"
                    if (match := re.search(r"\{([^}]*)\}", detail)) and "arameter" in detail:
                        names = {n.strip().strip("'\"") for n in match.group(1).split(",")}
                        raise GeoSphereParamError(detail, names)
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
        dropped = self._dropped.setdefault(path, set())
        wanted = [p for p in parameters if p not in dropped]
        while True:
            params = [("parameters", p) for p in wanted]
            params.append(("lat_lon", f"{lat},{lon}"))
            params.extend(extra)
            try:
                data = await self._get_json(f"{API_BASE}/{path}", params)
                break
            except GeoSphereParamError as err:
                bad = err.params & set(wanted)
                if not bad or bad == set(wanted):
                    raise
                # Einzelne Parameter abgeschafft -> ohne sie weitermachen statt komplett auszufallen
                _LOGGER.warning(
                    "GeoSphere %s kennt die Parameter %s nicht mehr – sie werden ab jetzt weggelassen",
                    path, ", ".join(sorted(bad)),
                )
                dropped |= bad
                wanted = [p for p in wanted if p not in bad]

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

    async def stations(self) -> list[dict[str, Any]]:
        """Alle TAWES-Stationen (Metadaten)."""
        data = await self._get_json(f"{STATION_URL}/metadata", [])
        return data.get("stations", [])

    async def station_current(
        self, station_id: str, parameters: list[str]
    ) -> dict[str, Any]:
        """Aktuelle 10-min-Messwerte einer Station."""
        params = [("parameters", p) for p in parameters]
        params.append(("station_ids", station_id))
        data = await self._get_json(STATION_URL, params)
        features = data.get("features") or []
        if not features or not data.get("timestamps"):
            raise GeoSphereError(f"Keine Messwerte für Station {station_id}")
        raw = features[0]["properties"]["parameters"]
        return {
            "time": datetime.fromisoformat(data["timestamps"][-1]),
            "values": {
                name: (p.get("data") or [None])[-1] for name, p in raw.items()
            },
        }

    async def open_meteo(self, lat: float, lon: float, days: int) -> dict[str, Any]:
        """Tagesvorhersage (GeoSphere-seamless) + UV-Index (Standardmodell) von Open-Meteo."""
        base = [
            ("latitude", str(lat)), ("longitude", str(lon)), ("timezone", "auto"),
            ("forecast_days", str(days)), ("wind_speed_unit", "ms"),
        ]
        daily = await self._get_json(
            OPEN_METEO_URL,
            [*base, ("models", OPEN_METEO_MODEL), ("daily", ",".join(OPEN_METEO_DAILY))],
        )
        # UV liefert geosphere_seamless nicht -> Standardmodell, täglich + stündlich
        uv = await self._get_json(
            OPEN_METEO_URL,
            [*base, ("daily", "uv_index_max"), ("hourly", "uv_index"), ("timeformat", "unixtime")],
        )
        if "daily" not in daily or "daily" not in uv:
            raise GeoSphereError(f"Open-Meteo: {daily.get('reason') or uv.get('reason')}")
        return {
            "daily": daily["daily"],
            # Tageszeitstempel = lokale Mitternacht als Unixzeit -> lokales Datum
            "uv_daily": {
                datetime.fromtimestamp(t + uv.get("utc_offset_seconds", 0), UTC).date().isoformat(): v
                for t, v in zip(uv["daily"]["time"], uv["daily"]["uv_index_max"], strict=False)
            },
            "uv_hourly": {
                datetime.fromtimestamp(t, UTC): v
                for t, v in zip(uv["hourly"]["time"], uv["hourly"]["uv_index"], strict=False)
            },
        }

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
