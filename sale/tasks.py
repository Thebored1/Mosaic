"""Celery tasks for sales document generation."""

import logging

from celery import shared_task

from .models import Invoice

logger = logging.getLogger(__name__)


@shared_task
def generate_invoice_documents(invoice_id):
    """Generate the placeholder e-way bill and e-invoice payload for an invoice."""
    logger.info('Generating invoice documents invoice_id=%s', invoice_id)
    invoice = Invoice.objects.get(pk=invoice_id)
    result = {
        'e_way_bill': invoice.generate_e_way_bill(),
        'e_invoice_details': invoice.generate_e_invoice(),
    }
    logger.info('Generated invoice documents invoice_id=%s', invoice_id)
    return result
