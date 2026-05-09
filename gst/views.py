"""GST compliance API views."""

from datetime import date
from decimal import Decimal

from django.db.models import Count, Sum
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from account.models import Organization
from configuration.authentication import ECOMMERCE_MARKER, SUPER_ADMIN_MARKER, ScopedRolePermission
from sale.models import CreditNote, DebitNote, Invoice, PurchaseInvoice

from .clients import SandboxGSTClient
from .models import GSTEInvoice, GSTEWayBill, GSTIntegrationRequest, GSTReturnFiling, TallyExport
from .reports import (
    EInvoicePayloadBuilder,
    EWayBillPayloadBuilder,
    GSTR1PayloadBuilder,
    GSTR3BPayloadBuilder,
    GSTR9PayloadBuilder,
    TallyVoucherXMLBuilder,
)


class GSTEmptySerializer(serializers.Serializer):
    """Placeholder serializer for schema generation on dynamic GST actions."""


def parse_report_decimal(value, field_name):
    """Parse optional report decimal query values."""
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ValidationError({field_name: 'Must be a valid decimal value.'}) from exc


def org_filter(qs, request, organization_path='business_location__organization'):
    """Scope GST source documents to the authenticated organization."""
    if not hasattr(request, 'auth') or request.auth is None:
        return qs.none()
    if request.auth == ECOMMERCE_MARKER:
        return qs.none()
    if request.auth == SUPER_ADMIN_MARKER:
        org_id = request.query_params.get('organization')
        if not org_id:
            return qs.none()
        return qs.filter(**{f'{organization_path}_id': org_id})
    return qs.filter(**{organization_path: request.auth})


def request_organization(request):
    """Resolve the organization for GST records."""
    if request.auth == SUPER_ADMIN_MARKER:
        org_id = request.query_params.get('organization') or request.data.get('organization')
        if not org_id:
            raise ValidationError({'organization': 'organization is required for super admin GST operations.'})
        try:
            return Organization.objects.get(pk=org_id)
        except Organization.DoesNotExist as exc:
            raise ValidationError({'organization': 'Organization was not found.'}) from exc
    if request.auth == ECOMMERCE_MARKER or not request.auth:
        raise ValidationError({'organization': 'Organization-scoped authentication is required.'})
    return request.auth


def split_return_period(return_period):
    """Return Sandbox path year/month from MMYYYY."""
    if len(return_period) != 6 or not return_period.isdigit():
        raise ValidationError({'return_period': 'Use MMYYYY format.'})
    return return_period[2:], return_period[:2]


def financial_year_dates(value):
    """Parse YYYY-YYYY financial year into date bounds."""
    if not value or '-' not in value:
        raise ValidationError({'financial_year': 'Use YYYY-YYYY format.'})
    start_text, end_text = value.split('-', 1)
    if not (start_text.isdigit() and end_text.isdigit()):
        raise ValidationError({'financial_year': 'Use YYYY-YYYY format.'})
    start_year = int(start_text)
    end_year = int(end_text)
    if end_year != start_year + 1:
        raise ValidationError({'financial_year': 'Financial year must span consecutive years.'})
    return date(start_year, 4, 1), date(end_year, 3, 31)


@extend_schema_view(
    gst_register=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    gstr1=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    gstr2=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    gst_liability=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    itc_reconciliation=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    gstr1_payload=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    gstr3b_payload=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    gstr9_payload=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    e_invoice_payload=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    generate_e_invoice=extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT),
    e_way_bill_payload=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    generate_e_way_bill=extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT),
    tally_sales_xml=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    save_gstr1_sandbox=extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT),
    save_gstr3b_sandbox=extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT),
)
class GSTReportViewSet(viewsets.ViewSet):
    """GST return payload reports built from ERP source documents."""

    serializer_class = GSTEmptySerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'reporting'

    def _date_filtered_invoices(self, request, invoice_types=None):
        invoices = org_filter(Invoice.objects.filter(status='Finalized'), request)
        if invoice_types is not None:
            invoices = invoices.filter(invoice_type__in=invoice_types)
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        if start_date:
            invoices = invoices.filter(invoice_date__date__gte=start_date)
        if end_date:
            invoices = invoices.filter(invoice_date__date__lte=end_date)
        return invoices

    def _date_filtered_purchase_invoices(self, request):
        purchases = org_filter(PurchaseInvoice.objects.filter(status='Finalized'), request)
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        if start_date:
            purchases = purchases.filter(invoice_date__date__gte=start_date)
        if end_date:
            purchases = purchases.filter(invoice_date__date__lte=end_date)
        return purchases

    def _date_filtered_credit_notes(self, request):
        credit_notes = org_filter(CreditNote.objects.all(), request, organization_path='invoice__business_location__organization')
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        if start_date:
            credit_notes = credit_notes.filter(created_at__date__gte=start_date)
        if end_date:
            credit_notes = credit_notes.filter(created_at__date__lte=end_date)
        return credit_notes

    @action(detail=False, methods=['get'])
    def gst_register(self, request):
        """Return finalized sales grouped for GST register review."""
        register = self._date_filtered_invoices(request).values('invoice_type').annotate(
            count=Count('id'),
            total=Sum('grand_total'),
            taxable=Sum('taxable_amount'),
            cgst=Sum('cgst_amount'),
            sgst=Sum('sgst_amount'),
            igst=Sum('igst_amount'),
        )

        return Response({'register': list(register)})

    @action(detail=False, methods=['get'])
    def gstr1(self, request):
        """Return the legacy row-based GSTR-1 export."""
        invoices = self._date_filtered_invoices(
            request,
            invoice_types=['Tax Invoice', 'Export', 'SEZ'],
        )

        data = []
        for inv in invoices:
            for item in inv.items.all():
                data.append({
                    'invoice_number': inv.invoice_number,
                    'invoice_date': inv.invoice_date.strftime('%Y-%m-%d'),
                    'party_gstin': inv.party.gstin if inv.party else '',
                    'party_name': inv.party.name if inv.party else '',
                    'place_of_supply': inv.billing_state.name if inv.billing_state else '',
                    'hsn_code': item.hsn_code,
                    'quantity': str(item.quantity),
                    'rate': str(item.rate),
                    'taxable_value': str(item.taxable_amount),
                    'cgst_rate': str(item.cgst_rate),
                    'cgst_amount': str(item.cgst_amount),
                    'sgst_rate': str(item.sgst_rate),
                    'sgst_amount': str(item.sgst_amount),
                    'igst_rate': str(item.igst_rate),
                    'igst_amount': str(item.igst_amount),
                })

        return Response({'gstr1_data': data})

    @action(detail=False, methods=['get'])
    def gstr2(self, request):
        """Return the legacy inward supply summary."""
        rows = []
        for pi in self._date_filtered_purchase_invoices(request).select_related('supplier'):
            rows.append({
                'invoice_number': pi.invoice_number,
                'invoice_date': pi.invoice_date.strftime('%Y-%m-%d'),
                'supplier_name': pi.supplier.name if pi.supplier else '',
                'supplier_gstin': pi.supplier.gstin if pi.supplier else '',
                'taxable_amount': str(pi.taxable_amount),
                'cgst_amount': str(pi.cgst_amount),
                'sgst_amount': str(pi.sgst_amount),
                'igst_amount': str(pi.igst_amount),
                'grand_total': str(pi.grand_total),
            })
        return Response({'gstr2_data': rows})

    @action(detail=False, methods=['get'])
    def gst_liability(self, request):
        """Return a simple GST liability summary."""
        sales = self._date_filtered_invoices(request).aggregate(
            taxable=Sum('taxable_amount'),
            cgst=Sum('cgst_amount'),
            sgst=Sum('sgst_amount'),
            igst=Sum('igst_amount'),
        )
        purchases = self._date_filtered_purchase_invoices(request).aggregate(
            taxable=Sum('taxable_amount'),
            cgst=Sum('cgst_amount'),
            sgst=Sum('sgst_amount'),
            igst=Sum('igst_amount'),
        )
        sales_tax = (sales['cgst'] or Decimal('0')) + (sales['sgst'] or Decimal('0')) + (sales['igst'] or Decimal('0'))
        input_tax = (purchases['cgst'] or Decimal('0')) + (purchases['sgst'] or Decimal('0')) + (purchases['igst'] or Decimal('0'))
        return Response({
            'sales_tax': str(sales_tax),
            'input_tax_credit': str(input_tax),
            'net_liability': str(sales_tax - input_tax),
        })

    @action(detail=False, methods=['get'])
    def itc_reconciliation(self, request):
        """Return a basic input tax credit reconciliation summary."""
        purchases = self._date_filtered_purchase_invoices(request)
        debit_notes = org_filter(
            DebitNote.objects.all(),
            request,
            organization_path='purchase_invoice__business_location__organization',
        )
        itc = purchases.aggregate(
            cgst=Sum('cgst_amount'),
            sgst=Sum('sgst_amount'),
            igst=Sum('igst_amount'),
        )
        reversed_itc = debit_notes.aggregate(cgst=Sum('amount'))
        total_itc = (itc['cgst'] or Decimal('0')) + (itc['sgst'] or Decimal('0')) + (itc['igst'] or Decimal('0'))
        return Response({
            'eligible_itc': str(total_itc),
            'reversed_itc': str(reversed_itc['cgst'] or Decimal('0')),
            'net_itc': str(total_itc - (reversed_itc['cgst'] or Decimal('0'))),
        })

    @action(detail=False, methods=['get'])
    def gstr1_payload(self, request):
        """Return a GSTR-1 section payload suitable for portal/GSP save calls."""
        invoices = self._date_filtered_invoices(
            request,
            invoice_types=['Tax Invoice', 'Bill of Supply', 'Export', 'SEZ', 'Cash'],
        ).select_related(
            'party',
            'billing_state',
            'business_location',
            'business_location__state',
        ).prefetch_related('items__item__tax_code', 'items__unit')

        credit_notes = self._date_filtered_credit_notes(request).select_related(
            'party',
            'invoice',
            'invoice__billing_state',
            'invoice__business_location',
            'invoice__business_location__state',
        ).prefetch_related('items__invoice_item__item__tax_code', 'items__invoice_item__unit')

        gstin = (
            request.query_params.get('gstin', '')
            or request.data.get('gstin', '')
        ).strip().upper()
        if gstin:
            invoices = invoices.filter(business_location__gstin=gstin)
            credit_notes = credit_notes.filter(invoice__business_location__gstin=gstin)

        invoice_gstins = set(invoices.values_list('business_location__gstin', flat=True).distinct())
        note_gstins = set(credit_notes.values_list('invoice__business_location__gstin', flat=True).distinct())
        detected_gstins = {value for value in invoice_gstins | note_gstins if value}
        if not gstin:
            if len(detected_gstins) > 1:
                raise ValidationError({'gstin': 'gstin query parameter is required when the period contains multiple GSTINs.'})
            if len(detected_gstins) == 1:
                gstin = detected_gstins.pop()
            else:
                raise ValidationError({'gstin': 'gstin query parameter is required when there are no documents in the period.'})

        return_period = (
            request.query_params.get('return_period', '')
            or request.data.get('return_period', '')
        ).strip()
        if not return_period:
            start_date = request.query_params.get('start_date') or request.data.get('start_date')
            if start_date:
                parsed_start = parse_date(start_date)
                if parsed_start is None:
                    raise ValidationError({'start_date': 'Use YYYY-MM-DD format.'})
                return_period = parsed_start.strftime('%m%Y')
            else:
                return_period = timezone.localdate().strftime('%m%Y')
        if len(return_period) != 6 or not return_period.isdigit():
            raise ValidationError({'return_period': 'Use MMYYYY format.'})

        builder = GSTR1PayloadBuilder(
            invoices=invoices,
            credit_notes=credit_notes,
            gstin=gstin,
            return_period=return_period,
            aggregate_turnover=parse_report_decimal(request.query_params.get('gt') or request.data.get('gt'), 'gt'),
            current_turnover=parse_report_decimal(request.query_params.get('cur_gt') or request.data.get('cur_gt'), 'cur_gt'),
        )
        return Response(builder.build())

    @action(detail=False, methods=['get'])
    def gstr3b_payload(self, request):
        """Return a GSTR-3B summary payload."""
        gstin, return_period = self._gstin_and_return_period(request)
        invoices = self._date_filtered_invoices(request).filter(business_location__gstin=gstin)
        purchases = self._date_filtered_purchase_invoices(request).filter(business_location__gstin=gstin)
        credit_notes = self._date_filtered_credit_notes(request).filter(invoice__business_location__gstin=gstin)
        debit_notes = org_filter(
            DebitNote.objects.all(),
            request,
            organization_path='purchase_invoice__business_location__organization',
        ).filter(purchase_invoice__business_location__gstin=gstin)
        builder = GSTR3BPayloadBuilder(invoices, purchases, credit_notes, debit_notes, gstin, return_period)
        return Response(builder.build())

    @action(detail=False, methods=['get'])
    def gstr9_payload(self, request):
        """Return an annual GSTR-9 summary payload."""
        gstin = (request.query_params.get('gstin', '') or request.data.get('gstin', '')).strip().upper()
        if not gstin:
            raise ValidationError({'gstin': 'gstin query parameter is required.'})
        financial_year = (request.query_params.get('financial_year', '') or request.data.get('financial_year', '')).strip()
        start_date, end_date = financial_year_dates(financial_year)
        invoices = org_filter(Invoice.objects.filter(status='Finalized'), request).filter(
            business_location__gstin=gstin,
        )
        purchases = org_filter(PurchaseInvoice.objects.filter(status='Finalized'), request).filter(
            business_location__gstin=gstin,
        )
        credit_notes = org_filter(CreditNote.objects.all(), request, organization_path='invoice__business_location__organization').filter(
            invoice__business_location__gstin=gstin,
        )
        debit_notes = org_filter(
            DebitNote.objects.all(),
            request,
            organization_path='purchase_invoice__business_location__organization',
        ).filter(purchase_invoice__business_location__gstin=gstin)
        builder = GSTR9PayloadBuilder(invoices, purchases, credit_notes, debit_notes, gstin, financial_year)
        return Response(builder.build())

    @action(detail=False, methods=['post'])
    def save_gstr9_sandbox(self, request):
        """Prepare or save GSTR-9 draft data through Sandbox."""
        payload_response = self.gstr9_payload(request).data
        gstin = payload_response['payload']['gstin']
        financial_year = payload_response['payload']['financial_year']
        filing = self._upsert_return(request, 'GSTR9', gstin, financial_year, payload_response)
        if self._is_dry_run(request):
            self._log_prepared(filing.organization, 'save_gstr9', payload_response['payload'], return_filing=filing)
            return Response({'id': filing.id, 'status': filing.status, **payload_response})
        return self._send_return(filing, '/gst/compliance/tax-payer/gstrs/gstr-9/save')

    @action(detail=False, methods=['post'])
    def proceed_gstr9_sandbox(self, request):
        """Run GSTR-9 validations and move the draft toward filing."""
        return self._file_gstr9_step(request, 'proceed', '/gst/compliance/tax-payer/gstrs/gstr-9/proceed')

    @action(detail=False, methods=['post'])
    def file_gstr9_sandbox(self, request):
        """File GSTR-9 using the saved draft and EVC OTP."""
        return self._file_gstr9_step(request, 'file', '/gst/compliance/tax-payer/gstrs/gstr-9/file')

    @action(detail=False, methods=['get'])
    def e_invoice_payload(self, request):
        """Return the e-invoice payload for an invoice."""
        invoice = self._invoice_from_request(request)
        return Response(EInvoicePayloadBuilder(invoice).build())

    @action(detail=False, methods=['post'])
    def generate_e_invoice(self, request):
        """Prepare or send an e-invoice generation request."""
        invoice = self._invoice_from_request(request)
        result = EInvoicePayloadBuilder(invoice).build()
        record, _ = GSTEInvoice.objects.update_or_create(
            invoice=invoice,
            defaults={
                'organization': invoice.business_location.organization,
                'request_payload': result['payload'],
                'status': 'Failed' if result['validation']['errors'] else 'Ready',
                'last_error': '; '.join(error['message'] for error in result['validation']['errors']),
                'created_by': request.user if request.user.is_authenticated else None,
            },
        )
        if result['validation']['errors']:
            return Response({'id': record.id, **result}, status=400)
        if self._is_dry_run(request):
            self._log_prepared(invoice.business_location.organization, 'generate_e_invoice', result['payload'], e_invoice=record)
            return Response({'id': record.id, 'status': record.status, **result})
        return self._send_e_invoice(record, result['payload'])

    @action(detail=False, methods=['get'])
    def e_way_bill_payload(self, request):
        """Return the e-way bill payload for an invoice."""
        invoice = self._invoice_from_request(request)
        return Response(EWayBillPayloadBuilder(invoice, request.query_params).build())

    @action(detail=False, methods=['post'])
    def generate_e_way_bill(self, request):
        """Prepare or send an e-way bill generation request."""
        invoice = self._invoice_from_request(request)
        result = EWayBillPayloadBuilder(invoice, request.data).build()
        record, _ = GSTEWayBill.objects.update_or_create(
            invoice=invoice,
            defaults={
                'organization': invoice.business_location.organization,
                'request_payload': result['payload'],
                'status': 'Failed' if result['validation']['errors'] else 'Ready',
                'transporter_id': request.data.get('transporter_id', ''),
                'transporter_name': request.data.get('transporter_name', ''),
                'transport_mode': str(request.data.get('transport_mode', '')),
                'transport_doc_no': request.data.get('transport_doc_no', ''),
                'vehicle_no': request.data.get('vehicle_no', ''),
                'distance_km': int(request.data.get('distance_km') or 0),
                'last_error': '; '.join(error['message'] for error in result['validation']['errors']),
                'created_by': request.user if request.user.is_authenticated else None,
            },
        )
        if result['validation']['errors']:
            return Response({'id': record.id, **result}, status=400)
        if self._is_dry_run(request):
            self._log_prepared(invoice.business_location.organization, 'generate_e_way_bill', result['payload'], e_way_bill=record)
            return Response({'id': record.id, 'status': record.status, **result})
        return self._send_e_way_bill(record, result['payload'])

    @action(detail=False, methods=['get'])
    def tally_sales_xml(self, request):
        """Generate TallyPrime sales voucher XML."""
        invoices = self._date_filtered_invoices(request)
        gstin = request.query_params.get('gstin', '').strip().upper()
        if gstin:
            invoices = invoices.filter(business_location__gstin=gstin)
        organization = request_organization(request)
        company_name = request.query_params.get('company_name', organization.name)
        xml = TallyVoucherXMLBuilder(invoices.select_related('party', 'business_location'), company_name).build()
        export = TallyExport.objects.create(
            organization=organization,
            gstin=gstin,
            start_date=parse_date(request.query_params.get('start_date') or '') if request.query_params.get('start_date') else None,
            end_date=parse_date(request.query_params.get('end_date') or '') if request.query_params.get('end_date') else None,
            filename=f'tally-sales-{timezone.now().strftime("%Y%m%d%H%M%S")}.xml',
            content=xml,
            metadata={'invoice_count': invoices.count()},
            created_by=request.user if request.user.is_authenticated else None,
        )
        if request.query_params.get('download') == '1':
            response = HttpResponse(xml, content_type='application/xml')
            response['Content-Disposition'] = f'attachment; filename="{export.filename}"'
            return response
        return Response({'id': export.id, 'filename': export.filename, 'content': xml})

    @action(detail=False, methods=['post'])
    def save_gstr1_sandbox(self, request):
        """Prepare or send GSTR-1 to Sandbox save API."""
        payload_response = self.gstr1_payload(request).data
        gstin = payload_response['payload']['gstin']
        return_period = payload_response['payload']['fp']
        filing = self._upsert_return(request, 'GSTR1', gstin, return_period, payload_response)
        if self._is_dry_run(request):
            self._log_prepared(filing.organization, 'save_gstr1', payload_response['payload'], return_filing=filing)
            return Response({'id': filing.id, 'status': filing.status, **payload_response})
        year, month = split_return_period(return_period)
        return self._send_return(filing, f'/gst/compliance/tax-payer/gstrs/gstr-1/{year}/{month}')

    @action(detail=False, methods=['post'])
    def save_gstr3b_sandbox(self, request):
        """Prepare or send GSTR-3B to Sandbox save API."""
        payload_response = self.gstr3b_payload(request).data
        gstin = payload_response['payload']['gstin']
        return_period = payload_response['payload']['ret_period']
        filing = self._upsert_return(request, 'GSTR3B', gstin, return_period, payload_response)
        if self._is_dry_run(request):
            self._log_prepared(filing.organization, 'save_gstr3b', payload_response['payload'], return_filing=filing)
            return Response({'id': filing.id, 'status': filing.status, **payload_response})
        year, month = split_return_period(return_period)
        return self._send_return(filing, f'/gst/compliance/tax-payer/gstrs/gstr-3b/{year}/{month}')

    def _file_gstr9_step(self, request, operation, path):
        payload_response = self.gstr9_payload(request).data
        gstin = payload_response['payload']['gstin']
        financial_year = payload_response['payload']['financial_year']
        filing = self._upsert_return(request, 'GSTR9', gstin, financial_year, payload_response)
        if self._is_dry_run(request):
            self._log_prepared(filing.organization, f'gstr9_{operation}', payload_response['payload'], return_filing=filing)
            return Response({'id': filing.id, 'status': filing.status, **payload_response})
        return self._send_return(filing, path)

    def _gstin_and_return_period(self, request):
        gstin = request.query_params.get('gstin', '').strip().upper() or request.data.get('gstin', '').strip().upper()
        if not gstin:
            raise ValidationError({'gstin': 'gstin is required.'})
        return_period = request.query_params.get('return_period', '').strip() or request.data.get('return_period', '').strip()
        if not return_period:
            start_date = request.query_params.get('start_date') or request.data.get('start_date')
            return_period = parse_date(start_date).strftime('%m%Y') if start_date and parse_date(start_date) else timezone.localdate().strftime('%m%Y')
        split_return_period(return_period)
        return gstin, return_period

    def _invoice_from_request(self, request):
        invoice_id = request.query_params.get('invoice') or request.data.get('invoice')
        if not invoice_id:
            raise ValidationError({'invoice': 'invoice id is required.'})
        try:
            return org_filter(Invoice.objects.all(), request).select_related(
                'party',
                'billing_state',
                'business_location',
                'business_location__state',
            ).prefetch_related('items__item__tax_code', 'items__unit').get(pk=invoice_id)
        except Invoice.DoesNotExist as exc:
            raise ValidationError({'invoice': 'Invoice was not found.'}) from exc

    def _is_dry_run(self, request):
        value = request.query_params.get('dry_run', request.data.get('dry_run', True))
        return str(value).lower() not in {'0', 'false', 'no'}

    def _upsert_return(self, request, return_type, gstin, period, payload_response):
        organization = request_organization(request)
        return GSTReturnFiling.objects.update_or_create(
            organization=organization,
            gstin=gstin,
            return_type=return_type,
            period=period,
            defaults={
                'status': 'Ready',
                'payload': payload_response['payload'],
                'validation': payload_response.get('validation', {}),
                'provider': 'sandbox',
                'last_error': '',
                'created_by': request.user if request.user.is_authenticated else None,
            },
        )[0]

    def _log_prepared(self, organization, operation, payload, return_filing=None, e_invoice=None, e_way_bill=None):
        return GSTIntegrationRequest.objects.create(
            organization=organization,
            operation=operation,
            status='Prepared',
            request_payload=payload,
            return_filing=return_filing,
            e_invoice=e_invoice,
            e_way_bill=e_way_bill,
        )

    def _send_return(self, filing, path):
        client = SandboxGSTClient()
        log = GSTIntegrationRequest.objects.create(
            organization=filing.organization,
            operation=f'send_{filing.return_type.lower()}',
            status='Sent',
            request_payload=filing.payload,
            return_filing=filing,
        )
        try:
            endpoint, status_code, response_payload = client.post(path, filing.payload)
        except ImproperlyConfigured as exc:
            filing.status = 'Failed'
            filing.last_error = str(exc)
            filing.save(update_fields=['status', 'last_error', 'updated_at'])
            log.status = 'Failed'
            log.error = str(exc)
            log.save(update_fields=['status', 'error'])
            raise ValidationError({'sandbox': str(exc)}) from exc
        log.endpoint = endpoint
        log.status_code = status_code
        log.response_payload = response_payload
        log.status = 'Succeeded' if 200 <= status_code < 300 else 'Failed'
        log.save(update_fields=['endpoint', 'status_code', 'response_payload', 'status'])
        filing.status = 'Submitted' if log.status == 'Succeeded' else 'Failed'
        filing.response_payload = response_payload
        filing.last_error = '' if log.status == 'Succeeded' else str(response_payload)
        filing.save(update_fields=['status', 'response_payload', 'last_error', 'updated_at'])
        return Response({'id': filing.id, 'status': filing.status, 'response': response_payload}, status=status_code)

    def _send_e_invoice(self, record, payload):
        return self._send_document(record, payload, '/gst/compliance/e-invoice/tax-payer/invoice', 'generate_e_invoice')

    def _send_e_way_bill(self, record, payload):
        return self._send_document(record, payload, '/gst/compliance/e-way-bill/consignor/bill', 'generate_e_way_bill')

    def _send_document(self, record, payload, path, operation):
        client = SandboxGSTClient()
        log = GSTIntegrationRequest.objects.create(
            organization=record.organization,
            operation=operation,
            status='Sent',
            request_payload=payload,
            e_invoice=record if isinstance(record, GSTEInvoice) else None,
            e_way_bill=record if isinstance(record, GSTEWayBill) else None,
        )
        try:
            endpoint, status_code, response_payload = client.post(path, payload)
        except ImproperlyConfigured as exc:
            record.status = 'Failed'
            record.last_error = str(exc)
            record.save(update_fields=['status', 'last_error', 'updated_at'])
            log.status = 'Failed'
            log.error = str(exc)
            log.save(update_fields=['status', 'error'])
            raise ValidationError({'sandbox': str(exc)}) from exc
        log.endpoint = endpoint
        log.status_code = status_code
        log.response_payload = response_payload
        log.status = 'Succeeded' if 200 <= status_code < 300 else 'Failed'
        log.save(update_fields=['endpoint', 'status_code', 'response_payload', 'status'])
        record.response_payload = response_payload
        record.status = 'Generated' if log.status == 'Succeeded' else 'Failed'
        record.last_error = '' if log.status == 'Succeeded' else str(response_payload)
        if isinstance(record, GSTEInvoice):
            record.irn = response_payload.get('Irn', response_payload.get('irn', record.irn))
            record.ack_no = str(response_payload.get('AckNo', record.ack_no) or '')
            record.signed_invoice = response_payload.get('SignedInvoice', record.signed_invoice)
            record.signed_qr_code = response_payload.get('SignedQRCode', record.signed_qr_code)
        if isinstance(record, GSTEWayBill):
            record.ewb_no = str(response_payload.get('ewayBillNo', response_payload.get('ewbNo', record.ewb_no)) or '')
        record.save()
        return Response({'id': record.id, 'status': record.status, 'response': response_payload}, status=status_code)
