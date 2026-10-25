# Simulador NMEA 0183

Servidor NMEA 0183 con interfaz web para probar plotters, software de navegación y pilotos.
Solo usa la librería estándar de Python (`pyserial` es opcional, solo para la salida por puerto serie).

## Uso
```
python3 simulador_nmea_ais_arpa_profundidad.py [opciones]
```

| Opción | Descripción |
|---|---|
| `--tcp-port 10111` | Puerto TCP NMEA |
| `--web-port 8085` | Puerto de la interfaz web |
| `--web-host 127.0.0.1` | Interfaz de la web (por defecto `0.0.0.0`, sin autenticación) |
| `--udp HOST:PUERTO` | Salida UDP adicional (repetible), p. ej. `--udp 255.255.255.255:10110` |
| `--serial PUERTO[:BAUDIOS]` | Salida por puerto serie (repetible, requiere pyserial), p. ej. `--serial COM3:38400` |
| `--open` | Abre la web en el navegador (siempre activo en el `.exe`) |

Interfaz web: `http://localhost:8085` (se adapta a móvil; botón de vista Auto / Móvil / Escritorio).
El mapa (Leaflet y OpenStreetMap) necesita Internet.

## Qué emite
- **GPS:** `RMC`, `GGA`, `GLL`, `VTG`, `HDT`.
- **Profundidad:** `DPT`, `DBT`.
- **Navegación de la ruta activa:** `RMB`, `APB`, `XTE`, `BOD`, `BWC` cada segundo; `WPL` y `RTE` cada 10 s.
  XTE positivo = barco a la derecha de la derrota, y se gobierna a la izquierda (`L`).
- **ARPA:** `TTM` con CPA y TCPA calculados.
- **AIS**, por clase de blanco:
  - **A:** mensajes 1 (posición) y 5 (datos estáticos).
  - **B:** mensajes 18 y 24 (partes A y B).
  - **AtoN (balizas y boyas):** mensaje 21, con MMSI `99…`.
  - **Estación base:** mensaje 4, con MMSI `00MID….` (7 cifras en el formulario).
  - **AIS-SART y MOB:** mensaje 1 con estado 14 y mensaje 14 (`SART ACTIVE` / `MOB ACTIVE`), con MMSI `970…` / `972…`.
  - **Mensajes de seguridad:** mensaje 14 de texto libre, de un solo envío.
- Los mensajes lentos (nombres AIS, `WPL`, `RTE`) se reenvían de inmediato cuando se conecta un cliente o salida nueva.

## Salidas
- **TCP:** todos los clientes conectados al puerto `--tcp-port`.
- **UDP:** una sentencia por datagrama (lo más compatible). Se pueden añadir y quitar desde la web.
- **Serie:** a 4800 baudios solo caben unos 480 bytes por segundo; si hay muchos blancos se descartan tramas
  (el contador de "descartadas" se ve en la web). Para AIS usa 38400 o más.
  Para dar un puerto serie a otro programa del mismo equipo, usa un par virtual:
  `com0com` en Windows o `socat -d -d pty,raw,echo=0 pty,raw,echo=0` en Linux.

## Ramas
- `main`: estable.
- `desarrollo`: pruebas del script.
- `android`: app Android (Chaquopy + WebView).

## .exe de Windows
El workflow **Build EXE** (GitHub Actions) genera `SimuladorNMEA.exe` con PyInstaller.
Al subir una etiqueta (`git tag v1.0 && git push --tags`) también lo publica como Release.
