"""
Reportes diarios de mercado y operaciones enviados a Discord.
  - Pre-mercado:  09:10 ET (20 min antes apertura NYSE 09:30)
  - Post-mercado: 16:15 ET (15 min después cierre NYSE 16:00)
Solo días laborables (lunes–viernes).
"""

import asyncio
import json
import os
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo
import aiohttp
from loguru import logger

from .position_tracker import TradeEvent


ET_ZONE         = ZoneInfo("America/New_York")
MADRID_ZONE     = ZoneInfo("Europe/Madrid")
PREMARKET_TIME  = time(9, 10)   # 20 min antes apertura
POSTMARKET_TIME = time(16, 15)  # 15 min después cierre
WEEKLY_TIME          = time(11, 30)  # sábados 11:30 Madrid → P&L
PORTFOLIO_TIME       = time(11, 35)  # sábados 11:35 Madrid → cartera
LOGBOOK_EXPORT_TIME  = time(11, 40)  # sábados 11:40 Madrid → Excel operaciones

_SPANISH_NEWS_FEEDS = [
    "https://e00-expansion.uecdn.es/rss/portada.xml",
    "https://cincodias.elpais.com/rss/cincodias/ultimasnoticias/",
    "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/economia/portada",
]

_MARKET_TICKERS = {
    "S&P 500": "%5EGSPC",
    "Nasdaq":  "%5EIXIC",
    "VIX":     "%5EVIX",
}

_FUTURES_TICKERS = {
    "S&P 500":   "ES=F",
    "Nasdaq 100": "NQ=F",
}

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}


class DailyReporter:
    _STATE_FILE = "data/daily_reporter_state.json"

    def __init__(self, webhook_url: str):
        self.webhook_url        = webhook_url
        self.log_channel        = None   # asignado desde main.py
        self.premarket_webhook       = ""     # asignado desde main.py
        self.pnl_tracker        = None   # asignado desde main.py
        self.weekly_webhook          = ""     # asignado desde main.py
        self.portfolio_tracker       = None   # asignado desde main.py
        self.portfolio_webhook       = ""     # asignado desde main.py
        self.logbook_updater         = None   # asignado desde main.py (legacy)
        self.flex_logbook_exporter   = None   # asignado desde main.py (nuevo — IB Flex Query)
        self.logbook_export_webhook  = ""     # asignado desde main.py
        self.facebook_poster         = None   # asignado desde main.py
        self.instagram_poster        = None   # asignado desde main.py
        self.twitter_poster          = None   # asignado desde main.py
        self.social_cfg              = {}     # cfg completo para leer flags
        self.ib                      = None   # referencia IB (asignado desde main.py al conectar)
        self.approver                = None   # asignado desde main.py si está activo
        self._reset_day()
        self._load_state()   # restaurar operaciones del día si el bot se reinició

    # ── Persistencia de estado (sobrevive reinicios del bot) ─────

    def _save_state(self) -> None:
        """Guarda las operaciones del día en disco tras cada registro."""
        try:
            os.makedirs(os.path.dirname(self._STATE_FILE), exist_ok=True)
            state = {
                "date":            date.today().isoformat(),
                "opens":           self._opens,
                "rolls":           self._rolls,
                "closes":          self._closes,
                "prem_collected":  self._prem_collected,
                "prem_paid":       self._prem_paid,
                "pnl_closed":      self._pnl_closed,
            }
            with open(self._STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Daily reporter: no se pudo guardar estado: {e}")

    def _load_state(self) -> None:
        """Al arrancar, restaura las operaciones del día si el estado guardado es de hoy."""
        try:
            if not os.path.exists(self._STATE_FILE):
                return
            with open(self._STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
            if state.get("date") != date.today().isoformat():
                logger.debug("Daily reporter: estado guardado es de otro día, ignorando")
                return
            self._opens           = state.get("opens",  [])
            self._rolls           = state.get("rolls",  [])
            self._closes          = state.get("closes", [])
            self._prem_collected  = float(state.get("prem_collected", 0.0))
            self._prem_paid       = float(state.get("prem_paid",      0.0))
            self._pnl_closed      = float(state.get("pnl_closed",     0.0))
            # Poblar el set de claves ya registradas para evitar duplicados en recovery
            for e in self._opens:
                self._seen_keys.add((e["symbol"], e["account"], "open"))
            for e in self._closes:
                self._seen_keys.add((e["symbol"], e["account"], "close"))
            for e in self._rolls:
                self._seen_keys.add((e["symbol"], e["account"], "roll"))
            total = len(self._opens) + len(self._rolls) + len(self._closes)
            logger.info(
                f"Daily reporter: {total} operación(es) restauradas desde disco "
                f"({len(self._opens)} aperturas, {len(self._closes)} cierres, {len(self._rolls)} rolls)"
            )
        except Exception as e:
            logger.warning(f"Daily reporter: no se pudo cargar estado: {e}")

    # ── Registro de operaciones del día ───────────────────────

    # Estrategias que por definición son APERTURAS cuando reciben crédito neto.
    # Si una pata cierra una posición previa del mismo subyacente (ej: long PUT
    # de un PCS anterior), determine_trade_event devuelve CLOSE para esa pata.
    # Pero el SPREAD en su conjunto es una apertura → forzamos OPEN.
    _CREDIT_OPENING_NAMES = {
        "PCS", "CCS", "CS", "BWB", "IC", "Iron Condor",
        "CC", "CSP", "SP",  # Covered Call, Cash Secured Put, Short Put
    }

    def record_trade(self, event_type: str, strategy, metrics, account_name: str) -> None:
        prem      = metrics.net_premium_after_comm or 0.0

        # Corrección: spread de crédito neto → siempre es APERTURA
        if (event_type in (TradeEvent.CLOSE, TradeEvent.PARTIAL_CLOSE)
                and strategy.short_name in self._CREDIT_OPENING_NAMES
                and prem > 0):
            logger.info(
                f"Daily reporter: {strategy.short_name} {strategy.underlying} "
                f"reclasificado CLOSE→OPEN (crédito neto={prem:+.2f}, spread de apertura)"
            )
            event_type = TradeEvent.OPEN

        ev_simple = "open" if event_type in (TradeEvent.OPEN, TradeEvent.ADD) else "close"
        key       = (strategy.underlying, account_name, ev_simple)

        # Evitar duplicados cuando la recovery de IB re-añade ops ya en el estado de disco
        if key in self._seen_keys:
            logger.debug(f"Daily reporter: {strategy.underlying} ({account_name}) ya registrado, ignorando duplicado")
            return
        self._seen_keys.add(key)

        entry = {"symbol": strategy.underlying, "account": account_name, "date": date.today().isoformat()}
        if event_type in (TradeEvent.OPEN, TradeEvent.ADD):
            self._opens.append(entry)
        elif event_type in (TradeEvent.CLOSE, TradeEvent.PARTIAL_CLOSE):
            self._closes.append(entry)
            self._pnl_closed += metrics.trade_result or 0.0

        self._prem_collected += max(prem, 0.0)
        self._prem_paid      += max(-prem, 0.0)
        self._save_state()

    def record_roll(self, open_strategy, open_metrics, account_name: str) -> None:
        prem = open_metrics.net_premium_after_comm or 0.0
        key  = (open_strategy.underlying, account_name, "roll")
        if key in self._seen_keys:
            logger.debug(f"Daily reporter: roll {open_strategy.underlying} ({account_name}) ya registrado, ignorando duplicado")
            return
        self._seen_keys.add(key)
        self._rolls.append({"symbol": open_strategy.underlying, "account": account_name, "date": date.today().isoformat()})
        self._prem_collected += max(prem, 0.0)
        self._prem_paid      += max(-prem, 0.0)
        self._save_state()

    # ── Scheduler ─────────────────────────────────────────────

    async def start(self) -> None:
        asyncio.ensure_future(self._loop())
        asyncio.ensure_future(self._weekly_loop())
        asyncio.ensure_future(self._portfolio_loop())
        asyncio.ensure_future(self._logbook_export_loop())
        logger.info("Daily reporter iniciado")

    async def _loop(self) -> None:
        while True:
            try:
                now_et = datetime.now(ET_ZONE)
                today  = now_et.date()

                if today.weekday() >= 5:  # sábado o domingo
                    monday = today + timedelta(days=(7 - today.weekday()))
                    await _sleep_until(datetime.combine(monday, time(8, 0), tzinfo=ET_ZONE))
                    continue

                pre_dt  = datetime.combine(today, PREMARKET_TIME,  tzinfo=ET_ZONE)
                post_dt = datetime.combine(today, POSTMARKET_TIME, tzinfo=ET_ZONE)

                if now_et < pre_dt:
                    await _sleep_until(pre_dt)
                    await self.send_premarket()
                    # Esperar hasta post-mercado
                    await _sleep_until(datetime.combine(today, POSTMARKET_TIME, tzinfo=ET_ZONE))
                    await self.send_postmarket()
                    self._reset_day(new_day=True)
                    await _sleep_until(datetime.combine(_next_trading_day(today), time(8, 0), tzinfo=ET_ZONE))

                elif now_et < post_dt:
                    await _sleep_until(post_dt)
                    await self.send_postmarket()
                    self._reset_day(new_day=True)
                    await _sleep_until(datetime.combine(_next_trading_day(today), time(8, 0), tzinfo=ET_ZONE))

                else:
                    # Ya pasó todo hoy
                    self._reset_day(new_day=True)
                    await _sleep_until(datetime.combine(_next_trading_day(today), time(8, 0), tzinfo=ET_ZONE))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error en daily reporter loop: {e}")
                await asyncio.sleep(300)

    async def _weekly_loop(self) -> None:
        """Cada sábado a las 11:30 hora Madrid publica el reporte semanal de P&L."""
        while True:
            try:
                now_mad      = datetime.now(MADRID_ZONE)
                today        = now_mad.date()
                # 5 = sábado en weekday()
                days_to_sat  = (5 - today.weekday()) % 7
                if days_to_sat == 0 and now_mad.time() >= WEEKLY_TIME:
                    days_to_sat = 7   # ya pasó hoy, ir al siguiente sábado
                next_sat = today + timedelta(days=days_to_sat)
                fire_dt  = datetime.combine(next_sat, WEEKLY_TIME, tzinfo=MADRID_ZONE)
                logger.info(
                    f"Weekly P&L: próximo envío el {fire_dt.strftime('%d/%m/%Y %H:%M')} Madrid"
                )
                await _sleep_until(fire_dt)
                if self.pnl_tracker and self.weekly_webhook:
                    image_url = await self.pnl_tracker.send_weekly_report(self.weekly_webhook)
                    await self._post_to_social("post_weekly_pnl", image_url,
                        "📊 Resumen semanal P&L de MTO Opciones — ¡nueva semana, nuevas oportunidades! #opciones #trading")
                else:
                    logger.warning("Weekly report: pnl_tracker o weekly_webhook no configurados")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error en weekly loop: {e}")
                await asyncio.sleep(3600)

    async def _portfolio_loop(self) -> None:
        """Cada sábado a las 11:35 hora Madrid publica el reporte de evolución de cartera."""
        while True:
            try:
                now_mad     = datetime.now(MADRID_ZONE)
                today       = now_mad.date()
                days_to_sat = (5 - today.weekday()) % 7
                if days_to_sat == 0 and now_mad.time() >= PORTFOLIO_TIME:
                    days_to_sat = 7
                next_sat = today + timedelta(days=days_to_sat)
                fire_dt  = datetime.combine(next_sat, PORTFOLIO_TIME, tzinfo=MADRID_ZONE)
                logger.info(
                    f"Portfolio: próximo envío el {fire_dt.strftime('%d/%m/%Y %H:%M')} Madrid"
                )
                await _sleep_until(fire_dt)
                if self.portfolio_tracker and self.portfolio_webhook:
                    image_url = await self.portfolio_tracker.send_weekly_report(self.portfolio_webhook)
                    await self._post_to_social("post_portfolio", image_url,
                        "📈 Evolución de cartera semanal de MTO Opciones #opciones #trading #bolsa")
                else:
                    logger.warning("Portfolio report: tracker o webhook no configurados")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error en portfolio loop: {e}")
                await asyncio.sleep(3600)

    async def _logbook_export_loop(self) -> None:
        """Cada sábado a las 11:40 hora Madrid exporta el Log_book a Discord."""
        while True:
            try:
                now_mad     = datetime.now(MADRID_ZONE)
                today       = now_mad.date()
                days_to_sat = (5 - today.weekday()) % 7
                if days_to_sat == 0 and now_mad.time() >= LOGBOOK_EXPORT_TIME:
                    days_to_sat = 7
                next_sat = today + timedelta(days=days_to_sat)
                fire_dt  = datetime.combine(next_sat, LOGBOOK_EXPORT_TIME, tzinfo=MADRID_ZONE)
                logger.info(
                    f"Logbook export: próximo envío el {fire_dt.strftime('%d/%m/%Y %H:%M')} Madrid"
                )
                await _sleep_until(fire_dt)
                if self.flex_logbook_exporter and self.logbook_export_webhook:
                    # Nuevo: genera el Excel directamente desde IB Flex Query
                    await self.flex_logbook_exporter.export_and_publish(
                        self.logbook_export_webhook
                    )
                    await self._post_to_social("post_logbook_export", None,
                        "📋 Log de operaciones semanal actualizado — MTO Opciones #opciones #trading")
                elif self.logbook_updater and self.logbook_export_webhook:
                    # Fallback legacy: export incremental desde el Excel en Dropbox
                    await self.logbook_updater.export_and_publish(self.logbook_export_webhook)
                    await self._post_to_social("post_logbook_export", None,
                        "📋 Log de operaciones semanal actualizado — MTO Opciones #opciones #trading")
                else:
                    logger.warning("Logbook export: ni flex_logbook_exporter ni logbook_updater configurados")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error en logbook export loop: {e}")
                await asyncio.sleep(3600)

    # ── Reporte pre-mercado ────────────────────────────────────

    async def send_premarket(self) -> None:
        try:
            now_et = datetime.now(ET_ZONE)
            today  = now_et.date()
            fecha  = _fmt_date_es(today)

            # Datos de futuros: IB Gateway primero, Yahoo como fallback.
            # frozen=False → tipo 3 (Delayed streaming): precio pre-market actual -15 min.
            # No usar tipo 4 aquí: congelaría al cierre de ayer → valor incorrecto.
            ib_quotes = {}
            if self.ib and self.ib.isConnected():
                ib_quotes = await _fetch_ib_quotes(self.ib, frozen=False)
                if ib_quotes:
                    logger.info(f"Pre-mercado: datos IB ({list(ib_quotes.keys())})")
                else:
                    logger.warning("Pre-mercado: IB no devolvió datos, usando Yahoo")

            if ib_quotes:
                futures = {
                    "S&P 500":    ib_quotes.get("S&P 500", {}),
                    "Nasdaq 100": ib_quotes.get("Nasdaq",  {}),
                }
                vix = ib_quotes.get("VIX", {}).get("price", 0.0)
                macro_evs, headlines = await asyncio.gather(
                    _fetch_macro_next_days(today, days=3),
                    _fetch_spanish_headlines(),
                )
            else:
                logger.info("Pre-mercado: usando Yahoo Finance (IB no disponible)")
                futures, vix, macro_evs, headlines = await asyncio.gather(
                    _fetch_futures(),
                    _fetch_vix(),
                    _fetch_macro_next_days(today, days=3),
                    _fetch_spanish_headlines(),
                    return_exceptions=False,
                )

            narrative = _build_premarket_narrative(futures, vix, macro_evs, headlines, today)

            # Bloque de futuros tabulado (igual que el bloque Mercados del cierre)
            fut_lines = []
            fut_display = [
                ("S&P 500", futures.get("S&P 500", {})),
                ("Nasdaq",  futures.get("Nasdaq 100", {})),
            ]
            for name, d in fut_display:
                if d and d.get("price"):
                    arrow = "▲" if d["chg_pct"] >= 0 else "▼"
                    sign  = "+" if d["chg_pct"] >= 0 else ""
                    fut_lines.append(
                        f"`{name:<8}` {d['price']:>10,.2f}   {arrow} `{sign}{d['chg_pct']:.2f}%`"
                    )
            if vix:
                fut_lines.append(f"`VIX     ` {vix:>10.2f}")
            fut_text = "\n".join(fut_lines) or "_Sin datos de futuros_"

            embed = {
                "author":      {"name": f"📰  RESUMEN PRE-MERCADO  —  {fecha}"},
                "description": narrative,
                "color":       0xF39C12,
                "fields": [
                    {"name": "📊  Futuros", "value": fut_text, "inline": False},
                ],
                "footer":      {"text": "Apertura NYSE en ~20 min  ·  09:30 ET"},
            }
            target = self.premarket_webhook or self.webhook_url
            if self.approver:
                await self.approver.post_for_review(
                    embeds=[embed],
                    report_type="premarket",
                    publish_webhook=target,
                    header="📰 **PRE-MERCADO listo para revisión** — elige qué hacer:",
                )
                logger.info("Reporte pre-mercado enviado a revisión")
                if self.log_channel:
                    await self.log_channel.send_info("📰 Reporte pre-mercado listo para revisión")
            else:
                await _post_webhook(target, {"embeds": [embed]})
                logger.info("Reporte pre-mercado enviado")
                if self.log_channel:
                    await self.log_channel.send_info("📰 Reporte pre-mercado publicado correctamente")
        except Exception as e:
            logger.error(f"Error en send_premarket: {e}")
            if self.log_channel:
                await self.log_channel.send_error(f"No se pudo publicar el reporte pre-mercado: {e}")

    # ── Reporte post-mercado ───────────────────────────────────

    async def send_postmarket(self) -> None:
        try:
            fecha = _fmt_date_es(datetime.now(ET_ZONE).date())

            # Datos de cierre: Yahoo Finance primero (chartPreviousClose → % cambio fiable),
            # IB como fallback (ticker.close puede ser un cierre antiguo → % incorrecto).
            mkt = await _fetch_market_data()
            if mkt:
                logger.info(f"Post-mercado: datos Yahoo ({list(mkt.keys())})")
            else:
                logger.warning("Post-mercado: Yahoo sin datos, intentando IB Gateway")
                if self.ib and self.ib.isConnected():
                    mkt = await _fetch_ib_quotes(self.ib, spot_sp500=True, frozen=True)
                    if mkt:
                        logger.info(f"Post-mercado: datos IB ({list(mkt.keys())})")
                    else:
                        logger.warning("Post-mercado: sin datos de mercado disponibles")

            # Bloque mercados (2-3 líneas)
            mkt_lines = []
            for name, d in mkt.items():
                arrow = "▲" if d["chg_pct"] >= 0 else "▼"
                sign  = "+" if d["chg_pct"] >= 0 else ""
                mkt_lines.append(
                    f"`{name:<8}` {d['price']:>10,.2f}   {arrow} `{sign}{d['chg_pct']:.2f}%`"
                )
            mkt_text = "\n".join(mkt_lines) or "_Sin datos de mercado_"

            # Filtrar por la fecha de hoy — garantía adicional contra duplicados
            # acumulados de días anteriores por reinicios del bot
            today_iso    = date.today().isoformat()
            opens_today  = [o for o in self._opens  if o.get("date", today_iso) == today_iso]
            rolls_today  = [r for r in self._rolls  if r.get("date", today_iso) == today_iso]
            closes_today = [c for c in self._closes if c.get("date", today_iso) == today_iso]

            # Bloque operaciones del día
            ops_parts = []
            if opens_today:
                rows = "\n".join(f"  **{o['symbol']}**  ·  _{o['account']}_" for o in opens_today)
                ops_parts.append(f"🟢  **APERTURAS**\n{rows}")
            if rolls_today:
                rows = "\n".join(f"  **{r['symbol']}**  ·  _{r['account']}_" for r in rolls_today)
                ops_parts.append(f"🔄  **ROLLS**\n{rows}")
            if closes_today:
                rows = "\n".join(f"  **{c['symbol']}**  ·  _{c['account']}_" for c in closes_today)
                ops_parts.append(f"🔴  **CIERRES**\n{rows}")
            ops_text = "\n\n".join(ops_parts) if ops_parts else "_Sin operaciones hoy_"

            # Bloque financiero — prima neta (spreads agrupados)
            net_prem = self._prem_collected - self._prem_paid
            fin_lines = []
            if abs(net_prem) > 0.01:
                if net_prem > 0:
                    fin_lines.append(f"💵  Prima neta cobrada:  `{_fmt_money(net_prem, sign=True)}`")
                else:
                    fin_lines.append(f"💸  Prima neta pagada:   `{_fmt_money(net_prem, sign=True)}`")
            if abs(self._pnl_closed) > 0.01:
                fin_lines.append(f"🏆  P&L cierres:      `{_fmt_money(self._pnl_closed, sign=True)}`")
            fin_text = "\n".join(fin_lines) if fin_lines else "_Sin actividad financiera hoy_"

            intro = _build_postmarket_intro(mkt)

            embed = {
                "author":      {"name": f"📊  CIERRE DE MERCADO  —  {fecha}"},
                "description": intro,
                "color":       0x2980B9,
                "fields": [
                    {"name": "📈  Mercados",            "value": mkt_text,  "inline": False},
                    {"name": "📋  Operaciones del día", "value": ops_text,  "inline": False},
                    {"name": "💰  Resumen financiero",  "value": fin_text,  "inline": False},
                ],
                "footer": {"text": "NYSE cerrado  ·  16:00 ET"},
            }
            if self.approver:
                await self.approver.post_for_review(
                    embeds=[embed],
                    report_type="postmarket",
                    publish_webhook=self.premarket_webhook or self.webhook_url,
                    header="📊 **CIERRE DE MERCADO listo para revisión** — elige qué hacer:",
                )
                logger.info("Reporte post-mercado enviado a revisión")
                if self.log_channel:
                    await self.log_channel.send_info("📊 Reporte post-mercado listo para revisión")
            else:
                await _post_webhook(self.webhook_url, {"embeds": [embed]})
                logger.info("Reporte post-mercado enviado")
                if self.log_channel:
                    await self.log_channel.send_info("📊 Reporte post-mercado publicado correctamente")
        except Exception as e:
            logger.error(f"Error en send_postmarket: {e}")
            if self.log_channel:
                await self.log_channel.send_error(f"No se pudo publicar el reporte post-mercado: {e}")

    # ── Social media ──────────────────────────────────────────

    async def _post_to_social(self, flag: str, image_url: Optional[str], text: str) -> None:
        """Publica en X, Instagram y/o Facebook según los flags del config."""
        fb_cfg = self.social_cfg.get("facebook", {})
        ig_cfg = self.social_cfg.get("instagram", {})
        tw_cfg = self.social_cfg.get("twitter", {})

        # Twitter / X
        if tw_cfg.get(flag) and self.twitter_poster:
            try:
                ok = await self.twitter_poster.post(text)
                logger.info(f"Twitter {flag}: {'✅' if ok else '❌'}")
            except Exception as e:
                logger.error(f"Twitter {flag} error: {e}")

        # Instagram
        if ig_cfg.get(flag) and self.instagram_poster:
            try:
                if image_url:
                    ok = await self.instagram_poster.post_image(image_url, text)
                else:
                    ok = await self.instagram_poster.post_text(text)
                logger.info(f"Instagram {flag}: {'✅' if ok else '❌'}")
            except Exception as e:
                logger.error(f"Instagram {flag} error: {e}")

        # Facebook
        if fb_cfg.get(flag) and self.facebook_poster:
            try:
                if image_url:
                    ok = await self.facebook_poster.post_image(image_url, text)
                else:
                    ok = await self.facebook_poster.post_text(text)
                logger.info(f"Facebook {flag}: {'✅' if ok else '❌'}")
            except Exception as e:
                logger.error(f"Facebook {flag} error: {e}")

    # ── Utilidades internas ────────────────────────────────────

    def _reset_day(self, new_day: bool = False) -> None:
        """
        Reinicia el estado en memoria.
        new_day=True: también borra el archivo de estado en disco
                      (solo al hacer la transición real de día en el scheduler).
        new_day=False: solo limpia memoria (llamada desde __init__ al arrancar).
        """
        self._opens:      List[dict] = []
        self._rolls:      List[dict] = []
        self._closes:     List[dict] = []
        self._seen_keys:  set        = set()   # deduplicación de operaciones
        self._prem_collected = 0.0
        self._prem_paid      = 0.0
        self._pnl_closed     = 0.0
        if new_day:
            # Borrar estado en disco al cambiar de día
            try:
                if os.path.exists(self._STATE_FILE):
                    os.remove(self._STATE_FILE)
            except Exception:
                pass


# ── Helpers de módulo ─────────────────────────────────────────

async def _sleep_until(dt: datetime) -> None:
    secs = (dt - datetime.now(dt.tzinfo)).total_seconds()
    if secs > 0:
        await asyncio.sleep(secs)


def _next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


async def _yf_quote_async(sym: str, live: bool = False) -> dict:
    """
    Fetch directo a la API de Yahoo Finance (chart v8).

    live=False (post-mercado, default):
        Usa el array OHLCV diario (última vela completa) para el cierre OFICIAL
        de la sesión regular. Más preciso que regularMarketPrice.

    live=True (pre-mercado, futuros en tiempo real):
        Usa regularMarketPrice (precio actual de mercado) vs chartPreviousClose
        (cierre de ayer). Correcto para futuros que cotizan casi 24h.
    """
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
        f"?interval=1d&range=5d"
    )
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(headers=_BROWSER_HEADERS, timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.debug(f"Yahoo API {sym}: HTTP {resp.status}")
                    return {"price": 0.0, "chg_pct": 0.0, "prev": 0.0}
                data = await resp.json(content_type=None)

        res  = data.get("chart", {}).get("result", [{}])[0]
        meta = res.get("meta", {})

        if live:
            # ── Modo live (pre-mercado / futuros) ───────────────────
            # regularMarketPrice = precio actual en tiempo real del futuro.
            # chartPreviousClose = cierre oficial de la sesión anterior.
            # Esto da el % de cambio real respecto al cierre de ayer.
            price = float(meta.get("regularMarketPrice") or 0)
            prev  = float(
                meta.get("chartPreviousClose")
                or meta.get("regularMarketPreviousClose")
                or 0
            )
        else:
            # ── Modo close (post-mercado) ────────────────────────────
            # Usamos el array OHLCV diario (última vela completa) para el
            # cierre OFICIAL de la sesión regular.
            # Filtramos None (Yahoo rellena con null las barras incompletas)
            closes = [
                c for c in (res.get("indicators", {})
                               .get("quote", [{}])[0]
                               .get("close", []))
                if c is not None
            ]

            if len(closes) >= 2:
                price = float(closes[-1])
                prev  = float(closes[-2])
            elif len(closes) == 1:
                price = float(closes[0])
                prev  = float(
                    meta.get("chartPreviousClose")
                    or meta.get("regularMarketPreviousClose")
                    or 0
                )
            else:
                price = float(meta.get("regularMarketPrice") or 0)
                prev  = float(
                    meta.get("chartPreviousClose")
                    or meta.get("regularMarketPreviousClose")
                    or 0
                )

        chg = ((price - prev) / prev * 100) if prev else 0.0
        logger.debug(f"Yahoo {sym} (live={live}): price={price:.2f}  prev={prev:.2f}  chg={chg:+.2f}%")
        return {"price": price, "chg_pct": chg, "prev": prev}

    except Exception as e:
        logger.debug(f"Yahoo API {sym}: {e}")
        return {"price": 0.0, "chg_pct": 0.0, "prev": 0.0}


async def _fetch_futures() -> Dict[str, dict]:
    """
    Precios de futuros en tiempo real (para pre-mercado).
    Usa live=True para obtener regularMarketPrice vs cierre de ayer.
    """
    result: Dict[str, dict] = {}
    tasks = {
        name: _yf_quote_async(sym, live=True)
        for name, sym in _FUTURES_TICKERS.items()
    }
    for name, coro in tasks.items():
        data = await coro
        if data["price"]:
            result[name] = data
    logger.debug(f"Futuros: { {k: (v['price'], v['chg_pct']) for k, v in result.items()} }")
    return result


async def _fetch_vix() -> float:
    try:
        data = await _yf_quote_async("%5EVIX", live=True)
        return data["price"]
    except Exception as e:
        logger.debug(f"VIX: {e}")
    return 0.0


async def _fetch_macro_next_days(today: date, days: int = 3) -> List[dict]:
    """Eventos de alto impacto desde hoy hasta 'days' días vista (ForexFactory)."""
    url     = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    events = await resp.json(content_type=None)
                    result = []
                    for ev in events:
                        if ev.get("impact") != "High":
                            continue
                        if ev.get("currency") not in ("USD", "EUR", "GBP", "JPY"):
                            continue
                        ev_date_str = ev.get("date", "")
                        try:
                            if len(ev_date_str) == 10 and ev_date_str[2] == "-":
                                ev_date = datetime.strptime(ev_date_str, "%m-%d-%Y").date()
                            else:
                                ev_date = datetime.strptime(ev_date_str[:10], "%Y-%m-%d").date()
                            delta = (ev_date - today).days
                            if 0 <= delta <= days:
                                result.append({**ev, "_delta": delta, "_date": ev_date})
                        except Exception:
                            pass
                    return result
    except Exception as e:
        logger.debug(f"Macro next days: {e}")
    return []


async def _fetch_spanish_headlines() -> List[str]:
    """Titulares financieros en español de prensa española."""
    headlines: List[str] = []
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
        for url in _SPANISH_NEWS_FEEDS:
            try:
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text()
                    root = ET.fromstring(text)
                    for item in root.iter("item"):
                        title = (item.findtext("title") or "").strip()
                        if title and title not in headlines and len(title) > 20:
                            headlines.append(title)
                        if len(headlines) >= 3:
                            break
            except Exception as e:
                logger.debug(f"Spanish RSS {url}: {e}")
            if len(headlines) >= 3:
                break
    return headlines[:3]


def _build_postmarket_intro(mkt: dict) -> str:
    """Saludo + 2-3 líneas sobre la jornada: dirección del mercado y contexto VIX."""
    sp   = mkt.get("S&P 500", {})
    nq   = mkt.get("Nasdaq",  {})
    vix  = mkt.get("VIX",     {})

    sp_chg  = sp.get("chg_pct", 0.0)
    nq_chg  = nq.get("chg_pct", 0.0)
    vix_val = vix.get("price", 20.0)

    greeting = "Buenas noches @everyone 👋"

    # Dirección del mercado
    if sp_chg > 1.0:
        direction = (
            f"Jornada muy positiva para los mercados americanos, con el S&P 500 cerrando "
            f"con una subida del {sp_chg:.2f}%"
        )
        if nq_chg > 0:
            direction += f" y el Nasdaq avanzando un {nq_chg:.2f}%"
        direction += "."
    elif sp_chg > 0.2:
        direction = (
            f"Sesión moderadamente alcista, con el S&P 500 cerrando al alza un {sp_chg:.2f}%"
        )
        if nq_chg > 0:
            direction += f" y el Nasdaq sumando un {nq_chg:.2f}%"
        direction += "."
    elif sp_chg > -0.2:
        direction = (
            f"Jornada sin dirección clara, con el S&P 500 cerrando prácticamente plano "
            f"({sp_chg:+.2f}%). El mercado no encontró catalizadores suficientes para "
            f"definir tendencia."
        )
    elif sp_chg > -1.0:
        direction = (
            f"Sesión bajista, con el S&P 500 cediendo un {abs(sp_chg):.2f}%"
        )
        if nq_chg < 0:
            direction += f" y el Nasdaq retrocediendo un {abs(nq_chg):.2f}%"
        direction += "."
    else:
        direction = (
            f"Jornada dura para los mercados americanos, con el S&P 500 cayendo "
            f"un {abs(sp_chg):.2f}%"
        )
        if nq_chg < 0:
            direction += f" y el Nasdaq perdiendo un {abs(nq_chg):.2f}%"
        direction += "."

    # Contexto VIX (proxy de eventos/noticias)
    if vix_val >= 30:
        vix_note = (
            f" El VIX cerró en {vix_val:.1f}, señalando alta volatilidad — "
            f"probablemente impulsado por algún evento o noticia relevante que generó "
            f"movimientos bruscos durante la sesión."
        )
    elif vix_val >= 22:
        vix_note = (
            f" El VIX en {vix_val:.1f} refleja un entorno de volatilidad elevada, "
            f"posiblemente por incertidumbre macro o noticias de impacto."
        )
    elif vix_val >= 16:
        vix_note = (
            f" VIX en {vix_val:.1f}: volatilidad moderada, sin grandes sobresaltos "
            f"ni noticias disruptivas que alteraran la tendencia principal."
        )
    else:
        vix_note = (
            f" VIX en {vix_val:.1f}: mercado tranquilo, sin eventos relevantes que "
            f"cambiaran el sesgo de la jornada."
        )

    direction += vix_note

    # Frase de operaciones alineadas con la tendencia
    if abs(sp_chg) < 0.2:
        ops_note = "Hemos gestionado las posiciones existentes adaptándonos al movimiento lateral del mercado."
    elif sp_chg > 0:
        ops_note = "Hemos operado aprovechando el sesgo alcista de la jornada."
    else:
        ops_note = "Hemos operado con cautela, adaptando las posiciones al sesgo bajista de la sesión."

    return f"{greeting}\n\n{direction} {ops_note}"


def _build_premarket_narrative(
    futures: Dict[str, dict],
    vix: float,
    macro_evs: List[dict],
    headlines: List[str],
    today: date,
) -> str:
    parts: List[str] = ["Buenas tardes @everyone 👋"]

    # ── Bloque 1: tono de mercado (futuros + VIX) ─────────────
    sp  = futures.get("S&P 500", {})
    nq  = futures.get("Nasdaq 100", {})
    sp_chg   = sp.get("chg_pct", 0.0)
    nq_chg   = nq.get("chg_pct", 0.0)
    sp_price = sp.get("price", 0.0)

    price_str = f" — S&P 500 en **{sp_price:,.0f}** puntos" if sp_price else ""

    if sp_chg > 0.4:
        tone = f"Los futuros apuntan a una **apertura alcista**{price_str} ({sp_chg:+.2f}%)"
        if nq_chg > 0:
            tone += f", con el Nasdaq también en verde ({nq_chg:+.2f}%)"
        tone += "."
    elif sp_chg < -0.4:
        tone = f"Los futuros anticipan una **apertura a la baja**{price_str} ({sp_chg:+.2f}%)"
        if nq_chg < 0:
            tone += f", con el Nasdaq también en rojo ({nq_chg:+.2f}%)"
        tone += "."
    else:
        tone = f"Los futuros apuntan a una **apertura plana**{price_str} ({sp_chg:+.2f}%), sin dirección clara de cara a la apertura."

    if vix > 0:
        if vix >= 30:
            tone += f" El VIX en **{vix:.1f}** señala alta volatilidad — jornada potencialmente movida."
        elif vix >= 22:
            tone += f" VIX en {vix:.1f}: volatilidad elevada, las primas de opciones están altas."
        elif vix >= 16:
            tone += f" VIX en {vix:.1f}: volatilidad moderada."
        else:
            tone += f" VIX en {vix:.1f}: mercado tranquilo."

    parts.append(tone)

    # ── Bloque 2: macro próximos 3 días ───────────────────────
    day_label = {0: "**hoy**", 1: "**mañana**", 2: "**pasado mañana**", 3: "**en 3 días**"}
    today_evs    = [ev for ev in macro_evs if ev["_delta"] == 0]
    upcoming_evs = [ev for ev in macro_evs if ev["_delta"] > 0]

    if today_evs:
        names = " y ".join(f"**{e.get('title', '')}**" for e in today_evs[:2])
        macro_text = f"Hoy se publican datos importantes: {names}."
        if upcoming_evs:
            up_parts = [
                f"{day_label.get(e['_delta'], '')} **{e.get('title', '')}**"
                for e in upcoming_evs[:2]
            ]
            macro_text += f" También a seguir: {', '.join(up_parts)}."
    elif upcoming_evs:
        up_parts = [
            f"{day_label.get(e['_delta'], '')} **{e.get('title', '')}**"
            for e in upcoming_evs[:3]
        ]
        macro_text = f"Hoy sin referencias macro relevantes. Atentos a los próximos días: {', '.join(up_parts)}."
    else:
        macro_text = "Sin eventos macro de alto impacto en los próximos días — la sesión seguirá impulsos técnicos y noticias puntuales."

    parts.append(macro_text)

    # ── Bloque 3: titular relevante (filtrado de política española) ──
    filtered = _filter_headlines(headlines)
    if filtered:
        parts.append(f"_{filtered[0]}_")

    # ── Cierre ────────────────────────────────────────────────
    parts.append("Nos vemos en breve operando en los canales de operaciones del Discord 📊")

    return "\n\n".join(parts)


# Un titular solo se incluye si contiene al menos una de estas palabras financieras
_FINANCIAL_KEYWORDS = [
    "bolsa", "ibex", "mercado", "mercados", "nasdaq", "dow jones", "s&p", "sp500",
    "inflación", "inflacion", "tipos de interés", "tipos de interes", "tipo de interés",
    "fed", "reserva federal", "bce", "banco central", "boe",
    "aranceles", "trump", "china", "guerra comercial", "comercio",
    "wall street", "wall st", "futuros",
    "petróleo", "petroleo", "oro ", "barril",
    "dólar", "dolar", "euro ", "libra ", "yen ",
    "deuda", "bono ", "bonos", "prima de riesgo",
    "pib", "crecimiento económico", "recesión", "recesion",
    "empleo", "desempleo", "paro ", "nóminas", "nominas",
    "resultados empresariales", "beneficios", "ingresos",
    "economía", "economia", "exportaciones", "importaciones",
    "inversión", "inversion", "dividendo",
    "banco ", "sector bancario", "financiero",
    "nvidia", "apple", "microsoft", "amazon", "alphabet", "meta ", "tesla",
]

def _filter_headlines(headlines: List[str]) -> List[str]:
    """
    Solo acepta titulares que contengan al menos una palabra financiera/económica.
    Cualquier titular sin contexto de mercado (política, deporte, cultura) queda fuera.
    """
    result = []
    for h in headlines:
        h_lower = h.lower()
        if any(kw in h_lower for kw in _FINANCIAL_KEYWORDS):
            result.append(h)
    return result


async def _fetch_news() -> List[str]:
    """Mantenido por compatibilidad — ya no se usa en send_premarket."""
    return []


async def _fetch_ib_quotes(ib, spot_sp500: bool = False, frozen: bool = False) -> Dict[str, dict]:
    """
    Cotizaciones directas de IB Gateway (sin suscripción RT, datos retrasados 15 min).
    spot_sp500=False (pre-mercado):  S&P 500 = ES futuros (indica dirección apertura)
    spot_sp500=True  (post-mercado): S&P 500 = SPX índice spot (cierre oficial del índice)
    Nasdaq siempre = NQ futuros (~29.000), VIX = índice spot CBOE.

    frozen=False → tipo 3 (Delayed streaming): precio actual con 15 min de retraso.
                   CORRECTO para pre-mercado: refleja el precio pre-market de los futuros ES/NQ.
    frozen=True  → tipo 4 (Delayed-Frozen): precio congelado al cierre de la sesión regular.
                   CORRECTO para post-mercado: devuelve el cierre oficial del índice/futuro.

    ¡IMPORTANTE! Tipo 4 en pre-mercado devuelve el cierre de AYER, no el precio actual.
    Por eso el informe mostraba un valor ~20 puntos distinto al real: era el cierre anterior.
    """
    import math
    from ib_insync import ContFuture, Index

    sp_contract = Index("SPX", "CBOE", currency="USD") if spot_sp500 else ContFuture("ES", "CME", currency="USD")
    items = [
        ("S&P 500", sp_contract),
        ("Nasdaq",  ContFuture("NQ", "CME", currency="USD")),
        ("VIX",     Index("VIX", "CBOE", currency="USD")),
    ]
    result: Dict[str, dict] = {}
    try:
        names     = [n for n, _ in items]
        contracts = [c for _, c in items]
        # Tipo 3 = Delayed streaming (precio actual -15 min, ideal para pre-mercado)
        # Tipo 4 = Delayed-Frozen (precio del último cierre, ideal para post-mercado)
        ib.reqMarketDataType(4 if frozen else 3)
        qualified = await asyncio.wait_for(
            ib.qualifyContractsAsync(*contracts), timeout=10
        )
        if not qualified:
            logger.warning("IB market data: contratos no calificados")
            return result
        tickers = await asyncio.wait_for(
            ib.reqTickersAsync(*qualified), timeout=10
        )
        for name, ticker in zip(names, tickers):
            price = ticker.marketPrice()
            close = ticker.close or 0.0
            if math.isnan(price): price = 0.0
            if math.isnan(close): close = 0.0
            if not price:
                logger.debug(f"IB {name}: sin precio")
                continue
            chg = ((price - close) / close * 100) if close else 0.0
            result[name] = {"price": price, "chg_pct": chg, "prev": close}
            logger.debug(f"IB {name}: price={price:.2f}  prev={close:.2f}  chg={chg:+.2f}%")
    except asyncio.TimeoutError:
        logger.warning("IB market data: timeout (>10s)")
    except Exception as exc:
        logger.warning(f"IB market data error: {exc}")
    finally:
        # Restaurar modo normal (no afectar al resto de operaciones del bot)
        try:
            ib.reqMarketDataType(1)
        except Exception:
            pass
    return result


async def _fetch_market_data() -> Dict[str, dict]:
    """
    Obtiene datos de mercado para el informe post-mercado.
    - S&P 500: ^GSPC (spot, precio oficial de cierre del índice ~7.432)
    - Nasdaq:  NQ=F  (futuros Nasdaq 100 ~29.297, NO el Composite ^IXIC ~26.000)
    - VIX:     ^VIX  (spot)
    """
    result: Dict[str, dict] = {}

    # S&P 500: spot index — refleja el cierre oficial del índice
    sp_data = await _yf_quote_async("%5EGSPC")
    if sp_data["price"]:
        result["S&P 500"] = sp_data

    # Nasdaq 100: futuros NQ=F — precio ~29.000 que ve el usuario
    # (NO usar ^IXIC que es el Composite y cotiza ~26.000)
    nq_data = await _yf_quote_async("NQ=F")
    if nq_data["price"]:
        result["Nasdaq"] = nq_data

    # VIX: solo spot
    vix_data = await _yf_quote_async("%5EVIX")
    if vix_data["price"]:
        result["VIX"] = vix_data

    logger.debug(f"Mercados: { {k: (v['price'], v['chg_pct']) for k, v in result.items()} }")
    return result


async def _post_webhook(url: str, payload: dict) -> None:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                logger.error(f"Webhook error {resp.status}: {text}")


def _fmt_money(v: float, sign: bool = False) -> str:
    s = "+" if sign and v > 0 else ""
    return f"{s}${v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_date_es(d: date) -> str:
    meses = ["ene.", "feb.", "mar.", "abr.", "may.", "jun.",
             "jul.", "ago.", "sep.", "oct.", "nov.", "dic."]
    return f"{d.day} {meses[d.month - 1]} {d.year}"
