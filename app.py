from flask import Flask, request, jsonify, Response
from collections import deque
from threading import Lock, Thread
import json, math, os, time, urllib.request, urllib.error, html

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
        '
