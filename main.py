import functools
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from calendar_manager import check_conflicts, create_event
from llm_parser import parse_user_intent
from local_tasks import (
    add_shopping_item,
    clear_shopping_list,
    get_shopping_items,
    init_db,
    init_scheduler,
    remove_shopping_item,
    schedule_reminder,
)

# Configuración de Logging nativo
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("mlunlop_assistant")

load_dotenv()

# Validaciones iniciales de variables de entorno
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID_RAW = os.getenv("ALLOWED_CHAT_ID")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Falta la variable de entorno TELEGRAM_BOT_TOKEN en el archivo .env")

if not ALLOWED_CHAT_ID_RAW:
    raise ValueError("Falta la variable de entorno ALLOWED_CHAT_ID en el archivo .env")

ALLOWED_CHAT_ID = int(ALLOWED_CHAT_ID_RAW.strip())


def restricted(func):
    """
    Decorador de seguridad estricto.
    Rechaza cualquier mensaje cuyo chat_id no coincida exactamente con ALLOWED_CHAT_ID.
    """
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id != ALLOWED_CHAT_ID:
            logger.warning(
                "ACCESO DENEGADO: Intento no autorizado desde Chat ID %s (Usuario: %s)",
                chat_id,
                update.effective_user.username if update.effective_user else "desconocido"
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


# ==========================================
# Handlers de Comandos Básicos
# ==========================================

@restricted
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mensaje de bienvenida con resumen de capacidades."""
    welcome_text = (
        "👋 *¡Hola! Soy tu asistente personal mlunlop-assistant.*\n\n"
        "Puedes hablarme de forma completamente natural. Por ejemplo:\n"
        "• 📅 *Calendario:* _\"Apúntame cita con el dentista mañana a las 4 de la tarde\"_\n"
        "• ⏰ *Recordatorios:* _\"Recuérdame sacar el agua en 15 minutos\"_\n"
        "• 🛒 *Lista de compra:* _\"Añade café a la lista de la compra\"_ o _\"¿Qué hay en la compra?\"_\n\n"
        "Estoy listo para ayudarte."
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)


@restricted
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra la ayuda del asistente."""
    help_text = (
        "💡 *Comandos disponibles:*\n"
        "/start - Iniciar el asistente\n"
        "/help - Ver esta ayuda\n"
        "/compra - Ver tu lista de compra actual\n"
        "/limpiar_compra - Vaciar la lista de compra\n\n"
        "También puedes simplemente escribir o enviar mensajes de voz para que analice tu intención con IA."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


@restricted
async def view_shopping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando directo para inspeccionar la lista de la compra."""
    items = get_shopping_items()
    if not items:
        await update.message.reply_text("🛒 Tu lista de la compra está vacía.")
        return

    text_list = "\n".join([f"• {item}" for item in items])
    await update.message.reply_text(f"🛒 *Lista de la compra:*\n\n{text_list}", parse_mode=ParseMode.MARKDOWN)


@restricted
async def clear_shopping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando directo para vaciar la lista de la compra."""
    count = clear_shopping_list()
    await update.message.reply_text(f"🗑️ Se han eliminado {count} elementos de la lista de compra.")


# ==========================================
# Ruteo de Intenciones (LLM Router)
# ==========================================

@restricted
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Punto neurálgico:
    1. Recibe el texto en lenguaje natural.
    2. Lo procesa con el LLM parser.
    3. Encamina el flujo a Calendar, Recordatorios o SQLite.
    """
    user_text = update.message.text.strip()
    logger.info("Mensaje recibido de Chat ID %s: '%s'", update.effective_chat.id, user_text)

    # Indicador de 'escribiendo...' en Telegram mientras el LLM y las APIs procesan
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    tz_name = os.getenv("TIMEZONE", "Europe/Madrid")
    try:
        now_dt = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now_dt = datetime.now()

    intent_data = parse_user_intent(user_text, now=now_dt)
    intencion = intent_data.get("intencion")

    # 1. RUTA: CALENDARIO
    if intencion == "calendario":
        titulo = intent_data.get("titulo", "Evento sin título")
        inicio = intent_data.get("inicio")
        fin = intent_data.get("fin")

        if not inicio or not fin:
            await update.message.reply_text("⚠️ No pude determinar las horas de inicio y fin del evento. Por favor, especifícalas.")
            return

        try:
            has_conflict, conflicting_event = check_conflicts(inicio, fin, margin_minutes=30)
            if has_conflict:
                # Guardamos los datos temporalmente en user_data para el flujo interactivo
                context.user_data["pending_calendar_event"] = {
                    "titulo": titulo,
                    "inicio": inicio,
                    "fin": fin
                }
                keyboard = [
                    [
                        InlineKeyboardButton("✅ Sí, apuntar", callback_data="confirm_calendar"),
                        InlineKeyboardButton("❌ Cancelar", callback_data="cancel_calendar")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                msg = (
                    f"⚠️ *Tienes poco margen de tiempo*\n\n"
                    f"Tienes *{conflicting_event}* cerca o a esa hora.\n\n"
                    f"¿Deseas agendar *\"{titulo}\"* de todos modos?"
                )
                await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
                return

            # Sin conflicto: Crear evento directamente
            create_event(titulo, inicio, fin)
            inicio_fmt = datetime.fromisoformat(inicio).strftime("%d/%m/%Y a las %H:%M")
            await update.message.reply_text(
                f"✅ *Evento agendado en Google Calendar:*\n📅 *{titulo}*\n🕒 {inicio_fmt}",
                parse_mode=ParseMode.MARKDOWN
            )

        except FileNotFoundError as e:
            logger.error("Error de credenciales de Google Calendar: %s", e)
            await update.message.reply_text("⚠️ Google Calendar no está autenticado. Revisa credentials.json en el servidor.")
        except Exception as e:
            logger.error("Error gestionando evento de calendario: %s", e, exc_info=True)
            await update.message.reply_text(f"❌ Ocurrió un error al contactar con Google Calendar: {e}")

    # 2. RUTA: RECORDATORIO
    elif intencion == "recordatorio":
        mensaje = intent_data.get("mensaje", user_text)
        hora_ejecucion_raw = intent_data.get("hora_ejecucion")

        if not hora_ejecucion_raw:
            await update.message.reply_text("⚠️ No pude entender cuándo programar el recordatorio. Por favor, indícame la hora o fecha.")
            return

        try:
            run_dt = datetime.fromisoformat(hora_ejecucion_raw)
            if run_dt.tzinfo is None:
                run_dt = run_dt.replace(tzinfo=ZoneInfo(tz_name))

            now_cmp = datetime.now(ZoneInfo(tz_name))
            if run_dt <= now_cmp:
                await update.message.reply_text("⚠️ La hora indicada para el recordatorio ya ha pasado.")
                return

            schedule_reminder(context.bot, update.effective_chat.id, mensaje, run_dt)
            hora_fmt = run_dt.strftime("%d/%m/%Y a las %H:%M")
            await update.message.reply_text(
                f"⏰ *Recordatorio programado para:* {hora_fmt}\n📝 \"_{mensaje}_\"",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error("Error al programar recordatorio: %s", e, exc_info=True)
            await update.message.reply_text(f"❌ Error al programar el recordatorio: {e}")

    # 3. RUTA: LISTA DE LA COMPRA
    elif intencion == "compra":
        accion = intent_data.get("accion", "listar").lower()
        item = intent_data.get("item", "").strip()

        if accion == "añadir":
            if not item:
                await update.message.reply_text("¿Qué artículo quieres que añada a la lista?")
                return
            added = add_shopping_item(item)
            if added:
                await update.message.reply_text(f"🛒 Añadido a la lista de compra: *{item}*", parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(f"ℹ️ *{item}* ya estaba en tu lista de compra.", parse_mode=ParseMode.MARKDOWN)

        elif accion in ["eliminar", "borrar", "quitar"]:
            if not item:
                await update.message.reply_text("¿Qué artículo deseas eliminar?")
                return
            removed = remove_shopping_item(item)
            if removed:
                await update.message.reply_text(f"🗑️ Eliminado de la lista: *{item}*", parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(f"⚠️ *{item}* no se encontraba en la lista de compra.", parse_mode=ParseMode.MARKDOWN)

        elif accion in ["limpiar", "vaciar"]:
            count = clear_shopping_list()
            await update.message.reply_text(f"🗑️ Lista de compra vaciada ({count} elementos eliminados).")

        else:  # listar por defecto
            items = get_shopping_items()
            if not items:
                await update.message.reply_text("🛒 Tu lista de la compra está vacía.")
            else:
                text_list = "\n".join([f"• {i}" for i in items])
                await update.message.reply_text(f"🛒 *Lista de la compra:*\n\n{text_list}", parse_mode=ParseMode.MARKDOWN)

    # 4. RUTA: DESCONOCIDO O CONVERSACIONAL
    else:
        respuesta = intent_data.get("mensaje", "No estoy seguro de cómo procesar esa petición.")
        await update.message.reply_text(respuesta)


# ==========================================
# Callback Query Handler (Botones Inline)
# ==========================================

@restricted
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Maneja las pulsaciones de botones (confirmación o cancelación de eventos conflictivos)."""
    query = update.callback_query
    await query.answer()

    data = query.data
    logger.info("Callback query recibido: '%s'", data)

    if data == "confirm_calendar":
        pending = context.user_data.pop("pending_calendar_event", None)
        if not pending:
            await query.edit_message_text("⚠️ No se encontró la información del evento pendiente o ya expiró.")
            return

        try:
            create_event(pending["titulo"], pending["inicio"], pending["fin"])
            inicio_fmt = datetime.fromisoformat(pending["inicio"]).strftime("%d/%m/%Y a las %H:%M")
            await query.edit_message_text(
                f"✅ *Evento confirmado y añadido a tu Google Calendar:*\n📅 *{pending['titulo']}*\n🕒 {inicio_fmt}",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error("Error al forzar la inserción del evento: %s", e)
            await query.edit_message_text(f"❌ Error al añadir el evento: {e}")

    elif data == "cancel_calendar":
        context.user_data.pop("pending_calendar_event", None)
        await query.edit_message_text("❌ Se ha cancelado la creación del evento en tu calendario.")


# ==========================================
# Inicio del Bot
# ==========================================

async def post_init(application: Application) -> None:
    """Se ejecuta justo después de iniciar la aplicación de Telegram."""
    logger.info("Ejecutando post_init: Inicializando DB y Scheduler...")
    init_db()
    init_scheduler()
    logger.info("Bot listo y escuchando actualizaciones.")


def main() -> None:
    """Punto de entrada principal para ejecutar el bot de Telegram."""
    logger.info("Iniciando mlunlop-assistant...")

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Registro de Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("compra", view_shopping_command))
    application.add_handler(CommandHandler("limpiar_compra", clear_shopping_command))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Ejecución por polling
    logger.info("Polling iniciado para ALLOWED_CHAT_ID: %s", ALLOWED_CHAT_ID)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

