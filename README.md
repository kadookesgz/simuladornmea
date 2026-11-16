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

## Mini-Guard 008 (pantalla `/miniguard`)

Segunda pantalla (enlace «🔔 Mini-Guard 008» en la cabecera) que simula un anunciador de alarmas de 8 canales tipo
Praxis Mini-Guard 008 y emite las alarmas por NMEA por los mismos TCP, UDP y puerto serie.

- Por canal: nombre, contacto NA/NC, retardo individual, activar/desactivar, disparo de la entrada y simulación de cable roto.
- Botones: ACEPTAR, PARAR BOCINA y PRUEBA de lámparas (la prueba no emite alarmas). Bocina opcional en el navegador.
- Salida: `$ERALR,hhmmss.ss,id,A|V,A|V,texto*hh` (hora UTC del cambio, id de alarma, condición A=alarma/V=normal,
  aceptación A=aceptada/V=sin aceptar). Se envía en cada cambio, se repite mientras esté activa y se vuelca al conectar un cliente.
- Ids: canales 1-8 → `id_base`…`id_base+7` (por defecto 001-008); avería de cable → +100 (101-108).
  Talker (por defecto `ER`), id base, periodo de repetición y detección de cable roto son configurables.

**Importante:** el formato NMEA es un diseño propio basado en la sentencia estándar ALR; el equipo real no está documentado
(en lo que he podido consultar) con salida NMEA. La secuencia de pilotos y bocina es la habitual de un anunciador, no el firmware real.
La recepción de `$--ACK` no está implementada.

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
