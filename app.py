
from flask import Flask, request, jsonify, render_template_string
from collections import deque
from threading import Lock
import math
import time

app = Flask(__name__)
lock = Lock()

# =========================================================
# CONFIGURACION DEL SISTEMA
# =========================================================

UMBRAL_VIBRACION = 0.025
UMBRAL_TEMBLOR = 0.045
UMBRAL_ALERTA = 0.120
UMBRAL_ESTABLE = 0.010

TIEMPO_ESTABLE_ALERTA = 5.0
TIEMPO_FIN_EVENTO = 1.0

MUESTRAS_CALIBRACION = 100
MUESTRAS_FILTRO = 5

historial_filtro = deque(maxlen=MUESTRAS_FILTRO)
historial_grafico = deque(maxlen=180)
registro_eventos = deque(maxlen=30)

estado = {
    "calibrando": True,
    "estado": "CALIBRANDO SENSOR",
    "magnitud": 0.0,
    "filtrada": 0.0,
    "nivel": 0,
    "direccion": "SIN DATOS",
    "evento_activo": False,
    "inicio_evento": None,
    "ultima_actividad": 0.0,
    "inicio_alerta": None,
    "contador": 0,
    "ultima_actualizacion": time.time(),
    "x": 0.0,
    "y": 0.0,
    "z": 0.0,
    "duracion": 0.0,
    "ultimo_evento": "NINGUNO"
}

muestras_calibracion = 0
suma_calibracion = 0.0


def num(valor, defecto=0.0):
    try:
        n = float(valor)
        return n if math.isfinite(n) else defecto
    except (TypeError, ValueError):
        return defecto


def clasificar(magnitud, duracion):
    # La magnitud fuerte puede generar alerta incluso
    # cuando el evento es breve.
    if magnitud >= UMBRAL_ALERTA:
        return 4, "ALERTA CRITICA"

    if magnitud >= UMBRAL_TEMBLOR and duracion >= 0.30:
        return 3, "SISMO EN CURSO"

    if magnitud >= UMBRAL_VIBRACION and duracion >= 0.15:
        return 2, "MOVIMIENTO DETECTADO"

    if magnitud >= UMBRAL_ESTABLE:
        return 1, "VIBRACION LEVE"

    return 0, "SISTEMA ESTABLE"


@app.route("/")
def index():
    return render_template_string(PAGINA)


@app.route("/datos", methods=["POST"])
def recibir_datos():
    global muestras_calibracion, suma_calibracion

    data = request.get_json(silent=True) or {}

    # Acepta nombres habituales usados por el ESP8266.
    x = num(data.get("x", data.get("ax", data.get("accelX", 0))))
    y = num(data.get("y", data.get("ay", data.get("accelY", 0))))
    z = num(data.get("z", data.get("az", data.get("accelZ", 0))))

    # Si el ESP8266 ya envía una magnitud calculada,
    # se aprovecha ese dato.
    magnitud_recibida = data.get("magnitud", data.get("m"))

    if magnitud_recibida is not None:
        magnitud = abs(num(magnitud_recibida))
    else:
        magnitud = math.sqrt(x*x + y*y + z*z)

    ahora = time.time()

    with lock:
        estado["x"] = x
        estado["y"] = y
        estado["z"] = z
        estado["magnitud"] = magnitud
        estado["contador"] += 1
        estado["ultima_actualizacion"] = ahora

        if estado["calibrando"]:
            muestras_calibracion += 1
            suma_calibracion += magnitud

            if muestras_calibracion >= MUESTRAS_CALIBRACION:
                estado["calibrando"] = False
                estado["estado"] = "SISTEMA ESTABLE"
                estado["filtrada"] = 0.0
                historial_filtro.clear()
                historial_grafico.clear()

            return jsonify({
                "ok": True,
                "calibrando": estado["calibrando"]
            })

        historial_filtro.append(magnitud)
        filtrada = sum(historial_filtro) / len(historial_filtro)
        estado["filtrada"] = filtrada

        historial_grafico.append({
            "t": ahora,
            "v": round(filtrada, 5)
        })

        if filtrada >= UMBRAL_VIBRACION:
            if not estado["evento_activo"]:
                estado["evento_activo"] = True
                estado["inicio_evento"] = ahora
                estado["inicio_alerta"] = None
                estado["ultimo_evento"] = time.strftime(
                    "%H:%M:%S", time.localtime(ahora)
                )

            estado["ultima_actividad"] = ahora

        duracion = 0.0
        if estado["evento_activo"] and estado["inicio_evento"]:
            duracion = ahora - estado["inicio_evento"]

        estado["duracion"] = round(duracion, 2)

        nivel, nombre = clasificar(filtrada, duracion)

        if estado["evento_activo"]:
            estado["nivel"] = nivel
            estado["estado"] = nombre

            if nivel >= 3 and estado["inicio_alerta"] is None:
                estado["inicio_alerta"] = ahora

            # Finaliza el evento solo después de un período
            # continuo de baja actividad.
            silencio = ahora - estado["ultima_actividad"]

            if silencio >= TIEMPO_FIN_EVENTO:
                registro_eventos.appendleft({
                    "hora": time.strftime(
                        "%H:%M:%S", time.localtime(ahora)
                    ),
                    "nivel": estado["nivel"],
                    "estado": estado["estado"],
                    "maximo": round(filtrada, 4),
                    "duracion": round(duracion, 2)
                })

                estado["evento_activo"] = False
                estado["inicio_evento"] = None
                estado["inicio_alerta"] = None
                estado["duracion"] = 0.0
                estado["nivel"] = 0
                estado["estado"] = "SISTEMA ESTABLE"
                estado["direccion"] = "SIN MOVIMIENTO"
        else:
            estado["nivel"] = nivel
            estado["estado"] = nombre if nivel == 0 else "SISTEMA ESTABLE"

    return jsonify({"ok": True})


@app.route("/estado", methods=["GET"])
def obtener_estado():
    ahora = time.time()

    with lock:
        resultado = dict(estado)
        resultado["historial"] = list(historial_grafico)
        resultado["eventos"] = list(registro_eventos)

        if ahora - estado["ultima_actualizacion"] > 10:
            resultado["estado"] = "SIN DATOS DEL SENSOR"
            resultado["direccion"] = "REVISAR CONEXION"

        return jsonify(resultado)


@app.route("/salud", methods=["GET"])
def salud():
    return jsonify({
        "ok": True,
        "sistema": "SEISMIC CORE X",
        "servidor": "activo"
    })


PAGINA = r"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEISMIC CORE X</title>
<style>
:root {
 --bg:#030811; --panel:#071522; --line:#12384b;
 --cyan:#00eaff; --green:#35ffb0; --yellow:#ffd45a;
 --orange:#ff8c42; --red:#ff3e5d; --text:#d6f8ff;
 --muted:#6b9ba9;
}
*{box-sizing:border-box}
html,body{margin:0;width:100%;height:100%;background:var(--bg);
 color:var(--text);font-family:Consolas,"Courier New",monospace}
body{overflow:auto;background:
 radial-gradient(ellipse at 50% 40%,#0a2638 0%,#030811 65%)}
button{font-family:inherit;cursor:pointer}
#app{min-height:100vh;padding:12px;display:flex;flex-direction:column;gap:10px}
header{display:flex;justify-content:space-between;align-items:center;
 gap:12px;border-bottom:1px solid var(--line);padding:8px 4px 13px}
.brand{font-size:clamp(20px,2.5vw,36px);font-weight:bold;letter-spacing:4px;
 color:var(--cyan);text-shadow:0 0 15px #00cce8}
.subtitle{font-size:11px;color:var(--muted);letter-spacing:2px;margin-top:5px}
.header-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.pill{border:1px solid var(--line);padding:8px 11px;font-size:11px}
.online{color:var(--green)}
.btn{background:#08202c;color:var(--cyan);border:1px solid #12627b;padding:9px 12px}
.layout{display:grid;grid-template-columns:minmax(270px,1fr) minmax(300px,1.35fr) minmax(250px,1fr);
 gap:10px;flex:1}
.panel{position:relative;background:linear-gradient(145deg,#081724dd,#030a13f5);
 border:1px solid var(--line);padding:14px;min-width:0;overflow:hidden}
.panel:before{content:"";position:absolute;top:0;left:0;width:55px;height:2px;background:var(--cyan)}
.heading{font-size:12px;color:var(--cyan);letter-spacing:2px;margin-bottom:13px}
.status{font-size:clamp(18px,2vw,27px);font-weight:bold;line-height:1.3;
 color:var(--green);text-shadow:0 0 14px currentColor}
.small{font-size:10px;color:var(--muted);letter-spacing:1px}
.metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:17px}
.metric{border:1px solid #102e3d;padding:12px;background:#04101a}
.metric .value{font-size:clamp(18px,2vw,28px);color:var(--cyan);margin-top:8px}
.metric .unit{font-size:10px;color:var(--muted)}
.bar{height:7px;background:#102331;margin-top:10px;overflow:hidden}
.bar>div{height:100%;width:0;background:var(--cyan);transition:width .2s}
.radar-panel{display:flex;flex-direction:column;align-items:center}
.radar{position:relative;width:min(100%,min(49vh,470px));aspect-ratio:1;
 border-radius:50%;border:1px solid #16708a;
 background:repeating-radial-gradient(circle,transparent 0 12%,#0c3b4b 12.2% 12.5%,transparent 12.7% 24%),
 linear-gradient(90deg,transparent 49.8%,#174355 50%,transparent 50.2%),
 linear-gradient(transparent 49.8%,#174355 50%,transparent 50.2%);
 box-shadow:0 0 25px #00394c66,inset 0 0 25px #00394c66;overflow:hidden}
.radar:before{content:"";position:absolute;inset:0;border-radius:50%;
 background:conic-gradient(from 0deg,transparent 0deg,transparent 310deg,#00eaff08 330deg,#00eaff55 355deg,#00eaffaa 360deg);
 animation:sweep 5s linear infinite}
.radar:after{content:"";position:absolute;left:50%;top:50%;width:7px;height:7px;
 transform:translate(-50%,-50%);border-radius:50%;background:var(--cyan);
 box-shadow:0 0 15px var(--cyan)}
@keyframes sweep{to{transform:rotate(360deg)}}
.cross{position:absolute;background:#164253}
.cross.h{height:1px;left:0;right:0;top:50%}
.cross.v{width:1px;top:0;bottom:0;left:50%}
.event-point{position:absolute;width:12px;height:12px;border-radius:50%;
 transform:translate(-50%,-50%);z-index:3;display:none;
 background:var(--yellow);box-shadow:0 0 16px currentColor}
.event-point.active{display:block;animation:pulse 1s infinite}
@keyframes pulse{50%{opacity:.35;box-shadow:0 0 28px currentColor}}
.radar-label{margin-top:12px;font-size:11px;color:var(--muted);text-align:center}
.right-stack{display:flex;flex-direction:column;gap:10px}
.levels{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin-top:13px}
.level{height:6px;background:#152734}
.level.on:nth-child(1){background:var(--yellow)}
.level.on:nth-child(2){background:var(--orange)}
.level.on:nth-child(3){background:#ff654f}
.level.on:nth-child(4){background:var(--red)}
#chart{width:100%;height:150px;display:block}
.table{width:100%;border-collapse:collapse;font-size:11px}
.table th,.table td{text-align:left;padding:8px 4px;border-bottom:1px solid #102938}
.table th{color:var(--muted);font-weight:normal}
.footer{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;
 border-top:1px solid var(--line);padding:10px 4px;color:var(--muted);font-size:10px}
.color0{color:var(--green)}.color1{color:var(--yellow)}
.color2{color:var(--orange)}.color3{color:#ff654f}.color4{color:var(--red)}
@media(max-width:1050px){.layout{grid-template-columns:1fr 1fr}.radar-panel{grid-column:1/-1;grid-row:1}
 .radar{width:min(70vw,390px)}}
@media(max-width:650px){#app{padding:7px}.layout{grid-template-columns:1fr}
 .radar-panel{grid-column:auto;grid-row:auto}.header-right{justify-content:flex-end}
 header{align-items:flex-start}.brand{letter-spacing:2px}.panel{padding:11px}}
</style>
</head>
<body>
<div id="app">
<header>
 <div>
  <div class="brand">SEISMIC CORE X</div>
  <div class="subtitle">VIBRATION MONITORING SYSTEM // REAL TIME</div>
 </div>
 <div class="header-right">
  <div class="pill online" id="connection">● CONECTANDO</div>
  <button class="btn" onclick="activarSonido()">ACTIVAR AUDIO</button>
  <button class="btn" onclick="document.documentElement.requestFullscreen?.()">PANTALLA COMPLETA</button>
 </div>
</header>

<div class="layout">
 <section class="panel">
  <div class="heading">01 // ESTADO DEL SISTEMA</div>
  <div class="small">ESTADO ACTUAL</div>
  <div class="status" id="status">ESPERANDO SENSOR</div>
  <div class="levels">
   <div class="level"></div><div class="level"></div>
   <div class="level"></div><div class="level"></div>
  </div>
  <div class="metrics">
   <div class="metric"><div class="small">MAGNITUD</div><div class="value" id="mag">0.000</div><div class="unit">UNIDADES DEL SENSOR</div></div>
   <div class="metric"><div class="small">SEÑAL FILTRADA</div><div class="value" id="filtered">0.000</div><div class="unit">PROMEDIO MOVIL</div></div>
   <div class="metric"><div class="small">DURACION</div><div class="value" id="duration">0.0 s</div><div class="unit">EVENTO ACTUAL</div></div>
   <div class="metric"><div class="small">PAQUETES</div><div class="value" id="packets">0</div><div class="unit">RECIBIDOS</div></div>
  </div>
  <div class="metric" style="margin-top:8px">
   <div class="small">INTENSIDAD RELATIVA</div>
   <div class="bar"><div id="bar"></div></div>
   <div class="small" id="intensity" style="margin-top:7px">0 %</div>
  </div>
  <div class="metrics">
   <div class="metric"><div class="small">EJE X</div><div class="value" id="x">0.000</div></div>
   <div class="metric"><div class="small">EJE Y</div><div class="value" id="y">0.000</div></div>
   <div class="metric"><div class="small">EJE Z</div><div class="value" id="z">0.000</div></div>
   <div class="metric"><div class="small">DIRECCION</div><div class="value" id="direction" style="font-size:14px">--</div></div>
  </div>
 </section>

 <section class="panel radar-panel">
  <div class="heading" style="align-self:flex-start">02 // RADAR DE ACTIVIDAD</div>
  <div class="radar" id="radar">
   <div class="cross h"></div><div class="cross v"></div>
   <div class="event-point" id="point"></div>
  </div>
  <div class="radar-label" id="radarText">ESCANEANDO AREA // SISTEMA EN ESPERA</div>
  <div class="metrics" style="width:100%">
   <div class="metric"><div class="small">ULTIMO EVENTO</div><div class="value" id="lastEvent" style="font-size:18px">NINGUNO</div></div>
   <div class="metric"><div class="small">REGISTRO</div><div class="value" id="eventCount" style="font-size:18px">0</div></div>
  </div>
 </section>

 <div class="right-stack">
  <section class="panel">
   <div class="heading">03 // TRAZA DE VIBRACION</div>
   <canvas id="chart"></canvas>
   <div class="small">HISTORIAL RECIENTE // ACTUALIZACION AUTOMATICA</div>
  </section>
  <section class="panel" style="flex:1">
   <div class="heading">04 // REGISTRO DE EVENTOS</div>
   <table class="table">
    <thead><tr><th>HORA</th><th>EVENTO</th><th>MAX.</th><th>SEG.</th></tr></thead>
    <tbody id="events"><tr><td colspan="4">ESPERANDO EVENTOS...</td></tr></tbody>
   </table>
  </section>
 </div>
</div>

<div class="footer">
 <span>SEISMIC CORE X // MONITOREO EXPERIMENTAL</span>
 <span id="updated">ACTUALIZACION: --</span>
 <span>RADAR ACTIVO // <span id="audioStatus">AUDIO DESACTIVADO</span></span>
</div>
</div>

<script>
let audioCtx=null, audioEnabled=false, lastLevel=0, lastSweep=0;
let lastRevolution=0, lastEventState=false, lastEventSignature="";
let lastData=null, eventCount=0, pointX=50, pointY=50;
let points=[], sweepStart=performance.now();

function activarSonido(){
 try{
  audioCtx=audioCtx||new (window.AudioContext||window.webkitAudioContext)();
  audioCtx.resume();
  audioEnabled=true;
  document.getElementById("audioStatus").textContent="AUDIO ACTIVADO";
  bip(680,0.07,0.035);
 }catch(e){
  document.getElementById("audioStatus").textContent="AUDIO NO DISPONIBLE";
 }
}
function tono(freq,duration,gain=0.045,type="sine"){
 if(!audioEnabled||!audioCtx)return;
 const osc=audioCtx.createOscillator(), vol=audioCtx.createGain();
 osc.type=type;osc.frequency.value=freq;
 vol.gain.setValueAtTime(0.0001,audioCtx.currentTime);
 vol.gain.exponentialRampToValueAtTime(gain,audioCtx.currentTime+0.01);
 vol.gain.exponentialRampToValueAtTime(0.0001,audioCtx.currentTime+duration);
 osc.connect(vol);vol.connect(audioCtx.destination);
 osc.start();osc.stop(audioCtx.currentTime+duration+0.02);
}
function bip(freq=900,duration=0.055,gain=0.025){tono(freq,duration,gain,"sine")}
function sonidoNivel(n){
 if(n===1)bip(540,0.09,0.025);
 if(n===2){tono(620,0.13,0.035);setTimeout(()=>tono(780,0.12,0.03),130)}
 if(n===3){tono(820,0.18,0.05,"triangle");setTimeout(()=>tono(640,0.2,0.045,"triangle"),220)}
 if(n>=4){tono(950,0.22,0.055,"sawtooth");setTimeout(()=>tono(720,0.22,0.05,"sawtooth"),250);setTimeout(()=>tono(950,0.22,0.05,"sawtooth"),500)}
}
function colorNivel(n){
 return ["#35ffb0","#ffd45a","#ff8c42","#ff654f","#ff3e5d"][Math.max(0,Math.min(4,n))];
}
function moverPunto(data){
 const point=document.getElementById("point");
 if(!data.evento_activo){point.classList.remove("active");return}
 if(!point.classList.contains("active")){
  pointX=32+Math.random()*36;pointY=32+Math.random()*36;
 }
 point.classList.add("active");
 point.style.left=pointX+"%";point.style.top=pointY+"%";
 point.style.background=colorNivel(data.nivel||0);
 point.style.color=colorNivel(data.nivel||0);
 point.style.boxShadow="0 0 18px "+colorNivel(data.nivel||0);
}
function actualizar(data){
 lastData=data;
 document.getElementById("connection").textContent="● "+(data.calibrando?"CALIBRANDO":"SENSOR EN LINEA");
 document.getElementById("connection").style.color=data.calibrando?"#ffd45a":"#35ffb0";
 const n=data.nivel||0;
 const status=document.getElementById("status");
 status.textContent=data.estado||"SIN DATOS";
 status.className="status color"+n;
 document.getElementById("mag").textContent=(data.magnitud||0).toFixed(4);
 document.getElementById("filtered").textContent=(data.filtrada||0).toFixed(4);
 document.getElementById("duration").textContent=(data.duracion||0).toFixed(1)+" s";
 document.getElementById("packets").textContent=data.contador||0;
 document.getElementById("x").textContent=(data.x||0).toFixed(3);
 document.getElementById("y").textContent=(data.y||0).toFixed(3);
 document.getElementById("z").textContent=(data.z||0).toFixed(3);
 document.getElementById("direction").textContent=data.direccion||"--";
 document.getElementById("lastEvent").textContent=data.ultimo_evento||"NINGUNO";
 const intensity=Math.min(100,Math.max(0,(data.filtrada||0)/UMBRAL_VISUAL*100));
 document.getElementById("bar").style.width=intensity+"%";
 document.getElementById("bar").style.background=colorNivel(n);
 document.getElementById("intensity").textContent=intensity.toFixed(1)+" %";
 document.querySelectorAll(".level").forEach((el,i)=>el.classList.toggle("on",i<n));
 document.getElementById("radarText").textContent=data.evento_activo?
  "EVENTO ACTIVO // NIVEL "+n:"ESCANEANDO AREA // SISTEMA EN ESPERA";
 moverPunto(data);
 document.getElementById("updated").textContent="ACTUALIZACION: "+new Date().toLocaleTimeString();
 points=(data.historial||[]).map(v=>v.v);
 dibujarGrafico();
 pintarEventos(data.eventos||[]);
 document.getElementById("eventCount").textContent=(data.eventos||[]).length;
 if(n>lastLevel && n>0)sonidoNivel(n);
 lastLevel=n;
}
const UMBRAL_VISUAL=0.12;
function pintarEventos(ev){
 const tbody=document.getElementById("events");
 if(!ev.length){tbody.innerHTML='<tr><td colspan="4">ESPERANDO EVENTOS...</td></tr>';return}
 tbody.innerHTML=ev.slice(0,8).map(e=>
 `<tr><td>${esc(e.hora)}</td><td style="color:${colorNivel(e.nivel)}">${esc(e.estado)}</td><td>${Number(e.maximo).toFixed(3)}</td><td>${Number(e.duracion).toFixed(1)}</td></tr>`
 ).join("");
}
function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function dibujarGrafico(){
 const c=document.getElementById("chart"),ctx=c.getContext("2d");
 const rect=c.getBoundingClientRect(),dpr=window.devicePixelRatio||1;
 c.width=rect.width*dpr;c.height=rect.height*dpr;
 ctx.scale(dpr,dpr);
 const w=rect.width,h=rect.height;
 ctx.clearRect(0,0,w,h);
 ctx.strokeStyle="#12384b";ctx.lineWidth=1;
 for(let i=1;i<5;i++){ctx.beginPath();ctx.moveTo(0,h*i/5);ctx.lineTo(w,h*i/5);ctx.stroke()}
 if(points.length<2)return;
 const max=Math.max(0.15,...points);
 ctx.beginPath();
 points.forEach((v,i)=>{
  const x=i*w/(points.length-1),y=h-8-(v/max)*(h-16);
  i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);
 });
 ctx.strokeStyle="#00eaff";ctx.lineWidth=2;ctx.shadowBlur=8;ctx.shadowColor="#00eaff";ctx.stroke();ctx.shadowBlur=0;
}
async function consultar(){
 try{
  const r=await fetch("/estado",{cache:"no-store"});
  if(!r.ok)throw Error("HTTP "+r.status);
  actualizar(await r.json());
 }catch(e){
  document.getElementById("connection").textContent="● SIN CONEXION";
  document.getElementById("connection").style.color="#ff3e5d";
 }
}
setInterval(consultar,500);
consultar();
window.addEventListener("resize",dibujarGrafico);

// Bip al completar cada vuelta del radar.
function animarRadar(t){
 const vuelta=Math.floor((t-sweepStart)/5000);
 if(vuelta!==lastRevolution){
  lastRevolution=vuelta;
  bip(1050,0.055,0.022);
 }
 requestAnimationFrame(animarRadar);
}
requestAnimationFrame(animarRadar);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
