"""
Mensajes al canal de log de Discord (canal 4).
Incluye: heartbeat periódico enriquecido, errores, reconexiones, confirmaciones de trades.

El heartbeat cada hora muestra un embed con:
  - Estado de cada sistema (IB, Bot, redes sociales, reportes…)
  - Emails en cola (embajador, encuesta, bienvenida)
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo
from loguru import logger

_TZ = ZoneInfo("Europe/Madrid")

from .discord import _post_webhook

_PENDING_FILE = "data/onboarding_pending.json"

_EMAIL_TYPE_LABEL = {
    "ambassador": "🤝 Embajador",
    "survey":     "📝 Como nos conociste",
    "welcome":    "👋 Bienvenida",
}


class LogChannel:
    def __init__(
        self,
        webhook_url:        str,
        heartbeat_interval: int,
        account_names:      List[str],
        # Callables opcionales para el heartbeat enriquecido
        get_ib_connected:   Optional[Callable[[], bool]] = None,
        get_systems:        Optional[Callable[[], Dict[str, bool]]] = None,
    ):
        self.webhook_url        = webhook_url
        self.heartbeat_interval = heartbeat_interval
        self.account_names      = account_names
        self._start_time        = datetime.now()
        self._heartbeat_task:   Optional[asyncio.Task] = None
        self._connected_accounts: List[str] = []
        self._get_ib_connected  = get_ib_connected
        self._get_systems       = get_systems

    async def start(self) -> None:
        await self.send_startup()
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())

    async def stop(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        await self.send_shutdown()

    def set_connected_accounts(self, accounts: List[str]) -> None:
        self._connected_accounts = accounts

    # ── Mensajes de sistema ──────────────────────────────────

    async def send_startup(self) -> None:
        msg = (
            "🟢 **MTO Sistema iniciado correctamente**\n"
            f"Cuentas configuradas: {', '.join(self.account_names)}\n"
            f"Hora inicio: {_now()}"
        )
        await self._send(msg)

    async def send_shutdown(self) -> None:
        await self._send(f"🔴 **MTO Sistema detenido** | {_now()}")

    async def send_connected(self) -> None:
        accounts_str = ", ".join(self._connected_accounts) if self._connected_accounts \
            else ", ".join(self.account_names)
        await self._send(
            f"🔄 **Reconectado a IB Gateway** | {_now()}\n"
            f"Cuentas activas: {accounts_str}"
        )

    async def send_disconnected(self, reason: str) -> None:
        await self._send(
            f"🔴 **Desconectado de IB Gateway** | {_now()}\n"
            f"Motivo: {reason}\nReconectando..."
        )

    async def send_info(self, message: str) -> None:
        await self._send(f"ℹ️ {message} | {_now()}")

    async def send_error(self, message: str) -> None:
        await self._send(f"⚠️ **Error** | {_now()}\n{message}")

    async def send_trade_confirmation(self, account_name: str, event_type: str,
                                      strategy_short: str, symbol: str,
                                      contracts: int, net_premium: float) -> None:
        sign = "+" if net_premium >= 0 else ""
        amount = f"{sign}${net_premium:,.2f}"
        await self._send(
            f"✅ **Operación** | {event_type} | {strategy_short} {symbol} | "
            f"{account_name} | {contracts} contrato(s) | {amount} | {_now()}"
        )

    # ── Heartbeat enriquecido ─────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        # Sincronizar con la hora en punto de Madrid
        now_mad = datetime.now(_TZ)
        secs_to_next_hour = (60 - now_mad.minute) * 60 - now_mad.second
        if secs_to_next_hour > 0:
            await asyncio.sleep(secs_to_next_hour)

        while True:
            await self._send_heartbeat_embed()
            await asyncio.sleep(self.heartbeat_interval)

    async def _send_heartbeat_embed(self) -> None:
        uptime       = _uptime(self._start_time)
        accounts_str = ", ".join(self._connected_accounts) if self._connected_accounts \
            else ", ".join(self.account_names)
        now_str = datetime.now(_TZ).strftime("%d/%m/%Y %H:%M") + " (Madrid)"

        fields = []
        all_ok = True

        # ── Campo 1: Sistemas ─────────────────────────────────
        if self._get_systems:
            systems = self._get_systems()
            lines = []
            for name, ok in systems.items():
                icon = "✅" if ok else "❌"
                lines.append(f"{icon} {name}")
                if not ok:
                    all_ok = False
            systems_text = "  ·  ".join(lines) if lines else "Sin datos"
            fields.append({
                "name":   "📊 Sistemas",
                "value":  systems_text,
                "inline": False,
            })

        # ── Campo 2: IB Gateway ───────────────────────────────
        if self._get_ib_connected:
            ib_ok = self._get_ib_connected()
            ib_icon = "✅" if ib_ok else "❌"
            if not ib_ok:
                all_ok = False
            fields.append({
                "name":   f"{ib_icon} IB Gateway",
                "value":  f"{'Conectado' if ib_ok else '**DESCONECTADO**'}  ·  {accounts_str}",
                "inline": False,
            })

        # ── Campo 3: Emails en cola ───────────────────────────
        pending_lines = _get_pending_emails_lines()
        if pending_lines:
            fields.append({
                "name":   f"📧 Emails en cola ({len(pending_lines)})",
                "value":  "\n".join(pending_lines),
                "inline": False,
            })
        else:
            fields.append({
                "name":   "📧 Emails en cola",
                "value":  "✅ Sin emails pendientes",
                "inline": False,
            })

        color = 0x2ECC71 if all_ok else 0xF39C12  # verde / naranja

        embed = {
            "title":       f"💚  Sistema activo  ·  {now_str}",
            "description": f"Uptime: **{uptime}**",
            "color":       color,
            "fields":      fields,
            "footer":      {"text": "MTO Bot · heartbeat horario"},
            "timestamp":   datetime.utcnow().isoformat() + "Z",
        }

        payload = {"embeds": [embed]}
        ok = await _post_webhook(self.webhook_url, payload)
        if not ok:
            logger.warning("Heartbeat: no se pudo enviar al canal de log")

    # ── Envío texto simple ────────────────────────────────────

    async def _send(self, message: str) -> None:
        payload = {"content": message}
        ok = await _post_webhook(self.webhook_url, payload)
        if not ok:
            logger.warning(f"No se pudo enviar al canal de log: {message[:80]}...")


# ── Helpers ───────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(_TZ).strftime("%d/%m/%Y %H:%M:%S") + " (Madrid)"


def _uptime(start: datetime) -> str:
    delta = datetime.now() - start
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m = rem // 60
    return f"{h}h {m}m"


def _get_pending_emails_lines() -> List[str]:
    """
    Lee onboarding_pending.json y devuelve las líneas para el embed.
    Solo muestra los que aún no han sido enviados (sent=False).
    """
    try:
        if not os.path.exists(_PENDING_FILE):
            return []
        with open(_PENDING_FILE, encoding="utf-8") as f:
            entries = json.load(f)

        now = datetime.now(_TZ)
        lines = []
        for e in entries:
            if e.get("sent", False):
                continue
            email_type = e.get("type", "?")
            label      = _EMAIL_TYPE_LABEL.get(email_type, f"📨 {email_type.capitalize()}")
            name       = e.get("name", "Desconocido")
            email      = e.get("email", "?")

            # Tiempo restante hasta send_at
            send_at_str = e.get("send_at", "")
            time_str = ""
            if send_at_str:
                try:
                    send_at = datetime.fromisoformat(send_at_str)
                    if send_at.tzinfo is None:
                        send_at = send_at.replace(tzinfo=_TZ)
                    diff = send_at - now.replace(tzinfo=_TZ if now.tzinfo is None else now.tzinfo)
                    total_secs = int(diff.total_seconds())
                    if total_secs <= 0:
                        time_str = "⏰ listo para enviar"
                    elif total_secs < 3600:
                        time_str = f"en {total_secs // 60}m"
                    elif total_secs < 86400:
                        time_str = f"en {total_secs // 3600}h"
                    else:
                        time_str = f"en {total_secs // 86400}d"
                except Exception:
                    pass

            line = f"{label}  ·  **{name}** ({email})"
            if time_str:
                line += f"  ·  {time_str}"
            lines.append(line)

        return lines
    except Exception as e:
        logger.warning(f"_get_pending_emails_lines error: {e}")
        return []
