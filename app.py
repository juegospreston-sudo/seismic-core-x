from flask import Flask, request, jsonify, render_template_string
import math
import time
from collections import deque

app = Flask(__name__)


UMBRAL_VIBRACION = 0.040
UMBRAL_TEMBLOR = 0.060
UMBRAL_ALERTA = 0.120

UMBRAL_ESTABLE = 0.020
TIEMPO_ESTABLE_ALERTA = 5.0
TIEMPO_FIN_EVENTO = 2.0

MUESTRAS_CALIBRACION = 100
MUESTRAS_FILTRO = 5

base_x = 0.0
base_y = 0.0
base_z = 0.0

calibrando = True
muestras_calibracion = []

filtro = deque(maxlen=MUESTRAS_FILTRO)

ultima_magnitud = 0.0
magnitud_filtrada = 0.0

estado_actual = "SISTEMA ESTABLE"
nivel_actual = 1

ultimo_movimiento = 0.0
inicio_estabilidad = None

alerta_declarada = False

ultimo_x = 0.0
ultimo_y = 0.0
ultimo_z = 0.0

evento_activo = False
inicio_evento = None


def procesar_calibracion(x, y, z):

    global base_x
    global base_y
    global base_z
    global calibrando
    global muestras_calibracion

    muestras_calibracion.append((x, y, z))

    if len(muestras_calibracion) >= MUESTRAS_CALIBRACION:

        base_x = sum(p[0] for p in muestras_calibracion) / len(muestras_calibracion)
        base_y = sum(p[1] for p in muestras_calibracion) / len(muestras_calibracion)
        base_z = sum(p[2] for p in muestras_calibracion) / len(muestras_calibracion)

        calibrando = False

        print("Calibracion terminada")
        print("Base X:", base_x)
        print("Base Y:", base_y)
        print("Base Z:", base_z)


def procesar_sensor(x, y, z):

    global ultima_magnitud
    global magnitud_filtrada
    global estado_actual
    global nivel_actual
    global ultimo_movimiento
    global inicio_estabilidad
    global alerta_declarada
    global ultimo_x
    global ultimo_y
    global ultimo_z
    global evento_activo
    global inicio_evento

    ultimo_x = x
    ultimo_y = y
    ultimo_z = z

    if calibrando:
        procesar_calibracion(x, y, z)
        return

    x_corregido = x - base_x
    y_corregido = y - base_y
    z_corregido = z - base_z

    magnitud = math.sqrt(
        x_corregido * x_corregido +
        y_corregido * y_corregido +
        z_corregido * z_corregido
    )

    ultima_magnitud = magnitud

    filtro.append(magnitud)

    if len(filtro) > 0:
        magnitud_filtrada = sum(filtro) / len(filtro)

    ahora = time.time()

    if magnitud_filtrada >= UMBRAL_ALERTA:

        alerta_declarada = True
        estado_actual = "ALERTA CRITICA"
        nivel_actual = 4
        evento_activo = True

        if inicio_evento is None:
            inicio_evento = ahora

        ultimo_movimiento = ahora
        inicio_estabilidad = None

        return

    if alerta_declarada:

        if magnitud_filtrada <= UMBRAL_ESTABLE:

            if inicio_estabilidad is None:
                inicio_estabilidad = ahora

            elif ahora - inicio_estabilidad >= TIEMPO_ESTABLE_ALERTA:

                alerta_declarada = False
                estado_actual = "SISTEMA ESTABLE"
                nivel_actual = 1

                evento_activo = False
                inicio_evento = None
                ultimo_movimiento = 0.0
                inicio_estabilidad = None

        else:

            inicio_estabilidad = None
            ultimo_movimiento = ahora
            evento_activo = True

        return

    if magnitud_filtrada >= UMBRAL_TEMBLOR:

        estado_actual = "SISMO EN CURSO"
        nivel_actual = 3
        evento_activo = True

        if inicio_evento is None:
            inicio_evento = ahora

        ultimo_movimiento = ahora
        inicio_estabilidad = None

        return

    if magnitud_filtrada >= UMBRAL_VIBRACION:

        estado_actual = "MOVIMIENTO DETECTADO"
        nivel_actual = 2
        evento_activo = True

        if inicio_evento is None:
            inicio_evento = ahora

        ultimo_movimiento = ahora
        inicio_estabilidad = None

        return

    if evento_activo:

        if ultimo_movimiento > 0:

            if ahora - ultimo_movimiento >= TIEMPO_FIN_EVENTO:

                estado_actual = "SISTEMA ESTABLE"
                nivel_actual = 1

                evento_activo = False
                inicio_evento = None
                ultimo_movimiento = 0.0

        return

    estado_actual = "SISTEMA ESTABLE"
    nivel_actual = 1


def calcular_direccion():

    if not evento_activo:
        return None

    x = ultimo_x - base_x
    y = ultimo_y - base_y

    fuerza_horizontal = math.sqrt(
        x * x +
        y * y
    )

    if fuerza_horizontal < 0.0005:
        return None

    angulo = math.atan2(x, y)

    grados = math.degrees(angulo)

    if grados < 0:
        grados += 360

    valor = magnitud_filtrada

    distancia = (valor / UMBRAL_ALERTA) * 0.82

    if distancia < 0.10:
        distancia = 0.10

    if distancia > 0.82:
        distancia = 0.82

    return {
        "angulo": grados,
        "distancia": distancia
    }


@app.route("/datos", methods=["POST"])
def recibir_datos():

    try:

        datos = request.get_json(force=True)

        x = float(datos.get("x", 0))
        y = float(datos.get("y", 0))
        z = float(datos.get("z", 0))

        procesar_sensor(x, y, z)

        return jsonify({
            "ok": True
        })

    except Exception as e:

        print("Error recibiendo datos:", e)

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 400


@app.route("/estado")
def obtener_estado():

    direccion = calcular_direccion()

    return jsonify({

        "estado": estado_actual,

        "nivel": nivel_actual,

        "magnitud": round(ultima_magnitud, 5),

        "filtrada": round(magnitud_filtrada, 5),

        "calibrando": calibrando,

        "evento": evento_activo,

        "direccion": direccion,

        "timestamp": time.time()

    })


HTML = r"""
<!DOCTYPE html>

<html lang="es">

<head>

<meta charset="UTF-8">

<meta name="viewport" content="width=device-width, initial-scale=1.0">

<title>SEISMIC CORE X</title>

<style>
* { box-sizing: border-box; }

:root {
    --fondo: #050914;
    --panel: rgba(13, 24, 42, .92);
    --borde: rgba(0, 220, 255, .25);
    --cian: #00e5ff;
    --texto: #e1edf8;
    --suave: #829ab4;
    --verde: #00ff88;
    --amarillo: #ffe600;
    --naranja: #ff7a00;
    --rojo: #ff183d;
}

html, body {
    margin: 0;
    padding: 0;
    width: 100%;
    height: 100%;
    overflow: hidden;
    background:
        radial-gradient(ellipse at 45% 42%, #10213a 0%, #080f1d 48%, #03060d 100%);
    color: var(--texto);
    font-family: "Segoe UI", Arial, sans-serif;
}

body { display: flex; flex-direction: column; }

.header {
    min-height: 72px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 0 28px;
    border-bottom: 1px solid var(--borde);
    background: rgba(4, 10, 21, .88);
    box-shadow: 0 4px 24px rgba(0, 0, 0, .25);
}

.title {
    font-size: 27px;
    font-weight: 800;
    letter-spacing: 3px;
    color: #f0f8ff;
    text-shadow: 0 0 18px rgba(0, 229, 255, .24);
}

.connection {
    font-size: 12px;
    letter-spacing: 1.5px;
    font-weight: 700;
}

.main {
    flex: 1;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr) 340px;
    gap: 18px;
    padding: 18px;
}

.radar-area {
    min-width: 0;
    min-height: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
    border: 1px solid rgba(0, 229, 255, .13);
    border-radius: 22px;
    background: linear-gradient(145deg, rgba(11, 24, 43, .52), rgba(4, 10, 20, .35));
    box-shadow: inset 0 0 50px rgba(0, 0, 0, .18);
}

.radar {
    position: relative;
    width: min(calc(100vh - 175px), calc(100vw - 430px));
    height: min(calc(100vh - 175px), calc(100vw - 430px));
    min-width: 280px;
    min-height: 280px;
    max-width: 760px;
    max-height: 760px;
    border: 1px solid rgba(0, 229, 255, .62);
    border-radius: 50%;
    background:
        radial-gradient(circle, rgba(0, 229, 255, .09) 0%, rgba(0, 229, 255, .025) 48%, rgba(0, 0, 0, .1) 75%);
    box-shadow: 0 0 24px rgba(0, 229, 255, .10), inset 0 0 32px rgba(0, 229, 255, .07);
    overflow: hidden;
}

.radar::before, .radar::after {
    content: "";
    position: absolute;
    pointer-events: none;
    background: rgba(0, 229, 255, .23);
}

.radar::before {
    left: 50%; top: 0; width: 1px; height: 100%;
}

.radar::after {
    left: 0; top: 50%; width: 100%; height: 1px;
}

.circle1, .circle2, .circle3 {
    position: absolute;
    left: 50%; top: 50%;
    transform: translate(-50%, -50%);
    border: 1px solid rgba(0, 229, 255, .24);
    border-radius: 50%;
    pointer-events: none;
}

.circle1 { width: 25%; height: 25%; }
.circle2 { width: 50%; height: 50%; }
.circle3 { width: 75%; height: 75%; }

.center {
    position: absolute;
    left: 50%; top: 50%;
    width: 7px; height: 7px;
    transform: translate(-50%, -50%);
    border-radius: 50%;
    background: var(--cian);
    box-shadow: 0 0 10px var(--cian), 0 0 22px rgba(0, 229, 255, .6);
}

.sweep {
    position: absolute;
    left: 50%; top: 50%;
    width: 50%; height: 2px;
    transform-origin: left center;
    background: linear-gradient(90deg, rgba(0, 229, 255, .95), rgba(0, 229, 255, 0));
    box-shadow: 0 0 10px rgba(0, 229, 255, .8);
    pointer-events: none;
}

.cardinal {
    position: absolute;
    font-size: 13px;
    font-weight: 800;
    color: #9bd8e7;
    text-shadow: 0 0 9px rgba(0, 229, 255, .5);
}

.norte { top: 12px; left: 50%; transform: translateX(-50%); }
.sur { bottom: 12px; left: 50%; transform: translateX(-50%); }
.este { right: 13px; top: 50%; transform: translateY(-50%); }
.oeste { left: 13px; top: 50%; transform: translateY(-50%); }
.ne { top: 16%; right: 16%; }
.se { bottom: 16%; right: 16%; }
.so { bottom: 16%; left: 16%; }
.no { top: 16%; left: 16%; }

.event-point {
    position: absolute;
    width: 18px; height: 18px;
    margin-left: -9px; margin-top: -9px;
    border-radius: 50%;
    pointer-events: none;
    opacity: 0;
    transform: scale(1);
    transition: left .18s linear, top .18s linear, opacity .4s ease, transform .15s ease;
    z-index: 20;
}

.event-point.visible { opacity: 1; }

.event-point.yellow {
    background: var(--amarillo);
    box-shadow: 0 0 8px var(--amarillo), 0 0 22px rgba(255, 230, 0, .75);
}

.event-point.orange {
    background: var(--naranja);
    box-shadow: 0 0 9px var(--naranja), 0 0 24px rgba(255, 122, 0, .8);
}

.event-point.red {
    background: var(--rojo);
    box-shadow: 0 0 10px var(--rojo), 0 0 28px rgba(255, 24, 61, .9);
}

.event-point.blink { animation: puntoParpadeo .65s infinite; }

@keyframes puntoParpadeo {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: .18; transform: scale(.62); }
}

.panel {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 14px;
}

.box {
    border: 1px solid var(--borde);
    border-radius: 15px;
    padding: 18px;
    background: linear-gradient(145deg, rgba(17, 31, 51, .92), rgba(7, 15, 28, .94));
    box-shadow: 0 8px 24px rgba(0, 0, 0, .18), inset 0 1px rgba(255, 255, 255, .025);
}

.label {
    font-size: 11px;
    letter-spacing: 2px;
    color: var(--suave);
    margin-bottom: 9px;
    font-weight: 700;
}

.value {
    font-size: 24px;
    font-weight: 800;
    letter-spacing: 1px;
    overflow-wrap: anywhere;
}

.green { color: var(--verde); }
.yellow { color: var(--amarillo); }
.orange { color: var(--naranja); }
.red { color: var(--rojo); }

.metric {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 9px 0;
    border-bottom: 1px solid rgba(0, 229, 255, .10);
}

.metric:last-child { border-bottom: none; }
.metric-name { color: var(--suave); font-size: 13px; }
.metric-value { font-size: 17px; font-weight: 800; }

.footer {
    min-height: 34px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    letter-spacing: 2px;
    color: #829ab4;
    background: rgba(4, 10, 21, .7);
    border-top: 1px solid rgba(0, 229, 255, .12);
}

@media (max-width: 900px) {
    html, body { overflow: auto; min-height: 100%; height: auto; }
    .header { min-height: 64px; padding: 12px 16px; }
    .title { font-size: 20px; letter-spacing: 2px; }
    .main { grid-template-columns: 1fr; }
    .radar-area { min-height: 360px; padding: 12px; }
    .radar { width: min(78vw, 55vh); height: min(78vw, 55vh); min-width: 250px; min-height: 250px; }
    .panel { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .box { padding: 14px; }
}

@media (max-width: 520px) {
    .header { align-items: flex-start; flex-direction: column; }
    .panel { grid-template-columns: 1fr; }
    .value { font-size: 21px; }
}

/* ===== SEISMIC CORE X · FUTURISTIC HUD ===== */

:root {
    --hud-cyan: #48f5ff;
    --hud-blue: #168bff;
    --hud-text: #d9f8ff;
    --hud-muted: #7295ab;
    --hud-border: rgba(72,245,255,.30);
    --hud-panel: rgba(5,17,32,.88);
}

body {
    color: var(--hud-text);
    background:
        radial-gradient(ellipse at 50% 35%, rgba(0,105,170,.13), transparent 55%),
        #030711;
    font-family: "Consolas", "Courier New", monospace;
}

.header {
    position: relative;
    padding: 22px 28px;
    border-bottom: 1px solid var(--hud-border);
    background: linear-gradient(90deg, rgba(5,20,39,.97), rgba(5,12,27,.88));
    box-shadow: 0 5px 30px rgba(0,170,255,.09);
}

.header::after {
    content: "";
    position: absolute;
    bottom: -1px;
    left: 4%;
    width: 28%;
    height: 2px;
    background: var(--hud-cyan);
    box-shadow: 0 0 12px var(--hud-cyan);
}

.title {
    color: var(--hud-cyan);
    letter-spacing: .20em;
    text-shadow: 0 0 8px rgba(72,245,255,.65),
                 0 0 24px rgba(22,139,255,.35);
    font-weight: 800;
}

.connection {
    border: 1px solid var(--hud-border);
    background: rgba(0,180,220,.07);
    padding: 8px 12px;
    letter-spacing: .09em;
    box-shadow: inset 0 0 12px rgba(72,245,255,.04);
}

.main {
    gap: 24px;
    padding: 24px;
}

.radar-area {
    position: relative;
    border: 1px solid var(--hud-border);
    border-radius: 5px;
    background: linear-gradient(145deg, rgba(8,28,48,.75), rgba(2,8,19,.9));
    box-shadow: inset 0 0 35px rgba(0,140,255,.07),
                0 0 22px rgba(0,140,255,.07);
}

.radar-area::before {
    content: "LIVE RADAR  /  MPU6050";
    position: absolute;
    top: 12px;
    left: 16px;
    z-index: 5;
    color: var(--hud-cyan);
    font-size: 10px;
    letter-spacing: .16em;
    opacity: .85;
}

.radar {
    isolation: isolate;
    background:
        radial-gradient(circle, rgba(0,220,255,.09) 0 1px, transparent 2px),
        radial-gradient(circle at center, rgba(0,105,160,.16), rgba(2,10,22,.95) 72%);
    background-size: 12px 12px, auto;
    border: 1px solid rgba(72,245,255,.25);
    box-shadow: inset 0 0 35px rgba(0,190,255,.12),
                0 0 20px rgba(0,160,255,.09);
}

.radar::before {
    content: "";
    position: absolute;
    inset: 0;
    border-radius: 50%;
    pointer-events: none;
    background: repeating-linear-gradient(
        0deg,
        transparent 0px,
        transparent 5px,
        rgba(72,245,255,.025) 6px
    );
}

.circle1, .circle2, .circle3 {
    border-color: rgba(72,245,255,.40) !important;
    box-shadow: 0 0 7px rgba(0,190,255,.08);
}

.cardinal {
    color: #9befff;
    text-shadow: 0 0 8px rgba(72,245,255,.55);
    font-weight: bold;
}

.center {
    box-shadow: 0 0 8px var(--hud-cyan),
                0 0 22px rgba(72,245,255,.65);
}

.sweep {
    filter: drop-shadow(0 0 5px var(--hud-cyan));
}

.panel {
    gap: 14px;
}

.box {
    position: relative;
    overflow: hidden;
    border: 1px solid var(--hud-border);
    border-radius: 4px;
    background: linear-gradient(135deg, rgba(9,28,47,.94), var(--hud-panel));
    box-shadow: inset 0 0 18px rgba(0,140,255,.045),
                0 5px 18px rgba(0,0,0,.18);
    padding: 17px;
}

.box::before {
    content: "";
    position: absolute;
    top: 0;
    left: 0;
    width: 46px;
    height: 2px;
    background: var(--hud-cyan);
    box-shadow: 0 0 9px var(--hud-cyan);
}

.label {
    color: var(--hud-muted);
    font-size: 11px;
    letter-spacing: .16em;
    margin-bottom: 10px;
}

.value {
    font-weight: 800;
    letter-spacing: .04em;
    text-shadow: 0 0 12px currentColor;
}

.metric {
    padding: 9px 0;
    border-bottom: 1px solid rgba(72,245,255,.10);
}

.metric:last-child {
    border-bottom: 0;
}

.metric-name {
    color: var(--hud-muted);
    letter-spacing: .08em;
}

.metric-value {
    color: var(--hud-text);
    text-shadow: 0 0 8px rgba(72,245,255,.2);
}

.footer {
    border-top: 1px solid var(--hud-border);
    color: var(--hud-muted);
    letter-spacing: .14em;
    font-size: 10px;
    padding: 16px;
    text-align: center;
}

@keyframes hud-glow {
    0%, 100% { box-shadow: 0 0 5px rgba(72,245,255,.08); }
    50% { box-shadow: 0 0 16px rgba(72,245,255,.20); }
}

.box {
    animation: hud-glow 5s ease-in-out infinite;
}

@media (max-width: 760px) {
    .main { padding: 12px; gap: 14px; }
    .header { padding: 16px; }
    .title { letter-spacing: .10em; }
}


/* SEISMIC CORE X - panel extendido */
.main {
    grid-template-columns: minmax(0, 1fr) 300px;
    grid-template-rows: minmax(0, 1fr) 176px;
    gap: 12px;
    padding: 12px;
}
.radar-area, .panel { min-height: 0; }
.panel { overflow-y: auto; }
.telemetry-strip {
    grid-column: 1 / -1;
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr) 230px;
    gap: 12px;
}
.telemetry-card {
    min-width: 0;
    overflow: hidden;
    position: relative;
    padding: 12px 15px;
    border: 1px solid rgba(0,229,255,.27);
    border-radius: 12px;
    background: linear-gradient(135deg,rgba(10,27,48,.96),rgba(4,12,25,.94));
    box-shadow: inset 0 0 22px rgba(0,180,255,.045);
}
.telemetry-heading {
    color: #00e5ff;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 2px;
    margin-bottom: 7px;
}
#waveCanvas { display:block; width:100%; height:100px; }
.telemetry-values {
    display:grid;
    grid-template-columns: repeat(3,minmax(0,1fr));
    gap:8px;
}
.telemetry-value {
    font-size: clamp(16px,1.8vw,25px);
    font-weight:800;
    color:#e5faff;
    overflow-wrap:anywhere;
}
.telemetry-caption { color:#829ab4; font-size:10px; letter-spacing:1px; }
.clock-layout { display:flex; align-items:center; gap:14px; height:112px; }
.clock-ring {
    width:84px; height:84px; flex:none; border-radius:50%;
    display:grid; place-items:center;
    background:conic-gradient(#00e5ff 0deg,#00e5ff 0deg,rgba(0,229,255,.12) 0deg);
    box-shadow:0 0 22px rgba(0,229,255,.12);
}
.clock-inner {
    width:68px;height:68px;border-radius:50%;
    display:flex;flex-direction:column;align-items:center;justify-content:center;
    background:#07111f;border:1px solid rgba(0,229,255,.28);
}
#clockSeconds { font-size:22px;font-weight:800;color:#fff; }
#soundButton {
    margin-top:8px; border:1px solid #00cfe8; border-radius:6px;
    padding:7px 10px; background:#09253a;color:#aaf8ff;
    font-weight:800;font-size:10px;letter-spacing:1px;cursor:pointer;
}
#soundButton:hover { background:#10405a; }
#historial { font-size:11px;line-height:1.7;color:#c8e8f4; }
.history-line { display:flex;justify-content:space-between;gap:8px;border-bottom:1px solid rgba(0,229,255,.09); }
.history-line span:last-child { color:#00e5ff;font-variant-numeric:tabular-nums; }
@media(max-height:760px) {
    .header { min-height:52px; }
    .main { grid-template-rows:minmax(0,1fr) 145px; padding:8px; gap:8px; }
    .telemetry-card { padding:8px 10px; }
    #waveCanvas { height:75px; }
    .clock-layout { height:95px; }
    .clock-ring { width:70px;height:70px; }
    .clock-inner { width:56px;height:56px; }
}
@media(max-width:850px) {
    html,body { height:auto; min-height:100%; overflow:auto; }
    .main { display:grid; grid-template-columns:minmax(0,1fr); grid-template-rows:auto; }
    .radar-area { min-height:360px; }
    .panel { overflow:visible; }
    .telemetry-strip { grid-column:1; grid-template-columns:minmax(0,1fr); }
    .telemetry-card { min-height:145px; }
}


/* SEISMIC CORE X - MATRIX */
:root {
    --fondo: #000500;
    --panel: rgba(0, 14, 3, .94);
    --borde: rgba(0, 255, 65, .48);
    --cian: #00ff41;
    --texto: #c8ffd2;
    --suave: #53bd70;
    --verde: #00ff41;
    --amarillo: #caff00;
    --naranja: #8cff00;
    --rojo: #ff3030;
}

html, body {
    background: #000 !important;
    color: #c8ffd2 !important;
    font-family: Consolas, "Courier New", monospace !important;
}

body {
    position: relative;
    isolation: isolate;
}

body::before {
    content: "010101  ░  01  11001  ▓  1010  01101  ░  001  10110  ▓  0101  110  00101  ░  10101  011  11001  ▓  01010  101  00110  ░  11010  010  10101  ▓  00101  11001  010";
    position: fixed;
    inset: -100% 0 0 0;
    z-index: -1;
    pointer-events: none;
    overflow: hidden;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: #00ff41;
    opacity: .16;
    font: bold 15px/1.8 Consolas, monospace;
    text-shadow: 0 0 7px #00ff41;
    animation: matrixCaida 18s linear infinite;
}

@keyframes matrixCaida {
    from { transform: translateY(-25%); }
    to   { transform: translateY(75%); }
}

.header {
    background: rgba(0, 8, 1, .97) !important;
    border-bottom: 1px solid #00ff41 !important;
    box-shadow: 0 0 18px rgba(0, 255, 65, .2) !important;
}

.title {
    color: #00ff41 !important;
    text-shadow: 0 0 12px rgba(0, 255, 65, .8) !important;
}

.radar-area,
[class*="panel"],
[class*="card"],
[class*="telemetry"] {
    background-color: rgba(0, 12, 2, .82) !important;
    border-color: rgba(0, 255, 65, .4) !important;
    box-shadow: inset 0 0 22px rgba(0, 255, 65, .06) !important;
}

button {
    color: #00ff41 !important;
    background: #001406 !important;
    border: 1px solid #00ff41 !important;
    font-family: Consolas, monospace !important;
    box-shadow: 0 0 10px rgba(0, 255, 65, .18);
}

button:hover {
    background: #00300c !important;
    box-shadow: 0 0 18px rgba(0, 255, 65, .45);
}

canvas {
    filter: hue-rotate(65deg) saturate(1.5);
}

</style>

</head>

<body>

<header class="header">

    <div class="title">
        SEISMIC CORE X
    </div>

    <div
        id="conexion"
        class="connection green">
        INTERNET CONECTADO
    </div>

</header>

<main class="main">

    <section class="radar-area">

        <div
            id="radar"
            class="radar">

            <div class="circle1"></div>
            <div class="circle2"></div>
            <div class="circle3"></div>

            <div class="cardinal norte">
                N
            </div>

            <div class="cardinal ne">
                NE
            </div>

            <div class="cardinal este">
                E
            </div>

            <div class="cardinal se">
                SE
            </div>

            <div class="cardinal sur">
                S
            </div>

            <div class="cardinal so">
                SO
            </div>

            <div class="cardinal oeste">
                O
            </div>

            <div class="cardinal no">
                NO
            </div>

            <div class="center"></div>

            <div
                id="barrido"
                class="sweep">
            </div>

            <div
                id="puntoEvento"
                class="event-point">
            </div>

        </div>

    </section>

    <aside class="panel">

        <div class="box">

            <div class="label">
                ESTADO DEL SENSOR
            </div>

            <div
                id="estado"
                class="value green">
                SISTEMA ESTABLE
            </div>

        </div>

        <div class="box">

            <div class="label">
                MAGNITUD
            </div>

            <div
                id="magnitud"
                class="value green">
                0.000 g
            </div>

        </div>

        <div class="box">

            <div class="label">
                FILTRADA
            </div>

            <div
                id="filtrada"
                class="value green">
                0.000 g
            </div>

        </div>

        <div class="box">

            <div class="label">
                SENSOR
            </div>

            <div class="metric">

                <span class="metric-name">
                    ESTADO
                </span>

                <span
                    id="sensorEstado"
                    class="metric-value green">
                    ACTIVO
                </span>

            </div>

            <div class="metric">

                <span class="metric-name">
                    UMBRAL
                </span>

                <span class="metric-value">
                    0.025 g
                </span>

            </div>

            <div class="metric">

                <span class="metric-name">
                    DIRECCION
                </span>

                <span
                    id="direccion"
                    class="metric-value">
                    —
                </span>

            </div>

        </div>

    </aside>


    <section id="seismic-extra-panel" class="telemetry-strip">
        <div class="telemetry-card">
            <div class="telemetry-heading">▰ SEÑAL SÍSMICA EN VIVO</div>
            <canvas id="waveCanvas" aria-label="Gráfica de vibración en tiempo real"></canvas>
        </div>
        <div class="telemetry-card">
            <div class="telemetry-heading">▰ TELEMETRÍA DEL SENSOR</div>
            <div class="telemetry-values">
                <div><div class="telemetry-caption">MAGNITUD</div><div id="teleMagnitud" class="telemetry-value">0.000</div></div>
                <div><div class="telemetry-caption">FILTRADA</div><div id="teleFiltrada" class="telemetry-value">0.000</div></div>
                <div><div class="telemetry-caption">NIVEL</div><div id="teleNivel" class="telemetry-value">1</div></div>
            </div>
            <div class="telemetry-caption" style="margin-top:12px">ESTADO: <span id="teleEstado">ESPERANDO DATOS</span></div>
            <div class="telemetry-caption" style="margin-top:5px">LECTURAS RECIENTES · MPU6050</div>
        </div>
        <div class="telemetry-card">
            
            <div class="clock-layout">
                
                <div>
                    
                    
                    <div id="clockTime" role="button" tabindex="0"
                         title="Pulsa para activar el sonido"
                         style="font-size:30px;font-weight:900;letter-spacing:2px;color:#00ff41;text-shadow:0 0 12px #00ff41;cursor:pointer;font-family:Consolas,monospace">
                        --:--:--
                    </div>
                </div>
            </div>
        </div>
    </section>

</main>

<footer class="footer">

    MPU6050 · NODEMCU · SEISMIC CORE X

</footer>

<script>

let audioContext = null;

let ultimoSonar = 0;

let ultimoSonidoEvento = 0;

function obtenerAudio() {

    if (!audioContext) {

        try {

            audioContext =
                new (
                    window.AudioContext ||
                    window.webkitAudioContext
                )();

        } catch (error) {

            return null;
        }
    }

    if (
        audioContext.state ===
        "suspended"
    ) {

        audioContext.resume();
    }

    return audioContext;
}


function pingEstable() {
    const audio = obtenerAudio();
    if (!audio) return;

    const t = audio.currentTime;
    const osc = audio.createOscillator();
    const volumen = audio.createGain();
    const reverb = audio.createConvolver();
    const cola = audio.createGain();

    // Reverberacion larga con desvanecimiento suave
    const duracion = 3.0;
    const longitud = Math.floor(audio.sampleRate * duracion);
    const impulso = audio.createBuffer(2, longitud, audio.sampleRate);

    for (let canal = 0; canal < 2; canal++) {
        const datos = impulso.getChannelData(canal);
        for (let i = 0; i < longitud; i++) {
            const tiempo = i / longitud;
            datos[i] = (Math.random() * 2 - 1)
                * Math.pow(1 - tiempo, 3) * 0.45;
        }
    }

    reverb.buffer = impulso;
    cola.gain.value = 0.42;

    osc.type = "sine";
    osc.frequency.setValueAtTime(2200, t);

    // Ataque suave, tono sostenido y salida gradual
    volumen.gain.setValueAtTime(0.0001, t);
    volumen.gain.linearRampToValueAtTime(0.16, t + 0.03);
    volumen.gain.setValueAtTime(0.16, t + 0.55);
    volumen.gain.exponentialRampToValueAtTime(0.0001, t + 1.10);

    osc.connect(volumen);
    volumen.connect(audio.destination);
    volumen.connect(reverb);
    reverb.connect(cola);
    cola.connect(audio.destination);

    osc.start(t);
    osc.stop(t + 1.12);
}

function sonidoMovimiento(nivel) {

    const audio = obtenerAudio();

    if (!audio) {
        return;
    }

    const ahora = audio.currentTime;

    const oscilador = audio.createOscillator();
    const ganancia = audio.createGain();

    oscilador.type = "sine";

    if (nivel === 2) {

        oscilador.frequency.setValueAtTime(
            620,
            ahora
        );

        oscilador.frequency.exponentialRampToValueAtTime(
            420,
            ahora + 0.22
        );

    } else {

        oscilador.frequency.setValueAtTime(
            780,
            ahora
        );

        oscilador.frequency.exponentialRampToValueAtTime(
            460,
            ahora + 0.30
        );
    }

    ganancia.gain.setValueAtTime(
        0.0001,
        ahora
    );

    ganancia.gain.exponentialRampToValueAtTime(
        nivel === 2 ? 0.12 : 0.18,
        ahora + 0.025
    );

    ganancia.gain.exponentialRampToValueAtTime(
        0.0001,
        ahora + 0.34
    );

    oscilador.connect(ganancia);
    ganancia.connect(audio.destination);

    oscilador.start(ahora);
    oscilador.stop(ahora + 0.4);
}


function sonidoCritico() {

    const audio = obtenerAudio();

    if (!audio) {
        return;
    }

    const ahora = audio.currentTime;

    for (
        let i = 0;
        i < 6;
        i++
    ) {

        const oscilador = audio.createOscillator();
        const ganancia = audio.createGain();

        const inicio =
            ahora + i * 0.18;

        oscilador.type = "square";

        oscilador.frequency.setValueAtTime(
            i % 2 === 0 ? 920 : 650,
            inicio
        );

        ganancia.gain.setValueAtTime(
            0.0001,
            inicio
        );

        ganancia.gain.exponentialRampToValueAtTime(
            0.16,
            inicio + 0.02
        );

        ganancia.gain.exponentialRampToValueAtTime(
            0.0001,
            inicio + 0.14
        );

        oscilador.connect(ganancia);
        ganancia.connect(audio.destination);

        oscilador.start(inicio);
        oscilador.stop(inicio + 0.16);
    }
}


let anguloRadar = 0;

function animarRadar() {

    anguloRadar += 0.35;

    if (anguloRadar >= 360) {
        anguloRadar -= 360;
    }

    const barrido =
        document.getElementById("barrido");

    barrido.style.transform =
        "rotate(" +
        anguloRadar +
        "deg)";

    requestAnimationFrame(
        animarRadar
    );
}


let ultimoNivel = 1;

let puntoVisible = false;

let ultimoEventoRecibido = 0;

let numeroDesvanecimiento = 0;


function actualizarPunto(datos) {

    const punto =
        document.getElementById(
            "puntoEvento"
        );

    const radar =
        document.getElementById(
            "radar"
        );

    if (
        !datos.evento ||
        !datos.direccion
    ) {

        if (puntoVisible) {

            numeroDesvanecimiento++;

            const desvanecimientoActual =
                numeroDesvanecimiento;

            punto.classList.remove(
                "blink"
            );

            punto.style.opacity =
                "0";

            punto.style.transform =
                "scale(0.35)";

            setTimeout(
                function() {

                    if (
                        desvanecimientoActual ===
                        numeroDesvanecimiento
                    ) {

                        punto.classList.remove(
                            "visible"
                        );

                        punto.style.left =
                            "50%";

                        punto.style.top =
                            "50%";

                        punto.style.opacity =
                            "0";

                        punto.style.transform =
                            "scale(0.35)";

                        puntoVisible =
                            false;
                    }

                },
                700
            );
        }

        return;
    }

    numeroDesvanecimiento++;

    ultimoEventoRecibido =
        Date.now();

    const rect =
        radar.getBoundingClientRect();

    const centroX =
        rect.width / 2;

    const centroY =
        rect.height / 2;

    const radioMaximo =
        Math.min(
            centroX,
            centroY
        ) * 0.88;

    const distanciaNormalizada =
        Math.max(
            0.10,
            Math.min(
                0.82,
                datos.direccion.distancia
            )
        );

    // Punto más exterior, incluso con movimientos pequeños
    const distanciaVisual =
        0.65 +
        ((distanciaNormalizada - 0.10) / 0.72) * 0.33;

    const radio =
        radioMaximo *
        distanciaVisual;

    const angulo =
        datos.direccion.angulo *
        Math.PI /
        180;

    let x =
        centroX +
        Math.sin(angulo) *
        radio;

    let y =
        centroY -
        Math.cos(angulo) *
        radio;

    const dx =
        x - centroX;

    const dy =
        y - centroY;

    const distanciaReal =
        Math.sqrt(
            dx * dx +
            dy * dy
        );

    if (
        distanciaReal >
        radioMaximo
    ) {

        const factor =
            radioMaximo /
            distanciaReal;

        x =
            centroX +
            dx * factor;

        y =
            centroY +
            dy * factor;
    }

    punto.style.left =
        x + "px";

    punto.style.top =
        y + "px";

    punto.classList.remove(
        "yellow",
        "orange",
        "red"
    );

    if (datos.nivel === 2) {

        punto.classList.add(
            "yellow"
        );

    } else if (
        datos.nivel === 3
    ) {

        punto.classList.add(
            "orange"
        );

    } else if (
        datos.nivel === 4
    ) {

        punto.classList.add(
            "red"
        );
    }

    punto.classList.add(
        "visible"
    );

    punto.classList.add(
        "blink"
    );

    puntoVisible = true;
}



let soundEnabled = false;
const vibrationHistory = [];
const maxHistory = 100;
let lastClockCycle = -1;
const clockDuration = 12000;
const clockStart = performance.now();

function registrarLectura(datos) {
    const magnitud = Number(datos.magnitud) || 0;
    const filtrada = Number(datos.filtrada) || 0;
    const nivel = Number(datos.nivel) || 1;

    vibrationHistory.push(filtrada);
    if (vibrationHistory.length > maxHistory) vibrationHistory.shift();

    document.getElementById("teleMagnitud").textContent = magnitud.toFixed(3);
    document.getElementById("teleFiltrada").textContent = filtrada.toFixed(3);
    document.getElementById("teleNivel").textContent = nivel;
    document.getElementById("teleEstado").textContent = datos.estado || "SIN DATOS";

    dibujarOnda();
}

function dibujarOnda() {
    const canvas = document.getElementById("waveCanvas");
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;

    const dpr = window.devicePixelRatio || 1;
    const w = Math.floor(rect.width * dpr);
    const h = Math.floor(rect.height * dpr);
    if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
    }

    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = "rgba(0,229,255,0.12)";
    ctx.lineWidth = 1 * dpr;
    for (let y = h * .25; y < h; y += h * .25) {
        ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(w,y); ctx.stroke();
    }

    const values = vibrationHistory;
    if (values.length < 2) return;
    const maxValue = Math.max(0.025, ...values.map(v => Math.abs(v)));
    ctx.beginPath();
    values.forEach((value, i) => {
        const x = i / (maxHistory - 1) * w;
        const y = h / 2 - (value / maxValue) * h * .40;
        if (i === 0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    });
    ctx.strokeStyle = "#00e5ff";
    ctx.lineWidth = 2 * dpr;
    ctx.shadowColor = "#00e5ff";
    ctx.shadowBlur = 7 * dpr;
    ctx.stroke();
    ctx.shadowBlur = 0;
}

function activarSonido() {
    const audio = obtenerAudio();
    if (!audio) return;
    audio.resume().then(() => {
        soundEnabled = true;
        document.getElementById("clockStatus").textContent = "PITIDO ACTIVADO";
        document.getElementById("clockTime").style.color = "#00ff41";
        pingEstable();
    }).catch(() => {
        document.getElementById("clockStatus").textContent = "NO DISPONIBLE";
    });
}

const relojDigital = document.getElementById("clockTime");
relojDigital.addEventListener("click", activarSonido);
relojDigital.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") activarSonido();
});

function actualizarHoraDigital() {
    const ahora = new Date();
    document.getElementById("clockTime").textContent =
        ahora.toLocaleTimeString("es-CL", { hour12: false });
}
actualizarHoraDigital();
setInterval(actualizarHoraDigital, 1000);




function actualizarInterfaz(datos) {
    registrarLectura(datos);

    const estado =
        document.getElementById(
            "estado"
        );

    const magnitud =
        document.getElementById(
            "magnitud"
        );

    const filtrada =
        document.getElementById(
            "filtrada"
        );

    const direccion =
        document.getElementById(
            "direccion"
        );

    const conexion =
        document.getElementById(
            "conexion"
        );

    estado.textContent =
        datos.estado;

    magnitud.textContent =
        Number(
            datos.magnitud
        ).toFixed(3) +
        " g";

    filtrada.textContent =
        Number(
            datos.filtrada
        ).toFixed(3) +
        " g";

    estado.classList.remove(
        "green",
        "yellow",
        "orange",
        "red"
    );

    if (datos.nivel === 1) {

        estado.classList.add(
            "green"
        );

    } else if (
        datos.nivel === 2
    ) {

        estado.classList.add(
            "yellow"
        );

    } else if (
        datos.nivel === 3
    ) {

        estado.classList.add(
            "orange"
        );

    } else if (
        datos.nivel === 4
    ) {

        estado.classList.add(
            "red"
        );
    }

    if (
        datos.direccion &&
        datos.evento
    ) {

        direccion.textContent =
            Math.round(
                datos.direccion.angulo
            ) +
            "°";

    } else {

        direccion.textContent =
            "—";
    }

    conexion.textContent =
        "INTERNET CONECTADO";

    conexion.className =
        "connection green";

    actualizarPunto(datos);

    const ahora =
        Date.now();

    if (
        datos.evento &&
        datos.nivel !== ultimoNivel
    ) {

        if (datos.nivel === 4) {

            sonidoCritico();

        } else if (
            datos.nivel === 2 ||
            datos.nivel === 3
        ) {

            sonidoMovimiento(
                datos.nivel
            );
        }

        ultimoNivel =
            datos.nivel;
    }

    if (
        datos.evento &&
        (
            datos.nivel === 2 ||
            datos.nivel === 3
        ) &&
        ahora - ultimoSonidoEvento > 2500
    ) {

        sonidoMovimiento(
            datos.nivel
        );

        ultimoSonidoEvento =
            ahora;
    }

    if (!datos.evento) {

        ultimoNivel = 1;
    }
}


async function consultarEstado() {

    try {

        const respuesta =
            await fetch(
                "/estado",
                {
                    cache: "no-store"
                }
            );

        if (!respuesta.ok) {

            throw new Error(
                "Servidor"
            );
        }

        const datos =
            await respuesta.json();

        actualizarInterfaz(
            datos
        );

    } catch (error) {

        const conexion =
            document.getElementById(
                "conexion"
            );

        conexion.textContent =
            "SIN CONEXION";

        conexion.className =
            "connection red";
    }
}


function mantenerSonar() {

    const ahora =
        Date.now();

    if (
        ahora - ultimoSonar >= 2800
    ) {

        pingEstable();

        ultimoSonar =
            ahora;
    }
}


animarRadar();

consultarEstado();

setInterval(
    consultarEstado,
    100
);

setInterval(
    mantenerSonar,
    100
);

</script>

</body>

</html>
"""


@app.route("/")
def inicio():

    return render_template_string(
        HTML
    )


if __name__ == "__main__":

    print("======================================")
    print("      SEISMIC CORE X")
    print("      ESTACION DE VIBRACION")
    print("======================================")
    print("Servidor: 0.0.0.0:5000")
    print("NodeMCU esperado: 192.168.1.199")
    print("======================================")

    app.run(
        host="0.0.0.0",
        port=5000,
        threaded=True
    )
