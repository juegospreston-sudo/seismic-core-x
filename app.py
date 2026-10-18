from flask import Flask, request, jsonify, Response
from flask_sock import Sock
from collections import deque
from threading import Lock, Thread
import json, math, os, time, urllib.request, urllib.error, html

app = Flask(__name__)
sock = Sock(app)

# Clientes WebSocket conectados: cada socket tiene su propio bloqueo de envío.
clientes_ws = {}
clientes_ws_lock = Lock()

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
ultimo_dato = ultima_actividad = inicio_evento = inicio_movimiento_sostenido = 0.0
magnitud = filtrada = maximo_evento = duracion_evento = actual = 0.0
nivel, estado, direccion = 1, 'ESPERANDO SENSOR', None
calibrando = True
muestras_recibidas = total_eventos = 0
evento_activo = alerta_critica = precursor_enviado = critica_enviada = False
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
    color = (
        '#ff355e'
        if 'CRITICA' in tipo.upper()
        else '#ff984a'
    )

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
            sufijo = (
                f' (aviso {i + 1} de {cantidad})'
                if cantidad > 1 else ''
            )

            ok, detalle = enviar_correo_brevo(
                destinatarios,
                asunto + sufijo,
                cuerpo
            )

            resultados.append(
                f'{i + 1}: {"OK" if ok else "ERROR"} — {detalle}'
            )
            todo_ok = todo_ok and ok

        registrar_envio(
            tipo,
            todo_ok,
            ' | '.join(resultados)
        )

    Thread(target=trabajo, daemon=True).start()


def num(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def obtener_magnitud(datos):
    for key in (
        'magnitud', 'magnitude', 'mag',
        'vibracion', 'vibration',
        'aceleracion', 'acceleration',
        'filtrada', 'filtered', 'valor'
    ):
        if key in datos:
            return abs(num(datos[key]))

    return None


def actualizar_estado(valor, datos):
    global ultimo_dato, ultima_actividad, inicio_evento
    global inicio_movimiento_sostenido
    global magnitud, filtrada, actual, nivel, estado
    global direccion, calibrando, muestras_recibidas
    global total_eventos, maximo_evento, duracion_evento
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

        maximo_evento = max(
            maximo_evento,
            magnitud,
            filtrada
        )

        duracion_evento = max(
            0.0,
            ahora - inicio_evento
        )

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
        duracion_evento = max(
            0.0,
            ahora - inicio_evento
        )
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

    conectado = bool(
        ultimo_dato and ahora - ultimo_dato < 15
    )

    estado_visible = (
        estado
        if conectado
        else (
            'ESPERANDO SENSOR'
            if not ultimo_dato
            else 'SENSOR SIN COMUNICACION'
        )
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
        'tiempo_activo': (
            round(ahora - ultimo_dato, 2)
            if ultimo_dato else None
        ),
        'tiempo_servidor': round(
            ahora - inicio_servidor, 1
        ),
        'destinatarios_configurados': len(
            obtener_destinatarios()
        ),
        'correo': dict(ultimo_envio)
    }



def difundir_estado(estado_actual):
    """Envía el estado actual a todos los paneles y sensores conectados."""
    mensaje = json.dumps(estado_actual, ensure_ascii=False)
    with clientes_ws_lock:
        clientes = list(clientes_ws.items())

    desconectados = []
    for ws, send_lock in clientes:
        try:
            with send_lock:
                ws.send(mensaje)
        except Exception:
            desconectados.append(ws)

    if desconectados:
        with clientes_ws_lock:
            for ws in desconectados:
                clientes_ws.pop(ws, None)


def interpretar_muestra(datos):
    """Obtiene la magnitud desde magnitud/vibración o desde los ejes x, y, z."""
    valor = obtener_magnitud(datos)
    if valor is not None:
        return valor

    x = datos.get('x', datos.get('ax'))
    y = datos.get('y', datos.get('ay'))
    z = datos.get('z', datos.get('az'))

    if x is None or y is None or z is None:
        return None

    return abs(math.sqrt(num(x)**2 + num(y)**2 + num(z)**2) - 1.0)


@sock.route('/ws')
def websocket_seismic(ws):
    """Canal WebSocket para recibir telemetría y difundir el estado en vivo."""
    send_lock = Lock()
    with clientes_ws_lock:
        clientes_ws[ws] = send_lock

    try:
        with lock:
            estado_inicial = construir_estado()
        with send_lock:
            ws.send(json.dumps(estado_inicial, ensure_ascii=False))

        while True:
            mensaje = ws.receive()
            if mensaje is None:
                break

            try:
                datos = json.loads(mensaje)
            except (TypeError, json.JSONDecodeError):
                with send_lock:
                    ws.send(json.dumps({
                        'ok': False,
                        'error': 'Mensaje inválido: se esperaba JSON'
                    }, ensure_ascii=False))
                continue

            if not isinstance(datos, dict):
                with send_lock:
                    ws.send(json.dumps({
                        'ok': False,
                        'error': 'El mensaje JSON debe ser un objeto'
                    }, ensure_ascii=False))
                continue

            # Los paneles envían heartbeat para actualizar el estado de conexión.
            if datos.get('tipo') == 'heartbeat':
                with lock:
                    estado_actual = construir_estado()
                difundir_estado(estado_actual)
                continue

            valor = interpretar_muestra(datos)
            if valor is None:
                with send_lock:
                    ws.send(json.dumps({
                        'ok': False,
                        'error': 'Falta magnitud o ejes x, y, z'
                    }, ensure_ascii=False))
                continue

            with lock:
                actualizar_estado(valor, datos)
                estado_actual = construir_estado()

            difundir_estado(estado_actual)

    except Exception as e:
        app.logger.info('Conexión WebSocket cerrada: %s', e)
    finally:
        with clientes_ws_lock:
            clientes_ws.pop(ws, None)


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
            math.sqrt(
                num(x)**2 + num(y)**2 + num(z)**2
            ) - 1.0
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
:root{--bg:#050b12;--panel:rgba(7,18,29,.94);--line:#1b3949;--cyan:#64e8ff;--green:#63f5b0;--yellow:#ffd66b;--orange:#ffad70;--red:#ff5874;--muted:#8299a8}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 48% 0%,#102637 0%,#07111c 48%,#03070d 100%);color:#e5f2f7;font-family:Inter,Segoe UI,Arial,sans-serif}
body:before{content:'';position:fixed;inset:0;pointer-events:none;opacity:.035;background:repeating-linear-gradient(transparent 0 4px,rgba(100,232,255,.18) 5px);z-index:5}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:19px 26px;border-bottom:1px solid #203c4b;background:rgba(3,10,17,.92);box-shadow:0 8px 30px #0005}
.logo{color:var(--cyan);font-size:clamp(22px,3vw,35px);font-weight:750;letter-spacing:3px;text-shadow:0 0 22px #00bfff33}
.sub{color:#8da6b5;font-size:10px;letter-spacing:2px;margin-top:6px}
.connection{font-size:12px;color:var(--yellow);text-align:right}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--yellow);margin-right:7px;box-shadow:0 0 10px currentColor}
main{padding:16px;max-width:1800px;margin:auto}
.grid{display:grid;grid-template-columns:minmax(280px,1.15fr) minmax(300px,1fr) minmax(260px,.8fr);gap:14px}
.panel{position:relative;background:linear-gradient(145deg,rgba(12,29,43,.96),rgba(5,15,25,.96));border:1px solid #203d4d;border-radius:13px;padding:17px;min-width:0;box-shadow:0 12px 35px #0003,inset 0 1px #ffffff08}
.panel:before{content:'';position:absolute;top:-1px;left:15px;width:66px;border-top:2px solid var(--cyan);opacity:.8}
.heading{font-size:10px;color:#9dc8d8;letter-spacing:2px;margin-bottom:16px;font-weight:700}
.radar-wrap{position:relative;width:100%;max-width:470px;aspect-ratio:1;margin:auto;border-radius:50%;overflow:hidden;background:rgba(3,15,12,.95);border:1px solid rgba(99,245,176,.65);box-shadow:0 0 0 5px rgba(4,19,15,.96),0 0 0 7px rgba(57,255,155,.35),0 0 45px rgba(57,255,155,.22),inset 0 0 40px rgba(0,0,0,.85)}
#mapa{position:absolute;inset:0;z-index:1;background:#03120d;isolation:isolate;border-radius:50%;overflow:hidden}
/* Efecto translúcido y más transparente en las tesinas del mapa */
#mapa:after{content:'';position:absolute;inset:0;z-index:450;pointer-events:none;background:radial-gradient(ellipse at 40% 40%,rgba(63,245,176,.15) 0%,transparent 65%),linear-gradient(145deg,rgba(2,24,18,.15),rgba(0,8,6,.35));mix-blend-mode:screen}
#mapa .leaflet-tile-pane{filter:invert(0.9) hue-rotate(185deg) brightness(0.85) contrast(1.4) saturate(1.2);opacity:.78}
#radar{position:absolute;inset:0;width:100%;height:100%;z-index:2;display:block;background:transparent;pointer-events:none}
.leaflet-container{background:#03120d!important}
.leaflet-control-attribution{font-size:7px!important;background:rgba(3,17,12,.72)!important;color:#b8dfd1!important;border:1px solid rgba(95,205,175,.16);border-radius:6px 0 0 0;backdrop-filter:blur(9px);padding:2px 5px!important}
.leaflet-control-attribution a{color:#81e9d0!important}
.street-text-label{background:transparent;border:none;box-shadow:none;color:#ffffff;font:700 11px Inter,Arial,sans-serif;letter-spacing:.8px;text-shadow:0 0 5px #000,0 0 9px #000,0 0 12px #39ff9b;white-space:nowrap}
.radar-label{text-align:center;color:#c8eee7;font-size:12px;font-weight:700;margin-top:17px;letter-spacing:1.8px;text-shadow:0 0 14px rgba(45,220,180,.16)}
.status{border:1px solid var(--green);color:var(--green);text-align:center;padding:15px 7px;font-size:clamp(13px,1.8vw,19px);font-weight:bold;margin-bottom:12px;text-shadow:0 0 12px currentColor}
.level2{color:var(--yellow)!important;border-color:var(--yellow)!important}
.level3{color:var(--orange)!important;border-color:var(--orange)!important}
.level4{color:var(--red)!important;border-color:var(--red)!important;animation:blink .7s infinite}
.offline{color:var(--muted)!important;border-color:var(--muted)!important}
@keyframes blink{50%{opacity:.48}}
.metrics{display:grid;grid-template-columns:1fr 1fr;gap:9px}
.metric{background:#000a14b3;border:1px solid #103346;padding:12px 10px;min-height:83px}
.label{color:var(--muted);font-size:10px;letter-spacing:1px}
.value{color:var(--cyan);font-size:clamp(20px,2.4vw,29px);margin-top:10px}
.unit{color:var(--muted);font-size:10px}
.bar{height:7px;background:#102c3b;margin-top:9px}
.bar-inner{height:100%;width:0;background:var(--cyan);transition:width .25s}
.info-row{display:flex;justify-content:space-between;gap:12px;padding:11px 0;border-bottom:1px solid #123040;font-size:11px}
.info-row span:first-child{color:var(--muted)}
.info-row span:last-child{text-align:right;overflow-wrap:anywhere}
.clock-panel{display:flex;align-items:center;justify-content:center;gap:16px;margin:13px auto 0;padding:12px;border:1px solid var(--line);background:#000a14b3;color:var(--muted);font-size:10px;letter-spacing:2px}
.clock-panel strong{color:var(--cyan);font-size:clamp(20px,2.5vw,28px);letter-spacing:3px;text-shadow:0 0 12px #00eaff55;font-weight:600}
#history{width:100%;height:100px;display:block;background:#03111b;border:1px solid #103346}
.footer{color:#527d8c;font-size:10px;text-align:center;padding:18px 5px 8px;letter-spacing:1px}
@media(max-width:1050px){.grid{grid-template-columns:1fr 1fr}.side{grid-column:1/-1}}
@media(max-width:680px){header{align-items:flex-start;flex-direction:column}.connection{text-align:left}.grid{grid-template-columns:1fr}.side{grid-column:auto}main{padding:8px}}
</style>
</head>
<body>
<header>
<div>
<div class="logo">SEISMIC CORE X</div>
<div class="sub">EL SISTEMA ESTÁ DETECTANDO · TELEMETRÍA EN VIVO</div>
</div>
<div class="connection">
<div><span class="dot" id="dot"></span><span id="conn">EN LÍNEA</span></div>
</div>
</header>

<main>
<div class="grid">
<section class="panel">
<div class="heading">01 / RADAR DE ACTIVIDAD</div>
<div class="radar-wrap">
<div id="mapa"></div>
<canvas id="radar" width="600" height="600"></canvas>
</div>
<div class="radar-label">SENSOR ACTIVO</div>
<div class="clock-panel"><strong id="clock">--:--:--</strong></div>
</section>

<section class="panel">
<div class="heading">02 / ESTADO DEL SISTEMA</div>
<div id="status" class="status">ESPERANDO SENSOR</div>
<div class="metrics">
<div class="metric">
<div class="label">MAGNITUD</div>
<div class="value" id="mag">0.000000</div>
<div class="unit">VALOR RECIBIDO</div>
</div>
<div class="metric">
<div class="label">SEÑAL FILTRADA</div>
<div class="value" id="fil">0.000000</div>
<div class="unit">PROMEDIO MÓVIL</div>
</div>
<div class="metric">
<div class="label">NIVEL</div>
<div class="value" id="lvl">1</div>
<div class="unit">CLASIFICACIÓN INTERNA</div>
</div>
<div class="metric">
<div class="label">EVENTOS</div>
<div class="value" id="events">0</div>
<div class="unit">CONTADOR DE EVENTOS</div>
</div>
</div>
<div class="info-row"><span>INTENSIDAD RELATIVA</span><span id="pct">0%</span></div>
<div class="bar"><div class="bar-inner" id="bar"></div></div>
</section>

<section class="panel side">
<div class="heading">03 / TELEMETRÍA</div>
<div class="info-row"><span>COMUNICACIÓN</span><span id="network">ESPERANDO</span></div>
<div class="info-row"><span>CALIBRACIÓN</span><span id="cal">PENDIENTE</span></div>
<div class="info-row"><span>DIRECCIÓN</span><span id="dir">--</span></div>
<div class="info-row"><span>PICO DEL EVENTO</span><span id="peak">0.000000</span></div>
<div class="info-row"><span>DURACIÓN</span><span id="dur">0.00 s</span></div>
<div class="info-row"><span>ÚLTIMA SEÑAL</span><span id="age">--</span></div>
<div class="info-row"><span>UMBRAL MOVIMIENTO</span><span>0.035</span></div>
<div class="info-row"><span>UMBRAL BRUSCO</span><span>0.060</span></div>
<div class="info-row"><span>UMBRAL ACTIVIDAD SOSTENIDA</span><span>0.060 / 3 s</span></div>
<div class="info-row"><span>UMBRAL CRÍTICO</span><span>0.150</span></div>
<div class="heading" style="margin-top:20px">04 / SEÑAL EN VIVO</div>
<canvas id="history" width="700" height="180"></canvas>
<div class="info-row"><span>MUESTRAS RECIBIDAS</span><span id="samples">0</span></div>
<div class="info-row"><span>DESTINATARIOS</span><span id="recipients">0</span></div>
<div class="info-row"><span>ÚLTIMO CORREO</span><span id="mail">SIN ENVÍOS</span></div>
</section>
</div>
<div class="footer">SEISMIC CORE X // TELEMETRÍA EXPERIMENTAL // NO ES UN SISTEMA OFICIAL DE ALERTA SÍSMICA</div>
</main>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const $=id=>document.getElementById(id);

const mapa=L.map('mapa',{
    zoomControl:false,
    attributionControl:false,
    dragging:false,
    scrollWheelZoom:false,
    doubleClickZoom:false,
    touchZoom:false,
    keyboard:false
}).setView([-33.4937,-70.7474],15);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{
    maxZoom:19,
    crossOrigin:true
}).addTo(mapa);

L.marker([-33.4937,-70.7474],{
    icon:L.divIcon({
        className:'',
        html:'<div style="width:2px;height:2px;"></div>',
        iconSize:[2,2],
        iconAnchor:[1,1]
    })
}).addTo(mapa).bindTooltip('PUERTO CÁRDENAS',{
    permanent:true,
    direction:'top',
    offset:[0,-2],
    className:'street-text-label'
});

setTimeout(()=>mapa.invalidateSize(),500);

const rc=$('radar').getContext('2d');
const hc=$('history').getContext('2d');

let angle=0;
let enabled=false;
let paused=false;
let audio=null;
let lastSweep=-1;
let level=1;
let mag=0;
let radarMag=0;
let history=Array(120).fill(0);
let lastStatus='';

function color(l){
    return l >= 4 ? '#ff355e' :
           l === 3 ? '#ff984a' :
           l === 2 ? '#ffe66d' :
           '#39ff9b';
}

function dibujarRadar(){
    const w = $('radar').width;
    const h = $('radar').height;
    const cx = w / 2;
    const cy = h / 2;
    const r = Math.min(w, h) * 0.47;

    rc.clearRect(0, 0, w, h);

    // Anillos del radar
    rc.save();
    rc.strokeStyle = 'rgba(57,255,155,0.25)';
    rc.lineWidth = 1;

    for(let i = 1; i <= 4; i++){
        rc.beginPath();
        rc.arc(cx, cy, r * i / 4, 0, Math.PI * 2);
        rc.stroke();
    }

    rc.beginPath();
    rc.moveTo(cx - r, cy);
    rc.lineTo(cx + r, cy);
    rc.moveTo(cx, cy - r);
    rc.lineTo(cx, cy + r);
    rc.stroke();

    // Barrido giratorio
    const grad = rc.createRadialGradient(cx, cy, 0, cx, cy, r);
    grad.addColorStop(0, 'rgba(57,255,155,0.02)');
    grad.addColorStop(1, 'rgba(57,255,155,0.20)');

    rc.beginPath();
    rc.moveTo(cx, cy);
    rc.arc(cx, cy, r, angle - 0.45, angle);
    rc.closePath();
    rc.fillStyle = grad;
    rc.fill();

    rc.beginPath();
    rc.moveTo(cx, cy);
    rc.lineTo(
        cx + Math.cos(angle) * r,
        cy + Math.sin(angle) * r
    );
    rc.strokeStyle = 'rgba(99,255,180,0.95)';
    rc.lineWidth = 2;
    rc.shadowColor = '#39ff9b';
    rc.shadowBlur = 12;
    rc.stroke();

    // Punto de actividad cuando existe movimiento
    if(enabled && radarMag > 0.001){
        const intensidad = Math.min(radarMag / 0.15, 1);
        const distancia = r * (0.2 + intensidad * 0.65);
        const px = cx + Math.cos(angle * 0.7) * distancia;
        const py = cy + Math.sin(angle * 0.7) * distancia;
        const c = color(level);

        rc.beginPath();
        rc.arc(px, py, 5 + intensidad * 7, 0, Math.PI * 2);
        rc.fillStyle = c;
        rc.shadowColor = c;
        rc.shadowBlur = 20;
        rc.fill();

        rc.beginPath();
        rc.arc(px, py, 12 + intensidad * 10, 0, Math.PI * 2);
        rc.strokeStyle = c;
        rc.lineWidth = 1.5;
        rc.stroke();
    }

    rc.restore();
}

function dibujarHistorial(){
    const w = $('history').width;
    const h = $('history').height;

    hc.clearRect(0, 0, w, h);
    hc.fillStyle = '#03111b';
    hc.fillRect(0, 0, w, h);

    hc.strokeStyle = 'rgba(100,232,255,0.12)';
    hc.lineWidth = 1;

    for(let y = 0; y <= h; y += h / 4){
        hc.beginPath();
        hc.moveTo(0, y);
        hc.lineTo(w, y);
        hc.stroke();
    }

    const maximo = Math.max(
        0.2,
        ...history.map(v => Math.abs(v))
    );

    hc.beginPath();

    history.forEach((v, i) => {
        const x = i * w / (history.length - 1);
        const y = h - 8 - Math.min(Math.abs(v) / maximo, 1) * (h - 16);

        if(i === 0) hc.moveTo(x, y);
        else hc.lineTo(x, y);
    });

    hc.strokeStyle = color(level);
    hc.lineWidth = 2;
    hc.shadowColor = color(level);
    hc.shadowBlur = 7;
    hc.stroke();
    hc.shadowBlur = 0;
}

function actualizarPanel(d){
    const conectado = d.conexion === true;

    $('conn').textContent = conectado
        ? 'EN LÍNEA'
        : 'SENSOR SIN COMUNICACIÓN';

    $('dot').style.background = conectado
        ? '#63f5b0'
        : '#ff5874';

    $('dot').style.color = conectado
        ? '#63f5b0'
        : '#ff5874';

    $('status').textContent = d.estado || 'ESPERANDO SENSOR';
    $('status').className = 'status';

    if(!conectado){
        $('status').classList.add('offline');
    }else if(d.nivel >= 4){
        $('status').classList.add('level4');
    }else if(d.nivel === 3){
        $('status').classList.add('level3');
    }else if(d.nivel === 2){
        $('status').classList.add('level2');
    }

    $('network').textContent = d.conexion_texto || 'ESPERANDO';
    $('mag').textContent = Number(d.magnitud || 0).toFixed(6);
    $('fil').textContent = Number(d.filtrada || 0).toFixed(6);
    $('lvl').textContent = d.nivel ?? 1;
    $('events').textContent = d.eventos ?? 0;
    $('cal').textContent = d.calibrando ? 'EN CALIBRACIÓN' : 'COMPLETADA';
    $('dir').textContent = d.direccion ?? '--';
    $('peak').textContent = Number(d.maximo_evento || 0).toFixed(6);
    $('dur').textContent = Number(d.duracion_evento || 0).toFixed(2) + ' s';
    $('age').textContent = d.tiempo_activo == null
        ? '--'
        : Number(d.tiempo_activo).toFixed(1) + ' s';
    $('samples').textContent = d.muestras ?? 0;
    $('recipients').textContent = d.destinatarios_configurados ?? 0;

    const intensidad = Math.min(
        100,
        Math.max(0, Number(d.magnitud || 0) / 0.15 * 100)
    );

    $('pct').textContent = intensidad.toFixed(1) + '%';
    $('bar').style.width = intensidad + '%';
    $('bar').style.background = color(Number(d.nivel || 1));

    if(d.correo){
        const c = d.correo;
        $('mail').textContent = c.tipo
            ? c.tipo + ' — ' + (c.ok === true ? 'OK' : c.ok === false ? 'ERROR' : 'PENDIENTE')
            : 'SIN ENVÍOS';
        $('mail').title = c.detalle || '';
    }

    level = Number(d.nivel || 1);
    mag = Number(d.magnitud || 0);
    radarMag = Math.max(
        Number(d.magnitud || 0),
        Number(d.filtrada || 0)
    );
    enabled = conectado;

    history.push(conectado ? mag : 0);
    if(history.length > 120) history.shift();

    dibujarHistorial();
}

let socket = null;
let heartbeatTimer = null;
let reconnectTimer = null;

function conectarWebSocket(){
    if(socket && (socket.readyState === WebSocket.OPEN ||
                  socket.readyState === WebSocket.CONNECTING)) return;

    const protocolo = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${protocolo}//${location.host}/ws`);

    socket.addEventListener('open', () => {
        paused = false;
        $('conn').textContent = 'WEBSOCKET CONECTADO';
        $('network').textContent = 'CANAL EN VIVO';
        if(heartbeatTimer) clearInterval(heartbeatTimer);
        // Mantiene actualizado el panel incluso cuando no llegan muestras nuevas.
        heartbeatTimer = setInterval(() => {
            if(socket && socket.readyState === WebSocket.OPEN){
                socket.send(JSON.stringify({tipo:'heartbeat'}));
            }
        }, 2000);
    });

    socket.addEventListener('message', event => {
        try{
            const datos = JSON.parse(event.data);
            if(datos && datos.ok === false && datos.error){
                console.warn('WebSocket:', datos.error);
                return;
            }
            actualizarPanel(datos);
            paused = false;
        }catch(error){
            console.error('Mensaje WebSocket no válido:', error);
        }
    });

    socket.addEventListener('error', () => {
        paused = true;
        $('conn').textContent = 'ERROR DE CONEXIÓN';
        $('network').textContent = 'NO DISPONIBLE';
        $('status').textContent = 'NO SE PUEDE CONECTAR AL SERVIDOR';
        $('status').className = 'status offline';
        $('dot').style.background = '#ff5874';
        enabled = false;
    });

    socket.addEventListener('close', () => {
        if(heartbeatTimer) clearInterval(heartbeatTimer);
        heartbeatTimer = null;
        paused = true;
        $('conn').textContent = 'REINTENTANDO WEBSOCKET';
        $('network').textContent = 'SIN CANAL';
        $('status').textContent = 'RECONECTANDO CON EL SERVIDOR';
        $('status').className = 'status offline';
        $('dot').style.background = '#ff5874';
        enabled = false;
        if(reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(conectarWebSocket, 2000);
    });
}

function animar(){
    angle += 0.012;
    if(angle >= Math.PI * 2){
        angle -= Math.PI * 2;
    }

    dibujarRadar();
    requestAnimationFrame(animar);
}

function actualizarReloj(){
    $('clock').textContent = new Date().toLocaleTimeString(
        'es-CL',
        {hour12: false}
    );
}

actualizarReloj();
setInterval(actualizarReloj, 1000);
conectarWebSocket();
animar();

window.addEventListener('resize', () => {
    dibujarRadar();
    dibujarHistorial();
});
</script>
</body>
</html>'''


@app.route('/')
def inicio():
    return Response(PAGINA, mimetype='text/html')


if __name__ == '__main__':
    puerto = int(os.environ.get('PORT', 5000))
    app.run(
        host='0.0.0.0',
        port=puerto,
        debug=False,
        threaded=True
    )