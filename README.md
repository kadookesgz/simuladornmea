# Simulador NMEA – app Android

Empaqueta `simulador_nmea_ais_arpa_profundidad.py` (Python 3.12 con Chaquopy) en un APK.
La app arranca el simulador como servicio en primer plano y muestra la interfaz web en una WebView.

- Interfaz web: se sirve solo en el propio móvil (`127.0.0.1:8085`).
- Salida NMEA por TCP: puerto `10111` (otra app del móvil: `127.0.0.1:10111`; otros equipos: `IP_DEL_MOVIL:10111`).
- El mapa necesita Internet (Leaflet y OpenStreetMap).
- Para detenerlo: botón "Detener" de la notificación. El botón Atrás solo lo manda a segundo plano.

## Compilar sin instalar nada: GitHub Actions
1. Crea un repositorio en GitHub (puede ser privado) y sube todo el contenido de esta carpeta, incluida `.github`.
2. Pestaña **Actions** → workflow **Build APK** (se lanza solo al subir; también con *Run workflow*).
3. Al terminar (unos 5–10 min), descarga el artefacto **simulador-nmea-apk**: dentro está `app-debug.apk`.

## Compilar con Android Studio
Abre esta carpeta como proyecto, deja que sincronice Gradle y pulsa *Build → Build APK(s)*.
Hace falta Python 3.12 instalado en el PC (`python3.12` en el PATH).

## Instalar en el móvil
1. Copia `app-debug.apk` al móvil y ábrelo. Permite "Instalar apps desconocidas" para la app desde la que lo abras.
2. Si HyperOS muestra un aviso de seguridad, elige "Instalar de todos modos".
3. Primer arranque: acepta el permiso de notificaciones.
4. Xiaomi/Poco: Ajustes → Apps → Simulador NMEA → Ahorro de batería → **Sin restricciones**, y activa **Inicio automático**.

## Actualizar el script
Sustituye `app/src/main/python/simulador_nmea_ais_arpa_profundidad.py` y vuelve a compilar.
