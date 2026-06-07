"""
Flex Logbook Exporter — Genera Excel de operaciones desde IB Flex Query API.

Reemplaza el export semanal incremental de logbook_updater.py.
La fuente de verdad es siempre IB: sin duplicados, sin errores de estado.

Flujo:
  1. Llama a IB Flex Web Service (2 pasos: SendRequest → GetStatement)
  2. Parsea el XML: Trades + OpenPositions + OptionEAE
  3. Empareja aperturas con cierres por cuenta/symbol/strike/expiry/right (FIFO)
  4. Genera Excel con 3 hojas:
       - Hoja 1 "Resumen":   P&L mensual (opciones + acciones) + saldo inmovilizado
       - Hoja 2 "Opciones":  todas las operaciones con opciones (todas las cuentas)
       - Hoja 3 "Acciones":  todas las operaciones con acciones (todas las cuentas)
  5. Sube a Dropbox (carpeta DOCUMENTOS PUBLICADOS EN DISCORD)
  6. Publica en Discord como adjunto

Programado: sábados a las 11:40 Madrid (sustituye al export anterior).
"""

from __future__ import annotations

import asyncio
import io
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import aiohttp
from loguru import logger

try:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    OPENPYXL_OK = True
except ImportError:
    OPENPYXL_OK = False
    logger.warning("openpyxl no disponible — FlexLogbookExporter desactivado")

_MADRID = ZoneInfo("Europe/Madrid")

# IB Flex Web Service endpoints
_FLEX_SEND = (
    "https://gdcdyn.interactivebrokers.com"
    "/Universal/servlet/FlexStatementService.SendRequest"
)
_FLEX_GET = (
    "https://gdcdyn.interactivebrokers.com"
    "/Universal/servlet/FlexStatementService.GetStatement"
)

# Paleta de colores MTO
_C_HEADER   = "1A1A2E"   # azul oscuro MTO
_C_SUBHDR   = "2980B9"   # azul claro
_C_DARK2    = "2C3E50"   # cabeceras de tabla
_C_ALT      = "F0F4F8"   # filas alternas
_C_OPEN     = "E8F5E9"   # posición abierta
_C_EXPIRED  = "FFF8E1"   # expirada
_C_ASSIGNED = "FCE4EC"   # asignada/ejercida
_C_PROFIT   = "E8F5E9"   # P&L positivo
_C_LOSS     = "FFEBEE"   # P&L negativo
_C_WHITE    = "FFFFFF"
_C_TOTAL    = "1A1A2E"   # fila totales

# Nombres de meses en español (1-based)
_MESES = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
          "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de conversión
# ─────────────────────────────────────────────────────────────────────────────

def _float(val) -> float:
    try:
        return float(val) if val not in (None, "", "0", 0) else 0.0
    except Exception:
        return 0.0


def _parse_date(s: str) -> Optional[date]:
    """Parsea fecha IB: '20240115' o '2024-01-15'."""
    if not s:
        return None
    s = s.strip().replace("-", "")[:8]
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except Exception:
        return None


def _parse_dt(s: str) -> Optional[datetime]:
    """Parsea datetime IB: '20240115;093000', '20240115 09:30:00' o '20240115'."""
    if not s:
        return None
    s = s.strip().replace(";", " ").replace(",", " ")
    for fmt in ("%Y%m%d %H%M%S", "%Y%m%d %H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(s[: len(fmt)], fmt)
        except Exception:
            pass
    try:
        return datetime.strptime(s[:8], "%Y%m%d")
    except Exception:
        return None


def _action_es(buy_sell: str) -> str:
    return "Venta" if (buy_sell or "").upper() in ("SELL", "SLD") else "Compra"


def _right_es(put_call: str) -> str:
    r = (put_call or "").upper()
    if r == "P":
        return "PUT"
    if r == "C":
        return "CALL"
    return put_call or ""


def _strategy(buy_sell: str, put_call: str) -> str:
    """Nombre completo de estrategia para una sola pata de opción."""
    bs = (buy_sell or "").upper()
    pc = (put_call or "").upper()
    if bs in ("SELL", "SLD"):
        return "Short Put" if pc == "P" else ("Short Call" if pc == "C" else "Venta Opción")
    return "Long Put" if pc == "P" else ("Long Call" if pc == "C" else "Compra Opción")


def _opt_key(acct: str, sym: str, pc: str, strike: float, exp) -> tuple:
    exp_s = exp.strftime("%Y%m%d") if hasattr(exp, "strftime") else str(exp or "")
    return (acct, sym.upper(), (pc or "").upper(), round(strike or 0.0, 4), exp_s)


import re as _re
_OCC_RE = _re.compile(r'^([A-Z][A-Z0-9.]{0,9})\s+\d{6}[PC]\d', _re.IGNORECASE)

def _extract_underlying(sym: str) -> str:
    """
    Extrae el ticker subyacente de un símbolo OCC completo.
    Ej: 'UBER  260320C00110000' → 'UBER'
        'SPY   260327C00663000' → 'SPY'
    Si no es OCC, devuelve el símbolo tal cual.
    """
    if not sym:
        return sym
    m = _OCC_RE.match(sym.strip())
    return m.group(1) if m else sym.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Clase principal
# ─────────────────────────────────────────────────────────────────────────────

class FlexLogbookExporter:

    def __init__(
        self,
        flex_token:           str,
        flex_query_id:        str,
        account_names:        Dict[str, str],   # {account_id: display_name}
        dropbox_app_key:      str = "",
        dropbox_app_secret:   str = "",
        dropbox_refresh_token: str = "",
        log_channel=None,
    ):
        self.flex_token    = flex_token
        self.flex_query_id = flex_query_id
        self.account_names = account_names
        self.dbx_key       = dropbox_app_key
        self.dbx_secret    = dropbox_app_secret
        self.dbx_refresh   = dropbox_refresh_token
        self.log_channel   = log_channel
        self._root_ns_id: Optional[str] = None

    # ── Punto de entrada ──────────────────────────────────────────

    async def get_today_trades(self, target_date: date = None) -> Optional[dict]:
        """
        Obtiene los trades de hoy directamente de IB Flex Query.
        Usado por el informe de cierre de mercado para mostrar:
          - Aperturas del día con detalle (sym, estrategia, strike, vencimiento, prima)
          - Cierres del día con detalle (sym, estrategia, strike, vencimiento, P&L)
          - Prima neta cobrada real
          - P&L total de cierres

        Retorna None si la Flex Query falla (el daily_reporter usa el fallback).
        """
        if target_date is None:
            target_date = date.today()

        try:
            xml_data = await self._fetch_flex_xml()
            if not xml_data:
                return None

            opt_trades, _, open_positions = self._parse_xml(xml_data)

            # Filtrar trades del día
            today_trades = [
                t for t in opt_trades
                if t.get("dt") and t["dt"].date() == target_date
            ]
            if not today_trades:
                logger.info(f"Flex Query: sin trades de opciones para {target_date}")
                return {"opens": [], "closes": [], "prima_neta": 0.0, "pnl_cierres": 0.0}

            opens_raw  = [t for t in today_trades if t["oc"] == "O"]
            closes_raw = [t for t in today_trades if t["oc"] not in ("O",)]

            # Prima neta: suma neta de primas (créditos - débitos - comisiones)
            prima_neta = 0.0
            for t in opens_raw:
                mult = t.get("mult", 100.0)
                if t["bs"].upper() in ("SELL", "SLD"):
                    prima_neta += t["price"] * t["qty"] * mult - t["comm"]
                else:
                    prima_neta -= t["price"] * t["qty"] * mult + t["comm"]

            # Para P&L de cierres usar las filas emparejadas (FIFO)
            opt_rows    = self._build_option_rows(opt_trades, open_positions)
            close_rows  = [
                r for r in opt_rows
                if r.get("estado") != "Abierta"
                and r.get("dt_close")
                and (r["dt_close"].date() if isinstance(r["dt_close"], datetime) else r["dt_close"]) == target_date
            ]
            open_rows   = [
                r for r in opt_rows
                if r.get("estado") == "Abierta"
                and r.get("dt_open")
                and (r["dt_open"].date() if isinstance(r["dt_open"], datetime) else r["dt_open"]) == target_date
            ]

            # Rellenar account_name en open_rows si falta
            for r in open_rows + close_rows:
                if not r.get("acct_name") and r.get("_acct_id"):
                    r["acct_name"] = self.account_names.get(r["_acct_id"], r["_acct_id"])

            pnl_cierres = sum((r.get("pnl_neto") or 0.0) for r in close_rows)

            logger.info(
                f"Flex Query ({target_date}): "
                f"{len(open_rows)} aperturas, {len(close_rows)} cierres, "
                f"prima neta {prima_neta:+.2f}, P&L {pnl_cierres:+.2f}"
            )
            return {
                "opens":       open_rows,
                "closes":      close_rows,
                "prima_neta":  round(prima_neta, 2),
                "pnl_cierres": round(pnl_cierres, 2),
            }

        except Exception as e:
            logger.error(f"Flex Query get_today_trades: {e}")
            return None

    async def export_and_publish(self, discord_webhook: str = "") -> None:
        """Exporta el Excel desde Flex Query y lo publica en Discord."""
        if not OPENPYXL_OK:
            logger.error("FlexLogbookExporter: openpyxl no disponible")
            return
        try:
            logger.info("FlexLogbookExporter: iniciando exportación desde IB Flex Query…")

            xml_data = await self._fetch_flex_xml()
            if not xml_data:
                raise RuntimeError("No se pudo obtener datos de IB Flex Query")

            opt_trades, stk_trades, open_positions = self._parse_xml(xml_data)
            logger.info(
                f"FlexLogbookExporter: {len(opt_trades)} trades OPT · "
                f"{len(stk_trades)} trades STK · "
                f"{len(open_positions)} posiciones abiertas"
            )

            opt_rows = self._build_option_rows(opt_trades, open_positions)
            stk_rows = self._build_stock_rows(stk_trades)

            excel_bytes = self._build_excel(opt_rows, stk_rows, open_positions)

            # Subir a Dropbox
            fecha    = datetime.now(_MADRID)
            filename = fecha.strftime("%Y.%m.%d") + " Excel operaciones.xlsx"
            dest     = (
                "/Easy Tax Advice/CLIENTES/74141 MTO OPCIONES, S.L"
                "/COMPARTIDA MTO/PUBLICACIONES/DOCUMENTOS PUBLICADOS EN DISCORD"
                f"/{filename}"
            )
            uploaded = await self._dropbox_upload(excel_bytes, dest)
            if uploaded:
                logger.info(f"FlexLogbookExporter: subido a Dropbox → {filename}")

            if discord_webhook:
                await self._discord_publish(
                    excel_bytes, filename, discord_webhook, opt_rows, stk_rows
                )

            logger.info("FlexLogbookExporter: ✅ exportación completada")

        except Exception as e:
            logger.error(f"FlexLogbookExporter: error en export_and_publish: {e}")
            if self.log_channel:
                await self.log_channel.send_error(
                    f"❌ Error exportando Excel desde IB Flex Query: {e}"
                )

    # ── IB Flex Web Service ───────────────────────────────────────

    async def _fetch_flex_xml(self) -> Optional[bytes]:
        """
        Llama a IB Flex Web Service en 2 pasos:
          1. SendRequest → obtiene reference code
          2. GetStatement (con reintentos) → obtiene el XML
        """
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:

            # Paso 1: enviar request
            async with session.get(
                _FLEX_SEND,
                params={"t": self.flex_token, "q": self.flex_query_id, "v": "3"},
            ) as resp:
                if resp.status != 200:
                    logger.error(f"Flex SendRequest HTTP {resp.status}")
                    return None
                text = await resp.text()

            ref = self._extract_ref_code(text)
            if not ref:
                logger.error(f"Flex SendRequest: sin reference code. Resp: {text[:300]}")
                return None
            logger.info(f"Flex Query: reference code = {ref}")

            # Paso 2: obtener el statement (IB tarda unos segundos en generarlo)
            for attempt in range(12):
                await asyncio.sleep(4 if attempt == 0 else 6)
                try:
                    async with session.get(
                        _FLEX_GET,
                        params={"t": self.flex_token, "q": ref, "v": "3"},
                    ) as resp2:
                        if resp2.status != 200:
                            logger.warning(
                                f"Flex GetStatement HTTP {resp2.status} "
                                f"(intento {attempt+1})"
                            )
                            continue
                        data = await resp2.read()
                        snippet = data.decode("utf-8", errors="ignore")[:400]

                        # Si IB devuelve error de estado, reintentar
                        if (
                            "Statement generation in progress" in snippet
                            or "<Status>1</Status>" in snippet
                        ):
                            logger.info(
                                f"Flex Query: statement en preparación… "
                                f"(intento {attempt+1})"
                            )
                            continue

                        # Error de autenticación u otro error fatal
                        if "<ErrorCode>" in snippet and "<FlexQueryResponse" not in snippet:
                            logger.error(f"Flex Query error IB: {snippet[:200]}")
                            return None

                        return data
                except Exception as e:
                    logger.warning(f"Flex GetStatement intento {attempt+1}: {e}")

            logger.error("Flex Query: timeout esperando el statement (12 intentos)")
            return None

    def _extract_ref_code(self, xml_text: str) -> Optional[str]:
        try:
            root = ET.fromstring(xml_text)
            for elem in root.iter():
                if elem.tag in ("ReferenceCode", "referenceCode"):
                    return (elem.text or "").strip() or None
            ref = root.get("ReferenceCode") or root.get("referenceCode")
            return (ref or "").strip() or None
        except Exception as e:
            logger.error(f"Flex: error parseando reference code: {e}")
            return None

    # ── Parseo XML ────────────────────────────────────────────────

    def _parse_xml(
        self, xml_data: bytes
    ) -> Tuple[List[dict], List[dict], List[dict]]:
        """
        Parsea el XML de Flex Query.
        Retorna (opt_trades, stk_trades, open_positions).
        """
        opt_trades:     List[dict] = []
        stk_trades:     List[dict] = []
        open_positions: List[dict] = []

        try:
            root = ET.fromstring(xml_data)
        except Exception as e:
            logger.error(f"Flex: error parseando XML raíz: {e}")
            return opt_trades, stk_trades, open_positions

        for elem in root.iter():
            tag = elem.tag

            if tag == "Trade":
                t = self._parse_trade(elem)
                if t:
                    if t["asset"] == "OPT":
                        opt_trades.append(t)
                    elif t["asset"] == "STK":
                        stk_trades.append(t)

            elif tag == "OptionEAE":
                # Ejercicios, asignaciones y vencimientos de opciones
                t = self._parse_eae(elem)
                if t:
                    opt_trades.append(t)

            elif tag == "OpenPosition":
                p = self._parse_open_pos(elem)
                if p:
                    open_positions.append(p)

        # Ordenar cronológicamente
        opt_trades.sort(key=lambda x: x.get("dt") or datetime.min)
        stk_trades.sort(key=lambda x: x.get("dt") or datetime.min)

        return opt_trades, stk_trades, open_positions

    def _parse_trade(self, e) -> Optional[dict]:
        g     = e.get
        asset = g("assetCategory", "")
        if asset not in ("OPT", "STK"):
            return None

        # Símbolo: preferir underlyingSymbol si existe, limpiar OCC de lo contrario
        raw_sym  = g("symbol", "")
        under    = g("underlyingSymbol", "")
        sym      = under if under else _extract_underlying(raw_sym)

        # Fecha: intentar varios campos que IB puede usar
        dt_str = (g("dateTime") or g("tradeDate") or
                  g("settleDate") or g("reportDate") or "")
        dt = _parse_dt(dt_str)
        # Si sólo tenemos fecha sin hora (tradeDate = "YYYY-MM-DD")
        if dt is None and dt_str:
            dt = _parse_date(dt_str)
            if dt:
                dt = datetime(dt.year, dt.month, dt.day)

        return {
            "acct":   g("accountId", ""),
            "sym":    sym,
            "asset":  asset,
            "pc":     g("putCall", ""),
            "strike": _float(g("strike")),
            "expiry": _parse_date(g("expiry")),
            "bs":     g("buySell", ""),
            "qty":    abs(_float(g("quantity"))),
            "price":  _float(g("tradePrice")),
            "comm":   abs(_float(g("ibCommission"))),
            "dt":     dt,
            "oc":     (g("openCloseIndicator") or "O").upper(),
            "pnl":    _float(g("fifoPnlRealized")),
            "mult":   _float(g("multiplier")) or 100.0,
            "curr":   g("currency", "USD"),
            "eae":    "",
        }

    def _parse_eae(self, e) -> Optional[dict]:
        """Parsea OptionEAE: Exercise, Assignment, Expiration."""
        g    = e.get
        type_ = g("type", "")
        # Mapear type a openCloseIndicator equivalente
        oc_map = {"Ex": "EX", "As": "A", "Ep": "EP", "Exp": "EP"}
        qty   = _float(g("quantity"))
        raw_sym = g("symbol", "")
        under   = g("underlyingSymbol", "")
        sym     = under if under else _extract_underlying(raw_sym)
        return {
            "acct":   g("accountId", ""),
            "sym":    sym,
            "asset":  "OPT",
            "pc":     g("putCall", ""),
            "strike": _float(g("strike")),
            "expiry": _parse_date(g("expiry")),
            "bs":     "BUY" if qty > 0 else "SELL",
            "qty":    abs(qty),
            "price":  _float(g("tradePrice")),
            "comm":   abs(_float(g("ibCommission"))),
            "dt":     _parse_dt(g("date") or g("dateTime")),
            "oc":     oc_map.get(type_, "C"),
            "pnl":    _float(g("fifoPnlRealized")),
            "mult":   _float(g("multiplier")) or 100.0,
            "curr":   g("currency", "USD"),
            "eae":    type_,
        }

    def _parse_open_pos(self, e) -> Optional[dict]:
        g = e.get
        raw_sym = g("symbol", "")
        under   = g("underlyingSymbol", "")
        sym     = under if under else _extract_underlying(raw_sym)
        return {
            "acct":      g("accountId", ""),
            "sym":       sym,
            "asset":     g("assetCategory", ""),
            "pc":        g("putCall", ""),
            "strike":    _float(g("strike")),
            "expiry":    _parse_date(g("expiry")),
            "qty":       _float(g("position")),
            "mark":      _float(g("markPrice")),
            "open_px":   _float(g("openPrice") or g("costBasisPrice")),
            "unreal_pnl":_float(g("unrealizedPnl")),
            "mult":      _float(g("multiplier")) or 100.0,
            "curr":      g("currency", "USD"),
        }

    # ── Emparejamiento opens / closes ─────────────────────────────

    def _build_option_rows(
        self,
        trades:    List[dict],
        open_pos:  List[dict],
    ) -> List[dict]:
        """
        Empareja aperturas con cierres (FIFO).
        Las posiciones sin cierre se marcan como 'Abierta'.
        """
        # open_lots: key → lista de lotes abiertos (FIFO)
        open_lots: Dict[tuple, List[dict]] = defaultdict(list)
        rows: List[dict] = []

        for t in trades:
            key = _opt_key(t["acct"], t["sym"], t["pc"], t["strike"], t["expiry"])
            oc  = t["oc"]
            eae = t["eae"]

            if oc == "O":
                # ── Apertura ──────────────────────────────────────
                open_lots[key].append({
                    "acct":     t["acct"],
                    "sym":      t["sym"],
                    "pc":       t["pc"],
                    "strike":   t["strike"],
                    "expiry":   t["expiry"],
                    "bs":       t["bs"],
                    "qty":      t["qty"],
                    "price":    t["price"],
                    "comm":     t["comm"],
                    "dt":       t["dt"],
                    "mult":     t["mult"],
                    "curr":     t["curr"],
                    "strategy": _strategy(t["bs"], t["pc"]),
                })
            else:
                # ── Cierre, asignación, ejercicio, expiración ────
                estado_map = {
                    "C": "Cerrada", "A": "Asignada",
                    "EX": "Ejercida", "EP": "Expirada",
                }
                eae_map = {
                    "Ex": "Ejercida", "As": "Asignada",
                    "Ep": "Expirada", "Exp": "Expirada",
                }
                estado = eae_map.get(eae) or estado_map.get(oc, "Cerrada")
                # Cierre a precio 0 sin ser EAE → la opción expiró sin valor
                if estado == "Cerrada" and (t.get("price") or 0) == 0:
                    estado = "Expirada"

                remaining = t["qty"]
                while remaining > 0 and open_lots[key]:
                    lot       = open_lots[key][0]
                    closed    = min(remaining, lot["qty"])
                    lot_ratio = closed / lot["qty"] if lot["qty"] else 1.0
                    cls_ratio = closed / t["qty"] if t["qty"] else 1.0

                    # P&L bruto: usar fifoPnlRealized de IB si está disponible
                    if t["pnl"] != 0.0:
                        pnl_bruto = t["pnl"] * cls_ratio
                    elif lot["bs"].upper() in ("SELL", "SLD"):
                        pnl_bruto = (lot["price"] - t["price"]) * closed * t["mult"]
                    else:
                        pnl_bruto = (t["price"] - lot["price"]) * closed * t["mult"]

                    open_comm  = lot["comm"] * lot_ratio
                    close_comm = t["comm"] * cls_ratio
                    pnl_neto   = round(pnl_bruto - open_comm - close_comm, 2)

                    rows.append(self._opt_row(
                        lot=lot,
                        closed_qty=closed,
                        open_comm=round(open_comm, 2),
                        close_dt=t["dt"],
                        close_bs=t["bs"],
                        close_price=t["price"],
                        close_comm=round(close_comm, 2),
                        pnl_bruto=round(pnl_bruto, 2),
                        pnl_neto=pnl_neto,
                        estado=estado,
                        mult=t["mult"],
                        curr=t["curr"],
                    ))

                    remaining -= closed
                    if closed >= lot["qty"]:
                        open_lots[key].pop(0)
                    else:
                        open_lots[key][0]["qty"] -= closed
                        open_lots[key][0]["comm"] *= (1.0 - lot_ratio)

                # Contratos sin apertura conocida
                if remaining > 0:
                    rows.append({
                        "acct_name":   self.account_names.get(t["acct"], t["acct"]),
                        "sym":         t["sym"],
                        "strategy":    _strategy(t["bs"], t["pc"]),
                        "pc":          _right_es(t["pc"]),
                        "strike":      t["strike"],
                        "expiry":      t["expiry"],
                        "action_open": "—",
                        "qty":         remaining,
                        "price_open":  None,
                        "comm_open":   None,
                        "dt_open":     None,
                        "dt_close":    t["dt"],
                        "action_close":_action_es(t["bs"]),
                        "price_close": t["price"],
                        "comm_close":  t["comm"],
                        "pnl_bruto":   t["pnl"] or 0.0,
                        "pnl_neto":    round((t["pnl"] or 0.0) - t["comm"], 2),
                        "estado":      estado,
                        "mult":        t["mult"],
                        "curr":        t["curr"],
                        "notional":    None,
                    })

        # ── Índice de posiciones abiertas (para mark price y P&L no realizado) ──
        pos_idx = {
            _opt_key(p["acct"], p["sym"], p["pc"], p["strike"], p["expiry"]): p
            for p in open_pos
            if p["asset"] == "OPT"
        }

        # ── Añadir lotes aún abiertos ─────────────────────────────
        for key, lots in open_lots.items():
            for lot in lots:
                pos = pos_idx.get(key, {})
                notional = (lot["strike"] or 0) * (lot["mult"] or 100) * lot["qty"]
                rows.append({
                    "acct_name":   self.account_names.get(lot["acct"], lot["acct"]),
                    "sym":         lot["sym"],
                    "strategy":    lot["strategy"],
                    "pc":          _right_es(lot["pc"]),
                    "strike":      lot["strike"],
                    "expiry":      lot["expiry"],
                    "action_open": _action_es(lot["bs"]),
                    "qty":         lot["qty"],
                    "price_open":  lot["price"],
                    "comm_open":   lot["comm"],
                    "dt_open":     lot["dt"],
                    "dt_close":    None,
                    "action_close":None,
                    "price_close": pos.get("mark"),
                    "comm_close":  None,
                    "pnl_bruto":   pos.get("unreal_pnl"),
                    "pnl_neto":    pos.get("unreal_pnl"),
                    "estado":      "Abierta",
                    "mult":        lot["mult"],
                    "curr":        lot["curr"],
                    "notional":    notional,
                })

        # Filtrar filas fantasma: cierres de OptionEAE sin apertura conocida y Strike=0
        # (surgen cuando la apertura fue antes del rango de la Flex Query y el cierre es EAE)
        rows = [
            r for r in rows
            if not (
                r.get("action_open") == "—"
                and (r.get("strike") or 0) == 0
                and r.get("price_open") is None
            )
        ]

        # Ordenar: abiertas primero, luego por fecha apertura
        rows.sort(key=lambda r: (
            0 if r["estado"] == "Abierta" else 1,
            r["dt_open"] or datetime.min,
        ))
        return rows

    @staticmethod
    def _opt_row(
        lot, closed_qty, open_comm,
        close_dt, close_bs, close_price, close_comm,
        pnl_bruto, pnl_neto, estado, mult, curr,
    ) -> dict:
        return {
            "acct_name":   "",        # se rellenará tras la llamada
            "_acct_id":    lot["acct"],
            "sym":         lot["sym"],
            "strategy":    lot["strategy"],
            "pc":          _right_es(lot["pc"]),
            "strike":      lot["strike"],
            "expiry":      lot["expiry"],
            "action_open": _action_es(lot["bs"]),
            "qty":         closed_qty,
            "price_open":  lot["price"],
            "comm_open":   open_comm,
            "dt_open":     lot["dt"],
            "dt_close":    close_dt,
            "action_close":_action_es(close_bs),
            "price_close": close_price,
            "comm_close":  close_comm,
            "pnl_bruto":   pnl_bruto,
            "pnl_neto":    pnl_neto,
            "estado":      estado,
            "mult":        mult,
            "curr":        curr,
            "notional":    None,
        }

    def _build_stock_rows(self, trades: List[dict]) -> List[dict]:
        """Empareja compras con ventas de acciones (FIFO)."""
        open_lots: Dict[tuple, List[dict]] = defaultdict(list)
        rows: List[dict] = []

        for t in trades:
            key = (t["acct"], t["sym"])
            bs  = (t["bs"] or "").upper()
            qty = t["qty"]

            if bs in ("BUY", "BOT"):
                open_lots[key].append({
                    "acct":  t["acct"],
                    "sym":   t["sym"],
                    "qty":   qty,
                    "price": t["price"],
                    "comm":  t["comm"],
                    "dt":    t["dt"],
                    "curr":  t["curr"],
                })
            else:  # SELL / SLD
                remaining = qty
                while remaining > 0 and open_lots[key]:
                    lot    = open_lots[key][0]
                    closed = min(remaining, lot["qty"])
                    ratio  = closed / lot["qty"] if lot["qty"] else 1.0
                    cls_r  = closed / qty if qty else 1.0

                    pnl_b  = (t["price"] - lot["price"]) * closed
                    o_comm = lot["comm"] * ratio
                    c_comm = t["comm"] * cls_r
                    pnl_n  = round(pnl_b - o_comm - c_comm, 2)

                    rows.append({
                        "acct_name":   self.account_names.get(lot["acct"], lot["acct"]),
                        "sym":         lot["sym"],
                        "qty":         closed,
                        "price_open":  lot["price"],
                        "comm_open":   round(o_comm, 2),
                        "dt_open":     lot["dt"],
                        "dt_close":    t["dt"],
                        "price_close": t["price"],
                        "comm_close":  round(c_comm, 2),
                        "pnl_bruto":   round(pnl_b, 2),
                        "pnl_neto":    pnl_n,
                        "estado":      "Cerrada",
                        "curr":        t["curr"],
                    })

                    remaining -= closed
                    if closed >= lot["qty"]:
                        open_lots[key].pop(0)
                    else:
                        open_lots[key][0]["qty"] -= closed
                        open_lots[key][0]["comm"] *= (1.0 - ratio)

                if remaining > 0:
                    rows.append({
                        "acct_name":   self.account_names.get(t["acct"], t["acct"]),
                        "sym":         t["sym"],
                        "qty":         remaining,
                        "price_open":  None,
                        "comm_open":   None,
                        "dt_open":     None,
                        "dt_close":    t["dt"],
                        "price_close": t["price"],
                        "comm_close":  t["comm"],
                        "pnl_bruto":   t["pnl"] or 0.0,
                        "pnl_neto":    round((t["pnl"] or 0.0) - t["comm"], 2),
                        "estado":      "Cerrada",
                        "curr":        t["curr"],
                    })

        # Posiciones abiertas restantes
        for key, lots in open_lots.items():
            for lot in lots:
                rows.append({
                    "acct_name":   self.account_names.get(lot["acct"], lot["acct"]),
                    "sym":         lot["sym"],
                    "qty":         lot["qty"],
                    "price_open":  lot["price"],
                    "comm_open":   lot["comm"],
                    "dt_open":     lot["dt"],
                    "dt_close":    None,
                    "price_close": None,
                    "comm_close":  None,
                    "pnl_bruto":   None,
                    "pnl_neto":    None,
                    "estado":      "Abierta",
                    "curr":        lot["curr"],
                })

        rows.sort(key=lambda r: (
            0 if r["estado"] == "Abierta" else 1,
            r["dt_open"] or datetime.min,
        ))
        return rows

    # ── Generación del Excel ──────────────────────────────────────

    def _build_excel(
        self,
        opt_rows:  List[dict],
        stk_rows:  List[dict],
        open_pos:  List[dict],
    ) -> bytes:
        # Rellenar account_name donde falta (filas con _acct_id)
        for r in opt_rows:
            if not r.get("acct_name") and r.get("_acct_id"):
                r["acct_name"] = self.account_names.get(r["_acct_id"], r["_acct_id"])

        wb = openpyxl.Workbook()
        wb.remove(wb.active)

        ws_res = wb.create_sheet("Resumen",  0)
        ws_opt = wb.create_sheet("Opciones", 1)
        ws_stk = wb.create_sheet("Acciones", 2)

        self._sheet_options(ws_opt, opt_rows)
        self._sheet_stocks(ws_stk, stk_rows)
        self._sheet_summary(ws_res, opt_rows, stk_rows)

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    # ── Hoja Opciones ─────────────────────────────────────────────

    def _sheet_options(self, ws, rows: List[dict]) -> None:
        headers = [
            "Cuenta", "Fecha Apertura", "Ticker", "Estrategia",
            "Strike", "Vencimiento", "P/C", "Acción Ap.",
            "Contratos", "Prima Ap.", "Comisión Ap.",
            "Fecha Cierre", "Acción Ci.", "Prima Ci.", "Comisión Ci.",
            "P&L Bruto", "P&L Neto", "Estado",
        ]
        widths = [20, 17, 9, 11, 9, 14, 7, 11, 11, 11, 13, 17, 11, 11, 13, 12, 12, 12]

        ws.append(headers)
        self._hdr_row(ws, 1, len(headers), _C_HEADER)
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows)+1}"

        for i, r in enumerate(rows, 2):
            estado = r.get("estado", "")
            bg = (
                _C_OPEN     if estado == "Abierta"  else
                _C_EXPIRED  if estado == "Expirada" else
                _C_ASSIGNED if estado in ("Asignada", "Ejercida") else
                _C_WHITE
            )
            ws.append([
                r.get("acct_name", ""),
                r.get("dt_open"),
                r.get("sym", ""),
                r.get("strategy", ""),
                r.get("strike"),
                r.get("expiry"),
                r.get("pc", ""),
                r.get("action_open", ""),
                r.get("qty"),
                r.get("price_open"),
                r.get("comm_open"),
                r.get("dt_close"),
                r.get("action_close") or "",
                r.get("price_close"),
                r.get("comm_close"),
                r.get("pnl_bruto"),
                r.get("pnl_neto"),
                estado,
            ])
            fill = PatternFill("solid", fgColor=bg)
            for col in range(1, len(headers) + 1):
                c = ws.cell(row=i, column=col)
                c.fill      = fill
                c.alignment = Alignment(horizontal="center", vertical="center")
                c.font      = Font(size=10)
            # Fechas
            for col in (2, 12):
                ws.cell(row=i, column=col).number_format = "DD/MM/YYYY"
            # Números
            for col in (5, 10, 14):
                c = ws.cell(row=i, column=col)
                if isinstance(c.value, (int, float)):
                    c.number_format = "#,##0.00"
            for col in (11, 15, 16, 17):
                c = ws.cell(row=i, column=col)
                if isinstance(c.value, (int, float)):
                    c.number_format = "#,##0.00"
            # Colores P&L
            for col in (16, 17):
                c = ws.cell(row=i, column=col)
                if isinstance(c.value, (int, float)):
                    pos = c.value >= 0
                    c.fill = PatternFill("solid", fgColor=_C_PROFIT if pos else _C_LOSS)
                    c.font = Font(size=10, bold=True,
                                  color="27AE60" if pos else "E74C3C")

        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    # ── Hoja Acciones ─────────────────────────────────────────────

    def _sheet_stocks(self, ws, rows: List[dict]) -> None:
        headers = [
            "Cuenta", "Fecha Apertura", "Ticker",
            "Cantidad", "Precio Ap.", "Comisión Ap.",
            "Fecha Cierre", "Precio Ci.", "Comisión Ci.",
            "P&L Bruto", "P&L Neto", "Estado",
        ]
        widths = [20, 17, 10, 10, 12, 13, 17, 12, 13, 12, 12, 12]

        ws.append(headers)
        self._hdr_row(ws, 1, len(headers), _C_SUBHDR)
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows)+1}"

        for i, r in enumerate(rows, 2):
            estado = r.get("estado", "")
            bg     = _C_OPEN if estado == "Abierta" else _C_WHITE
            ws.append([
                r.get("acct_name", ""),
                r.get("dt_open"),
                r.get("sym", ""),
                r.get("qty"),
                r.get("price_open"),
                r.get("comm_open"),
                r.get("dt_close"),
                r.get("price_close"),
                r.get("comm_close"),
                r.get("pnl_bruto"),
                r.get("pnl_neto"),
                estado,
            ])
            fill = PatternFill("solid", fgColor=bg)
            for col in range(1, len(headers) + 1):
                c = ws.cell(row=i, column=col)
                c.fill      = fill
                c.alignment = Alignment(horizontal="center")
                c.font      = Font(size=10)
            for col in (2, 7):
                ws.cell(row=i, column=col).number_format = "DD/MM/YYYY"
            for col in (5, 8, 10, 11):
                c = ws.cell(row=i, column=col)
                if isinstance(c.value, (int, float)):
                    c.number_format = "#,##0.00"
            for col in (10, 11):
                c = ws.cell(row=i, column=col)
                if isinstance(c.value, (int, float)):
                    pos = c.value >= 0
                    c.fill = PatternFill("solid", fgColor=_C_PROFIT if pos else _C_LOSS)
                    c.font = Font(size=10, bold=True,
                                  color="27AE60" if pos else "E74C3C")

        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    # ── Hoja Resumen ──────────────────────────────────────────────

    def _sheet_summary(
        self,
        ws,
        opt_rows: List[dict],
        stk_rows: List[dict],
    ) -> None:
        """
        Resumen con el mismo formato que el Log_book anterior:
          - Tabla 1: "Beneficio realizado" — P&L neto por cuenta y mes (operaciones cerradas)
          - Tabla 2: "Efectivo cobrado"    — Prima neta de posiciones abiertas por cuenta y mes
        Columnas: [nombre] | Total | mes_N | mes_N-1 | … | mes_1 | Acumulado
        """
        # ── Mapeo de nombre largo → nombre corto para el resumen ─────
        _DISPLAY = {
            "MTO Cuenta 10K":        "10 k",
            "MTO Cuenta 50k":        "50 k",
            "MTO Dividendos ETF 5K": "Dividendos - ETF",
        }
        _ACCT_ORDER = ["10 k", "50 k", "Dividendos - ETF", "Op. Sueltas"]
        N_COLS = 5   # meses individuales a mostrar; el resto va a "Acumulado"

        def _display(name: str) -> str:
            return _DISPLAY.get(name, "Op. Sueltas")

        def _month_key(dt) -> str:
            if isinstance(dt, datetime):
                return dt.strftime("%Y-%m")
            if isinstance(dt, date):
                return dt.strftime("%Y-%m")
            return str(dt)[:7] if dt else ""

        def _month_label(key: str) -> str:
            try:
                yr, mo = int(key[:4]), int(key[5:7])
                return f"{_MESES[mo].lower()}-{str(yr)[2:]}"
            except Exception:
                return key

        # ── 1. Calcular beneficio_realizado: P&L neto de cierres ─────
        # {(display_name, month_key): pnl_neto}
        ben: Dict[tuple, float] = defaultdict(float)
        all_months_ben: set = set()

        for r in opt_rows + stk_rows:
            if r.get("estado") == "Abierta":
                continue
            pnl = (r.get("pnl_bruto") or 0.0) - (r.get("comm_open") or 0.0) - (r.get("comm_close") or 0.0)
            dt  = r.get("dt_close") or r.get("dt_open")
            mk  = _month_key(dt)
            if not mk:
                continue
            dn = _display(r.get("acct_name", ""))
            ben[(dn, mk)] += pnl
            all_months_ben.add(mk)

        # ── 2. Calcular efectivo_cobrado: prima neta de posiciones abiertas ──
        # Para cada posición abierta: SELL = +prima cobrada, BUY = -prima pagada
        efec: Dict[tuple, float] = defaultdict(float)
        all_months_efec: set = set()

        for r in opt_rows:
            if r.get("estado") != "Abierta":
                continue
            mk = _month_key(r.get("dt_open"))
            if not mk:
                continue
            dn     = _display(r.get("acct_name", ""))
            qty    = r.get("qty") or 0
            price  = r.get("price_open") or 0.0
            comm   = r.get("comm_open") or 0.0
            mult   = r.get("mult") or 100.0
            if (r.get("action_open") or "").lower() == "venta":
                cash = price * qty * mult - comm
            else:
                cash = -(price * qty * mult + comm)
            efec[(dn, mk)] += cash
            all_months_efec.add(mk)

        # ── Elegir columnas de meses (los N más recientes) ─────────────
        def _pivot_cols(all_months: set):
            sorted_months = sorted(all_months, reverse=True)
            shown   = sorted_months[:N_COLS]   # más recientes
            acum    = sorted_months[N_COLS:]    # más antiguos → "Acumulado"
            return shown, acum

        # ── Helper para escribir una tabla pivote ──────────────────────
        def _write_pivot(
            ws, start_row: int,
            title: str, nota1: str, nota2: str,
            data: Dict[tuple, float],
            shown_months: list, acum_months: list,
        ) -> int:
            r = start_row
            ncols = 2 + len(shown_months) + 1   # nombre + Total + meses + Acumulado

            # Cabecera de sección
            for col in range(1, ncols + 1):
                c = ws.cell(row=r, column=col)
                c.fill = PatternFill("solid", fgColor="D9D9D9")
                c.font = Font(bold=True, size=10)
                c.border = _thin_border()
            ws.cell(row=r, column=1).value = title
            ws.cell(row=r, column=2).value = "Total"
            for i, mk in enumerate(shown_months):
                ws.cell(row=r, column=3 + i).value = _month_label(mk)
                ws.cell(row=r, column=3 + i).alignment = Alignment(horizontal="center")
            ws.cell(row=r, column=ncols).value = "Acumulado"
            ws.cell(row=r, column=ncols).alignment = Alignment(horizontal="center")
            ws.cell(row=r, column=2).alignment = Alignment(horizontal="center")
            r += 1

            # Filas de cuentas
            for acct in _ACCT_ORDER:
                row_total = sum(v for (dn, mk), v in data.items() if dn == acct)
                row_shown = [sum(v for (dn, mk), v in data.items()
                                 if dn == acct and mk == m) for m in shown_months]
                row_acum  = sum(v for (dn, mk), v in data.items()
                                if dn == acct and mk in acum_months)

                vals = [acct, row_total] + row_shown + [row_acum]
                for col, val in enumerate(vals, 1):
                    c = ws.cell(row=r, column=col, value=val if val != 0.0 else None)
                    c.font      = Font(size=10)
                    c.border    = _thin_border()
                    c.alignment = Alignment(
                        horizontal="left" if col == 1 else "right",
                        vertical="center",
                    )
                    if col > 1 and isinstance(val, (int, float)) and val != 0.0:
                        c.number_format = '#,##0.00'
                        if val < 0:
                            c.font = Font(size=10, color="CC0000")
                r += 1

            # Fila vacía
            r += 1

            # Fila total
            total_total  = sum(data.values())
            total_shown  = [sum(v for (dn, mk), v in data.items()
                                if mk == m) for m in shown_months]
            total_acum   = sum(v for (dn, mk), v in data.items()
                               if mk in acum_months)
            tot_vals = ["Total", total_total] + total_shown + [total_acum]
            for col, val in enumerate(tot_vals, 1):
                c = ws.cell(row=r, column=col, value=val if val != 0.0 else None)
                c.font      = Font(bold=True, size=10)
                c.fill      = PatternFill("solid", fgColor="D9D9D9")
                c.border    = _thin_border()
                c.alignment = Alignment(
                    horizontal="left" if col == 1 else "right",
                    vertical="center",
                )
                if col > 1 and isinstance(val, (int, float)) and val != 0.0:
                    c.number_format = '#,##0.00'
                    if val < 0:
                        c.font = Font(bold=True, size=10, color="CC0000")
            r += 1

            # Notas
            for nota in (nota1, nota2):
                ws.cell(row=r, column=1).value = nota
                ws.cell(row=r, column=1).font  = Font(size=8, italic=True, color="666666")
                ws.merge_cells(f"A{r}:{get_column_letter(ncols)}{r}")
                r += 1

            return r + 1   # espacio entre tablas

        # ── Thin border helper ─────────────────────────────────────────
        from openpyxl.styles import Border, Side

        def _thin_border():
            s = Side(style="thin", color="CCCCCC")
            return Border(left=s, right=s, top=s, bottom=s)

        # ── Calcular columnas para cada tabla ──────────────────────────
        shown_ben,  acum_ben  = _pivot_cols(all_months_ben)
        shown_efec, acum_efec = _pivot_cols(all_months_efec or all_months_ben)

        # ── Escribir las dos tablas ────────────────────────────────────
        next_row = _write_pivot(
            ws, start_row=1,
            title="Beneficio realizado *",
            nota1="* Importe correspondiente a operaciones cerradas, la fecha del beneficio se computa en la fecha de cierre.",
            nota2="* El valor de estos saldos solo se puede modificar por el mes en curso, a mes cerrado es invariable.",
            data=ben,
            shown_months=shown_ben,
            acum_months=acum_ben,
        )

        _write_pivot(
            ws, start_row=next_row,
            title="Efectivo cobrado *",
            nota1="* Efectivo de primas cobradas de operaciones no cerradas, a medida que se cierran operaciones, este valor tiende a cero.",
            nota2="* El valor de los meses cerrados se modifica en tanto se van cerrando operaciones, siempre debe tender a cero.",
            data=efec,
            shown_months=shown_efec,
            acum_months=acum_efec,
        )

        # Anchos de columna
        ws.column_dimensions["A"].width = 20
        ws.column_dimensions["B"].width = 14
        for i in range(3, 3 + N_COLS + 1):
            ws.column_dimensions[get_column_letter(i)].width = 13

    # ── Helpers de estilo ─────────────────────────────────────────

    @staticmethod
    def _hdr_row(ws, row: int, ncols: int, color: str) -> None:
        for col in range(1, ncols + 1):
            c = ws.cell(row=row, column=col)
            c.font      = Font(bold=True, size=10, color="FFFFFF")
            c.fill      = PatternFill("solid", fgColor=color)
            c.alignment = Alignment(horizontal="center", vertical="center",
                                    wrap_text=True)
        ws.row_dimensions[row].height = 28

    # ── Dropbox ───────────────────────────────────────────────────

    async def _dropbox_upload(self, data: bytes, path: str) -> bool:
        if not self.dbx_refresh:
            return False
        try:
            token     = await self._dbx_token()
            if not token:
                return False
            path_root = await self._dbx_path_root(token)
            headers   = {
                "Authorization":   f"Bearer {token}",
                "Dropbox-API-Arg": json.dumps({
                    "path": path, "mode": "overwrite",
                    "autorename": False, "mute": True,
                }),
                "Content-Type": "application/octet-stream",
                **path_root,
            }
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://content.dropboxapi.com/2/files/upload",
                    headers=headers, data=data,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.warning(f"Dropbox upload {resp.status}: {text[:100]}")
                        return False
                    return True
        except Exception as e:
            logger.error(f"FlexLogbook Dropbox upload: {e}")
            return False

    async def _dbx_token(self) -> Optional[str]:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://api.dropboxapi.com/oauth2/token",
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": self.dbx_refresh,
                    "client_id":     self.dbx_key,
                    "client_secret": self.dbx_secret,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    return (await resp.json()).get("access_token")
        return None

    async def _dbx_path_root(self, token: str) -> dict:
        if not self._root_ns_id:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        "https://api.dropboxapi.com/2/users/get_current_account",
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json"},
                        data=b"null",
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            acc = await resp.json()
                            self._root_ns_id = (
                                acc.get("root_info", {}).get("root_namespace_id")
                            )
            except Exception as e:
                logger.warning(f"Dropbox path root: {e}")
        if self._root_ns_id:
            return {"Dropbox-API-Path-Root": json.dumps({
                ".tag": "namespace_id",
                "namespace_id": self._root_ns_id,
            })}
        return {}

    # ── Discord ───────────────────────────────────────────────────

    async def _discord_publish(
        self,
        excel_bytes: bytes,
        filename:    str,
        webhook_url: str,
        opt_rows:    List[dict],
        stk_rows:    List[dict],
    ) -> None:
        n_open   = sum(1 for r in opt_rows if r.get("estado") == "Abierta")
        n_closed = sum(1 for r in opt_rows if r.get("estado") != "Abierta")
        n_stk    = len(stk_rows)
        fecha    = datetime.now(_MADRID).strftime("%d/%m/%Y")

        content = (
            f"📊 **EXCEL OPERACIONES  —  {fecha}**\n"
            f"Log_book completo con todos los valores actualizados."
        )

        boundary = "DiscordFileBoundary7MA4YW"
        nl       = b"\r\n"
        body     = (
            b"--" + boundary.encode() + nl
            + b'Content-Disposition: form-data; name="payload_json"' + nl
            + b"Content-Type: application/json" + nl + nl
            + json.dumps({"content": content}).encode() + nl
            + b"--" + boundary.encode() + nl
            + b'Content-Disposition: form-data; name="file"; filename="'
            + filename.encode("utf-8") + b'"' + nl
            + b"Content-Type: application/vnd.openxmlformats-officedocument"
              b".spreadsheetml.sheet" + nl + nl
            + excel_bytes + nl
            + b"--" + boundary.encode() + b"--" + nl
        )

        async with aiohttp.ClientSession() as s:
            async with s.post(
                webhook_url, data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status in (200, 204):
                    logger.info(f"FlexLogbook: ✅ Excel publicado en Discord ({filename})")
                else:
                    text = await resp.text()
                    raise RuntimeError(f"Discord {resp.status}: {text[:100]}")


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def build_from_config(cfg: dict, log_channel=None) -> Optional[FlexLogbookExporter]:
    flex_cfg = cfg.get("ib_flex", {})
    token    = flex_cfg.get("token", "").strip()
    query_id = str(flex_cfg.get("query_id", "")).strip()

    if not token or not query_id:
        return None

    lb_cfg = cfg.get("logbook", {})
    account_names = {
        acc["id"]: acc["name"]
        for acc in cfg.get("accounts", [])
        if acc.get("id") and acc.get("name")
    }

    return FlexLogbookExporter(
        flex_token            = token,
        flex_query_id         = query_id,
        account_names         = account_names,
        dropbox_app_key       = lb_cfg.get("dropbox_app_key", ""),
        dropbox_app_secret    = lb_cfg.get("dropbox_app_secret", ""),
        dropbox_refresh_token = lb_cfg.get("dropbox_refresh_token", ""),
        log_channel           = log_channel,
    )
