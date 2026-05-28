"""
Genera la imagen PNG del reporte semanal de evolución de cartera.
Replica el formato del Excel del usuario:
  - Tabla histórica de NAV (EVOLUCIÓN CARTERA 2026)
  - Tres tablas de composición por cuenta (fondo oscuro)
"""

import io
import os
from typing import Dict, List, Optional, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ── Fuentes ───────────────────────────────────────────────────
_FONT_CACHE: dict = {}

def _load_font(size: int, bold: bool = False) -> "ImageFont.ImageFont":
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    candidates = [
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"               if bold else
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"        if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            f = ImageFont.truetype(path, size)
            _FONT_CACHE[key] = f
            return f
    return ImageFont.load_default()

# ── Colores ───────────────────────────────────────────────────
C_WHITE      = (255, 255, 255)
C_BLACK      = (0,   0,   0)
C_BORDER     = (160, 160, 160)
C_HDR_BG     = (215, 225, 242)   # Azul-gris claro (cabecera evolución)
C_DARK_BG    = (22,  22,  38)    # Fondo oscuro composición
C_DARK_ROW   = (30,  30,  48)    # Fila alternativa
C_DARK_HDR   = (35,  35,  55)    # Cabecera oscura
C_DARK_TOTAL = (18,  18,  32)    # Fila Total oscura
C_DARK_TEXT  = (220, 220, 230)   # Texto sobre fondo oscuro
C_DARK_LABEL = (140, 140, 160)   # Etiquetas cabecera oscura
C_GREEN      = (34,  139, 34)
C_RED        = (200, 40,  40)
C_TITLE      = (0,   0,   0)

# Colores de los cuadrados de instrumento
COMP_COLORS = {
    "Acciones": (66,  133, 244),   # Azul
    "Opciones": (219, 116,  56),   # Naranja
    "Efectivo": (52,  168,  83),   # Verde
    "Devengos": (155, 155,  20),   # Oliva
}

# ── Dimensiones ───────────────────────────────────────────────
PAD       = 14    # Padding exterior
GAP       = 10    # Gap entre secciones
ROW_EVO   = 21    # Alto fila tabla evolución
ROW_COMP  = 27    # Alto fila tabla composición
SQ        = 11    # Lado del cuadrado de color en composición
TITLE_H   = 36    # Alto del título "EVOLUCIÓN CARTERA 2026"

# Anchos columnas tabla evolución
EVO_COLS = [
    ("Fecha",        90),
    ("Cuenta 10K",  112),
    ("Cuenta 50k",  112),
    ("Div-ETF 5K",  100),
    ("Total",       112),
    ("Diferencial",  80),
]
EVO_TABLE_W = sum(w for _, w in EVO_COLS)   # 606 px

# Columnas composición
COMP_NAME_W = EVO_TABLE_W - 165
COMP_VAL_W  = 165

IMG_W = EVO_TABLE_W + 2 * PAD   # 634 px


def generate_portfolio_image(
    snapshots:    List[Dict],              # [{date, accounts:{name:val}, total}]
    composition:  Dict[str, Dict[str, float]],  # {acc_name: {instrument: val}}
    account_order: List[str],             # nombres internos en orden
    display_names: Dict[str, str],        # {interno: corto}
    year:         int = 2026,
) -> Optional[bytes]:
    if not PIL_AVAILABLE:
        return None

    # ── Calcular altura total ─────────────────────────────────
    n_snap   = min(len(snapshots), 12)
    evo_h    = TITLE_H + ROW_EVO + n_snap * ROW_EVO   # título + header + filas

    comp_h = 0
    for acc in account_order:
        disp = display_names.get(acc, acc)
        instr = composition.get(acc, {})
        n_rows = len(instr) + 1   # instrumentos + Total
        comp_h += 20 + ROW_COMP + n_rows * ROW_COMP + 6   # título + header + rows + gap

    total_h = PAD + evo_h + GAP + comp_h + PAD

    img  = Image.new("RGB", (IMG_W, total_h), C_WHITE)
    draw = ImageDraw.Draw(img)

    fn_title  = _load_font(15, bold=True)
    fn_hdr    = _load_font(10, bold=True)
    fn_data   = _load_font(11, bold=False)
    fn_bold   = _load_font(11, bold=True)
    fn_ctitle = _load_font(12, bold=False)
    fn_dark_h = _load_font(10, bold=True)
    fn_dark_d = _load_font(12, bold=False)
    fn_dark_b = _load_font(12, bold=True)

    def _tw(text, font):
        bb = draw.textbbox((0, 0), text, font=font)
        return bb[2] - bb[0]

    def _th(font):
        bb = draw.textbbox((0, 0), "A", font=font)
        return bb[3] - bb[1]

    def _txt_r(text, x, y, w, h, font, color=C_BLACK):
        tw = _tw(text, font)
        th = _th(font)
        draw.text((x + w - tw - 4, y + (h - th) // 2), text, fill=color, font=font)

    def _txt_l(text, x, y, h, font, color=C_BLACK):
        th = _th(font)
        draw.text((x + 4, y + (h - th) // 2), text, fill=color, font=font)

    def _txt_c(text, x, y, w, h, font, color=C_BLACK):
        tw = _tw(text, font)
        th = _th(font)
        draw.text((x + (w - tw) // 2, y + (h - th) // 2), text, fill=color, font=font)

    y = PAD

    # ════════════════════════════════════════════════════════
    # SECCIÓN 1: EVOLUCIÓN DE CARTERA
    # ════════════════════════════════════════════════════════

    # ── Título centrado con subrayado ──
    title_txt = f"EVOLUCIÓN CARTERA {year}"
    tw = _tw(title_txt, fn_title)
    tx = PAD + (EVO_TABLE_W - tw) // 2
    ty = y + (TITLE_H - _th(fn_title)) // 2
    draw.text((tx, ty), title_txt, fill=C_TITLE, font=fn_title)
    draw.line([tx, ty + _th(fn_title) + 4, tx + tw, ty + _th(fn_title) + 4],
              fill=C_TITLE, width=1)
    y += TITLE_H

    # ── Cabecera de la tabla ──
    x = PAD
    draw.rectangle([x, y, PAD + EVO_TABLE_W - 1, y + ROW_EVO - 1], fill=C_HDR_BG)
    for col_name, col_w in EVO_COLS:
        draw.rectangle([x, y, x + col_w - 1, y + ROW_EVO - 1], outline=C_BORDER)
        _txt_c(col_name, x, y, col_w, ROW_EVO, fn_hdr)
        x += col_w
    y += ROW_EVO

    # ── Filas de datos (más reciente primero, máx 12) ──
    recent = list(reversed(snapshots))[:12]
    for i, snap in enumerate(recent):
        x  = PAD
        bg = C_WHITE
        draw.rectangle([PAD, y, PAD + EVO_TABLE_W - 1, y + ROW_EVO - 1], fill=bg)

        accs = snap.get("accounts", {})
        total = snap.get("total", sum(accs.values()))
        diff  = snap.get("diff_pct")

        # Fecha
        draw.rectangle([x, y, x + EVO_COLS[0][1] - 1, y + ROW_EVO - 1], outline=C_BORDER)
        _txt_c(snap["date"], x, y, EVO_COLS[0][1], ROW_EVO, fn_data)
        x += EVO_COLS[0][1]

        # Valores por cuenta
        for acc_name, (_, col_w) in zip(account_order, EVO_COLS[1:4]):
            v = accs.get(acc_name, 0.0)
            draw.rectangle([x, y, x + col_w - 1, y + ROW_EVO - 1], outline=C_BORDER)
            _txt_r(_fmt_dollar(v), x, y, col_w, ROW_EVO, fn_data)
            x += col_w

        # Total
        _, tw_col = EVO_COLS[4]
        draw.rectangle([x, y, x + tw_col - 1, y + ROW_EVO - 1], outline=C_BORDER)
        _txt_r(_fmt_dollar(total), x, y, tw_col, ROW_EVO, fn_bold)
        x += tw_col

        # Diferencial
        _, dw = EVO_COLS[5]
        draw.rectangle([x, y, x + dw - 1, y + ROW_EVO - 1], outline=C_BORDER)
        if diff is not None:
            d_txt  = f"{diff:+.2f}%"
            d_col  = C_GREEN if diff >= 0 else C_RED
            _txt_r(d_txt, x, y, dw, ROW_EVO, fn_bold, color=d_col)
        y += ROW_EVO

    y += GAP

    # ════════════════════════════════════════════════════════
    # SECCIÓN 2: COMPOSICIÓN POR CUENTA
    # ════════════════════════════════════════════════════════

    for acc_name in account_order:
        disp  = display_names.get(acc_name, acc_name)
        instr = composition.get(acc_name, {})
        total_comp = sum(instr.values())

        n_rows  = len(instr) + 1   # instrumentos + Total
        table_h = ROW_COMP + n_rows * ROW_COMP

        # ── Título de la tabla (sobre fondo blanco) ──
        draw.text((PAD, y + 4), f"Composición cuenta {disp}", fill=C_BLACK, font=fn_ctitle)
        y += 20

        # ── Fondo de la tabla ──
        draw.rectangle([PAD, y, PAD + EVO_TABLE_W - 1, y + table_h - 1], fill=C_DARK_BG)

        # ── Cabecera oscura ──
        draw.rectangle([PAD, y, PAD + EVO_TABLE_W - 1, y + ROW_COMP - 1], fill=C_DARK_HDR)
        _txt_l("INSTRUMENTO FINANCIERO", PAD + 30, y, ROW_COMP, fn_dark_h, color=C_DARK_LABEL)
        _txt_r("VALOR ACTUAL", PAD, y, EVO_TABLE_W, ROW_COMP, fn_dark_h, color=C_DARK_LABEL)
        y += ROW_COMP

        # ── Filas de instrumentos ──
        instrument_order = ["Acciones", "Opciones", "Efectivo", "Devengos"]
        for j, inst in enumerate(instrument_order):
            if inst not in instr:
                continue
            val   = instr[inst]
            color = COMP_COLORS.get(inst, (180, 180, 180))
            row_bg = C_DARK_ROW if j % 2 == 1 else C_DARK_BG

            draw.rectangle([PAD, y, PAD + EVO_TABLE_W - 1, y + ROW_COMP - 1], fill=row_bg)

            # Cuadrado de color
            sq_x = PAD + 10
            sq_y = y + (ROW_COMP - SQ) // 2
            draw.rectangle([sq_x, sq_y, sq_x + SQ, sq_y + SQ], fill=color)

            # Nombre instrumento
            _txt_l(inst, PAD + 28, y, ROW_COMP, fn_dark_d, color=C_DARK_TEXT)

            # Valor
            v_txt = _fmt_us(val)
            v_col = C_DARK_TEXT if val >= 0 else (220, 100, 100)
            _txt_r(v_txt, PAD, y, EVO_TABLE_W, ROW_COMP, fn_dark_d, color=v_col)
            y += ROW_COMP

        # ── Fila Total ──
        draw.rectangle([PAD, y, PAD + EVO_TABLE_W - 1, y + ROW_COMP - 1], fill=C_DARK_TOTAL)
        _txt_l("Total", PAD + 28, y, ROW_COMP, fn_dark_b, color=C_DARK_TEXT)
        _txt_r(_fmt_us(total_comp), PAD, y, EVO_TABLE_W, ROW_COMP, fn_dark_b, color=C_DARK_TEXT)
        y += ROW_COMP
        y += 6   # pequeño gap entre tablas

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(150, 150))
    return buf.getvalue()


# ── Formatos numéricos ────────────────────────────────────────

def _fmt_dollar(v: float) -> str:
    """$9,933.86 (formato americano con $)"""
    return f"${v:,.2f}"

def _fmt_us(v: float) -> str:
    """9,933.86 (formato americano sin $, negativo con -)"""
    if abs(v) < 0.005:
        return "-"
    return f"{v:,.2f}"
