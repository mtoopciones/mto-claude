"""
Automatización de onboarding de nuevos clientes Stripe.

Flujo:
  1. Webhook Stripe customer.created → servidor HTTP puerto 8080
  2. Envío INMEDIATO de email de bienvenida
  3. Cola persistente (JSON) → a las 72h:
       - Genera código embajador (6 chars email + 3 dígitos)
       - Crea cupón en Stripe (19,99 € / una vez)
       - Envía email Club Embajadores con PDF adjunto
  4. Log en Discord de cada paso (éxito / error)
  5. Limpieza mensual (día 1 de cada mes, 09:00 Madrid):
       - Borra cupones embajadores sin canje cuyo email no tiene suscripción activa
"""

import asyncio
import json
import os
import random
import re
import smtplib
import ssl
import string
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import stripe
from aiohttp import web
from loguru import logger

MADRID_ZONE = ZoneInfo("Europe/Madrid")


# ── Clase principal ────────────────────────────────────────────

class StripeOnboarding:
    def __init__(self, cfg: dict, log_channel=None):
        stripe_cfg    = cfg.get("stripe",       {})
        smtp_cfg      = cfg.get("smtp",          {})
        onboard_cfg   = cfg.get("onboarding",   {})

        self.stripe_api_key  = stripe_cfg.get("api_key",        "")
        self.webhook_secret  = stripe_cfg.get("webhook_secret", "")
        self.webhook_port    = int(stripe_cfg.get("webhook_port", 8080))

        self.smtp_host = smtp_cfg.get("host",     "mail.mtoopciones.com")
        self.smtp_port = int(smtp_cfg.get("port", 587))
        self.smtp_user = smtp_cfg.get("user",     "info@mtoopciones.com")
        self.smtp_pass = smtp_cfg.get("password", "")

        self.pending_file  = onboard_cfg.get("pending_file", "data/onboarding_pending.json")
        self.faqs_pdf_path = onboard_cfg.get("faqs_pdf",     "data/faqs_embajadores.pdf")

        self.log_channel = log_channel
        self._pending    = []
        self._load_pending()

        stripe.api_key = self.stripe_api_key

    # ── Persistencia cola ──────────────────────────────────────

    def _load_pending(self) -> None:
        try:
            if os.path.exists(self.pending_file):
                with open(self.pending_file, encoding="utf-8") as f:
                    self._pending = json.load(f)
                logger.info(f"Onboarding: {len(self._pending)} entradas en cola")
        except Exception as e:
            logger.error(f"Error cargando cola onboarding: {e}")
            self._pending = []

    def _save_pending(self) -> None:
        os.makedirs(os.path.dirname(self.pending_file), exist_ok=True)
        with open(self.pending_file, "w", encoding="utf-8") as f:
            json.dump(self._pending, f, indent=2, ensure_ascii=False, default=str)

    # ── Inicio ─────────────────────────────────────────────────

    async def start(self) -> None:
        asyncio.ensure_future(self._start_webhook_server())
        asyncio.ensure_future(self._pending_loop())
        asyncio.ensure_future(self._monthly_cleanup_loop())
        logger.info(f"Stripe onboarding iniciado — webhook en puerto {self.webhook_port}")

    async def _start_webhook_server(self) -> None:
        app = web.Application()
        app.router.add_post("/stripe/webhook", self._handle_webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.webhook_port)
        await site.start()
        logger.info(f"Webhook HTTP escuchando en 0.0.0.0:{self.webhook_port}/stripe/webhook")

    # ── Webhook handler ────────────────────────────────────────

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        payload    = await request.read()
        sig_header = request.headers.get("Stripe-Signature", "")

        try:
            if self.webhook_secret:
                event = stripe.Webhook.construct_event(
                    payload, sig_header, self.webhook_secret
                )
            else:
                event = json.loads(payload)
        except Exception as e:
            logger.error(f"Webhook error de firma: {e}")
            return web.Response(status=400, text=str(e))

        event_type = event.type if hasattr(event, "type") else event.get("type")
        customer_obj = event.data.object if hasattr(event, "data") else event["data"]["object"]
        if event_type == "customer.created":
            customer = customer_obj
            asyncio.ensure_future(self._handle_new_customer(customer))

        return web.Response(status=200, text="ok")

    async def _handle_new_customer(self, customer: dict) -> None:
        email       = (customer.get("email") or "").strip()
        name        = (customer.get("name")  or email.split("@")[0]).strip()
        customer_id = customer.get("id", "")

        if not email:
            logger.warning(f"Nuevo cliente Stripe sin email: {customer_id}")
            return

        logger.info(f"Nuevo cliente Stripe: {name} <{email}> ({customer_id})")

        # 1. Email bienvenida — inmediato
        await self._send_welcome_email(email, name)

        # 2. Encolar email embajadores para +3 días
        send_at_3d = (datetime.now(MADRID_ZONE) + timedelta(days=3)).isoformat()
        self._pending.append({
            "type":        "ambassador",
            "customer_id": customer_id,
            "email":       email,
            "name":        name,
            "send_at":     send_at_3d,
            "sent":        False,
        })

        # 3. Encolar email encuesta para +6 días
        send_at_6d = (datetime.now(MADRID_ZONE) + timedelta(days=6)).isoformat()
        self._pending.append({
            "type":        "survey",
            "customer_id": customer_id,
            "email":       email,
            "name":        name,
            "send_at":     send_at_6d,
            "sent":        False,
        })

        self._save_pending()
        logger.info(f"Email embajadores encolado para {send_at_3d[:16]} — {email}")
        logger.info(f"Email encuesta encolado para {send_at_6d[:16]} — {email}")

    # ── Loop de cola (cada hora) ───────────────────────────────

    async def _pending_loop(self) -> None:
        while True:
            try:
                now     = datetime.now(MADRID_ZONE)
                changed = False
                for entry in self._pending:
                    if entry.get("sent"):
                        continue
                    try:
                        send_at = datetime.fromisoformat(entry["send_at"])
                        # Asegurar que send_at tiene zona horaria
                        if send_at.tzinfo is None:
                            send_at = send_at.replace(tzinfo=MADRID_ZONE)
                    except Exception:
                        continue
                    if now >= send_at:
                        email_type = entry.get("type", "ambassador")
                        if email_type == "survey":
                            await self._send_survey_email(
                                entry["email"], entry["name"]
                            )
                        else:
                            await self._send_ambassador_email(
                                entry["email"], entry["name"], entry["customer_id"]
                            )
                        entry["sent"] = True
                        changed = True
                if changed:
                    self._save_pending()
            except Exception as e:
                logger.error(f"Error en pending loop onboarding: {e}")
            await asyncio.sleep(3600)  # revisar cada hora

    # ── Loop limpieza mensual ──────────────────────────────────

    async def _monthly_cleanup_loop(self) -> None:
        """Día 1 de cada mes a las 09:00 Madrid — limpia cupones embajadores sin uso."""
        while True:
            try:
                now      = datetime.now(MADRID_ZONE)
                # Calcular próximo día 1 del mes a las 09:00
                if now.day == 1 and now.hour < 9:
                    fire = now.replace(hour=9, minute=0, second=0, microsecond=0)
                else:
                    # Primer día del mes siguiente
                    if now.month == 12:
                        fire = now.replace(year=now.year + 1, month=1, day=1,
                                           hour=9, minute=0, second=0, microsecond=0)
                    else:
                        fire = now.replace(month=now.month + 1, day=1,
                                           hour=9, minute=0, second=0, microsecond=0)

                secs = (fire - now).total_seconds()
                logger.info(
                    f"Limpieza cupones embajadores: próxima ejecución "
                    f"{fire.strftime('%d/%m/%Y %H:%M')} Madrid "
                    f"({int(secs/3600)}h)"
                )
                await asyncio.sleep(secs)
                await self.run_ambassador_cleanup()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error en monthly cleanup loop: {e}")
                await asyncio.sleep(3600)

    async def run_ambassador_cleanup(self) -> None:
        """
        Recorre todos los cupones Stripe con metadata.email,
        borra los que cumplen ambas condiciones:
          - times_redeemed == 0  (nunca canjeado)
          - el email no tiene ninguna suscripción activa en Stripe
        Publica resumen en Discord.
        """
        logger.info("Iniciando limpieza mensual de cupones embajadores...")
        loop = asyncio.get_event_loop()

        try:
            # 1. Obtener todos los cupones con metadata.email (paginado)
            ambassador_coupons = await loop.run_in_executor(
                None, self._fetch_ambassador_coupons
            )
            logger.info(f"Cupones embajadores encontrados: {len(ambassador_coupons)}")

            deleted   = []
            kept      = []
            errors    = []

            for coupon in ambassador_coupons:
                meta  = coupon.metadata
                email = ""
                if meta:
                    try:
                        email = meta.get("email", "") if hasattr(meta, "get") else getattr(meta, "email", "")
                    except Exception:
                        email = ""
                code  = coupon.id
                redeemed     = coupon.times_redeemed or 0

                # Si ya fue canjeado → conservar siempre
                if redeemed > 0:
                    kept.append(f"`{code}` ({email}) — {redeemed} canje(s)")
                    continue

                # Comprobar si el email tiene suscripción activa
                try:
                    has_active = await loop.run_in_executor(
                        None, self._has_active_subscription, email
                    )
                except Exception as e:
                    errors.append(f"`{code}` ({email}) — error al verificar: {e}")
                    continue

                if has_active:
                    kept.append(f"`{code}` ({email}) — suscripción activa")
                else:
                    # Sin canje y sin suscripción activa → borrar
                    try:
                        await loop.run_in_executor(
                            None, lambda c=code: stripe.Coupon.delete(c)
                        )
                        deleted.append(f"`{code}` ({email})")
                        logger.info(f"Cupón eliminado: {code} ({email})")
                    except Exception as e:
                        errors.append(f"`{code}` ({email}) — error al borrar: {e}")

            # 2. Publicar resumen en Discord
            await self._log_cleanup_summary(
                total=len(ambassador_coupons),
                deleted=deleted,
                kept=kept,
                errors=errors,
            )

        except Exception as e:
            logger.error(f"Error en run_ambassador_cleanup: {e}")
            if self.log_channel:
                await self.log_channel.send_error(
                    f"❌ Error en limpieza mensual de cupones: {e}"
                )

    def _fetch_ambassador_coupons(self) -> list:
        """Obtiene todos los cupones que tienen metadata.email (paginado). Compatible Stripe v15."""
        result      = []
        last_id     = None

        while True:
            params: dict = {"limit": 100}
            if last_id:
                params["starting_after"] = last_id

            response = stripe.Coupon.list(**params)
            data     = list(response.data)

            for coupon in data:
                # En Stripe v15 metadata puede ser dict o StripeObject
                meta = coupon.metadata
                email = None
                if meta:
                    try:
                        email = meta.get("email") if hasattr(meta, "get") else meta["email"]
                    except Exception:
                        email = getattr(meta, "email", None)
                if email:
                    result.append(coupon)

            if not response.has_more or not data:
                break
            last_id = data[-1].id

        return result

    def _has_active_subscription(self, email: str) -> bool:
        """Devuelve True si el email tiene al menos una suscripción activa en Stripe."""
        customers = stripe.Customer.list(email=email, limit=5)
        for customer in list(customers.data):
            subs = stripe.Subscription.list(
                customer=customer.id,
                status="active",
                limit=1,
            )
            if list(subs.data):
                return True
        return False

    async def _log_cleanup_summary(
        self,
        total:   int,
        deleted: list,
        kept:    list,
        errors:  list,
    ) -> None:
        now_str = datetime.now(MADRID_ZONE).strftime("%d/%m/%Y %H:%M")

        if not self.log_channel:
            return

        if not deleted and not errors:
            await self.log_channel.send_info(
                f"🧹 **Limpieza cupones embajadores** — {now_str}\n"
                f"→ {total} cupones revisados · **0 eliminados** · todo en orden ✅"
            )
            return

        lines = [f"🧹 **Limpieza cupones embajadores** — {now_str}"]
        lines.append(f"→ {total} cupones revisados")

        if deleted:
            lines.append(f"\n🗑️ **Eliminados ({len(deleted)}):**")
            lines.extend(f"  • {d}" for d in deleted[:15])
            if len(deleted) > 15:
                lines.append(f"  … y {len(deleted) - 15} más")

        if errors:
            lines.append(f"\n⚠️ **Errores ({len(errors)}):**")
            lines.extend(f"  • {e}" for e in errors[:5])

        lines.append(f"\n✅ Conservados con suscripción activa o canjes: {len(kept)}")

        await self.log_channel.send_info("\n".join(lines))

    # ── Envío email bienvenida ─────────────────────────────────

    async def _send_welcome_email(self, email: str, name: str) -> None:
        subject = f"¡Bienvenido a MTO OPCIONES, {name}!"
        html    = _WELCOME_HTML.replace("{{NAME}}", name)

        ok, error = await asyncio.get_event_loop().run_in_executor(
            None, self._send_smtp, email, subject, html, None
        )

        if ok:
            logger.info(f"✅ Bienvenida enviada → {email}")
            if self.log_channel:
                await self.log_channel.send_info(
                    f"📧 Email bienvenida enviado ✅ → **{name}** ({email})"
                )
        else:
            logger.error(f"❌ Error bienvenida → {email}: {error}")
            if self.log_channel:
                await self.log_channel.send_error(
                    f"❌ Error enviando email bienvenida → {email}: {error}"
                )

    # ── Envío email Club Embajadores ───────────────────────────

    async def _send_ambassador_email(
        self, email: str, name: str, customer_id: str
    ) -> None:
        code      = _generate_ambassador_code(email)
        coupon_ok = await self._create_stripe_coupon(code, email)

        subject = f"¡Bienvenid@ al Club Embajadores MTO®! Tu código: {code}"
        html    = _AMBASSADOR_HTML.replace("{{NAME}}", name).replace("{{CODE}}", code)

        # Adjuntar PDF FAQs si existe
        pdf_bytes: Optional[bytes] = None
        if os.path.exists(self.faqs_pdf_path):
            with open(self.faqs_pdf_path, "rb") as f:
                pdf_bytes = f.read()
        else:
            logger.warning(f"PDF FAQs no encontrado: {self.faqs_pdf_path}")

        ok, error = await asyncio.get_event_loop().run_in_executor(
            None, self._send_smtp, email, subject, html, pdf_bytes
        )

        if ok:
            logger.info(f"✅ Club Embajadores enviado → {email} | código: {code}")
            if self.log_channel:
                await self.log_channel.send_info(
                    f"🎖️ Email Club Embajadores enviado ✅\n"
                    f"→ **{name}** ({email})\n"
                    f"→ Código: `{code}`\n"
                    f"→ Cupón Stripe: {'✅ creado' if coupon_ok else '⚠️ error al crear'}"
                )
        else:
            logger.error(f"❌ Error Club Embajadores → {email}: {error}")
            if self.log_channel:
                await self.log_channel.send_error(
                    f"❌ Error enviando Club Embajadores → {email}: {error}"
                )

    # ── Envío email encuesta (día 6) ───────────────────────────

    async def _send_survey_email(self, email: str, name: str) -> None:
        subject = "Queremos saber más de ti - ¿Cómo nos conociste?"
        html    = _SURVEY_HTML.replace("{{NAME}}", name)

        ok, error = await asyncio.get_event_loop().run_in_executor(
            None, self._send_smtp, email, subject, html, None
        )

        if ok:
            logger.info(f"✅ Encuesta enviada → {email}")
            if self.log_channel:
                await self.log_channel.send_info(
                    f"💬 Email encuesta (día 6) enviado ✅ → **{name}** ({email})"
                )
        else:
            logger.error(f"❌ Error encuesta → {email}: {error}")
            if self.log_channel:
                await self.log_channel.send_error(
                    f"❌ Error enviando email encuesta → {email}: {error}"
                )

    # ── SMTP ───────────────────────────────────────────────────

    def _send_smtp(
        self,
        to_email: str,
        subject:  str,
        html:     str,
        pdf_bytes: Optional[bytes],
    ) -> tuple:
        try:
            msg = MIMEMultipart("mixed")
            msg["From"]    = f"MTO OPCIONES <{self.smtp_user}>"
            msg["To"]      = to_email
            msg["CC"]      = self.smtp_user
            msg["Subject"] = subject
            msg["Reply-To"] = self.smtp_user

            msg.attach(MIMEText(html, "html", "utf-8"))

            if pdf_bytes:
                att = MIMEApplication(pdf_bytes, _subtype="pdf")
                att.add_header(
                    "Content-Disposition", "attachment",
                    filename="FAQs Club Embajadores MTO.pdf"
                )
                msg.attach(att)

            recipients = [to_email, self.smtp_user]

            ctx = ssl.create_default_context()
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=ctx)
                server.ehlo()
                server.login(self.smtp_user, self.smtp_pass)
                server.sendmail(self.smtp_user, recipients, msg.as_bytes())

            return True, None
        except Exception as e:
            return False, str(e)

    # ── Stripe API ─────────────────────────────────────────────

    async def _create_stripe_coupon(self, code: str, email: str) -> bool:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: stripe.Coupon.create(
                    id=code,
                    name=code,
                    amount_off=1999,      # 19,99 €
                    currency="eur",
                    duration="once",
                    metadata={"email": email},
                ),
            )
            logger.info(f"Cupón Stripe creado: {code} para {email}")
            return True
        except Exception as e:
            # Compatibilidad SDK v8+ (stripe.InvalidRequestError)
            # y versiones anteriores (stripe.error.InvalidRequestError)
            if "already exists" in str(e):
                logger.warning(f"Cupón {code} ya existe en Stripe — OK")
                return True
            logger.error(f"Error Stripe al crear cupón {code}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error Stripe al crear cupón {code}: {e}")
            return False


# ── Generación de código embajador ────────────────────────────

def _generate_ambassador_code(email: str) -> str:
    """
    Regla: primeros 6 chars alfanuméricos del prefijo del email (uppercase)
    + 3 dígitos aleatorios.
    Si el prefijo tiene menos de 6 chars alfanuméricos, rellena con letras aleatorias.
    Ejemplo: lmgiordano08@gmail.com → LMGIOR + 172 = LMGIOR172
    """
    prefix = email.split("@")[0]
    clean  = re.sub(r"[^a-zA-Z0-9]", "", prefix).upper()
    if len(clean) < 6:
        clean += "".join(random.choices(string.ascii_uppercase, k=6 - len(clean)))
    code_prefix = clean[:6]
    suffix      = "".join(random.choices(string.digits, k=3))
    return code_prefix + suffix


# ── HTML: Email bienvenida ────────────────────────────────────

_WELCOME_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bienvenido a MTO OPCIONES</title>
</head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:20px 0;">
  <tr><td align="center">
    <table width="620" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;">

      <!-- CABECERA -->
      <tr><td style="background:#1a1a2e;padding:30px 40px;text-align:center;">
        <img src="https://mtoopciones.com/wp-content/uploads/2025/10/WhatsApp-Image-2025-09-24-at-11.38.13-Photoroom-e1760168605872.png"
             width="140" alt="MTO Opciones" style="display:block;margin:0 auto 16px;">
        <h1 style="color:#ffffff;margin:0;font-size:28px;font-weight:bold;">
          ¡Bienvenido a MTO® Opciones!
        </h1>
      </td></tr>

      <!-- CUERPO -->
      <tr><td style="padding:40px;">
        <p style="font-size:16px;color:#333;margin:0 0 16px;">
          <strong>Querid@ {{NAME}},</strong>
        </p>
        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 16px;">
          ¡Te damos la más calurosa bienvenida a <strong>MTO® Opciones</strong>!
        </p>
        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 16px;">
          Desde ya mismo esta pasa a ser tu casa. Formas parte de una gran familia, que, sin duda,
          es una de las más completas comunidades sobre opciones financieras en habla hispana.
        </p>
        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 30px;">
          Has tomado la mejor decisión para impulsar tu patrimonio. Las opciones financieras son un
          maravilloso y versátil instrumento, y su conocimiento y experiencia en el correcto empleo
          de estas, te convertirá en un inversor más completo.
        </p>

        <!-- SECCIÓN DISCORD -->
        <h2 style="color:#1a1a2e;font-size:20px;border-bottom:2px solid #e0e0e0;padding-bottom:8px;">
          Cómo navegar en Discord
        </h2>
        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 24px;">
          Si no estás familiarizado con el uso de <strong>Discord</strong>, comentarte que se trata
          de una plataforma interactiva, organizada por canales que, a su vez, se combinan en distintos
          hilos donde se distribuyen las temáticas y contenidos puestos a tu disposición.
        </p>

        <h2 style="color:#1a1a2e;font-size:20px;border-bottom:2px solid #e0e0e0;padding-bottom:8px;">
          Estructura de MTO® Opciones
        </h2>
        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 16px;">
          En el caso de <strong>MTO® Opciones</strong>, encontrarás la siguiente estructura:
        </p>
        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 8px;">
          A través de <strong>#anuncios moderador</strong> os informamos de todas las novedades;
          y a través de <strong>#propuestas mejoras</strong> nos trasladáis vuestras recomendaciones.
        </p>

        <!-- CANALES -->
        <p style="font-size:15px;color:#2980b9;font-weight:bold;margin:20px 0 8px;">Canales de contenido:</p>

        <table width="100%" cellpadding="12" cellspacing="0" style="border-left:4px solid #2980b9;background:#f8f9fa;border-radius:4px;margin-bottom:12px;">
          <tr><td>
            <strong style="color:#1a1a2e;">1. Operaciones con Opciones</strong><br>
            <span style="font-size:14px;color:#555;">Publicamos en tiempo real las operaciones que abrimos,
            cerramos o gestionamos en MTO®. Un hilo por cada cuenta ($10K, $50K y $5K para ETFs de dividendos).
            También incluye <strong>#dudas operaciones</strong> para plantear tus preguntas.</span>
          </td></tr>
        </table>

        <table width="100%" cellpadding="12" cellspacing="0" style="border-left:4px solid #27ae60;background:#f8f9fa;border-radius:4px;margin-bottom:12px;">
          <tr><td>
            <strong style="color:#1a1a2e;">2. Log Book</strong><br>
            <span style="font-size:14px;color:#555;">Información actualizada de cada cuenta en IBKR:
            <strong>#excel de operaciones</strong>, <strong>#pérdidas y ganancias</strong>
            y balances en <strong>#evolución de las cuentas</strong>.</span>
          </td></tr>
        </table>

        <table width="100%" cellpadding="12" cellspacing="0" style="border-left:4px solid #8e44ad;background:#f8f9fa;border-radius:4px;margin-bottom:12px;">
          <tr><td>
            <strong style="color:#1a1a2e;">3. Formación</strong><br>
            <span style="font-size:14px;color:#555;">Videos exprés, recursos externos, newsletters,
            masterclass, sesiones en directo periódicas. Y espacios de interacción como
            <strong>#comparte tus operaciones</strong> y <strong>#faq</strong>.</span>
          </td></tr>
        </table>

        <table width="100%" cellpadding="12" cellspacing="0" style="border-left:4px solid #e74c3c;background:#f8f9fa;border-radius:4px;margin-bottom:24px;">
          <tr><td>
            <strong style="color:#1a1a2e;">4. Directos (y directos guardados 🎥)</strong><br>
            <span style="font-size:14px;color:#555;">Entrevistas con expertos del mundo de las opciones
            financieras y masterclass sobre temas concretos.</span>
          </td></tr>
        </table>

        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 16px;">
          Como hemos dicho, esperamos te sientas desde el primer minuto en tu casa. Para cualquier duda
          de gestión de tu membresía, no dudes en contactarnos en
          <a href="mailto:info@mtoopciones.com" style="color:#2980b9;">info@mtoopciones.com</a>.
        </p>
        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 30px;">
          Agradecerte todo lo que nos vas a aportar a partir de hoy. Si <strong>MTO® Opciones</strong>
          es una familia cada vez más especial, es gracias a que <strong>TÚ</strong> ahora estás en ella.
        </p>

        <!-- FIRMA -->
        <table width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #e0e0e0;padding-top:20px;">
          <tr><td align="center">
            <p style="margin:0;font-size:15px;font-weight:bold;color:#1a1a2e;">Un abrazo.</p>
            <p style="margin:4px 0;font-size:14px;color:#555;">Equipo administrador MTO Opciones</p>
            <a href="mailto:info@mtoopciones.com" style="color:#2980b9;font-size:14px;">info@mtoopciones.com</a><br>
            <a href="https://www.mtoopciones.com" style="color:#2980b9;font-size:14px;">www.mtoopciones.com</a>
          </td></tr>
        </table>
      </td></tr>

      <!-- FOOTER LEGAL -->
      <tr><td style="background:#f0f0f0;padding:24px 40px;">
        <p style="font-size:11px;color:#888;line-height:1.5;margin:0 0 10px;">
          <strong>AVISO LEGAL</strong><br>
          Este mensaje y sus archivos adjuntos van dirigidos exclusivamente a su destinatario, pudiendo
          contener información confidencial sometida a secreto profesional. No está permitida su
          comunicación, reproducción o distribución sin la autorización expresa de MTO OPCIONES, SL.
        </p>
        <p style="font-size:11px;color:#888;line-height:1.5;margin:0 0 10px;">
          <strong>PROTECCIÓN DE DATOS</strong><br>
          De conformidad con lo dispuesto en el Reglamento (UE) 2016/679 (GDPR), los datos personales
          serán tratados bajo la responsabilidad de MTO OPCIONES, SL para el envío de comunicaciones
          sobre nuestros productos y servicios. Puede ejercer sus derechos dirigiéndose a
          C. San Lorenzo, núm. 14, 1º - 07840 SANTA EULALIA DEL RIO (ISLAS BALEARES) o a
          <a href="mailto:info@mtoopciones.com" style="color:#888;">info@mtoopciones.com</a>.
        </p>
        <p style="font-size:11px;color:#888;line-height:1.5;margin:0;">
          <strong>PUBLICIDAD</strong><br>
          En cumplimiento del art. 21 de la LSSICE, si no desea recibir más comunicaciones puede
          darse de baja enviando un correo a
          <a href="mailto:info@mtoopciones.com" style="color:#888;">info@mtoopciones.com</a>
          con el asunto "BAJA".
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


# ── HTML: Email Club Embajadores ──────────────────────────────

_AMBASSADOR_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Club Embajadores MTO®</title>
</head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:20px 0;">
  <tr><td align="center">
    <table width="620" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;">

      <!-- CABECERA -->
      <tr><td style="background:#1a1a2e;padding:30px 40px;text-align:center;">
        <img src="https://mtoopciones.com/wp-content/uploads/2025/10/WhatsApp-Image-2025-09-24-at-11.38.13-Photoroom-e1760168605872.png"
             width="140" alt="MTO Opciones" style="display:block;margin:0 auto 16px;">
        <p style="color:#f39c12;font-size:13px;font-weight:bold;letter-spacing:2px;margin:0 0 8px;">
          CLUB EMBAJADORES
        </p>
        <h1 style="color:#ffffff;margin:0;font-size:26px;font-weight:bold;">
          MTO® Opciones
        </h1>
      </td></tr>

      <!-- CUERPO -->
      <tr><td style="padding:40px;">
        <p style="font-size:16px;color:#333;margin:0 0 16px;">
          Hola <strong style="color:#2980b9;">{{NAME}}</strong>,
        </p>
        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 16px;">
          Te agradecemos profundamente la confianza depositada en <strong>MTO Opciones</strong>
          y tu participación en nuestra comunidad.
        </p>
        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 30px;">
          Te damos la más cordial bienvenida a formar parte oficial del
          <strong style="color:#2980b9;">Club de Embajadores MTO®</strong>.
        </p>

        <!-- CÓDIGO -->
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border:2px solid #2980b9;border-radius:8px;margin-bottom:30px;">
          <tr><td style="padding:24px;text-align:center;">
            <p style="font-size:11px;font-weight:bold;letter-spacing:3px;color:#2980b9;margin:0 0 12px;">
              TU CÓDIGO PERSONAL DE EMBAJADOR
            </p>
            <p style="font-size:36px;font-weight:bold;letter-spacing:6px;color:#1a1a2e;
                      font-family:'Courier New',monospace;margin:0 0 12px;">
              {{CODE}}
            </p>
            <p style="font-size:13px;color:#777;font-style:italic;margin:0;">
              Guárdalo en un lugar seguro, ¡es tu acceso exclusivo!
            </p>
          </td></tr>
        </table>

        <!-- QUÉ PUEDES HACER -->
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#fffbf0;border-left:4px solid #f39c12;
                      border-radius:4px;margin-bottom:30px;">
          <tr><td style="padding:20px 24px;">
            <p style="font-size:14px;font-weight:bold;color:#f39c12;margin:0 0 14px;">
              ✨ ¿Qué puedes hacer con tu código?
            </p>
            <p style="font-size:14px;color:#444;margin:0 0 8px;">
              ✓ <strong>Obtener descuento</strong> directo en tu cuota mensual/anual
            </p>
            <p style="font-size:14px;color:#444;margin:0 0 8px;">
              ✓ <strong>Ganar descuentos</strong> por cada nuevo miembro que use tu código
            </p>
            <p style="font-size:14px;color:#444;margin:0;">
              ✓ <strong>Conseguir hasta un año gratis</strong> en tu suscripción
            </p>
          </td></tr>
        </table>

        <!-- COMUNIDAD -->
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#2980b9;border-radius:8px;margin-bottom:30px;">
          <tr><td style="padding:24px;text-align:center;">
            <p style="font-size:15px;font-weight:bold;color:#ffffff;margin:0 0 8px;">
              🌐 Únete a Nuestra Comunidad
            </p>
            <p style="font-size:14px;color:#d6eaf8;margin:0 0 8px;">
              Toda la información sobre las bases del Club está en:
            </p>
            <p style="font-size:15px;font-weight:bold;color:#ffffff;margin:0 0 4px;">
              #Club de Embajadores MTO
            </p>
            <p style="font-size:13px;color:#d6eaf8;margin:0;">
              En nuestro servidor Discord
            </p>
          </td></tr>
        </table>

        <p style="font-size:14px;color:#777;text-align:center;margin:0 0 30px;">
          ¿Tienes dudas o preguntas?<br>
          <a href="mailto:info@mtoopciones.com"
             style="color:#2980b9;font-weight:bold;">📧 info@mtoopciones.com</a>
        </p>

        <!-- CIERRE -->
        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 20px;">
          Agradecerte de nuevo tu compañía. La nuestra es cada vez una de las mayores
          comunidades de habla hispana sobre opciones financieras, y eso es
          <strong>gracias a ti</strong>. 🧡
        </p>

        <!-- FIRMA -->
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-top:1px solid #e0e0e0;padding-top:20px;">
          <tr><td align="center">
            <p style="margin:0;font-size:15px;font-weight:bold;color:#1a1a2e;">MTO Opciones</p>
            <p style="margin:4px 0;font-size:14px;color:#555;">Equipo Administrativo</p>
            <a href="https://www.mtoopciones.com"
               style="color:#2980b9;font-size:14px;">www.mtoopciones.com</a>
          </td></tr>
        </table>
      </td></tr>

      <!-- FOOTER LEGAL -->
      <tr><td style="background:#f0f0f0;padding:24px 40px;">
        <p style="font-size:11px;color:#888;line-height:1.5;margin:0 0 10px;">
          <strong>AVISO LEGAL</strong><br>
          Este mensaje va dirigido exclusivamente a su destinatario, pudiendo contener información
          confidencial. No está permitida su comunicación sin autorización expresa de MTO OPCIONES, SL.
        </p>
        <p style="font-size:11px;color:#888;line-height:1.5;margin:0;">
          <strong>PROTECCIÓN DE DATOS (GDPR)</strong><br>
          Sus datos serán tratados bajo la responsabilidad de MTO OPCIONES, SL. Puede ejercer sus
          derechos en <a href="mailto:info@mtoopciones.com" style="color:#888;">info@mtoopciones.com</a>.
          Para darse de baja envíe un correo con asunto "BAJA".
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


# ── HTML: Email encuesta día 6 ────────────────────────────────

_SURVEY_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>¿Cómo nos conociste?</title>
</head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:20px 0;">
  <tr><td align="center">
    <table width="620" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;">

      <!-- CABECERA -->
      <tr><td style="background:#1a1a2e;padding:30px 40px;text-align:center;">
        <img src="https://mtoopciones.com/wp-content/uploads/2025/10/WhatsApp-Image-2025-09-24-at-11.38.13-Photoroom-e1760168605872.png"
             width="140" alt="MTO Opciones" style="display:block;margin:0 auto 16px;">
        <h1 style="color:#ffffff;margin:0;font-size:26px;font-weight:bold;">
          ¿Cómo nos conociste?
        </h1>
      </td></tr>

      <!-- CUERPO -->
      <tr><td style="padding:40px;">
        <p style="font-size:16px;color:#333;margin:0 0 20px;">
          <strong>¡Hola {{NAME}}! 👋</strong>
        </p>
        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 16px;">
          Hace unos días que te uniste a nuestra comunidad y queríamos darte la
          <strong>bienvenida oficial</strong>. ¡Nos alegra muchísimo tenerte por aquí! 😊
        </p>
        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 16px;">
          Nos encantaría saber: <strong>¿Cómo nos conociste?</strong> 🤔
        </p>
        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 30px;">
          ¿Fue por redes sociales, algún amigo que te recomendó, un artículo que leíste,
          o fue pura casualidad? <strong>Tu historia nos importa.</strong>
        </p>

        <!-- BLOQUE DESTACADO -->
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#f0f7ff;border-left:4px solid #2980b9;border-radius:4px;margin-bottom:30px;">
          <tr><td style="padding:20px 24px;">
            <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 12px;">
              Si tienes cualquier duda o quieres comentar algo, este es tu espacio.
              ¡Disfruta y participa cuando quieras! 🚀
            </p>
            <p style="font-size:15px;color:#1a1a2e;font-weight:bold;margin:0;">
              Cuéntanos tu historia →
            </p>
          </td></tr>
        </table>

        <!-- BOTÓN RESPONDER -->
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:30px;">
          <tr><td align="center">
            <a href="mailto:info@mtoopciones.com?subject=Así%20os%20conocí"
               style="display:inline-block;background:#2980b9;color:#ffffff;
                      font-size:15px;font-weight:bold;padding:14px 36px;
                      border-radius:6px;text-decoration:none;">
              ✉️ Responder ahora
            </a>
          </td></tr>
        </table>

        <p style="font-size:15px;color:#444;line-height:1.6;margin:0 0 30px;text-align:center;">
          Esperamos tu respuesta con entusiasmo.<br>
          <strong>¡La comunidad de MTO OPCIONES crece gracias a gente como tú! 💪</strong>
        </p>

        <!-- FIRMA -->
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-top:1px solid #e0e0e0;padding-top:20px;">
          <tr><td align="center">
            <p style="margin:0;font-size:15px;font-weight:bold;color:#1a1a2e;">Equipo MTO OPCIONES</p>
            <p style="margin:4px 0 8px;font-size:13px;color:#777;font-style:italic;">
              Tu comunidad para dominar el mundo de las opciones financieras 📈
            </p>
            <a href="mailto:info@mtoopciones.com" style="color:#2980b9;font-size:14px;">info@mtoopciones.com</a><br>
            <a href="https://www.mtoopciones.com" style="color:#2980b9;font-size:14px;">www.mtoopciones.com</a>
          </td></tr>
        </table>
      </td></tr>

      <!-- FOOTER LEGAL -->
      <tr><td style="background:#f0f0f0;padding:24px 40px;">
        <p style="font-size:11px;color:#888;line-height:1.5;margin:0 0 10px;">
          <strong>AVISO LEGAL</strong><br>
          Este mensaje va dirigido exclusivamente a su destinatario, pudiendo contener información
          confidencial. No está permitida su comunicación sin autorización expresa de MTO OPCIONES, SL.
        </p>
        <p style="font-size:11px;color:#888;line-height:1.5;margin:0;">
          <strong>PROTECCIÓN DE DATOS (GDPR)</strong><br>
          Sus datos serán tratados bajo la responsabilidad de MTO OPCIONES, SL. Puede ejercer sus
          derechos en <a href="mailto:info@mtoopciones.com" style="color:#888;">info@mtoopciones.com</a>.
          Para darse de baja envíe un correo con asunto "BAJA".
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""
