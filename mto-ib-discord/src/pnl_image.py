"""
Generador de imagen PNG para el reporte semanal de P&L.
Replica el formato de la tabla Excel del usuario.
"""

import io
import os
from typing import Dict, List, Tuple, Optional

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ── Fuentes (mismas rutas que card_generator.py) ──────────────
_FONT_CACHE: dict = {}

def _load_font(size: int, bold: bool = False) -> "ImageFont.ImageFont":
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    candidates = [
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"          if bold else
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"   if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            font = ImageFont.truetype(path, size)
            _FONT_CACHE[key] = font
            return font
    return ImageFont.load_default()

# ── Dimensiones ───────────────────────────────────────────────
PAD       = 12    # Padding exterior de la imagen
ROW_H     = 22    # Alto de fila normal
HALF_H    = 7     # Alto de la fila vacía entre datos y Total
GAP_H     = 16    # Separación vertical entre las dos tablas
FN_H      = 16    # Alto de cada línea de nota al pie

COL_NAME  = 170   # Columna de nombres
COL_TOTAL = 95    # Columna Total
DB_W      = 4     # Ancho del separador doble
COL_MON   = 88    # Cada columna mensual

FONT_SZ   = 12
FONT_NOTE = 11

# ── Colores (estilo Excel blanco) ─────────────────────────────
C_WHITE     = (255, 255, 255)
C_BLACK     = (0,   0,   0)
C_HDR_BG    = (220, 230, 241)   # Azul-gris claro (cabecera, igual que Excel)
C_BORDER    = (120, 120, 120)   # Gris medio para bordes
C_DB_LINE   = (60,  60,  60)    # Línea doble más oscura
C_NOTE_TXT  = (60,  60,  60)    # Texto de notas (gris oscuro)
C_NOTE_BLUE = (31,  73, 125)    # Azul Excel para la segunda nota

# ── Footnotes (idénticas al Excel) ───────────────────────────
_FN_R = [
    "* Importe correspondiente a operaciones cerradas, "
    "la fecha del beneficio se computa en la fecha de cierre.",
    "* El valor de estos saldos solo se puede modificar por el mes en curso, "
    "a mes cerrado es invariable.",
]
_FN_C = [
    "* Efectivo de primas cobradas de operaciones no cerradas, "
    "a medida que se cierran operaciones, este valor tiende a cero.",
    "* El valor de los meses cerrados, se modifica en tanto se van cerrando "
    "operaciones, siempre debe tender a cero.",
]


def generate_pnl_image(
    headers:        List[str],
    accounts:       List[str],
    display_names:  Dict[str, str],
    fixed_rows:     List[str],
    realized_table: Dict[str, List[float]],
    cash_table:     Dict[str, List[float]],
) -> Optional[bytes]:
    """
    Genera la imagen PNG con las dos tablas de P&L.

    headers       : columnas de mes, de más reciente (izq) a más antiguo (der)
    accounts      : nombres internos de cuenta (en orden de visualización)
    display_names : {nombre_interno: etiqueta_corta}
    fixed_rows    : filas fijas siempre presentes (ej. "Op. Sueltas")
    realized_table: {nombre_interno: [val_col0, val_col1, ...]}
    cash_table    : igual para efectivo cobrado
    """
    if not PIL_AVAILABLE:
        return None

    n_mon    = len(headers)
    n_data   = len(accounts) + len(fixed_rows)
    n_cols   = 1 + n_mon   # Total + months

    # Dimensiones de una tabla
    table_w  = PAD + COL_NAME + COL_TOTAL + DB_W + n_mon * COL_MON + PAD
    table_h  = ROW_H + n_data * ROW_H + HALF_H + ROW_H + len(_FN_R) * FN_H

    total_w  = table_w
    total_h  = PAD + table_h + GAP_H + table_h + PAD

    img  = Image.new("RGB", (total_w, total_h), C_WHITE)
    draw = ImageDraw.Draw(img)

    fn_reg  = _load_font(FONT_SZ,   bold=False)
    fn_bold = _load_font(FONT_SZ,   bold=True)
    fn_note = _load_font(FONT_NOTE, bold=False)

    # ── Posiciones X de las columnas ─────────────────────────
    x0 = PAD
    x_name  = x0
    x_total = x_name  + COL_NAME
    x_db    = x_total + COL_TOTAL          # inicio separador doble
    x_mon0  = x_db    + DB_W               # inicio primera columna mensual
    x_mons  = [x_mon0 + i * COL_MON for i in range(n_mon)]
    x_right = x_mons[-1] + COL_MON if x_mons else x_total + COL_TOTAL

    def _draw_table(
        y0:         int,
        title:      str,
        table:      Dict[str, List[float]],
        footnotes:  List[str],
    ) -> int:
        """Dibuja una tabla y devuelve la y final (después de las notas)."""
        y = y0

        # ── Helpers internos ─────────────────────────────────

        def _cell(x, y, w, h, bg=C_WHITE, border=True):
            draw.rectangle([x, y, x + w - 1, y + h - 1], fill=bg)
            if border:
                draw.rectangle([x, y, x + w - 1, y + h - 1],
                               outline=C_BORDER)

        def _txt_r(text, x, y, w, h, font=fn_reg, color=C_BLACK):
            """Texto alineado a la derecha en la celda."""
            bb = draw.textbbox((0, 0), text, font=font)
            tw = bb[2] - bb[0]
            th = bb[3] - bb[1]
            tx = x + w - tw - 4
            ty = y + (h - th) // 2
            draw.text((tx, ty), text, fill=color, font=font)

        def _txt_l(text, x, y, w, h, font=fn_reg, color=C_BLACK):
            """Texto alineado a la izquierda en la celda."""
            bb = draw.textbbox((0, 0), text, font=font)
            th = bb[3] - bb[1]
            ty = y + (h - th) // 2
            draw.text((x + 4, ty), text, fill=color, font=font)

        def _db_sep(y, h, bg=C_WHITE):
            """Dibuja el separador doble vertical entre Total y meses."""
            draw.rectangle([x_db, y, x_db + DB_W - 1, y + h - 1], fill=bg)
            draw.line([x_db,            y, x_db,            y + h - 1], fill=C_DB_LINE)
            draw.line([x_db + DB_W - 1, y, x_db + DB_W - 1, y + h - 1], fill=C_DB_LINE)

        # ── Fila de cabecera ──────────────────────────────────
        _cell(x_name,  y, COL_NAME,  ROW_H, bg=C_HDR_BG)
        _txt_l(title, x_name, y, COL_NAME, ROW_H, font=fn_bold)

        _cell(x_total, y, COL_TOTAL, ROW_H, bg=C_HDR_BG)
        _txt_r("Total", x_total, y, COL_TOTAL, ROW_H, font=fn_bold)

        _db_sep(y, ROW_H, bg=C_HDR_BG)

        for i, h_name in enumerate(headers):
            _cell(x_mons[i], y, COL_MON, ROW_H, bg=C_HDR_BG)
            _txt_r(h_name, x_mons[i], y, COL_MON, ROW_H, font=fn_bold)

        y += ROW_H

        # ── Filas de datos ────────────────────────────────────
        all_rows: List[Tuple[str, bool]] = (
            [(a, False) for a in accounts] +
            [(f, True)  for f in fixed_rows]
        )
        for acc, is_fixed in all_rows:
            if is_fixed:
                vals  = [0.0] * n_mon
                total = 0.0
                label = acc
            else:
                vals  = table.get(acc, [0.0] * n_mon)
                total = sum(vals)
                label = display_names.get(acc, acc)

            _cell(x_name,  y, COL_NAME,  ROW_H)
            _txt_l(label, x_name, y, COL_NAME, ROW_H)

            _cell(x_total, y, COL_TOTAL, ROW_H)
            _txt_r(_fmt_eu(total), x_total, y, COL_TOTAL, ROW_H)

            _db_sep(y, ROW_H)

            for i, v in enumerate(vals):
                _cell(x_mons[i], y, COL_MON, ROW_H)
                _txt_r(_fmt_eu(v), x_mons[i], y, COL_MON, ROW_H)

            y += ROW_H

        # ── Fila vacía ────────────────────────────────────────
        draw.rectangle([x_name, y, x_right - 1, y + HALF_H - 1], fill=C_WHITE)
        draw.line([x_name,    y,            x_right - 1, y],            fill=C_BORDER)
        draw.line([x_name,    y + HALF_H-1, x_right - 1, y + HALF_H-1], fill=C_BORDER)
        draw.line([x_name,    y,            x_name,       y + HALF_H-1], fill=C_BORDER)
        draw.line([x_right-1, y,            x_right-1,    y + HALF_H-1], fill=C_BORDER)
        y += HALF_H

        # ── Fila Total ────────────────────────────────────────
        grand = [
            sum(table.get(a, [0.0]*n_mon)[i] for a in accounts)
            for i in range(n_mon)
        ]
        gt = sum(grand)

        _cell(x_name,  y, COL_NAME,  ROW_H)
        _txt_l("Total", x_name, y, COL_NAME, ROW_H, font=fn_bold)

        _cell(x_total, y, COL_TOTAL, ROW_H)
        _txt_r(_fmt_eu(gt), x_total, y, COL_TOTAL, ROW_H, font=fn_bold)

        _db_sep(y, ROW_H)

        for i, v in enumerate(grand):
            _cell(x_mons[i], y, COL_MON, ROW_H)
            _txt_r(_fmt_eu(v), x_mons[i], y, COL_MON, ROW_H, font=fn_bold)

        y += ROW_H

        # ── Notas al pie ──────────────────────────────────────
        y += 3
        for fn_line in footnotes:
            draw.text((x_name, y), fn_line, fill=C_NOTE_TXT, font=fn_note)
            y += FN_H

        return y

    # ── Dibujar las dos tablas ────────────────────────────────
    y1 = _draw_table(PAD,            "Beneficio realizado *", realized_table, _FN_R)
    _draw_table(y1 + GAP_H,         "Efectivo cobrado *",    cash_table,     _FN_C)

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(150, 150))
    return buf.getvalue()


# ── Formateo numérico europeo ─────────────────────────────────

def _fmt_eu(v: float) -> str:
    """1374.31 → '1.374,31' | -312.34 → '- 312,34' | 0 → '-'"""
    if abs(v) < 0.005:
        return "-"
    raw = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"- {raw}" if v < 0 else raw
