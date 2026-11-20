# Invoice-Ninja-Bank-Import

[English](README.md) | **Deutsch**

Importiert Banktransaktionen aus CAMT.053-Kontoauszügen in [Invoice Ninja](https://invoiceninja.com) (v5, selbst gehostet oder gehostet), inklusive:

- **Prüfung vor dem Import:** In einer lokalen HTML-Seite wählen Sie aus, welche Buchungen betrieblich und welche privat sind.
- **Zahlungszuordnung:** Eingehende Zahlungen werden über die Rechnungsnummer im Verwendungszweck offenen Rechnungen zugeordnet. Dabei wird eine Zahlung angelegt und die Rechnung als bezahlt markiert.
- **Ausgaben:** Ausgehende Buchungen werden zu Ausgaben mit Lieferant, Kategorie und USt. Fehlende Lieferanten und Kategorien werden angelegt.
- **Umsatzsteuer:** Die Vorgaben folgen den österreichischen Regeln (20 / 13 / 10 %, steuerfrei, Reverse Charge, ausländische USt, kein Vorsteuerabzug beim PKW). Der Vorschlag richtet sich nach dem Land des Zahlungsempfängers.
- **Sichere Testimporte:** Jeder Lauf wird protokolliert und lässt sich vollständig rückgängig machen.

Liest **CAMT.053**-XML und den **George-JSON-Export** (Erste Bank / George Business, Österreich). Buchungen, die in beiden Formaten vorkommen, werden nur einmal importiert. Andere Banken, die camt.053.001.02 exportieren, sollten ebenfalls funktionieren.

Benötigt wird nur die Python-3-Standardbibliothek.

## Einrichtung

```sh
cp .env.example .env      # IN_URL und IN_TOKEN eintragen (Invoice Ninja → Einstellungen → Kontoverwaltung → API-Tokens)
chmod 600 .env
```

## Ablauf

1. **Exportieren:** CAMT.053-Kontoauszüge aus dem Online-Banking exportieren. Den Ordner oder die XML-Dateien neben die Skripte legen.
2. **Betriebliche Buchungen auswählen:**
   ```sh
   ./make_review.py <camt-ordner>        # erzeugt review.html
   ```
   review.html öffnen, die betrieblichen Buchungen anhaken und **Save selection** klicken. Die Datei `ninja_selection.json` aus den Downloads in diesen Ordner verschieben.
3. **Ausgaben zuordnen** (Lieferant, Kategorie und USt je Gegenpartei):
   ```sh
   ./make_expenses.py <camt-ordner>      # erzeugt expenses.html
   ```
   **Save mapping** klicken und `expense_map.json` in diesen Ordner verschieben.
   Optional: `./make_expenses.py --learn` erzeugt Ihre eigenen Lieferanten-Vorschläge (`suggestions.json`). Grundlage sind die Lieferanten, Kategorien und USt Ihrer Ausgaben in Invoice Ninja sowie `expense_map.json`. Neue Gegenparteien desselben Lieferanten sind dann vorausgefüllt. Die Datei bleibt lokal und wird nicht ins Git übernommen. Ohne sie werden nur einige allgemeine Bank-Einträge vorgeschlagen.
4. **Vorschau, dann Import:**
   ```sh
   ./camt2ninja.py <camt-ordner> --only ninja_selection.json --match --expenses expense_map.json            # Probelauf
   ./camt2ninja.py <camt-ordner> --only ninja_selection.json --match --expenses expense_map.json --commit
   ```
5. **Bei Bedarf rückgängig machen:**
   ```sh
   ./camt2ninja.py --runs                    # Läufe anzeigen
   ./camt2ninja.py --rollback last --commit  # löscht Zahlungen, angelegte Ausgaben/Lieferanten/Kategorien und Transaktionen des Laufs
   ```
   Was Sie in Invoice Ninja von Hand zugeordnet haben, bleibt erhalten und wird aufgelistet.

Im nächsten Monat sind Auswahl und Zuordnung vorausgefüllt. Sie entscheiden nur noch über neue Buchungen und neue Gegenparteien. Bereits importierte Buchungen werden automatisch übersprungen.

## Weitere Funktionen

- **Bereits bezahlte Rechnungen:** Ist eine Rechnung in Invoice Ninja schon als bezahlt markiert, verknüpft `--match` die Bankbuchung mit der vorhandenen Zahlung und setzt das Zahlungsdatum auf das Bankdatum, statt eine zweite Zahlung anzulegen.
- **PayPal:** Legen Sie PayPal-Aktivitätsexporte (CSV) in einen Ordner `PAYPAL/`. Bankbuchungen wie `1234567890123/PAYPAL` erhalten dann den echten Händler (z. B. „Example Music AS (PayPal)“). `./make_review.py --paypal <ordner>` erzeugt eine Auswahlseite nur mit den PayPal-Buchungen.
- **Sonderfälle** in `overrides.json` (lokal, nicht im Git), Schlüssel `bankref|CRDT/DBIT|betrag`:
  - `{"action": "passthrough", "note": "..."}`: weder Rechnung noch Ausgabe, z. B. ein Durchlaufposten.
  - `{"action": "link_payment", "payment": "2026/000123"}`: mit dieser vorhandenen Zahlung verknüpfen.
  - `{"action": "replace_payment", "payment": "...", "invoice": "..."}`: eine Zahlung mit falschem Betrag durch den Bankbetrag ersetzen.
  - `{"action": "expense", "vendor": "...", "category": "...", "vat": "rc"}`: diese eine Buchung als Ausgabe.

## Rechnungen aus einem früheren Programm

`./ebinterface2ninja.py <ordner> [--prefix PS-]` importiert Ausgangsrechnungen aus **ebInterface-XML** (z. B. ProSaldo.net-Export):
- **Was angelegt wird:** Kunden, zugeordnet über die UID (fehlende werden angelegt), alle Positionen mit Rabatt und USt, das PDF als Anhang. Die Rechnungen werden als versendet markiert, ohne E-Mail.
- **Nummern:** `PS-2024-019`, damit sie nicht mit der Invoice-Ninja-Nummerierung kollidieren.
- **Gutschriften** stornieren die passende Rechnung: gleicher Kunde, gleicher Betrag, bevorzugt vom selben Tag.
- **Rückgängig:** `--rollback last --commit`.

Setzen Sie `IN_LEGACY_PREFIX=PS-` und `IN_LEGACY_UNTIL=2024-08-01` in der `.env`. Zahlungen vor diesem Datum, die auf die alten Nummern verweisen („RN 19“, „Rechnung-Nr. 22+24“, „Invoice 40“, „#38“), ordnet `--match` dann zu, wenn der Betrag passt.

## Weitere Optionen

| Option | Wirkung |
|---|---|
| `--limit N` | nur die ersten N Buchungen importieren (Testlauf) |
| `--credits-only` / `--debits-only` | nur eingehende bzw. nur ausgehende Buchungen |
| `--since JJJJ-MM-TT` | ältere Buchungen ignorieren |
| `--overrides DATEI` | Sonderfälle (Standard `overrides.json`) |
| `--existing` | mit `--expenses`: bereits importierte, noch nicht zugeordnete Buchungen nachträglich zu Ausgaben machen (z. B. nach einem PayPal-Export); ein Rollback hebt die Verknüpfung wieder auf |

## USt-Codes in der Zuordnung

| Code | Bedeutung |
|---|---|
| `20`, `13`, `10` | inländische USt, im Betrag enthalten |
| `0` | steuerfrei (Versicherung, Bankspesen, Sozialversicherung) |
| `rc` | Reverse Charge: USt als Leistungsempfänger geschuldet (§19 UStG) |
| `foreign` | ausländische USt, vor Ort bezahlt, nicht abzugsfähig |
| `nodeduct` | kein Vorsteuerabzug (z. B. PKW-Kosten) |

Die Steuernamen werden aus den in Invoice Ninja angelegten Steuersätzen übernommen.

Diese Vorgaben sind Standardwerte und keine Steuerberatung. Bitte prüfen Sie sie mit Ihrer Steuerberatung.

## Datenschutz

Kontoauszüge, Auswahl, Zuordnungen, Lieferanten-Vorschläge, Laufprotokolle und die erzeugten HTML-Seiten bleiben lokal und sind per `.gitignore` ausgeschlossen. Daten werden nur an Ihre eigene Invoice-Ninja-Instanz gesendet.

## Hinweise

- **Unvollständige Zertifikatskette:** Wenn Ihr Server nur das Endzertifikat sendet (`CERTIFICATE_VERIFY_FAILED`), setzen Sie `IN_CA_FILE` auf die PEM-Datei des Zwischenzertifikats. Die eigentliche Lösung ist, die vollständige Kette am Server zu konfigurieren.
- **Keine automatische Zuordnung:** Invoice Ninja ordnet per API angelegte Transaktionen nicht automatisch zu. Verwenden Sie dafür `--match`.

## Lizenz

[MIT](LICENSE)
