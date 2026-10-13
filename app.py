from flask import Flask, request, jsonify, render_template_string
import math
import time
from collections import deque

app = Flask(__name__)

# ==========================================================
# SEISMIC CORE X — MOTOR DE DETECCIÓN
# ==========================================================

UMBRAL_VIBRACION = 0.040
UMBRAL_TEMBLOR = 0.060
UMBRAL_ALERTA = 0.120

UMBRAL_ESTABLE = 0.020
TIEMPO_ESTABLE_ALERTA = 5.0
TIEMPO_FIN_EVENTO = 2.0

MUESTRAS_CALIBRACION = 100
MUESTRAS_FILTRO = 5
TIEMPO_SENSOR_DESCONECTADO = 5.0

base_x = 0.0
base_y = 0.0
base_z = 0.0

calibrando = True
muestras_calibracion = []
filtro = deque(maxlen=MUESTRAS_FILTRO)

ultima_magnitud = 0.0
magnitud_filtrada = 0.0

estado_actual = "ESPERANDO SENSOR"
nivel_actual = 1

ultimo_movimiento = 0.0
inicio_estabilidad = None
alerta_declarada = False

ultimo_x = 0.0
ultimo_y = 0.0
ultimo_z = 0.0

evento_activo = False
inicio_evento = None
ultimo_dato_sensor = 0.0


def procesar_calibracion(x, y, z):
    global base_x, base_y, base_z, calibrando
    global muestras_calibracion

    muestras_calibracion.append((x, y, z))

    if len(muestras_calibracion) >= MUESTRAS_CALIBRACION:
        cantidad = len(muestras_calibracion)

        base_x = sum(p[0] for p in muestras_calibracion) / cantidad
        base_y = sum(p[1] for p in muestras_calibracion) / cantidad
        base_z = sum(p[2] for p in muestras_calibracion) / cantidad

        calibrando = False
        filtro.clear()

        print("Calibración terminada")
        print("Base X:", base_x)
        print("Base Y:", base_y)
        print("Base Z:", base_z)


def procesar_sensor(x, y, z):
    global ultima_magnitud, magnitud_filtrada
    global estado_actual, nivel_actual
    global ultimo_movimiento, inicio_estabilidad
    global alerta_declarada, ultimo_x, ultimo_y, ultimo_z
    global evento_activo, inicio_evento

    ultimo_x, ultimo_y, ultimo_z = x, y, z

    if calibrando:
        procesar_calibracion(x, y, z)
        return

    x_corregido = x - base_x
    y_corregido = y - base_y
    z_corregido = z - base_z

    magnitud = math.sqrt(
        x_corregido ** 2 +
        y_corregido ** 2 +
        z_corregido ** 2
    )

    ultima_magnitud = magnitud
    filtro.append(magnitud)
    magnitud_filtrada = sum(filtro) / len(filtro)

    ahora = time.time()

    if magnitud_filtrada >= UMBRAL_ALERTA:
        estado_actual = "ALERTA CRÍTICA"
        nivel_actual = 4
        alerta_declarada = True
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
        if ultimo_movimiento > 0 and ahora - ultimo_movimiento >= TIEMPO_FIN_EVENTO:
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

    if math.sqrt(x * x + y * y) < 0.0005:
        return None

    grados = math.degrees(math.atan2(x, y))

    if grados < 0:
        grados += 360

    distancia = magnitud_filtrada / UMBRAL_ALERTA * 0.82
    distancia = max(0.10, min(0.82, distancia))

    return {
        "angulo": grados,
        "distancia": distancia
    }


# ==========================================================
# API ORIGINAL: /datos Y /estado
# ==========================================================

@app.route("/datos", methods=["POST"])
def recibir_datos():
    global ultimo_dato_sensor

    try:
        datos = request.get_json(force=True)

        x = float(datos.get("x", 0))
        y = float(datos.get("y", 0))
        z = float(datos.get("z", 0))

        ultimo_dato_sensor = time.time()
        procesar_sensor(x, y, z)

        return jsonify({"ok": True})

    except Exception as error:
        print("Error recibiendo datos:", error)
        return jsonify({"ok": False, "error": str(error)}), 400


@app.route("/estado")
def obtener_estado():
    sensor_conectado = (
        ultimo_dato_sensor > 0
        and time.time() - ultimo_dato_sensor < TIEMPO_SENSOR_DESCONECTADO
    )

    if not sensor_conectado:
        estado_visible = "ESPERANDO SENSOR"
        nivel_visible = 1
    elif calibrando:
        estado_visible = "CALIBRANDO SENSOR"
        nivel_visible = 1
    else:
        estado_visible = estado_actual
        nivel_visible = nivel_actual

    return jsonify({
        "estado": estado_visible,
        "nivel": nivel_visible,
        "magnitud": round(ultima_magnitud, 5),
        "filtrada": round(magnitud_filtrada, 5),
        "calibrando": calibrando,
        "evento": evento_activo if sensor_conectado else False,
        "direccion": calcular_direccion() if sensor_conectado else None,
        "sensor_conectado": sensor_conectado,
        "timestamp": time.time()
    })


# ==========================================================
# NUEVO DISEÑO HUD — SEISMIC CORE X
# ==========================================================

HTML = r"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SEISMIC CORE X | Monitoring System</title>

<style>
:root {
    --green: #00ff78;
    --green2: #8affbb;
    --bg: #020704;
    --panel: rgba(4, 18, 11, .94);
    --border: rgba(0, 255, 120, .27);
    --muted: #669b7a;
    --yellow: #eaff00;
    --orange: #ff8c32;
    --red: #ff304e;
}

* { box-sizing: border-box; }

html, body {
    margin: 0;
    width: 100%;
    min-height: 100%;
    background: var(--bg);
    color: #d5ffe3;
    font-family: Consolas, "Courier New", monospace;
}

body {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    overflow-x: hidden;
}

body::before {
    content: "";
    position: fixed;
    inset: 0;
    z-index: 0;
    pointer-events: none;
    opacity: .10;
    background: repeating-linear-gradient(
        0deg,
        transparent 0,
        transparent 3px,
        rgba(0,255,120,.11) 4px
    );
}

header, main, footer { position: relative; z-index: 1; }

header {
    min-height: 78px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    padding: 15px 25px;
    background: linear-gradient(90deg, #020b05, #07180c, #020804);
    border-bottom: 1px solid var(--green);
    box-shadow: 0 0 24px rgba(0,255,120,.10);
}

.brand {
    display: flex;
    align-items: center;
    gap: 15px;
}

.logo {
    width: 43px;
    height: 43px;
    display: grid;
    place-items: center;
    border: 1px solid var(--green);
    color: var(--green);
    font-size: 23px;
    transform: rotate(45deg);
    box-shadow: 0 0 16px rgba(0,255,120,.17);
}

.logo span { transform: rotate(-45deg); }

.title {
    font-size: clamp(19px, 2vw, 29px);
    font-weight: 900;
    letter-spacing: .15em;
    color: var(--green);
    text-shadow: 0 0 17px rgba(0,255,120,.5);
}

.subtitle {
    color: var(--muted);
    font-size: 10px;
    letter-spacing: .22em;
    margin-top: 5px;
}

.header-right {
    display: flex;
    align-items: center;
    gap: 22px;
}

.clock {
    font-size: 21px;
    font-weight: bold;
    letter-spacing: .08em;
    color: var(--green2);
}

.connection {
    border: 1px solid var(--border);
    padding: 10px 13px;
    font-size: 10px;
    letter-spacing: .1em;
    color: var(--green);
    background: rgba(0,255,120,.05);
}

.connection::before {
    content: "●";
    margin-right: 8px;
}

.connection.offline {
    color: var(--orange);
    border-color: rgba(255,140,50,.5);
}

.connection.offline::before { color: var(--orange); }

main {
    flex: 1;
    display: grid;
    grid-template-columns: minmax(0, 1fr) 315px;
    grid-template-rows: minmax(420px, 1fr) auto;
    gap: 13px;
    padding: 14px;
}

.panel {
    min-width: 0;
    border: 1px solid var(--border);
    background: var(--panel);
    box-shadow: inset 0 0 25px rgba(0,255,120,.025);
    position: relative;
}

.panel::before {
    content: "";
    position: absolute;
    top: -1px;
    left: 18px;
    width: 65px;
    height: 2px;
    background: var(--green);
    box-shadow: 0 0 10px var(--green);
}

.panel-title {
    font-size: 11px;
    color: var(--green);
    letter-spacing: .16em;
    padding: 15px 16px 10px;
    border-bottom: 1px solid rgba(0,255,120,.12);
}

.radar-panel {
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
}

.radar-meta {
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
    color: var(--muted);
    font-size: 10px;
    padding: 11px 16px;
}

.radar-stage {
    flex: 1;
    min-height: 350px;
    display: grid;
    place-items: center;
    padding: 12px;
    position: relative;
    overflow: hidden;
    background:
        radial-gradient(ellipse at center, rgba(0,255,120,.06), transparent 66%);
}

.radar {
    position: relative;
    width: min( min(70vw, 72vh), 690px);
    aspect-ratio: 1;
    max-width: 100%;
    border-radius: 50%;
    border: 1px solid rgba(0,255,120,.65);
    overflow: hidden;
    background:
        radial-gradient(circle, transparent 0 14%, rgba(0,255,120,.08) 14.2%, transparent 14.6%),
        radial-gradient(circle, transparent 0 29%, rgba(0,255,120,.15) 29.2%, transparent 29.6%),
        radial-gradient(circle, transparent 0 44%, rgba(0,255,120,.18) 44.2%, transparent 44.6%),
        radial-gradient(circle, transparent 0 59%, rgba(0,255,120,.22) 59.2%, transparent 59.6%),
        radial-gradient(circle, rgba(0,255,120,.035), rgba(0,0,0,.7));
    box-shadow: 0 0 35px rgba(0,255,120,.08), inset 0 0 40px rgba(0,255,120,.06);
}

.radar::before, .radar::after {
    content: "";
    position: absolute;
    background: rgba(0,255,120,.22);
    pointer-events: none;
}

.radar::before {
    width: 1px;
    height: 100%;
    left: 50%;
}

.radar::after {
    height: 1px;
    width: 100%;
    top: 50%;
}

.axis {
    position: absolute;
    left: 50%;
    top: 50%;
    width: 100%;
    height: 1px;
    background: rgba(0,255,120,.09);
}

.axis.a { transform: rotate(45deg); }
.axis.b { transform: rotate(-45deg); }

.sweep {
    position: absolute;
    width: 50%;
    height: 2px;
    left: 50%;
    top: 50%;
    transform-origin: left center;
    background: linear-gradient(90deg, var(--green), transparent);
    box-shadow: 0 0 8px var(--green);
    z-index: 4;
}

.sweep::after {
    content: "";
    position: absolute;
    left: 0;
    top: -115px;
    width: 100%;
    height: 230px;
    background: linear-gradient(0deg, transparent, rgba(0,255,120,.025));
    transform: rotate(-12deg);
    transform-origin: left center;
}

.radar-center {
    position: absolute;
    left: 50%;
    top: 50%;
    width: 8
