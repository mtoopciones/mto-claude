"""
Utilidades compartidas para el envío de emails con logo inline y firma corporativa.
"""

from __future__ import annotations

import os
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from loguru import logger

_LOGO_PATH = "data/mto_logo.png"

# Firma corporativa (usa cid:mto_logo para la imagen inline)
FIRMA_HTML = """
<table cellpadding="0" cellspacing="0" border="0"
       style="font-family:Calibri,'Gill Sans',Arial,sans-serif;font-size:11pt;color:#000000;margin-top:8px;">
  <tr><td>
    <strong>Toni Faura</strong><br>
    Dirección financiera<br>
    <a href="mailto:info@mtoopciones.com" style="color:#1155CC;text-decoration:none;">info@mtoopciones.com</a><br>
    <a href="https://www.mtoopciones.com"  style="color:#1155CC;text-decoration:none;">www.mtoopciones.com</a>
  </td></tr>
  <tr><td style="padding-top:8px;">
    <img src="cid:mto_logo" alt="MTO Opciones" width="90" style="display:block;">
  </td></tr>
  <tr><td style="padding-top:14px;font-size:7.5pt;color:#555555;
                 border-top:1px solid #cccccc;max-width:580px;line-height:1.4;">
    <strong>AVISO LEGAL:</strong> Este mensaje y sus archivos adjuntos van dirigidos exclusivamente
    a su destinatario, pudiendo contener información confidencial sometida a secreto profesional.
    No está permitida su comunicación, reproducción o distribución sin la autorización expresa de
    MTO OPCIONES, SL. Si usted no es el destinatario final, por favor elimínelo e infórmenos por
    esta vía.<br><br>
    <strong>PROTECCIÓN DE DATOS:</strong> De conformidad con lo dispuesto en el Reglamento (UE)
    2016/679 de 27 de abril de 2016 (GDPR), le informamos que los datos personales y dirección de
    correo electrónico del interesado, serán tratados bajo la responsabilidad de MTO OPCIONES, SL
    para el envío de comunicaciones sobre nuestros productos y servicios y se conservarán mientras
    exista un interés mutuo para ello. Los datos no serán comunicados a terceros, salvo obligación
    legal. Le informamos que puede ejercer los derechos de acceso, rectificación, portabilidad y
    supresión de sus datos y los de limitación y oposición a su tratamiento dirigiéndose a
    C. San Lorenzo, núm. 14, 1º - 07840 SANTA EULALIA DEL RIO (ISLAS BALEARES).
    Email: <a href="mailto:info@mtoopciones.com" style="color:#1155CC;">info@mtoopciones.com</a>.
    Si considera que el tratamiento no se ajusta a la normativa vigente, podrá presentar una
    reclamación ante la autoridad de control en
    <a href="https://www.agpd.es" style="color:#1155CC;">www.agpd.es</a>.<br><br>
    <strong>PUBLICIDAD:</strong> En cumplimiento de lo previsto en el artículo 21 de la Ley
    34/2002 de Servicios de la Sociedad de la Información y Comercio Electrónico (LSSICE), si
    usted no desea recibir más información sobre nuestros productos y/o servicios, puede darse de
    baja enviando un correo electrónico a
    <a href="mailto:info@mtoopciones.com" style="color:#1155CC;">info@mtoopciones.com</a>,
    indicando en el Asunto <strong>"BAJA"</strong> o <strong>"NO ENVIAR"</strong>.
  </td></tr>
</table>
"""


def load_logo() -> Optional[bytes]:
    """Carga el logo MTO desde disco. Devuelve None si no existe."""
    try:
        with open(_LOGO_PATH, "rb") as f:
            return f.read()
    except Exception as e:
        logger.debug(f"email_utils: logo no encontrado en {_LOGO_PATH}: {e}")
        return None


def build_message(
    from_addr:   str,
    to_addr:     str,
    subject:     str,
    html_body:   str,
    attachments: list | None = None,
    cc_addr:     str | None  = None,
    reply_to:    str | None  = None,
    from_name:   str | None  = None,
) -> MIMEMultipart:
    """
    Construye un mensaje MIME con imagen inline (logo) y adjuntos opcionales.

    Estructura:
      MIMEMultipart('mixed')
        └─ MIMEMultipart('related')
              ├─ MIMEText(html_body, 'html')
              └─ MIMEImage(logo_bytes)   ← inline, cid:mto_logo
        └─ MIMEApplication(...)          ← adjuntos (uno por cada elemento de attachments)

    attachments: lista de tuplas (bytes, subtype, filename)
                 ej. [(pdf_bytes, 'pdf', 'FAQs.pdf'), (xlsx_bytes, 'vnd...', 'data.xlsx')]
    """
    logo_bytes = load_logo()

    from_header = f"{from_name} <{from_addr}>" if from_name else from_addr

    msg_outer = MIMEMultipart("mixed")
    msg_outer["From"]    = from_header
    msg_outer["To"]      = to_addr
    msg_outer["Subject"] = subject
    if cc_addr:
        msg_outer["CC"] = cc_addr
    if reply_to:
        msg_outer["Reply-To"] = reply_to

    msg_related = MIMEMultipart("related")
    msg_related.attach(MIMEText(html_body, "html", "utf-8"))

    if logo_bytes:
        img = MIMEImage(logo_bytes, _subtype="png")
        img.add_header("Content-ID",          "<mto_logo>")
        img.add_header("Content-Disposition", "inline", filename="mto_logo.png")
        msg_related.attach(img)

    msg_outer.attach(msg_related)

    for att_bytes, att_subtype, att_filename in (attachments or []):
        from email.mime.application import MIMEApplication
        part = MIMEApplication(att_bytes, _subtype=att_subtype, Name=att_filename)
        part.add_header("Content-Disposition", "attachment", filename=att_filename)
        msg_outer.attach(part)

    return msg_outer
