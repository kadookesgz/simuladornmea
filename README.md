# Simulador NMEA 0183

Servidor NMEA 0183 por TCP con interfaz web: GPS, profundidad, blancos AIS (tipos 1 y 5) y ARPA (TTM con CPA/TCPA), rutas con waypoints y vista adaptada a móvil.

## Uso
```
python3 simulador_nmea_ais_arpa_profundidad.py [--tcp-port 10111] [--web-port 8085] [--web-host 127.0.0.1]
```
- Interfaz web: `http://localhost:8085`
- Salida NMEA: TCP `10111`
- Solo usa la librería estándar de Python. El mapa (Leaflet y OpenStreetMap) necesita Internet.
- Por defecto la web escucha en `0.0.0.0` y no tiene autenticación; usa `--web-host 127.0.0.1` para no exponerla en la red.
