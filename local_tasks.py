import logging
import os
import sqlite3
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Instancia global del scheduler asíncrono
_scheduler: Optional[AsyncIOScheduler] = None


def get_db_path() -> str:
    return os.getenv("SQLITE_DB_PATH", "assistant.db")


def init_db(db_path: Optional[str] = None) -> None:
    """Inicializa la base de datos SQLite y crea las tablas si no existen."""
    path = db_path or get_db_path()
    logger.info("Inicializando base de datos SQLite en: %s", path)

    with sqlite3.connect(path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lista_compra (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    logger.debug("Esquema de base de datos verificado.")


def add_shopping_item(item: str, db_path: Optional[str] = None) -> bool:
    """Añade un artículo a la lista de la compra. Devuelve True si se añadió, False si ya existía."""
    path = db_path or get_db_path()
    cleaned = item.strip()
    if not cleaned:
        return False

    with sqlite3.connect(path) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO lista_compra (item) VALUES (?)", (cleaned,))
            conn.commit()
            logger.info("Artículo añadido a la lista de compra: '%s'", cleaned)
            return True
        except sqlite3.IntegrityError:
            logger.info("El artículo '%s' ya estaba en la lista de compra.", cleaned)
            return False


def get_shopping_items(db_path: Optional[str] = None) -> List[str]:
    """Obtiene todos los artículos pendientes en la lista de la compra."""
    path = db_path or get_db_path()
    with sqlite3.connect(path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT item FROM lista_compra ORDER BY id ASC")
        rows = cursor.fetchall()
        return [row[0] for row in rows]


def remove_shopping_item(item: str, db_path: Optional[str] = None) -> bool:
    """Elimina un artículo específico de la lista. Devuelve True si se eliminó."""
    path = db_path or get_db_path()
    cleaned = item.strip()

    with sqlite3.connect(path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM lista_compra WHERE item = ? COLLATE NOCASE", (cleaned,))
        conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Artículo eliminado de la compra: '%s'", cleaned)
        else:
            logger.info("No se encontró el artículo '%s' para eliminar.", cleaned)
        return deleted


def clear_shopping_list(db_path: Optional[str] = None) -> int:
    """Vacía por completo la lista de la compra y devuelve el número de elementos eliminados."""
    path = db_path or get_db_path()
    with sqlite3.connect(path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM lista_compra")
        count = cursor.rowcount
        conn.commit()
        logger.info("Lista de la compra vaciada. Total eliminados: %d", count)
        return count


# ==========================================
# Scheduler para recordatorios efímeros
# ==========================================

async def _send_reminder_job(bot, chat_id: int, message: str) -> None:
    """Función de callback ejecutada por APScheduler para notificar al usuario."""
    logger.info("Ejecutando trabajo de recordatorio para chat_id %s: '%s'", chat_id, message)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"⏰ *Recordatorio programado:*\n\n{message}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error("Error al enviar recordatorio a %s: %s", chat_id, e, exc_info=True)


def init_scheduler() -> AsyncIOScheduler:
    """Crea e inicia el scheduler si aún no está en marcha."""
    global _scheduler
    if _scheduler is None:
        tz_name = os.getenv("TIMEZONE", "Europe/Madrid")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")

        _scheduler = AsyncIOScheduler(timezone=tz)
        _scheduler.start()
        logger.info("AsyncIOScheduler iniciado con zona horaria %s", tz)
    return _scheduler


def schedule_reminder(bot, chat_id: int, message: str, run_date: datetime) -> str:
    """Programa un recordatorio a una fecha/hora específica."""
    scheduler = init_scheduler()
    job = scheduler.add_job(
        _send_reminder_job,
        trigger="date",
        run_date=run_date,
        args=[bot, chat_id, message],
        misfire_grace_time=120  # Si se retrasa hasta 2 min por sleep, aún se ejecuta
    )
    logger.info("Recordatorio programado (ID: %s) para %s", job.id, run_date.isoformat())
    return job.id

