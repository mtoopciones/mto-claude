"""
Genera tarjetas visuales PNG para publicar en Discord.
Diseño infographic light-style.
"""

import io, os
from datetime import datetime
from typing import Optional, List, Tuple
from loguru import logger

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logger.warning("Pillow no disponible — se usarán embeds de texto")

from .strategy import StrategyInfo, Leg
from .metrics import TradeMetrics
from .position_tracker import TradeEvent


W   = 520
PAD = 18

# ── Palette ───────────────────────────────────────────────────
WHITE      = (255, 255, 255)
BORDER     = (218, 224, 236)
TEXT       = ( 28,  33,  50)
TEXT_MED   = ( 75,  88, 110)
TEXT_GRAY  = (148, 158, 178)

ALERT_RED  = (214,  68,  38)   # orange-red header
BLUE_DEEP  = ( 29,  95, 140)   # financial section header
BLUE_MID   = ( 41, 128, 185)   # ticker box
BLUE_LIGHT = ( 52, 152, 219)   # blue icon

# Icon circle colors
IC_PURPLE  = (130,  60, 165)
IC_BLUE    = ( 52, 152, 219)
IC_RED     = (185,  55,  40)
IC_ORANGE  = (225, 120,  30)
IC_TEAL    = ( 22, 158, 130)
IC_GREEN   = ( 39, 174,  96)
IC_GRAY    = (120, 135, 150)
IC_DEEP    = ( 29,  95, 140)

# Value pill  (background, text)
PILL_GREEN  = ((210, 247, 224), ( 25, 145,  78))
PILL_RED    = ((251, 213, 208), (175,  45,  30))
PILL_ORANGE = ((254, 242, 194), (180,  95,  15))
PILL_GRAY   = ((232, 236, 240), ( 75,  88, 110))
PILL_BLUE   = ((209, 233, 250), ( 25,  90, 140))
PILL_TEAL   = ((205, 244, 238), ( 18, 140, 115))

ACCENT_MAP = {
    TradeEvent.OPEN:          ALERT_RED,
    TradeEvent.ADD:           IC_GREEN,
    TradeEvent.CLOSE:         BLUE_DEEP,
    TradeEvent.PARTIAL_CLOSE: IC_PURPLE,
    TradeEvent.ROLL:          IC_ORANGE,
}

LABEL_MAP = {
    TradeEvent.OPEN:          "APERTURA DE OPERACION",
    TradeEvent.ADD:           "AMPLIACION DE POSICION",
    TradeEvent.CLOSE:         "CIERRE DE OPERACION",
    TradeEvent.PARTIAL_CLOSE: "CIERRE PARCIAL",
    TradeEvent.ROLL:          "ROLL DE POSICION",
}

_STRAT_DESC = {
    "CSP":  "Generar ingreso vendiendo puts con compromiso de compra.",
    "CC":   "Generar ingreso vendiendo calls sobre acciones en cartera.",
    "NC":   "Prima recibida sin cobertura. Riesgo ilimitado al alza.",
    "SC":   "Prima recibida sin cobertura. Riesgo ilimitado al alza.",
    "NP":   "Prima recibida. Obligacion de comprar al strike si precio cae.",
    "SP":   "Prima recibida. Obligacion de comprar al strike si precio cae.",
    "BCS":  "Spread alcista de calls. Coste neto, ganancia acotada.",
    "BPS":  "Spread alcista de puts. Credito neto, riesgo acotado.",
    "BRS":  "Spread bajista de calls. Credito neto, riesgo acotado.",
    "STRD": "Venta straddle: max. ganancia si precio queda en el strike.",
    "STRG": "Venta strangle: max. ganancia en rango amplio de precios.",
    "BWB":  "Mariposa asimetrica. Credito neto al abrir, riesgo acotado.",
    "CDS":  "Diferencial temporal entre vencimientos.",
}


# ── Fuentes ──────────────────────────────────────────────────
_FONTS: dict = {}

def _load_fonts() -> dict:
    global _FONTS
    if _FONTS:
        return _FONTS
    candidates = [
        ("/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
         "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ]
    reg = bold = None
    for r, b in candidates:
        if os.path.exists(r):
            reg  = r
            bold = b if os.path.exists(b) else r
            break
    if reg:
        try:
            _FONTS = {
                "xs":   ImageFont.truetype(reg,  11),
                "sm":   ImageFont.truetype(reg,  13),
                "reg":  ImageFont.truetype(reg,  15),
                "med":  ImageFont.truetype(bold, 15),
                "lg":   ImageFont.truetype(bold, 19),
                "xl":   ImageFont.truetype(bold, 24),
                "ttl":  ImageFont.truetype(bold, 29),
                "huge": ImageFont.truetype(bold, 34),
            }
            return _FONTS
        except Exception:
            pass
    f = ImageFont.load_default()
    _FONTS = {k: f for k in ("xs","sm","reg","med","lg","xl","ttl","huge")}
    return _FONTS


# ── Logo ─────────────────────────────────────────────────────
_LOGO: Optional["Image.Image"] = None

def _get_logo(url: str, size: int = 56) -> Optional["Image.Image"]:
    global _LOGO
    if not PIL_AVAILABLE or not url:
        return None
    if _LOGO is None:
        try:
            if url.startswith("/"):
                # Local file path on server
                _LOGO = Image.open(url).convert("RGBA")
            else:
                from urllib.request import urlopen, Request
                req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
                data = urlopen(req, timeout=6).read()
                _LOGO = Image.open(io.BytesIO(data)).convert("RGBA")
        except Exception as e:
            logger.warning(f"Logo no cargado: {e}")
            return None
    return _LOGO.resize((size, size), Image.LANCZOS)


def _paste_logo(img: "Image.Image", logo: "Image.Image", x: int, y: int) -> None:
    """Paste RGBA logo using its own alpha channel (no circular crop)."""
    if logo.mode == "RGBA":
        img.paste(logo.convert("RGBA"), (x, y), logo.split()[3])
    else:
        img.paste(logo, (x, y))


# ── Drawing helpers ───────────────────────────────────────────

def _tw(draw: "ImageDraw.Draw", text: str, font) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]

def _th(draw: "ImageDraw.Draw", text: str, font) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[3] - bb[1]

def _rrect(draw: "ImageDraw.Draw", xy: Tuple, r: int,
           fill=None, outline=None, lw: int = 1) -> None:
    draw.rounded_rectangle(list(xy), radius=r, fill=fill, outline=outline, width=lw)

def _hline(draw: "ImageDraw.Draw", y: int,
           x0: int = 0, x1: int = W, color=BORDER) -> None:
    draw.line([(x0, y), (x1, y)], fill=color, width=1)

def _icon_circle(draw: "ImageDraw.Draw", cx: int, cy: int,
                 r: int, bg, letter: str, font) -> None:
    """Filled circle with a centered letter."""
    draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=bg)
    lw = _tw(draw, letter, font)
    lh = _th(draw, letter, font)
    draw.text((cx - lw // 2, cy - lh // 2), letter, font=font, fill=WHITE)

def _shield(draw: "ImageDraw.Draw", cx: int, top_y: int,
            w: int, h: int, color) -> None:
    """Pentagon shield: rounded top + pointed bottom."""
    _rrect(draw, (cx - w // 2, top_y, cx + w // 2, top_y + h * 2 // 3), r=10, fill=color)
    draw.polygon([
        (cx - w // 2, top_y + h * 2 // 3 - 2),
        (cx + w // 2, top_y + h * 2 // 3 - 2),
        (cx, top_y + h),
    ], fill=color)

def _value_pill(draw: "ImageDraw.Draw", right_x: int, cy: int,
                text: str, pill: Tuple, font) -> None:
    """Right-aligned rounded pill with colored background."""
    bg, fg = pill
    pw = _tw(draw, text, font) + 22
    ph = _th(draw, text, font) + 12
    px = right_x - pw
    py = cy - ph // 2
    _rrect(draw, (px, py, px + pw, py + ph), r=7, fill=bg)
    tw = _tw(draw, text, font)
    draw.text((px + (pw - tw) // 2, py + 6), text, font=font, fill=fg)

def _wrap_text(draw: "ImageDraw.Draw", text: str, font, max_w: int) -> List[str]:
    words = text.split()
    lines, cur = [], ""
    for word in words:
        test = (cur + " " + word).strip()
        if _tw(draw, test, font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


# ── Data formatting ───────────────────────────────────────────

def _fdate(expiry: Optional[str]) -> str:
    if not expiry or len(expiry) < 8:
        return "—"
    meses = ["ene.","feb.","mar.","abr.","may.","jun.",
             "jul.","ago.","sep.","oct.","nov.","dic."]
    try:
        y, m, d = int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8])
        return f"{d:02d}-{meses[m-1]}-{str(expiry[:4])[2:]}"
    except Exception:
        return expiry

def _fmoney(v: Optional[float], sign: bool = False) -> str:
    if v is None: return "—"
    if v ==  float("inf"): return "Ilimitada"
    if v == -float("inf"): return "-Ilimitada"
    s = "+" if sign and v > 0 else ""
    return f"{s}${v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def _fpct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.2f}%".replace(".", ",")

def _fmt_strike(v: Optional[float]) -> str:
    if v is None: return "—"
    return f"${v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def _mc_pill(v: Optional[float]) -> Tuple:
    if v is None: return PILL_GRAY
    return PILL_GREEN if v >= 0 else PILL_RED

def _puts(s: StrategyInfo) -> List[Leg]:
    return sorted([l for l in s.legs if l.sec_type == "OPT" and l.right == "P"],
                  key=lambda l: l.strike or 0, reverse=True)

def _calls(s: StrategyInfo) -> List[Leg]:
    return sorted([l for l in s.legs if l.sec_type == "OPT" and l.right == "C"],
                  key=lambda l: l.strike or 0)


# ── Public API ───────────────────────────────────────────────

def generate(strategy: StrategyInfo, metrics: TradeMetrics,
             event_type: str, logo_url: str, account_name: str) -> Optional[bytes]:
    if not PIL_AVAILABLE:
        return None
    try:
        return _draw_card(strategy, metrics, event_type, logo_url, account_name)
    except Exception as e:
        logger.error(f"Error generando tarjeta: {e}")
        return None


# ── Main card ────────────────────────────────────────────────

def _draw_card(strategy: StrategyInfo, metrics: TradeMetrics,
               event_type: str, logo_url: str, account_name: str) -> bytes:

    fonts   = _load_fonts()
    puts    = _puts(strategy)
    calls   = _calls(strategy)
    accent  = ACCENT_MAP.get(event_type, ALERT_RED)
    ev_lbl  = LABEL_MAP.get(event_type, "OPERACION")
    is_open = event_type in (TradeEvent.OPEN, TradeEvent.ADD)

    now = datetime.now()
    mes = ["ene.","feb.","mar.","abr.","may.","jun.",
           "jul.","ago.","sep.","oct.","nov.","dic."]
    today     = f"{now.day:02d}-{mes[now.month-1]}-{str(now.year)[2:]}"
    leg0      = strategy.legs[0] if strategy.legs else None
    exec_time = leg0.exec_time.strftime("%H:%M") if (leg0 and leg0.exec_time) else ""
    co_name   = leg0.company_name if (leg0 and leg0.company_name) else ""
    exchange  = leg0.exchange if (leg0 and leg0.exchange) else "SMART"
    ticker    = strategy.underlying
    desc      = _STRAT_DESC.get(strategy.short_name, "")

    # Info rows definition: (icon_letter, icon_color, label, value)
    info_rows: List[Tuple] = [
        ("T", IC_PURPLE, "TIPO DE OPERACION",  f"{strategy.short_name} - {strategy.name}"),
        ("A", IC_BLUE,   "APERTURA",            today),
        ("V", IC_RED,    "VENCIMIENTO",         _fdate(strategy.primary_expiry)),
    ]
    for put in puts:
        act = "VENTA" if put.action == "SELL" else "COMPRA"
        info_rows.append(("S", IC_ORANGE, f"STRIKE {act} PUT", _fmt_strike(put.strike)))
    for call in calls:
        act = "VENTA" if call.action == "SELL" else "COMPRA"
        info_rows.append(("S", IC_TEAL, f"STRIKE {act} CALL", _fmt_strike(call.strike)))
    nc = strategy.num_contracts
    info_rows.append(("C", IC_DEEP, "CONTRATOS ABIERTOS", str(nc)))

    # Financial rows: (icon_letter, icon_color, label, value, pill)
    if is_open:
        fin_rows: List[Tuple] = [
            ("P", IC_PURPLE, "Prima +/- comision",       _fmoney(metrics.net_premium_after_comm), _mc_pill(metrics.net_premium_after_comm)),
            ("B", IC_BLUE,   "Buying Power requerido",    _fmoney(metrics.buying_power),           PILL_GRAY),
            ("G", IC_GREEN,  "Maxima ganancia",            _fmoney(metrics.max_gain),               PILL_GREEN),
            ("R", IC_RED,    "Riesgo maximo",              _fmoney(metrics.max_loss),               PILL_RED),
            ("E", IC_ORANGE, "Break-even (B.E.)",
             _fmoney(metrics.breakeven) if not metrics.breakeven_high
             else f"{_fmoney(metrics.breakeven)} / {_fmoney(metrics.breakeven_high)}",
             PILL_ORANGE),
            ("D", IC_GRAY,   "DTE (Dias al vencimiento)", f"{metrics.dte} dias" if metrics.dte is not None else "—", PILL_GRAY),
            ("%", IC_DEEP,   "ROI estimado",               _fpct(metrics.roi_pct),                 PILL_BLUE),
        ]
    else:
        # close_prem_gross = prima bruta pagada/cobrada (sin comisión)
        # Para cierres de crédito (recibimos prima): net_premium > 0
        # Para cierres de débito (pagamos prima):    net_premium < 0
        _is_close_credit = (metrics.net_premium or 0) >= 0
        _prima_label = "Prima recibida" if _is_close_credit else "Prima pagada"
        _prima_value = _fmoney(abs(metrics.net_premium or 0))
        _prima_pill  = PILL_GREEN if _is_close_credit else PILL_RED

        fin_rows = [
            ("P", IC_PURPLE, _prima_label,         _prima_value,                              _prima_pill),
            ("C", IC_RED,    "Coste operacion",     _fmoney(abs(metrics.net_premium_after_comm or 0)), PILL_RED),
            ("R", IC_GREEN,  "Resultado",
             _fmoney(metrics.trade_result, sign=True) if metrics.trade_result is not None else "—",
             PILL_GREEN if (metrics.trade_result or 0) >= 0 else PILL_RED),
            ("D", IC_GRAY,   "DTE (Dias al vencimiento)", f"{metrics.dte} dias" if metrics.dte is not None else "—", PILL_GRAY),
        ]

    # ── Heights ───────────────────────────────────────────────
    ALERT_W  = 92     # left ALERTA panel width
    HDR_H    = 110    # header
    CO_H     = 64     # company row
    IR_H     = 44     # info row height
    IP       = 14     # info section padding
    INFO_H   = len(info_rows) * IR_H + IP * 2
    FIN_H_H  = 48     # financial header
    FIN_R_H  = 42     # financial row
    FOOT_H   = 76     # footer
    DIV      = 8

    total_h = HDR_H + CO_H + DIV + INFO_H + DIV + FIN_H_H + len(fin_rows) * FIN_R_H + DIV + FOOT_H

    img  = Image.new("RGB", (W, total_h), WHITE)
    draw = ImageDraw.Draw(img)
    y    = 0

    # ── HEADER ────────────────────────────────────────────────
    # Left ALERTA panel
    draw.rectangle([(0, y), (ALERT_W, y + HDR_H)], fill=accent)

    # Logo or bell icon fallback
    logo = _get_logo(logo_url, 78)
    lx   = (ALERT_W - 78) // 2
    ly   = y + (HDR_H - 78) // 2
    if logo:
        _paste_logo(img, logo, lx, ly)
    else:
        bx, by = ALERT_W // 2, y + 40
        draw.ellipse([(bx - 18, by - 20), (bx + 18, by + 4)], fill=WHITE)
        draw.rectangle([(bx - 18, by - 4), (bx + 18, by + 12)], fill=WHITE)
        draw.ellipse([(bx - 5, by + 10), (bx + 5, by + 20)], fill=WHITE)
        draw.rectangle([(bx - 4, by - 26), (bx + 4, by - 16)], fill=WHITE)

    # "ALERTA" label
    aw = _tw(draw, "ALERTA", fonts["sm"])
    draw.text(((ALERT_W - aw) // 2, y + HDR_H - 26), "ALERTA", font=fonts["sm"], fill=WHITE)

    # Right title panel
    draw.rectangle([(ALERT_W, y), (W, y + HDR_H)], fill=WHITE)
    # Thin accent left border
    draw.rectangle([(ALERT_W, y), (ALERT_W + 4, y + HDR_H)], fill=accent)

    tx = ALERT_W + 14
    strat_txt = f"{strategy.short_name} / {strategy.name}"
    draw.text((tx, y + 14), strat_txt, font=fonts["ttl"], fill=TEXT)
    draw.text((tx, y + 56), ev_lbl, font=fonts["med"], fill=accent)
    draw.text((tx, y + 80), account_name, font=fonts["xs"], fill=TEXT_GRAY)
    y += HDR_H

    # ── COMPANY ROW ───────────────────────────────────────────
    draw.rectangle([(0, y), (W, y + CO_H)], fill=WHITE)
    _hline(draw, y,          0, W, BORDER)
    _hline(draw, y + CO_H,   0, W, BORDER)

    # Company icon circle
    ic_cx, ic_cy = PAD + 18, y + CO_H // 2
    draw.ellipse([(ic_cx - 18, ic_cy - 18), (ic_cx + 18, ic_cy + 18)], fill=BLUE_DEEP)
    co_init = co_name[0].upper() if co_name else ticker[0].upper()
    _icon_circle(draw, ic_cx, ic_cy, 18, BLUE_DEEP, co_init, fonts["med"])

    # Company name + exchange
    ncx = ic_cx + 26
    cn_display = co_name.upper() if co_name else ticker
    draw.text((ncx, y + 10), cn_display, font=fonts["med"], fill=TEXT)
    exch_txt = f"({exchange}: {ticker})"
    draw.text((ncx, y + 32), exch_txt, font=fonts["xs"], fill=TEXT_GRAY)

    # Ticker box (solid blue, right side)
    tb_w, tb_h = 78, CO_H - 16
    tb_x = W - PAD - tb_w
    tb_y = y + 8
    _rrect(draw, (tb_x, tb_y, tb_x + tb_w, tb_y + tb_h), r=6, fill=BLUE_MID)
    tick_lbl = "TICKER:"
    tl_w = _tw(draw, tick_lbl, fonts["xs"])
    draw.text((tb_x + (tb_w - tl_w) // 2, tb_y + 6), tick_lbl, font=fonts["xs"], fill=WHITE)
    tv_w = _tw(draw, ticker, fonts["lg"])
    draw.text((tb_x + (tb_w - tv_w) // 2, tb_y + 22), ticker, font=fonts["lg"], fill=WHITE)

    y += CO_H + DIV

    # ── INFO SECTION ──────────────────────────────────────────
    draw.rectangle([(0, y), (W, y + INFO_H)], fill=WHITE)

    LEFT_W  = int(W * 0.60)
    RIGHT_W = W - LEFT_W

    # Vertical divider
    draw.line([(LEFT_W, y + 10), (LEFT_W, y + INFO_H - 10)], fill=BORDER, width=1)

    # Left column: info rows with icon circles
    iy = y + IP
    for (ic_l, ic_c, lbl, val) in info_rows:
        ic_cx2, ic_cy2 = PAD + 15, iy + IR_H // 2
        _icon_circle(draw, ic_cx2, ic_cy2, 15, ic_c, ic_l, fonts["sm"])
        rx2 = ic_cx2 + 22
        draw.text((rx2, iy + 4),  lbl, font=fonts["xs"], fill=TEXT_GRAY)
        draw.text((rx2, iy + 19), val, font=fonts["med"], fill=TEXT)
        iy += IR_H

    # Right column: shield + strategy description
    rx3 = LEFT_W + 12
    ry3 = y + IP
    sh_cx = rx3 + (RIGHT_W - 20) // 2

    # Shield icon
    _shield(draw, sh_cx, ry3, 52, 60, BLUE_MID)
    # Dollar sign inside shield
    ds = "$"
    ds_w = _tw(draw, ds, fonts["xl"])
    draw.text((sh_cx - ds_w // 2, ry3 + 8), ds, font=fonts["xl"], fill=WHITE)

    # Strategy description box (green tint)
    desc_y  = ry3 + 68
    dbox_w  = RIGHT_W - 22
    dbox_h  = y + INFO_H - IP - desc_y
    if dbox_h > 24 and desc:
        _rrect(draw, (rx3, desc_y, rx3 + dbox_w, desc_y + dbox_h), r=8, fill=(236, 252, 242))
        draw.text((rx3 + 8, desc_y + 6), "Estrategia:", font=fonts["xs"], fill=IC_GREEN)
        lines = _wrap_text(draw, desc, fonts["xs"], dbox_w - 14)
        ty3   = desc_y + 20
        for line in lines[:5]:
            draw.text((rx3 + 8, ty3), line, font=fonts["xs"], fill=TEXT_MED)
            ty3 += 15

    y += INFO_H + DIV

    # ── FINANCIAL SECTION ─────────────────────────────────────
    # Header (dark blue)
    draw.rectangle([(0, y), (W, y + FIN_H_H)], fill=BLUE_DEEP)
    # Small bag icon circle
    _icon_circle(draw, PAD + 16, y + FIN_H_H // 2, 16, BLUE_MID, "$", fonts["med"])
    hdr_x = PAD + 40
    draw.text((hdr_x, y + 8),  "DETALLES FINANCIEROS", font=fonts["med"], fill=WHITE)
    draw.text((hdr_x, y + 28), "(por cada contrato abierto)", font=fonts["xs"], fill=(178, 212, 238))
    y += FIN_H_H

    # Financial rows
    for i, (ic_l, ic_c, lbl, val, pill) in enumerate(fin_rows):
        row_bg = WHITE if i % 2 == 0 else (246, 249, 253)
        draw.rectangle([(0, y), (W, y + FIN_R_H)], fill=row_bg)
        _hline(draw, y, 0, W, (228, 234, 242))

        fi_cx, fi_cy = PAD + 15, y + FIN_R_H // 2
        _icon_circle(draw, fi_cx, fi_cy, 15, ic_c, ic_l, fonts["sm"])

        draw.text((fi_cx + 22, y + (FIN_R_H - 15) // 2), lbl, font=fonts["reg"], fill=TEXT_MED)

        _value_pill(draw, W - PAD, y + FIN_R_H // 2, val, pill, fonts["med"])

        y += FIN_R_H

    _hline(draw, y, 0, W, BORDER)
    y += DIV

    # ── FOOTER ────────────────────────────────────────────────
    draw.rectangle([(0, y), (W, y + FOOT_H)], fill=(255, 249, 215))
    _hline(draw, y, 0, W, (240, 215, 120))

    # Lightbulb icon
    lb_cx, lb_cy = PAD + 16, y + 28
    draw.ellipse([(lb_cx - 16, lb_cy - 16), (lb_cx + 16, lb_cy + 10)], fill=(255, 210, 30))
    draw.rectangle([(lb_cx - 8, lb_cy + 8), (lb_cx + 8, lb_cy + 18)], fill=(255, 210, 30))
    draw.rectangle([(lb_cx - 5, lb_cy + 17), (lb_cx + 5, lb_cy + 22)], fill=(220, 170, 20))
    ex_w = _tw(draw, "!", fonts["med"])
    draw.text((lb_cx - ex_w // 2, lb_cy - 10), "!", font=fonts["med"], fill=WHITE)

    draw.text((lb_cx + 26, y + 8), "RECUERDA:", font=fonts["med"], fill=(165, 105, 10))
    disc = ("Las opciones implican riesgos significativos y no son adecuadas para todos "
            "los inversores. Asegurate de comprender los riesgos antes de operar.")
    disc_lines = _wrap_text(draw, disc, fonts["xs"], W - lb_cx - 44)
    ty4 = y + 28
    for line in disc_lines[:3]:
        draw.text((lb_cx + 26, ty4), line, font=fonts["xs"], fill=(130, 90, 15))
        ty4 += 15

    # Timestamp bottom right
    ts  = f"{today}  {exec_time}" if exec_time else today
    tsw = _tw(draw, ts, fonts["xs"])
    draw.text((W - PAD - tsw, y + FOOT_H - 16), ts, font=fonts["xs"], fill=TEXT_GRAY)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── Roll generator ────────────────────────────────────────────

def generate_roll(close_strategy: StrategyInfo, close_metrics: TradeMetrics,
                  open_strategy: StrategyInfo, open_metrics: TradeMetrics,
                  logo_url: str, account_name: str) -> Optional[bytes]:
    if not PIL_AVAILABLE:
        return None
    try:
        return _draw_roll_card(close_strategy, close_metrics,
                               open_strategy, open_metrics,
                               logo_url, account_name)
    except Exception as e:
        logger.error(f"Error generando tarjeta roll: {e}")
        return None


def _draw_roll_card(close_strategy: StrategyInfo, close_metrics: TradeMetrics,
                    open_strategy: StrategyInfo, open_metrics: TradeMetrics,
                    logo_url: str, account_name: str) -> bytes:

    fonts = _load_fonts()
    c_puts  = _puts(close_strategy)
    c_calls = _calls(close_strategy)
    o_puts  = _puts(open_strategy)
    o_calls = _calls(open_strategy)

    leg0    = open_strategy.legs[0] if open_strategy.legs else None
    co_name = leg0.company_name if (leg0 and leg0.company_name) else ""
    exchange = leg0.exchange if (leg0 and leg0.exchange) else "SMART"
    ticker  = open_strategy.underlying

    now = datetime.now()
    mes = ["ene.","feb.","mar.","abr.","may.","jun.",
           "jul.","ago.","sep.","oct.","nov.","dic."]
    today  = f"{now.day:02d}-{mes[now.month-1]}-{str(now.year)[2:]}"
    c_leg0 = close_strategy.legs[0] if close_strategy.legs else None
    o_leg0 = open_strategy.legs[0]  if open_strategy.legs  else None
    c_time = c_leg0.exec_time.strftime("%H:%M") if (c_leg0 and c_leg0.exec_time) else ""
    o_time = o_leg0.exec_time.strftime("%H:%M") if (o_leg0 and o_leg0.exec_time) else ""

    close_cost   = close_metrics.net_premium_after_comm or 0.0
    open_premium = open_metrics.net_premium_after_comm  or 0.0
    net_result   = open_premium + close_cost

    ALERT_W = 92
    HDR_H   = 110
    CO_H    = 64
    SEC_H   = 38
    ROW_H   = 44
    FIN_H_H = 48
    FIN_R_H = 42
    NET_H   = 58
    FOOT_H  = 76
    DIV     = 8

    n_c = len(c_puts) + len(c_calls)
    n_o = len(o_puts) + len(o_calls)

    fin_rows: List[Tuple] = [
        ("P", IC_PURPLE, "Prima neta (apertura)", _fmoney(open_metrics.net_premium_after_comm), _mc_pill(open_metrics.net_premium_after_comm)),
        ("B", IC_BLUE,   "Buying Power requerido", _fmoney(open_metrics.buying_power), PILL_GRAY),
        ("G", IC_GREEN,  "Maxima ganancia", _fmoney(open_metrics.max_gain), PILL_GREEN),
        ("R", IC_RED,    "Riesgo maximo",   _fmoney(open_metrics.max_loss), PILL_RED),
        ("E", IC_ORANGE, "Break-even (B.E.)",
         _fmoney(open_metrics.breakeven) if not open_metrics.breakeven_high
         else f"{_fmoney(open_metrics.breakeven)} / {_fmoney(open_metrics.breakeven_high)}",
         PILL_ORANGE),
        ("D", IC_GRAY,   "DTE (Dias al vencimiento)", f"{open_metrics.dte} dias" if open_metrics.dte else "—", PILL_GRAY),
        ("%", IC_DEEP,   "ROI estimado", _fpct(open_metrics.roi_pct), PILL_BLUE),
    ]

    total_h = (HDR_H + CO_H + DIV +
               SEC_H + (1 + n_c + 1) * ROW_H + DIV +
               SEC_H + (1 + n_o + 1) * ROW_H + DIV +
               FIN_H_H + len(fin_rows) * FIN_R_H + DIV +
               NET_H + DIV + FOOT_H)

    img  = Image.new("RGB", (W, total_h), WHITE)
    draw = ImageDraw.Draw(img)
    y    = 0
    MID  = W // 2
    roll_accent = IC_ORANGE

    # ── HEADER ────────────────────────────────────────────────
    draw.rectangle([(0, y), (ALERT_W, y + HDR_H)], fill=roll_accent)
    logo = _get_logo(logo_url, 78)
    lx2  = (ALERT_W - 78) // 2
    ly2  = y + (HDR_H - 78) // 2
    if logo:
        _paste_logo(img, logo, lx2, ly2)
    else:
        mto_w = _tw(draw, "MTO", fonts["med"])
        draw.text(((ALERT_W - mto_w) // 2, y + 36), "MTO", font=fonts["med"], fill=WHITE)
    aw = _tw(draw, "ROLL", fonts["sm"])
    draw.text(((ALERT_W - aw) // 2, y + HDR_H - 26), "ROLL", font=fonts["sm"], fill=WHITE)
    draw.rectangle([(ALERT_W, y), (W, y + HDR_H)], fill=WHITE)
    draw.rectangle([(ALERT_W, y), (ALERT_W + 4, y + HDR_H)], fill=roll_accent)
    tx = ALERT_W + 14
    draw.text((tx, y + 14), f"{open_strategy.short_name} / {open_strategy.name}", font=fonts["ttl"], fill=TEXT)
    draw.text((tx, y + 56), "ROLL DE POSICION", font=fonts["med"], fill=roll_accent)
    draw.text((tx, y + 80), account_name, font=fonts["xs"], fill=TEXT_GRAY)
    y += HDR_H

    # ── COMPANY ROW ───────────────────────────────────────────
    draw.rectangle([(0, y), (W, y + CO_H)], fill=WHITE)
    _hline(draw, y, 0, W, BORDER)
    _hline(draw, y + CO_H, 0, W, BORDER)
    ic_cx2, ic_cy2 = PAD + 18, y + CO_H // 2
    _icon_circle(draw, ic_cx2, ic_cy2, 18, BLUE_DEEP,
                 (co_name[0].upper() if co_name else ticker[0].upper()), fonts["med"])
    ncx2 = ic_cx2 + 26
    draw.text((ncx2, y + 10), co_name.upper() if co_name else ticker, font=fonts["med"], fill=TEXT)
    draw.text((ncx2, y + 32), f"({exchange}: {ticker})", font=fonts["xs"], fill=TEXT_GRAY)
    tb_x = W - PAD - 78
    _rrect(draw, (tb_x, y + 8, tb_x + 78, y + CO_H - 8), r=6, fill=BLUE_MID)
    tl_w = _tw(draw, "TICKER:", fonts["xs"])
    draw.text((tb_x + (78 - tl_w) // 2, y + 14), "TICKER:", font=fonts["xs"], fill=WHITE)
    tv_w = _tw(draw, ticker, fonts["lg"])
    draw.text((tb_x + (78 - tv_w) // 2, y + 30), ticker, font=fonts["lg"], fill=WHITE)
    y += CO_H + DIV

    def _sec_hdr(title: str, color) -> None:
        nonlocal y
        draw.rectangle([(0, y), (W, y + SEC_H)], fill=(244, 247, 252))
        _hline(draw, y, 0, W, BORDER)
        _icon_circle(draw, PAD + 14, y + SEC_H // 2, 12, color, title[0], fonts["xs"])
        tw = _tw(draw, title, fonts["med"])
        cx_title = (W - tw) // 2
        draw.text((cx_title, y + (SEC_H - 16) // 2), title, font=fonts["med"], fill=color)
        y += SEC_H

    def _data_row(label: str, value: str, ic_l: str, ic_c, v_color=TEXT, alt: bool = False) -> None:
        nonlocal y
        draw.rectangle([(0, y), (W, y + ROW_H)], fill=(248, 250, 253) if alt else WHITE)
        _hline(draw, y, 0, W, (228, 234, 242))
        rc = PAD + 15
        _icon_circle(draw, rc, y + ROW_H // 2, 13, ic_c, ic_l, fonts["xs"])
        draw.text((rc + 20, y + 6),  label, font=fonts["xs"], fill=TEXT_GRAY)
        draw.text((rc + 20, y + 22), value, font=fonts["med"], fill=v_color)
        y += ROW_H

    # ── CLOSE SECTION ─────────────────────────────────────────
    _sec_hdr("CIERRE DE POSICION", BLUE_DEEP)
    exp_v = _fdate(close_strategy.primary_expiry)
    if c_time:
        exp_v += f"  |  Hora: {c_time}"
    _data_row("VENCIMIENTO CERRADO", exp_v, "V", IC_RED)
    for i, p in enumerate(c_puts):
        act = "VENTA" if p.action == "SELL" else "COMPRA"
        _data_row(f"STRIKE {act} PUT", _fmt_strike(p.strike), "S", IC_ORANGE, alt=bool(i % 2))
    for i, c in enumerate(c_calls):
        act = "VENTA" if c.action == "SELL" else "COMPRA"
        _data_row(f"STRIKE {act} CALL", _fmt_strike(c.strike), "S", IC_TEAL, alt=bool(i % 2))

    # Close cost + result row
    draw.rectangle([(0, y), (W, y + ROW_H)], fill=WHITE)
    _hline(draw, y, 0, W, BORDER)
    _icon_circle(draw, PAD + 15, y + ROW_H // 2, 13, IC_RED, "C", fonts["xs"])
    draw.text((PAD + 36, y + 6),  "COSTE CIERRE", font=fonts["xs"], fill=TEXT_GRAY)
    draw.text((PAD + 36, y + 22), _fmoney(close_metrics.close_cost or close_cost), font=fonts["med"], fill=IC_RED)
    if close_metrics.trade_result is not None:
        draw.line([(MID, y + 8), (MID, y + ROW_H - 8)], fill=BORDER, width=1)
        _icon_circle(draw, MID + 15, y + ROW_H // 2, 13, IC_GREEN, "R", fonts["xs"])
        draw.text((MID + 36, y + 6),  "RESULTADO", font=fonts["xs"], fill=TEXT_GRAY)
        res_v = _fmoney(close_metrics.trade_result, sign=True)
        draw.text((MID + 36, y + 22), res_v, font=fonts["med"], fill=IC_GREEN if (close_metrics.trade_result or 0) >= 0 else IC_RED)
    y += ROW_H
    y += DIV

    # ── OPEN SECTION ──────────────────────────────────────────
    _sec_hdr("APERTURA DEL ROLL", IC_ORANGE)
    dte_v = f"  |  DTE: {open_metrics.dte} dias" if open_metrics.dte else ""
    _data_row("NUEVO VENCIMIENTO", f"{_fdate(open_strategy.primary_expiry)}{dte_v}", "V", IC_BLUE)
    for i, p in enumerate(o_puts):
        act = "VENTA" if p.action == "SELL" else "COMPRA"
        _data_row(f"STRIKE {act} PUT", _fmt_strike(p.strike), "S", IC_ORANGE, alt=bool(i % 2))
    for i, c in enumerate(o_calls):
        act = "VENTA" if c.action == "SELL" else "COMPRA"
        _data_row(f"STRIKE {act} CALL", _fmt_strike(c.strike), "S", IC_TEAL, alt=bool(i % 2))

    nc2 = open_strategy.num_contracts
    c_s = f"{nc2} contrato{'s' if nc2 != 1 else ''}"
    be_v = ""
    if open_metrics.breakeven is not None:
        be_v = (_fmoney(open_metrics.breakeven) if not open_metrics.breakeven_high
                else f"{_fmoney(open_metrics.breakeven)} / {_fmoney(open_metrics.breakeven_high)}")
    draw.rectangle([(0, y), (W, y + ROW_H)], fill=WHITE)
    _hline(draw, y, 0, W, BORDER)
    _icon_circle(draw, PAD + 15, y + ROW_H // 2, 13, IC_DEEP, "C", fonts["xs"])
    draw.text((PAD + 36, y + 6),  "CONTRATOS", font=fonts["xs"], fill=TEXT_GRAY)
    draw.text((PAD + 36, y + 22), c_s, font=fonts["med"], fill=TEXT)
    if be_v:
        draw.line([(MID, y + 8), (MID, y + ROW_H - 8)], fill=BORDER, width=1)
        _icon_circle(draw, MID + 15, y + ROW_H // 2, 13, IC_TEAL, "E", fonts["xs"])
        draw.text((MID + 36, y + 6),  "BREAK-EVEN", font=fonts["xs"], fill=TEXT_GRAY)
        draw.text((MID + 36, y + 22), be_v, font=fonts["med"], fill=IC_TEAL)
    y += ROW_H
    y += DIV

    # ── FINANCIAL DETAILS ─────────────────────────────────────
    draw.rectangle([(0, y), (W, y + FIN_H_H)], fill=BLUE_DEEP)
    _icon_circle(draw, PAD + 16, y + FIN_H_H // 2, 16, BLUE_MID, "$", fonts["med"])
    draw.text((PAD + 40, y + 8),  "DETALLES FINANCIEROS", font=fonts["med"], fill=WHITE)
    draw.text((PAD + 40, y + 28), "(por cada contrato abierto)", font=fonts["xs"], fill=(178, 212, 238))
    y += FIN_H_H

    for i, (ic_l, ic_c, lbl, val, pill) in enumerate(fin_rows):
        row_bg = WHITE if i % 2 == 0 else (246, 249, 253)
        draw.rectangle([(0, y), (W, y + FIN_R_H)], fill=row_bg)
        _hline(draw, y, 0, W, (228, 234, 242))
        _icon_circle(draw, PAD + 15, y + FIN_R_H // 2, 15, ic_c, ic_l, fonts["sm"])
        draw.text((PAD + 36, y + (FIN_R_H - 15) // 2), lbl, font=fonts["reg"], fill=TEXT_MED)
        _value_pill(draw, W - PAD, y + FIN_R_H // 2, val, pill, fonts["med"])
        y += FIN_R_H

    _hline(draw, y, 0, W, BORDER)
    y += DIV

    # ── NET ROLL RESULT ───────────────────────────────────────
    net_col  = IC_GREEN if net_result >= 0 else IC_RED
    net_pill = PILL_GREEN if net_result >= 0 else PILL_RED
    net_bg   = (232, 250, 240) if net_result >= 0 else (255, 232, 232)
    draw.rectangle([(0, y), (W, y + NET_H)], fill=net_bg)
    _hline(draw, y, 0, W, BORDER)
    draw.rectangle([(0, y), (4, y + NET_H)], fill=net_col)
    draw.text((PAD + 8, y + 10), "CREDITO NETO DEL ROLL", font=fonts["xs"], fill=TEXT_GRAY)
    net_txt = _fmoney(net_result, sign=True)
    ntw = _tw(draw, net_txt, fonts["ttl"])
    draw.text((W - PAD - ntw, y + (NET_H - 28) // 2), net_txt, font=fonts["ttl"], fill=net_col)
    y += NET_H
    y += DIV

    # ── FOOTER ────────────────────────────────────────────────
    draw.rectangle([(0, y), (W, y + FOOT_H)], fill=(255, 249, 215))
    _hline(draw, y, 0, W, (240, 215, 120))
    lb_cx2, lb_cy2 = PAD + 16, y + 28
    draw.ellipse([(lb_cx2 - 16, lb_cy2 - 16), (lb_cx2 + 16, lb_cy2 + 10)], fill=(255, 210, 30))
    draw.rectangle([(lb_cx2 - 8, lb_cy2 + 8), (lb_cx2 + 8, lb_cy2 + 18)], fill=(255, 210, 30))
    draw.text((lb_cx2 + 26, y + 8), "RECUERDA:", font=fonts["med"], fill=(165, 105, 10))
    disc = ("Las opciones implican riesgos significativos y no son adecuadas para todos "
            "los inversores. Asegurate de comprender los riesgos antes de operar.")
    disc_lines = _wrap_text(draw, disc, fonts["xs"], W - lb_cx2 - 44)
    ty5 = y + 28
    for line in disc_lines[:3]:
        draw.text((lb_cx2 + 26, ty5), line, font=fonts["xs"], fill=(130, 90, 15))
        ty5 += 15
    ts  = f"{today}  {o_time}" if o_time else today
    tsw = _tw(draw, ts, fonts["xs"])
    draw.text((W - PAD - tsw, y + FOOT_H - 16), ts, font=fonts["xs"], fill=TEXT_GRAY)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
