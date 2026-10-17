from flask import Flask, request, jsonify, Response
from collections import deque
from threading import Lock, Thread
import json
import math
import os
import time
import urllib.request
import urllib.error
import html

app = Flask(__name__)

# SEISMIC CORE X — umbrales experimentales originales
UMBRAL_VIBRACION = 0.035
UMBRAL_BRUSCO = 0.060
UMBRAL_TEMBLOR = 0.060
UMBRAL_ALERTA = 0.150
UMBRAL_ESTABLE = 0.015
TIEMPO_ESTABLE_ALERTA = 5.0
TIEMPO_FIN_EVENTO = 1.0
TIEMPO_PRECURSOR = 3.0
MUESTRAS_FILTRO = 3
MUESTRAS_CALIBRACION = 100

lock = Lock()
historial = deque(maxlen=MUESTRAS_FILTRO)
inicio_servidor = time.time()
ultimo_dato = 0.0
ultima_actividad = 0.0
inicio_evento = 0.0
inicio_movimiento_sostenido = 0.0

magnitud = 0.0
filtrada = 0.0
maximo_evento = 0.0
duracion_evento = 0.0
actual = 0.0

nivel = 1
estado = 'ESPERANDO SENSOR'
direccion = None
calibrando = True
muestras_recibidas = 0
total_eventos = 0

evento_activo = False
alerta_critica = False
precursor_enviado = False
critica_enviada = False

ultimo_envio = {
    'tipo': None,
    'ok': None,
    'detalle': 'Aún no se ha enviado ninguna alerta',
    'hora': None
}

DESTINATARIO_PREDETERMINADO = 'edwin.rojas.hansen@gmail.com'


def obtener_destinatarios():
    correos = []

    for parte in os.environ.get('ALERTAS_EMAILS', '').split(','):
        correo = parte.strip()

        if (
            correo
            and '@' in correo
            and '.' in correo.rsplit('@', 1)[-1]
            and correo.lower() not in [c.lower() for c in correos]
        ):
            correos.append(correo)

    return correos or [DESTINATARIO_PREDETERMINADO]


def enviar_correo_brevo(destinatarios, asunto, mensaje):
    api_key = os.environ.get('BREVO_API_KEY')

    if not api_key:
        return False, 'Falta BREVO_API_KEY en Render'

    if not destinatarios:
        return False, 'No hay destinatarios válidos configurados'

    payload = {
        'sender': {
            'name': 'MONITOR SÍSMICO',
            'email': 'sismoencurso@gmail.com'
        },
        'to': [{'email': c} for c in destinatarios],
        'subject': asunto,
        'htmlContent': mensaje
    }

    req = urllib.request.Request(
        'https://api.brevo.com/v3/smtp/email',
        data=json.dumps(payload).encode(),
        headers={
            'api-key': api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        },
        method='POST'
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            return resp.status in (200, 201, 202), body or f'HTTP {resp.status}'

    except urllib.error.HTTPError as e:
        return False, (
            f'Brevo HTTP {e.code}: '
            f'{e.read().decode("utf-8", errors="replace")}'
        )

    except Exception as e:
        return False, str(e)


def registrar_envio(tipo, ok, detalle):
    global ultimo_envio

    with lock:
        ultimo_envio = {
            'tipo': tipo,
            'ok': bool(ok),
            'detalle': str(detalle)[:1000],
            'hora': time.strftime(
                '%Y-%m-%d %H:%M:%S',
                time.localtime()
            )
        }


def html_alerta(tipo, valor, valor_filtrado, duracion, estado_alerta):
    color = '#ff355e' if 'CRITICA' in tipo.upper() else '#ff984a'

    return f'''<!doctype html>
<html lang="es">
<body style="background:#07111b;color:#d7f8ff;font-family:Arial,sans-serif;padding:24px">
<div style="max-width:650px;margin:auto;border:1px solid {color};padding:24px;background:#0a1b28">
<h1 style="color:#00eaff">SENSOR ACTIVO</h1>
<p style="color:#79aabb;letter-spacing:2px">MONITOREO EN CURSO · SEISMIC CORE X</p>
<h2 style="color:{color}">{html.escape(tipo)}</h2>
<p><b>Estado:</b> {html.escape(estado_alerta)}</p>
<p><b>Magnitud recibida:</b> {valor:.6f}</p>
<p><b>Señal filtrada:</b> {valor_filtrado:.6f}</p>
<p><b>Duración:</b> {duracion:.2f} s</p>
<p>Hora: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}</p>
<hr>
<small>Monitor experimental de vibraciones; no es una predicción sísmica ni una alerta oficial.</small>
</div>
</body>
</html>'''


def enviar_alerta_async(tipo, asunto, cantidad, cuerpo):
    def trabajo():
        destinatarios = obtener_destinatarios()

        if not destinatarios:
            registrar_envio(
                tipo,
                False,
                'No hay destinatarios válidos configurados'
            )
            app.logger.error(
                'Alerta %s no enviada: no hay destinatarios válidos',
                tipo
            )
            return

        resultados = []
        todo_ok = True

        for i in range(cantidad):
            sufijo = f' (aviso {i + 1} de {cantidad})' if cantidad > 1 else ''

            ok, detalle = enviar_correo_brevo(
                destinatarios,
                asunto + sufijo,
                cuerpo
            )

            resultados.append(
                f'{i + 1}: {"OK" if ok else "ERROR"} — {detalle}'
            )
            todo_ok = todo_ok and ok

        registrar_envio(tipo, todo_ok, ' | '.join(resultados))

    Thread(target=trabajo, daemon=True).start()


def num(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def obtener_magnitud(datos):
    claves = (
        'magnitud', 'magnitude', 'mag',
        'vibracion', 'vibration',
        'aceleracion', 'acceleration',
        'filtrada', 'filtered', 'valor'
    )

    for key in claves:
        if key in datos:
            return abs(num(datos[key]))

    return None


def actualizar_estado(valor, datos):
    global ultimo_dato, ultima_actividad
    global inicio_evento, inicio_movimiento_sostenido
    global magnitud, filtrada, actual
    global nivel, estado, direccion, calibrando
    global muestras_recibidas, total_eventos
    global maximo_evento, duracion_evento
    global evento_activo, alerta_critica
    global precursor_enviado, critica_enviada

    ahora = time.time()
    ultimo_dato = ahora
    muestras_recibidas += 1

    magnitud = abs(valor)
    actual = abs(num(datos.get('actual', magnitud)))

    historial.append(magnitud)
    filtrada = sum(historial) / len(historial)

    direccion = datos.get(
        'direccion',
        datos.get('direction', direccion)
    )

    if datos.get('calibrando') is False:
        calibrando = False
    elif datos.get('calibrando') is True:
        calibrando = True
    elif muestras_recibidas >= MUESTRAS_CALIBRACION:
        calibrando = False

    if calibrando:
        estado, nivel = 'CALIBRANDO SENSOR', 1
        return

    movimiento_instantaneo = magnitud >= UMBRAL_BRUSCO

    movimiento_detectado = (
        magnitud >= UMBRAL_VIBRACION
        or filtrada >= UMBRAL_VIBRACION
    )

    temblor_detectado = (
        magnitud >= UMBRAL_TEMBLOR
        or filtrada >= UMBRAL_TEMBLOR
    )

    critica_detectada = (
        magnitud >= UMBRAL_ALERTA
        or filtrada >= UMBRAL_ALERTA
    )

    if movimiento_detectado or movimiento_instantaneo:
        ultima_actividad = ahora

        if not evento_activo:
            evento_activo = True
            inicio_evento = ahora
            total_eventos += 1
            maximo_evento = 0.0
            duracion_evento = 0.0
            inicio_movimiento_sostenido = 0.0
            precursor_enviado = False
            critica_enviada = False

        maximo_evento = max(maximo_evento, magnitud, filtrada)
        duracion_evento = max(0.0, ahora - inicio_evento)

    if evento_activo and temblor_detectado:
        if inicio_movimiento_sostenido == 0.0:
            inicio_movimiento_sostenido = ahora

        if (
            ahora - inicio_movimiento_sostenido >= TIEMPO_PRECURSOR
            and not precursor_enviado
        ):
            precursor_enviado = True

            cuerpo = html_alerta(
                'MOVIMIENTO SOSTENIDO',
                magnitud,
                filtrada,
                duracion_evento,
                'MOVIMIENTO SOSTENIDO — REVISAR TELEMETRÍA'
            )

            enviar_alerta_async(
                'MOVIMIENTO_SOSTENIDO',
                'MOVIMIENTO SOSTENIDO — REVISAR TELEMETRÍA',
                2,
                cuerpo
            )
    else:
        inicio_movimiento_sostenido = 0.0

    if critica_detectada:
        nivel = 4
        estado = 'ALERTA CRITICA'
        alerta_critica = True

        if not critica_enviada:
            critica_enviada = True

            cuerpo = html_alerta(
                'ALERTA CRITICA',
                magnitud,
                filtrada,
                duracion_evento,
                'ALERTA CRITICA'
            )

            enviar_alerta_async(
                'CRITICA',
                'ALERTA CRÍTICA — ACTIVIDAD SÍSMICA INTENSA',
                1,
                cuerpo
            )

        return

    if temblor_detectado:
        if (
            alerta_critica
            and ahora - ultima_actividad < TIEMPO_ESTABLE_ALERTA
        ):
            nivel, estado = 4, 'ALERTA CRITICA'
        else:
            nivel, estado = 3, 'SISMO EN CURSO'

        return

    if movimiento_detectado or movimiento_instantaneo:
        nivel, estado = 2, 'MOVIMIENTO DETECTADO'
        return

    calma = (
        ahora - ultima_actividad
        if ultima_actividad
        else float('inf')
    )

    if evento_activo and calma >= TIEMPO_FIN_EVENTO:
        evento_activo = False
        duracion_evento = max(0.0, ahora - inicio_evento)
        inicio_evento = 0.0
        inicio_movimiento_sostenido = 0.0

    if alerta_critica:
        if calma < TIEMPO_ESTABLE_ALERTA:
            nivel, estado = 4, 'ALERTA CRITICA'
            return

        alerta_critica = False

    if evento_activo:
        nivel, estado = 2, 'MOVIMIENTO DETECTADO'
    elif filtrada <= UMBRAL_ESTABLE:
        nivel, estado = 1, 'SISTEMA ESTABLE'
        precursor_enviado = False
        critica_enviada = False
    else:
        nivel, estado = 1, 'MOVIMIENTO LEVE'


def construir_estado():
    ahora = time.time()
    conectado = bool(ultimo_dato and ahora - ultimo_dato < 15)

    estado_visible = estado if conectado else (
        'ESPERANDO SENSOR'
        if not ultimo_dato
        else 'SENSOR SIN COMUNICACION'
    )

    return {
        'ok': True,
        'conexion': conectado,
        'conexion_texto': (
            'EN LÍNEA'
            if conectado
            else (
                'ESPERANDO DATOS'
                if not ultimo_dato
                else 'SIN DATOS DEL SENSOR'
            )
        ),
        'estado': estado_visible,
        'magnitud': round(magnitud, 6),
        'filtrada': round(filtrada, 6),
        'actual': round(actual, 6),
        'nivel': nivel,
        'direccion': direccion,
        'calibrando': calibrando,
        'muestras': muestras_recibidas,
        'eventos': total_eventos,
        'maximo_evento': round(maximo_evento, 6),
        'duracion_evento': round(duracion_evento, 2),
        'evento_activo': evento_activo,
        'tiempo_activo': round(ahora - ultimo_dato, 2) if ultimo_dato else None,
        'tiempo_servidor': round(ahora - inicio_servidor, 1),
        'destinatarios_configurados': len(obtener_destinatarios()),
        'correo': dict(ultimo_envio)
    }


@app.route('/datos', methods=['POST'])
def recibir_datos():
    datos = request.get_json(silent=True)

    if not isinstance(datos, dict):
        datos = request.form.to_dict()

    if not datos:
        return jsonify({
            'ok': False,
            'error': 'No se recibieron datos JSON'
        }), 400

    valor = obtener_magnitud(datos)

    if valor is None:
        x = datos.get('x', datos.get('ax'))
        y = datos.get('y', datos.get('ay'))
        z = datos.get('z', datos.get('az'))

        if x is None or y is None or z is None:
            return jsonify({
                'ok': False,
                'error': 'Falta magnitud o ejes x, y, z'
            }), 400

        valor = abs(
            math.sqrt(num(x)**2 + num(y)**2 + num(z)**2) - 1.0
        )

    with lock:
        actualizar_estado(valor, datos)
        result = construir_estado()

    return jsonify({
        'ok': True,
        'estado': result['estado'],
        'magnitud': result['magnitud'],
        'filtrada': result['filtrada']
    })


@app.route('/estado', methods=['GET'])
def consultar_estado():
    with lock:
        return jsonify(construir_estado())


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'ok': True,
        'service': 'SEISMIC CORE X'
    })


@app.route('/probar-correo', methods=['POST'])
def probar_correo():
    token = os.environ.get('ADMIN_TOKEN')

    if token and request.headers.get('X-Admin-Token') != token:
        return jsonify({
            'ok': False,
            'error': 'No autorizado'
        }), 401

    ok, detalle = enviar_correo_brevo(
        obtener_destinatarios(),
        'PRUEBA DE CORREO — MONITOR SÍSMICO',
        '<h2>Prueba de correo</h2>'
        '<p>Prueba de envío de SEISMIC CORE X mediante Brevo. '
        'No es una alerta sísmica.</p>'
    )

    registrar_envio('PRUEBA', ok, detalle)

    return jsonify({
        'ok': ok,
        'destinatarios': len(obtener_destinatarios()),
        'detalle': detalle
    }), (200 if ok else 502)


PAGINA = r'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEISMIC CORE X</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">

<style>
:root {
    --bg: #050b12;
    --panel: rgba(7,18,29,.94);
    --line: #1b3949;
    --cyan: #64e8ff;
    --green: #63f5b0;
    --yellow: #ffd66b;
    --orange: #ffad70;
    --red: #ff5874;
    --muted: #8299a8;
}

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    min-height: 100vh;
    background: radial-gradient(
        ellipse at 48% 0%,
        #102637 0%,
        #07111c 48%,
        #03070d 100%
    );
    color: #e5f2f7;
    font-family: Inter, Segoe UI, Arial, sans-serif;
}

body:before {
    content: '';
    position: fixed;
    inset: 0;
    pointer-events: none;
    opacity: .035;
    background: repeating-linear-gradient(
        transparent 0 4px,
        rgba(100,232,255,.18) 5px
    );
    z-index: 5;
}

header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 19px 26px;
    border-bottom: 1px solid #203c4b;
    background: rgba(3,10,17,.92);
    box-shadow: 0 8px 30px #0005;
}

.logo {
    color: var(--cyan);
    font-size: clamp(22px,3vw,35px);
    font-weight: 750;
    letter-spacing: 3px;
    text-shadow: 0 0 22px #00bfff33;
}

.sub {
    color: #8da6b5;
    font-size: 10px;
    letter-spacing: 2px;
    margin-top: 6px;
}

.connection {
    font-size: 12px;
    color: var(--yellow);
    text-align: right;
}

.dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--yellow);
    margin-right: 7px;
    box-shadow: 0 0 10px currentColor;
}

main {
    padding: 16px;
    max-width: 1800px;
    margin: auto;
}

.grid {
    display: grid;
    grid-template-columns: minmax(280px,1.15fr) minmax(300px,1fr) minmax(260px,.8fr);
    gap: 14px;
}

.panel {
    position: relative;
    background: linear-gradient(
        145deg,
        rgba(12,29,43,.96),
        rgba(5,15,25,.96)
    );
    border: 1px solid #203d4d;
    border-radius: 13px;
    padding: 17px;
    min-width: 0;
    box-shadow: 0 12px 35px #0003, inset 0 1px #ffffff08;
}

.panel:before {
    content: '';
    position: absolute;
    top: -1px;
    left: 15px;
    width: 66px;
    border-top: 2px solid var(--cyan);
    opacity: .8;
}

.heading {
    font-size: 10px;
    color: #9dc8d8;
    letter-spacing: 2px;
    margin-bottom: 16px;
    font-weight: 700;
}

.radar-wrap {
    position: relative;
    width: 100%;
    max-width: 470px;
    aspect-ratio: 1;
    margin: auto;
    border-radius: 50%;
    overflow: hidden;
    background: #07111a;
    border: 1px solid rgba(99,245,176,.48);
    box-shadow:
        0 0 0 5px rgba(7,19,29,.96),
        0 0 0 6px rgba(67,202,191,.34),
        0 0 34px rgba(0,166,255,.12),
        inset 0 0 35px #0009;
}

/* Cartografía dentro del marco futurista */
#mapa {
    position: absolute;
    inset: 0;
    z-index: 1;
    background: #071722;
    isolation: isolate;
    border-radius: 50%;
    overflow: hidden;
}

/* Brillo verde y azul, sin tapar las calles */
#mapa:after {
    content: '';
    position: absolute;
    inset: 0;
    z-index: 450;
    pointer-events: none;
    background:
        radial-gradient(
            ellipse at 25% 22%,
            rgba(34,235,163,.12) 0%,
            transparent 46%
        ),
        radial-gradient(
            ellipse at 78% 76%,
            rgba(0,145,255,.16) 0%,
            transparent 58%
        ),
        linear-gradient(
            145deg,
            rgba(4,25,36,.04),
            rgba(1,9,24,.24)
        );
    mix-blend-mode: screen;
}

#mapa .leaflet-tile-pane {
    filter: saturate(1.34) hue-rotate(8deg) brightness(1.08) contrast(1.08);
    opacity: .98;
}

#radar {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    z-index: 2;
    display: block;
    background: transparent;
    pointer-events: none;
}

.leaflet-container {
    background: #071722 !important;
}

.leaflet-control-attribution {
    font-size: 7px !important;
    background: rgba(4,13,23,.72) !important;
    color: #b8d9df !important;
    border: 1px solid rgba(95,195,205,.16);
    border-radius: 6px 0 0 0;
    backdrop-filter: blur(9px);
    padding: 2px 5px !important;
}

.leaflet-control-attribution a {
    color: #81e9d0 !important;
}

.radar-label {
    text-align: center;
    color: #c8eee7;
    font-size: 11px;
    font-weight: 650;
    margin-top: 17px;
    letter-spacing: 1.8px;
