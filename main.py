# main.py — EFFICON API Gateway + PostgreSQL Database + ChatGPT Inteligente
from flask import Flask, request, jsonify
import os, requests, traceback, json
from datetime import datetime
from urllib.parse import urlparse
import pg8000.dbapi

app = Flask(__name__)

# ================= Configuración OpenAI =================
API_KEY    = os.getenv("OPENAI_API_KEY", "")
PROJECT_ID = os.getenv("OPENAI_PROJECT_ID", "")
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
MODEL      = os.getenv("MODEL", "gpt-4o")

# ================= Configuración PostgreSQL (pg8000) =================
DATABASE_URL = os.getenv("DATABASE_URL", "")

def get_db_connection():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL no está configurada en Railway.")
    
    # Desarmamos la URL para dársela en bandeja de plata a pg8000
    parsed = urlparse(DATABASE_URL)
    return pg8000.dbapi.connect(
        user=parsed.username,
        password=parsed.password,
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=parsed.path.lstrip('/')
    )

def init_db():
    if not DATABASE_URL:
        print("Aviso: DATABASE_URL no detectada.")
        return
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Ocultar advertencias si la secuencia ya existe
        conn.autocommit = True 
        cur.execute("CREATE SEQUENCE IF NOT EXISTS tramite_seq START 1;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tramites_efficom (
                id_tramite VARCHAR(50) PRIMARY KEY,
                estado VARCHAR(50),
                datos_completos JSONB,
                fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.autocommit = False # Restaurar seguridad
        
        cur.close()
        conn.close()
        print("PostgreSQL inicializado correctamente con pg8000.")
    except Exception as e:
        print(f"Error inicializando BD: {e}")

# Ejecutar inicialización al arrancar
init_db()

# =================================================================
# RUTA 1: CREAR TRÁMITE NUEVO (ID Personalizado desde C8)
# =================================================================
@app.post("/guardar_tramite")
def guardar_tramite():
    try:
        payload = request.get_json(silent=True)
        if not payload:
            return jsonify({"ok": False, "error": "No se recibieron datos JSON"}), 400
            
        estado_inicial = payload.get("estado", "EN_COMPRAS")
        
        # 1. Atrapamos el prefijo que viene de la fórmula en C8
        # .strip().upper() asegura que no haya espacios y todo sea mayúsculas
        prefijo = str(payload.get("prefijo_tramite", "REQ")).strip().upper()
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 2. Obtenemos el siguiente número de la secuencia global
        cur.execute("SELECT nextval('tramite_seq');")
        secuencia = cur.fetchone()[0]
        anio_actual = datetime.now().year
        
        # 3. Ensamblamos el ID Maestro: SIGLAS-AÑO-SECUENCIA
        # Resultado ej: IC-CBCC-2026-0001
        nuevo_id = f"{prefijo}-{anio_actual}-{str(secuencia).zfill(4)}"
        
        # Guardamos el JSON completo
        json_string = json.dumps(payload)
        cur.execute("""
            INSERT INTO tramites_efficom (id_tramite, estado, datos_completos)
            VALUES (%s, %s, cast(%s as jsonb))
        """, (nuevo_id, estado_inicial, json_string))
        
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"ok": True, "mensaje": "Éxito", "id_tramite": nuevo_id}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

# =================================================================
# RUTA 2: DESCARGAR TRÁMITE (Para Compras Públicas)
# =================================================================
@app.get("/obtener_tramite/<id_tramite>")
def obtener_tramite(id_tramite):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Buscamos el trámite exacto en la base de datos
        cur.execute("""
            SELECT estado, datos_completos 
            FROM tramites_efficom 
            WHERE id_tramite = %s
        """, (id_tramite,))
        resultado = cur.fetchone()
        
        cur.close()
        conn.close()

        if resultado:
            estado, datos_completos = resultado
            return jsonify({
                "ok": True,
                "id_tramite": id_tramite,
                "estado": estado,
                "datos_completos": datos_completos
            }), 200
        else:
            return jsonify({"ok": False, "error": "Trámite no encontrado en la base de datos"}), 404

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

# =================================================================
# RUTA 3: ACTUALIZAR TRÁMITE EXISTENTE (Correcciones UR o Compras)
# =================================================================
@app.post("/actualizar_tramite")
def actualizar_tramite():
    try:
        payload = request.get_json(silent=True)
        if not payload or "id_tramite" not in payload:
            return jsonify({"ok": False, "error": "Falta el ID del trámite para actualizar"}), 400
            
        # Sacamos el ID del paquete para saber a quién actualizar
        id_tramite = payload.pop("id_tramite") 
        json_string = json.dumps(payload)
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Buscamos la fila exacta y le caemos encima con los datos nuevos
        cur.execute("""
            UPDATE tramites_efficom 
            SET datos_completos = datos_completos || cast(%s as jsonb),
                fecha_actualizacion = CURRENT_TIMESTAMP
            WHERE id_tramite = %s
        """, (json_string, id_tramite))
        
        filas_afectadas = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()

        if filas_afectadas == 0:
            return jsonify({"ok": False, "error": "El trámite no existe en la base de datos."}), 404

        return jsonify({"ok": True, "mensaje": "Actualizado", "id_tramite": id_tramite}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

# =================================================================
# RUTA 4: CHATGPT (Motor de Inteligencia EFFICON)
# =================================================================
def openai_call(messages, max_tokens=2500, temperature=0.2, timeout_s=180):
    if not API_KEY:
        return {"ok": False, "status": 500, "text": "Error: API_KEY no configurada"}
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    if PROJECT_ID: headers["OpenAI-Project"] = PROJECT_ID
    payload = {"model": MODEL, "max_tokens": max_tokens, "temperature": temperature, "messages": messages}

    try:
        r = requests.post(OPENAI_URL, headers=headers, json=payload, timeout=timeout_s)
        data = r.json()
        opciones = data.get("choices", [])
        
        if not opciones:
            text = f"EFFICON INFORMA: {data.get('error', 'Error desconocido')}"
        else:
            text = opciones[0].get("message", {}).get("content", "").strip()

        ok = (r.status_code == 200 and "EFFICON INFORMA" not in text)
        return {"ok": ok, "text": text, "error": None if ok else text}
    except Exception as e:
        return {"ok": False, "text": f"Error de red: {e}"}

@app.post("/chatgpt")
def chatgpt():
    data = request.get_json(silent=True) or {}
    user_prompt = (data.get("prompt") or "").strip()
    system_msg_cliente = (data.get("system") or "Eres un asistente útil.").strip()

    if not user_prompt:
        return jsonify({"ok": False, "text": "Prompt vacío"}), 200

    potenciador_cognitivo = (
        "DIRECTIVA DE RAZONAMIENTO AVANZADO: Asume inmediatamente el rol. "
        "REGLAS: 1. Piensa paso a paso. 2. ESTRICTAMENTE PROHIBIDO usar frases de relleno. "
        "3. Entrega un resultado final impecable y directo al grano."
    )
    system_msg_final = f"{potenciador_cognitivo}\n\nINSTRUCCIONES DEL USUARIO:\n{system_msg_cliente}"
    messages = [{"role": "system", "content": system_msg_final}, {"role": "user", "content": user_prompt}]

    res = openai_call(messages)
    return jsonify(res), 200

@app.get("/")
def home():
    return jsonify({"ok": True, "message": "EFFICON Server Activo con Sistema de Actualización CRUD."}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))