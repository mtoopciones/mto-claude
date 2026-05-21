"""
Calcula métricas financieras a partir de las patas de la operación.
Todas las cifras son POR CONTRATO salvo indicación contraria.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional, Tuple

from .strategy import Leg, StrategyInfo


@dataclass
class TradeMetrics:
    net_premium: float
    total_commission: float
    net_premium_after_comm: float
    buying_power: float
    max_gain: float
    max_loss: float
    dte: Optional[int]
    roi_pct: Optional[float] = None
    breakeven: Optional[float] = None
    breakeven_high: Optional[float] = None
    close_cost: Optional[float] = None
    trade_result: Optional[float] = None
    realized_pnl: Optional[float] = None   # PNL real reportado por IB


def calculate(strategy: StrategyInfo, open_premium: Optional[float] = None) -> TradeMetrics:
    legs = strategy.legs
    opt_legs = [l for l in legs if l.sec_type == "OPT"]

    total_commission = sum(l.commission for l in legs)
    num_contracts = strategy.num_contracts

    # fill_price es ya por acción → × multiplier da el valor por contrato.
    # NO dividir por num_contracts: la suma de patas ya da la prima neta
    # por unidad de estrategia (spread, naked, etc.) independientemente
    # de cuántos contratos se hayan negociado.
    gross_premium_per_contract = sum(
        (l.fill_price if l.action == "SELL" else -l.fill_price) * l.multiplier
        for l in opt_legs
    )

    comm_per_contract = total_commission / max(num_contracts, 1)
    net_premium = gross_premium_per_contract - comm_per_contract

    dte = _calc_dte(strategy.primary_expiry, legs[0].exec_time if legs else None)

    max_gain, max_loss, buying_power = _calc_max(strategy, gross_premium_per_contract)
    max_gain_net = max_gain - comm_per_contract
    max_loss_net = max_loss - comm_per_contract

    buying_power_final = round(buying_power - comm_per_contract, 2)

    roi_pct = None
    if net_premium > 0 and buying_power_final != 0:
        # Estrategia de crédito: ROI = prima neta / capital comprometido
        roi_pct = round((net_premium / abs(buying_power_final)) * 100, 2)
    elif (max_gain_net not in (float("inf"), float("-inf"))
          and max_gain_net > 0
          and max_loss_net < 0):
        # Estrategia de débito (spread): ROI = ganancia máxima / coste máximo
        roi_pct = round((max_gain_net / abs(max_loss_net)) * 100, 2)

    breakeven, breakeven_high = _calc_breakeven(strategy, gross_premium_per_contract)

    close_cost: Optional[float] = None
    trade_result: Optional[float] = None
    realized_pnl_total: Optional[float] = None

    if open_premium is not None:
        close_cost = -net_premium
        trade_result = open_premium + net_premium

    # Si alguna pata trae realizedPNL de IB, lo usamos directamente
    pnl_from_ib = sum(l.realized_pnl for l in legs)
    if pnl_from_ib != 0.0:
        realized_pnl_total = round(pnl_from_ib, 2)
        # Para cierres: close_cost = coste bruto de cerrar (prima + comisión)
        if close_cost is None:
            close_cost = round(-net_premium, 2)   # net_premium es negativo en cierres de débito
        # trade_result viene de IB directamente (más preciso que el cálculo local)
        trade_result = realized_pnl_total

    return TradeMetrics(
        net_premium=round(gross_premium_per_contract, 2),
        total_commission=round(total_commission, 2),
        net_premium_after_comm=round(net_premium, 2),
        buying_power=buying_power_final,
        max_gain=round(max_gain_net, 2),
        max_loss=round(max_loss_net, 2),
        dte=dte,
        roi_pct=roi_pct,
        breakeven=breakeven,
        breakeven_high=breakeven_high,
        close_cost=round(close_cost, 2) if close_cost is not None else None,
        trade_result=round(trade_result, 2) if trade_result is not None else None,
        realized_pnl=realized_pnl_total,
    )


def _calc_max(strategy: StrategyInfo, gross_premium: float):
    opt_legs = [l for l in strategy.legs if l.sec_type == "OPT"]
    puts = [l for l in opt_legs if l.right == "P"]
    calls = [l for l in opt_legs if l.right == "C"]
    short = strategy.short_name

    if short in ("PCS", "CCS"):
        all_legs = puts if puts else calls
        strikes = sorted(set(l.strike for l in all_legs if l.strike))
        spread_width = (max(strikes) - min(strikes)) * 100 if len(strikes) >= 2 else 500
        max_gain = gross_premium
        max_loss = -(spread_width - gross_premium)
        return max_gain, max_loss, max_loss

    if short in ("PDS", "CDS", "BPS", "BCS"):
        all_legs = puts if puts else calls
        strikes = sorted(set(l.strike for l in all_legs if l.strike))
        spread_width = (max(strikes) - min(strikes)) * 100 if len(strikes) >= 2 else 500
        max_gain = spread_width + gross_premium
        max_loss = gross_premium
        return max_gain, max_loss, gross_premium

    if short in ("ICON", "IC"):
        put_strikes = sorted(set(l.strike for l in puts if l.strike))
        call_strikes = sorted(set(l.strike for l in calls if l.strike))
        put_width = (max(put_strikes) - min(put_strikes)) * 100 if len(put_strikes) >= 2 else 500
        call_width = (max(call_strikes) - min(call_strikes)) * 100 if len(call_strikes) >= 2 else 500
        max_span = max(put_width, call_width)
        max_gain = gross_premium
        max_loss = -(max_span - gross_premium)
        return max_gain, max_loss, max_loss

    if short in ("IBUT", "IBF"):
        put_strikes = sorted(set(l.strike for l in puts if l.strike))
        call_strikes = sorted(set(l.strike for l in calls if l.strike))
        max_span = max(
            (max(put_strikes) - min(put_strikes)) if len(put_strikes) >= 2 else 0,
            (max(call_strikes) - min(call_strikes)) if len(call_strikes) >= 2 else 0,
        ) * 100
        max_gain = gross_premium
        max_loss = -(max_span - gross_premium)
        return max_gain, max_loss, max_loss

    if short in ("SS", "SStr"):
        max_gain = gross_premium
        max_loss = float("-inf")
        buying_power = -gross_premium * 3
        return max_gain, max_loss, buying_power

    if short in ("LS", "LStr"):
        max_gain = float("inf")
        max_loss = gross_premium
        return max_gain, max_loss, gross_premium

    if short in ("CSP", "SP"):
        strikes = [l.strike for l in opt_legs if l.strike]
        strike = min(strikes) if strikes else 0
        max_gain = gross_premium
        max_loss = -(strike * 100 - gross_premium)
        buying_power = -(strike * 100 * 0.20)
        return max_gain, max_loss, buying_power

    if short == "SC":
        max_gain = gross_premium
        max_loss = float("-inf")
        buying_power = -gross_premium * 5
        return max_gain, max_loss, buying_power

    max_gain = gross_premium if gross_premium > 0 else float("inf")
    max_loss = gross_premium if gross_premium < 0 else float("-inf")
    return max_gain, max_loss, -abs(gross_premium)


def _calc_breakeven(strategy: StrategyInfo, gross_premium: float) -> Tuple[Optional[float], Optional[float]]:
    opt_legs = [l for l in strategy.legs if l.sec_type == "OPT"]
    puts = [l for l in opt_legs if l.right == "P"]
    calls = [l for l in opt_legs if l.right == "C"]
    short = strategy.short_name
    prem_per_share = gross_premium / 100

    if short in ("CSP", "SP"):
        strikes = [l.strike for l in puts if l.strike]
        if strikes:
            return round(min(strikes) - prem_per_share, 2), None

    if short == "PCS":
        sell_strikes = [l.strike for l in puts if l.action == "SELL" and l.strike]
        if sell_strikes:
            return round(max(sell_strikes) - prem_per_share, 2), None

    if short == "CCS":
        sell_strikes = [l.strike for l in calls if l.action == "SELL" and l.strike]
        if sell_strikes:
            return round(min(sell_strikes) + prem_per_share, 2), None

    if short in ("PDS", "BPS"):
        # Debit put spread: compra put alta, vende put baja
        # B/E = strike compra put - prima neta por acción (prima es negativa)
        buy_strikes = [l.strike for l in puts if l.action == "BUY" and l.strike]
        if buy_strikes:
            return round(max(buy_strikes) + prem_per_share, 2), None

    if short in ("CDS", "BCS"):
        # Debit call spread: compra call baja, vende call alta
        # B/E = strike compra call + prima neta por acción (prima es negativa → suma negativa)
        buy_strikes = [l.strike for l in calls if l.action == "BUY" and l.strike]
        if buy_strikes:
            return round(min(buy_strikes) - prem_per_share, 2), None

    if short in ("ICON", "IBUT", "IC", "IBF"):
        sell_put = next((l for l in puts if l.action == "SELL" and l.strike), None)
        sell_call = next((l for l in calls if l.action == "SELL" and l.strike), None)
        if sell_put and sell_call:
            return round(sell_put.strike - prem_per_share, 2), round(sell_call.strike + prem_per_share, 2)

    if short == "SS":
        sell = next((l for l in opt_legs if l.action == "SELL" and l.strike), None)
        if sell:
            return round(sell.strike - prem_per_share, 2), round(sell.strike + prem_per_share, 2)

    if short == "SStr":
        sell_put = next((l for l in puts if l.action == "SELL" and l.strike), None)
        sell_call = next((l for l in calls if l.action == "SELL" and l.strike), None)
        if sell_put and sell_call:
            return round(sell_put.strike - prem_per_share, 2), round(sell_call.strike + prem_per_share, 2)

    return None, None


def _calc_dte(expiry: Optional[str], exec_time: Optional[datetime]) -> Optional[int]:
    if not expiry or not exec_time:
        return None
    try:
        exp_date = date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8]))
        return (exp_date - exec_time.date()).days
    except Exception:
        return None
