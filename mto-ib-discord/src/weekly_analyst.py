"""
ANÁLISIS SEMANAL — publicado cada lunes a las 12:15 hora Madrid.
Canal: resumen-diario (daily_report_webhook).

Secciones:
  1. ¿Qué pasó la semana pasada? (6 activos — redactado editorial)
  2. Fear & Greed Index (CNN)
  3. Datos macro de la semana (ForexFactory)
  4. Perspectivas de la semana (síntesis generada)
  5. Earnings de la semana (Yahoo Finance)
"""

import asyncio
from datetime import datetime, timedelta, date, time
from typing import List, Dict, Optional
from zoneinfo import ZoneInfo
import aiohttp
from loguru import logger


MADRID_ZONE = ZoneInfo("Europe/Madrid")
_FIRE_TIME  = time(12, 15)   # lunes 12:15 Madrid

_WEEKLY_TICKERS: Dict[str, str] = {
    "S&P 500":      "%5EGSPC",
    "Nasdaq 100":   "%5ENDX",
    "Russell 2000": "%5ERUT",
    "Bitcoin":      "BTC-USD",
    "Oro":          "GC=F",
    "WTI":          "CL=F",
}
_VIX_SYM = "%5EVIX"

_TICKER_EMOJIS: Dict[str, str] = {
    "S&P 500":      "📈",
    "Nasdaq 100":   "💻",
    "Russell 2000": "📊",
    "Bitcoin":      "₿",
    "Oro":          "🥇",
    "WTI":          "🛢️",
}

# User-Agent que no bloquea CNN (418 con UA genérico)
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.cnn.com/markets/fear-and-greed",
    "Origin":          "https://www.cnn.com",
}

_MACRO_CONTEXT: Dict[str, str] = {
    "CPI":           "Inflación al consumo — el dato más vigilado por la Fed para calibrar los tipos.",
    "PPI":           "Inflación mayorista, adelanta la presión sobre el IPC.",
    "NFP":           "Dato de empleo más importante: mueve fuerte al dólar y a los bonos.",
    "Non-Farm":      "Dato de empleo más importante: mueve fuerte al dólar y a los bonos.",
    "GDP":           "Crecimiento económico trimestral — confirma o cuestiona el aterrizaje suave.",
    "FOMC":          "Reunión de la Fed: posible cambio de tipos o señales sobre futuros movimientos.",
    "Fed":           "Comunicado de la Fed — los mercados analizan cada palabra.",
    "Retail Sales":  "Consumo interno: un dato sólido refuerza la resistencia de la economía.",
    "PMI":           "Actividad empresarial: >50 expansión, <50 contracción.",
    "ISM":           "Encuesta clave que adelanta la dirección de la economía.",
    "Unemployment":  "Tasa de paro — junto al NFP, la referencia laboral más seguida.",
    "Jobless":       "Solicitudes de desempleo semanales, termómetro laboral en tiempo real.",
    "PCE":           "Inflación preferida de la Fed — un PCE alto complica las bajadas de tipos.",
    "Durable":       "Pedidos duraderos: mide la confianza empresarial en inversión a largo plazo.",
    "Housing":       "Inmobiliario, muy sensible al nivel de tipos de interés.",
    "Consumer Conf": "Confianza del consumidor, anticipa el gasto de los hogares.",
    "ECB":           "Banco Central Europeo — impacto directo en el euro y activos europeos.",
    "BOE":           "Banco de Inglaterra — clave para la libra y mercados UK.",
    "BOJ":           "Banco de Japón — afecta al yen y a los carry trades globales.",
    "Inflation":     "Dato de inflación que condiciona la política monetaria.",
}


# ── Clase principal ────────────────────────────────────────────

class WeeklyAnalyst:
    def __init__(self, daily_report_webhook: str, log_channel=None):
        self.webhook_url = daily_report_webhook
        self.log_channel = log_channel
        self.approver    = None   # asignado desde main.py si está activo

    async def start(self) -> None:
        asyncio.ensure_future(self._loop())
        logger.info("Weekly analyst iniciado (lunes 12:15 Madrid)")

    async def _loop(self) -> None:
        while True:
            try:
                now_mad     = datetime.now(MADRID_ZONE)
                today       = now_mad.date()
                days_to_mon = (0 - today.weekday()) % 7
                if days_to_mon == 0 and now_mad.time() >= _FIRE_TIME:
                    days_to_mon = 7
                next_mon = today + timedelta(days=days_to_mon)
                fire_dt  = datetime.combine(next_mon, _FIRE_TIME, tzinfo=MADRID_ZONE)
                logger.info(
                    f"Weekly analyst: próximo envío "
                    f"{fire_dt.strftime('%A %d/%m/%Y %H:%M')} Madrid"
                )
                await _sleep_until(fire_dt)
                await self.send_weekly_analysis()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error en weekly analyst loop: {e}")
                await asyncio.sleep(3600)

    async def send_weekly_analysis(self) -> None:
        now_mad  = datetime.now(MADRID_ZONE)
        today    = now_mad.date()
        # Header: semana ACTUAL (lunes → viernes de esta semana)
        this_fri = today + timedelta(days=4)
        week_str = (
            f"{today.day} al {this_fri.day} de "
            f"{_month_es(this_fri.month)} de {this_fri.year}"
        )
        # Para el log: semana pasada analizada
        last_fri = today - timedelta(days=3)
        last_mon = today - timedelta(days=7)
        try:
            market_data  = await _fetch_weekly_market_data()
            vix          = await _fetch_vix()
            fg_data      = await _fetch_fear_greed()
            macro_events = await _fetch_macro_calendar()
            earnings     = await _fetch_earnings_week(today)

            fg_score  = float(fg_data.get("score", 50))
            fg_rating = _normalize_rating(str(fg_data.get("rating", "Neutral")))

            # ── Embed 1: Mercados ──────────────────────────────────
            market_blocks: List[str] = []
            for name, d in market_data.items():
                emoji     = _TICKER_EMOJIS.get(name, "•")
                arrow     = "▲" if d["weekly_chg"] >= 0 else "▼"
                sign      = "+" if d["weekly_chg"] >= 0 else ""
                price_fmt = _fmt_price(name, d["price"])
                chg       = d["weekly_chg"]
                comment   = _market_narrative(name, chg, vix)
                market_blocks.append(
                    f"**{emoji} {name} — {price_fmt} — {arrow} {sign}{chg:.2f}%**\n"
                    f"{comment}"
                )

            market_text = "\n\n".join(market_blocks) or "_No se pudieron obtener datos de mercado._"

            embed1 = {
                "author": {
                    "name": f"📊  ANÁLISIS SEMANAL  |  Semana del {week_str}",
                },
                "description": (
                    "Buenos días @everyone 👋\n\n"
                    "Aquí tienes el análisis de lo que pasó la semana pasada "
                    "y los eventos más importantes de esta semana.\n\n"
                    "─────────────────────────\n"
                    "**¿Qué pasó la semana pasada?**\n"
                    "─────────────────────────\n\n"
                    + market_text
                ),
                "color": 0x2ECC71,
            }

            # ── Embed 2: Fear & Greed ──────────────────────────────
            fg_prev_wk   = float(fg_data.get("previous_1_week", fg_score))
            fg_delta     = fg_score - fg_prev_wk
            fg_sign      = "+" if fg_delta >= 0 else ""
            fg_spectrum  = _fg_spectrum(int(fg_score), fg_rating)
            fg_narrative = _fg_narrative(fg_score, fg_rating, fg_delta)
            fg_color     = _fg_color(int(fg_score))

            embed2 = {
                "title": "🧠  Fear & Greed Index",
                "description": (
                    f"{fg_spectrum}\n\n"
                    f"({fg_sign}{fg_delta:.1f} puntos respecto a la semana pasada)\n\n"
                    f"{fg_narrative}"
                ),
                "color": fg_color,
            }

            # ── Embed 3: Macro ─────────────────────────────────────
            macro_text = _format_macro_narrative(macro_events)
            embed3 = {
                "title": "📅  Datos macro de la semana",
                "description": macro_text,
                "color": 0x9B59B6,
                "footer": {"text": "Solo eventos de alto impacto  ·  Fuente: ForexFactory"},
            }

            # ── Embed 4: Perspectivas ──────────────────────────────
            perspectives = _generate_perspectives(
                market_data, fg_score, fg_rating, vix, macro_events, earnings
            )
            embed4 = {
                "title": "🔭  Perspectivas de la semana",
                "description": perspectives,
                "color": 0x1ABC9C,
            }

            # ── Embed 5: Earnings ──────────────────────────────────
            earnings_text = _format_earnings_narrative(earnings)
            embed5 = {
                "title": "🏦  Earnings destacados de la semana",
                "description": earnings_text,
                "color": 0xF39C12,
                "footer": {
                    "text": (
                        "Los resultados pueden generar picos de volatilidad. "
                        "Revisa tus posiciones en estos subyacentes."
                    )
                },
            }

            embeds = [embed1, embed2, embed3, embed4, embed5]
            if self.approver:
                # Publicar en canal de revisión con botones de aprobación
                await self.approver.post_for_review(
                    embeds=embeds,
                    report_type="weekly",
                    publish_webhook=self.webhook_url,
                    header=(
                        f"📊 **ANÁLISIS SEMANAL — semana del {week_str}**\n"
                        f"Revisa el informe y elige qué hacer con él:"
                    ),
                )
                logger.info("Análisis semanal enviado a revisión")
                if self.log_channel:
                    await self.log_channel.send_info(
                        f"📊 Análisis semanal listo para revisión — semana del {week_str}"
                    )
            else:
                # Sin approver: publicar directamente (comportamiento anterior)
                await _post_webhook(self.webhook_url, {"embeds": embeds})
                logger.info("Análisis semanal publicado correctamente")
                if self.log_channel:
                    await self.log_channel.send_info(
                        f"📊 Análisis semanal publicado — semana del {week_str}"
                    )

        except Exception as e:
            logger.error(f"Error en send_weekly_analysis: {e}")
            if self.log_channel:
                await self.log_channel.send_error(
                    f"Error al publicar análisis semanal: {e}"
                )


# ── Fetchers ──────────────────────────────────────────────────

async def _fetch_weekly_market_data() -> Dict[str, dict]:
    """
    Precio y variación semanal (viernes pasado vs viernes anterior).
    Usa datos diarios (interval=1d, range=1mo) y compara closes[-1] vs closes[-6]
    (5 sesiones de distancia = semana completa de trading).
    Evita el problema del bar incompleto de la semana actual en datos semanales.
    """
    result: Dict[str, dict] = {}
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
        for name, sym in _WEEKLY_TICKERS.items():
            try:
                url = (
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
                    f"?interval=1d&range=1mo"
                )
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status != 200:
                        continue
                    data   = await resp.json()
                    res    = data["chart"]["result"][0]
                    meta   = res["meta"]
                    closes = res.get("indicators", {}).get("quote", [{}])[0].get("close", [])
                    closes = [c for c in closes if c is not None]

                    price = float(meta.get("regularMarketPrice", closes[-1] if closes else 0))

                    # closes[-1] = viernes pasado  |  closes[-6] = viernes anterior (5 sesiones atrás)
                    if len(closes) >= 6:
                        last_fri  = closes[-1]
                        prev_fri  = closes[-6]
                        weekly_chg = (last_fri - prev_fri) / prev_fri * 100 if prev_fri else 0.0
                        price = last_fri   # precio de cierre de la semana completada
                    elif len(closes) >= 2:
                        weekly_chg = (closes[-1] - closes[-2]) / closes[-2] * 100 if closes[-2] else 0.0
                    else:
                        weekly_chg = 0.0

                    result[name] = {"price": price, "weekly_chg": weekly_chg}
            except Exception as e:
                logger.debug(f"Weekly market data {name}: {e}")
    return result


async def _fetch_vix() -> float:
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{_VIX_SYM}?interval=1d&range=2d"
        async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    meta = data["chart"]["result"][0]["meta"]
                    return float(meta.get("regularMarketPrice", 20))
    except Exception as e:
        logger.debug(f"VIX fetch: {e}")
    return 20.0


async def _fetch_fear_greed() -> dict:
    """
    CNN Fear & Greed Index.
    Requiere cabeceras de navegador real — con UA genérico devuelve HTTP 418.
    """
    url     = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    fg   = data.get("fear_and_greed", {})
                    return {
                        "score":           float(fg.get("score", 50)),
                        "rating":          str(fg.get("rating", "neutral")),
                        "previous_1_week": float(fg.get("previous_1_week", fg.get("score", 50))),
                    }
                logger.warning(f"Fear & Greed HTTP {resp.status}")
    except Exception as e:
        logger.debug(f"Fear & Greed: {e}")
    return {"score": 50, "rating": "neutral", "previous_1_week": 50}


async def _fetch_macro_calendar() -> List[dict]:
    url     = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    events = await resp.json(content_type=None)
                    return [
                        e for e in events
                        if e.get("impact") == "High"
                        and e.get("currency") in ("USD", "EUR", "GBP", "JPY")
                    ][:15]
    except Exception as e:
        logger.debug(f"Macro calendar: {e}")
    return []


async def _fetch_earnings_week(week_start: date) -> List[dict]:
    """
    Calendario de earnings desde la API pública de Nasdaq.
    Filtra compañías con market cap >= $10B y toma hasta 6 por día.
    """
    results: List[dict] = []
    timeout  = aiohttp.ClientTimeout(total=10)
    days = [week_start + timedelta(days=i) for i in range(5)]  # lun → vie

    async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
        for day in days:
            try:
                url = f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}"
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    rows = data.get("data", {}).get("rows", []) or []
                    count = 0
                    for row in rows:
                        if count >= 6:
                            break
                        mc = _parse_market_cap(row.get("marketCap", ""))
                        if mc < 10_000_000_000:   # solo empresas >$10B
                            continue
                        timing = row.get("time", "")
                        results.append({
                            "ticker":      row.get("symbol", ""),
                            "company":     row.get("name", row.get("symbol", "")),
                            "date":        day.isoformat(),
                            "time":        "🌅 Pre-market" if "pre" in timing else "🌙 After-hours",
                            "eps_est":     row.get("epsForecast", ""),
                            "market_cap":  mc,
                        })
                        count += 1
            except Exception as e:
                logger.debug(f"Earnings Nasdaq {day}: {e}")

    return results


def _parse_market_cap(mc_str: str) -> float:
    try:
        return float(mc_str.replace("$", "").replace(",", ""))
    except Exception:
        return 0.0


# ── Perspectivas de la semana ──────────────────────────────────

def _generate_perspectives(
    market_data: dict,
    fg_score: float,
    fg_rating: str,
    vix: float,
    macro_events: List[dict],
    earnings: List[dict],
) -> str:
    sp500  = market_data.get("S&P 500",      {})
    nasdaq = market_data.get("Nasdaq 100",   {})
    btc    = market_data.get("Bitcoin",      {})
    gold   = market_data.get("Oro",          {})

    sp_chg  = sp500.get("weekly_chg", 0)
    nas_chg = nasdaq.get("weekly_chg", 0)
    btc_chg = btc.get("weekly_chg", 0)
    gold_chg = gold.get("weekly_chg", 0)

    # ── Párrafo 1: contexto de mercado ────────────────────────
    avg_eq = (sp_chg + nas_chg) / 2

    if avg_eq > 3:
        p1 = (
            f"El mercado llega a esta semana con una inercia alcista muy fuerte tras subidas "
            f"del {sp_chg:.1f}% en el S&P 500 y del {nas_chg:.1f}% en el Nasdaq 100. "
            f"El 'momentum' favorece la continuidad al alza, aunque en estas zonas conviene "
            f"vigilar los niveles de resistencia y no perseguir el precio."
        )
    elif avg_eq > 1:
        p1 = (
            f"Semana sólida la pasada, con el S&P 500 avanzando un {sp_chg:.1f}% y el Nasdaq "
            f"un {nas_chg:.1f}%. El mercado mantiene el sesgo alcista de fondo y llega a esta "
            f"semana con buen tono. La tendencia es tu amiga — operar en su dirección."
        )
    elif avg_eq > -1:
        p1 = (
            f"Los índices americanos cerraron prácticamente planos la semana pasada "
            f"(S&P 500 {sp_chg:+.1f}%, Nasdaq {nas_chg:+.1f}%), lo que indica que el mercado "
            f"está en una fase de consolidación. Sin catalizadores claros, es probable que "
            f"el movimiento lateral continúe en los próximos días."
        )
    elif avg_eq > -3:
        p1 = (
            f"La semana pasada fue de corrección, con el S&P 500 cayendo un {abs(sp_chg):.1f}% "
            f"y el Nasdaq un {abs(nas_chg):.1f}%. El mercado llega a esta semana en busca de "
            f"estabilización — un rebote técnico es posible, pero no está garantizado. "
            f"Gestión del riesgo ante todo."
        )
    else:
        p1 = (
            f"Semana muy dura la pasada, con caídas del {abs(sp_chg):.1f}% en el S&P 500 y "
            f"del {abs(nas_chg):.1f}% en el Nasdaq. El mercado entra en esta semana en modo "
            f"defensivo. Los rebotes en mercados bajistas pueden ser violentos — precaución "
            f"con las posiciones cortas y atención a los soportes clave."
        )

    # Añadir contexto cripto/oro si hay movimiento relevante
    extra = []
    if abs(btc_chg) > 5:
        extra.append(
            f"Bitcoin {'subió' if btc_chg > 0 else 'cayó'} un {abs(btc_chg):.1f}% la semana pasada, "
            f"{'señal de apetito de riesgo que suele ser positiva para las acciones' if btc_chg > 0 else 'indicando aversión al riesgo en activos especulativos'}."
        )
    if gold_chg > 2:
        extra.append(
            f"El oro subió un {gold_chg:.1f}%, lo que refleja que parte del mercado "
            f"busca protección — una señal de cautela que no hay que ignorar."
        )
    if extra:
        p1 += " " + " ".join(extra)

    # ── Párrafo 2: catalizadores de la semana ─────────────────
    macro_titles = [
        ev.get("title", ev.get("event", "")) for ev in macro_events[:3]
    ]
    earn_names = [
        (e.get("company") or e.get("ticker", "")) for e in earnings[:3]
    ]

    if macro_titles and earn_names:
        p2 = (
            f"Esta semana hay catalizadores importantes en ambos frentes. "
            f"En macro, los datos más relevantes son **{macro_titles[0]}**"
            f"{f' y **{macro_titles[1]}**' if len(macro_titles) > 1 else ''}, "
            f"que podrían generar volatilidad en función de si los datos sorprenden al mercado. "
            f"En el frente empresarial, pendientes de los resultados de "
            f"**{earn_names[0]}**"
            f"{f' y **{earn_names[1]}**' if len(earn_names) > 1 else ''}"
            f", que pueden mover a sus sectores de referencia."
        )
    elif macro_titles:
        p2 = (
            f"La agenda macro de esta semana incluye datos relevantes, "
            f"con **{macro_titles[0]}**"
            f"{f' y **{macro_titles[1]}**' if len(macro_titles) > 1 else ''} "
            f"como referencias principales. Una lectura sorprendente podría "
            f"cambiar el tono del mercado en pocas horas."
        )
    elif earn_names:
        p2 = (
            f"La semana viene cargada de resultados empresariales. "
            f"Ojo especialmente con **{earn_names[0]}**"
            f"{f' y **{earn_names[1]}**' if len(earn_names) > 1 else ''}"
            f" — los earnings pueden doblar la volatilidad implícita de un día para otro."
        )
    else:
        p2 = (
            f"La semana no tiene grandes referencias macro ni earnings de primer nivel, "
            f"lo que puede traducirse en sesiones de menor volumen y movimientos más técnicos. "
            f"Buen entorno para gestionar posiciones existentes con tranquilidad."
        )

    # ── Párrafo 3: consejo para opciones ──────────────────────
    if fg_score >= 70 and vix < 16:
        p3 = (
            f"⚠️ **Para traders de opciones:** nos encontramos en un entorno exigente — "
            f"codicia elevada ({int(fg_score)}) y VIX bajo ({vix:.1f}). Las primas están "
            f"muy comprimidas, lo que reduce la compensación al vender volatilidad. "
            f"En estos mercados conviene reducir el tamaño de las posiciones, "
            f"ampliar los strikes de protección y no forzar entradas."
        )
    elif fg_score >= 55 and vix < 20:
        p3 = (
            f"📌 **Para traders de opciones:** el mercado está en zona de codicia con "
            f"volatilidad moderada (VIX {vix:.1f}). Las primas están por debajo de la media "
            f"histórica. Es posible operar, pero con tamaños conservadores y strikes "
            f"suficientemente alejados del precio. Evita abrir demasiadas posiciones "
            f"simultáneas — la calidad importa más que la cantidad."
        )
    elif fg_score <= 30 and vix > 25:
        p3 = (
            f"✅ **Para traders de opciones:** el miedo extremo y el VIX elevado ({vix:.1f}) "
            f"crean el mejor entorno posible para vender volatilidad. Las primas están "
            f"en niveles muy atractivos. Sin embargo, en mercados de pánico el precio "
            f"puede seguir bajando — usa spreads para limitar el riesgo máximo y "
            f"gestiona bien el capital."
        )
    elif fg_score <= 45 and vix > 20:
        p3 = (
            f"✅ **Para traders de opciones:** el miedo en el mercado y el VIX en {vix:.1f} "
            f"ofrecen primas por encima de la media. Buen entorno para estrategias como "
            f"PCS, CCS o Iron Condors con margen de seguridad amplio. "
            f"La incertidumbre puede mantenerse varios días — spreads mejor que opciones naked."
        )
    else:
        p3 = (
            f"📌 **Para traders de opciones:** entorno de volatilidad normalizada "
            f"(VIX {vix:.1f}, Fear & Greed {int(fg_score)}). Opera con tus estrategias "
            f"habituales, respeta tu plan de trading y no te dejes llevar por el ruido "
            f"del corto plazo. La consistencia gana a la suerte."
        )

    return f"{p1}\n\n{p2}\n\n{p3}"


# ── Narrativa de mercado ───────────────────────────────────────

def _market_narrative(name: str, chg: float, vix: float) -> str:
    vix_note = ""
    if name in ("S&P 500", "Nasdaq 100", "Russell 2000"):
        if vix >= 30:
            vix_note = f" El VIX en zona de estrés ({vix:.1f}) eleva las primas — mayor compensación pero también mayor riesgo."
        elif vix >= 20:
            vix_note = f" Con el VIX en {vix:.1f}, las primas de opciones están en niveles atractivos."
        elif vix >= 15:
            vix_note = f" VIX en {vix:.1f}: volatilidad normalizada, primas en niveles estándar."
        else:
            vix_note = f" VIX en zona de complacencia ({vix:.1f}) — primas bajas que exigen mayor selectividad."

    thresholds = {
        "S&P 500":      (2.5, 0.75),
        "Nasdaq 100":   (3.0, 0.75),
        "Russell 2000": (3.0, 1.0),
        "Bitcoin":      (6.0, 2.0),
        "Oro":          (2.0, 0.5),
        "WTI":          (3.5, 1.0),
    }
    big, small = thresholds.get(name, (2.5, 0.75))

    if name == "S&P 500":
        if chg > big:
            return (f"Excelente semana para las bolsas americanas, con el S&P 500 avanzando un {chg:.2f}% "
                    f"y compras generalizadas en todos los sectores. El mercado entró en modo 'risk-on', "
                    f"con los inversores asumiendo más riesgo ante un entorno macro favorable.{vix_note}")
        if chg > small:
            return (f"Semana positiva para el S&P 500 con una subida del {chg:.2f}%. "
                    f"Los datos macro continuaron respaldando el escenario de aterrizaje suave "
                    f"y no hubo grandes sorpresas que alteraran el sesgo alcista del mercado.{vix_note}")
        if chg > -small:
            return (f"Semana plana para el S&P 500, que cerró prácticamente sin cambios ({chg:+.2f}%). "
                    f"El mercado está en modo espera, digiriendo datos recientes y buscando un nuevo catalizador "
                    f"que marque la próxima dirección.{vix_note}")
        if chg > -big:
            return (f"El S&P 500 cedió un {abs(chg):.2f}% en la semana, con toma de beneficios "
                    f"e incertidumbre macro como protagonistas. La corrección, aunque molesta, "
                    f"se mantiene dentro de un rango técnicamente saludable.{vix_note}")
        return (f"Semana dura para el S&P 500, que cayó un {abs(chg):.2f}% con ventas generalizadas "
                f"y aversión al riesgo. Los inversores redujeron exposición ante la incertidumbre. "
                f"Técnicamente, el índice prueba soportes importantes que conviene vigilar.{vix_note}")

    if name == "Nasdaq 100":
        if chg > big:
            return (f"Los tecnológicos lideraron la subida semanal con el Nasdaq 100 avanzando un {chg:.2f}%. "
                    f"El optimismo en torno a resultados empresariales y el entorno de tipos "
                    f"impulsó a los grandes valores de crecimiento con fuerza.{vix_note}")
        if chg > small:
            return (f"El Nasdaq 100 cerró con una subida del {chg:.2f}%, apoyado en un entorno favorable "
                    f"para los valores tecnológicos y de crecimiento. "
                    f"La resistencia del sector sigue siendo un pilar del mercado alcista.{vix_note}")
        if chg > -small:
            return (f"Semana sin dirección para el Nasdaq 100 ({chg:+.2f}%). "
                    f"Los valores tecnológicos cotizaron en rango mientras el mercado evalúa "
                    f"las próximas referencias de resultados y política monetaria.{vix_note}")
        if chg > -big:
            return (f"El Nasdaq 100 retrocedió un {abs(chg):.2f}%, con el sector tecnológico "
                    f"sometido a toma de beneficios. La rotación hacia sectores más defensivos "
                    f"también pesó en el índice.{vix_note}")
        return (f"Fuerte corrección en el Nasdaq 100, que cayó un {abs(chg):.2f}% en la semana. "
                f"Los valores de crecimiento sufrieron ante la reevaluación del entorno de tipos "
                f"y las dudas sobre las valoraciones del sector tecnológico.{vix_note}")

    if name == "Russell 2000":
        if chg > big:
            return (f"Las pequeñas empresas brillaron esta semana con el Russell 2000 subiendo un {chg:.2f}%. "
                    f"Cuando las small caps lideran, suele indicar un optimismo renovado sobre "
                    f"la economía doméstica americana — señal positiva de amplitud de mercado.")
        if chg > small:
            return (f"El Russell 2000 avanzó un {chg:.2f}%, una señal positiva sobre la amplitud del mercado. "
                    f"Cuando las small caps participan en las subidas, el rally tiende a ser "
                    f"más sólido y sostenible.")
        if chg > -small:
            return (f"El Russell 2000 cerró plano ({chg:+.2f}%), sin liderazgo claro de las pequeñas empresas. "
                    f"La amplitud del mercado sigue siendo un factor a vigilar esta semana.")
        if chg > -big:
            return (f"Semana floja para las small caps, con el Russell 2000 cayendo un {abs(chg):.2f}%. "
                    f"Las pequeñas empresas son más sensibles a los tipos y a la incertidumbre, "
                    f"lo que explica el peor comportamiento relativo.")
        return (f"El Russell 2000 sufrió una caída del {abs(chg):.2f}%, penalizado especialmente "
                f"por la aversión al riesgo. Las small caps suelen comportarse peor en entornos "
                f"de incertidumbre elevada.")

    if name == "Bitcoin":
        if chg > big:
            return (f"Bitcoin vivió una semana espectacular con una subida del {chg:.2f}%, "
                    f"liderando el sector cripto con fuerza. El apetito de riesgo fue claramente alcista "
                    f"y la correlación positiva con el Nasdaq se mantuvo en este movimiento.")
        if chg > small:
            return (f"Bitcoin avanzó un {chg:.2f}% en la semana en un entorno de apetito de riesgo "
                    f"moderadamente positivo. El activo mantiene su tendencia de fondo "
                    f"y los niveles técnicos clave siguen respetándose.")
        if chg > -small:
            return (f"Semana lateral para Bitcoin ({chg:+.2f}%), sin catalizadores claros "
                    f"que rompieran el rango de consolidación. "
                    f"El mercado cripto está en pausa, digiriendo el movimiento previo.")
        if chg > -big:
            return (f"Bitcoin corrigió un {abs(chg):.2f}% en la semana, con presión vendedora "
                    f"y salida de flujos del sector cripto. Las correcciones en Bitcoin "
                    f"suelen ser rápidas e intensas — importante gestionar bien el riesgo.")
        return (f"Fuerte caída del {abs(chg):.2f}% para Bitcoin, con ventas masivas "
                f"que generaron un entorno de alta volatilidad en el sector cripto. "
                f"En estas fases de pánico, la gestión del riesgo es más importante que nunca.")

    if name == "Oro":
        if chg > big:
            return (f"El oro tuvo una excelente semana con una subida del {chg:.2f}%, "
                    f"funcionando como activo refugio ante la incertidumbre global. "
                    f"La combinación de tensiones geopolíticas y compras de bancos centrales "
                    f"continúa siendo el motor estructural del metal.")
        if chg > small:
            return (f"El oro cerró la semana al alza con un avance del {chg:.2f}%, "
                    f"respaldado por la demanda de activos refugio y un dólar moderadamente débil. "
                    f"La tendencia alcista de fondo sigue intacta.")
        if chg > -small:
            return (f"Semana tranquila para el oro ({chg:+.2f}%), que cotizó en rango "
                    f"sin grandes catalizadores. El soporte estructural sigue siendo "
                    f"sólido a largo plazo.")
        if chg > -big:
            return (f"El oro cedió un {abs(chg):.2f}% esta semana. "
                    f"Un dólar más fuerte y la reducción de la aversión al riesgo "
                    f"frenaron la demanda del metal. El soporte estructural sigue vigente.")
        return (f"Caída inusual del oro, que bajó un {abs(chg):.2f}% — raro en un activo refugio. "
                f"Generalmente responde a un dólar muy fuerte o a ventas forzadas "
                f"para cubrir pérdidas en otros activos.")

    if name == "WTI":
        if chg > big:
            return (f"El petróleo WTI avanzó un {chg:.2f}%, impulsado por expectativas de mayor demanda "
                    f"y reducción de inventarios. El repunte del crudo añade presión inflacionista "
                    f"y puede complicar las expectativas de bajadas de tipos.")
        if chg > small:
            return (f"El crudo WTI cerró la semana con una subida del {chg:.2f}%, "
                    f"apoyado en datos de inventarios favorables y expectativas de demanda sostenida. "
                    f"El sector energético se beneficia de este entorno.")
        if chg > -small:
            return (f"El petróleo WTI cerró plano ({chg:+.2f}%), con las fuerzas de oferta "
                    f"y demanda en equilibrio. El mercado del crudo está pendiente de "
                    f"las decisiones de la OPEP+ y la evolución de la economía global.")
        if chg > -big:
            return (f"El WTI cayó un {abs(chg):.2f}% ante preocupaciones sobre la demanda global. "
                    f"Una caída del crudo reduce la presión inflacionista, "
                    f"lo que podría facilitar el camino a la Fed para bajar tipos.")
        return (f"Fuerte caída del WTI, que se desplomó un {abs(chg):.2f}% en la semana. "
                f"La combinación de débil demanda y exceso de oferta presionó el precio. "
                f"El sector energético en cartera puede haber sufrido esta semana.")

    return f"Variación semanal: {chg:+.2f}%."


# ── Fear & Greed ───────────────────────────────────────────────

def _fg_spectrum(score: int, rating: str) -> str:
    zones = [
        (0,  25,  "🔴", "Miedo Extremo"),
        (25, 45,  "🟠", "Miedo"),
        (45, 55,  "🟡", "Neutral"),
        (55, 75,  "🟢", "Codicia"),
        (75, 100, "💚", "Codicia Extrema"),
    ]
    parts = []
    for lo, hi, emoji, label in zones:
        active = lo < score <= hi or (score == 0 and lo == 0)
        if active:
            parts.append(f"**{emoji} {label} — {score} / 100**")
        else:
            parts.append(emoji)
    return "  ·  ".join(parts)


def _fg_narrative(score: float, rating: str, delta: float) -> str:
    s         = int(score)
    trend_str = (
        f"Ha {'subido' if delta >= 0 else 'bajado'} {abs(delta):.1f} puntos "
        f"respecto a la semana pasada, lo que indica que el sentimiento "
        f"está {'mejorando' if delta >= 0 else 'deteriorándose'}."
    )
    if s <= 25:
        return (
            f"El mercado está en **Miedo Extremo** — zona que históricamente ha ofrecido "
            f"las mejores oportunidades de compra a largo plazo. Los inversores están asustados "
            f"y vendiendo. {trend_str}\n\n"
            f"Para traders de opciones: las primas están **muy elevadas** — excelente momento "
            f"para vender volatilidad, aunque con gestión de riesgo estricta dado el entorno. "
            f"El mercado puede seguir bajando antes de recuperarse."
        )
    if s <= 45:
        return (
            f"El sentimiento está en zona de **Miedo**, con los inversores adoptando una postura "
            f"más defensiva. {trend_str}\n\n"
            f"Para traders de opciones: las primas están **por encima de la media histórica**, "
            f"lo que favorece las estrategias de venta de volatilidad. "
            f"Buen entorno para abrir posiciones con margen de seguridad amplio."
        )
    if s <= 55:
        return (
            f"El mercado está en zona **Neutral**, con equilibrio entre compradores y vendedores. "
            f"{trend_str}\n\n"
            f"Para traders de opciones: las primas reflejan **volatilidad normalizada**. "
            f"No hay señal clara de dirección — momento de ser selectivo con las estrategias "
            f"y no forzar operaciones."
        )
    if s <= 75:
        return (
            f"El sentimiento se inclina hacia la **Codicia**, con el optimismo dominando el mercado. "
            f"{trend_str}\n\n"
            f"Para traders de opciones: las primas están **más bajas de lo normal**, "
            f"lo que reduce la compensación al vender volatilidad. "
            f"Ojo con la complacencia — los mercados en codicia pueden corregir sin avisar."
        )
    return (
        f"Estamos en zona de **Codicia Extrema** — euforia generalizada. "
        f"{trend_str}\n\n"
        f"Para traders de opciones: las primas están **muy comprimidas**. "
        f"Considera proteger las posiciones abiertas y sé conservador con nuevas aperturas. "
        f"Históricamente, las lecturas extremas de codicia preceden a correcciones."
    )


def _fg_color(score: int) -> int:
    if score <= 25: return 0xE74C3C
    if score <= 45: return 0xE67E22
    if score <= 55: return 0xF1C40F
    if score <= 75: return 0x2ECC71
    return 0x27AE60


# ── Macro narrativo ────────────────────────────────────────────

def _format_macro_narrative(events: List[dict]) -> str:
    if not events:
        return (
            "_No se encontraron eventos de alto impacto esta semana según ForexFactory._\n\n"
            "Aun así, la liquidez del inicio de semana y posibles noticias inesperadas "
            "siempre pueden generar volatilidad."
        )

    day_names = {0: "Lunes", 1: "Martes", 2: "Miércoles", 3: "Jueves", 4: "Viernes"}
    flag_map  = {"USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵"}
    by_day: Dict[str, List[str]] = {}

    for ev in events:
        ev_date_str = ev.get("date", "")
        try:
            if len(ev_date_str) == 10 and ev_date_str[2] == "-":
                ev_date = datetime.strptime(ev_date_str, "%m-%d-%Y").date()
            else:
                ev_date = datetime.strptime(ev_date_str[:10], "%Y-%m-%d").date()
            day = day_names.get(ev_date.weekday(), ev_date.strftime("%d/%m"))
        except Exception:
            day = "Semana"

        currency = ev.get("currency", "")
        title    = ev.get("title", ev.get("event", "Evento económico"))
        flag     = flag_map.get(currency, "🌍")
        context  = ""
        for keyword, note in _MACRO_CONTEXT.items():
            if keyword.lower() in title.lower():
                context = f"\n    _{note}_"
                break
        by_day.setdefault(day, []).append(f"  {flag} **{title}**{context}")

    intro = "Esta semana tenemos eventos macro importantes que pueden generar volatilidad:\n\n"
    lines: List[str] = [intro]
    for day in ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes"]:
        if day in by_day:
            lines.append(f"**📆 {day}**")
            lines.extend(by_day[day])
            lines.append("")
    return "\n".join(lines).rstrip()


# ── Earnings narrativo ─────────────────────────────────────────

def _format_earnings_narrative(earnings: List[dict]) -> str:
    base_links = (
        "\n\n🔗 Calendario completo: "
        "[EarningsWhispers](https://www.earningswhispers.com) · "
        "[Nasdaq](https://www.nasdaq.com/market-activity/earnings)"
    )
    if not earnings:
        return (
            "_No se detectaron earnings de grandes compañías esta semana._\n\n"
            "Aun así, revisa si tienes posiciones en subyacentes con resultados "
            "pendientes — un earnings puede doblar la volatilidad implícita de un día para otro."
            + base_links
        )

    day_names = {0: "Lunes", 1: "Martes", 2: "Miércoles", 3: "Jueves", 4: "Viernes"}
    by_day: Dict[str, List[str]] = {}
    for e in earnings:
        try:
            d   = datetime.strptime(e["date"], "%Y-%m-%d").date()
            day = day_names.get(d.weekday(), e["date"])
        except Exception:
            day = "Semana"
        ticker   = e.get("ticker", "")
        company  = e.get("company") or ticker
        timing   = e.get("time", "")
        eps_est  = e.get("eps_est", "")
        eps_str  = f" · EPS est. **{eps_est}**" if eps_est else ""
        by_day.setdefault(day, []).append(
            f"  `{ticker}` **{company}** {timing}{eps_str}"
        )

    intro = (
        "Si tienes posiciones abiertas en alguno de estos subyacentes, "
        "revisa tu riesgo antes del evento:\n\n"
    )
    lines: List[str] = [intro]
    for day in ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes"]:
        if day in by_day:
            lines.append(f"**📆 {day}**")
            lines.extend(by_day[day])
            lines.append("")
    return "\n".join(lines).rstrip() + base_links


# ── Helpers ───────────────────────────────────────────────────

def _normalize_rating(rating: str) -> str:
    mapping = {
        "extreme fear":  "Miedo Extremo",
        "fear":          "Miedo",
        "neutral":       "Neutral",
        "greed":         "Codicia",
        "extreme greed": "Codicia Extrema",
    }
    return mapping.get(rating.lower(), rating.title())


def _fmt_price(name: str, price: float) -> str:
    if name == "Bitcoin":
        return f"{price:,.0f}"
    return f"{price:,.2f}"


def _month_es(month: int) -> str:
    return ["enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre",
            "diciembre"][month - 1]


async def _sleep_until(dt: datetime) -> None:
    secs = (dt - datetime.now(dt.tzinfo)).total_seconds()
    if secs > 0:
        await asyncio.sleep(secs)


async def _post_webhook(url: str, payload: dict) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                raise RuntimeError(f"Webhook HTTP {resp.status}: {text[:200]}")
