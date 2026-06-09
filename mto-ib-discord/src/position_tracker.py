"""
Rastrea posiciones por cuenta para detectar apertura/cierre/ajuste.
Clave de posición: (account, symbol, sec_type, right, strike, expiry)
"""

from collections import defaultdict
from typing import Dict, List, Optional, Tuple
from loguru import logger
from ib_insync import IB, Position


PosKey = Tuple[str, str, str, str, float, str]  # account,sym,secType,right,strike,expiry


class TradeEvent:
    OPEN          = "APERTURA"
    CLOSE         = "CIERRE"
    PARTIAL_CLOSE = "CIERRE PARCIAL"
    ADD           = "INCREMENTO"
    ROLL          = "ROLL"
    UNKNOWN       = "AJUSTE"


class PositionTracker:
    def __init__(self):
        # {pos_key: signed_quantity}  — positivo=long, negativo=short
        self._positions: Dict[PosKey, float] = defaultdict(float)
        self._loaded = False

    async def load_from_ib(self, ib: IB) -> None:
        """Carga snapshot inicial de posiciones al arrancar."""
        try:
            positions: List[Position] = ib.positions()
            self._positions.clear()
            for pos in positions:
                key = _make_key(pos)
                self._positions[key] = pos.position
            self._loaded = True
            logger.info(f"Posiciones cargadas: {len(self._positions)} posiciones activas")
        except Exception as e:
            logger.error(f"Error cargando posiciones: {e}")

    def get_position(self, account: str, symbol: str, sec_type: str,
                     right: str, strike: float, expiry: str) -> float:
        key = (account, symbol, sec_type, right or "", strike or 0.0, expiry or "")
        return self._positions.get(key, 0.0)

    def update(self, account: str, symbol: str, sec_type: str,
               right: Optional[str], strike: Optional[float],
               expiry: Optional[str], new_qty: float) -> None:
        key = (account, symbol, sec_type, right or "", strike or 0.0, expiry or "")
        if new_qty == 0:
            self._positions.pop(key, None)
        else:
            self._positions[key] = new_qty

    def determine_trade_event(self, account: str, symbol: str, sec_type: str,
                               right: Optional[str], strike: Optional[float],
                               expiry: Optional[str], delta: float) -> str:
        """
        delta = cambio firmado de posición (positivo=comprado, negativo=vendido).
        Devuelve el tipo de evento de trading.
        """
        key = (account, symbol, sec_type, right or "", strike or 0.0, expiry or "")
        prev = self._positions.get(key, 0.0)
        new = prev + delta

        if prev == 0:
            return TradeEvent.OPEN
        if new == 0:
            return TradeEvent.CLOSE
        if abs(new) < abs(prev) and (prev * delta < 0):
            return TradeEvent.PARTIAL_CLOSE
        if abs(new) > abs(prev) and (prev * delta > 0):
            return TradeEvent.ADD
        return TradeEvent.UNKNOWN


def _make_key(pos: Position) -> PosKey:
    c = pos.contract
    return (
        pos.account,
        c.symbol,
        c.secType,
        getattr(c, "right", "") or "",
        getattr(c, "strike", 0.0) or 0.0,
        getattr(c, "lastTradeDateOrContractMonth", "") or "",
    )
