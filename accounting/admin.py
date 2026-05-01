from django.contrib import admin

from .models import Expense, FiscalPeriod, JournalEntry, JournalLine, LedgerAccount, PostingBatch, Reconciliation


admin.site.register(LedgerAccount)
admin.site.register(FiscalPeriod)
admin.site.register(PostingBatch)
admin.site.register(JournalEntry)
admin.site.register(JournalLine)
admin.site.register(Reconciliation)
admin.site.register(Expense)
