"""
Mensajes al canal de log de Discord (canal 4).
Incluye: heartbeat periódico, errores, reconexiones, confirmaciones de trades.
"""

import asyncio
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo
from loguru import logger

_TZ = ZoneInfo("Europe/Madrid")

from .discord import _post_webhook


class LogChannel:
    def __init__(self, webhook_url: str, heartbeat_interval: int,
                 account_names: List[str]):
        self.webhook_url = webhook_url
        self.heartbeat_interval = heartbeat_interval
        self.account_names = account_names
        self._start_time = datetime.now()
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._connected_accounts: List[str] = []

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

    # ── Heartbeat ────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        # Esperar hasta la próxima hora en punto (Madrid) antes de empezar
        now_mad = datetime.now(_TZ)
        secs_to_next_hour = (60 - now_mad.minute) * 60 - now_mad.second
        if secs_to_next_hour > 0:
            await asyncio.sleep(secs_to_next_hour)

        while True:
            uptime = _uptime(self._start_time)
            accounts_str = ", ".join(self._connected_accounts) if self._connected_accounts \
                else "Verificando..."
            msg = (
                f"💚 **Sistema activo** | {_now()}\n"
                f"Cuentas: {accounts_str} | Uptime: {uptime}"
            )
            await self._send(msg)
            await asyncio.sleep(self.heartbeat_interval)

    # ── Envío ────────────────────────────────────────────────

    async def _send(self, message: str) -> None:
        payload = {"content": message}
        ok = await _post_webhook(self.webhook_url, payload)
        if not ok:
            logger.warning(f"No se pudo enviar al canal de log: {message[:80]}...")


def _now() -> str:
    return datetime.now(_TZ).strftime("%d/%m/%Y %H:%M:%S") + " (Madrid)"


def _uptime(start: datetime) -> str:
    delta = datetime.now() - start
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m = rem // 60
    return f"{h}h {m}m"
