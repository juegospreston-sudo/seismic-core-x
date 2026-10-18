from flask import Flask, request, jsonify, Response
from collections import deque
from threading import Lock, Thread
import json, math, os, time, urllib.request, urllib.error, html
from flask_socketio import SocketIO

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

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
        with lock:
            socketio.emit('estado', construir_estado())

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


PAGINA = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SEISMIC CORE X</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><style>
:root{--cyan:#64e8ff;--green:#63f5b0;--yellow:#ffd66b;--orange:#ff984a;--red:#ff355e;--muted:#8299a8}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#102637,#07111c 50%,#03070d);color:#e5f2f7;font-family:Segoe UI,Arial,sans-serif}header{display:flex;justify-content:space-between;align-items:center;padding:18px 24px;background:#030a11e8;border-bottom:1px solid #203c4b}.logo{font-size:clamp(22px,3vw,35px);font-weight:800;letter-spacing:3px;color:var(--cyan)}.sub,.heading,.label{color:var(--muted);font-size:10px;letter-spacing:1.5px}.connection{font-size:12px;color:var(--yellow)}main{padding:15px;max-width:1800px;margin:auto}.grid{display:grid;grid-template-columns:1.1fr 1fr .9fr;gap:14px}.panel{background:linear-gradient(145deg,#0c1d2bf5,#050f19f5);border:1px solid #203d4d;border-radius:13px;padding:16px;min-width:0}.heading{margin-bottom:15px;color:#9dc8d8;font-weight:bold}.radar-wrap{position:relative;width:100%;max-width:460px;aspect-ratio:1;margin:auto;border-radius:50%;overflow:hidden;border:2px solid #39ff9b88;box-shadow:0 0 35px #39ff9b22}#mapa,#radar{position:absolute;inset:0;width:100%;height:100%}#radar{pointer-events:none;z-index:400}.radar-label{text-align:center;color:#c8eee7;font-size:12px;margin:15px 0;letter-spacing:2px}.clock{text-align:center;color:var(--cyan);font-size:26px;letter-spacing:3px;padding:12px;border:1px solid #1b3949}.status{text-align:center;padding:15px 5px;border:1px solid var(--green);color:var(--green);font-weight:bold;margin-bottom:12px}.level2{color:var(--yellow)!important;border-color:var(--yellow)!important}.level3{color:var(--orange)!important;border-color:var(--orange)!important}.level4{color:var(--red)!important;border-color:var(--red)!important;animation:blink .7s infinite}.offline{color:var(--muted)!important;border-color:var(--muted)!important}@keyframes blink{50%{opacity:.45}}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:9px}.metric{background:#000a14;border:1px solid #103346;padding:12px;min-height:82px}.value{font-size:clamp(20px,2.4vw,29px);margin-top:9px;color:var(--cyan)}.unit{font-size:10px;color:var(--muted)}.info-row{display:flex;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid #123040;font-size:11px}.info-row span:first-child{color:var(--muted)}.info-row span:last-child{text-align:right;overflow-wrap:anywhere}.bar{height:7px;background:#102c3b;margin:10px 0}.bar-inner{height:100%;width:0;background:var(--cyan);transition:width .2s}#history{width:100%;height:100px;background:#03111b;border:1px solid #103346}.footer{text-align:center;color:#527d8c;font-size:10px;padding:18px 5px;letter-spacing:1px}.socket-status{font-size:10px;color:var(--muted);margin-top:6px}@media(max-width:1050px){.grid{grid-template-columns:1fr 1fr}.side{grid-column:1/-1}}@media(max-width:680px){header{align-items:flex-start;flex-direction:column;gap:10px}.grid{grid-template-columns:1fr}.side{grid-column:auto}main{padding:8px}}
</style></head><body><header><div><div class="logo">SEISMIC CORE X</div><div class="sub">TELEMETRÍA EN TIEMPO REAL · WEBSOCKET</div></div><div class="connection"><span id="dot">●</span> <span id="conn">CONECTANDO...</span><div class="socket-status" id="ws">Canal en preparación</div></div></header>
<main><div class="grid"><section class="panel"><div class="heading">01 / RADAR DE ACTIVIDAD</div><div class="radar-wrap"><div id="mapa"></div><canvas id="radar" width="600" height="600"></canvas></div><div class="radar-label">SENSOR ACTIVO</div><div class="clock" id="clock">--:--:--</div></section>
<section class="panel"><div class="heading">02 / ESTADO DEL SISTEMA</div><div id="status" class="status offline">ESPERANDO SENSOR</div><div class="metrics"><div class="metric"><div class="label">MAGNITUD</div><div id="mag" class="value">0.000000</div><div class="unit">VALOR RECIBIDO</div></div><div class="metric"><div class="label">SEÑAL FILTRADA</div><div id="fil" class="value">0.000000</div><div class="unit">PROMEDIO MÓVIL</div></div><div class="metric"><div class="label">NIVEL</div><div id="lvl" class="value">1</div><div class="unit">CLASIFICACIÓN INTERNA</div></div><div class="metric"><div class="label">EVENTOS</div><div id="events" class="value">0</div><div class="unit">CONTADOR DE EVENTOS</div></div></div><div class="info-row"><span>INTENSIDAD RELATIVA</span><span id="pct">0%</span></div><div class="bar"><div id="bar" class="bar-inner"></div></div><div class="info-row"><span>EVENTO ACTIVO</span><span id="event">NO</span></div></section>
<section class="panel side"><div class="heading">03 / TELEMETRÍA</div><div class="info-row"><span>COMUNICACIÓN SENSOR</span><span id="network">ESPERANDO</span></div><div class="info-row"><span>CALIBRACIÓN</span><span id="cal">PENDIENTE</span></div><div class="info-row"><span>DIRECCIÓN</span><span id="dir">--</span></div><div class="info-row"><span>PICO DEL EVENTO</span><span id="peak">0.000000</span></div><div class="info-row"><span>DURACIÓN</span><span id="dur">0.00 s</span></div><div class="info-row"><span>ÚLTIMA SEÑAL</span><span id="age">--</span></div><div class="info-row"><span>UMBRAL MOVIMIENTO</span><span>0.035</span></div><div class="info-row"><span>UMBRAL BRUSCO</span><span>0.060</span></div><div class="info-row"><span>ACTIVIDAD SOSTENIDA</span><span>0.060 / 3 s</span></div><div class="info-row"><span>UMBRAL CRÍTICO</span><span>0.150</span></div><div class="heading" style="margin-top:18px">04 / SEÑAL EN VIVO</div><canvas id="history" width="700" height="180"></canvas><div class="info-row"><span>MUESTRAS RECIBIDAS</span><span id="samples">0</span></div><div class="info-row"><span>DESTINATARIOS</span><span id="recipients">0</span></div><div class="info-row"><span>ÚLTIMO CORREO</span><span id="mail">SIN ENVÍOS</span></div></section></div><div class="footer">SEISMIC CORE X // MONITOR EXPERIMENTAL // NO ES UN SISTEMA OFICIAL DE ALERTA SÍSMICA</div></main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script><script>
const $=id=>document.getElementById(id);const map=L.map('mapa',{zoomControl:false,attributionControl:true,dragging:false,scrollWheelZoom:false,doubleClickZoom:false,touchZoom:false,keyboard:false}).setView([-33.4937,-70.7474],15);L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{maxZoom:19,attribution:'© OpenStreetMap © CARTO'}).addTo(map);setTimeout(()=>map.invalidateSize(),500);
const rc=$('radar').getContext('2d'),hc=$('history').getContext('2d');let angle=0,level=1,mag=0,radarMag=0,history=Array(120).fill(0),lastData=0;
function radar(){const w=rc.canvas.width,h=rc.canvas.height,cx=w/2,cy=h/2,r=Math.min(w,h)*.47;rc.clearRect(0,0,w,h);rc.save();rc.translate(cx,cy);rc.strokeStyle='rgba(57,255,155,.28)';rc.lineWidth=1;for(let f of [.25,.5,.75,1]){rc.beginPath();rc.arc(0,0,r*f,0,Math.PI*2);rc.stroke()}rc.beginPath();rc.moveTo(-r,0);rc.lineTo(r,0);rc.moveTo(0,-r);rc.lineTo(0,r);rc.stroke();const g=rc.createConicGradient(angle,0,0);g.addColorStop(0,'rgba(57,255,155,0)');g.addColorStop(.82,'rgba(57,255,155,0)');g.addColorStop(1,'rgba(57,255,155,.35)');rc.fillStyle=g;rc.beginPath();rc.arc(0,0,r,0,Math.PI*2);rc.fill();rc.rotate(angle);rc.strokeStyle='rgba(99,255,190,.95)';rc.beginPath();rc.moveTo(0,0);rc.lineTo(r,0);rc.stroke();if(radarMag>.012){rc.fillStyle=level>=4?'#ff355e':level===3?'#ff984a':level===2?'#ffe66d':'#39ff9b';rc.shadowBlur=18;rc.shadowColor=rc.fillStyle;rc.beginPath();rc.arc(r*.48,0,Math.min(13,4+radarMag*50),0,Math.PI*2);rc.fill()}rc.restore();angle=(angle+.012)%(Math.PI*2);requestAnimationFrame(radar)}radar();
function drawHistory(){const w=hc.canvas.width,h=hc.canvas.height;hc.clearRect(0,0,w,h);hc.strokeStyle='#103346';for(let y=0;y<h;y+=30){hc.beginPath();hc.moveTo(0,y);hc.lineTo(w,y);hc.stroke()}hc.strokeStyle='#64e8ff';hc.lineWidth=2;hc.beginPath();history.forEach((v,i)=>{const x=i*w/(history.length-1),y=h-Math.min(1,v/.2)*(h-8)-4;if(i===0)hc.moveTo(x,y);else hc.lineTo(x,y)});hc.stroke()}
function paint(d){if(!d)return;lastData=Date.now();level=Number(d.nivel||1);mag=Number(d.magnitud||0);radarMag=Number(d.filtrada||mag);$('conn').textContent=d.conexion_texto||'EN LÍNEA';$('dot').style.color=d.conexion?'#63f5b0':'#ff5874';$('network').textContent=d.conexion_texto||'--';$('status').textContent=d.estado||'--';$('status').className='status '+(d.conexion?'':'offline')+(level===2?' level2':level===3?' level3':level>=4?' level4':'');$('mag').textContent=mag.toFixed(6);$('fil').textContent=Number(d.filtrada||0).toFixed(6);$('lvl').textContent=level;$('events').textContent=d.eventos||0;$('pct').textContent=Math.min(100,Math.round(mag/.15*100))+'%';$('bar').style.width=Math.min(100,mag/.15*100)+'%';$('bar').style.background=level>=4?'#ff355e':level===3?'#ff984a':level===2?'#ffe66d':'#64e8ff';$('cal').textContent=d.calibrando?'EN CURSO':'COMPLETA';$('dir').textContent=d.direccion??'--';$('peak').textContent=Number(d.maximo_evento||0).toFixed(6);$('dur').textContent=Number(d.duracion_evento||0).toFixed(2)+' s';$('age').textContent=d.tiempo_activo==null?'--':Number(d.tiempo_activo).toFixed(1)+' s';$('event').textContent=d.evento_activo?'SÍ':'NO';$('samples').textContent=d.muestras||0;$('recipients').textContent=d.destinatarios_configurados||0;const c=d.correo||{};$('mail').textContent=c.tipo?(c.ok?'OK · ':'ERROR · ')+c.tipo:'SIN ENVÍOS';history.push(mag);if(history.length>120)history.shift();drawHistory()}
const socket=io({transports:['websocket','polling'],reconnection:true,reconnectionAttempts:Infinity,reconnectionDelay:1000,reconnectionDelayMax:5000});socket.on('connect',()=>{$('ws').textContent='WEBSOCKET CONECTADO';socket.emit('solicitar_estado')});socket.on('disconnect',()=>{$('ws').textContent='RECONEXIÓN EN CURSO';$('dot').style.color='#ffd66b'});socket.on('connect_error',()=>{$('ws').textContent='ERROR DE CONEXIÓN'});socket.on('estado',paint);socket.on('snapshot',paint);setInterval(()=>$('clock').textContent=new Date().toLocaleTimeString('es-CL',{hour12:false}),250);setInterval(()=>{if(lastData&&Date.now()-lastData>15000){$('network').textContent='SIN DATOS';$('conn').textContent='SENSOR SIN COMUNICACIÓN';$('status').className='status offline'}},2000);
</script></body></html>"""

@app.route('/')
def inicio():
    return Response(PAGINA, mimetype='text/html')

@socketio.on('solicitar_estado')
def socket_solicitar_estado():
    with lock:
        emit_data = construir_estado()
    socketio.emit('snapshot', emit_data, to=request.sid)


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

    socketio.emit('estado', result)
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



if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), allow_unsafe_werkzeug=True)