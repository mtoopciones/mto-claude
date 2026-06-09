"""
Clasificación de estrategias a partir de las patas (legs) de una orden.
Soporta: opciones simples, spreads verticales, calendarios, diagonales,
straddles, strangles, iron condors, butterflies y más.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class Leg:
    symbol: str
    sec_type: str           # OPT | STK | FUT
    right: Optional[str]    # P | C | None
    strike: Optional[float]
    expiry: Optional[str]   # YYYYMMDD
    action: str             # BUY | SELL
    quantity: float         # siempre positivo
    fill_price: float
    commission: float
    account: str
    order_id: int
    exec_time: datetime
    multiplier: float = 100.0
    company_name: str = ""
    exchange: str = ""
    realized_pnl: float = 0.0   # realizedPNL de IB (0 si no aplica / no disponible)


@dataclass
class StrategyInfo:
    name: str           # "Put Credit Spread"
    short_name: str     # "PCS"
    legs: List[Leg]
    is_credit: bool     # True = recibimos prima
    underlying: str
    primary_expiry: Optional[str]
    num_contracts: int  # contratos (basado en la pata principal)


# ─────────────────────────────────────────────────────────────
# Clasificador principal
# ─────────────────────────────────────────────────────────────

def classify(legs: List[Leg]) -> StrategyInfo:
    opt_legs = [l for l in legs if l.sec_type == "OPT"]
    stk_legs = [l for l in legs if l.sec_type == "STK"]

    underlying = legs[0].symbol if legs else "???"
    num_contracts = int(legs[0].quantity) if legs else 1

    # Solo acciones
    if not opt_legs and stk_legs:
        leg = stk_legs[0]
        name = "Long Stock" if leg.action == "BUY" else "Short Stock"
        return StrategyInfo(name, "Stock", legs, leg.action == "SELL",
                            underlying, None, num_contracts)

    # Covered Call: compra de acciones + venta de call en la misma orden
    if stk_legs and opt_legs and len(opt_legs) == 1:
        opt = opt_legs[0]
        stk = stk_legs[0]
        if opt.right == "C" and opt.action == "SELL" and stk.action == "BUY":
            expiry = opt.expiry
            return StrategyInfo("Covered Call", "CC", legs, True,
                                underlying, expiry, num_contracts)

    # Extraemos información útil
    puts = [l for l in opt_legs if l.right == "P"]
    calls = [l for l in opt_legs if l.right == "C"]
    sells = [l for l in opt_legs if l.action == "SELL"]
    buys = [l for l in opt_legs if l.action == "BUY"]
    expiries = sorted(set(l.expiry for l in opt_legs if l.expiry))
    primary_expiry = expiries[0] if expiries else None
    n = len(opt_legs)

    # ── 1 pata ──────────────────────────────────────────────
    if n == 1:
        l = opt_legs[0]
        if l.right == "P":
            name, short = ("Buy Put", "PUT") if l.action == "BUY" else ("Naked Put", "CSP")
            is_credit = l.action == "SELL"
        else:
            name, short = ("Buy Call", "CALL") if l.action == "BUY" else ("Naked Call", "SC")
            is_credit = l.action == "SELL"
        return StrategyInfo(name, short, legs, is_credit, underlying,
                            primary_expiry, num_contracts)

    # ── 2 patas ─────────────────────────────────────────────
    if n == 2:
        same_expiry = len(expiries) == 1

        # Mismo tipo de opción
        if len(puts) == 2 and same_expiry:
            sell_put = next((l for l in puts if l.action == "SELL"), None)
            buy_put = next((l for l in puts if l.action == "BUY"), None)
            if sell_put and buy_put:
                if sell_put.strike > buy_put.strike:
                    return StrategyInfo("Put Credit Spread", "PCS", legs, True,
                                        underlying, primary_expiry, num_contracts)
                else:
                    return StrategyInfo("Put Debit Spread", "PDS", legs, False,
                                        underlying, primary_expiry, num_contracts)

        if len(calls) == 2 and same_expiry:
            sell_call = next((l for l in calls if l.action == "SELL"), None)
            buy_call = next((l for l in calls if l.action == "BUY"), None)
            if sell_call and buy_call:
                if sell_call.strike < buy_call.strike:
                    return StrategyInfo("Call Credit Spread", "CCS", legs, True,
                                        underlying, primary_expiry, num_contracts)
                else:
                    return StrategyInfo("Call Debit Spread", "CDS", legs, False,
                                        underlying, primary_expiry, num_contracts)

        # Put + Call misma expiración
        if len(puts) == 1 and len(calls) == 1 and same_expiry:
            put_l, call_l = puts[0], calls[0]
            both_sell = put_l.action == "SELL" and call_l.action == "SELL"
            both_buy = put_l.action == "BUY" and call_l.action == "BUY"
            same_strike = put_l.strike == call_l.strike
            if both_sell:
                name, short = ("Short Straddle", "SS") if same_strike else ("Short Strangle", "SStr")
                return StrategyInfo(name, short, legs, True, underlying,
                                    primary_expiry, num_contracts)
            if both_buy:
                name, short = ("Long Straddle", "LS") if same_strike else ("Long Strangle", "LStr")
                return StrategyInfo(name, short, legs, False, underlying,
                                    primary_expiry, num_contracts)

        # Diferentes expiraciones = Calendar / Diagonal
        if len(expiries) == 2:
            if len(puts) == 2:
                same_strike = puts[0].strike == puts[1].strike
                name, short = ("Put Calendar", "PCal") if same_strike else ("Put Diagonal", "PDiag")
                is_credit = next((l for l in puts if l.action == "SELL"), None) is not None
                return StrategyInfo(name, short, legs, is_credit, underlying,
                                    expiries[-1], num_contracts)
            if len(calls) == 2:
                same_strike = calls[0].strike == calls[1].strike
                name, short = ("Call Calendar", "CCal") if same_strike else ("Call Diagonal", "CDiag")
                is_credit = next((l for l in calls if l.action == "SELL"), None) is not None
                return StrategyInfo(name, short, legs, is_credit, underlying,
                                    expiries[-1], num_contracts)

    # ── 3 patas ─────────────────────────────────────────────
    if n == 3:
        rights = puts if len(puts) == 3 else (calls if len(calls) == 3 else opt_legs)
        n_sells = sum(1 for l in rights if l.action == "SELL")
        if n_sells == 2:
            t = "puts" if len(puts) == 3 else "calls"
            name = f"Broken Wing Butterfly ({t.capitalize()})"
            short = "BWB"
            return StrategyInfo(name, short, legs, True, underlying,
                                primary_expiry, num_contracts)
        return StrategyInfo("3-Leg Strategy", "3L", legs, len(sells) > len(buys),
                            underlying, primary_expiry, num_contracts)

    # ── 4 patas ─────────────────────────────────────────────
    if n == 4:
        if len(puts) == 2 and len(calls) == 2:
            sell_put = next((l for l in puts if l.action == "SELL"), None)
            buy_put = next((l for l in puts if l.action == "BUY"), None)
            sell_call = next((l for l in calls if l.action == "SELL"), None)
            buy_call = next((l for l in calls if l.action == "BUY"), None)
            if all([sell_put, buy_put, sell_call, buy_call]):
                same_center = sell_put.strike == sell_call.strike
                name, short = ("Iron Butterfly", "IBUT") if same_center else ("Iron Condor", "ICON")
                return StrategyInfo(name, short, legs, True, underlying,
                                    primary_expiry, num_contracts)
        if len(puts) == 4:
            return StrategyInfo("Put Condor", "PCondor", legs, len(sells) == 2,
                                underlying, primary_expiry, num_contracts)
        if len(calls) == 4:
            return StrategyInfo("Call Condor", "CCondor", legs, len(sells) == 2,
                                underlying, primary_expiry, num_contracts)

    # Fallback genérico
    is_credit = len(sells) >= len(buys)
    return StrategyInfo(f"{n}-Leg Strategy", f"{n}L", legs, is_credit,
                        underlying, primary_expiry, num_contracts)
