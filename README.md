# GeoSphere Austria Forecast (Home Assistant Custom Integration)

Vorhersagen aus dem GeoSphere Data Hub, die die offizielle `zamg`-Integration nicht hat.
Kein API-Key nötig. Daten: GeoSphere Austria, CC BY 4.0.

| Quelle | Datensatz | Verwendung |
|---|---|---|
| NWP | `nwp-v2-1h-1km` | Weather-Entity (stündlich ~53 h, täglich), Tages-Sensoren |
| Nowcast | `nowcast-v1-15min-1km` | aktuelle Werte, Niederschlag nächste Stunde (nur Österreich) |
| Luftqualität | `chem-v2-1h-3km`, `chem_aqi-v1-1d-3km` | NO₂, O₃, PM10, PM2.5, AQI |
| Ensemble | `ensemble-v2-1h-1km` | Regenwahrscheinlichkeit (geschätzt aus 10./50./90. Perzentil), Bandbreite für Temperatur und Niederschlag |
| INCA-Analyse | `inca-v1-1h-1km` | „gemessener“ Niederschlag (1 h / 24 h), Temperatur, Globalstrahlung am Standort, ca. 1 h verzögert |
| Messstation | TAWES `tawes-v1-10min` | Messwerte der nächsten (oder gewählten) Station: Temperatur, Taupunkt, Feuchte, Wind, Druck, Niederschlag, Strahlung, Schneehöhe, Bodentemperatur |
| Klima & Trockenheit | `winfore-v2-1d-1km`, `spartacus-v3-1d-1km` | Referenzverdunstung ET0, klimatische Wasserbilanz 7 Tage, SPEI 30/90/365, Temperaturabweichung vom Mittel 1991–2020 (ca. 2 Tage verzögert) |
| Schneedecke | `snowgrid_cl-v2-1d-1km` | Schneehöhe und Schneelast am Standort (ca. 2 Tage verzögert) |
| Verlängerung (optional) | Open-Meteo `geosphere_seamless` + Standardmodell | Tagesvorhersage auf 3–15 Tage (GeoSphere-Tage bleiben, danach ECMWF), UV-Index stündlich/täglich, gefühlte Temperatur |
| Wüstenstaub | `chem_dust-v1-1h-0p2deg` | Staubsäule (mg/m²) jetzt, Max. 24 h, Max. 5 Tage + Zeitpunkt |
| Warnungen | warnungen.zamg.at | Warnstufe + Details als Attribut |

## Für Automatisierungen

| Entity | Logik |
|---|---|
| Regen in der nächsten Stunde | Nowcast sagt Regen (≥ 0,05 mm / 15 min) innerhalb 60 min voraus; ohne Nowcast: Regenwahrscheinlichkeit ≥ 50 % |
| Regenbeginn / Regenende | Zeitstempel aus dem 15-min-Nowcast (ca. 3 h Horizont), leer wenn nicht im Horizont |
| Frostgefahr heute Nacht | Tiefstwert bis 09:00 < 2 °C – auch wenn nur das 10. Perzentil des Ensembles darunter liegt |
| Gewittergefahr (12 h) | Gewittersymbol oder CAPE ≥ 1000 J/kg mit ≥ 0,5 mm/h in den nächsten 12 h |
| Bewässerung nötig | Wasserbilanz 7 Tage ≤ −10 mm und < 2 mm Regen in den nächsten 24 h erwartet |
| Unwetterwarnung aktiv (+ je Typ) | aktive Warnung ab Stufe gelb; Attribute: Stufe, bis wann, nächste Warnung |

Die Wetter-Entity bietet stündliche, tägliche und Tag/Nacht-Vorhersage (06–18 / 18–06 Uhr).

**Aktueller Zustand:** Grundlage ist das Modell-Wettersymbol. Zeigen Radar-Nowcast oder eine Station im Umkreis
von 10 km *jetzt* Regen, Schnee oder Nebel (bzw. Trockenheit trotz Regensymbol), gilt die Beobachtung
(Attribut `condition_source`). Ab 10,8 m/s Mittelwind wird `windy` / `windy-variant` angezeigt.

**Zusatzfelder in der Vorhersage** (über `weather.get_forecasts`): `global_radiation`, `snow_limit`, `cape`,
`sunshine_minutes` / `sunshine_hours`, `native_dew_point`, `uv_index`, `temp_p10`/`temp_p90`, `precip_p10`/`precip_p90`,
`rain`, `snow`, `source` (geosphere / open-meteo).

**Robustheit:** Schafft GeoSphere einen Parameter ab, wird er automatisch weggelassen statt die ganze Abfrage
zu verlieren. Nach Fehlern wird nach 1, 2, 3, 5, 8, 13 min erneut versucht, bis dahin bleiben die letzten Daten erhalten.

## Installation

### Über HACS (empfohlen)
1. HACS → ⋮ → *Benutzerdefinierte Repositories* → `https://github.com/Schleifmaschine/geosphere-forecast`, Typ *Integration*.
2. „GeoSphere Austria Forecast“ herunterladen, Home Assistant neu starten.
3. *Einstellungen → Geräte & Dienste → Integration hinzufügen → „GeoSphere Austria Forecast“*.

### Manuell
1. Ordner `custom_components/geosphere_forecast` nach `/config/custom_components/` kopieren.
2. Home Assistant neu starten.
3. *Einstellungen → Geräte & Dienste → Integration hinzufügen → „GeoSphere Austria Forecast“*, Standort auf der Karte wählen.
4. Über *Konfigurieren* lassen sich Nowcast, Luftqualität und Warnungen abschalten.

## Stündliche Werte als Attribut
Viele Sensoren haben ein Attribut `forecast` (`[{datetime, value}, …]`, nicht im Recorder gespeichert),
z. B. für ApexCharts:

```yaml
type: custom:apexcharts-card
graph_span: 48h
span: { start: hour }
series:
  - entity: sensor.wien_temperatur
    data_generator: |
      return entity.attributes.forecast.map(p => [new Date(p.datetime).getTime(), p.value]);
```

---
Inoffizielles Community-Projekt, nicht mit GeoSphere Austria verbunden. Name und Logo sind Marken von GeoSphere Austria
(Brand-Bilder aus dem [Home-Assistant-Brands-Repo](https://github.com/home-assistant/brands)).
