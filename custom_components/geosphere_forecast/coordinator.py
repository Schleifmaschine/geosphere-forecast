"""DataUpdateCoordinator und Datenaufbereitung für GeoSphere Forecast."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import math
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import GeoSphereClient, GeoSphereError, TimeSeries
from .const import (
    AQI_RESOURCE,
    CHEM_PARAMS,
    CHEM_RESOURCE,
    CONDITION_SEVERITY,
    CONF_AIR_QUALITY,
    CONF_DUST,
    CONF_ENSEMBLE,
    CONF_INCA,
    CONF_NOWCAST,
    CONF_WARNINGS,
    DOMAIN,
    DUST_RESOURCE,
    ENSEMBLE_PARAMS,
    ENSEMBLE_RESOURCE,
    INCA_HOURS,
    INCA_PARAMS,
    INCA_RESOURCE,
    NOWCAST_BBOX,
    NOWCAST_PARAMS,
    NOWCAST_RESOURCE,
    NWP_PARAMS,
    NWP_RESOURCE,
    PRECIP_THRESHOLD,
    SLOW_REFRESH,
    SYMBOL_CONDITION,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

type GeoSphereConfigEntry = ConfigEntry[GeoSphereCoordinator]


def in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    """Liegt der Punkt im Gebiet (lat_min, lon_min, lat_max, lon_max)?"""
    return bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]


def wind_from_uv(u: float | None, v: float | None) -> tuple[float | None, float | None]:
    """u/v-Komponenten -> (Geschwindigkeit m/s, meteorologische Richtung °)."""
    if u is None or v is None:
        return None, None
    speed = math.hypot(u, v)
    bearing = (math.degrees(math.atan2(-u, -v)) + 360) % 360
    return round(speed, 1), round(bearing)


def precip_probability(
    q10: float | None, q50: float | None, q90: float | None
) -> int | None:
    """Wahrscheinlichkeit (%) für Niederschlag >= PRECIP_THRESHOLD.

    Das Ensemble liefert nur die Perzentile 10/50/90 – dazwischen wird linear
    interpoliert. Das ist eine Schätzung, keine echte Member-Auszählung.
    """
    if q10 is None or q50 is None or q90 is None:
        return None
    thr = PRECIP_THRESHOLD
    if q10 >= thr:
        return 90
    if q50 >= thr:
        return round(50 + 40 * (q50 - thr) / max(q50 - q10, 1e-6))
    if q90 >= thr:
        return round(10 + 40 * (q90 - thr) / max(q90 - q50, 1e-6))
    return round(10 * max(q90, 0) / thr)


def symbol_condition(symbol: float | None, is_day: bool = True) -> str | None:
    """GeoSphere Wettersymbol -> HA condition (inkl. clear-night)."""
    if symbol is None:
        return None
    condition = SYMBOL_CONDITION.get(int(symbol))
    if condition == "sunny" and not is_day:
        return "clear-night"
    return condition


@dataclass
class GeoSphereData:
    """Gesamter Datenstand einer Location."""

    nwp: TimeSeries
    nowcast: TimeSeries | None = None
    chem: TimeSeries | None = None
    aqi: TimeSeries | None = None
    dust: TimeSeries | None = None
    ensemble: TimeSeries | None = None
    inca: TimeSeries | None = None
    warnings: list[dict[str, Any]] | None = None
    hourly: list[dict[str, Any]] = field(default_factory=list)
    daily: list[dict[str, Any]] = field(default_factory=list)
    current: dict[str, Any] = field(default_factory=dict)


class GeoSphereCoordinator(DataUpdateCoordinator[GeoSphereData]):
    """Holt NWP, Nowcast, Luftqualität und Warnungen für einen Punkt."""

    config_entry: GeoSphereConfigEntry

    def __init__(self, hass: HomeAssistant, entry: GeoSphereConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = GeoSphereClient(async_get_clientsession(hass))
        self.lat: float = entry.data[CONF_LATITUDE]
        self.lon: float = entry.data[CONF_LONGITUDE]
        opts = entry.options
        self.use_nowcast = opts.get(CONF_NOWCAST, True) and in_bbox(
            self.lat, self.lon, NOWCAST_BBOX
        )
        self.use_air_quality = opts.get(CONF_AIR_QUALITY, True)
        self.use_warnings = opts.get(CONF_WARNINGS, True)
        self.use_dust = opts.get(CONF_DUST, True)
        self.use_ensemble = opts.get(CONF_ENSEMBLE, True)
        self.use_inca = opts.get(CONF_INCA, True)
        self._slow_fetched: datetime | None = None

    async def _async_update_data(self) -> GeoSphereData:
        now = dt_util.utcnow()
        prev = self.data
        slow_due = self._slow_fetched is None or now - self._slow_fetched >= SLOW_REFRESH

        # NWP ist Pflicht – ohne sie keine Entities
        if slow_due or prev is None:
            try:
                nwp = await self.client.forecast(NWP_RESOURCE, NWP_PARAMS, self.lat, self.lon)
            except GeoSphereError as err:
                if prev is None:
                    raise UpdateFailed(str(err)) from err
                _LOGGER.warning("NWP-Abruf fehlgeschlagen, verwende alte Daten: %s", err)
                nwp = prev.nwp
            else:
                self._slow_fetched = now
        else:
            nwp = prev.nwp

        data = GeoSphereData(nwp=nwp)

        if self.use_nowcast:
            data.nowcast = await self._optional(
                self.client.forecast(NOWCAST_RESOURCE, NOWCAST_PARAMS, self.lat, self.lon),
                prev.nowcast if prev else None,
                "Nowcast",
            )

        if self.use_air_quality:
            if slow_due or prev is None or prev.chem is None:
                data.chem = await self._optional(
                    self.client.forecast(CHEM_RESOURCE, CHEM_PARAMS, self.lat, self.lon),
                    prev.chem if prev else None,
                    "Schadstoffprognose",
                )
                data.aqi = await self._optional(
                    self.client.forecast(AQI_RESOURCE, ["aqi"], self.lat, self.lon),
                    prev.aqi if prev else None,
                    "Luftqualitätsindex",
                )
            else:
                data.chem, data.aqi = prev.chem, prev.aqi

        if self.use_dust:
            if slow_due or prev is None or prev.dust is None:
                data.dust = await self._optional(
                    self.client.forecast(DUST_RESOURCE, ["dust"], self.lat, self.lon),
                    prev.dust if prev else None,
                    "Wüstenstaubprognose",
                )
            else:
                data.dust = prev.dust

        if self.use_ensemble:
            if slow_due or prev is None or prev.ensemble is None:
                data.ensemble = await self._optional(
                    self.client.forecast(ENSEMBLE_RESOURCE, ENSEMBLE_PARAMS, self.lat, self.lon),
                    prev.ensemble if prev else None,
                    "Ensemble",
                )
            else:
                data.ensemble = prev.ensemble

        if self.use_inca:
            end = now.replace(minute=0, second=0, microsecond=0)
            data.inca = await self._optional(
                self.client.historical(
                    INCA_RESOURCE, INCA_PARAMS, self.lat, self.lon,
                    end - timedelta(hours=INCA_HOURS + 2), end,
                ),
                prev.inca if prev else None,
                "INCA-Analyse",
            )

        if self.use_warnings:
            data.warnings = await self._optional(
                self.client.warnings(self.lat, self.lon),
                prev.warnings if prev else None,
                "Warnungen",
            )

        data.hourly = build_hourly(nwp, now, data.ensemble)
        data.daily = build_daily(data.hourly)
        data.current = build_current(data, now)
        return data

    async def _optional(self, coro: Any, fallback: Any, label: str) -> Any:
        try:
            return await coro
        except GeoSphereError as err:
            _LOGGER.debug("%s nicht verfügbar: %s", label, err)
            return fallback


def build_hourly(
    nwp: TimeSeries, now: datetime, ens: TimeSeries | None = None
) -> list[dict[str, Any]]:
    """Stündliche Vorhersage.

    Akkumulierte Größen (tp, rain, sf, sund, 10fg) beziehen sich auf das
    Intervall *vor* dem Zeitstempel, Momentanwerte (2t, tcc, sy, ...) auf den
    Zeitstempel selbst. Ein HA-Forecast-Eintrag beschreibt die Stunde *ab*
    seinem Zeitstempel – daher wird dafür der Wert von i+1 genommen.
    """
    ens_idx = {ts: k for k, ts in enumerate(ens.timestamps)} if ens else {}

    def e(param: str, ts: datetime) -> float | None:
        k = ens_idx.get(ts)
        return ens.get(param, k) if ens is not None and k is not None else None

    result: list[dict[str, Any]] = []
    for i in range(len(nwp.timestamps) - 1):
        start = nwp.timestamps[i]
        if nwp.timestamps[i + 1] <= now:
            continue
        g = nwp.get
        speed, bearing = wind_from_uv(g("10u", i), g("10v", i))
        radiation = g("ssrd", i + 1)
        is_day = radiation is not None and radiation > 0
        msl = g("msl", i)
        nxt = nwp.timestamps[i + 1]
        result.append(
            {
                "datetime": start.isoformat(),
                "condition": symbol_condition(g("sy", i), is_day),
                "is_daytime": is_day,
                "native_temperature": g("2t", i),
                "humidity": g("2r", i),
                "cloud_coverage": g("tcc", i),
                "native_precipitation": g("tp", i + 1),
                "native_pressure": round(msl / 100, 1) if msl is not None else None,
                "native_wind_speed": speed,
                "native_wind_gust_speed": g("10fg", i + 1),
                "wind_bearing": bearing,
                # Zusatzwerte (nicht Teil des HA-Standards, aber im Attribut nutzbar)
                "rain": g("rain", i + 1),
                "snow": g("sf", i + 1),
                "sunshine_minutes": _div(g("sund", i + 1), 60),
                "global_radiation": radiation,
                "snow_limit": g("snowlmt", i),
                "cape": g("cape", i),
                "symbol": g("sy", i),
                # Ensemble-Bandbreite (10./90. Perzentil)
                "precipitation_probability": precip_probability(
                    e("tp_p10", nxt), e("tp_p50", nxt), e("tp_p90", nxt)
                ),
                "temp_p10": e("2t_p10", start),
                "temp_p90": e("2t_p90", start),
                "precip_p10": e("tp_p10", nxt),
                "precip_p90": e("tp_p90", nxt),
                "gust_p90": e("10fg_p90", nxt),
            }
        )
    return result


def build_daily(hourly: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tageswerte aus den Stundenwerten (lokale Kalendertage) aggregieren."""
    days: dict[Any, list[dict[str, Any]]] = {}
    for h in hourly:
        local = dt_util.as_local(datetime.fromisoformat(h["datetime"]))
        days.setdefault(local.date(), []).append(h)

    result = []
    for n, (day, hours) in enumerate(days.items()):
        # Das Modell reicht nur 61 h. Der letzte Tag ist fast immer angeschnitten,
        # wird aber behalten: das HA-Frontend zeigt Vorhersagen erst ab 3 Einträgen.
        if n > 0 and len(hours) < 6:
            continue
        temps = [h["native_temperature"] for h in hours if h["native_temperature"] is not None]
        day_hours = [h for h in hours if h["is_daytime"]] or hours
        conditions = [h["condition"] for h in day_hours if h["condition"]]
        winds = [h for h in hours if h["native_wind_speed"] is not None]
        strongest = max(winds, key=lambda h: h["native_wind_speed"], default=None)
        start = dt_util.start_of_local_day(day)
        result.append(
            {
                "datetime": dt_util.as_utc(start).isoformat(),
                "condition": _worst(conditions),
                "native_temperature": max(temps, default=None),
                "native_templow": min(temps, default=None),
                "native_precipitation": _sum(h["native_precipitation"] for h in hours),
                "humidity": _mean(h["humidity"] for h in hours),
                "cloud_coverage": _mean(h["cloud_coverage"] for h in day_hours),
                "native_wind_speed": strongest["native_wind_speed"] if strongest else None,
                "wind_bearing": strongest["wind_bearing"] if strongest else None,
                "native_wind_gust_speed": max(
                    (h["native_wind_gust_speed"] for h in hours if h["native_wind_gust_speed"] is not None),
                    default=None,
                ),
                "sunshine_hours": _div(_sum(h["sunshine_minutes"] for h in hours), 60),
                "snow": _sum(h["snow"] for h in hours),
                "hours_covered": len(hours),
                "precipitation_probability": max(
                    (h["precipitation_probability"] for h in hours if h["precipitation_probability"] is not None),
                    default=None,
                ),
                "temp_max_p10": _max(h["temp_p10"] for h in hours),
                "temp_max_p90": _max(h["temp_p90"] for h in hours),
                "templow_p10": _min(h["temp_p10"] for h in hours),
                "templow_p90": _min(h["temp_p90"] for h in hours),
                # Summe der Stunden-Perzentile: grobe Näherung der Tages-Bandbreite
                "precip_p10": _sum(h["precip_p10"] for h in hours),
                "precip_p90": _sum(h["precip_p90"] for h in hours),
            }
        )
    return result


def build_current(data: GeoSphereData, now: datetime) -> dict[str, Any]:
    """Aktuelle Werte – Nowcast (1 km, 15 min) hat Vorrang vor NWP."""
    nwp = data.nwp
    i = nwp.index_at(now)
    speed, bearing = wind_from_uv(nwp.get("10u", i), nwp.get("10v", i))
    msl = nwp.get("msl", i)
    radiation = nwp.get("ssrd", i)
    cur: dict[str, Any] = {
        "temperature": nwp.get("2t", i),
        "humidity": nwp.get("2r", i),
        "wind_speed": speed,
        "wind_bearing": bearing,
        "wind_gust": nwp.get("10fg", min(i + 1, len(nwp.timestamps) - 1)),
        "pressure": round(msl / 100, 1) if msl is not None else None,
        "cloud_coverage": nwp.get("tcc", i),
        "global_radiation": radiation,
        "snow_limit": nwp.get("snowlmt", i),
        "cape": nwp.get("cape", i),
        "dew_point": None,
        "precipitation_next_hour": None,
    }
    sy = data.hourly[0]["symbol"] if data.hourly else nwp.get("sy", i)
    cur["symbol"] = sy
    cur["condition"] = symbol_condition(sy, radiation is not None and radiation > 0)

    if (nc := data.nowcast) is not None and nc.timestamps:
        j = nc.index_at(now)
        cur["temperature"] = nc.get("t2m", j) if nc.get("t2m", j) is not None else cur["temperature"]
        cur["humidity"] = nc.get("rh2m", j) if nc.get("rh2m", j) is not None else cur["humidity"]
        cur["dew_point"] = nc.get("td", j)
        if nc.get("ff", j) is not None:
            cur["wind_speed"] = round(nc.get("ff", j), 1)
            cur["wind_bearing"] = nc.get("dd", j)
            cur["wind_gust"] = nc.get("fx", j)
        # rr ist die Menge je 15-min-Intervall -> nächste Stunde = 4 Schritte
        cur["precipitation_next_hour"] = _sum(nc.get("rr", k) for k in range(j + 1, j + 5))
    return cur


def _worst(conditions: list[str]) -> str | None:
    if not conditions:
        return None
    return max(conditions, key=lambda c: CONDITION_SEVERITY.index(c) if c in CONDITION_SEVERITY else 0)


def _max(values: Any) -> float | None:
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def _min(values: Any) -> float | None:
    vals = [v for v in values if v is not None]
    return min(vals) if vals else None


def _sum(values: Any) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals), 1) if vals else None


def _mean(values: Any) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals)) if vals else None


def _div(value: float | None, by: float) -> float | None:
    return round(value / by, 1) if value is not None else None
