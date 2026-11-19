# Invoice-Ninja-Bank-Import

Import your bank transactions into [Invoice Ninja](https://invoiceninja.com) (v5, self-hosted or hosted) from CAMT.053 statements, including:

- **Review before import:** a local HTML page where you tick which bookings are business and which are private.
- **Payment matching:** incoming payments are matched to open invoices by invoice number in the payment reference. A payment is created and the invoice is marked paid.
- **Expenses:** outgoing bookings become expenses with vendor, category and VAT. Missing vendors and categories are created.
- **VAT handling:** presets follow Austrian rules (20 / 13 / 10 %, exempt, reverse charge, foreign VAT, no input VAT for passenger cars), with the default picked from the counterparty's country.
- **Safe test imports:** every run is logged and can be fully rolled back.

Tested with Erste Bank / George Business (Austria) CAMT.053 exports. Other banks that export camt.053.001.02 should work too.

Only the Python 3 standard library is needed.

## Setup

```sh
cp .env.example .env      # set IN_URL and IN_TOKEN (Invoice Ninja → Settings → Account Management → API Tokens)
chmod 600 .env
```

## Workflow

1. **Export** CAMT.053 statements from your bank and put the folder or XML files next to the scripts.
2. **Choose business bookings:**
   ```sh
   ./make_review.py <camt-folder>        # writes review.html
   ```
   Open review.html, tick the business bookings, click **Save selection**, and move `ninja_selection.json` into this folder.
3. **Map expenses** (vendor, category, VAT per counterparty):
   ```sh
   ./make_expenses.py <camt-folder>      # writes expenses.html
   ```
   Click **Save mapping** and move `expense_map.json` into this folder.
4. **Preview, then import:**
   ```sh
   ./camt2ninja.py <camt-folder> --only ninja_selection.json --match --expenses expense_map.json            # dry run
   ./camt2ninja.py <camt-folder> --only ninja_selection.json --match --expenses expense_map.json --commit
   ```
5. **Undo if needed:**
   ```sh
   ./camt2ninja.py --runs                    # list runs
   ./camt2ninja.py --rollback last --commit  # delete payments, created expenses/vendors/categories and transactions of a run
   ```
   Anything you matched by hand in Invoice Ninja is kept and listed.

Your previous selection and mapping are pre-filled next month, so only new bookings and counterparties need a decision. Already imported bookings are skipped automatically.

## Other options

| Option | Effect |
|---|---|
| `--limit N` | import only the first N bookings (test run) |
| `--credits-only` / `--debits-only` | only incoming / outgoing bookings |
| `--since YYYY-MM-DD` | ignore older bookings |

## VAT codes in the mapping

| Code | Meaning |
|---|---|
| `20`, `13`, `10` | domestic VAT included in the amount |
| `0` | exempt (insurance, bank charges, social insurance) |
| `rc` | reverse charge: VAT owed as recipient (§19 UStG) |
| `foreign` | foreign VAT paid on site, not deductible |
| `nodeduct` | no input VAT deduction (e.g. passenger car costs) |

The tax names are taken from the tax rates configured in Invoice Ninja.

These presets are defaults, not tax advice. Check them with your tax advisor.

## Privacy

Bank statements, selections, mappings, run logs and the generated HTML pages stay local and are excluded by `.gitignore`. Nothing is sent anywhere except to your own Invoice Ninja instance.

## Notes

- **Incomplete certificate chain:** if your server sends only the leaf certificate (`CERTIFICATE_VERIFY_FAILED`), set `IN_CA_FILE` to the intermediate certificate PEM. The proper fix is to configure the full chain on the server.
- **No auto-matching:** Invoice Ninja does not auto-match transactions created through the API. Use `--match` instead.

## License

[MIT](LICENSE)
