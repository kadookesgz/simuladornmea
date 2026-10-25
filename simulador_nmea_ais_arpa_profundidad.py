#!/usr/bin/env python3
"""
Simulador NMEA 0183 (GPS + profundidad + AIS + ARPA) con interfaz web.

- Dibuja rutas con waypoints (arrastrables, clic derecho para borrar) y el barco las navega.
- Modo manual opcional: avanzar a estima con el rumbo y la velocidad configurados.
- Blancos sintéticos AIS (tipos 1 y 5) y ARPA (TTM con CPA/TCPA calculados).
- Servidor web multihilo (por defecto :8085) y servidor TCP NMEA (por defecto :10111).
- Sin dependencias de Python. La UI carga Leaflet y los mapas de OpenStreetMap desde Internet.
"""

import argparse
import errno
import json
import math
import queue
import socket
import sys
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

DEFAULT_TCP_PORT = 10111
DEFAULT_WEB_PORT = 8085
MAX_BODY_BYTES = 1_000_000
MAX_TARGETS = 50
MAX_WAYPOINTS = 500
WP_ARRIVAL_M = 10.0               # radio de llegada a un waypoint
STATIC_AIS_INTERVAL_S = 15.0      # cada cuánto se emite el AIS tipo 5 (nombre, dimensiones)
NM_M = 1852.0
KNOT_MS = NM_M / 3600.0           # 1 nudo en m/s
R_EARTH_METERS = 6371000.0

stop_event = threading.Event()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    # En Windows SO_REUSEADDR permite que dos procesos compartan puerto; se usa acceso exclusivo.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# Orden de bloqueo si hace falta anidar: route_lock -> state_lock.
state_lock = threading.Lock()
gps_state = {
    "lat": 43.6833,
    "lon": -7.5950,
    "speed_knots": 10.0,      # velocidad configurada
    "sog_knots": 0.0,         # velocidad realmente emitida (0 si el barco no avanza)
    "heading": 0.0,
    "altitude_m": 5.0,
    "depth_m": 20.0,
    "satellites": 10,
    "manual_drive": False,    # avanzar a estima fuera de ruta
    "tcp_port": DEFAULT_TCP_PORT,
    "connected_clients": 0,
    "sentences_sent": 0,
    "latest_sentences": [],
    "status_msg": "Iniciado",
}

route_lock = threading.Lock()
route_state = {
    "waypoints": [],         # lista de [lat, lon]
    "is_navigating": False,
    "loop_route": True,
    "current_wp_idx": 0,
}

targets_lock = threading.Lock()
targets = []                 # blancos sintéticos AIS/ARPA

tcp_clients = []
tcp_clients_lock = threading.Lock()
tcp_server_sock = None
tcp_server_lock = threading.Lock()

_static_last = {}            # mmsi -> instante (monotonic) del último AIS tipo 5
_ais_seq = 0                 # identificador de secuencia para mensajes AIS multisentencia


# ----------------- CÁLCULOS NÁUTICOS -----------------
def haversine_distance(lat1, lon1, lat2, lon2):
    """Distancia en metros entre dos puntos."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    return R_EARTH_METERS * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def calculate_bearing(lat1, lon1, lat2, lon2):
    """Rumbo inicial (0-360) entre dos puntos."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)
    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination_point(lat, lon, distance_m, bearing_deg):
    """Punto alcanzado tras recorrer distance_m con rumbo bearing_deg."""
    delta = distance_m / R_EARTH_METERS
    theta = math.radians(bearing_deg)
    phi1, lambda1 = math.radians(lat), math.radians(lon)
    sin_phi2 = math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    phi2 = math.asin(max(-1.0, min(1.0, sin_phi2)))
    y = math.sin(theta) * math.sin(delta) * math.cos(phi1)
    x = math.cos(delta) - math.sin(phi1) * math.sin(phi2)
    lambda2 = lambda1 + math.atan2(y, x)
    return math.degrees(phi2), (math.degrees(lambda2) + 540.0) % 360.0 - 180.0


def target_metrics(own, t):
    """Distancia, demora, CPA y TCPA de un blanco respecto al barco propio.

    own = (lat, lon, sog_kn, cog). Aproximación plana local, válida para distancias de radar.
    TCPA en minutos (negativo si el blanco se aleja, None si no hay movimiento relativo).
    """
    own_lat, own_lon, own_sog, own_cog = own
    dist_nm = haversine_distance(own_lat, own_lon, t["lat"], t["lon"]) / NM_M
    bearing = calculate_bearing(own_lat, own_lon, t["lat"], t["lon"])
    br = math.radians(bearing)
    rx, ry = dist_nm * math.sin(br), dist_nm * math.cos(br)        # posición relativa (nm)

    t_sog = float(t.get("speed", 0.0))
    t_cog = math.radians(float(t.get("heading", 0.0)))
    o_cog = math.radians(own_cog)
    vx = t_sog * math.sin(t_cog) - own_sog * math.sin(o_cog)       # velocidad relativa (kn)
    vy = t_sog * math.cos(t_cog) - own_sog * math.cos(o_cog)
    v2 = vx * vx + vy * vy

    if v2 < 1e-6:
        cpa, tcpa = dist_nm, None
    else:
        t_h = -(rx * vx + ry * vy) / v2
        cpa = dist_nm if t_h <= 0 else math.hypot(rx + vx * t_h, ry + vy * t_h)
        tcpa = max(-999.9, min(999.9, t_h * 60.0))
    return {"distance_nm": dist_nm, "bearing": bearing, "cpa_nm": cpa, "tcpa_min": tcpa}


# ----------------- NMEA 0183 -----------------
def calculate_checksum(sentence: str) -> str:
    csum = 0
    for char in sentence:
        csum ^= ord(char)
    return f"{csum:02X}"


def nmea_sentence(body: str) -> str:
    return f"${body}*{calculate_checksum(body)}"


def fmt_deg(value: float) -> str:
    """Grados con un decimal, siempre en [0, 360)."""
    return f"{round(value, 1) % 360.0:.1f}"


def deg_to_nmea(degrees: float, is_lat: bool) -> tuple:
    """Grados decimales -> (ddmm.mmmm | dddmm.mmmm, hemisferio), sin minutos=60.0000 por redondeo."""
    if is_lat:
        hemi, width = ("N" if degrees >= 0 else "S"), 2
    else:
        hemi, width = ("E" if degrees >= 0 else "W"), 3
    total = int(round(abs(degrees) * 60.0 * 10000))      # diezmilésimas de minuto
    d, rem = divmod(total, 60 * 10000)
    return f"{d:0{width}d}{rem / 10000.0:07.4f}", hemi


# ----- AIS (ITU-R M.1371) -----
_AIS_CHARSET = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"


def _bits(value, width):
    return format(int(value) & ((1 << width) - 1), f"0{width}b")


def _ais_text(text, chars):
    text = text.upper()[:chars].ljust(chars, "@")
    return "".join(_bits(max(_AIS_CHARSET.find(ch), 0), 6) for ch in text)


def _armor(bitstring):
    fill = (6 - len(bitstring) % 6) % 6
    bitstring += "0" * fill
    chars = "".join(chr(v + 48 if v < 40 else v + 56)
                    for v in (int(bitstring[i:i + 6], 2) for i in range(0, len(bitstring), 6)))
    return chars, fill


def _ais_sentences(bitstring):
    """Convierte bits en una o varias sentencias !AIVDM (máx. 60 caracteres de payload cada una)."""
    global _ais_seq
    chars, fill = _armor(bitstring)
    parts = [chars[i:i + 60] for i in range(0, len(chars), 60)]
    seq = ""
    if len(parts) > 1:
        _ais_seq = (_ais_seq + 1) % 10
        seq = str(_ais_seq)
    out = []
    for n, part in enumerate(parts, 1):
        body = f"AIVDM,{len(parts)},{n},{seq},A,{part},{fill if n == len(parts) else 0}"
        out.append(f"!{body}*{calculate_checksum(body)}")
    return out


def encode_ais_type1(mmsi, lat, lon, speed_knots, course):
    """AIS clase A, mensaje 1 (informe de posición), 168 bits."""
    lon_raw = max(-108000000, min(108000000, int(round(lon * 600000))))
    lat_raw = max(-54000000, min(54000000, int(round(lat * 600000))))
    sog = max(0, min(1022, int(round(speed_knots * 10))))
    cog = max(0, min(3599, int(round((course % 360) * 10))))
    hdg = int(round(course)) % 360
    nav_status = 0 if speed_knots > 0.2 else 5            # 0 = navegando con motor, 5 = amarrado
    msg = "".join([
        _bits(1, 6), _bits(0, 2), _bits(mmsi, 30), _bits(nav_status, 4),
        _bits(-128, 8),                                    # ROT: no disponible
        _bits(sog, 10), _bits(0, 1), _bits(lon_raw, 28), _bits(lat_raw, 27),
        _bits(cog, 12), _bits(hdg, 9),
        _bits(datetime.now(timezone.utc).second, 6),       # sello de tiempo UTC
        _bits(0, 2), _bits(0, 3), _bits(0, 1), _bits(0, 19),
    ])
    assert len(msg) == 168
    return _ais_sentences(msg)[0]


def encode_ais_type5(mmsi, name, ship_type=70):
    """AIS clase A, mensaje 5 (datos estáticos: nombre, tipo, dimensiones), 424 bits en 2 sentencias."""
    msg = "".join([
        _bits(5, 6), _bits(0, 2), _bits(mmsi, 30), _bits(0, 2),
        _bits(0, 30),                                      # IMO: no disponible
        _ais_text("", 7), _ais_text(name, 20),             # indicativo vacío y nombre
        _bits(ship_type, 8),
        _bits(40, 9), _bits(10, 9), _bits(5, 6), _bits(5, 6),   # proa, popa, babor, estribor (m)
        _bits(1, 4),                                       # GPS
        _bits(0, 4), _bits(0, 5), _bits(24, 5), _bits(60, 6),   # ETA no disponible
        _bits(30, 8),                                      # calado 3,0 m
        _ais_text("", 20), _bits(0, 1), _bits(0, 1),
    ])
    assert len(msg) == 424
    return _ais_sentences(msg)


# ----- Sentencias compuestas -----
def build_depth_sentences(depth_m: float):
    depth_ft = depth_m * 3.280839895
    depth_fathoms = depth_m * 0.546806649
    return [nmea_sentence(f"SDDPT,{depth_m:.1f},0.0"),
            nmea_sentence(f"SDDBT,{depth_ft:.1f},f,{depth_m:.1f},M,{depth_fathoms:.1f},F")]


def build_ttm(t, m, time_str):
    """$RATTM correcto: nº, dist, demora, T, vel, rumbo, T, CPA, TCPA, unidad, nombre, estado, ref, hora, adq."""
    tcpa = "" if m["tcpa_min"] is None else f"{m['tcpa_min']:.1f}"
    body = (f"RATTM,{t['id'] % 100},{min(m['distance_nm'], 999.99):.2f},{fmt_deg(m['bearing'])},T,"
            f"{t['speed']:.1f},{fmt_deg(t['heading'])},T,{min(m['cpa_nm'], 999.99):.2f},{tcpa},N,"
            f"{t['name'][:8]},T,R,{time_str},A")
    return nmea_sentence(body)


def build_target_sentences(target_list, own, time_str):
    out = []
    now_m = time.monotonic()
    live = {t["mmsi"] for t in target_list}
    for mmsi in [k for k in _static_last if k not in live]:
        del _static_last[mmsi]
    for t in target_list:
        if t.get("arpa", True):
            out.append(build_ttm(t, target_metrics(own, t), time_str))
        if t.get("ais", True):
            out.append(encode_ais_type1(t["mmsi"], t["lat"], t["lon"], t["speed"], t["heading"]))
            if now_m - _static_last.get(t["mmsi"], -1e9) >= STATIC_AIS_INTERVAL_S:
                out.extend(encode_ais_type5(t["mmsi"], t["name"]))
                _static_last[t["mmsi"]] = now_m
    return out


def build_nmea():
    with state_lock:
        lat, lon = gps_state["lat"], gps_state["lon"]
        sog = gps_state["sog_knots"]
        heading = gps_state["heading"]
        alt, sats, depth = gps_state["altitude_m"], gps_state["satellites"], gps_state["depth_m"]

    now = datetime.now(timezone.utc)
    time_str = now.strftime("%H%M%S.00")
    date_str = now.strftime("%d%m%y")
    lat_s, lat_h = deg_to_nmea(lat, True)
    lon_s, lon_h = deg_to_nmea(lon, False)
    hdg = fmt_deg(heading)

    sentences = [
        nmea_sentence(f"GPRMC,{time_str},A,{lat_s},{lat_h},{lon_s},{lon_h},{sog:.1f},{hdg},{date_str},,,A"),
        nmea_sentence(f"GPGGA,{time_str},{lat_s},{lat_h},{lon_s},{lon_h},1,{sats:02d},0.9,{alt:.1f},M,0.0,M,,"),
        nmea_sentence(f"GPGLL,{lat_s},{lat_h},{lon_s},{lon_h},{time_str},A,A"),
        nmea_sentence(f"GPVTG,{hdg},T,,M,{sog:.1f},N,{sog * 1.852:.1f},K,A"),
        nmea_sentence(f"HEHDT,{hdg},T"),
    ]
    sentences.extend(build_depth_sentences(depth))
    with targets_lock:
        target_snapshot = [dict(t) for t in targets]
    sentences.extend(build_target_sentences(target_snapshot, (lat, lon, sog, heading), time_str))
    return sentences


# ----------------- SERVIDOR TCP -----------------
class TcpClient:
    """Cada cliente tiene su propia cola y su hilo: un cliente lento no bloquea al resto."""

    def __init__(self, conn, addr):
        self.conn = conn
        self.addr = addr
        self.alive = True
        self.q = queue.Queue(maxsize=5)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, payload: bytes):
        if self.alive:
            try:
                self.q.put_nowait(payload)
            except queue.Full:
                pass   # cliente lento: se descarta esta ráfaga

    def close(self):
        self.alive = False
        for fn in (lambda: self.conn.shutdown(socket.SHUT_RDWR), self.conn.close):
            try:
                fn()
            except OSError:
                pass

    def _run(self):
        try:
            while self.alive and not stop_event.is_set():
                try:
                    data = self.q.get(timeout=1.0)
                except queue.Empty:
                    continue
                self.conn.sendall(data)
        except OSError:
            pass
        finally:
            self.close()
            with tcp_clients_lock:
                if self in tcp_clients:
                    tcp_clients.remove(self)
                cnt = len(tcp_clients)
            with state_lock:
                gps_state["connected_clients"] = cnt
            print(f"[-] [CLIENTE DESCONECTADO] {self.addr[0]}:{self.addr[1]} | Clientes activos: {cnt}")


def start_tcp_listener(port: int):
    """Abre el puerto nuevo ANTES de cerrar el anterior: si falla, el listener actual sigue activo."""
    global tcp_server_sock
    if not 1 <= port <= 65535:
        return False, "Puerto fuera de rango (1-65535)"

    with tcp_server_lock:
        with state_lock:
            current = gps_state["tcp_port"]
        if tcp_server_sock is not None and current == port:
            return True, f"Puerto {port} ya activo"

        new_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                new_sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                new_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            new_sock.bind(("0.0.0.0", port))
            new_sock.listen(15)
            new_sock.settimeout(1.0)
        except OSError as e:
            new_sock.close()
            err = f"No se pudo abrir el puerto {port}: {e}"
            print(f"[!] [TCP ERROR] {err}")
            with state_lock:
                gps_state["status_msg"] = err
            return False, err

        old_sock, tcp_server_sock = tcp_server_sock, new_sock
        if old_sock:
            try:
                old_sock.close()
            except OSError:
                pass
        with tcp_clients_lock:
            old_clients = list(tcp_clients)
            tcp_clients.clear()
        for c in old_clients:
            c.close()
        with state_lock:
            gps_state["tcp_port"] = port
            gps_state["connected_clients"] = 0
            gps_state["status_msg"] = f"Escuchando en 0.0.0.0:{port}"
    print(f"[*] [TCP] Servidor NMEA escuchando en 0.0.0.0:{port}")
    return True, f"Puerto {port} activo"


def accept_worker():
    while not stop_event.is_set():
        with tcp_server_lock:
            sock = tcp_server_sock
        if sock is None:
            stop_event.wait(0.3)
            continue
        try:
            conn, addr = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            stop_event.wait(0.3)
            continue
        try:
            conn.settimeout(5.0)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        client = TcpClient(conn, addr)
        with tcp_clients_lock:
            tcp_clients.append(client)
            cnt = len(tcp_clients)
        with state_lock:
            gps_state["connected_clients"] = cnt
        client.start()
        print(f"[+] [CLIENTE CONECTADO] {addr[0]}:{addr[1]} | Clientes activos: {cnt}")


# ----------------- MOTOR DE NAVEGACIÓN -----------------
def advance_own_ship(dt: float):
    """Avanza el barco propio dt segundos (ruta, o a estima si está activado el modo manual)."""
    log = []
    with route_lock, state_lock:
        wps = route_state["waypoints"]
        navigating = route_state["is_navigating"]
        idx = route_state["current_wp_idx"]
        loop = route_state["loop_route"]
        lat, lon = gps_state["lat"], gps_state["lon"]
        speed = gps_state["speed_knots"]
        heading = gps_state["heading"]
        step = speed * KNOT_MS * dt
        sog = 0.0

        if navigating and wps:
            if idx >= len(wps):
                idx = 0
            tlat, tlon = wps[idx]
            if haversine_distance(lat, lon, tlat, tlon) <= max(step, WP_ARRIVAL_M):
                lat, lon = tlat, tlon
                sog = speed
                if idx + 1 < len(wps):
                    log.append(f"[*] [RUTA] Waypoint {idx + 1}/{len(wps)} alcanzado. Rumbo al siguiente.")
                    idx += 1
                elif loop and len(wps) > 1:
                    log.append("[*] [RUTA] Fin de ruta alcanzado. Reiniciando bucle.")
                    idx = 0
                else:
                    log.append("[*] [RUTA] Ruta completada.")
                    idx = 0
                    route_state["is_navigating"] = False
                    sog = 0.0
            elif step > 0:
                bearing = calculate_bearing(lat, lon, tlat, tlon)
                lat, lon = destination_point(lat, lon, step, bearing)
                heading = round(bearing, 1)
                sog = speed
            route_state["current_wp_idx"] = idx
        elif gps_state["manual_drive"] and step > 0:
            lat, lon = destination_point(lat, lon, step, heading)
            sog = speed

        gps_state["lat"], gps_state["lon"] = lat, lon
        gps_state["heading"] = heading
        gps_state["sog_knots"] = sog
    for line in log:
        print(line)


def advance_targets(dt: float):
    with targets_lock:
        for t in targets:
            step_m = max(0.0, float(t.get("speed", 0.0))) * KNOT_MS * dt
            if step_m > 0:
                t["lat"], t["lon"] = destination_point(t["lat"], t["lon"], step_m, float(t.get("heading", 0.0)))


def broadcast_nmea():
    sentences = build_nmea()
    payload = ("\r\n".join(sentences) + "\r\n").encode("ascii", errors="replace")
    with state_lock:
        gps_state["latest_sentences"] = sentences
        gps_state["sentences_sent"] += len(sentences)
    with tcp_clients_lock:
        clients = list(tcp_clients)
    for c in clients:
        c.send(payload)


def navigation_and_broadcast_worker():
    """Física y emisión a 1 Hz. Un error puntual no debe matar el hilo."""
    period = 1.0
    last = time.monotonic()
    next_tick = last
    while not stop_event.is_set():
        try:
            now = time.monotonic()
            dt = min(max(now - last, 0.0), 5.0)
            last = now
            advance_own_ship(dt)
            advance_targets(dt)
            broadcast_nmea()
        except Exception as e:   # noqa: BLE001
            print(f"[!] [WORKER] {type(e).__name__}: {e}")
        next_tick += period
        delay = next_tick - time.monotonic()
        if delay < 0:            # nos hemos retrasado: no emitir ráfagas para recuperar
            next_tick = time.monotonic()
            delay = 0.0
        stop_event.wait(delay)


# ----------------- API (validación + lógica) -----------------
def _num(body, key, lo, hi, default=None):
    if key not in body:
        if default is None:
            raise ValueError(f"Falta el campo '{key}'")
        return default
    try:
        v = float(body[key])
    except (TypeError, ValueError):
        raise ValueError(f"'{key}' no es un número") from None
    if not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError(f"'{key}' fuera de rango ({lo} a {hi})")
    return v


def _parse_waypoints(raw):
    if not isinstance(raw, list):
        raise ValueError("'waypoints' debe ser una lista")
    if len(raw) > MAX_WAYPOINTS:
        raise ValueError(f"Máximo {MAX_WAYPOINTS} waypoints")
    out = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise ValueError("Waypoint inválido: se espera [lat, lon]")
        wlat = _num({"lat": p[0]}, "lat", -90, 90)
        wlon = _num({"lon": p[1]}, "lon", -180, 180)
        out.append([wlat, wlon])
    return out


def _clean_name(raw):
    """Solo ASCII imprimible seguro para NMEA y para el alfabeto de 6 bits de AIS."""
    text = unicodedata.normalize("NFKD", str(raw)).encode("ascii", "ignore").decode("ascii")   # Ñ -> N, Ú -> U
    name = "".join(ch for ch in text.upper() if ch.isalnum() or ch in "-_ ")
    return name[:20].strip() or "BLANCO"


def api_set_pos(body):
    updates = {}
    if "lat" in body:
        updates["lat"] = _num(body, "lat", -90, 90)
    if "lon" in body:
        updates["lon"] = _num(body, "lon", -180, 180)
    if "speed" in body:
        updates["speed_knots"] = _num(body, "speed", 0, 200)
    if "heading" in body:
        updates["heading"] = _num(body, "heading", 0, 360) % 360.0
    if "manual_drive" in body:
        updates["manual_drive"] = bool(body["manual_drive"])
    with state_lock:
        gps_state.update(updates)
    return {"status": "ok"}


def api_set_route(body):
    wps = _parse_waypoints(body["waypoints"]) if "waypoints" in body else None
    with route_lock:
        if wps is not None:
            route_state["waypoints"] = wps
        if "is_navigating" in body:
            route_state["is_navigating"] = bool(body["is_navigating"])
        if "loop_route" in body:
            route_state["loop_route"] = bool(body["loop_route"])
        if route_state["current_wp_idx"] >= len(route_state["waypoints"]):
            route_state["current_wp_idx"] = 0
    return {"status": "ok"}


def api_set_port(body):
    try:
        port = int(body.get("port", DEFAULT_TCP_PORT))
    except (TypeError, ValueError):
        raise ValueError("Puerto inválido") from None
    ok, msg = start_tcp_listener(port)
    if not ok:
        raise ValueError(msg)
    return {"status": "ok", "port": port, "message": msg}


def api_set_depth(body):
    depth = _num(body, "depth", 0, 12000)
    with state_lock:
        gps_state["depth_m"] = depth
    return {"status": "ok", "depth_m": depth}


def api_add_target(body):
    lat = _num(body, "lat", -90, 90)
    lon = _num(body, "lon", -180, 180)
    try:
        mmsi = int(body["mmsi"])
    except (TypeError, ValueError):
        raise ValueError("MMSI inválido") from None
    if not 100000000 <= mmsi <= 999999999:
        raise ValueError("MMSI debe tener 9 cifras")
    speed = _num(body, "speed", 0, 102.2, default=0.0)
    heading = _num(body, "heading", 0, 360, default=0.0) % 360.0
    arpa, ais = bool(body.get("arpa", True)), bool(body.get("ais", True))
    if not arpa and not ais:
        raise ValueError("Activa AIS, ARPA o ambos")
    with targets_lock:
        if len(targets) >= MAX_TARGETS:
            raise ValueError(f"Máximo {MAX_TARGETS} blancos sintéticos")
        if any(t["mmsi"] == mmsi for t in targets):
            raise ValueError("Ese MMSI ya existe")
        used = {t["id"] for t in targets}
        tid = next(i for i in range(1, 100) if i not in used)    # nº de blanco TTM (1-99)
        target = {"id": tid, "lat": lat, "lon": lon, "mmsi": mmsi, "name": _clean_name(body.get("name", "")),
                  "speed": speed, "heading": heading, "arpa": arpa, "ais": ais}
        targets.append(target)
    return {"status": "ok", "target": target}


def api_delete_target(body):
    try:
        tid = int(body["id"])
    except (TypeError, ValueError):
        raise ValueError("Id inválido") from None
    with targets_lock:
        before = len(targets)
        targets[:] = [t for t in targets if t["id"] != tid]
        if len(targets) == before:
            raise ValueError("Ese blanco no existe")
    return {"status": "ok"}


def api_clear_targets(body):
    with targets_lock:
        targets.clear()
    return {"status": "ok"}


POST_ROUTES = {
    "/set_pos": api_set_pos,
    "/set_route": api_set_route,
    "/set_port": api_set_port,
    "/set_depth": api_set_depth,
    "/targets": api_add_target,
    "/targets/delete": api_delete_target,
    "/targets/clear": api_clear_targets,
}


def targets_with_metrics(own):
    with targets_lock:
        snapshot = [dict(t) for t in targets]
    for t in snapshot:
        t.update(target_metrics(own, t))
    return snapshot


def snapshot_status():
    with state_lock:
        st = dict(gps_state)
    with route_lock:
        st["route_info"] = {
            "is_navigating": route_state["is_navigating"],
            "current_wp_idx": route_state["current_wp_idx"],
            "total_wps": len(route_state["waypoints"]),
            "loop_route": route_state["loop_route"],
            "waypoints": [list(p) for p in route_state["waypoints"]],
        }
    st["targets"] = targets_with_metrics((st["lat"], st["lon"], st["sog_knots"], st["heading"]))
    return st


HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Simulador NMEA GPS - Trazador de Rutas</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <script>
    // Vista: auto (según ancho), móvil o escritorio. Se recuerda en localStorage; también ?view=mobile|desktop|auto
    (function () {
      var root = document.documentElement;
      var mq = window.matchMedia('(max-width: 820px)');
      var valid = {auto: 'mobile', mobile: 'desktop', desktop: 'auto'};
      window.viewPref = 'auto';
      try { window.viewPref = localStorage.getItem('nmea_view') || 'auto'; } catch (e) {}
      var q = /[?&]view=(mobile|desktop|auto)/.exec(location.search);
      if (q) window.viewPref = q[1];
      if (!valid[window.viewPref]) window.viewPref = 'auto';
      window.isMobileView = function () { return root.classList.contains('mobile'); };
      window.applyView = function () {
        var m = window.viewPref === 'mobile' || (window.viewPref === 'auto' && mq.matches);
        root.classList.toggle('mobile', m);
        var b = document.getElementById('btn-view');
        if (b) b.innerText = window.viewPref === 'auto' ? '📱 Auto' : (window.viewPref === 'mobile' ? '📱 Móvil' : '🖥 Escritorio');
        if (window.onViewChange) window.onViewChange(m);
      };
      window.cycleView = function () {
        window.viewPref = valid[window.viewPref];
        try { localStorage.setItem('nmea_view', window.viewPref); } catch (e) {}
        window.applyView();
      };
      if (mq.addEventListener) mq.addEventListener('change', window.applyView); else mq.addListener(window.applyView);
      window.applyView();
    })();
  </script>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0b111e; color: #e2e8f0; height: 100vh; height: 100dvh; display: flex; flex-direction: column; overflow: hidden; }
    header { background: #111a2e; border-bottom: 1px solid #1e293b; padding: 8px 16px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 6px; }
    .badge { background: #0284c7; color: white; padding: 3px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }
    #main { display: flex; flex: 1; min-height: 0; }
    #map { flex: 1; height: 100%; cursor: crosshair; }
    #side { width: 370px; background: #0f172a; border-left: 1px solid #1e293b; padding: 12px; display: flex; flex-direction: column; gap: 9px; overflow-y: auto; }
    .card { background: #1e293b; border-radius: 6px; padding: 10px; border: 1px solid #334155; }
    .coords { font-family: monospace; font-size: 14px; color: #38bdf8; font-weight: bold; }
    .feed { background: #050811; border-radius: 4px; padding: 6px; font-family: monospace; font-size: 10.5px; color: #4ade80; height: 125px; overflow: auto; line-height: 1.35; white-space: pre; }
    .stat-badge { display: inline-block; padding: 2px 6px; border-radius: 4px; font-weight: bold; background: #334155; color: #f8fafc; font-size: 12px; }
    .btn { background: #0284c7; color: white; border: none; padding: 6px 12px; border-radius: 4px; font-weight: bold; cursor: pointer; transition: background 0.15s; font-size: 12px; }
    .btn:hover { background: #0369a1; }
    .btn-green { background: #10b981; }
    .btn-green:hover { background: #059669; }
    .btn-red { background: #ef4444; }
    .btn-red:hover { background: #dc2626; }
    .btn-amber { background: #f59e0b; }
    .btn-amber:hover { background: #d97706; }
    input[type="number"], input[type="text"] { background: #0b111e; border: 1px solid #475569; color: #f8fafc; padding: 5px 7px; border-radius: 4px; font-size: 12px; }
    .btn-group { display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; }
    .pill { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; padding: 2px 6px; border-radius: 4px; background: #0b111e; color: #94a3b8; }
    .tgt-tip { background: #0b111e; color: #fda4af; border: 1px solid #334155; font-size: 10px; padding: 1px 4px; box-shadow: none; }
    .monitor { flex: 1; display: flex; flex-direction: column; }
    #btn-mapsize { display: none; }
    .tgt-row { display: flex; justify-content: space-between; align-items: center; gap: 6px; padding: 3px 0; border-bottom: 1px solid #334155; }

    /* ===== Vista móvil: mapa arriba, panel de control con scroll debajo ===== */
    .mobile #main { flex-direction: column; }
    .mobile #map { flex: none; width: 100%; height: 42vh; height: 42dvh; min-height: 220px; cursor: default; }
    .mobile.map-big #map { height: 68vh; height: 68dvh; }
    .mobile #side { width: 100%; flex: 1; min-height: 0; border-left: none; border-top: 1px solid #1e293b; gap: 10px;
                    padding: 10px 10px calc(16px + env(safe-area-inset-bottom)); -webkit-overflow-scrolling: touch; }
    .mobile #side > * { flex-shrink: 0; }
    .mobile .monitor { flex: none; }
    .mobile #btn-mapsize { display: inline-flex; }
    .mobile header { padding: 6px 10px; }
    .mobile .btn { padding: 10px 14px; font-size: 14px; min-height: 42px; }
    .mobile .btn-group .btn { flex: 1 1 auto; }
    .mobile .tgt-row .btn { padding: 4px 12px !important; min-height: 34px; }
    .mobile input[type="number"], .mobile input[type="text"] { font-size: 16px; padding: 9px 8px; }  /* 16px evita el zoom de iOS */
    .mobile input[type="checkbox"] { width: 20px; height: 20px; flex: none; }
    .mobile #side [style*="font-size"] { font-size: 12.5px !important; }   /* unifica los textos de 10-11px del panel */
    .mobile .feed { height: 140px; font-size: 11px; }
    .mobile .leaflet-control-zoom a { width: 38px; height: 38px; line-height: 38px; }
  </style>
</head>
<body>
  <header>
    <div style="display:flex; align-items:center; gap:8px;">
      <strong>GPS NMEA 0183 SIMULATOR</strong>
      <span class="badge" id="header-port-badge">TCP: 10111</span>
      <span id="route-indicator" class="pill" style="color:#fbbf24;">⚓ Modo Manual</span>
    </div>
    <div style="font-size:12px;">
      <button class="btn" id="btn-view" onclick="cycleView()" style="padding:3px 8px" title="Cambiar vista: auto / móvil / escritorio">📱 Auto</button>
      <button class="btn" id="btn-mapsize" onclick="toggleMapSize()" style="padding:3px 8px">⤢ Mapa</button>
      <span id="conn" class="pill">● conectando…</span>
      Clientes TCP: <span id="clients" class="stat-badge">0</span> | Tramas: <span id="sent" class="stat-badge">0</span>
    </div>
  </header>
  <div id="main">
    <div id="map"></div>
    <div id="side">

      <!-- CONTROL DE RUTA -->
      <div class="card" style="border-left: 4px solid #10b981;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div style="font-size:11px;font-weight:bold;color:#34d399;">🗺️ DIBUJAR Y NAVEGAR RUTA</div>
          <span style="font-size:11px; color:#94a3b8;" id="wp-counter">0 WPs</span>
        </div>
        <div style="font-size:11.5px; color:#cbd5e1; margin-top:4px;">
          Activa "Trazar Puntos" y haz clic en el mapa. Arrastra un waypoint para moverlo; clic derecho (o toque, en móvil) para borrarlo.
        </div>
        <div class="btn-group">
          <button class="btn" id="btn-draw-mode" onclick="toggleDrawMode()">✏️ Trazar Puntos</button>
          <button class="btn btn-green" id="btn-play-pause" onclick="toggleNavigation()">▶ Iniciar Ruta</button>
          <button class="btn btn-amber" onclick="undoWaypoint()">↶ Deshacer</button>
          <button class="btn btn-red" onclick="clearRoute()">🗑 Limpiar</button>
        </div>
        <div style="display:flex; gap:12px; margin-top:8px; align-items:center; font-size:11px;">
          <label style="display:flex; align-items:center; gap:4px; cursor:pointer;">
            <input type="checkbox" id="chk-loop" checked onchange="syncRouteToServer()"> Bucle infinito
          </label>
          <span id="nav-next-info" style="color:#38bdf8; font-family:monospace;">WP objetivo: -</span>
        </div>
      </div>

      <!-- VELOCIDAD Y RUMBO -->
      <div class="card" style="border-left: 4px solid #f59e0b;">
        <div style="font-size:11px;font-weight:bold;color:#fbbf24;margin-bottom:6px;">⚡ CINEMÁTICA / VELOCIDAD</div>
        <div style="display:flex; gap:8px;">
          <div style="flex:1;">
            <label style="font-size:10px;color:#94a3b8;">Velocidad (Nudos):</label>
            <input type="number" id="in-speed" value="12.0" min="0" max="60" step="0.5" style="width:100%;">
          </div>
          <div style="flex:1;">
            <label style="font-size:10px;color:#94a3b8;">Rumbo (°):</label>
            <input type="number" id="in-heading" value="0" min="0" max="360" step="1" style="width:100%;">
          </div>
        </div>
        <label style="display:flex; align-items:center; gap:4px; font-size:11px; margin-top:7px; cursor:pointer;">
          <input type="checkbox" id="chk-manual"> Avanzar en modo manual (a estima con rumbo y velocidad)
        </label>
        <div style="font-size:10px;color:#94a3b8;margin-top:3px;">Si el barco no avanza (sin ruta ni avance manual) se emite SOG = 0.</div>
      </div>

      <!-- PUERTO TCP NMEA -->
      <div class="card" style="border-left: 4px solid #38bdf8;">
        <div style="font-size:11px;font-weight:bold;color:#94a3b8;margin-bottom:4px;">⚙️ PUERTO TCP NMEA (ELEGIBLE)</div>
        <div style="display:flex; gap:6px; align-items:center;">
          <input type="number" id="in-tcp-port" value="10111" min="1" max="65535" style="width:110px;">
          <button class="btn" onclick="applyTcpPort()">Aplicar</button>
          <span style="font-size:11px; color:#94a3b8;">IP: <strong id="host-ip">127.0.0.1</strong></span>
        </div>
        <div id="port-status" style="font-size:10.5px; color:#38bdf8; margin-top:4px;">Puerto activo: 10111</div>
      </div>

      <!-- PROFUNDIDAD -->
      <div class="card" style="border-left:4px solid #a78bfa;">
        <div style="font-size:11px;font-weight:bold;color:#c4b5fd;margin-bottom:6px;">🌊 PROFUNDIDAD SIMULADA</div>
        <label style="font-size:10px;color:#94a3b8;">Profundidad (metros):</label>
        <div style="display:flex;gap:6px;margin-top:4px;">
          <input type="number" id="in-depth" value="20" min="0" max="12000" step="0.1" style="width:110px;">
          <button class="btn" onclick="sendDepth()">Aplicar</button>
        </div>
        <div style="font-size:10px;color:#94a3b8;margin-top:4px;">Emite SDDPT y SDDBT (metros, pies y brazas).</div>
      </div>

      <!-- BLANCOS AIS / ARPA -->
      <div class="card" style="border-left:4px solid #fb7185;">
        <div style="font-size:11px;font-weight:bold;color:#fda4af;margin-bottom:6px;">🎯 BLANCOS AIS / ARPA</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:5px;">
          <label style="font-size:10px;color:#94a3b8;">Latitud<input id="t-lat" type="number" step="0.0001" value="43.6900" style="width:100%;"></label>
          <label style="font-size:10px;color:#94a3b8;">Longitud<input id="t-lon" type="number" step="0.0001" value="-7.5800" style="width:100%;"></label>
          <label style="font-size:10px;color:#94a3b8;">MMSI<input id="t-mmsi" type="number" min="100000000" max="999999999" value="224123456" style="width:100%;"></label>
          <label style="font-size:10px;color:#94a3b8;">Nombre<input id="t-name" type="text" maxlength="20" value="BLANCO1" style="width:100%;"></label>
          <label style="font-size:10px;color:#94a3b8;">Velocidad (kn)<input id="t-speed" type="number" min="0" max="102" step="0.1" value="8" style="width:100%;"></label>
          <label style="font-size:10px;color:#94a3b8;">Rumbo (°)<input id="t-heading" type="number" min="0" max="359" step="1" value="90" style="width:100%;"></label>
        </div>
        <div style="display:flex;gap:12px;margin-top:7px;font-size:11px;">
          <label><input type="checkbox" id="t-arpa" checked> ARPA</label>
          <label><input type="checkbox" id="t-ais" checked> AIS</label>
        </div>
        <div class="btn-group">
          <button class="btn btn-amber" id="btn-pick" onclick="togglePick()">📍 Elegir en mapa</button>
          <button class="btn" onclick="addTarget()">＋ Añadir blanco</button>
          <button class="btn btn-red" onclick="clearTargets()">Eliminar todos</button>
        </div>
        <div style="font-size:10px;color:#cbd5e1;margin-top:6px;">
          <div id="target-empty">Sin blancos</div>
          <div id="target-list"></div>
        </div>
      </div>

      <!-- POSICIÓN ACTUAL -->
      <div class="card">
        <div style="font-size:10px;font-weight:bold;color:#94a3b8;margin-bottom:4px;">📍 POSICIÓN GPS TRANSMITIDA</div>
        <div class="coords" id="pos">43.6833° N, 7.5950° W</div>
        <div id="pos-extra" style="font-size:11px;color:#94a3b8;margin-top:3px;">SOG – · COG –</div>
      </div>

      <!-- MONITOR NMEA -->
      <div class="card monitor">
        <div style="font-size:10px;font-weight:bold;color:#94a3b8;margin-bottom:4px;">MONITOR NMEA (última emisión)</div>
        <div class="feed" id="feed">Iniciando monitor...</div>
      </div>

    </div>
  </div>

  <script>
    if (typeof L === 'undefined') {
      document.getElementById('map').innerHTML = '<div style="padding:24px;color:#fbbf24">No se pudo cargar el mapa (Leaflet y OpenStreetMap necesitan Internet). Recarga la página con conexión. El servidor NMEA sigue emitiendo.</div>';
      throw new Error('Leaflet no disponible');
    }

    const $ = id => document.getElementById(id);
    const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const postJSON = (url, data) => fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});

    let lat = 43.6833, lon = -7.5950;
    let isDrawingMode = false, isNavigating = false, pickTargetMode = false, draggingBoat = false;
    let lastNavChange = 0;
    let justDragged = false;
    let routeWaypoints = [];
    let wpMarkers = [];
    const targetMarkers = new Map();
    const targetRows = new Map();

    const map = L.map('map').setView([lat, lon], 12);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map);

    const routePolyline = L.polyline([], {color: '#10b981', weight: 3, dashArray: '5, 8'}).addTo(map);

    const boatIcon = L.divIcon({
      className: 'boat-marker',
      html: `<div id="boat-svg" style="transform: rotate(0deg); transition: transform 0.25s linear;">
               <svg width="34" height="34" viewBox="0 0 24 24" fill="#38bdf8" stroke="#0b111e" stroke-width="1.6">
                 <path d="M12 2L4 20l8-3 8 3z"/>
               </svg>
             </div>`,
      iconSize: [34, 34],
      iconAnchor: [17, 17]
    });
    const boatMarker = L.marker([lat, lon], { icon: boatIcon, draggable: true }).addTo(map);

    // ---------- Vista móvil / escritorio ----------
    function toggleMapSize() {
      document.documentElement.classList.toggle('map-big');
      setTimeout(() => map.invalidateSize(), 60);
    }
    window.onViewChange = () => {
      rebuildWaypoints();
      setTimeout(() => map.invalidateSize(), 60);
    };
    window.addEventListener('orientationchange', () => setTimeout(() => map.invalidateSize(), 300));

    // ---------- Estado visual ----------
    function applyNavUI() {
      const btn = $('btn-play-pause'), ind = $('route-indicator');
      btn.innerText = isNavigating ? '⏸ Pausar Ruta' : '▶ Iniciar Ruta';
      btn.classList.toggle('btn-red', isNavigating);
      btn.classList.toggle('btn-green', !isNavigating);
      if (isDrawingMode) { ind.innerText = '✏️ Trazando ruta...'; ind.style.color = '#fbbf24'; }
      else if (isNavigating) { ind.innerText = '🚢 Navegando Ruta'; ind.style.color = '#34d399'; }
      else { ind.innerText = '⚓ Modo Manual'; ind.style.color = '#94a3b8'; }
    }

    function setConn(ok) {
      const el = $('conn');
      el.innerText = ok ? '● en línea' : '● sin conexión';
      el.style.color = ok ? '#34d399' : '#f87171';
    }

    // ---------- Ruta ----------
    function toggleDrawMode() {
      isDrawingMode = !isDrawingMode;
      if (isDrawingMode && pickTargetMode) togglePick();
      const btn = $('btn-draw-mode');
      btn.classList.toggle('btn-amber', isDrawingMode);
      btn.innerText = isDrawingMode ? '🛑 Detener Trazado' : '✏️ Trazar Puntos';
      applyNavUI();
    }

    function rebuildWaypoints() {
      wpMarkers.forEach(m => map.removeLayer(m));
      wpMarkers = [];
      routeWaypoints.forEach((p, i) => {
        const sz = isMobileView() ? 30 : 18;   // más grandes para poder arrastrarlos con el dedo
        const icon = L.divIcon({
          className: 'wp-icon',
          html: `<div style="background:#10b981; color:#0b111e; font-weight:bold; font-size:${isMobileView() ? 13 : 10}px; border-radius:50%; width:${sz}px; height:${sz}px; display:flex; align-items:center; justify-content:center; border:2px solid #fff; box-shadow:0 0 4px rgba(0,0,0,0.5);">${i + 1}</div>`,
          iconSize: [sz, sz],
          iconAnchor: [sz / 2, sz / 2]
        });
        const m = L.marker(p, { icon: icon, draggable: true }).addTo(map);
        m.on('drag', () => { const ll = m.getLatLng(); routeWaypoints[i] = [ll.lat, ll.lng]; routePolyline.setLatLngs(routeWaypoints); });
        m.on('dragend', () => { justDragged = true; setTimeout(() => { justDragged = false; }, 400); syncRouteToServer(); });
        m.on('click', () => {   // en móvil no hay clic derecho: tocar un waypoint pregunta si borrarlo
          if (!isMobileView() || justDragged || isDrawingMode) return;
          if (confirm(`¿Borrar el waypoint ${i + 1}?`)) { routeWaypoints.splice(i, 1); afterRouteEdit(); }
        });
        m.on('contextmenu', () => { routeWaypoints.splice(i, 1); afterRouteEdit(); });
        wpMarkers.push(m);
      });
      routePolyline.setLatLngs(routeWaypoints);
      $('wp-counter').innerText = `${routeWaypoints.length} WPs`;
    }

    function afterRouteEdit() {
      rebuildWaypoints();
      if (isNavigating && routeWaypoints.length < 2) {
        isNavigating = false;
        lastNavChange = Date.now();
        applyNavUI();
      }
      syncRouteToServer();
    }

    function addWaypoint(wLat, wLon) {
      routeWaypoints.push([wLat, wLon]);
      rebuildWaypoints();
      if (routeWaypoints.length === 1 && !isNavigating) setBoatPos(wLat, wLon);
      syncRouteToServer();
    }

    function undoWaypoint() {
      if (routeWaypoints.length) { routeWaypoints.pop(); afterRouteEdit(); }
    }

    function clearRoute() {
      routeWaypoints = [];
      isNavigating = false;
      lastNavChange = Date.now();
      rebuildWaypoints();
      applyNavUI();
      $('nav-next-info').innerText = 'WP objetivo: -';
      syncRouteToServer();
    }

    function toggleNavigation() {
      if (!isNavigating && routeWaypoints.length < 2) {
        alert('Traza al menos 2 waypoints en el mapa haciendo clics para iniciar una ruta.');
        return;
      }
      isNavigating = !isNavigating;
      lastNavChange = Date.now();
      if (isDrawingMode) toggleDrawMode();
      applyNavUI();
      syncRouteToServer();
    }

    function syncRouteToServer() {
      postJSON('/set_route', {
        waypoints: routeWaypoints,
        is_navigating: isNavigating,
        loop_route: $('chk-loop').checked
      }).catch(() => {});
    }

    // ---------- Barco ----------
    function setBoatPos(nLat, nLon) {
      lat = nLat; lon = nLon;
      boatMarker.setLatLng([lat, lon]);
      updateCoordsDisplay(lat, lon);
      postJSON('/set_pos', {lat: lat, lon: lon}).catch(() => {});
    }

    function updateCoordsDisplay(cLat, cLon) {
      const latH = cLat >= 0 ? 'N' : 'S';
      const lonH = cLon >= 0 ? 'E' : 'W';
      $('pos').innerText = `${Math.abs(cLat).toFixed(4)}° ${latH}, ${Math.abs(cLon).toFixed(4)}° ${lonH}`;
    }

    boatMarker.on('dragstart', () => { draggingBoat = true; });
    boatMarker.on('dragend', () => {
      draggingBoat = false;
      if (!isNavigating) { const p = boatMarker.getLatLng(); setBoatPos(p.lat, p.lng); }
    });

    $('in-speed').addEventListener('input', e => {
      const v = parseFloat(e.target.value);
      if (Number.isFinite(v) && v >= 0) postJSON('/set_pos', {speed: v}).catch(() => {});
    });
    $('in-heading').addEventListener('input', e => {
      const v = parseFloat(e.target.value);
      if (!Number.isFinite(v)) return;
      const svg = $('boat-svg');
      if (svg) svg.style.transform = `rotate(${v}deg)`;
      postJSON('/set_pos', {heading: ((v % 360) + 360) % 360}).catch(() => {});
    });
    $('chk-manual').addEventListener('change', e => {
      postJSON('/set_pos', {manual_drive: e.target.checked}).catch(() => {});
    });

    // ---------- Clics en el mapa ----------
    map.on('click', e => {
      if (pickTargetMode) {
        $('t-lat').value = e.latlng.lat.toFixed(5);
        $('t-lon').value = e.latlng.lng.toFixed(5);
        togglePick();
      } else if (isDrawingMode) {
        addWaypoint(e.latlng.lat, e.latlng.lng);
      } else if (!isNavigating) {
        setBoatPos(e.latlng.lat, e.latlng.lng);
      }
    });

    // ---------- Puerto TCP y profundidad ----------
    function applyTcpPort() {
      const portVal = parseInt($('in-tcp-port').value);
      if (!portVal || portVal < 1 || portVal > 65535) {
        alert('Puerto inválido. Ingresa un número entre 1 y 65535.');
        return;
      }
      const stEl = $('port-status');
      stEl.innerText = 'Cambiando a puerto ' + portVal + '...';
      stEl.style.color = '#fbbf24';
      postJSON('/set_port', {port: portVal})
        .then(r => r.json())
        .then(res => {
          if (res.status === 'ok') {
            stEl.innerText = 'Puerto activo: ' + res.port;
            stEl.style.color = '#38bdf8';
          } else {
            stEl.innerText = res.message + ' (el puerto anterior sigue activo)';
            stEl.style.color = '#f87171';
          }
        })
        .catch(() => { stEl.innerText = 'Sin respuesta del servidor'; stEl.style.color = '#f87171'; });
    }

    async function sendDepth() {
      const depth = parseFloat($('in-depth').value);
      if (!Number.isFinite(depth) || depth < 0 || depth > 12000) { alert('Profundidad válida: 0–12000 m'); return; }
      const r = await postJSON('/set_depth', {depth});
      if (!r.ok) alert('No se pudo actualizar la profundidad');
    }

    // ---------- Blancos AIS / ARPA ----------
    function togglePick() {
      pickTargetMode = !pickTargetMode;
      if (pickTargetMode && isDrawingMode) toggleDrawMode();
      const b = $('btn-pick');
      b.innerText = pickTargetMode ? '🛑 Haz clic en el mapa…' : '📍 Elegir en mapa';
      b.classList.toggle('btn-red', pickTargetMode);
      b.classList.toggle('btn-amber', !pickTargetMode);
    }

    function targetIcon(t) {
      return L.divIcon({
        className: '',
        html: `<div style="transform: rotate(${t.heading}deg)"><svg width="22" height="22" viewBox="0 0 24 24" fill="#fb7185" stroke="#0b111e" stroke-width="1.6"><path d="M12 2L5 21l7-3 7 3z"/></svg></div>`,
        iconSize: [22, 22],
        iconAnchor: [11, 11]
      });
    }

    function renderTargets(list) {
      const seen = new Set();
      const box = $('target-list');
      list.forEach(t => {
        seen.add(t.id);
        let m = targetMarkers.get(t.id);
        if (!m) {
          m = L.marker([t.lat, t.lon], { icon: targetIcon(t), interactive: false, keyboard: false }).addTo(map);
          m.bindTooltip(esc(t.name), { permanent: true, direction: 'right', offset: [10, 0], className: 'tgt-tip' });
          targetMarkers.set(t.id, m);
        } else {
          m.setLatLng([t.lat, t.lon]);
          m.setIcon(targetIcon(t));
        }
        let row = targetRows.get(t.id);
        if (!row) {
          row = document.createElement('div');
          row.className = 'tgt-row';
          row.innerHTML = '<span class="info"></span><button class="btn btn-red" style="padding:1px 7px">✕</button>';
          row.querySelector('button').addEventListener('click', () => deleteTarget(t.id));
          box.appendChild(row);
          targetRows.set(t.id, row);
        }
        const tc = t.tcpa_min === null ? '–' : t.tcpa_min.toFixed(1);
        row.querySelector('.info').innerHTML =
          `${esc(t.name)} · ${t.mmsi} · ${t.speed.toFixed(1)} kn<br>` +
          `<span style="color:#94a3b8">${t.distance_nm.toFixed(2)} nm · CPA ${t.cpa_nm.toFixed(2)} nm · TCPA ${tc} min</span>`;
      });
      for (const [id, m] of targetMarkers) if (!seen.has(id)) { map.removeLayer(m); targetMarkers.delete(id); }
      for (const [id, row] of targetRows) if (!seen.has(id)) { row.remove(); targetRows.delete(id); }
      $('target-empty').style.display = list.length ? 'none' : 'block';
    }

    async function addTarget() {
      const data = {
        lat: parseFloat($('t-lat').value), lon: parseFloat($('t-lon').value),
        mmsi: parseInt($('t-mmsi').value), name: $('t-name').value || 'BLANCO',
        speed: parseFloat($('t-speed').value), heading: parseFloat($('t-heading').value),
        arpa: $('t-arpa').checked, ais: $('t-ais').checked
      };
      const r = await postJSON('/targets', data);
      const d = await r.json();
      if (!r.ok) { alert(d.message || 'Error al añadir blanco'); return; }
      $('t-mmsi').value = Math.min(999999999, data.mmsi + 1);
      pollStatus();
    }

    async function deleteTarget(id) {
      await postJSON('/targets/delete', {id});
      pollStatus();
    }

    async function clearTargets() {
      const r = await postJSON('/targets/clear', {});
      if (r.ok) pollStatus();
    }

    // ---------- Sincronización con el servidor ----------
    function pollStatus() {
      return fetch('/status')
        .then(r => r.json())
        .then(d => {
          setConn(true);
          $('clients').innerText = d.connected_clients;
          $('sent').innerText = d.sentences_sent;
          $('header-port-badge').innerText = 'TCP: ' + d.tcp_port;

          if (!draggingBoat && d.lat !== undefined && d.lon !== undefined) {
            lat = d.lat; lon = d.lon;
            boatMarker.setLatLng([lat, lon]);
            updateCoordsDisplay(lat, lon);
          }
          if (d.heading !== undefined) {
            const svg = $('boat-svg');
            if (svg) svg.style.transform = `rotate(${d.heading}deg)`;
            if (d.route_info && d.route_info.is_navigating && document.activeElement !== $('in-heading')) {
              $('in-heading').value = Math.round(d.heading);
            }
            $('pos-extra').innerText = `SOG ${d.sog_knots.toFixed(1)} kn · COG ${Math.round(d.heading)}°`;
          }

          const ri = d.route_info;
          if (ri) {
            // El servidor manda: p. ej. al terminar una ruta sin bucle, la UI vuelve a "Iniciar".
            if (Date.now() - lastNavChange > 2500 && ri.is_navigating !== isNavigating) {
              isNavigating = ri.is_navigating;
              applyNavUI();
            }
            $('nav-next-info').innerText = ri.is_navigating
              ? `WP objetivo: ${ri.current_wp_idx + 1}/${ri.total_wps}`
              : (ri.total_wps > 0 ? 'Ruta detenida' : 'WP objetivo: -');
          }

          renderTargets(d.targets || []);
          if (d.latest_sentences && d.latest_sentences.length) {
            $('feed').textContent = d.latest_sentences.join('\n');
          }
        })
        .catch(() => setConn(false));
    }

    // Al abrir/recargar la página se restaura el estado real del servidor (ruta, velocidad, etc.).
    async function initFromServer() {
      $('host-ip').innerText = location.hostname || '127.0.0.1';
      try {
        const d = await (await fetch('/status')).json();
        $('in-speed').value = d.speed_knots;
        $('in-heading').value = Math.round(d.heading);
        $('in-depth').value = d.depth_m;
        $('in-tcp-port').value = d.tcp_port;
        $('port-status').innerText = 'Puerto activo: ' + d.tcp_port;
        $('chk-manual').checked = !!d.manual_drive;
        if (d.route_info) {
          $('chk-loop').checked = !!d.route_info.loop_route;
          routeWaypoints = d.route_info.waypoints || [];
          isNavigating = !!d.route_info.is_navigating;
          rebuildWaypoints();
          applyNavUI();
        }
        lat = d.lat; lon = d.lon;
        if (routeWaypoints.length > 1) map.fitBounds(routePolyline.getBounds().pad(0.2));
        else map.setView([lat, lon], 12);
      } catch (e) { setConn(false); }
      setInterval(pollStatus, 1000);
      pollStatus();
    }
    initFromServer();
  </script>
  <script>applyView();</script>
</body>
</html>
"""


class FastHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send(self, code, payload, content_type="application/json; charset=utf-8", cors=False):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/status":
            self._send(200, snapshot_status(), cors=True)
        elif path == "/targets":
            with state_lock:
                own = (gps_state["lat"], gps_state["lon"], gps_state["sog_knots"], gps_state["heading"])
            self._send(200, {"targets": targets_with_metrics(own)})
        else:
            self._send(404, {"status": "error", "message": "No encontrado"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(413, {"status": "error", "message": "Cuerpo demasiado grande o inválido"})
            return
        raw = self.rfile.read(length) if length else b""

        handler = POST_ROUTES.get(urllib.parse.urlparse(self.path).path)
        if handler is None:
            self._send(404, {"status": "error", "message": "No encontrado"})
            return
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            if not isinstance(body, dict):
                raise ValueError("El cuerpo debe ser un objeto JSON")
            result = handler(body)
        except KeyError as e:
            self._send(400, {"status": "error", "message": f"Falta el campo {e}"})
        except (ValueError, TypeError, UnicodeDecodeError) as e:     # incluye json.JSONDecodeError
            self._send(400, {"status": "error", "message": str(e)})
        except Exception as e:   # noqa: BLE001
            print(f"[!] [HTTP] {type(e).__name__}: {e}")
            self._send(500, {"status": "error", "message": "Error interno"})
        else:
            self._send(200, result)


_httpd = None


def start_background(tcp_port=DEFAULT_TCP_PORT, web_port=DEFAULT_WEB_PORT, web_host="127.0.0.1"):
    """Arranque NO bloqueante (lo usa la app Android). Es idempotente: una segunda llamada no hace nada."""
    global _httpd
    if _httpd is not None:
        return "ya iniciado"
    start_tcp_listener(int(tcp_port))
    threading.Thread(target=accept_worker, daemon=True).start()
    threading.Thread(target=navigation_and_broadcast_worker, daemon=True).start()
    _httpd = ThreadedHTTPServer((web_host, int(web_port)), FastHandler)
    threading.Thread(target=_httpd.serve_forever, daemon=True).start()
    return f"web http://{web_host}:{web_port} | tcp {tcp_port}"


def main():
    parser = argparse.ArgumentParser(description="Simulador NMEA 0183: GPS, profundidad, AIS y ARPA")
    parser.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT)
    parser.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT)
    parser.add_argument("--web-host", default="0.0.0.0",
                        help="interfaz de la web (usa 127.0.0.1 para no exponer los controles en la red)")
    parser.add_argument("--open", action="store_true",
                        help="abrir la web en el navegador al arrancar (siempre activo en el .exe)")
    args = parser.parse_args()
    frozen = bool(getattr(sys, "frozen", False))    # True dentro del .exe de PyInstaller

    print("=" * 65)
    print("  SIMULADOR NMEA 0183 - GPS, PROFUNDIDAD, AIS Y ARPA")
    print(f"  [+] Puerto TCP NMEA (cartas/navegadores): {args.tcp_port}")
    print(f"  [+] Interfaz web:                        http://localhost:{args.web_port}")
    print("=" * 65)

    ok, msg = start_tcp_listener(args.tcp_port)
    if not ok:
        print("[!] El simulador arranca sin servidor TCP; elige otro puerto desde la web.")
    threading.Thread(target=accept_worker, daemon=True).start()
    threading.Thread(target=navigation_and_broadcast_worker, daemon=True).start()

    httpd = None
    try:
        httpd = ThreadedHTTPServer((args.web_host, args.web_port), FastHandler)
        print(f"[*] Servidor web disponible en http://localhost:{args.web_port}")
        if args.open or frozen:
            threading.Timer(1.0, webbrowser.open, (f"http://localhost:{args.web_port}/",)).start()
        httpd.serve_forever()
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, 10048):
            print(f"[!] El puerto web {args.web_port} ya está en uso. Prueba con --web-port <otro puerto>.")
        else:
            print(f"[!] Error servidor HTTP: {e}")
        if frozen:   # con doble clic la ventana se cerraría antes de poder leer el error
            input("Pulsa Enter para cerrar...")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nServidor apagado.")
    finally:
        stop_event.set()
        if httpd:
            httpd.server_close()
        with tcp_clients_lock:
            clients = list(tcp_clients)
        for c in clients:
            c.close()
        if tcp_server_sock:
            try:
                tcp_server_sock.close()
            except OSError:
                pass


if __name__ == "__main__":
    main()
