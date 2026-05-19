"""
Reportes diarios de mercado y operaciones enviados a Discord.
  - Pre-mercado:  09:10 ET (20 min antes apertura NYSE 09:30)
  - Post-mercado: 16:15 ET (15 min después cierre NYSE 16:00)
Solo días laborables (lunes–viernes).
"""

import asyncio
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
    def __init__(self, webhook_url: str):
        self.webhook_url        = webhook_url
        self.log_channel        = None   # asignado desde main.py
        self.premarket_webhook       = ""     # asignado desde main.py
        self.pnl_tracker        = None   # asignado desde main.py
        self.weekly_webhook          = ""     # asignado desde main.py
        self.portfolio_tracker       = None   # asignado desde main.py
        self.portfolio_webhook       = ""     # asignado desde main.py
        self.logbook_updater         = None   # asignado desde main.py
        self.logbook_export_webhook  = ""     # asignado desde main.py
        self.facebook_poster         = None   # asignado desde main.py
        self.instagram_poster        = None   # asignado desde main.py
        self.twitter_poster          = None   # asignado desde main.py
        self.social_cfg              = {}     # cfg completo para leer flags
        self._reset_day()

    # ── Registro de operaciones del día ───────────────────────

    def record_trade(self, event_type: str, strategy, metrics, account_name: str) -> None:
        prem   = metrics.net_premium_after_comm or 0.0
        entry  = {"symbol": strategy.underlying, "account": account_name}

        if event_type in (TradeEvent.OPEN, TradeEvent.ADD):
            self._opens.append(entry)
        elif event_type in (TradeEvent.CLOSE, TradeEvent.PARTIAL_CLOSE):
            self._closes.append(entry)
            self._pnl_closed += metrics.trade_result or 0.0

        self._prem_collected += max(prem, 0.0)
        self._prem_paid      += max(-prem, 0.0)

    def record_roll(self, open_strategy, open_metrics, account_name: str) -> None:
        prem = open_metrics.net_premium_after_comm or 0.0
        self._rolls.append({"symbol": open_strategy.underlying, "account": account_name})
        self._prem_collected += max(prem, 0.0)
        self._prem_paid      += max(-prem, 0.0)

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
                    self._reset_day()
                    await _sleep_until(datetime.combine(_next_trading_day(today), time(8, 0), tzinfo=ET_ZONE))

                elif now_et < post_dt:
                    await _sleep_until(post_dt)
                    await self.send_postmarket()
                    self._reset_day()
                    await _sleep_until(datetime.combine(_next_trading_day(today), time(8, 0), tzinfo=ET_ZONE))

                else:
                    # Ya pasó todo hoy
                    self._reset_day()
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
                if self.logbook_updater and self.logbook_export_webhook:
                    await self.logbook_updater.export_and_publish(self.logbook_export_webhook)
                    await self._post_to_social("post_logbook_export", None,
                        "📋 Log de operaciones semanal actualizado — MTO Opciones #opciones #trading")
                else:
                    logger.warning("Logbook export: logbook_updater o webhook no configurados")
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

            futures, vix, macro_evs, headlines = await asyncio.gather(
                _fetch_futures(),
                _fetch_vix(),
                _fetch_macro_next_days(today, days=3),
                _fetch_spanish_headlines(),
                return_exceptions=False,
            )

            narrative = _build_premarket_narrative(futures, vix, macro_evs, headlines, today)

            embed = {
                "author":      {"name": f"📰  RESUMEN PRE-MERCADO  —  {fecha}"},
                "description": narrative,
                "color":       0xF39C12,
                "footer":      {"text": "Apertura NYSE en ~20 min  ·  09:30 ET"},
            }
            target = self.premarket_webhook or self.webhook_url
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
            mkt   = await _fetch_market_data()

            # Bloque mercados (2-3 líneas)
            mkt_lines = []
            for name, d in mkt.items():
                arrow = "▲" if d["chg_pct"] >= 0 else "▼"
                sign  = "+" if d["chg_pct"] >= 0 else ""
                mkt_lines.append(
                    f"`{name:<8}` {d['price']:>10,.2f}   {arrow} `{sign}{d['chg_pct']:.2f}%`"
                )
            mkt_text = "\n".join(mkt_lines) or "_Sin datos de mercado_"

            # Bloque operaciones del día
            ops_parts = []
            if self._opens:
                rows = "\n".join(f"  **{o['symbol']}**  ·  _{o['account']}_" for o in self._opens)
                ops_parts.append(f"🟢  **APERTURAS**\n{rows}")
            if self._rolls:
                rows = "\n".join(f"  **{r['symbol']}**  ·  _{r['account']}_" for r in self._rolls)
                ops_parts.append(f"🔄  **ROLLS**\n{rows}")
            if self._closes:
                rows = "\n".join(f"  **{c['symbol']}**  ·  _{c['account']}_" for c in self._closes)
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

    def _reset_day(self) -> None:
        self._opens:  List[dict] = []
        self._rolls:  List[dict] = []
        self._closes: List[dict] = []
        self._prem_collected = 0.0
        self._prem_paid      = 0.0
        self._pnl_closed     = 0.0


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


async def _fetch_one_future(session: aiohttp.ClientSession, name: str, sym: str) -> tuple:
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
            f"?interval=1d&range=5d"
        )
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                return name, None
            data   = await resp.json()
            res    = data["chart"]["result"][0]
            meta   = res["meta"]
            closes = res.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            closes = [c for c in closes if c is not None]

            price = float(meta.get("regularMarketPrice", closes[-1] if closes else 0))
            # Prioridad: regularMarketChangePercent del meta (más fiable, siempre vs previousClose)
            # Fallback: calcular manualmente con previousClose explícito del meta
            chg_meta = meta.get("regularMarketChangePercent")
            if chg_meta is not None:
                chg = float(chg_meta)
            else:
                prev = float(meta.get("previousClose", 0) or 0)
                chg  = ((price - prev) / prev * 100) if prev else 0.0
            return name, {"price": price, "chg_pct": chg}
    except Exception as e:
        logger.debug(f"Futures {name}: {e}")
        return name, None


async def _fetch_futures() -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
        tasks = [
            _fetch_one_future(session, name, sym)
            for name, sym in _FUTURES_TICKERS.items()
        ]
        for name, data in await asyncio.gather(*tasks):
            if data is not None:
                result[name] = data
    return result


async def _fetch_vix() -> float:
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=2d"
        async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    meta = data["chart"]["result"][0]["meta"]
                    return float(meta.get("regularMarketPrice", 20))
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


async def _fetch_one_market(session: aiohttp.ClientSession, name: str, sym: str) -> tuple:
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=5d"
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                return name, None
            data   = await resp.json()
            res    = data["chart"]["result"][0]
            meta   = res["meta"]
            closes = res.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            closes = [c for c in closes if c is not None]

            price = float(meta.get("regularMarketPrice", closes[-1] if closes else 0))
            # Prioridad: regularMarketChangePercent del meta (siempre vs previousClose real)
            # Fallback: calcular manualmente con previousClose explícito del meta
            chg_meta = meta.get("regularMarketChangePercent")
            if chg_meta is not None:
                chg = float(chg_meta)
            else:
                prev = float(meta.get("previousClose", 0) or 0)
                chg  = ((price - prev) / prev * 100) if prev else 0.0
            return name, {"price": price, "chg_pct": chg}
    except Exception as e:
        logger.debug(f"Market data {name}: {e}")
        return name, None


async def _fetch_market_data() -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
        tasks = [
            _fetch_one_market(session, name, sym)
            for name, sym in _MARKET_TICKERS.items()
        ]
        for name, data in await asyncio.gather(*tasks):
            if data is not None:
                result[name] = data
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
