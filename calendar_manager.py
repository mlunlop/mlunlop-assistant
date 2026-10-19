import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_timezone() -> ZoneInfo:
    tz_name = os.getenv("TIMEZONE", "Europe/Madrid")
    try:
        return ZoneInfo(tz_name)
    except Exception as e:
        logger.warning("Zona horaria inválida '%s', usando UTC: %s", tz_name, e)
        return ZoneInfo("UTC")


def _ensure_timezone(dt: datetime, tz: ZoneInfo) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def get_calendar_service():
    """
    Obtiene el servicio autenticado de Google Calendar API.
    Maneja el flujo OAuth2 y almacena el token en token.json.
    """
    token_file = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
    credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    creds: Optional[Credentials] = None

    if os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
            logger.debug("Token cargado desde %s", token_file)
        except Exception as e:
            logger.warning("Error leyendo %s: %s. Se regenerará.", token_file, e)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refrescando token de acceso expirado...")
            try:
                creds.refresh(Request())
            except Exception as e:
                logger.error("Error al refrescar token: %s", e)
                creds = None

        if not creds:
            if not os.path.exists(credentials_file):
                logger.error("No se encontró el archivo de credenciales: %s", credentials_file)
                raise FileNotFoundError(
                    f"No se encontró '{credentials_file}'. Descárgalo desde Google Cloud Console."
                )

            logger.info("Iniciando flujo de autenticación OAuth local...")
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_file, "w", encoding="utf-8") as token:
            token.write(creds.to_json())
            logger.info("Credenciales guardadas en %s", token_file)

    return build("calendar", "v3", credentials=creds)


def check_conflicts(
    start_iso: str,
    end_iso: str,
    margin_minutes: int = 30
) -> Tuple[bool, Optional[str]]:
    """
    Verifica si existen eventos solapados o con margen menor a `margin_minutes`.
    Devuelve (True, nombre_evento_en_conflicto) si hay choque o margen insuficiente,
    o (False, None) si el horario está libre.
    """
    tz = _get_timezone()
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")

    new_start = _ensure_timezone(datetime.fromisoformat(start_iso), tz)
    new_end = _ensure_timezone(datetime.fromisoformat(end_iso), tz)

    if new_end <= new_start:
        new_end = new_start + timedelta(hours=1)

    margin = timedelta(minutes=margin_minutes)
    search_start = new_start - margin
    search_end = new_end + margin

    logger.info(
        "Buscando conflictos entre %s y %s (con margen de %d min)",
        search_start.isoformat(),
        search_end.isoformat(),
        margin_minutes
    )

    try:
        service = get_calendar_service()
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=search_start.isoformat(),
            timeMax=search_end.isoformat(),
            singleEvents=True,
            orderBy="startTime"
        ).execute()

        items = events_result.get("items", [])
        if not items:
            logger.info("No hay eventos en la ventana horaria analizada.")
            return False, None

        for event in items:
            event_summary = event.get("summary", "Sin título")
            event_start_raw = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
            event_end_raw = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")

            if not event_start_raw or not event_end_raw:
                continue

            # Manejar eventos de todo el día (formato YYYY-MM-DD)
            if "T" not in event_start_raw:
                # Evento de día completo: ignorar o evaluar si coincide con la fecha
                event_start_dt = datetime.strptime(event_start_raw, "%Y-%m-%d").replace(tzinfo=tz)
                event_end_dt = datetime.strptime(event_end_raw, "%Y-%m-%d").replace(tzinfo=tz)
            else:
                event_start_dt = _ensure_timezone(datetime.fromisoformat(event_start_raw), tz)
                event_end_dt = _ensure_timezone(datetime.fromisoformat(event_end_raw), tz)

            # Condición de solapamiento con margen:
            # Choque si el evento existente inicia antes de (new_end + margin)
            # Y finaliza después de (new_start - margin)
            has_conflict = (event_start_dt < search_end) and (event_end_dt > search_start)
            if has_conflict:
                start_fmt = event_start_dt.strftime("%H:%M")
                end_fmt = event_end_dt.strftime("%H:%M")
                logger.warning(
                    "Conflicto detectado con '%s' (%s - %s)",
                    event_summary,
                    start_fmt,
                    end_fmt
                )
                return True, f"{event_summary} ({start_fmt} - {end_fmt})"

        return False, None

    except HttpError as e:
        logger.error("Error al consultar Google Calendar: %s", e)
        raise
    except Exception as e:
        logger.error("Excepción inesperada comprobando conflictos: %s", e, exc_info=True)
        raise


def create_event(
    title: str,
    start_iso: str,
    end_iso: str
) -> Dict[str, Any]:
    """
    Inserta un nuevo evento en Google Calendar.
    """
    tz_name = os.getenv("TIMEZONE", "Europe/Madrid")
    tz = _get_timezone()
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")

    new_start = _ensure_timezone(datetime.fromisoformat(start_iso), tz)
    new_end = _ensure_timezone(datetime.fromisoformat(end_iso), tz)

    if new_end <= new_start:
        new_end = new_start + timedelta(hours=1)

    event_body = {
        "summary": title,
        "description": "Añadido automáticamente por mlunlop-assistant",
        "start": {
            "dateTime": new_start.isoformat(),
            "timeZone": tz_name,
        },
        "end": {
            "dateTime": new_end.isoformat(),
            "timeZone": tz_name,
        },
    }

    try:
        service = get_calendar_service()
        created_event = service.events().insert(
            calendarId=calendar_id,
            body=event_body
        ).execute()

        logger.info("Evento creado con éxito: ID %s", created_event.get("id"))
        return created_event

    except HttpError as e:
        logger.error("Error al insertar evento en Google Calendar: %s", e)
        raise

