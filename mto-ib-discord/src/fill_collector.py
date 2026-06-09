"""
Recibe fills de IB uno a uno y los agrupa por orden (orderId).
Espera `debounce_seconds` tras el último fill de una orden antes de procesar,
para capturar todas las patas de un spread en una sola llamada.
"""

import asyncio
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple
from loguru import logger
from ib_insync import Fill

from .strategy import Leg


# (account, orderId)
_OrderKey = Tuple[str, int]


class FillCollector:
    def __init__(self, debounce_seconds: float, on_order_complete: Callable):
        self.debounce = debounce_seconds
        self.on_order_complete = on_order_complete
        # {key: {"fills": [...], "task": asyncio.Task}}
        self._pending: Dict[_OrderKey, dict] = {}

    def handle_fill(self, fill: Fill) -> None:
        """Llamado desde el evento execDetailsEvent de ib_insync."""
        try:
            key = (fill.execution.acctNumber, fill.execution.orderId)
            if key not in self._pending:
                self._pending[key] = {"fills": [], "task": None}
                logger.debug(f"Nueva orden detectada: {key}")

            self._pending[key]["fills"].append(fill)

            # Cancelar timer anterior y arrancar uno nuevo
            task = self._pending[key].get("task")
            if task and not task.done():
                task.cancel()

            self._pending[key]["task"] = asyncio.ensure_future(
                self._process_after_delay(key)
            )
        except Exception as e:
            logger.error(f"Error en handle_fill: {e}")

    async def _process_after_delay(self, key: _OrderKey) -> None:
        await asyncio.sleep(self.debounce)
        entry = self._pending.get(key)
        if not entry:
            return

        fills: List[Fill] = entry["fills"]

        # IB a veces envía el commission report milisegundos después del fill.
        # Si alguna pata de opción aún no tiene comisión, esperamos hasta 3s más.
        opt_fills = [f for f in fills
                     if f.contract and getattr(f.contract, "secType", "") == "OPT"]
        if opt_fills:
            for _ in range(6):  # comprueba cada 0.5s durante 3s max
                all_have_comm = all(
                    f.commissionReport and f.commissionReport.commission >= 0
                    for f in opt_fills
                )
                if all_have_comm:
                    break
                await asyncio.sleep(0.5)

        entry = self._pending.pop(key, None)
        if not entry:
            return

        try:
            legs = _fills_to_legs(fills)
            if legs:
                await self.on_order_complete(legs)
        except Exception as e:
            logger.error(f"Error procesando orden {key}: {e}")


def _fills_to_legs(fills: List[Fill]) -> List[Leg]:
    legs = []
    for fill in fills:
        c = fill.contract
        ex = fill.execution
        commission = 0.0
        realized_pnl = 0.0
        if fill.commissionReport:
            commission = fill.commissionReport.commission or 0.0
            # realizedPNL: IB usa 1.7976931348623157e+308 como centinela cuando no aplica
            rp = getattr(fill.commissionReport, "realizedPNL", None)
            if rp is not None and rp < 1e300:
                realized_pnl = float(rp)

        # IB usa 'BOT' / 'SLD' — normalizamos a BUY / SELL
        action = "BUY" if ex.side == "BOT" else "SELL"

        leg = Leg(
            symbol=c.symbol,
            sec_type=c.secType,
            right=getattr(c, "right", None) or None,
            strike=getattr(c, "strike", None),
            expiry=getattr(c, "lastTradeDateOrContractMonth", None) or None,
            action=action,
            quantity=abs(ex.shares),
            fill_price=ex.price,
            commission=commission,
            account=ex.acctNumber,
            order_id=ex.orderId,
            exec_time=fill.time.replace(tzinfo=None) if fill.time else datetime.now(),
            multiplier=float(getattr(c, "multiplier", 100) or 100),
            company_name="",
            exchange=c.exchange or "",
            realized_pnl=realized_pnl,
        )
        legs.append(leg)
        pnl_str = f" | realizedPNL: {realized_pnl:.2f}" if realized_pnl != 0.0 else ""
        logger.debug(f"  Leg: {action} {leg.quantity}x {c.symbol} "
                     f"{getattr(c,'right','')}{getattr(c,'strike','')} "
                     f"@ {ex.price:.2f} | comisión: {commission:.2f}{pnl_str}")
    return legs
