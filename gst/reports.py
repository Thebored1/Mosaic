"""GST return payload builders."""

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from django.conf import settings


MONEY_QUANT = Decimal('0.01')
QTY_QUANT = Decimal('0.0001')
ZERO = Decimal('0')


def decimal_value(value):
    """Convert nullable values into Decimals for deterministic report math."""
    if value in (None, ''):
        return ZERO
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return ZERO


def money(value):
    """Return a JSON-native money value rounded to two decimals."""
    return float(decimal_value(value).quantize(MONEY_QUANT))


def quantity(value):
    """Return a JSON-native quantity value rounded to four decimals."""
    return float(decimal_value(value).quantize(QTY_QUANT))


def rate(value):
    """Return a JSON-native percentage rate rounded to two decimals."""
    return money(value)


def gst_rate(item):
    """Return the GST rate used by a line item."""
    igst = decimal_value(item.igst_rate)
    if igst:
        return igst
    return decimal_value(item.cgst_rate) + decimal_value(item.sgst_rate)


def state_code(state):
    """Return GST state code as a two-character string."""
    return getattr(state, 'state_code', '') or ''


def invoice_date(value):
    """Return GST portal date format."""
    return value.strftime('%d-%m-%Y')


def hsn_code(line):
    """Resolve HSN/SAC from line snapshot first, then item tax code."""
    source_line = getattr(line, 'invoice_item', line)
    if getattr(source_line, 'hsn_code', ''):
        return source_line.hsn_code
    tax_code = getattr(source_line.item, 'tax_code', None)
    return getattr(tax_code, 'code', '') or ''


def unit_code(line):
    """Resolve a best-effort UQC code."""
    source_line = getattr(line, 'invoice_item', line)
    unit = source_line.unit or getattr(source_line.item, 'unit', None)
    if not unit:
        return 'OTH'
    return (unit.short_code or unit.name or 'OTH').upper()[:3]


def invoice_type_code(invoice):
    """Map local invoice types to GSTR-1 invoice type codes."""
    if invoice.invoice_type == 'SEZ':
        return 'SEWP' if decimal_value(invoice.igst_amount) else 'SEWOP'
    return 'R'


def export_type(invoice):
    """Map local export invoices to GSTR-1 export type codes."""
    return 'WPAY' if decimal_value(invoice.igst_amount) else 'WOPAY'


def item_detail(line):
    """Build the GSTR-1 item detail object for one line."""
    return {
        'rt': rate(gst_rate(line)),
        'txval': money(line.taxable_amount),
        'iamt': money(line.igst_amount),
        'camt': money(line.cgst_amount),
        'samt': money(line.sgst_amount),
        'csamt': money(line.cess_amount),
    }


def line_items(lines):
    """Build numbered GSTR-1 item objects."""
    return [
        {
            'num': index,
            'itm_det': item_detail(line),
        }
        for index, line in enumerate(lines, start=1)
    ]


def document_summary(document_numbers):
    """Build one document range summary for GSTR-1 document issue details."""
    if not document_numbers:
        return []
    ordered = sorted(document_numbers)
    return [{
        'num': 1,
        'from': ordered[0],
        'to': ordered[-1],
        'totnum': len(ordered),
        'cancel': 0,
        'net_issue': len(ordered),
    }]


def tax_totals(documents):
    """Aggregate taxable value and GST components for documents."""
    return {
        'txval': sum((decimal_value(doc.taxable_amount) for doc in documents), ZERO),
        'iamt': sum((decimal_value(doc.igst_amount) for doc in documents), ZERO),
        'camt': sum((decimal_value(doc.cgst_amount) for doc in documents), ZERO),
        'samt': sum((decimal_value(doc.sgst_amount) for doc in documents), ZERO),
        'csamt': sum((decimal_value(getattr(doc, 'cess_amount', ZERO)) for doc in documents), ZERO),
    }


def tax_row(documents):
    """Return a GST tax summary row."""
    totals = tax_totals(documents)
    return {
        'txval': money(totals['txval']),
        'iamt': money(totals['iamt']),
        'camt': money(totals['camt']),
        'samt': money(totals['samt']),
        'csamt': money(totals['csamt']),
    }


def address_line(value):
    """Return a bounded address line for external GST payloads."""
    text = (value or '').replace('\r', ' ').replace('\n', ' ').strip()
    return text[:100] or 'NA'


def pincode_from_settings():
    """Return configured fallback pincode for payloads that require one."""
    return str(getattr(settings, 'GST_DEFAULT_PINCODE', '999999'))


def party_state_code(party, fallback_state):
    """Resolve recipient state code."""
    return state_code(getattr(party, 'state', None)) or state_code(fallback_state)


class GSTR1PayloadBuilder:
    """Build a GSTR-1-style section payload from finalized sales documents."""

    def __init__(
        self,
        invoices,
        credit_notes=None,
        gstin='',
        return_period='',
        aggregate_turnover=None,
        current_turnover=None,
    ):
        self.invoices = list(invoices)
        self.credit_notes = list(credit_notes or [])
        self.gstin = gstin
        self.return_period = return_period
        self.aggregate_turnover = aggregate_turnover
        self.current_turnover = current_turnover
        self.validation_errors = []
        self.validation_warnings = []
        self.b2cl_threshold = decimal_value(getattr(settings, 'GSTR1_B2CL_THRESHOLD', Decimal('100000')))

    def build(self):
        """Return a payload plus validation metadata."""
        payload = {
            'gstin': self.gstin,
            'fp': self.return_period,
            'gt': money(self.aggregate_turnover if self.aggregate_turnover is not None else self._invoice_total()),
            'cur_gt': money(self.current_turnover if self.current_turnover is not None else self._invoice_total()),
            'b2b': [],
            'b2cl': [],
            'b2cs': [],
            'exp': [],
            'cdnr': [],
            'cdnur': [],
            'hsn': {'data': []},
            'doc_issue': {'doc_det': self._document_issue_details()},
        }

        self._add_invoices(payload)
        self._add_credit_notes(payload)
        payload['hsn']['data'] = self._hsn_summary()

        return {
            'payload': payload,
            'validation': {
                'errors': self.validation_errors,
                'warnings': self.validation_warnings,
            },
        }

    def _invoice_total(self):
        return sum((decimal_value(invoice.grand_total) for invoice in self.invoices), ZERO)

    def _add_invoices(self, payload):
        b2b_by_ctin = defaultdict(list)
        b2cl_by_pos = defaultdict(list)
        b2cs_by_key = {}
        exp_by_type = defaultdict(list)

        for invoice in self.invoices:
            self._validate_invoice(invoice)
            lines = list(invoice.items.all())
            if invoice.invoice_type == 'Export':
                exp_by_type[export_type(invoice)].append(self._export_invoice(invoice, lines))
                continue

            party_gstin = (getattr(invoice.party, 'gstin', '') or '').upper()
            if party_gstin:
                b2b_by_ctin[party_gstin].append(self._regular_invoice(invoice, lines))
                continue

            if self._is_b2c_large(invoice):
                b2cl_by_pos[state_code(invoice.billing_state)].append(self._b2cl_invoice(invoice, lines))
                continue

            for line in lines:
                key = (
                    'INTER' if invoice.billing_state_id != invoice.business_location.state_id else 'INTRA',
                    state_code(invoice.billing_state),
                    rate(gst_rate(line)),
                )
                current = b2cs_by_key.setdefault(key, {
                    'sply_ty': key[0],
                    'pos': key[1],
                    'typ': 'OE',
                    'rt': key[2],
                    'txval': ZERO,
                    'iamt': ZERO,
                    'camt': ZERO,
                    'samt': ZERO,
                    'csamt': ZERO,
                })
                current['txval'] += decimal_value(line.taxable_amount)
                current['iamt'] += decimal_value(line.igst_amount)
                current['camt'] += decimal_value(line.cgst_amount)
                current['samt'] += decimal_value(line.sgst_amount)
                current['csamt'] += decimal_value(line.cess_amount)

        payload['b2b'] = [{'ctin': ctin, 'inv': invoices} for ctin, invoices in sorted(b2b_by_ctin.items())]
        payload['b2cl'] = [{'pos': pos, 'inv': invoices} for pos, invoices in sorted(b2cl_by_pos.items())]
        payload['b2cs'] = [
            {
                **{key: value for key, value in row.items() if key not in {'txval', 'iamt', 'camt', 'samt', 'csamt'}},
                'txval': money(row['txval']),
                'iamt': money(row['iamt']),
                'camt': money(row['camt']),
                'samt': money(row['samt']),
                'csamt': money(row['csamt']),
            }
            for row in b2cs_by_key.values()
        ]
        payload['exp'] = [{'exp_typ': exp_typ, 'inv': invoices} for exp_typ, invoices in sorted(exp_by_type.items())]

    def _add_credit_notes(self, payload):
        cdnr_by_ctin = defaultdict(list)
        cdnur = []

        for note in self.credit_notes:
            self._validate_credit_note(note)
            lines = list(note.items.all())
            party_gstin = (getattr(note.party, 'gstin', '') or '').upper()
            if party_gstin:
                cdnr_by_ctin[party_gstin].append(self._credit_note(note, lines))
            else:
                cdnur.append(self._unregistered_credit_note(note, lines))

        payload['cdnr'] = [{'ctin': ctin, 'nt': notes} for ctin, notes in sorted(cdnr_by_ctin.items())]
        payload['cdnur'] = cdnur

    def _regular_invoice(self, invoice, lines):
        return {
            'inum': invoice.invoice_number,
            'idt': invoice_date(invoice.invoice_date),
            'val': money(invoice.grand_total),
            'pos': state_code(invoice.billing_state),
            'rchrg': 'N',
            'inv_typ': invoice_type_code(invoice),
            'itms': line_items(lines),
        }

    def _b2cl_invoice(self, invoice, lines):
        return {
            'inum': invoice.invoice_number,
            'idt': invoice_date(invoice.invoice_date),
            'val': money(invoice.grand_total),
            'itms': line_items(lines),
        }

    def _export_invoice(self, invoice, lines):
        return {
            'inum': invoice.invoice_number,
            'idt': invoice_date(invoice.invoice_date),
            'val': money(invoice.grand_total),
            'sbpcode': '',
            'sbnum': '',
            'sbdt': '',
            'itms': line_items(lines),
        }

    def _credit_note(self, note, lines):
        return {
            'ntty': 'C',
            'nt_num': note.credit_note_number,
            'nt_dt': invoice_date(note.created_at),
            'p_gst': 'N',
            'rsn': note.reason,
            'inum': note.invoice.invoice_number,
            'idt': invoice_date(note.invoice.invoice_date),
            'val': money(note.amount),
            'itms': line_items(lines),
        }

    def _unregistered_credit_note(self, note, lines):
        original_invoice = note.invoice
        note_type = 'B2CL' if self._is_b2c_large(original_invoice) else 'B2CS'
        return {
            'typ': note_type,
            'ntty': 'C',
            'nt_num': note.credit_note_number,
            'nt_dt': invoice_date(note.created_at),
            'p_gst': 'N',
            'rsn': note.reason,
            'inum': original_invoice.invoice_number,
            'idt': invoice_date(original_invoice.invoice_date),
            'val': money(note.amount),
            'pos': state_code(original_invoice.billing_state),
            'itms': line_items(lines),
        }

    def _hsn_summary(self):
        rows = {}
        for invoice in self.invoices:
            for line in invoice.items.all():
                code = hsn_code(line)
                key = (code, unit_code(line), rate(gst_rate(line)))
                row = rows.setdefault(key, {
                    'hsn_sc': code,
                    'desc': line.item.name[:30],
                    'uqc': key[1],
                    'qty': ZERO,
                    'rt': key[2],
                    'txval': ZERO,
                    'iamt': ZERO,
                    'camt': ZERO,
                    'samt': ZERO,
                    'csamt': ZERO,
                    'val': ZERO,
                })
                row['qty'] += decimal_value(line.quantity)
                row['txval'] += decimal_value(line.taxable_amount)
                row['iamt'] += decimal_value(line.igst_amount)
                row['camt'] += decimal_value(line.cgst_amount)
                row['samt'] += decimal_value(line.sgst_amount)
                row['csamt'] += decimal_value(line.cess_amount)
                row['val'] += decimal_value(line.total)

        return [
            {
                'num': index,
                'hsn_sc': row['hsn_sc'],
                'desc': row['desc'],
                'uqc': row['uqc'],
                'qty': quantity(row['qty']),
                'rt': row['rt'],
                'txval': money(row['txval']),
                'iamt': money(row['iamt']),
                'camt': money(row['camt']),
                'samt': money(row['samt']),
                'csamt': money(row['csamt']),
                'val': money(row['val']),
            }
            for index, row in enumerate(rows.values(), start=1)
        ]

    def _document_issue_details(self):
        invoices = [invoice.invoice_number for invoice in self.invoices]
        credit_notes = [note.credit_note_number for note in self.credit_notes]
        doc_details = []
        for doc_num in range(1, 13):
            numbers = invoices if doc_num == 1 else credit_notes if doc_num == 5 else []
            doc_details.append({'doc_num': doc_num, 'docs': document_summary(numbers)})
        return doc_details

    def _is_b2c_large(self, invoice):
        is_interstate = invoice.billing_state_id != invoice.business_location.state_id
        return is_interstate and decimal_value(invoice.grand_total) > self.b2cl_threshold

    def _validate_invoice(self, invoice):
        if len(invoice.invoice_number or '') > 16:
            self.validation_warnings.append({
                'document': invoice.invoice_number,
                'field': 'invoice_number',
                'message': 'GST portal invoice numbers are limited to 16 characters.',
            })
        if not state_code(invoice.billing_state):
            self.validation_errors.append({
                'document': invoice.invoice_number,
                'field': 'billing_state',
                'message': 'Place of supply state code is required.',
            })
        if invoice.invoice_type in {'Tax Invoice', 'SEZ'} and not getattr(invoice.party, 'gstin', ''):
            if invoice.invoice_type == 'SEZ':
                severity = self.validation_errors
            else:
                severity = self.validation_warnings
            severity.append({
                'document': invoice.invoice_number,
                'field': 'party.gstin',
                'message': f'{invoice.invoice_type} should have recipient GSTIN for registered supply reporting.',
            })
        if invoice.invoice_type == 'Export':
            self.validation_warnings.append({
                'document': invoice.invoice_number,
                'field': 'shipping_bill',
                'message': 'Export shipping bill number, date, and port code are not stored yet.',
            })
        for line in invoice.items.all():
            if not hsn_code(line):
                self.validation_warnings.append({
                    'document': invoice.invoice_number,
                    'field': 'hsn_code',
                    'message': f'Line item {line.item_id} has no HSN/SAC code.',
                })

    def _validate_credit_note(self, note):
        if len(note.credit_note_number or '') > 16:
            self.validation_warnings.append({
                'document': note.credit_note_number,
                'field': 'credit_note_number',
                'message': 'GST portal note numbers are limited to 16 characters.',
            })


class GSTR3BPayloadBuilder:
    """Build a GSTR-3B-style summary payload from finalized books."""

    def __init__(self, invoices, purchases, credit_notes=None, debit_notes=None, gstin='', return_period=''):
        self.invoices = list(invoices)
        self.purchases = list(purchases)
        self.credit_notes = list(credit_notes or [])
        self.debit_notes = list(debit_notes or [])
        self.gstin = gstin
        self.return_period = return_period

    def build(self):
        taxable_sales = [invoice for invoice in self.invoices if invoice.invoice_type in {'Tax Invoice', 'Cash'}]
        zero_rated_sales = [invoice for invoice in self.invoices if invoice.invoice_type in {'Export', 'SEZ'}]
        nil_sales = [invoice for invoice in self.invoices if invoice.invoice_type == 'Bill of Supply']
        output = self._net_invoice_row(taxable_sales, self.credit_notes)
        zero_rated = tax_row(zero_rated_sales)
        nil_rated = tax_row(nil_sales)
        itc = tax_row(self.purchases)

        payload = {
            'gstin': self.gstin,
            'ret_period': self.return_period,
            'sup_details': {
                'osup_det': output,
                'osup_zero': zero_rated,
                'osup_nil_exmp': nil_rated,
                'isup_rev': tax_row([]),
                'osup_nongst': tax_row([]),
            },
            'inter_sup': self._interstate_unregistered_summary(),
            'itc_elg': {
                'itc_avl': [
                    {'ty': 'IMPG', **tax_row([])},
                    {'ty': 'IMPS', **tax_row([])},
                    {'ty': 'ISRC', **tax_row([])},
                    {'ty': 'ISD', **tax_row([])},
                    {'ty': 'OTH', **itc},
                ],
                'itc_rev': [
                    {'ty': 'RUL', **tax_row([])},
                    {'ty': 'OTH', **self._debit_note_row()},
                ],
                'itc_net': itc,
                'itc_inelg': [
                    {'ty': 'RUL', **tax_row([])},
                    {'ty': 'OTH', **tax_row([])},
                ],
            },
            'inward_sup': {
                'isup_details': [
                    {'ty': 'GST', 'inter': 0.0, 'intra': 0.0},
                    {'ty': 'NONGST', 'inter': 0.0, 'intra': 0.0},
                ],
            },
        }
        return {'payload': payload, 'validation': {'errors': [], 'warnings': []}}

    def _net_invoice_row(self, invoices, credit_notes):
        totals = tax_totals(invoices)
        for note in credit_notes:
            for line in note.items.all():
                totals['txval'] -= decimal_value(line.taxable_amount)
                totals['iamt'] -= decimal_value(line.igst_amount)
                totals['camt'] -= decimal_value(line.cgst_amount)
                totals['samt'] -= decimal_value(line.sgst_amount)
                totals['csamt'] -= decimal_value(line.cess_amount)
        return {
            'txval': money(totals['txval']),
            'iamt': money(totals['iamt']),
            'camt': money(totals['camt']),
            'samt': money(totals['samt']),
            'csamt': money(totals['csamt']),
        }

    def _debit_note_row(self):
        totals = {'txval': ZERO, 'iamt': ZERO, 'camt': ZERO, 'samt': ZERO, 'csamt': ZERO}
        for note in self.debit_notes:
            for line in note.items.all():
                totals['txval'] += decimal_value(line.taxable_amount)
                totals['iamt'] += decimal_value(line.igst_amount)
                totals['camt'] += decimal_value(line.cgst_amount)
                totals['samt'] += decimal_value(line.sgst_amount)
                totals['csamt'] += decimal_value(line.cess_amount)
        return {
            'txval': money(totals['txval']),
            'iamt': money(totals['iamt']),
            'camt': money(totals['camt']),
            'samt': money(totals['samt']),
            'csamt': money(totals['csamt']),
        }

    def _interstate_unregistered_summary(self):
        rows = defaultdict(lambda: {'txval': ZERO, 'iamt': ZERO})
        for invoice in self.invoices:
            if getattr(invoice.party, 'gstin', ''):
                continue
            if invoice.billing_state_id == invoice.business_location.state_id:
                continue
            code = state_code(invoice.billing_state)
            rows[code]['txval'] += decimal_value(invoice.taxable_amount)
            rows[code]['iamt'] += decimal_value(invoice.igst_amount)
        return {
            'unreg_details': [
                {'pos': pos, 'txval': money(row['txval']), 'iamt': money(row['iamt'])}
                for pos, row in sorted(rows.items())
            ],
            'comp_details': [],
            'uin_details': [],
        }


class GSTR9PayloadBuilder:
    """Build a portal-shaped GSTR-9 annual return payload."""

    def __init__(self, invoices, purchases, credit_notes=None, debit_notes=None, gstin='', financial_year=''):
        self.invoices = list(invoices)
        self.purchases = list(purchases)
        self.credit_notes = list(credit_notes or [])
        self.debit_notes = list(debit_notes or [])
        self.gstin = gstin
        self.financial_year = financial_year
        self.validation_errors = []
        self.validation_warnings = []
        self.fy_start, self.fy_end = self._financial_year_bounds(financial_year)

    def build(self):
        fy_invoices = self._fy_invoices()
        fy_purchases = self._fy_purchases()
        fy_credit_notes = self._fy_credit_notes()
        fy_debit_notes = self._fy_debit_notes()

        payload = {
            'gstin': self.gstin,
            'financial_year': self.financial_year,
            'period': {
                'start_date': self.fy_start.isoformat() if self.fy_start else '',
                'end_date': self.fy_end.isoformat() if self.fy_end else '',
            },
            'basic_details': self._basic_details(),
            'table_4': self._table_4(fy_invoices, fy_credit_notes),
            'table_5': self._table_5(fy_invoices),
            'table_6': self._table_6(fy_purchases),
            'table_7': self._table_7(fy_debit_notes),
            'table_8': self._table_8(),
            'table_9': self._table_9(fy_invoices, fy_credit_notes, fy_debit_notes),
            'table_10': self._table_10(),
            'table_11': self._table_11(),
            'table_12': self._table_12(),
            'table_13': self._table_13(),
            'table_14': self._table_14(),
            'table_15': self._table_15(),
            'table_16': self._table_16(),
            'table_17': self._annual_hsn_summary(fy_invoices),
            'table_18': self._annual_hsn_summary(fy_purchases),
            'table17_hsn_outward': self._annual_hsn_summary(fy_invoices),
            'table18_hsn_inward': self._annual_hsn_summary(fy_purchases),
        }
        return {'payload': payload, 'validation': {'errors': self.validation_errors, 'warnings': self.validation_warnings}}

    def _financial_year_bounds(self, financial_year):
        if not financial_year:
            self.validation_errors.append({'field': 'financial_year', 'message': 'financial_year is required in YYYY-YYYY format.'})
            return None, None
        if '-' not in financial_year:
            self.validation_errors.append({'field': 'financial_year', 'message': 'financial_year must use YYYY-YYYY format.'})
            return None, None
        start_text, end_text = financial_year.split('-', 1)
        if not (start_text.isdigit() and end_text.isdigit()):
            self.validation_errors.append({'field': 'financial_year', 'message': 'financial_year must use YYYY-YYYY format.'})
            return None, None
        start_year = int(start_text)
        end_year = int(end_text)
        if end_year != start_year + 1:
            self.validation_errors.append({'field': 'financial_year', 'message': 'financial_year must span consecutive years.'})
            return None, None
        return date(start_year, 4, 1), date(end_year, 3, 31)

    def _date_value(self, value):
        return getattr(value, 'date', lambda: value)()

    def _in_fy(self, value):
        if self.fy_start is None or self.fy_end is None:
            return True
        day = self._date_value(value)
        return self.fy_start <= day <= self.fy_end

    def _invoice_date(self, invoice):
        return self._date_value(invoice.invoice_date)

    def _note_date(self, note):
        return self._date_value(note.created_at)

    def _fy_invoices(self):
        return [invoice for invoice in self.invoices if self._in_fy(self._invoice_date(invoice))]

    def _fy_purchases(self):
        return [purchase for purchase in self.purchases if self._in_fy(self._invoice_date(purchase))]

    def _fy_credit_notes(self):
        return [note for note in self.credit_notes if self._in_fy(self._note_date(note))]

    def _fy_debit_notes(self):
        return [note for note in self.debit_notes if self._in_fy(self._note_date(note))]

    def _basic_details(self):
        location = self._first_location()
        return {
            'gstin': self.gstin,
            'financial_year': self.financial_year,
            'legal_name': getattr(location, 'legal_name', ''),
            'trade_name': getattr(location, 'trade_name', ''),
            'status': 'Draft',
            'type': 'Regular',
        }

    def _first_location(self):
        for invoice in self.invoices:
            return invoice.business_location
        for purchase in self.purchases:
            return purchase.business_location
        return None

    def _table_4(self, invoices, credit_notes):
        return {
            '4A_taxable_b2b': tax_row([i for i in invoices if getattr(i.party, 'gstin', '') and i.invoice_type == 'Tax Invoice']),
            '4B_taxable_b2c': tax_row([i for i in invoices if not getattr(i.party, 'gstin', '') and i.invoice_type in {'Tax Invoice', 'Cash'}]),
            '4C_zero_rated': tax_row([i for i in invoices if i.invoice_type in {'Export', 'SEZ'}]),
            '4D_registered_credit_notes': self._credit_note_row(credit_notes, registered_only=True),
            '4E_registered_debit_notes': tax_row([]),
            '4F_exempt_nil': tax_row([i for i in invoices if i.invoice_type == 'Bill of Supply']),
            '4G_other_outward': tax_row([i for i in invoices if i.invoice_type not in {'Tax Invoice', 'Cash', 'Export', 'SEZ', 'Bill of Supply'}]),
            '4H_amendments': tax_row([]),
        }

    def _table_5(self, invoices):
        return {
            '5A_exempt': tax_row([i for i in invoices if i.invoice_type == 'Bill of Supply']),
            '5B_nil_rated': tax_row([]),
            '5C_non_gst': tax_row([i for i in invoices if i.invoice_type == 'Non GST']),
        }

    def _table_6(self, purchases):
        total = tax_row(purchases)
        return {
            '6A_itc_availed': total,
            '6B_inward_inputs': total,
            '6C_inward_input_services': tax_row([]),
            '6D_inward_capital_goods': tax_row([]),
            '6E_imports_goods': tax_row([]),
            '6F_imports_services': tax_row([]),
            '6G_rcm': tax_row([]),
            '6H_reclaimed': tax_row([]),
        }

    def _table_7(self, debit_notes):
        total = self._debit_note_row(debit_notes)
        return {
            '7A_rule_37': tax_row([]),
            '7B_rule_37A': tax_row([]),
            '7C_rule_42_43': tax_row([]),
            '7D_section_17_5': tax_row([]),
            '7E_other_reversals': total,
            '7F_cess': {'csamt': total['csamt']},
            '7G_total': total,
        }

    def _table_8(self):
        return {
            '8A_itc_as_per_2b': tax_row(self.purchases),
            '8B_itc_as_per_6b': tax_row(self.purchases),
            '8C_itc_avail_next_fy': tax_row([]),
            '8D_difference': tax_row([]),
            '8E_c_envt_and_other': tax_row([]),
            '8F_other_reconciliations': tax_row([]),
            '8G_imports_igst': tax_row([]),
            '8H_itc_reclaimed': tax_row([]),
        }

    def _table_9(self, invoices, credit_notes, debit_notes):
        return {
            '9A_tax_paid_cash': tax_row(invoices),
            '9B_tax_paid_itc': self._credit_note_row(credit_notes),
            '9C_interest_late_fee_penalty': tax_row([]),
            '9D_other_payments': tax_row([]),
            '9E_net_tax_paid': tax_row(invoices),
            '9F_adjustments': self._debit_note_row(debit_notes),
        }

    def _table_10(self):
        data = self._previous_year_transactions_reported_in_current_fy()
        return {
            '10A_invoices': data['invoices'],
            '10B_credit_notes': data['credit_notes'],
            '10C_debit_notes': data['debit_notes'],
            '10D_amendments': data['amendments'],
        }

    def _table_11(self):
        return {
            '11A_differential_tax': self._differential_tax_paid(),
            '11B_interest': tax_row([]),
            '11C_other': tax_row([]),
        }

    def _table_12(self):
        return {
            '12A_itc_reversed_next_fy': tax_row([]),
            '12B_reclaimed_rule_37_37A': tax_row([]),
            '12C_other_itc_reversal': tax_row([]),
        }

    def _table_13(self):
        return {
            '13A_itc_availed_next_fy': tax_row([]),
            '13B_prev_fy_itc_current_fy': tax_row([]),
            '13C_debit_note_itc_current_fy': tax_row([]),
        }

    def _table_14(self):
        return {
            '14A_differential_tax': self._differential_tax_paid(),
            '14B_interest': tax_row([]),
            '14C_other_adjustments': tax_row([]),
        }

    def _table_15(self):
        return {
            '15A_demand': [],
            '15B_refund': [],
            '15C_demand_adjusted': [],
            '15D_refund_adjusted': [],
            '15E_net': tax_row([]),
        }

    def _table_16(self):
        return {
            '16A_composition_supplies': tax_row([]),
            '16B_deemed_supply': tax_row([]),
            '16C_goods_sent_approval': tax_row([]),
            '16D_goods_returned_approval': tax_row([]),
            '16E_net': tax_row([]),
        }

    def _credit_note_row(self, credit_notes, registered_only=False):
        totals = {'txval': ZERO, 'iamt': ZERO, 'camt': ZERO, 'samt': ZERO, 'csamt': ZERO}
        for note in credit_notes:
            if registered_only and not getattr(note.party, 'gstin', ''):
                continue
            for line in note.items.all():
                totals['txval'] += decimal_value(line.taxable_amount)
                totals['iamt'] += decimal_value(line.igst_amount)
                totals['camt'] += decimal_value(line.cgst_amount)
                totals['samt'] += decimal_value(line.sgst_amount)
                totals['csamt'] += decimal_value(line.cess_amount)
        return {
            'txval': money(totals['txval']),
            'iamt': money(totals['iamt']),
            'camt': money(totals['camt']),
            'samt': money(totals['samt']),
            'csamt': money(totals['csamt']),
        }

    def _debit_note_row(self, debit_notes):
        totals = {'txval': ZERO, 'iamt': ZERO, 'camt': ZERO, 'samt': ZERO, 'csamt': ZERO}
        for note in debit_notes:
            for line in note.items.all():
                totals['txval'] += decimal_value(line.taxable_amount)
                totals['iamt'] += decimal_value(line.igst_amount)
                totals['camt'] += decimal_value(line.cgst_amount)
                totals['samt'] += decimal_value(line.sgst_amount)
                totals['csamt'] += decimal_value(line.cess_amount)
        return {
            'txval': money(totals['txval']),
            'iamt': money(totals['iamt']),
            'camt': money(totals['camt']),
            'samt': money(totals['samt']),
            'csamt': money(totals['csamt']),
        }

    def _previous_year_transactions_reported_in_current_fy(self):
        if self.fy_start is None or self.fy_end is None:
            return {'invoices': tax_row([]), 'credit_notes': tax_row([]), 'debit_notes': tax_row([]), 'amendments': tax_row([])}
        invoices = [invoice for invoice in self.invoices if self._invoice_date(invoice) < self.fy_start and self._date_value(invoice.created_at) <= self.fy_end]
        credit_notes = [note for note in self.credit_notes if self._note_date(note) < self.fy_start and self._date_value(note.created_at) <= self.fy_end]
        debit_notes = [note for note in self.debit_notes if self._note_date(note) < self.fy_start and self._date_value(note.created_at) <= self.fy_end]
        return {
            'invoices': tax_row(invoices),
            'credit_notes': self._credit_note_row(credit_notes),
            'debit_notes': self._debit_note_row(debit_notes),
            'amendments': tax_row([]),
        }

    def _differential_tax_paid(self):
        return tax_row([])

    def _annual_hsn_summary(self, documents):
        rows = {}
        for document in documents:
            for line in document.items.all():
                code = hsn_code(line)
                key = (code, unit_code(line), rate(gst_rate(line)))
                row = rows.setdefault(key, {
                    'hsn_sc': code,
                    'desc': line.item.name[:30],
                    'uqc': key[1],
                    'qty': ZERO,
                    'rt': key[2],
                    'txval': ZERO,
                    'iamt': ZERO,
                    'camt': ZERO,
                    'samt': ZERO,
                    'csamt': ZERO,
                    'val': ZERO,
                })
                row['qty'] += decimal_value(line.quantity)
                row['txval'] += decimal_value(line.taxable_amount)
                row['iamt'] += decimal_value(line.igst_amount)
                row['camt'] += decimal_value(line.cgst_amount)
                row['samt'] += decimal_value(line.sgst_amount)
                row['csamt'] += decimal_value(line.cess_amount)
                row['val'] += decimal_value(line.total)

        return [
            {
                'num': index,
                'hsn_sc': row['hsn_sc'],
                'desc': row['desc'],
                'uqc': row['uqc'],
                'qty': quantity(row['qty']),
                'rt': row['rt'],
                'txval': money(row['txval']),
                'iamt': money(row['iamt']),
                'camt': money(row['camt']),
                'samt': money(row['samt']),
                'csamt': money(row['csamt']),
                'val': money(row['val']),
            }
            for index, row in enumerate(rows.values(), start=1)
        ]


class EInvoicePayloadBuilder:
    """Build an IRP e-invoice request payload for a finalized sales invoice."""

    def __init__(self, invoice):
        self.invoice = invoice
        self.validation_errors = []
        self.validation_warnings = []

    def build(self):
        self._validate()
        seller = self.invoice.business_location
        buyer = self.invoice.party
        payload = {
            'Version': '1.1',
            'TranDtls': {
                'TaxSch': 'GST',
                'SupTyp': 'SEZWP' if self.invoice.invoice_type == 'SEZ' else 'EXPWP' if self.invoice.invoice_type == 'Export' else 'B2B',
                'RegRev': 'N',
                'IgstOnIntra': 'N',
            },
            'DocDtls': {
                'Typ': 'INV',
                'No': self.invoice.invoice_number[:16],
                'Dt': self.invoice.invoice_date.strftime('%d/%m/%Y'),
            },
            'SellerDtls': {
                'Gstin': seller.gstin,
                'LglNm': seller.legal_name,
                'TrdNm': seller.trade_name or seller.name,
                'Addr1': address_line(seller.address),
                'Loc': seller.name[:50],
                'Pin': int(pincode_from_settings()),
                'Stcd': state_code(seller.state),
            },
            'BuyerDtls': {
                'Gstin': (getattr(buyer, 'gstin', '') or '').upper(),
                'LglNm': getattr(buyer, 'name', 'Cash Customer')[:100],
                'TrdNm': getattr(buyer, 'name', 'Cash Customer')[:100],
                'Pos': state_code(self.invoice.billing_state),
                'Addr1': address_line(getattr(buyer, 'address', '')),
                'Loc': getattr(self.invoice.billing_state, 'name', '')[:50] or 'NA',
                'Pin': int(pincode_from_settings()),
                'Stcd': party_state_code(buyer, self.invoice.billing_state),
            },
            'ItemList': [self._item(index, line) for index, line in enumerate(self.invoice.items.all(), start=1)],
            'ValDtls': {
                'AssVal': money(self.invoice.taxable_amount),
                'CgstVal': money(self.invoice.cgst_amount),
                'SgstVal': money(self.invoice.sgst_amount),
                'IgstVal': money(self.invoice.igst_amount),
                'CesVal': money(sum((decimal_value(line.cess_amount) for line in self.invoice.items.all()), ZERO)),
                'Discount': money(self.invoice.discount_amount),
                'OthChrg': money(self.invoice.tcs_amount),
                'RndOffAmt': money(self.invoice.round_off),
                'TotInvVal': money(self.invoice.grand_total),
            },
        }
        return {'payload': payload, 'validation': {'errors': self.validation_errors, 'warnings': self.validation_warnings}}

    def _item(self, index, line):
        return {
            'SlNo': str(index),
            'PrdDesc': line.item.name[:300],
            'IsServc': 'N',
            'HsnCd': hsn_code(line),
            'Qty': quantity(line.quantity),
            'Unit': unit_code(line),
            'UnitPrice': money(line.rate),
            'TotAmt': money(decimal_value(line.quantity) * decimal_value(line.rate)),
            'Discount': money(line.discount),
            'AssAmt': money(line.taxable_amount),
            'GstRt': rate(gst_rate(line)),
            'IgstAmt': money(line.igst_amount),
            'CgstAmt': money(line.cgst_amount),
            'SgstAmt': money(line.sgst_amount),
            'CesRt': rate(line.cess_rate),
            'CesAmt': money(line.cess_amount),
            'TotItemVal': money(line.total),
        }

    def _validate(self):
        if self.invoice.status != 'Finalized':
            self.validation_errors.append({'field': 'status', 'message': 'Only finalized invoices can be reported for IRN.'})
        if self.invoice.invoice_type not in {'Tax Invoice', 'Export', 'SEZ'}:
            self.validation_errors.append({'field': 'invoice_type', 'message': 'E-invoice applies to B2B, export, or SEZ invoices.'})
        if self.invoice.invoice_type == 'Tax Invoice' and not getattr(self.invoice.party, 'gstin', ''):
            self.validation_errors.append({'field': 'party.gstin', 'message': 'Recipient GSTIN is required for B2B e-invoice.'})
        if len(self.invoice.invoice_number or '') > 16:
            self.validation_warnings.append({'field': 'invoice_number', 'message': 'IRP document number is limited to 16 characters; payload is truncated.'})
        for line in self.invoice.items.all():
            if not hsn_code(line):
                self.validation_errors.append({'field': 'hsn_code', 'message': f'Line {line.pk} has no HSN/SAC.'})


class EWayBillPayloadBuilder:
    """Build an e-way bill request payload for a sales invoice."""

    def __init__(self, invoice, transport=None):
        self.invoice = invoice
        self.transport = transport or {}
        self.validation_errors = []
        self.validation_warnings = []

    def build(self):
        self._validate()
        seller = self.invoice.business_location
        buyer = self.invoice.party
        payload = {
            'supplyType': 'O',
            'subSupplyType': '1',
            'docType': 'INV',
            'docNo': self.invoice.invoice_number[:16],
            'docDate': self.invoice.invoice_date.strftime('%d/%m/%Y'),
            'fromGstin': seller.gstin,
            'fromTrdName': seller.trade_name or seller.legal_name,
            'fromAddr1': address_line(seller.address),
            'fromPlace': seller.name[:50],
            'fromPincode': int(self.transport.get('from_pincode') or pincode_from_settings()),
            'fromStateCode': int(state_code(seller.state) or 0),
            'actFromStateCode': int(state_code(seller.state) or 0),
            'toGstin': (getattr(buyer, 'gstin', '') or 'URP').upper(),
            'toTrdName': getattr(buyer, 'name', 'Cash Customer')[:100],
            'toAddr1': address_line(getattr(buyer, 'shipping_address', '') or getattr(buyer, 'address', '')),
            'toPlace': getattr(self.invoice.billing_state, 'name', '')[:50] or 'NA',
            'toPincode': int(self.transport.get('to_pincode') or pincode_from_settings()),
            'toStateCode': int(state_code(self.invoice.billing_state) or 0),
            'actToStateCode': int(state_code(self.invoice.billing_state) or 0),
            'transactionType': 1,
            'totalValue': money(self.invoice.taxable_amount),
            'cgstValue': money(self.invoice.cgst_amount),
            'sgstValue': money(self.invoice.sgst_amount),
            'igstValue': money(self.invoice.igst_amount),
            'cessValue': money(sum((decimal_value(line.cess_amount) for line in self.invoice.items.all()), ZERO)),
            'totInvValue': money(self.invoice.grand_total),
            'transMode': str(self.transport.get('transport_mode', '1')),
            'transDistance': str(self.transport.get('distance_km', 0)),
            'transporterId': self.transport.get('transporter_id', ''),
            'transporterName': self.transport.get('transporter_name', ''),
            'transDocNo': self.transport.get('transport_doc_no', ''),
            'transDocDate': self.transport.get('transport_doc_date', ''),
            'vehicleNo': self.transport.get('vehicle_no', ''),
            'vehicleType': self.transport.get('vehicle_type', 'R'),
            'itemList': [self._item(index, line) for index, line in enumerate(self.invoice.items.all(), start=1)],
        }
        return {'payload': payload, 'validation': {'errors': self.validation_errors, 'warnings': self.validation_warnings}}

    def _item(self, index, line):
        return {
            'itemNo': index,
            'productName': line.item.name[:100],
            'productDesc': line.item.description[:300] or line.item.name[:100],
            'hsnCode': hsn_code(line),
            'quantity': quantity(line.quantity),
            'qtyUnit': unit_code(line),
            'taxableAmount': money(line.taxable_amount),
            'sgstRate': rate(line.sgst_rate),
            'cgstRate': rate(line.cgst_rate),
            'igstRate': rate(line.igst_rate),
            'cessRate': rate(line.cess_rate),
        }

    def _validate(self):
        if self.invoice.status != 'Finalized':
            self.validation_errors.append({'field': 'status', 'message': 'Only finalized invoices can generate e-way bills.'})
        if decimal_value(self.invoice.grand_total) <= decimal_value(getattr(settings, 'GST_EWAY_BILL_THRESHOLD', Decimal('50000'))):
            self.validation_warnings.append({'field': 'grand_total', 'message': 'Invoice value is at or below the usual e-way bill threshold.'})
        if not self.transport.get('distance_km'):
            self.validation_warnings.append({'field': 'distance_km', 'message': 'Transport distance is required for live e-way bill generation.'})
        for line in self.invoice.items.all():
            if not hsn_code(line):
                self.validation_errors.append({'field': 'hsn_code', 'message': f'Line {line.pk} has no HSN/SAC.'})


class TallyVoucherXMLBuilder:
    """Build TallyPrime voucher import XML for finalized sales invoices."""

    def __init__(self, invoices, company_name=''):
        self.invoices = list(invoices)
        self.company_name = company_name

    def build(self):
        envelope = ET.Element('ENVELOPE')
        header = ET.SubElement(envelope, 'HEADER')
        ET.SubElement(header, 'TALLYREQUEST').text = 'Import Data'
        body = ET.SubElement(envelope, 'BODY')
        import_data = ET.SubElement(body, 'IMPORTDATA')
        request_desc = ET.SubElement(import_data, 'REQUESTDESC')
        ET.SubElement(request_desc, 'REPORTNAME').text = 'Vouchers'
        if self.company_name:
            static_variables = ET.SubElement(request_desc, 'STATICVARIABLES')
            ET.SubElement(static_variables, 'SVCURRENTCOMPANY').text = self.company_name
        request_data = ET.SubElement(import_data, 'REQUESTDATA')
        for invoice in self.invoices:
            message = ET.SubElement(request_data, 'TALLYMESSAGE')
            self._voucher(message, invoice)
        return ET.tostring(envelope, encoding='unicode')

    def _voucher(self, parent, invoice):
        voucher = ET.SubElement(parent, 'VOUCHER', {'VCHTYPE': 'Sales', 'ACTION': 'Create'})
        ET.SubElement(voucher, 'DATE').text = invoice.invoice_date.strftime('%Y%m%d')
        ET.SubElement(voucher, 'VOUCHERTYPENAME').text = 'Sales'
        ET.SubElement(voucher, 'VOUCHERNUMBER').text = invoice.invoice_number
        ET.SubElement(voucher, 'PARTYLEDGERNAME').text = getattr(invoice.party, 'name', 'Cash Sales')
        ET.SubElement(voucher, 'PERSISTEDVIEW').text = 'Accounting Voucher View'
        self._ledger(voucher, getattr(invoice.party, 'name', 'Cash Sales'), invoice.grand_total, is_debit=True)
        self._ledger(voucher, 'Sales', invoice.taxable_amount, is_debit=False)
        if decimal_value(invoice.cgst_amount):
            self._ledger(voucher, 'Output CGST', invoice.cgst_amount, is_debit=False)
        if decimal_value(invoice.sgst_amount):
            self._ledger(voucher, 'Output SGST', invoice.sgst_amount, is_debit=False)
        if decimal_value(invoice.igst_amount):
            self._ledger(voucher, 'Output IGST', invoice.igst_amount, is_debit=False)
        if decimal_value(invoice.round_off):
            self._ledger(voucher, 'Round Off', invoice.round_off, is_debit=decimal_value(invoice.round_off) < ZERO)

    def _ledger(self, parent, name, amount, is_debit):
        entry = ET.SubElement(parent, 'ALLLEDGERENTRIES.LIST')
        ET.SubElement(entry, 'LEDGERNAME').text = name
        ET.SubElement(entry, 'ISDEEMEDPOSITIVE').text = 'Yes' if is_debit else 'No'
        signed = decimal_value(amount) if is_debit else -decimal_value(amount)
        ET.SubElement(entry, 'AMOUNT').text = str(signed.quantize(MONEY_QUANT))
