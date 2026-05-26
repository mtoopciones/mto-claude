"""
Health Reporter — Estatus diario del bot MTO.

Envía cada día a las 09:00 hora Madrid un email con checklist completo
del estado de todos los sistemas a los administradores configurados.
"""

import asyncio
import smtplib
import socket
import ssl
import subprocess
from datetime import datetime, timedelta, time as dtime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import json

import aiohttp
import stripe
from loguru import logger

MADRID_ZONE  = ZoneInfo("Europe/Madrid")
REPORT_TIME  = dtime(9, 0)   # 09:00 Madrid

RECIPIENTS = [
    "tfaura@tax-advice.es",
    "mario.vph@gmail.com",
]

# ── Resultado de un check individual ──────────────────────────

class Check:
    def __init__(self, label: str, ok: bool, detail: str = ""):
        self.label  = label
        self.ok     = ok
        self.detail = detail

    def __repr__(self):
        icon = "OK" if self.ok else "KO"
        return f"[{icon}] {self.label}: {self.detail}"


# ── Clase principal ────────────────────────────────────────────

class HealthReporter:

    def __init__(self, cfg: dict, get_ib=None, get_positions=None):
        """
        cfg           — config.yaml completo
        get_ib        — callable que devuelve el objeto IB activo (o None)
        get_positions — callable que devuelve el nº de posiciones activas
        """
        self._cfg          = cfg
        self._get_ib       = get_ib        or (lambda: None)
        self._get_positions = get_positions or (lambda: 0)

        # Usa Gmail SMTP para relay externo si está configurado;
        # si no, cae sobre el SMTP de Webempresa
        gmail = cfg.get("gmail_smtp", {})
        smtp  = cfg.get("smtp", {})
        if gmail.get("user") and gmail.get("password"):
            self._smtp_host = gmail.get("host", "smtp.gmail.com")
            self._smtp_port = int(gmail.get("port", 587))
            self._smtp_user = gmail.get("user", "")
            self._smtp_pass = gmail.get("password", "").replace(" ", "")
        else:
            self._smtp_host = smtp.get("host", "mail.mtoopciones.com")
            self._smtp_port = int(smtp.get("port", 587))
            self._smtp_user = smtp.get("user", "info@mtoopciones.com")
            self._smtp_pass = smtp.get("password", "")

        self._log_file  = cfg.get("logging", {}).get("file", "logs/mto.log")

    # ── Scheduler ─────────────────────────────────────────────

    async def start(self) -> None:
        asyncio.ensure_future(self._loop())
        logger.info("Health reporter iniciado (email diario 09:00 Madrid)")

    async def _loop(self) -> None:
        while True:
            try:
                now    = datetime.now(MADRID_ZONE)
                fire   = datetime.combine(now.date(), REPORT_TIME, tzinfo=MADRID_ZONE)
                if now >= fire:
                    fire += timedelta(days=1)
                secs = (fire - now).total_seconds()
                logger.info(
                    f"Health report: próximo envío el "
                    f"{fire.strftime('%d/%m/%Y %H:%M')} Madrid"
                )
                await asyncio.sleep(secs)
                await self.run_and_send()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error en health reporter loop: {e}")
                await asyncio.sleep(3600)

    # ── Punto de entrada para ejecutar manualmente ─────────────

    async def run_and_send(self) -> None:
        """Ejecuta todos los checks y envía el email."""
        logger.info("Health reporter: ejecutando checks...")
        checks   = await self._run_all_checks()
        ok_count = sum(1 for c in checks if c.ok)
        total    = len(checks)
        html     = self._build_html(checks, ok_count, total)
        subject  = self._build_subject(ok_count, total)
        self._send_email(subject, html)
        logger.info(f"Health report enviado: {ok_count}/{total} checks OK")

    # ── Checks ─────────────────────────────────────────────────

    async def _run_all_checks(self) -> list:
        checks = []

        # ── 1. Servicios del sistema ────────────────────────────
        checks.append(self._check_service("mto-ib-discord", "Bot MTO (servicio)"))
        checks.append(self._check_docker_container("ib-docker-ib-gateway-1", "IB Gateway (servicio IBC)"))

        # ── 2. IB Gateway conectividad ──────────────────────────
        ib_cfg = self._cfg.get("ib", {})
        checks.append(self._check_port(
            ib_cfg.get("host", "127.0.0.1"),
            int(ib_cfg.get("port", 4001)),
            "Puerto IB Gateway 4001"
        ))
        ib = self._get_ib()
        checks.append(Check(
            "Conexión IB activa",
            ok     = bool(ib and ib.isConnected()),
            detail = "conectado" if (ib and ib.isConnected()) else "desconectado"
        ))
        positions = self._get_positions()
        checks.append(Check(
            "Posiciones cargadas en IB",
            ok     = positions > 0,
            detail = f"{positions} posiciones activas"
        ))

        # ── 3. Discord webhooks ─────────────────────────────────
        webhooks = self._collect_webhooks()
        wh_results = await self._check_all_webhooks(webhooks)
        checks.extend(wh_results)

        # ── 4. Informes diarios (log check) ─────────────────────
        checks.append(self._check_log_contains(
            "Reporte pre-mercado enviado",
            "Informe pre-mercado (ayer)",
            hours=28,
        ))
        checks.append(self._check_log_contains(
            "Reporte post-mercado enviado",
            "Informe post-mercado (ayer)",
            hours=28,
        ))

        # ── 5. Stripe ───────────────────────────────────────────
        checks.append(await self._check_stripe())
        checks.append(await self._check_stripe_recent_coupons(days=7))

        # ── 6. Email SMTP ───────────────────────────────────────
        checks.append(self._check_smtp())

        # ── 7. Redes sociales ───────────────────────────────────
        checks.append(await self._check_facebook())
        checks.append(await self._check_instagram())
        checks.append(self._check_twitter())

        # ── 8. Logbook Excel (Dropbox) ──────────────────────────
        checks.append(await self._check_dropbox())
        checks.append(self._check_logbook_index())

        # ── 9. Salud general ────────────────────────────────────
        checks.append(self._check_log_errors(hours=24))
        checks.append(self._check_disk_space())

        return checks

    # ── Checks individuales ────────────────────────────────────

    def _check_service(self, service: str, label: str) -> Check:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", service],
                capture_output=True, text=True, timeout=5
            )
            active = result.stdout.strip() == "active"
            return Check(label, active, result.stdout.strip())
        except Exception as e:
            return Check(label, False, str(e))

    def _check_docker_container(self, container: str, label: str) -> Check:
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container],
                capture_output=True, text=True, timeout=5
            )
            running = result.stdout.strip() == "true"
            detail  = "active" if running else "inactive"
            return Check(label, running, detail)
        except Exception as e:
            return Check(label, False, str(e))

    def _check_port(self, host: str, port: int, label: str) -> Check:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            result = sock.connect_ex((host, port))
            sock.close()
            ok = result == 0
            return Check(label, ok, "abierto" if ok else f"cerrado (err {result})")
        except Exception as e:
            return Check(label, False, str(e))

    def _collect_webhooks(self) -> dict:
        """Recoge todos los webhooks del config en un dict {label: url}."""
        wh = {}
        disc = self._cfg.get("discord", {})
        for key, label in [
            ("log_webhook",          "Discord · Canal Log"),
            ("daily_report_webhook", "Discord · Reportes diarios"),
            ("premarket_webhook",    "Discord · Pre-apertura"),
            ("weekly_report_webhook","Discord · Semanal P&L"),
            ("portfolio_webhook",    "Discord · Evolución cartera"),
            ("logbook_export_webhook","Discord · Excel operaciones"),
        ]:
            url = disc.get(key, "")
            if url:
                wh[label] = url
        for acc in self._cfg.get("accounts", []):
            url = acc.get("discord_webhook", "")
            if url:
                wh[f"Discord · Operaciones {acc['name']}"] = url
        return wh

    async def _check_all_webhooks(self, webhooks: dict) -> list:
        results = []
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for label, url in webhooks.items():
                try:
                    async with session.post(url, json={}) as r:
                        # 400 = webhook existe, payload vacío (OK)
                        # 404 = webhook borrado (KO)
                        ok = r.status in (200, 204, 400)
                        detail = "activo" if ok else f"HTTP {r.status}"
                        results.append(Check(label, ok, detail))
                except Exception as e:
                    results.append(Check(label, False, str(e)[:60]))
        return results

    def _check_log_contains(self, text: str, label: str, hours: int = 26) -> Check:
        try:
            log_path = Path(self._log_file)
            if not log_path.exists():
                return Check(label, False, "log no encontrado")
            cutoff = datetime.now() - timedelta(hours=hours)
            with open(log_path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if text in line:
                        # Intentar parsear la fecha de la línea de log
                        try:
                            ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                            if ts >= cutoff:
                                return Check(label, True, ts.strftime("último: %d/%m %H:%M"))
                        except Exception:
                            pass
            return Check(label, False, f"no encontrado en últimas {hours}h")
        except Exception as e:
            return Check(label, False, str(e)[:60])

    async def _check_stripe(self) -> Check:
        stripe_cfg = self._cfg.get("stripe", {})
        api_key = stripe_cfg.get("api_key", "")
        if not api_key:
            return Check("Stripe API", False, "api_key no configurada")
        try:
            def _test():
                stripe.api_key = api_key
                stripe.Balance.retrieve()
            await asyncio.to_thread(_test)
            return Check("Stripe API", True, "conectada")
        except Exception as e:
            return Check("Stripe API", False, str(e)[:80])

    async def _check_stripe_recent_coupons(self, days: int = 7) -> Check:
        stripe_cfg = self._cfg.get("stripe", {})
        api_key = stripe_cfg.get("api_key", "")
        if not api_key:
            return Check("Stripe · Cupones recientes", False, "no configurado")
        try:
            cutoff_ts = int((datetime.now() - timedelta(days=days)).timestamp())
            def _fetch():
                stripe.api_key = api_key
                coupons = stripe.Coupon.list(limit=20)
                return [c for c in coupons.data if c.created >= cutoff_ts]
            recent = await asyncio.to_thread(_fetch)
            return Check(
                f"Stripe · Cupones (últimos {days}d)",
                True,
                f"{len(recent)} creado(s)" if recent else "ninguno (normal si no hubo altas)"
            )
        except Exception as e:
            return Check(f"Stripe · Cupones (últimos {days}d)", False, str(e)[:80])

    def _check_smtp(self) -> Check:
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=10) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(self._smtp_user, self._smtp_pass)
            return Check("Email SMTP", True, f"{self._smtp_host}:{self._smtp_port} OK")
        except Exception as e:
            return Check("Email SMTP", False, str(e)[:80])

    async def _check_facebook(self) -> Check:
        fb_cfg = self._cfg.get("facebook", {})
        if not fb_cfg.get("enabled"):
            return Check("Facebook", True, "desactivado (OK)")
        token   = fb_cfg.get("page_access_token", "")
        page_id = fb_cfg.get("page_id", "")
        if not token or not page_id:
            return Check("Facebook · Token", False, "token o page_id vacío")
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(
                    f"https://graph.facebook.com/v21.0/{page_id}",
                    params={"fields": "id,name", "access_token": token}
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return Check("Facebook · Token", True, f"página '{data.get('name')}' OK")
                    else:
                        err = await r.text()
                        return Check("Facebook · Token", False, f"HTTP {r.status}: {err[:80]}")
        except Exception as e:
            return Check("Facebook · Token", False, str(e)[:80])

    async def _check_instagram(self) -> Check:
        ig_cfg = self._cfg.get("instagram", {})
        if not ig_cfg.get("enabled"):
            return Check("Instagram", True, "desactivado (OK)")
        token = ig_cfg.get("access_token", "")
        if not token:
            return Check("Instagram · Token", False, "token vacío")
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(
                    "https://graph.instagram.com/me",
                    params={"fields": "id,username", "access_token": token}
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return Check("Instagram · Token", True, f"@{data.get('username','?')} OK")
                    else:
                        err = await r.text()
                        return Check("Instagram · Token", False, f"HTTP {r.status}: {err[:80]}")
        except Exception as e:
            return Check("Instagram · Token", False, str(e)[:80])

    def _check_twitter(self) -> Check:
        tw_cfg = self._cfg.get("twitter", {})
        if not tw_cfg.get("enabled"):
            return Check("Twitter/X", True, "desactivado (OK)")
        required = ["api_key", "api_secret", "access_token", "access_token_secret"]
        missing  = [k for k in required if not tw_cfg.get(k)]
        if missing:
            return Check("Twitter/X · Credenciales", False, f"faltan: {missing}")
        return Check("Twitter/X · Credenciales", True, "configuradas")

    async def _check_dropbox(self) -> Check:
        """Verifica conectividad con Dropbox renovando el access token."""
        lb_cfg = self._cfg.get("logbook", {})
        if not lb_cfg.get("enabled", False):
            return Check("Dropbox · Conexión", True, "logbook desactivado (OK)")
        app_key    = lb_cfg.get("dropbox_app_key", "")
        app_secret = lb_cfg.get("dropbox_app_secret", "")
        refresh    = lb_cfg.get("dropbox_refresh_token", "")
        if not (app_key and app_secret and refresh):
            return Check("Dropbox · Conexión", False, "credenciales no configuradas")
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(
                    "https://api.dropboxapi.com/oauth2/token",
                    data={
                        "grant_type":    "refresh_token",
                        "refresh_token": refresh,
                        "client_id":     app_key,
                        "client_secret": app_secret,
                    }
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        token = data.get("access_token", "")
                        if token:
                            return Check("Dropbox · Conexión", True, "token renovado OK")
                        return Check("Dropbox · Conexión", False, "token vacío en respuesta")
                    err = await resp.text()
                    return Check("Dropbox · Conexión", False, f"HTTP {resp.status}: {err[:60]}")
        except Exception as e:
            return Check("Dropbox · Conexión", False, str(e)[:80])

    def _check_logbook_index(self) -> Check:
        """
        Verifica que el índice local del logbook existe, tiene entradas y
        muestra cuándo fue modificado por última vez.
        """
        lb_cfg   = self._cfg.get("logbook", {})
        if not lb_cfg.get("enabled", False):
            return Check("Logbook · Índice Excel", True, "logbook desactivado (OK)")
        idx_file = lb_cfg.get("index_file", "data/logbook_index.json")
        try:
            p = Path(idx_file)
            if not p.exists():
                return Check("Logbook · Índice Excel", False, "archivo índice no encontrado")
            with open(p, encoding="utf-8") as f:
                idx = json.load(f)
            entries = len(idx)
            mtime   = datetime.fromtimestamp(p.stat().st_mtime)
            age_h   = (datetime.now() - mtime).total_seconds() / 3600
            if age_h < 1:
                age_str = f"modificado hace {int(age_h * 60)} min"
            elif age_h < 48:
                age_str = f"modificado hace {age_h:.1f}h"
            else:
                age_str = f"último cambio: {mtime.strftime('%d/%m %H:%M')}"
            # Considerar OK siempre que el archivo exista (puede haber 0 abiertas)
            detail  = f"{entries} posición(es) abierta(s) · {age_str}"
            return Check("Logbook · Índice Excel", True, detail)
        except Exception as e:
            return Check("Logbook · Índice Excel", False, str(e)[:80])

    def _check_log_errors(self, hours: int = 24) -> Check:
        try:
            log_path = Path(self._log_file)
            if not log_path.exists():
                return Check(f"Errores críticos (últimas {hours}h)", True, "sin log (OK)")
            cutoff  = datetime.now() - timedelta(hours=hours)
            errors  = []
            with open(log_path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if " | ERROR    |" in line or " | CRITICAL |" in line:
                        try:
                            ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                            if ts >= cutoff:
                                errors.append(line[22:].strip()[:80])
                        except Exception:
                            pass
            if not errors:
                return Check(f"Errores en log (últimas {hours}h)", True, "sin errores")
            return Check(
                f"Errores en log (últimas {hours}h)",
                False,
                f"{len(errors)} errores · último: {errors[-1][:60]}"
            )
        except Exception as e:
            return Check(f"Errores en log (últimas {hours}h)", False, str(e)[:60])

    def _check_disk_space(self) -> Check:
        try:
            import shutil
            total, used, free = shutil.disk_usage("/")
            free_gb  = free  / (1024 ** 3)
            total_gb = total / (1024 ** 3)
            pct_used = used / total * 100
            ok = free_gb > 1.0  # al menos 1 GB libre
            return Check(
                "Espacio en disco",
                ok,
                f"{free_gb:.1f} GB libres de {total_gb:.0f} GB ({pct_used:.0f}% usado)"
            )
        except Exception as e:
            return Check("Espacio en disco", False, str(e)[:60])

    # ── Construcción del email ─────────────────────────────────

    def _build_subject(self, ok: int, total: int) -> str:
        emoji = "✅" if ok == total else ("⚠️" if ok >= total * 0.8 else "🔴")
        return f"{emoji} Estatus bot MTO — {ok}/{total} sistemas OK — {datetime.now(MADRID_ZONE).strftime('%d/%m/%Y %H:%M')}"

    def _build_html(self, checks: list, ok_count: int, total: int) -> str:
        now_str   = datetime.now(MADRID_ZONE).strftime("%d/%m/%Y a las %H:%M")
        all_ok    = ok_count == total
        bar_color = "#27ae60" if all_ok else ("#e67e22" if ok_count >= total * 0.8 else "#e74c3c")
        pct       = int(ok_count / total * 100) if total else 0

        rows = ""
        prev_group = None
        for c in checks:
            # Detectar grupo por prefijo del label
            group = c.label.split("·")[0].split("(")[0].strip()
            if group != prev_group:
                rows += f"""
                <tr>
                  <td colspan="3"
                      style="padding:12px 20px 4px;font-size:11px;font-weight:bold;
                             letter-spacing:1.5px;color:#888;background:#fafafa;
                             text-transform:uppercase;border-top:1px solid #eee;">
                    {group}
                  </td>
                </tr>"""
                prev_group = group

            icon  = "✅" if c.ok else "❌"
            color = "#27ae60" if c.ok else "#e74c3c"
            bg    = "#ffffff" if c.ok else "#fff5f5"
            rows += f"""
                <tr style="background:{bg};">
                  <td style="padding:10px 20px;font-size:14px;width:28px;">{icon}</td>
                  <td style="padding:10px 8px;font-size:14px;color:#222;font-weight:500;">
                    {c.label}
                  </td>
                  <td style="padding:10px 20px;font-size:13px;color:{color};text-align:right;">
                    {c.detail or ("OK" if c.ok else "KO")}
                  </td>
                </tr>"""

        status_text  = "TODOS LOS SISTEMAS OPERATIVOS" if all_ok else f"{total - ok_count} SISTEMAS REQUIEREN ATENCIÓN"
        header_color = "#1a1a2e" if all_ok else ("#c0392b" if ok_count < total * 0.8 else "#d35400")

        return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Estatus Bot MTO</title>
</head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;padding:24px 0;">
  <tr><td align="center">
    <table width="640" cellpadding="0" cellspacing="0"
           style="background:#fff;border-radius:10px;overflow:hidden;
                  box-shadow:0 2px 8px rgba(0,0,0,0.08);">

      <!-- CABECERA -->
      <tr><td style="background:{header_color};padding:28px 32px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td>
              <p style="margin:0;font-size:11px;letter-spacing:2px;
                        color:rgba(255,255,255,0.7);text-transform:uppercase;">
                MTO OPCIONES · BOT DE TRADING
              </p>
              <h1 style="margin:6px 0 0;font-size:22px;color:#fff;">
                Estatus Bot MTO
              </h1>
              <p style="margin:4px 0 0;font-size:13px;color:rgba(255,255,255,0.8);">
                Informe generado el {now_str} (Madrid)
              </p>
            </td>
            <td align="right" style="vertical-align:top;">
              <p style="margin:0;font-size:36px;font-weight:bold;color:#fff;">
                {ok_count}/{total}
              </p>
              <p style="margin:2px 0 0;font-size:11px;color:rgba(255,255,255,0.7);">
                sistemas OK
              </p>
            </td>
          </tr>
        </table>
      </td></tr>

      <!-- BARRA DE PROGRESO -->
      <tr><td style="padding:0;">
        <div style="background:#ddd;height:6px;">
          <div style="background:{bar_color};width:{pct}%;height:6px;"></div>
        </div>
      </td></tr>

      <!-- RESUMEN -->
      <tr><td style="padding:16px 32px;background:#f8f9fa;
                     border-bottom:1px solid #e8e8e8;">
        <p style="margin:0;font-size:13px;font-weight:bold;color:{bar_color};">
          {status_text}
        </p>
      </td></tr>

      <!-- CHECKLIST -->
      <tr><td style="padding:0;">
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;">
          {rows}
        </table>
      </td></tr>

      <!-- FOOTER -->
      <tr><td style="background:#f8f9fa;padding:20px 32px;
                     border-top:2px solid #e8e8e8;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="font-size:12px;color:#aaa;">
              MTO OPCIONES, SL &nbsp;·&nbsp;
              <a href="https://mtoopciones.com" style="color:#aaa;">mtoopciones.com</a>
            </td>
            <td align="right" style="font-size:12px;color:#aaa;">
              Servidor: 91.98.161.191
            </td>
          </tr>
        </table>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""

    # ── Envío SMTP ─────────────────────────────────────────────

    def _send_email(self, subject: str, html: str) -> None:
        try:
            msg = MIMEMultipart("mixed")
            msg["From"]    = f"MTO Bot <{self._smtp_user}>"
            msg["To"]      = ", ".join(RECIPIENTS)
            msg["Subject"] = subject
            msg.attach(MIMEText(html, "html", "utf-8"))

            ctx = ssl.create_default_context()
            with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(self._smtp_user, self._smtp_pass)
                s.sendmail(self._smtp_user, RECIPIENTS, msg.as_bytes())

            logger.info(f"Health report email enviado a {RECIPIENTS}")
        except Exception as e:
            logger.error(f"Error enviando health report email: {e}")
