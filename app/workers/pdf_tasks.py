from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any, Dict
from uuid import UUID

from app.config import get_settings
from app.workers.celery_app import celery_app

settings = get_settings()
logger = logging.getLogger(__name__)


def _build_invoice_pdf(invoice_data: Dict[str, Any]) -> bytes:
    """Build an invoice PDF using ReportLab and return bytes."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
    )

    styles = getSampleStyleSheet()
    story = []

    # Header
    header_data = [
        ["ENOVAR", f"FACTURE N° {invoice_data.get('reference', 'N/A')}"],
        ["Plateforme de tutorat en ligne", f"Date: {invoice_data.get('date', datetime.utcnow().strftime('%d/%m/%Y'))}"],
        ["contact@enovar.dz", ""],
    ]
    header_table = Table(header_data, colWidths=[10 * cm, 7 * cm])
    header_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (0, 0), 18),
        ("FONTNAME", (1, 0), (1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (1, 0), (1, 0), 14),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 1 * cm))

    # Client info
    story.append(Paragraph("<b>Facturé à :</b>", styles["Normal"]))
    story.append(Paragraph(invoice_data.get("client_name", "Client"), styles["Normal"]))
    story.append(Paragraph(invoice_data.get("client_email", ""), styles["Normal"]))
    story.append(Spacer(1, 0.5 * cm))

    # Items table
    items_data = [["Description", "Durée", "Prix unitaire", "Total"]]
    subject = invoice_data.get("subject", "Session de tutorat")
    teacher = invoice_data.get("teacher_name", "")
    duration = invoice_data.get("duration_minutes", 60)
    amount = invoice_data.get("amount_dzd", 0)
    items_data.append([
        f"{subject} avec {teacher}",
        f"{duration} min",
        f"{amount} DZD",
        f"{amount} DZD",
    ])

    items_table = Table(items_data, colWidths=[8 * cm, 3 * cm, 3 * cm, 3 * cm])
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4F46E5")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 0.5 * cm))

    # Total
    total_data = [
        ["", "", "Total TTC :", f"{amount} DZD"],
    ]
    total_table = Table(total_data, colWidths=[8 * cm, 3 * cm, 3 * cm, 3 * cm])
    total_table.setStyle(TableStyle([
        ("FONTNAME", (2, 0), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
    ]))
    story.append(total_table)
    story.append(Spacer(1, 1 * cm))

    # Footer
    story.append(Paragraph(
        "<font size=9 color='grey'>Enovar - Plateforme de tutorat en ligne - Algérie | "
        "contact@enovar.dz | enovar.dz</font>",
        styles["Normal"],
    ))

    doc.build(story)
    return buffer.getvalue()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=120)
def generate_invoice_pdf(self, invoice_id: str) -> str:
    """Generate an invoice PDF, upload to R2, and save URL in DB."""
    from app.database import get_session
    from app.models.payment import Payment
    from app.services.storage import upload_file
    from sqlmodel import select

    try:
        # Fetch payment record
        with next(get_session()) as db:
            uid = UUID(invoice_id)
            statement = select(Payment).where(Payment.id == uid)
            payment = db.exec(statement).first()

            if payment is None:
                logger.error("Payment %s not found", invoice_id)
                return ""

            invoice_data: Dict[str, Any] = {
                "reference": str(payment.id)[:8].upper(),
                "date": payment.created_at.strftime("%d/%m/%Y"),
                "amount_dzd": payment.amount,
                "client_name": "Client Enovar",
                "client_email": "",
            }

        pdf_bytes = _build_invoice_pdf(invoice_data)
        filename = f"invoice_{invoice_id}.pdf"
        public_url = upload_file(
            file_bytes=pdf_bytes,
            filename=filename,
            content_type="application/pdf",
            folder="invoices",
        )

        # Save URL back to payment record
        with next(get_session()) as db:
            statement = select(Payment).where(Payment.id == uid)
            payment = db.exec(statement).first()
            if payment:
                payment.invoice_url = public_url
                db.add(payment)
                db.commit()

        logger.info("Invoice PDF generated and uploaded: %s", public_url)
        return public_url

    except Exception as exc:
        logger.error("Failed to generate invoice PDF for %s: %s", invoice_id, exc)
        raise self.retry(exc=exc)
