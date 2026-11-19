# Invoice-Ninja-Bank-Import

[English](README.md) | **Deutsch**

Importiert Banktransaktionen aus CAMT.053-Kontoauszügen in [Invoice Ninja](https://invoiceninja.com) (v5, selbst gehostet oder gehostet), inklusive:

- **Prüfung vor dem Import:** In einer lokalen HTML-Seite wählen Sie aus, welche Buchungen betrieblich und welche privat sind.
- **Zahlungszuordnung:** Eingehende Zahlungen werden über die Rechnungsnummer im Verwendungszweck offenen Rechnungen zugeordnet. Dabei wird eine Zahlung angelegt und die Rechnung als bezahlt markiert.
- **Ausgaben:** Ausgehende Buchungen werden zu Ausgaben mit Lieferant, Kategorie und USt. Fehlende Lieferanten und Kategorien werden angelegt.
- **Umsatzsteuer:** Die Vorgaben folgen den österreichischen Regeln (20 / 13 / 10 %, steuerfrei, Reverse Charge, ausländische USt, kein Vorsteuerabzug beim PKW). Der Vorschlag richtet sich nach dem Land des Zahlungsempfängers.
- **Sichere Testimporte:** Jeder Lauf wird protokolliert und lässt sich vollständig rückgängig machen.

Getestet mit CAMT.053-Exporten von Erste Bank / George Business (Österreich). Andere Banken, die camt.053.001.02 exportieren, sollten ebenfalls funktionieren.

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

## Weitere Optionen

| Option | Wirkung |
|---|---|
| `--limit N` | nur die ersten N Buchungen importieren (Testlauf) |
| `--credits-only` / `--debits-only` | nur eingehende bzw. nur ausgehende Buchungen |
| `--since JJJJ-MM-TT` | ältere Buchungen ignorieren |

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

Kontoauszüge, Auswahl, Zuordnungen, Laufprotokolle und die erzeugten HTML-Seiten bleiben lokal und sind per `.gitignore` ausgeschlossen. Daten werden nur an Ihre eigene Invoice-Ninja-Instanz gesendet.

## Hinweise

- **Unvollständige Zertifikatskette:** Wenn Ihr Server nur das Endzertifikat sendet (`CERTIFICATE_VERIFY_FAILED`), setzen Sie `IN_CA_FILE` auf die PEM-Datei des Zwischenzertifikats. Die eigentliche Lösung ist, die vollständige Kette am Server zu konfigurieren.
- **Keine automatische Zuordnung:** Invoice Ninja ordnet per API angelegte Transaktionen nicht automatisch zu. Verwenden Sie dafür `--match`.

## Lizenz

[MIT](LICENSE)
