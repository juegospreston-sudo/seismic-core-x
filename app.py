from flask import Flask, request, jsonify, Response
from collections import deque
from threading import Lock
import math
import time
import os

app = Flask(__name__)

import json
import urllib.request
import urllib.error

def enviar_correo_brevo(destinatarios, asunto, mensaje):
    api_key = os.environ.get("BREVO_API_KEY")

    if not api_key:
        return False, "Falta BREVO_API_KEY en Render"

    datos = {
        "sender": {
            "name": "SISMO EN CURSO",
            "email": "sismoencurso@gmail.com"
        },
        "to": [
            {"email": correo}
            for correo in destinatarios
        ],
        "subject": asunto,
        "htmlContent": mensaje
    }

    solicitud = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(datos).encode("utf-8"),
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(solicitud, timeout=20) as respuesta:
            return respuesta.status in (200, 201, 202), respuesta.read().decode("utf-8")

    except urllib.error.HTTPError as error:
        detalle = error.read().decode("utf-8", errors="replace")
        return False, f"Brevo HTTP {error.code}: {detalle}"

    except Exception as error:
        return False, str(error)


# =========================================================
# SEISMIC CORE X - CONFIGURACION
# =========================================================

UMBRAL_VIBRACION = 0.025
UMBRAL_TEMBLOR = 0.045
UMBRAL_ALERTA = 0.120
UMBRAL_ESTABLE = 0.010

TIEMPO_ESTABLE_ALERTA = 5.0
TIEMPO_FIN_EVENTO = 1.0
MUESTRAS_FILTRO = 5

lock = Lock()
historial = deque(maxlen=MUESTRAS_FILTRO)

inicio = time.time()
ultimo_dato = 0.0
ultima_actividad = 0.0
inicio_evento = 0.0
ultimo_evento = 0.0

magnitud = 0.0
filtrada = 0.0
nivel = 1
estado = "ESPERANDO SENSOR"
direccion = None
calibrando = True
muestras_recibidas = 0
total_eventos = 0
maximo_evento = 0.0
duracion_evento = 0.0
evento_activo = False
alerta_critica = False


# =========================================================
# FUNCIONES DE PROCESAMIENTO
# =========================================================

def numero_seguro(valor, defecto=0.0):
    try:
        valor = float(valor)
        if math.isfinite(valor):
            return valor
    except (TypeError, ValueError):
        pass
    return defecto


def obtener_magnitud(datos):
    """
    Prioriza la magnitud calculada por el ESP8266.
    Admite nombres de campo habituales.
    """

    for clave in (
        "magnitud",
        "magnitude",
        "mag",
        "vibracion",
        "vibration",
        "aceleracion",
        "acceleration",
    ):
        if clave in datos:
            return abs(numero_seguro(datos[clave]))

    # Si el ESP8266 manda una magnitud filtrada,
    # también podemos utilizarla como fuente.
    for clave in ("filtrada", "filtered", "valor"):
        if clave in datos:
            return abs(numero_seguro(datos[clave]))

    return None


def actualizar_estado(valor, datos):
    global ultimo_dato, ultima_actividad, inicio_evento
    global ultimo_evento, magnitud, filtrada, nivel, estado
    global direccion, calibrando, muestras_recibidas
    global total_eventos, maximo_evento, duracion_evento
    global evento_activo, alerta_critica

    ahora = time.time()

    ultimo_dato = ahora
    muestras_recibidas += 1
    magnitud = abs(valor)

    historial.append(magnitud)
    filtrada = sum(historial) / len(historial)

    # Direccion opcional enviada por el dispositivo.
    direccion = datos.get(
        "direccion",
        datos.get("direction", direccion)
    )

    # Finaliza la calibracion inicial cuando llegan datos.
    if muestras_recibidas >= 100:
        calibrando = False

    if calibrando:
        estado = "CALIBRANDO SENSOR"
        nivel = 1
        return

    if filtrada >= UMBRAL_ALERTA:
        nivel = 4
        estado = "ALERTA CRITICA"
        alerta_critica = True
        ultima_actividad = ahora

        if not evento_activo:
            evento_activo = True
            inicio_evento = ahora
            total_eventos += 1

        ultimo_evento = ahora
        maximo_evento = max(maximo_evento, filtrada)
        duracion_evento = ahora - inicio_evento
        return

    # La alerta crítica se mantiene mientras exista
    # actividad y durante el tiempo de confirmación.
    if filtrada >= UMBRAL_TEMBLOR:
        ultima_actividad = ahora

        if not evento_activo:
            evento_activo = True
            inicio_evento = ahora
            total_eventos += 1

        ultimo_evento = ahora
        maximo_evento = max(maximo_evento, filtrada)
        duracion_evento = ahora - inicio_evento

        if alerta_critica:
            if ahora - ultima_actividad < TIEMPO_ESTABLE_ALERTA:
                estado = "ALERTA CRITICA"
                nivel = 4
                return

        if filtrada >= UMBRAL_ALERTA:
            estado = "ALERTA CRITICA"
            nivel = 4
        else:
            estado = "SISMO EN CURSO"
            nivel = 3
        return

    if filtrada >= UMBRAL_VIBRACION:
        ultima_actividad = ahora

        if not evento_activo:
            evento_activo = True
            inicio_evento = ahora
            total_eventos += 1

        ultimo_evento = ahora
        maximo_evento = max(maximo_evento, filtrada)
        duracion_evento = ahora - inicio_evento

        estado = "MOVIMIENTO DETECTADO"
        nivel = 2
        return

    # La actividad termina después de un periodo de calma.
    if evento_activo and ahora - ultima_actividad >= TIEMPO_FIN_EVENTO:
        evento_activo = False
        duracion_evento = ahora - inicio_evento
        alerta_critica = False
        maximo_evento = 0.0
        inicio_evento = 0.0

    if alerta_critica:
        if ahora - ultima_actividad < TIEMPO_ESTABLE_ALERTA:
            estado = "ALERTA CRITICA"
            nivel = 4
            return
        alerta_critica = False

    if evento_activo:
        estado = "MOVIMIENTO DETECTADO"
        nivel = 2
    elif filtrada <= UMBRAL_ESTABLE:
        estado = "SISTEMA ESTABLE"
        nivel = 1
    else:
        estado = "MOVIMIENTO LEVE"
        nivel = 1


def construir_estado():
    ahora = time.time()

    if ultimo_dato == 0:
        conexion = False
        texto_conexion = "ESPERANDO DATOS"
    else:
        conexion = ahora - ultimo_dato < 15
        texto_conexion = (
            "INTERNET CONECTADO"
            if conexion
            else "SIN DATOS DEL SENSOR"
        )

    # No declarar estabilidad si el sensor dejó de enviar datos.
    estado_actual = estado
    if not conexion:
        estado_actual = (
            "ESPERANDO SENSOR"
            if ultimo_dato == 0
            else "SENSOR SIN COMUNICACION"
        )

    return {
        "ok": True,
        "conexion": conexion,
        "conexion_texto": texto_conexion,
        "estado": estado_actual,
        "magnitud": round(magnitud, 6),
        "filtrada": round(filtrada, 6),
        "nivel": nivel,
        "direccion": direccion,
        "calibrando": calibrando,
        "muestras": muestras_recibidas,
        "eventos": total_eventos,
        "maximo_evento": round(maximo_evento, 6),
        "duracion_evento": round(duracion_evento, 2),
        "evento_activo": evento_activo,
        "tiempo_activo": round(ahora - ultimo_dato, 2)
        if ultimo_dato else None,
        "tiempo_servidor": round(ahora - inicio, 1),
    }


# =========================================================
# RECEPCION DE DATOS DEL ESP8266
# =========================================================

@app.route("/datos", methods=["POST"])
def recibir_datos():
    datos = request.get_json(silent=True)

    if not isinstance(datos, dict):
        datos = request.form.to_dict()

    if not datos:
        return jsonify({
            "ok": False,
            "error": "No se recibieron datos JSON"
        }), 400

    valor = obtener_magnitud(datos)

    if valor is None:
        # Si llegan los tres ejes, calcular la desviación
        # respecto de la gravedad aproximada de 1 g.
        x = datos.get("x", datos.get("ax"))
        y = datos.get("y", datos.get("ay"))
        z = datos.get("z", datos.get("az"))

        if x is not None and y is not None and z is not None:
            x = numero_seguro(x)
            y = numero_seguro(y)
            z = numero_seguro(z)
            valor = abs(math.sqrt(x*x + y*y + z*z) - 1.0)
        else:
            return jsonify({
                "ok": False,
                "error": "Falta magnitud o ejes x, y, z"
            }), 400

    with lock:
        actualizar_estado(valor, datos)
        resultado = construir_estado()

    return jsonify({
        "ok": True,
        "estado": resultado["estado"],
        "magnitud": resultado["magnitud"],
        "filtrada": resultado["filtrada"],
    })


# =========================================================
# CONSULTA DEL ESTADO
# =========================================================

@app.route("/estado", methods=["GET"])
def consultar_estado():
    with lock:
        return jsonify(construir_estado())


# =========================================================
# PAGINA WEB HUD
# HTML DIRECTO: NO SE PROCESA CON JINJA2
# =========================================================

PAGINA = r"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SEISMIC CORE X</title>
<style>
:root {
  color-scheme: dark;
  --bg: #030910;
  --panel: rgba(5, 24, 38, .88);
  --line: #12445a;
  --cyan: #00eaff;
  --green: #39ff9b;
  --yellow: #ffe66d;
  --orange: #ff984a;
  --red: #ff355e;
  --muted: #6e9aab;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  background:
    radial-gradient(ellipse at center, #0a2332 0%, #030910 68%),
    var(--bg);
  color: #d7f8ff;
  font-family: Consolas, "Courier New", monospace;
  overflow-x: hidden;
}
body:before {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  opacity: .12;
  background: repeating-linear-gradient(
    transparent 0 3px,
    rgba(0,234,255,.12) 4px
  );
  z-index: 5;
}
header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 17px 22px;
  border-bottom: 1px solid var(--line);
  background: rgba(2,10,18,.92);
}
.logo {
  color: var(--cyan);
  font-size: clamp(20px, 3vw, 34px);
  font-weight: bold;
  letter-spacing: 4px;
  text-shadow: 0 0 16px #00bfff;
}
.sub { color: var(--muted); font-size: 10px; letter-spacing: 3px; margin-top: 5px; }
.connection { font-size: 12px; color: var(--yellow); text-align: right; }
.dot {
  display: inline-block; width: 8px; height: 8px;
  border-radius: 50%; background: var(--yellow);
  margin-right: 7px; box-shadow: 0 0 10px currentColor;
}
main { padding: 14px; max-width: 1800px; margin: auto; }
.grid {
  display: grid;
  grid-template-columns: minmax(280px, 1.15fr) minmax(300px, 1fr) minmax(260px, .8fr);
  gap: 12px;
}
.panel {
  position: relative;
  background: var(--panel);
  border: 1px solid var(--line);
  padding: 15px;
  min-width: 0;
  box-shadow: inset 0 0 22px rgba(0,190,255,.035);
}
.panel:before {
  content: ""; position: absolute; top: -1px; left: 15px;
  width: 75px; border-top: 2px solid var(--cyan);
}
.heading {
  font-size: 11px; color: var(--cyan);
  letter-spacing: 2px; margin-bottom: 15px;
}
.radar-wrap {
  width: 100%; max-width: 470px; aspect-ratio: 1;
  margin: auto; position: relative;
}
#radar {
  display: block; width: 100%; height: 100%;
}
.radar-label {
  text-align: center; color: var(--muted);
  font-size: 10px; margin-top: 8px; letter-spacing: 2px;
}
.status {
  border: 1px solid var(--green); color: var(--green);
  text-align: center; padding: 15px 7px; font-size: clamp(13px, 1.8vw, 19px);
  font-weight: bold; margin-bottom: 12px;
  text-shadow: 0 0 12px currentColor;
}
.status.level2 { color: var(--yellow); border-color: var(--yellow); }
.status.level3 { color: var(--orange); border-color: var(--orange); }
.status.level4 { color: var(--red); border-color: var(--red); animation: blink .7s infinite; }
.status.offline { color: var(--muted); border-color: var(--muted); }
@keyframes blink { 50% { opacity: .48; } }
.metrics {
  display: grid; grid-template-columns: 1fr 1fr; gap: 9px;
}
.metric {
  background: rgba(0,10,20,.7); border: 1px solid #103346;
  padding: 12px 10px; min-height: 83px;
}
.metric .label { color: var(--muted); font-size: 10px; letter-spacing: 1px; }
.metric .value { color: var(--cyan); font-size: clamp(20px, 2.4vw, 29px); margin-top: 10px; }
.metric .unit { color: var(--muted); font-size: 10px; }
.big-number { color: var(--green); font-size: 38px; }
.bar {
  height: 7px; background: #102c3b; margin-top: 9px; overflow: hidden;
}
.bar-inner { height: 100%; width: 0%; background: var(--cyan); transition: width .25s; }
.info-row {
  display: flex; justify-content: space-between; gap: 12px;
  padding: 11px 0; border-bottom: 1px solid #123040;
  font-size: 11px;
}
.info-row span:first-child { color: var(--muted); }
.info-row span:last-child { text-align: right; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 13px; }
button {
  background: #071b28; color: var(--cyan); border: 1px solid #176078;
  padding: 10px 12px; font: inherit; font-size: 10px;
  cursor: pointer; letter-spacing: 1px;
}
button:hover { background: #0c3040; }
.timeline { margin-top: 12px; }
#history {
  width: 100%; height: 95px; display: block;
  background: #03111b; border: 1px solid #103346;
}
.footer {
  color: #527d8c; font-size: 10px; text-align: center;
  padding: 18px 5px 8px; letter-spacing: 1px;
}
@media (max-width: 1050px) {
  .grid { grid-template-columns: 1fr 1fr; }
  .side-panel { grid-column: 1 / -1; }
}
@media (max-width: 680px) {
  header { align-items: flex-start; flex-direction: column; }
  .connection { text-align: left; }
  .grid { grid-template-columns: 1fr; }
  .side-panel { grid-column: auto; }
  main { padding: 8px; }
}
</style>
</head>
<body>
<header>
  <div>
    <div class="logo">SEISMIC CORE X</div>
    <div class="sub">VIBRATION MONITORING SYSTEM / LIVE TELEMETRY</div>
  </div>
  <div class="connection">
    <div><span class="dot" id="connDot"></span><span id="connText">CONECTANDO...</span></div>
    <div class="sub" id="clock">--:--:--</div>
  </div>
</header>

<main>
<div class="grid">
  <section class="panel">
    <div class="heading">01 / RADAR DE ACTIVIDAD</div>
    <div class="radar-wrap">
      <canvas id="radar" width="600" height="600"></canvas>
    </div>
    <div class="radar-label">RADIAL SCAN // LIVE</div>
    <div class="controls">
      <button id="soundButton">ACTIVAR SONIDO</button>
      <button id="pauseButton">PAUSAR BIP</button>
    </div>
  </section>

  <section class="panel">
    <div class="heading">02 / ESTADO DEL SISTEMA</div>
    <div id="status" class="status">ESPERANDO SENSOR</div>
    <div class="metrics">
      <div class="metric">
        <div class="label">MAGNITUD</div>
        <div class="value" id="magnitude">0.000000</div>
        <div class="unit">VALOR RECIBIDO</div>
      </div>
      <div class="metric">
        <div class="label">SEÑAL FILTRADA</div>
        <div class="value" id="filtered">0.000000</div>
        <div class="unit">PROMEDIO MOVIL</div>
      </div>
      <div class="metric">
        <div class="label">NIVEL</div>
        <div class="value" id="level">1</div>
        <div class="unit">CLASIFICACION INTERNA</div>
      </div>
      <div class="metric">
        <div class="label">EVENTOS</div>
        <div class="value" id="events">0</div>
        <div class="unit">CONTADOR DE EVENTOS</div>
      </div>
    </div>
    <div class="timeline">
      <div class="info-row"><span>INTENSIDAD RELATIVA</span><span id="percent">0%</span></div>
      <div class="bar"><div class="bar-inner" id="bar"></div></div>
    </div>
  </section>

  <section class="panel side-panel">
    <div class="heading">03 / TELEMETRIA</div>
    <div class="info-row"><span>COMUNICACION</span><span id="network">ESPERANDO</span></div>
    <div class="info-row"><span>CALIBRACION</span><span id="calibration">PENDIENTE</span></div>
    <div class="info-row"><span>DIRECCION</span><span id="direction">--</span></div>
    <div class="info-row"><span>PICO DEL EVENTO</span><span id="peak">0.000000</span></div>
    <div class="info-row"><span>DURACION</span><span id="duration">0.00 s</span></div>
    <div class="info-row"><span>ULTIMA SEÑAL</span><span id="lastSignal">--</span></div>
    <div class="info-row"><span>UMBRAL MOVIMIENTO</span><span>0.025</span></div>
    <div class="info-row"><span>UMBRAL TEMBLOR</span><span>0.045</span></div>
    <div class="info-row"><span>UMBRAL CRITICO</span><span>0.120</span></div>
    <div class="heading" style="margin-top:20px">04 / SEÑAL EN VIVO</div>
    <canvas id="history" width="700" height="180"></canvas>
    <div class="info-row"><span>MUESTRAS RECIBIDAS</span><span id="samples">0</span></div>
  </section>
</div>
<div class="footer">SEISMIC CORE X // TELEMETRIA EXPERIMENTAL // NO ES UN SISTEMA OFICIAL DE ALERTA SISMICA</div>
</main>

<script>
"use strict";

const el = id => document.getElementById(id);
const radar = el("radar");
const ctx = radar.getContext("2d");
const historyCanvas = el("history");
const hctx = historyCanvas.getContext("2d");

let angle = 0;
let soundEnabled = false;
let soundPaused = false;
let audioContext = null;
let lastSweepBucket = -1;
let currentLevel = 1;
let currentMagnitude = 0;
let signalHistory = Array(120).fill(0);
let lastSuccessfulPoll = 0;
let lastStatus = "";

function colorForLevel(level) {
  if (level >= 4) return "#ff355e";
  if (level === 3) return "#ff984a";
  if (level === 2) return "#ffe66d";
  return "#39ff9b";
}

function beep(frequency, duration, volume) {
  if (!soundEnabled || soundPaused) return;
  try {
    if (!audioContext) {
      audioContext = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (audioContext.state === "suspended") audioContext.resume();

    const osc = audioContext.createOscillator();
    const gain = audioContext.createGain();
    osc.type = "sine";
    osc.frequency.value = frequency;
    gain.gain.setValueAtTime(volume, audioContext.currentTime);
    gain.gain.exponentialRampToValueAtTime(
      0.001, audioContext.currentTime + duration
    );
    osc.connect(gain);
    gain.connect(audioContext.destination);
    osc.start();
    osc.stop(audioContext.currentTime + duration);
  } catch (e) {
    console.warn("Audio no disponible", e);
  }
}

el("soundButton").addEventListener("click", async () => {
  soundEnabled = true;
  soundPaused = false;
  if (!audioContext) {
    audioContext = new (window.AudioContext || window.webkitAudioContext)();
  }
  if (audioContext.state === "suspended") await audioContext.resume();
  beep(880, 0.12, 0.08);
  el("soundButton").textContent = "SONIDO ACTIVADO";
});

el("pauseButton").addEventListener("click", () => {
  soundPaused = !soundPaused;
  el("pauseButton").textContent = soundPaused ? "REANUDAR BIP" : "PAUSAR BIP";
});

function drawRadar() {
  const w = radar.width;
  const h = radar.height;
  const cx = w / 2;
  const cy = h / 2;
  const r = Math.min(w, h) * 0.43;

  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#03111a";
  ctx.fillRect(0, 0, w, h);

  ctx.strokeStyle = "rgba(0,234,255,.23)";
  ctx.lineWidth = 1;

  for (let i = 1; i <= 4; i++) {
    ctx.beginPath();
    ctx.arc(cx, cy, r * i / 4, 0, Math.PI * 2);
    ctx.stroke();
  }

  ctx.beginPath();
  ctx.moveTo(cx - r, cy);
  ctx.lineTo(cx + r, cy);
  ctx.moveTo(cx, cy - r);
  ctx.lineTo(cx, cy + r);
  ctx.stroke();

  ctx.fillStyle = "#6e9aab";
  ctx.font = "13px Consolas";
  ctx.fillText("N", cx - 5, cy - r - 12);
  ctx.fillText("E", cx + r + 8, cy + 5);
  ctx.fillText("S", cx - 4, cy + r + 24);
  ctx.fillText("W", cx - r - 23, cy + 5);

  // Barrido y estela del radar.
  const sweep = angle;
  const gradient = ctx.createConicGradient(sweep, cx, cy);
  gradient.addColorStop(0, "rgba(0,255,220,.32)");
  gradient.addColorStop(0.13, "rgba(0,255,220,.08)");
  gradient.addColorStop(0.22, "rgba(0,255,220,0)");
  gradient.addColorStop(1, "rgba(0,255,220,0)");
  ctx.fillStyle = gradient;
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.closePath();
  ctx.fill();

  ctx.strokeStyle = "#00eaff";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(cx + Math.cos(sweep) * r, cy + Math.sin(sweep) * r);
  ctx.stroke();

  // Punto de actividad: posición visual estable, color según nivel.
  if (currentMagnitude >= 0.025) {
    const p = Math.min(0.88, Math.max(0.12, currentMagnitude / 0.2));
    const px = cx + Math.cos(-0.62) * r * p;
    const py = cy + Math.sin(-0.62) * r * p;
    const color = colorForLevel(currentLevel);

    ctx.save();
    ctx.shadowBlur = 24;
    ctx.shadowColor = color;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(px, py, currentLevel >= 3 ? 9 : 6, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();

    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.arc(px, py, currentLevel >= 3 ? 17 : 12, 0, Math.PI * 2);
    ctx.stroke();
  }

  angle += 0.012;

  const bucket = Math.floor(angle / (Math.PI * 2));
  if (bucket !== lastSweepBucket) {
    lastSweepBucket = bucket;
    beep(660, 0.07, 0.025);
  }

  requestAnimationFrame(drawRadar);
}

function drawHistory() {
  const w = historyCanvas.width;
  const h = historyCanvas.height;
  hctx.clearRect(0, 0, w, h);
  hctx.fillStyle = "#03111b";
  hctx.fillRect(0, 0, w, h);

  hctx.strokeStyle = "#123040";
  hctx.lineWidth = 1;
  for (let y = 30; y < h; y += 30) {
    hctx.beginPath();
    hctx.moveTo(0, y);
    hctx.lineTo(w, y);
    hctx.stroke();
  }

  hctx.strokeStyle = colorForLevel(currentLevel);
  hctx.lineWidth = 2;
  hctx.beginPath();
  signalHistory.forEach((v, i) => {
    const x = i * w / (signalHistory.length - 1);
    const y = h - 8 - Math.min(1, Math.abs(v) / 0.2) * (h - 16);
    if (i === 0) hctx.moveTo(x, y);
    else hctx.lineTo(x, y);
  });
  hctx.stroke();
}

async function refreshState() {
  try {
    const response = await fetch("/estado", {cache: "no-store"});
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    lastSuccessfulPoll = Date.now();

    const connected = Boolean(data.conexion);
    el("connText").textContent = data.conexion_texto || (connected ? "CONECTADO" : "SIN DATOS");
    el("connDot").style.background = connected ? "#39ff9b" : "#ff355e";
    el("connDot").style.color = connected ? "#39ff9b" : "#ff355e";
    el("network").textContent = connected ? "EN LINEA" : "SIN COMUNICACION";

    const status = el("status");
    status.textContent = data.estado || "ESPERANDO SENSOR";
    status.className = "status";
    currentLevel = Number(data.nivel || 1);

    if (!connected) status.classList.add("offline");
    else if (currentLevel === 2) status.classList.add("level2");
    else if (currentLevel === 3) status.classList.add("level3");
    else if (currentLevel >= 4) status.classList.add("level4");

    currentMagnitude = Number(data.filtrada || 0);
    el("magnitude").textContent = Number(data.magnitud || 0).toFixed(6);
    el("filtered").textContent = currentMagnitude.toFixed(6);
    el("level").textContent = currentLevel;
    el("events").textContent = data.eventos || 0;
    el("calibration").textContent = data.calibrando ? "EN CURSO" : "COMPLETADA";
    el("direction").textContent = data.direccion || "--";
    el("peak").textContent = Number(data.maximo_evento || 0).toFixed(6);
    el("duration").textContent = Number(data.duracion_evento || 0).toFixed(2) + " s";
    el("samples").textContent = data.muestras || 0;
    el("lastSignal").textContent = data.tiempo_activo == null ? "--" : Number(data.tiempo_activo).toFixed(1) + " s";

    const pct = Math.min(100, Math.max(0, currentMagnitude / 0.2 * 100));
    el("percent").textContent = pct.toFixed(1) + "%";
    el("bar").style.width = pct + "%";
    el("bar").style.background = colorForLevel(currentLevel);

    signalHistory.push(currentMagnitude);
    if (signalHistory.length > 120) signalHistory.shift();
    drawHistory();

    if (lastStatus && lastStatus !== data.estado && soundEnabled) {
      if (currentLevel >= 4) beep(180, 0.45, 0.12);
      else if (currentLevel === 3) beep(300, 0.22, 0.08);
      else if (currentLevel === 2) beep(460, 0.12, 0.05);
      else beep(720, 0.08, 0.025);
    }
    lastStatus = data.estado || "";
  } catch (error) {
    el("connText").textContent = "ERROR DE CONEXION";
    el("connDot").style.background = "#ff355e";
    el("network").textContent = "ERROR";
    console.error("No se pudo consultar /estado:", error);
  }
}

function updateClock() {
  el("clock").textContent = new Date().toLocaleTimeString("es-CL", {hour12: false});
}

updateClock();
setInterval(updateClock, 1000);
setInterval(refreshState, 500);
drawRadar();
drawHistory();
refreshState();
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    # Se sirve como HTML sin procesamiento de Jinja2.
    return Response(PAGINA, mimetype="text/html; charset=utf-8")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "SEISMIC CORE X"})


if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=puerto)
