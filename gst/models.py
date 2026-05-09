"""GST compliance persistence models."""

from django.conf import settings
from django.db import models


class GSTReturnFiling(models.Model):
    """Prepared or submitted GST return payload for one GSTIN and period."""

    RETURN_TYPES = [
        ('GSTR1', 'GSTR-1'),
        ('GSTR3B', 'GSTR-3B'),
        ('GSTR9', 'GSTR-9'),
    ]
    STATUS_CHOICES = [
        ('Draft', 'Draft'),
        ('Ready', 'Ready'),
        ('Submitted', 'Submitted'),
        ('Filed', 'Filed'),
        ('Failed', 'Failed'),
    ]

    organization = models.ForeignKey('account.Organization', on_delete=models.CASCADE, related_name='gst_returns')
    gstin = models.CharField(max_length=15)
    return_type = models.CharField(max_length=10, choices=RETURN_TYPES)
    period = models.CharField(max_length=20, help_text='MMYYYY for monthly returns, YYYY-YYYY for annual returns')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    payload = models.JSONField(default=dict, blank=True)
    validation = models.JSONField(default=dict, blank=True)
    provider = models.CharField(max_length=30, blank=True)
    provider_reference = models.CharField(max_length=120, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('organization', 'gstin', 'return_type', 'period')
        ordering = ['-updated_at']

    def __str__(self):
        return f'{self.return_type} {self.gstin} {self.period}'


class GSTEInvoice(models.Model):
    """E-invoice IRN lifecycle record for a sales invoice."""

    STATUS_CHOICES = [
        ('Draft', 'Draft'),
        ('Ready', 'Ready'),
        ('Generated', 'Generated'),
        ('Cancelled', 'Cancelled'),
        ('Failed', 'Failed'),
    ]

    organization = models.ForeignKey('account.Organization', on_delete=models.CASCADE, related_name='gst_e_invoices')
    invoice = models.OneToOneField('sale.Invoice', on_delete=models.CASCADE, related_name='gst_e_invoice')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    irn = models.CharField(max_length=64, blank=True)
    ack_no = models.CharField(max_length=30, blank=True)
    ack_date = models.DateTimeField(null=True, blank=True)
    signed_invoice = models.TextField(blank=True)
    signed_qr_code = models.TextField(blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'E-Invoice {self.invoice.invoice_number}'


class GSTEWayBill(models.Model):
    """E-way bill lifecycle record for a sales invoice."""

    STATUS_CHOICES = [
        ('Draft', 'Draft'),
        ('Ready', 'Ready'),
        ('Generated', 'Generated'),
        ('Cancelled', 'Cancelled'),
        ('Failed', 'Failed'),
    ]

    organization = models.ForeignKey('account.Organization', on_delete=models.CASCADE, related_name='gst_e_way_bills')
    invoice = models.OneToOneField('sale.Invoice', on_delete=models.CASCADE, related_name='gst_e_way_bill')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    ewb_no = models.CharField(max_length=20, blank=True)
    ewb_date = models.DateTimeField(null=True, blank=True)
    valid_upto = models.DateTimeField(null=True, blank=True)
    transporter_id = models.CharField(max_length=15, blank=True)
    transporter_name = models.CharField(max_length=120, blank=True)
    transport_mode = models.CharField(max_length=10, blank=True)
    transport_doc_no = models.CharField(max_length=30, blank=True)
    transport_doc_date = models.DateField(null=True, blank=True)
    vehicle_no = models.CharField(max_length=20, blank=True)
    distance_km = models.PositiveIntegerField(default=0)
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'E-Way Bill {self.invoice.invoice_number}'


class GSTIntegrationRequest(models.Model):
    """Provider request/response audit log for GST integrations."""

    STATUS_CHOICES = [
        ('Prepared', 'Prepared'),
        ('Sent', 'Sent'),
        ('Succeeded', 'Succeeded'),
        ('Failed', 'Failed'),
    ]

    organization = models.ForeignKey('account.Organization', on_delete=models.CASCADE, related_name='gst_integration_requests')
    provider = models.CharField(max_length=30, default='sandbox')
    operation = models.CharField(max_length=80)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Prepared')
    endpoint = models.CharField(max_length=255, blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    status_code = models.PositiveIntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    return_filing = models.ForeignKey(GSTReturnFiling, on_delete=models.SET_NULL, null=True, blank=True)
    e_invoice = models.ForeignKey(GSTEInvoice, on_delete=models.SET_NULL, null=True, blank=True)
    e_way_bill = models.ForeignKey(GSTEWayBill, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.provider} {self.operation} {self.status}'


class TallyExport(models.Model):
    """Generated Tally import file content for an organization."""

    EXPORT_TYPES = [
        ('SalesVoucherXML', 'Sales Voucher XML'),
    ]
    STATUS_CHOICES = [
        ('Generated', 'Generated'),
        ('Failed', 'Failed'),
    ]

    organization = models.ForeignKey('account.Organization', on_delete=models.CASCADE, related_name='tally_exports')
    gstin = models.CharField(max_length=15, blank=True)
    export_type = models.CharField(max_length=30, choices=EXPORT_TYPES, default='SalesVoucherXML')
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Generated')
    filename = models.CharField(max_length=120)
    content = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.filename
