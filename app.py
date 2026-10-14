from flask import Flask, request, jsonify, Response
from collections import deque
from threading import Lock, Thread
import json, math, os, time, urllib.request, urllib.error, html

app = Flask(__name__)

# SEISMIC CORE X — umbrales experimentales
UMBRAL_VIBRACION = 0.025
UMBRAL_TEMBLOR = 0.045
UMBRAL_ALERTA = 0.120
UMBRAL_ESTABLE = 0.010
TIEMPO_ESTABLE_ALERTA = 5.0
TIEMPO_FIN_EVENTO = 1.0
TIEMPO_PRECURSOR = 3.0
MUESTRAS_FILTRO = 5
MUESTRAS_CALIBRACION = 100

lock = Lock()
historial = deque(maxlen=MUESTRAS_FILTRO)
inicio_servidor = time.time()
ultimo_dato = 0.0
ultima_actividad = 0.0
inicio_evento = 0.0
inicio_movimiento_sostenido = 0.0
magnitud = filtrada = maximo_evento = duracion_evento = 0.0
nivel, estado, direccion = 1, 'ESPERANDO SENSOR', None
calibrando = True
muestras_recibidas = total_eventos = 0
evento_activo = alerta_critica = precursor_enviado = critica_enviada = False
ultimo_envio = {'tipo': None, 'ok': None, 'detalle': 'Aún no se ha enviado ninguna alerta', 'hora': None}


# Destinatario predeterminado para que el sistema funcione sin configurar ALERTAS_EMAILS.
# Si ALERTAS_EMAILS está definida con direcciones válidas en Render, esas direcciones tienen prioridad.
DESTINATARIO_PREDETERMINADO = 'edwin.rojas.hansen@gmail.com'


def obtener_destinatarios():
    correos = []
    for parte in os.environ.get('ALERTAS_EMAILS', '').split(','):
        correo = parte.strip()
        if correo and '@' in correo and '.' in correo.rsplit('@', 1)[-1] and correo.lower() not in [c.lower() for c in correos]:
            correos.append(correo)
    if not correos:
        correos.append(DESTINATARIO_PREDETERMINADO)
    return correos


def enviar_correo_brevo(destinatarios, asunto, mensaje):
    api_key = os.environ.get('BREVO_API_KEY')
    if not api_key:
        return False, 'Falta BREVO_API_KEY en Render'
    if not destinatarios:
        return False, 'No hay destinatarios válidos configurados'
    payload = {
        'sender': {'name': 'MONITOR SÍSMICO', 'email': 'sismoencurso@gmail.com'},
        'to': [{'email': c} for c in destinatarios],
        'subject': asunto, 'htmlContent': mensaje
    }
    req = urllib.request.Request('https://api.brevo.com/v3/smtp/email', data=json.dumps(payload).encode(),
        headers={'api-key': api_key, 'Content-Type': 'application/json', 'Accept': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            return resp.status in (200, 201, 202), body or f'HTTP {resp.status}'
    except urllib.error.HTTPError as e:
        return False, f'Brevo HTTP {e.code}: {e.read().decode("utf-8", errors="replace")}'
    except Exception as e:
        return False, str(e)


def registrar_envio(tipo, ok, detalle):
    global ultimo_envio
    with lock:
        ultimo_envio = {'tipo': tipo, 'ok': bool(ok), 'detalle': str(detalle)[:1000],
                        'hora': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}


def html_alerta(tipo, valor, valor_filtrado, duracion, estado_alerta):
    color = '#ff355e' if 'CRITICA' in tipo.upper() else '#ff984a'
    return f'''<!doctype html><html lang="es"><body style="background:#07111b;color:#d7f8ff;font-family:Arial,sans-serif;padding:24px"><div style="max-width:650px;margin:auto;border:1px solid {color};padding:24px;background:#0a1b28"><h1 style="color:#00eaff">SENSOR ACTIVO</h1><p style="color:#79aabb;letter-spacing:2px">MONITOREO EN CURSO · SEISMIC CORE X</p><h2 style="color:{color}">{html.escape(tipo)}</h2><p><b>Estado:</b> {html.escape(estado_alerta)}</p><p><b>Magnitud recibida:</b> {valor:.6f}</p><p><b>Señal filtrada:</b> {valor_filtrado:.6f}</p><p><b>Duración:</b> {duracion:.2f} s</p><p>Hora: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}</p><hr><small>Monitor experimental de vibraciones; no es una predicción sísmica ni una alerta oficial.</small></div></body></html>'''


def enviar_alerta_async(tipo, asunto, cantidad, cuerpo):
    def trabajo():
        destinatarios = obtener_destinatarios()
        if not destinatarios:
            registrar_envio(tipo, False, 'No hay destinatarios válidos configurados')
            app.logger.error('Alerta %s no enviada: no hay destinatarios válidos', tipo)
            return
        resultados, todo_ok = [], True
        for i in range(cantidad):
            sufijo = f' (aviso {i + 1} de {cantidad})' if cantidad > 1 else ''
            ok, detalle = enviar_correo_brevo(destinatarios, asunto + sufijo, cuerpo)
            resultados.append(f'{i + 1}: {"OK" if ok else "ERROR"} — {detalle}')
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
    for key in ('magnitud', 'magnitude', 'mag', 'vibracion', 'vibration', 'aceleracion', 'acceleration', 'filtrada', 'filtered', 'valor'):
        if key in datos:
            return abs(num(datos[key]))
    return None


def actualizar_estado(valor, datos):
    global ultimo_dato, ultima_actividad, inicio_evento, inicio_movimiento_sostenido
    global magnitud, filtrada, nivel, estado, direccion, calibrando, muestras_recibidas
    global total_eventos, maximo_evento, duracion_evento, evento_activo, alerta_critica
    global precursor_enviado, critica_enviada
    ahora = time.time()
    ultimo_dato = ahora
    muestras_recibidas += 1
    magnitud = abs(valor)
    historial.append(magnitud)
    filtrada = sum(historial) / len(historial)
    direccion = datos.get('direccion', datos.get('direction', direccion))
    if muestras_recibidas >= MUESTRAS_CALIBRACION:
        calibrando = False
    if calibrando:
        estado, nivel = 'CALIBRANDO SENSOR', 1
        return

    if filtrada >= UMBRAL_VIBRACION:
        ultima_actividad = ahora
        if not evento_activo:
            evento_activo = True
            inicio_evento = ahora
            total_eventos += 1
            maximo_evento = 0.0
            inicio_movimiento_sostenido = 0.0
            precursor_enviado = False
            critica_enviada = False
        maximo_evento = max(maximo_evento, filtrada)
        duracion_evento = max(0.0, ahora - inicio_evento)

    # Aviso de movimiento sostenido tras 3 s por encima del umbral de temblor.
    # También se evalúa si la señal alcanza el umbral crítico: la alerta crítica
    # se envía inmediatamente y el aviso sostenido se envía al cumplir 3 s.
    # Esto describe vibración observada; no predice un sismo.
    if evento_activo and filtrada >= UMBRAL_TEMBLOR:
        if inicio_movimiento_sostenido == 0.0:
            inicio_movimiento_sostenido = ahora
        if ahora - inicio_movimiento_sostenido >= TIEMPO_PRECURSOR and not precursor_enviado:
            precursor_enviado = True
            cuerpo = html_alerta('MOVIMIENTO SOSTENIDO', magnitud, filtrada, duracion_evento, 'MOVIMIENTO SOSTENIDO — REVISAR TELEMETRÍA')
            enviar_alerta_async('MOVIMIENTO_SOSTENIDO', 'MOVIMIENTO SOSTENIDO — REVISAR TELEMETRÍA', 2, cuerpo)
    else:
        inicio_movimiento_sostenido = 0.0

    if filtrada >= UMBRAL_ALERTA:
        nivel, estado, alerta_critica = 4, 'ALERTA CRITICA', True
        if not critica_enviada:
            critica_enviada = True
            cuerpo = html_alerta('ALERTA CRITICA', magnitud, filtrada, duracion_evento, 'ALERTA CRITICA')
            enviar_alerta_async('CRITICA', 'ALERTA CRÍTICA — ACTIVIDAD SÍSMICA INTENSA', 1, cuerpo)
        return
    if filtrada >= UMBRAL_TEMBLOR:
        if alerta_critica and ahora - ultima_actividad < TIEMPO_ESTABLE_ALERTA:
            nivel, estado = 4, 'ALERTA CRITICA'
        else:
            nivel, estado = 3, 'SISMO EN CURSO'
        return
    if filtrada >= UMBRAL_VIBRACION:
        nivel, estado = 2, 'MOVIMIENTO DETECTADO'
        return

    calma = ahora - ultima_actividad if ultima_actividad else float('inf')
    if evento_activo and calma >= TIEMPO_FIN_EVENTO:
        evento_activo = False
        duracion_evento = max(0.0, ahora - inicio_evento)
        inicio_evento = 0.0
        inicio_movimiento_sostenido = 0.0
        maximo_evento = 0.0
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
    estado_visible = estado if conectado else ('ESPERANDO SENSOR' if not ultimo_dato else 'SENSOR SIN COMUNICACION')
    return {'ok': True, 'conexion': conectado,
        'conexion_texto': 'INTERNET CONECTADO' if conectado else ('ESPERANDO DATOS' if not ultimo_dato else 'SIN DATOS DEL SENSOR'),
        'estado': estado_visible, 'magnitud': round(magnitud, 6), 'filtrada': round(filtrada, 6),
        'nivel': nivel, 'direccion': direccion, 'calibrando': calibrando, 'muestras': muestras_recibidas,
        'eventos': total_eventos, 'maximo_evento': round(maximo_evento, 6),
        'duracion_evento': round(duracion_evento, 2), 'evento_activo': evento_activo,
        'tiempo_activo': round(ahora - ultimo_dato, 2) if ultimo_dato else None,
        'tiempo_servidor': round(ahora - inicio_servidor, 1), 'destinatarios_configurados': len(obtener_destinatarios()),
        'correo': dict(ultimo_envio)}


@app.route('/datos', methods=['POST'])
def recibir_datos():
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        datos = request.form.to_dict()
    if not datos:
        return jsonify({'ok': False, 'error': 'No se recibieron datos JSON'}), 400
    valor = obtener_magnitud(datos)
    if valor is None:
        x, y, z = datos.get('x', datos.get('ax')), datos.get('y', datos.get('ay')), datos.get('z', datos.get('az'))
        if x is None or y is None or z is None:
            return jsonify({'ok': False, 'error': 'Falta magnitud o ejes x, y, z'}), 400
        valor = abs(math.sqrt(num(x)**2 + num(y)**2 + num(z)**2) - 1.0)
    with lock:
        actualizar_estado(valor, datos)
        result = construir_estado()
    return jsonify({'ok': True, 'estado': result['estado'], 'magnitud': result['magnitud'], 'filtrada': result['filtrada']})


@app.route('/estado', methods=['GET'])
def consultar_estado():
    with lock:
        return jsonify(construir_estado())


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'ok': True, 'service': 'SEISMIC CORE X'})


@app.route('/probar-correo', methods=['POST'])
def probar_correo():
    token = os.environ.get('ADMIN_TOKEN')
    if token and request.headers.get('X-Admin-Token') != token:
        return jsonify({'ok': False, 'error': 'No autorizado'}), 401
    ok, detalle = enviar_correo_brevo(obtener_destinatarios(), 'PRUEBA DE CORREO — MONITOR SÍSMICO',
        '<h2>Prueba de correo</h2><p>Prueba de envío de SEISMIC CORE X mediante Brevo. No es una alerta sísmica.</p>')
    registrar_envio('PRUEBA', ok, detalle)
    return jsonify({'ok': ok, 'destinatarios': len(obtener_destinatarios()), 'detalle': detalle}), (200 if ok else 502)


# HUD de pantalla completa: radar, barrido con bip, telemetría y gráfica histórica.
PAGINA = r'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SEISMIC CORE X</title>
<style>
:root{--bg:#030910;--panel:rgba(5,24,38,.9);--line:#12445a;--cyan:#00eaff;--green:#39ff9b;--yellow:#ffe66d;--orange:#ff984a;--red:#ff355e;--muted:#6e9aab}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at center,#0a2332,#030910 70%);color:#d7f8ff;font-family:Consolas,'Courier New',monospace}body:before{content:'';position:fixed;inset:0;pointer-events:none;opacity:.1;background:repeating-linear-gradient(transparent 0 3px,rgba(0,234,255,.12) 4px);z-index:5}header{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:17px 22px;border-bottom:1px solid var(--line);background:#020a12e8}.logo{color:var(--cyan);font-size:clamp(22px,3vw,36px);font-weight:bold;letter-spacing:4px;text-shadow:0 0 16px #00bfff}.sub{color:var(--muted);font-size:10px;letter-spacing:2px;margin-top:5px}.connection{font-size:12px;color:var(--yellow);text-align:right}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--yellow);margin-right:7px;box-shadow:0 0 10px currentColor}main{padding:14px;max-width:1800px;margin:auto}.grid{display:grid;grid-template-columns:minmax(280px,1.15fr) minmax(300px,1fr) minmax(260px,.8fr);gap:12px}.panel{position:relative;background:var(--panel);border:1px solid var(--line);padding:15px;min-width:0;box-shadow:inset 0 0 22px #00beff09}.panel:before{content:'';position:absolute;top:-1px;left:15px;width:75px;border-top:2px solid var(--cyan)}.heading{font-size:11px;color:var(--cyan);letter-spacing:2px;margin-bottom:15px}.radar-wrap{width:100%;max-width:470px;aspect-ratio:1;margin:auto}#radar{width:100%;height:100%;display:block}.radar-label{text-align:center;color:var(--muted);font-size:10px;margin-top:8px;letter-spacing:2px}.status{border:1px solid var(--green);color:var(--green);text-align:center;padding:15px 7px;font-size:clamp(13px,1.8vw,19px);font-weight:bold;margin-bottom:12px;text-shadow:0 0 12px currentColor}.level2{color:var(--yellow)!important;border-color:var(--yellow)!important}.level3{color:var(--orange)!important;border-color:var(--orange)!important}.level4{color:var(--red)!important;border-color:var(--red)!important;animation:blink .7s infinite}.offline{color:var(--muted)!important;border-color:var(--muted)!important}@keyframes blink{50%{opacity:.48}}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:9px}.metric{background:#000a14b3;border:1px solid #103346;padding:12px 10px;min-height:83px}.label{color:var(--muted);font-size:10px;letter-spacing:1px}.value{color:var(--cyan);font-size:clamp(20px,2.4vw,29px);margin-top:10px}.unit{color:var(--muted);font-size:10px}.bar{height:7px;background:#102c3b;margin-top:9px}.bar-inner{height:100%;width:0;background:var(--cyan);transition:width .25s}.info-row{display:flex;justify-content:space-between;gap:12px;padding:11px 0;border-bottom:1px solid #123040;font-size:11px}.info-row span:first-child{color:var(--muted)}.info-row span:last-child{text-align:right;overflow-wrap:anywhere}.controls{display:flex;flex-wrap:wrap;gap:8px;margin-top:13px}button{background:#071b28;color:var(--cyan);border:1px solid #176078;padding:10px 12px;font:inherit;font-size:10px;cursor:pointer;letter-spacing:1px}button:hover{background:#0c3040}#history{width:100%;height:100px;display:block;background:#03111b;border:1px solid #103346}.footer{color:#527d8c;font-size:10px;text-align:center;padding:18px 5px 8px;letter-spacing:1px}@media(max-width:1050px){.grid{grid-template-columns:1fr 1fr}.side{grid-column:1/-1}}@media(max-width:680px){header{align-items:flex-start;flex-direction:column}.connection{text-align:left}.grid{grid-template-columns:1fr}.side{grid-column:auto}main{padding:8px}}
</style></head><body><header><div><div class="logo">SEISMIC CORE X</div><div class="sub">VIBRATION MONITORING SYSTEM / LIVE TELEMETRY</div></div><div class="connection"><div><span class="dot" id="dot"></span><span id="conn">CONECTANDO...</span></div><div class="sub" id="clock">--:--:--</div></div></header>
<main><div class="grid"><section class="panel"><div class="heading">01 / RADAR DE ACTIVIDAD</div><div class="radar-wrap"><canvas id="radar" width="600" height="600"></canvas></div><div class="radar-label">RADIAL SCAN // LIVE</div><div class="controls"><button id="sound">ACTIVAR SONIDO</button><button id="pause">PAUSAR BIP</button></div></section>
<section class="panel"><div class="heading">02 / ESTADO DEL SISTEMA</div><div id="status" class="status">ESPERANDO SENSOR</div><div class="metrics"><div class="metric"><div class="label">MAGNITUD</div><div class="value" id="mag">0.000000</div><div class="unit">VALOR RECIBIDO</div></div><div class="metric"><div class="label">SEÑAL FILTRADA</div><div class="value" id="fil">0.000000</div><div class="unit">PROMEDIO MÓVIL</div></div><div class="metric"><div class="label">NIVEL</div><div class="value" id="lvl">1</div><div class="unit">CLASIFICACIÓN INTERNA</div></div><div class="metric"><div class="label">EVENTOS</div><div class="value" id="events">0</div><div class="unit">CONTADOR DE EVENTOS</div></div></div><div class="info-row"><span>INTENSIDAD RELATIVA</span><span id="pct">0%</span></div><div class="bar"><div class="bar-inner" id="bar"></div></div></section>
<section class="panel side"><div class="heading">03 / TELEMETRÍA</div><div class="info-row"><span>COMUNICACIÓN</span><span id="network">ESPERANDO</span></div><div class="info-row"><span>CALIBRACIÓN</span><span id="cal">PENDIENTE</span></div><div class="info-row"><span>DIRECCIÓN</span><span id="dir">--</span></div><div class="info-row"><span>PICO DEL EVENTO</span><span id="peak">0.000000</span></div><div class="info-row"><span>DURACIÓN</span><span id="dur">0.00 s</span></div><div class="info-row"><span>ÚLTIMA SEÑAL</span><span id="age">--</span></div><div class="info-row"><span>UMBRAL MOVIMIENTO</span><span>0.025</span></div><div class="info-row"><span>UMBRAL TEMBLOR</span><span>0.045</span></div><div class="info-row"><span>UMBRAL CRÍTICO</span><span>0.120</span></div><div class="heading" style="margin-top:20px">04 / SEÑAL EN VIVO</div><canvas id="history" width="700" height="180"></canvas><div class="info-row"><span>MUESTRAS RECIBIDAS</span><span id="samples">0</span></div><div class="info-row"><span>DESTINATARIOS</span><span id="recipients">0</span></div><div class="info-row"><span>ÚLTIMO CORREO</span><span id="mail">SIN ENVÍOS</span></div></section></div><div class="footer">SEISMIC CORE X // TELEMETRÍA EXPERIMENTAL // NO ES UN SISTEMA OFICIAL DE ALERTA SÍSMICA</div></main>
<script>
const $=id=>document.getElementById(id),rc=$('radar').getContext('2d'),hc=$('history').getContext('2d');let angle=0,enabled=false,paused=false,audio=null,lastSweep=-1,level=1,mag=0,history=Array(120).fill(0),lastStatus='';function color(l){return l>=4?'#ff355e':l===3?'#ff984a':l===2?'#ffe66d':'#39ff9b'}function beep(f=660,d=.08,v=.03){if(!enabled||paused)return;try{if(!audio)audio=new(window.AudioContext||window.webkitAudioContext)();if(audio.state==='suspended')audio.resume();let o=audio.createOscillator(),g=audio.createGain();o.type='sine';o.frequency.value=f;g.gain.setValueAtTime(v,audio.currentTime);g.gain.exponentialRampToValueAtTime(.001,audio.currentTime+d);o.connect(g);g.connect(audio.destination);o.start();o.stop(audio.currentTime+d)}catch(e){}}$('sound').onclick=async()=>{enabled=true;paused=false;if(!audio)audio=new(window.AudioContext||window.webkitAudioContext)();await audio.resume();beep(880,.12,.07);$('sound').textContent='SONIDO ACTIVADO'};$('pause').onclick=()=>{paused=!paused;$('pause').textContent=paused?'REANUDAR BIP':'PAUSAR BIP'};function radar(){let w=600,c=300,r=250;rc.clearRect(0,0,600,600);rc.fillStyle='#03111a';rc.fillRect(0,0,600,600);rc.strokeStyle='#00eaff3b';for(let i=1;i<=4;i++){rc.beginPath();rc.arc(c,c,r*i/4,0,Math.PI*2);rc.stroke()}rc.beginPath();rc.moveTo(c-r,c);rc.lineTo(c+r,c);rc.moveTo(c,c-r);rc.lineTo(c,c+r);rc.stroke();let g=rc.createConicGradient(angle,c,c);g.addColorStop(0,'#00ffcc55');g.addColorStop(.18,'#00ffcc10');g.addColorStop(.25,'#00ffcc00');g.addColorStop(1,'#00ffcc00');rc.fillStyle=g;rc.beginPath();rc.moveTo(c,c);rc.arc(c,c,r,0,Math.PI*2);rc.fill();rc.strokeStyle='#00eaff';rc.lineWidth=2;rc.beginPath();rc.moveTo(c,c);rc.lineTo(c+Math.cos(angle)*r,c+Math.sin(angle)*r);rc.stroke();if(mag>=.025){let p=Math.min(.88,Math.max(.12,mag/.2)),x=c+Math.cos(-.62)*r*p,y=c+Math.sin(-.62)*r*p;rc.save();rc.shadowBlur=24;rc.shadowColor=color(level);rc.fillStyle=color(level);rc.beginPath();rc.arc(x,y,level>=3?9:6,0,Math.PI*2);rc.fill();rc.restore()}angle+=.012;let b=Math.floor(angle/(2*Math.PI));if(b!==lastSweep){lastSweep=b;beep()}requestAnimationFrame(radar)}function graph(){hc.clearRect(0,0,700,180);hc.fillStyle='#03111b';hc.fillRect(0,0,700,180);hc.strokeStyle='#123040';for(let y=30;y<180;y+=30){hc.beginPath();hc.moveTo(0,y);hc.lineTo(700,y);hc.stroke()}hc.strokeStyle=color(level);hc.lineWidth=2;hc.beginPath();history.forEach((v,i)=>{let x=i*700/(history.length-1),y=172-Math.min(1,Math.abs(v)/.2)*164;i?hc.lineTo(x,y):hc.moveTo(x,y)});hc.stroke()}async function refresh(){try{let r=await fetch('/estado',{cache:'no-store'});if(!r.ok)throw Error(r.status);let d=await r.json(),on=!!d.conexion;$('conn').textContent=d.conexion_texto||'SIN DATOS';$('dot').style.background=on?'#39ff9b':'#ff355e';$('network').textContent=on?'EN LÍNEA':'SIN COMUNICACIÓN';let s=$('status');s.textContent=d.estado||'ESPERANDO SENSOR';s.className='status';level=Number(d.nivel||1);if(!on)s.classList.add('offline');else if(level===2)s.classList.add('level2');else if(level===3)s.classList.add('level3');else if(level>=4)s.classList.add('level4');mag=Number(d.filtrada||0);$('mag').textContent=Number(d.magnitud||0).toFixed(6);$('fil').textContent=mag.toFixed(6);$('lvl').textContent=level;$('events').textContent=d.eventos||0;$('cal').textContent=d.calibrando?'EN CURSO':'COMPLETADA';$('dir').textContent=d.direccion||'--';$('peak').textContent=Number(d.maximo_evento||0).toFixed(6);$('dur').textContent=Number(d.duracion_evento||0).toFixed(2)+' s';$('samples').textContent=d.muestras||0;$('age').textContent=d.tiempo_activo==null?'--':Number(d.tiempo_activo).toFixed(1)+' s';$('recipients').textContent=d.destinatarios_configurados??0;let m=d.correo||{};$('mail').textContent=m.tipo?(m.ok?'OK: ':'ERROR: ')+m.tipo:'SIN ENVÍOS';let p=Math.min(100,Math.max(0,mag/.2*100));$('pct').textContent=p.toFixed(1)+'%';$('bar').style.width=p+'%';$('bar').style.background=color(level);history.push(mag);if(history.length>120)history.shift();graph();if(lastStatus&&lastStatus!==d.estado&&enabled){if(level>=4)beep(180,.45,.12);else if(level===3)beep(300,.22,.08);else if(level===2)beep(460,.12,.05);else beep(720,.08,.025)}lastStatus=d.estado||''}catch(e){$('conn').textContent='ERROR DE CONEXIÓN';$('network').textContent='ERROR'}}$('clock').textContent=new Date().toLocaleTimeString('es-CL',{hour12:false});setInterval(()=>$('clock').textContent=new Date().toLocaleTimeString('es-CL',{hour12:false}),1000);setInterval(refresh,500);radar();graph();refresh();
</script></body></html>'''


@app.route('/', methods=['GET'])
def index():
    return Response(PAGINA, mimetype='text/html; charset=utf-8')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
