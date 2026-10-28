# E2E: Import kontrahentów KSeF → Symfonia FK „50" (Import specjalny / format 3.0)

> AGENT-FACING. Dense. Źródło reguł: pomoc.symfonia.pl art. 11288 „Import specjalny (format 3.0)" → podstrony „Kartoteki" + „Specyfikacja sekcji wyjściowych (dla FK)". Zweryfikowane 2026-07-08.
> Cel: zarejestrować kontrahentów w kartotece FK PRZED importem faktur, by faktury matchowały po NIP (`#DANEKH "2"` w SKsefInvoiceImport.AMS) zamiast tworzyć „gołe" wpisy.

## SCOPE
IN: kontrahenci wyekstrahowani z faktur KSeF (FA(3): Podmiot1=sprzedawca, Podmiot2=nabywca). OUT: plik `SDF` + szablon `Section("","kontrahent")` → import ręczny w „Katalogi wymiany danych". NIE dotyczy: importu faktur (osobny pipeline SKsefInvoiceImport).

## INWARIANTY (HARD — złamanie = fail/skip cicho)
- `INV1` Sekcja wyjściowa NAME == `"kontrahent"` (dosłownie, lowercase). Inna nazwa → dane nie trafią do kartoteki kontrahentów.
- `INV2` `$klucz` NIEPUSTY. Pusty → **komunikat + ABORT CAŁEGO importu** (nie tylko rekordu).
- `INV3` `$klucz != "0"`. Wartość `"0"` → **rekord cicho pominięty, ZERO komunikatu** (najgorsza pułapka — wygląda na sukces).
- `INV4` `$klucz` UNIKALNY per kontrahent w obrębie pliku. Duplikat → różni kontrahenci sklejeni / błędne przypisanie do kont/dokumentów.
- `INV5` `$klucz` STABILNY między przebiegami (ten sam kontrahent = ten sam klucz zawsze) → ponowny import = update, nie duplikat. → **klucz = znormalizowany NIP**.
- `INV6` Plik: kodowanie **Windows-1250**, końce linii **CRLF**, 1 kontrahent = 1 wiersz, pola rozdzielone `;`, wartości tekstowe w `"..."`.
- `INV7` NIE używać `KONTRAH.AMX` / `KONTRAHFK.AMX` (nagłówek: „Plik pomocniczy do importu, nie używać bezpośrednio"). Zawsze WŁASNY szablon wg §TEMPLATE.

## PRECONDITIONS
- `nip_wlasny` = NIP profilu (firmy), znormalizowany (same cyfry).
- Zbiór faktur `INV[]` pobrany z KSeF (query metadata Subject1+Subject2 + download XML). Patrz `reference/ksef2-recipe.md`.
- Katalog roboczy importu, np. `C:\ksef-import\<profil>\kontrahenci\`.

## ALGORYTM (deterministyczny)
```
1. parties = []
   for fa in INV:                      # fa = surowy XML FA(3)
       for rola,podmiot in [("sprzedawca", fa.Podmiot1), ("nabywca", fa.Podmiot2)]:
           nip = digits_only(podmiot.DaneIdentyfikacyjne.NIP)
           if nip == nip_wlasny: continue         # pomijamy siebie
           parties.append( extract_contractor(podmiot) )
2. uniq = dedup_by(parties, key=normalized_nip)    # 1 rekord / NIP; przy kolizji: preferuj rekord z pełniejszym adresem
3. rows = []
   for c in uniq:
       klucz = c.nip_norm if c.nip_norm else "K_" + sha1(c.nazwa + "|" + c.miejscowosc)[:12]   # fallback os.fiz/brak NIP
       assert klucz and klucz != "0"               # INV2/INV3
       rows.append( sdf_row(klucz, c) )
4. write_file("kontrahenci_<profil>_<YYYYMMDD>.txt", rows, encoding="cp1250", newline="\r\n")   # INV6
5. ensure_template("kontrahent_import.txt")         # §TEMPLATE, raz
6. IMPORT_MANUAL (§UI) → weryfikacja (§GATES)
```

## TEMPLATE (dokładny — zapisać jako plik szablonu, np. `kontrahent_import.txt`)
```
#format "SDF"
#disable 2
Section("", "kontrahent")
{
	$klucz       = [1]
	$skrot       = [2]
	$nazwa       = [3]
	$nip         = [4]
	$kraj        = [5]
	$miejscowosc = [6]
	$kod         = [7]
	$ulica       = [8]
	$numerDomu   = [9]
	$numerMieszk = [10]
	$email       = [11]
	$telefon1    = [12]
	$regon       = [13]
}
```
- `[N]` = 1-based numer kolumny w pliku SDF. Kolejność kolumn w pliku MUSI = kolejność wyżej.
- Pola opcjonalne mogą być puste (""), ale kolumna musi istnieć (zachować liczbę separatorów).
- Nazwy `$pól` kartoteki kontrahenta (pełny zestaw, referencja z KONTRAH.AMX): `$klucz $skrot $nazwa $nip $kraj $miejscowosc $gmina $ulica $numerDomu $numerMieszk $kod $poczta $telefon1 $telefon2 $email $nazwisko $imie $regon $pesel $w_banku1 $rachunek1 $typ $aktywny $uwagi $osfiz`.

## SDF_ROW (format wiersza; kolumny 1..13 wg TEMPLATE)
```
"5252445719";"DOSTAWCA";"Dostawca Testowy Sp. z o.o.";"525-24-45-719";"PL";"Warszawa";"00-001";"Przykladowa";"12";"3";"biuro@x.pl";"221234567";"012345678"
```
Reguły wartości:
- `$klucz` (kol.1) = NIP cyfry-only (bez myślników) — stabilny, unikalny. NIGDY pusty ani "0".
- `$nip` (kol.4) = NIP w formacie czytelnym (z myślnikami OK) — pole widoczne w kartotece.
- `$skrot` (kol.2) = symbol ≤ limit programu (bezpiecznie ≤ 20 zn.); deterministyczny z nazwy/NIP.
- Tekst zawsze w `"..."` (chroni przed `;` w treści). Puste pole = `""`.

## FIELD_MAP  FA(3) → kontrahent
| kontrahent | FA(3) źródło (Podmiot1/2) |
|---|---|
| $klucz | `digits_only(DaneIdentyfikacyjne/NIP)` (fallback hash) |
| $nip | `DaneIdentyfikacyjne/NIP` |
| $skrot | z `Nazwa`/`PelnaNazwa` → skrócone, lub NIP |
| $nazwa | `DaneIdentyfikacyjne/Nazwa` (FA(3)) / `PelnaNazwa` |
| $kraj | `Adres/KodKraju` (domyślnie "PL") |
| $miejscowosc | `Adres/Miejscowosc` (FA(3)) — jeśli tylko `AdresL1/L2`, sparsować |
| $ulica / $numerDomu / $numerMieszk | `Adres/Ulica,NrDomu,NrLokalu` (FA(3) rozbite) lub parse z `AdresL1` |
| $kod | `Adres/KodPocztowy` |
| $regon | `DaneIdentyfikacyjne/REGON` (o ile jest) |
> UWAGA: FA(3) MF ma inne nazwy pól niż znormalizowany `invoiceV2` z SKsefInvoiceImport.xslt. Ekstrakcję rób z RAW FA(3) (XSD: CIRFMF/ksef-api schemat_FA(3)_v1-0E). Jeśli w pipeline i tak budujesz `ksefInvoice/invoiceV2`, kontrahenta czerp stamtąd (Sprzedawca/Nabywca) — spójne z importem faktur.

## ERROR → ACTION
| Objaw | Przyczyna | Akcja |
|---|---|---|
| Import przerwany od razu, komunikat o kluczu | `$klucz` pusty gdzieś w pliku (INV2) | walidacja PRZED zapisem: odrzuć/hashuj rekord bez NIP |
| „Sukces" ale brak części kontrahentów | `$klucz=="0"` (INV3) | nigdy nie emituj "0"; NIP=="0"/brak → fallback hash |
| Kontrahenci sklejeni / złe konta | duplikat `$klucz` (INV4) | dedup po NIP przed zapisem |
| Dane nie w kartotece kontrahentów | zła nazwa sekcji (INV1) | sekcja == `"kontrahent"` |
| Krzaki zamiast ą/ż/ł | złe kodowanie (INV6) | zapis cp1250 + CRLF |
| „aplikacja nie czyta po szablonie" | użyto KONTRAH*.AMX bezpośrednio (INV7) | własny szablon §TEMPLATE |

## UI (import ręczny w Symfonii)
```
Konfiguracja współpracy → Katalogi wymiany danych → [Nowy]
  Nazwa   = "KSeF kontrahenci <profil>"
  Katalog = C:\ksef-import\<profil>\kontrahenci\
  Maska plików danych = *.txt
  Szablon = <ścieżka>\kontrahent_import.txt        # NASZ szablon §TEMPLATE (NIE KONTRAH.AMX)
  XSLT    = (puste — wyszarzone, OK; SDF nie wymaga XSLT)
  [x] Ja będę importował dane dla tego profilu
  [Zapisz] → zaznacz profil → [Użyj]
```

## GATES (weryfikacja E2E — wszystkie muszą przejść)
- [ ] `G1` plik: liczba wierszy == liczba unikalnych NIP; brak wiersza z pustym/"0" kluczem; cp1250+CRLF.
- [ ] `G2` import bez błędu; kartoteka kontrahentów += N.
- [ ] `G3` idempotencja: ponowny import tego samego pliku → 0 nowych duplikatów (bo $klucz=NIP stabilny, INV5).
- [ ] `G4` integracja: po imporcie kontrahentów → import faktur (SKsefInvoiceImport) → faktura podpina istniejącego kontrahenta po NIP, NIE tworzy nowego „gołego".

## KOLEJNOŚĆ vs import faktur
Rekomendacja: kontrahenci NAJPIERW (ten E2E), potem faktury. Ale nie jest to twarde — import faktur i tak zakłada brakujących kontrahentów z danych faktury (`#DANEKH "2"`). Rejestracja wcześniejsza = kontrola nad symbolem/kontem/danymi kontrahenta zanim wejdą faktury.
