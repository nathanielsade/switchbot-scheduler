// Max collector: prints the finance JSON contract to stdout ONLY. Reads MAX_* from env.
const { createScraper, CompanyTypes } = require('israeli-bank-scrapers');

(async () => {
  try {
    const scraper = createScraper({ companyId: CompanyTypes.max, startDate: new Date(process.env.FINANCE_START_DATE), combineInstallments: false, showBrowser: false });
    const result = await scraper.scrape({ username: process.env.MAX_USERNAME, password: process.env.MAX_PASSWORD });
    if (!result.success) { console.error(`scrape failed: ${result.errorType} ${result.errorMessage || ''}`); process.exit(2); }
    const out = {
      source: 'max', scraped_at: new Date().toISOString(),
      accounts: (result.accounts || []).map(a => ({
        account: String(a.accountNumber),
        balance: a.balance == null ? '0' : Number(a.balance).toFixed(2),
        transactions: (a.txns || []).map(t => ({
          identifier: t.identifier == null ? null : String(t.identifier),
          date: t.date, processedDate: t.processedDate || null,
          chargedAmount: Number(t.chargedAmount).toFixed(2),
          chargedCurrency: t.originalCurrency || 'ILS',
          description: t.description || '', status: t.status || 'completed',
        })),
      })),
    };
    process.stdout.write(JSON.stringify(out));
  } catch (e) { console.error(String(e && e.stack || e)); process.exit(1); }
})();
