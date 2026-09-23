"""DataUpdateCoordinator und Datenaufbereitung für GeoSphere Forecast."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
    CONF_CLIMATE,
    CONF_DUST,
    CONF_EXTENDED,
    CONF_EXTENDED_DAYS,
    CONF_ENSEMBLE,
    CONF_INCA,
    CONF_NOWCAST,
    CONF_SNOW,
    CONF_STATION,
    CONF_WARNINGS,
    DAILY_DAYS,
    DAILY_REFRESH,
    DAY_END_HOUR,
    DEFAULT_EXTENDED_DAYS,
    DAY_START_HOUR,
    DOMAIN,
    DUST_RESOURCE,
    ENSEMBLE_PARAMS,
    ENSEMBLE_RESOURCE,
    INCA_HOURS,
    INCA_PARAMS,
    INCA_RESOURCE,
    NOWCAST_BBOX,
    NOWCAST_PARAMS,
    NOWCAST_WET_15MIN,
    NOWCAST_RESOURCE,
    NWP_PARAMS,
    NWP_RESOURCE,
    OBS_FOG_HUMIDITY,
    OBS_FOG_WIND,
    OBS_HEAVY_RATE,
    OBS_RAIN_RATE,
    OBS_SLEET_TEMP,
    OBS_SNOW_TEMP,
    OBS_STATION_MAX_KM,
    PRECIP_CONDITIONS,
    PRECIP_THRESHOLD,
    SNOW_PARAMS,
    SNOW_RESOURCE,
    SPARTACUS_PARAMS,
    SPARTACUS_RESOURCE,
    STATION_AUTO,
    STATION_NONE,
    RETRY_MINUTES,
    SLOW_REFRESH,
    SYMBOL_CONDITION,
    TAWES_PARAMS,
    UPDATE_INTERVAL,
    WINFORE_PARAMS,
    WIND_STRONG,
    WINFORE_RESOURCE,
    WMO_CONDITION,
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


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Großkreisentfernung in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def nearest_stations(
    stations: list[dict[str, Any]], lat: float, lon: float, count: int = 15
) -> list[tuple[dict[str, Any], float]]:
    """Aktive Stationen nach Entfernung sortiert."""
    ranked = [
        (s, distance_km(lat, lon, s["lat"], s["lon"]))
        for s in stations
        if s.get("is_active", True)
    ]
    ranked.sort(key=lambda x: x[1])
    return ranked[:count]


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


def apply_wind(condition: str | None, wind: float | None) -> str | None:
    """Bei starkem Wind 'windy' / 'windy-variant' statt trockener Bedingung."""
    if wind is None or wind < WIND_STRONG:
        return condition
    if condition in ("sunny", "clear-night"):
        return "windy"
    if condition in ("partlycloudy", "cloudy"):
        return "windy-variant"
    return condition


def dew_point(temp: float | None, rh: float | None) -> float | None:
    """Taupunkt nach Magnus."""
    if temp is None or rh is None or rh <= 0:
        return None
    a, b = 17.62, 243.12
    gamma = a * temp / (b + temp) + math.log(rh / 100)
    return round(b * gamma / (a - gamma), 1)


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
    station: dict[str, Any] | None = None
    winfore: TimeSeries | None = None
    snowgrid: TimeSeries | None = None
    spartacus: TimeSeries | None = None
    open_meteo: dict[str, Any] | None = None
    warnings: list[dict[str, Any]] | None = None
    hourly: list[dict[str, Any]] = field(default_factory=list)
    daily: list[dict[str, Any]] = field(default_factory=list)
    twice_daily: list[dict[str, Any]] = field(default_factory=list)
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
        self.use_climate = opts.get(CONF_CLIMATE, True)
        self.use_snow = opts.get(CONF_SNOW, True)
        self.station_option: str = opts.get(CONF_STATION, STATION_AUTO)
        # Metadaten der gewählten Station (id, name, altitude, distance_km)
        self.station_info: dict[str, Any] | None = None
        self.use_extended = opts.get(CONF_EXTENDED, False)
        self.extended_days = int(opts.get(CONF_EXTENDED_DAYS, DEFAULT_EXTENDED_DAYS))
        self._daily_fetched: datetime | None = None
        self._slow_fetched: datetime | None = None
        # Fehler im aktuellen Lauf / aufeinanderfolgende Läufe mit Fehler
        self._failed = False
        self._fail_streak = 0
        self._retry_slow = False

    async def _async_update_data(self) -> GeoSphereData:
        now = dt_util.utcnow()
        prev = self.data
        slow_due = (
            self._slow_fetched is None
            or now - self._slow_fetched >= SLOW_REFRESH
            or self._retry_slow
        )
        self._failed = False

        # NWP ist Pflicht – ohne sie keine Entities
        if slow_due or prev is None:
            try:
                nwp = await self.client.forecast(NWP_RESOURCE, NWP_PARAMS, self.lat, self.lon)
            except GeoSphereError as err:
                if prev is None:
                    raise UpdateFailed(str(err)) from err
                _LOGGER.warning("NWP-Abruf fehlgeschlagen, verwende alte Daten: %s", err)
                self._failed = True
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

        if self.use_extended:
            if slow_due or prev is None or prev.open_meteo is None:
                data.open_meteo = await self._optional(
                    self.client.open_meteo(self.lat, self.lon, self.extended_days),
                    prev.open_meteo if prev else None,
                    "Open-Meteo",
                )
            else:
                data.open_meteo = prev.open_meteo

        await self._update_station(data, prev)
        await self._update_daily(data, prev, now)

        if self.use_warnings:
            data.warnings = await self._optional(
                self.client.warnings(self.lat, self.lon),
                prev.warnings if prev else None,
                "Warnungen",
            )

        data.hourly = build_hourly(nwp, now, data.ensemble, data.open_meteo)
        data.daily = build_daily(data.hourly)
        if data.open_meteo:
            data.daily = merge_extended(data.daily, data.open_meteo, self.extended_days)
        data.twice_daily = build_twice_daily(data.hourly)
        data.current = build_current(data, now, self.station_info)
        self._schedule_retry(slow_due)
        return data

    def _schedule_retry(self, slow_due: bool) -> None:
        """Nach Fehlern schneller erneut versuchen (1, 2, 3, 5, 8, 13 min)."""
        if self._failed:
            delay = RETRY_MINUTES[min(self._fail_streak, len(RETRY_MINUTES) - 1)]
            self._fail_streak += 1
            self.update_interval = min(timedelta(minutes=delay), UPDATE_INTERVAL)
            # fehlgeschlagene stündliche Quellen beim nächsten Lauf erneut holen
            self._retry_slow = slow_due
            _LOGGER.debug("Teilweise Fehler – nächster Versuch in %s min", delay)
        else:
            self._fail_streak = 0
            self._retry_slow = False
            self.update_interval = UPDATE_INTERVAL

    async def _update_station(self, data: GeoSphereData, prev: GeoSphereData | None) -> None:
        """Messwerte der gewählten bzw. nächstgelegenen TAWES-Station."""
        if self.station_option == STATION_NONE:
            return
        if self.station_info is None:
            stations = await self._optional(self.client.stations(), None, "Stationsliste")
            if not stations:
                return
            if self.station_option == STATION_AUTO:
                ranked = nearest_stations(stations, self.lat, self.lon, 1)
            else:
                ranked = [
                    (s, distance_km(self.lat, self.lon, s["lat"], s["lon"]))
                    for s in stations
                    if str(s["id"]) == self.station_option
                ]
            if not ranked:
                return
            station, dist = ranked[0]
            self.station_info = {
                "id": str(station["id"]),
                "name": station["name"].title(),
                "state": station.get("state"),
                "altitude": station.get("altitude"),
                "distance_km": round(dist, 1),
            }
        data.station = await self._optional(
            self.client.station_current(self.station_info["id"], TAWES_PARAMS),
            prev.station if prev else None,
            "Stationsmesswerte",
        )

    async def _update_daily(
        self, data: GeoSphereData, prev: GeoSphereData | None, now: datetime
    ) -> None:
        """Tägliche Rasterdaten (Verdunstung, Trockenheit, Schnee, Klima)."""
        if not (self.use_climate or self.use_snow):
            return
        due = self._daily_fetched is None or now - self._daily_fetched >= DAILY_REFRESH
        if not due and prev is not None:
            data.winfore, data.spartacus, data.snowgrid = (
                prev.winfore, prev.spartacus, prev.snowgrid
            )
            return
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=DAILY_DAYS)
        jobs = []
        if self.use_climate:
            jobs += [("winfore", WINFORE_RESOURCE, WINFORE_PARAMS),
                     ("spartacus", SPARTACUS_RESOURCE, SPARTACUS_PARAMS)]
        if self.use_snow:
            jobs.append(("snowgrid", SNOW_RESOURCE, SNOW_PARAMS))
        failed_before = self._failed
        self._failed = False
        for attr, resource, params in jobs:
            setattr(data, attr, await self._optional(
                self.client.historical(resource, params, self.lat, self.lon, start, end),
                getattr(prev, attr) if prev else None,
                resource,
            ))
        if not self._failed:
            self._daily_fetched = now
        self._failed = self._failed or failed_before

    async def _optional(self, coro: Any, fallback: Any, label: str) -> Any:
        try:
            return await coro
        except GeoSphereError as err:
            _LOGGER.debug("%s nicht verfügbar: %s", label, err)
            self._failed = True
            return fallback


def build_hourly(
    nwp: TimeSeries,
    now: datetime,
    ens: TimeSeries | None = None,
    om: dict[str, Any] | None = None,
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
                "condition": apply_wind(symbol_condition(g("sy", i), is_day), speed),
                "is_daytime": is_day,
                "native_temperature": g("2t", i),
                "humidity": g("2r", i),
                "native_dew_point": dew_point(g("2t", i), g("2r", i)),
                "uv_index": (om or {}).get("uv_hourly", {}).get(start),
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
                "condition": apply_wind(
                    _worst(conditions),
                    strongest["native_wind_speed"] if strongest else None,
                ),
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


def build_twice_daily(hourly: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tag (06–18) / Nacht (18–06) Ortszeit."""
    periods: dict[datetime, list[dict[str, Any]]] = {}
    for h in hourly:
        local = dt_util.as_local(datetime.fromisoformat(h["datetime"]))
        if DAY_START_HOUR <= local.hour < DAY_END_HOUR:
            start = local.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
        else:
            base = local if local.hour >= DAY_END_HOUR else local - timedelta(days=1)
            start = base.replace(hour=DAY_END_HOUR, minute=0, second=0, microsecond=0)
        periods.setdefault(start, []).append(h)

    result = []
    for start, hours in periods.items():
        if len(hours) < 3:
            continue
        is_day = start.hour == DAY_START_HOUR
        temps = [h["native_temperature"] for h in hours if h["native_temperature"] is not None]
        condition = _worst([h["condition"] for h in hours if h["condition"]])
        if not is_day and condition == "sunny":
            condition = "clear-night"
        winds = [h for h in hours if h["native_wind_speed"] is not None]
        strongest = max(winds, key=lambda h: h["native_wind_speed"], default=None)
        condition = apply_wind(condition, strongest["native_wind_speed"] if strongest else None)
        result.append(
            {
                "datetime": dt_util.as_utc(start).isoformat(),
                "is_daytime": is_day,
                "condition": condition,
                # Tag: Höchstwert, Nacht: Tiefstwert (so zeigt es das Frontend an)
                "native_temperature": (max if is_day else min)(temps, default=None),
                "native_templow": min(temps, default=None),
                "native_precipitation": _sum(h["native_precipitation"] for h in hours),
                "precipitation_probability": _max(h["precipitation_probability"] for h in hours),
                "humidity": _mean(h["humidity"] for h in hours),
                "cloud_coverage": _mean(h["cloud_coverage"] for h in hours),
                "native_wind_speed": strongest["native_wind_speed"] if strongest else None,
                "wind_bearing": strongest["wind_bearing"] if strongest else None,
                "native_wind_gust_speed": _max(h["native_wind_gust_speed"] for h in hours),
            }
        )
    return result


def nowcast_rain_window(
    nc: TimeSeries | None, now: datetime
) -> dict[str, datetime | None] | None:
    """Beginn/Ende des nächsten Regens aus dem 15-min-Nowcast.

    rr[i] ist die Menge im Intervall *vor* timestamps[i]. Liefert None ohne
    Nowcast; start/end sind None, wenn im Horizont kein Beginn/Ende liegt.
    """
    if nc is None or not nc.timestamps:
        return None
    step = timedelta(minutes=15)
    wet = [
        (ts - step, ts, (nc.get("rr", i) or 0) >= NOWCAST_WET_15MIN)
        for i, ts in enumerate(nc.timestamps)
        if ts > now
    ]
    start = end = None
    for begin, finish, is_wet in wet:
        if start is None:
            if is_wet:
                start = max(begin, now)
        elif not is_wet:
            end = begin
            break
    return {
        "start": start,
        "end": end,
        "raining_now": bool(wet) and wet[0][2],
        "horizon": wet[-1][1] if wet else None,
    }


def build_current(
    data: GeoSphereData, now: datetime, station_info: dict[str, Any] | None = None
) -> dict[str, Any]:
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
    cur["rain_window"] = nowcast_rain_window(data.nowcast, now)
    cur["uv_index"] = _uv_now(data.open_meteo, now)

    observed = observed_condition(data, now, cur, station_info)
    cur["condition_source"] = "observation" if observed else "model"
    if observed:
        cur["condition"] = observed
    cur["condition"] = apply_wind(cur["condition"], cur["wind_speed"])
    return cur


def _uv_now(om: dict[str, Any] | None, now: datetime) -> float | None:
    if not om or not om.get("uv_hourly"):
        return None
    hour = now.replace(minute=0, second=0, microsecond=0)
    return om["uv_hourly"].get(hour)


def observed_condition(
    data: GeoSphereData,
    now: datetime,
    cur: dict[str, Any],
    station_info: dict[str, Any] | None,
) -> str | None:
    """Korrigiert den Modellzustand mit dem, was *jetzt* beobachtet wird.

    Quellen: Nowcast (Radar, 1 km – immer am Standort) und eine TAWES-Station,
    sofern sie höchstens OBS_STATION_MAX_KM entfernt ist. Gibt None zurück,
    wenn die Beobachtung nichts am Modellzustand ändert.
    """
    model = cur["condition"]
    if model == "lightning-rainy":
        return None

    rates: list[float] = []
    humidity = wind = None
    temp = cur["temperature"]

    if (nc := data.nowcast) is not None and nc.timestamps:
        j = nc.index_at(now)
        steps = [nc.get("rr", k) for k in (j, j + 1) if nc.get("rr", k) is not None]
        if steps:
            rates.append(max(steps) * 4)  # mm/15 min -> mm/h
        humidity, wind = nc.get("rh2m", j), nc.get("ff", j)

    near = (
        station_info is not None
        and station_info["distance_km"] <= OBS_STATION_MAX_KM
        and data.station is not None
    )
    if near:
        st = data.station["values"]
        if st.get("RR") is not None:
            rates.append(st["RR"] * 6)  # mm/10 min -> mm/h
        # fehlende Stationswerte (None) nicht über Nowcast-Werte schreiben
        humidity = st["RF"] if st.get("RF") is not None else humidity
        wind = st["FFAM"] if st.get("FFAM") is not None else wind
        temp = st["TL"] if st.get("TL") is not None else temp

    if not rates:
        return None
    rate = max(rates)

    if rate >= OBS_RAIN_RATE:
        if temp is not None and temp <= OBS_SNOW_TEMP:
            return "snowy" if model != "snowy" else None
        if temp is not None and temp <= OBS_SLEET_TEMP:
            return "snowy-rainy" if model != "snowy-rainy" else None
        observed = "pouring" if rate >= OBS_HEAVY_RATE else "rainy"
        return observed if model != observed else None

    # Trocken, Modell sagt aber Niederschlag -> bewölkt
    if model in PRECIP_CONDITIONS:
        return "cloudy"
    if (
        humidity is not None and wind is not None
        and humidity >= OBS_FOG_HUMIDITY and wind < OBS_FOG_WIND
        and model != "fog"
    ):
        return "fog"
    return None


def merge_extended(
    daily: list[dict[str, Any]], om: dict[str, Any], days: int
) -> list[dict[str, Any]]:
    """GeoSphere-Tage behalten, angeschnittene/fehlende Tage mit Open-Meteo auffüllen."""
    extended = open_meteo_days(om)
    by_date = {e["date"]: e for e in extended}

    result: list[dict[str, Any]] = []
    for n, day in enumerate(daily):
        local_date = dt_util.as_local(datetime.fromisoformat(day["datetime"])).date()
        if n > 0 and day.get("hours_covered", 24) < 18:
            break  # ab hier übernimmt Open-Meteo
        extra = by_date.get(local_date, {})
        day = {
            **day,
            "source": "geosphere",
            "uv_index": extra.get("uv_index"),
            "native_apparent_temperature": extra.get("native_apparent_temperature"),
        }
        result.append(day)

    last = (
        dt_util.as_local(datetime.fromisoformat(result[-1]["datetime"])).date()
        if result else date.min
    )
    for e in extended:
        if len(result) >= days:
            break
        if e["date"] > last:
            result.append({k: v for k, v in e.items() if k != "date"})
    return result


def open_meteo_days(om: dict[str, Any]) -> list[dict[str, Any]]:
    """Open-Meteo-Tageswerte im Format der HA-Tagesvorhersage."""
    d = om["daily"]
    result = []
    for i, day_str in enumerate(d["time"]):
        def v(key: str, idx: int = i) -> Any:
            arr = d.get(key)
            return arr[idx] if arr and idx < len(arr) else None

        if v("temperature_2m_max") is None:
            continue
        day = date.fromisoformat(day_str)
        code = v("weather_code")
        sunshine = v("sunshine_duration")
        result.append(
            {
                "date": day,
                "datetime": dt_util.as_utc(dt_util.start_of_local_day(day)).isoformat(),
                "source": "open-meteo",
                "condition": apply_wind(
                    WMO_CONDITION.get(int(code)) if code is not None else None,
                    v("wind_speed_10m_max"),
                ),
                "native_temperature": v("temperature_2m_max"),
                "native_templow": v("temperature_2m_min"),
                "native_apparent_temperature": v("apparent_temperature_max"),
                "native_precipitation": v("precipitation_sum"),
                "precipitation_probability": v("precipitation_probability_max"),
                "native_wind_speed": v("wind_speed_10m_max"),
                "native_wind_gust_speed": v("wind_gusts_10m_max"),
                "wind_bearing": v("wind_direction_10m_dominant"),
                "cloud_coverage": v("cloud_cover_mean"),
                "humidity": v("relative_humidity_2m_mean"),
                "sunshine_hours": round(sunshine / 3600, 1) if sunshine is not None else None,
                "uv_index": om["uv_daily"].get(day_str),
            }
        )
    return result


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
