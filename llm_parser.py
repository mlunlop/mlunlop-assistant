import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, Optional
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_TEMPLATE = """Eres el cerebro de procesamiento de lenguaje natural de un asistente personal en Telegram.
Tu único trabajo es clasificar la intención del usuario y devolver EXCLUSIVAMENTE un objeto JSON válido, sin explicaciones ni markdown.

La fecha y hora actual del usuario es: {current_time} (Zona horaria: {timezone}).
Utiliza este momento de referencia para interpretar fechas y horas relativas (ej. "mañana a las 5pm", "en 15 minutos", "el próximo viernes").
Si el usuario especifica una cita de calendario sin duración, asume 1 hora por defecto.

Estructuras JSON permitidas según la intención:

1. Añadir evento al calendario:
{{
  "intencion": "calendario",
  "titulo": "string con el nombre del evento",
  "inicio": "YYYY-MM-DDTHH:MM:SS",
  "fin": "YYYY-MM-DDTHH:MM:SS"
}}

2. Programar un recordatorio:
{{
  "intencion": "recordatorio",
  "mensaje": "texto del recordatorio",
  "hora_ejecucion": "YYYY-MM-DDTHH:MM:SS"
}}

3. Gestionar lista de la compra:
{{
  "intencion": "compra",
  "accion": "añadir" | "listar" | "eliminar" | "limpiar",
  "item": "nombre del producto" (opcional si accion es listar o limpiar)
}}

4. Intención no reconocida o conversación general:
{{
  "intencion": "desconocido",
  "mensaje": "Respuesta breve aclaratoria o aviso de que solo gestionas calendario, recordatorios y compras"
}}

REGLAS CRÍTICAS:
- Tu respuesta DEBE ser un JSON parseable directamente.
- No incluyas bloques de código markdown como ```json ... ```. Solo el texto crudo del JSON.
- Asegúrate de que las fechas estén en formato ISO 8601 estricto (YYYY-MM-DDTHH:MM:SS).
"""


def _clean_json_response(raw_text: str) -> str:
    """Limpia posibles bloques markdown o caracteres extra antes de parsear."""
    text = raw_text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        text = match.group(1).strip()
    return text


def _call_gemini(prompt: str, current_time_str: str, timezone: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY no está configurada en las variables de entorno.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    system_instruction = SYSTEM_PROMPT_TEMPLATE.format(
        current_time=current_time_str,
        timezone=timezone
    )

    payload = {
        "system_instruction": {
            "parts": [{"text": system_instruction}]
        },
        "contents": [
            {
                "parts": [{"text": prompt}]
            }
        ],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.1
        }
    }

    logger.debug("Llamando a la API de Gemini...")
    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()
    data = response.json()

    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError("La API de Gemini devolvió una lista vacía de candidatos.")

    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts:
        raise ValueError("La respuesta de Gemini no contiene partes de texto.")

    return parts[0].get("text", "")


def _call_deepseek(prompt: str, current_time_str: str, timezone: str) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY no está configurada en las variables de entorno.")

    url = "https://api.deepseek.com/chat/completions"
    system_instruction = SYSTEM_PROMPT_TEMPLATE.format(
        current_time=current_time_str,
        timezone=timezone
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1
    }

    logger.debug("Llamando a la API de DeepSeek...")
    response = requests.post(url, json=payload, headers=headers, timeout=15)
    response.raise_for_status()
    data = response.json()

    choices = data.get("choices", [])
    if not choices:
        raise ValueError("La respuesta de DeepSeek no contiene elecciones.")

    return choices[0].get("message", {}).get("content", "")


def parse_user_intent(user_text: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Procesa el texto del usuario con el LLM seleccionado y devuelve un diccionario validado.
    """
    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    timezone = os.getenv("TIMEZONE", "Europe/Madrid")
    
    current_dt = now or datetime.now()
    current_time_str = current_dt.strftime("%Y-%m-%dT%H:%M:%S")

    logger.info("Analizando intención con proveedor '%s' para texto: '%s'", provider, user_text)

    try:
        if provider == "deepseek":
            raw_output = _call_deepseek(user_text, current_time_str, timezone)
        else:
            raw_output = _call_gemini(user_text, current_time_str, timezone)

        cleaned_output = _clean_json_response(raw_output)
        parsed = json.loads(cleaned_output)

        if not isinstance(parsed, dict) or "intencion" not in parsed:
            raise ValueError(f"JSON inválido o sin clave 'intencion': {parsed}")

        logger.info("Intención detectada con éxito: %s", parsed.get("intencion"))
        return parsed

    except requests.RequestException as e:
        logger.error("Error de conexión al llamar al LLM (%s): %s", provider, e)
        return {
            "intencion": "desconocido",
            "mensaje": f"Error de comunicación con el servicio de IA ({provider}). Inténtalo de nuevo."
        }
    except json.JSONDecodeError as e:
        logger.error("Error al decodificar JSON del LLM: %s. Salida cruda: %s", e, raw_output)
        return {
            "intencion": "desconocido",
            "mensaje": "No pude estructurar la información. Por favor, reformula tu petición."
        }
    except Exception as e:
        logger.error("Excepción inesperada en parse_user_intent: %s", e, exc_info=True)
        return {
            "intencion": "desconocido",
            "mensaje": f"Ocurrió un error inesperado al procesar tu mensaje: {e}"
        }

