"""
Gestión de la conexión con IB Gateway.
Reconexión automática con backoff exponencial.
"""

import asyncio
from typing import Callable, Optional
from loguru import logger
from ib_insync import IB, Contract


class IBConnector:
    def __init__(self, cfg: dict, on_connected: Callable, on_disconnected: Callable):
        self.host = cfg["ib"]["host"]
        self.port = cfg["ib"]["port"]
        self.client_id = cfg["ib"].get("client_id", 1)
        self.reconnect_interval = cfg["ib"].get("reconnect_interval", 30)
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected

        self.ib = IB()
        self._running = False
        self._reconnect_task: Optional[asyncio.Task] = None

        self.ib.disconnectedEvent += self._handle_disconnect
        self._keepalive_task: Optional[asyncio.Task] = None

    @property
    def connected(self) -> bool:
        return self.ib.isConnected()

    async def start(self) -> None:
        self._running = True
        await self._connect()

    async def stop(self) -> None:
        self._running = False
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self.ib.isConnected():
            self.ib.disconnect()

    async def _connect(self) -> None:
        attempt = 0
        while self._running:
            try:
                logger.info(f"Conectando a IB Gateway {self.host}:{self.port} "
                            f"(client_id={self.client_id})")
                await self.ib.connectAsync(self.host, self.port,
                                           clientId=self.client_id, timeout=20)
                logger.info("Conexión establecida con IB Gateway")
                await self.on_connected(self.ib)
                self._keepalive_task = asyncio.ensure_future(self._keepalive())
                return
            except Exception as e:
                attempt += 1
                wait = min(self.reconnect_interval * attempt, 300)
                logger.warning(f"No se pudo conectar: {e}. Reintentando en {wait}s...")
                await self.on_disconnected(str(e))
                await asyncio.sleep(wait)

    def _handle_disconnect(self) -> None:
        if not self._running:
            return
        logger.warning("Desconectado de IB Gateway")
        self._reconnect_task = asyncio.ensure_future(self._reconnect())

    async def _reconnect(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        await self.on_disconnected("Desconexión inesperada")
        await asyncio.sleep(self.reconnect_interval)
        await self._connect()

    async def _keepalive(self) -> None:
        """Envía un ping cada 3 minutos para evitar desconexiones por inactividad."""
        try:
            while self._running and self.ib.isConnected():
                await asyncio.sleep(180)
                if self.ib.isConnected():
                    self.ib.reqCurrentTime()
                    logger.debug("Keep-alive IB Gateway enviado")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Keep-alive error: {e}")

    async def get_contract_details(self, contract: Contract) -> Optional[str]:
        """Devuelve el nombre de la empresa del contrato, si está disponible."""
        try:
            details = await self.ib.reqContractDetailsAsync(contract)
            if details:
                return details[0].longName
        except Exception:
            pass
        return None
