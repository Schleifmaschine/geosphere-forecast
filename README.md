# GeoSphere Austria Forecast (Home Assistant Custom Integration)

Vorhersagen aus dem GeoSphere Data Hub, die die offizielle `zamg`-Integration nicht hat.
Kein API-Key nötig. Daten: GeoSphere Austria, CC BY 4.0.

| Quelle | Datensatz | Verwendung |
|---|---|---|
| NWP | `nwp-v2-1h-1km` | Weather-Entity (stündlich ~53 h, täglich), Tages-Sensoren |
| Nowcast | `nowcast-v1-15min-1km` | aktuelle Werte, Niederschlag nächste Stunde (nur Österreich) |
| Luftqualität | `chem-v2-1h-3km`, `chem_aqi-v1-1d-3km` | NO₂, O₃, PM10, PM2.5, AQI |
| Wüstenstaub | `chem_dust-v1-1h-0p2deg` | Staubsäule (mg/m²) jetzt, Max. 24 h, Max. 5 Tage + Zeitpunkt |
| Warnungen | warnungen.zamg.at | Warnstufe + Details als Attribut |

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
