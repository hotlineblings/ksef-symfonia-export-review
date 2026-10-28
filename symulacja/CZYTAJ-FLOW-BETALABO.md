# Plik flow (BETALABO) -- minimalny test całego przebiegu importu do Symfonii FK

Ten katalog zawiera WYŁĄCZNIE dane testowe wygenerowane skryptem
`generuj_flow_betalabo.py`, do jednorazowego, RĘCZNEGO wypróbowania importu w
Symfonii FK na kopii/testowej bazie firmy. Nic tu nie dotyka bazy
produkcyjnej (`%APPDATA%\KsefSymfoniaExport`) -- rejestr numerów i ledger są
osobnymi, tymczasowymi plikami `.db` w tym katalogu (`rejestr_flow.db`,
`ledger_flow.db`), ODRĘBNYMI od tych używanych przez `generuj_symulacje.py`.

Kartoteka źródłowa (`C:\Users\Anna\ksef-symfonia-export\tests\fixtures\kartoteka_google_sheets.csv`) jest wczytywana w POSTACI SUROWEJ, bez
żadnej normalizacji -- plik ma śmieciowy pierwszy wiersz (same przecinki,
nagłówek dopiero w drugim wierszu) i dwie nadmiarowe kolumny bez nazwy na
końcu każdego wiersza (eksport z Arkuszy Google). To jednocześnie test
odporności importera (`app/kontrahenci_nr.py::_wczytaj_csv` /
`_czy_wiersz_naglowka` / `_mapuj_wiersze_danych`) -- bramka w skrypcie
sprawdza tuż po seedzie: dokładnie 33 wpisy w rejestrze, a MERIDOL TOMASZ
KOWALSKI (NIP 9100010014) ląduje na pozycji 5.

## Co zawiera plik wynikowy

Plik: `symulacja\wynik_flow\betalabo-flow\hmffk_betalabo-flow_2026-07-01_2026-07-31_20260824-144223.TXT`

Format: AMS "Dane z programu Handel, format FK 3.0" (szablon `hmffk.AMS`),
CP1250 + CRLF. Zawiera:

- 2 sekcje `Kontrahent{}` -- 1 istniejący (MERIDOL, emitowany z danymi z tej
  faktury) + 1 nowy (Testowy Odbiorca Flow),
- 2 sekcje `dokument{}` -- 1 faktura kosztowa (FVZ) + 1 sprzedażowa (FVS),
  każda z numerem KSeF, rejestrem VAT (23%, netto 100,00 / VAT 23,00 /
  brutto 123,00) i PEŁNYM dekretem rozrachunkowym (sekcje `zapis{}` +
  `transakcja{}`): linia brutto kontrahenta dostaje konto rozrachunkowe,
  `NumerDok`, `IdDlaRozliczen` i sekcję `transakcja{}` z terminem płatności
  (2026-08-11) -- FK zakłada rozrachunek automatycznie, bez ręcznego
  wypełniania.
  - **Dekret sprzedaży** (WN brutto / MA netto / MA VAT): konto rozrachunkowe
    na WN brutto, dodatkowo `703` na MA netto i
    `222-1` na MA VAT.
  - **Dekret kosztowy** (WN netto / MA brutto / WN VAT -- kolejność jak w
    oryginalnym dekrecie zakupu Symfonii): WN netto zostaje BEZ konta
    (dekretuje księgowa), MA brutto dostaje konto rozrachunkowe (+
    rozrachunek), WN VAT dostaje `221-1` (VAT naliczony).
  - **Fallback terminu płatności**: faktura kosztowa (TEST-FLOW/1/2026) NIE
    niesie `Fa/Platnosc/TerminPlatnosci/Termin` (na żywo dotyczyło to
    505/547 faktur) -- termin = data wystawienia (P_1 = 2026-07-28) +
    `termin_fallback_dni` (14 dni) = 2026-08-11.
    Faktura sprzedażowa ma ten sam termin PODANY WPROST -- oba dokumenty
    kończą z jednym wspólnym terminem rozrachunku mimo dwóch różnych ścieżek
    jego wyznaczenia.

## Tabela oczekiwań

| Kontrahent (faktura)                                        | Klucz w rejestrze   | Numer karty (oczekiwany)              | Konto rozrachunkowe (brutto) | Inne konta dekretu             | Termin płatności |
|---------------------------------------------------------------|----------------------|-----------------------------------------|-------------------------------|----------------------------------|--------------------|
| MERIDOL JAN KOWALSKI (TEST-FLOW/1/2026, koszt)                  | NIP 9100010014 | 5 (ISTNIEJĄCA, z kartoteki, BEZ duplikatu) | 202-2-5 (MA) | 221-1 (WN vat); netto bez konta | **fallback** (brak Platnosc w FA(3), P_1+14dni) = 2026-08-11 |
| TESTOWY ODBIORCA FLOW SP. Z O.O. (TEST-FLOW/2/2026, sprzedaż)  | NIP 9000000016 | 34 (NOWA)                  | 201-2-34 (WN) | 703 (MA) / 222-1 (MA) | jawny w FA(3) = 2026-08-11 |

Uwaga: numer karty MERIDOL (5) pochodzi z zaimportowanej kartoteki
BETALABO (33 pozycje, 1-33, z CSV). Nowa karta dostaje **max+1** -- tu
34 -- zgodnie z ustaleniem empirycznym z produkcji (FK NIE
zasypuje dziur po usuniętych kartach), które odwzorowuje rejestr numerów w
tej aplikacji (`app/kontrahenci_nr.py::RejestrNumerow.nastepny_numer`).

## Instrukcja importu w Symfonii FK

1. Otwórz Symfonię FK na profilu/firmie TESTOWEJ (NIE na produkcyjnej bazie
   BETALABO!).
2. Menu: **Import/Eksport -> Katalogi wymiany danych** (albo "Import
   specjalny", zależnie od wersji).
3. Wskaż szablon **"Dane z programu Handel, format FK 3.0"** (to jest
   standardowy szablon `hmffk.AMS`, dostępny w każdej instalacji FK -- nic nie
   trzeba dokopiowywać do folderu szablonów).
4. Jako plik danych wskaż `hmffk_betalabo-flow_2026-07-01_2026-07-31_20260824-144223.TXT`.
5. Przy PIERWSZYM imporcie tego typu dokumentów FK poprosi o **uzgodnienie
   typów dokumentów** (FVS dla sprzedaży, FVZ dla kosztu) **i rejestrów VAT**
   -- wskaż istniejące typy/rejestry w tej bazie (albo pozwól założyć nowe, wg
   polityki księgowej).
6. Zatwierdź import.

## Co sprawdzić PO imporcie

- **Karta MERIDOL** (NIP 9100010014) NIE powinna się zduplikować -- system
  powinien rozpoznać istniejącą kartę nr 5 po kluczu (id = NIP) i
  dopisać do niej fakturę, a nie założyć nowej.
- **Nowa karta** (Testowy Odbiorca Flow) powinna dostać numer **34**
  -- max+1 po 33 z kartoteki BETALABO.
- **Bufor dokumentów** (2 dokumenty w buforze, przed zatwierdzeniem/
  zaksięgowaniem): na zapisie z kontem rozrachunkowym sprawdź, że konto
  zgadza się z numerem karty kontrahenta:
    - TEST-FLOW/1/2026 (MERIDOL, koszt -- termin fallbackiem): konto
      **202-2-5** na stronie MA (brutto 123,00),
      plus **221-1** na WN VAT (23,00); WN netto (100,00) BEZ
      konta; kolejność zapisów: WN netto -> MA brutto -> WN VAT,
    - TEST-FLOW/2/2026 (Testowy Odbiorca Flow, sprzedaż -- termin jawny): konto
      **201-2-34** na stronie WN, plus
      **703** (MA netto) i **222-1**
      (MA VAT).
  Konto netto kosztu zostaje puste -- dekretuje je księgowa ręcznie w
  buforze wg zasad tej firmy; konto WN VAT (221-1) przychodzi
  już wypełnione.
- **Rozrachunki (2/2)**: na obu dokumentach powinien automatycznie powstać
  otwarty rozrachunek z terminem płatności **2026-08-11** (bez
  klikania "rozrachunków" ręcznie) -- to efekt sekcji `transakcja{}`
  towarzyszącej zapisowi brutto (patrz `reference/rozrachunki-hmffk-E2E.md`).
  Na TEST-FLOW/1/2026 ten termin powstaje mimo braku sekcji
  Platnosc/TerminPlatnosci w źródłowej FA(3) -- to efekt fallbacku
  `termin_fallback_dni` (14 dni od daty wystawienia)
  w `map_fa3_to_hmffk`.

## Jak POSPRZĄTAĆ po teście

Test jest odwracalny -- żeby nie zaśmiecać bazy testowej:

1. W buforze dokumentów usuń 2 zaimportowane dokumenty: TEST-FLOW/1/2026,
   TEST-FLOW/2/2026 (numery KSeF TEST-FLOW-0001, TEST-FLOW-0002) -- wraz z
   rozrachunkami, które do nich powstały.
2. W kartotece kontrahentów usuń TESTOWĄ kartę: TESTOWY ODBIORCA FLOW SP. Z
   O.O. (nr 34). NIE usuwaj karty MERIDOL (nr 5) -- ona
   była w kartotece PRZED testem.
3. Po usunięciu tej karty FK **NIE** odzyska numeru 34 dla
   kolejnego NOWEGO kontrahenta -- FK nie zasypuje dziur po usuniętych
   kartach, więc kolejna nowa karta i tak dostanie max(pozycja)+1 (dziura po
   34 zostanie pusta) -- ustalenie empiryczne z produkcji, które
   odwzorowuje też rejestr numerów w tej aplikacji
   (`app/kontrahenci_nr.py::RejestrNumerow.nastepny_numer`).

## Pliki tymczasowe tego testu

- `rejestr_flow.db` -- rejestr numerów kart (seed z kartoteki BETALABO CSV +
  1 nowy przydział). Możesz go bezpiecznie usunąć po teście.
- `ledger_flow.db` -- rejestr wyeksportowanych faktur (dedup). Możesz go
  bezpiecznie usunąć po teście.
- `wynik_flow/betalabo-flow/*.TXT` -- plik wynikowy.

Żaden z powyższych plików NIE jest używany przez aplikację produkcyjną --
produkcyjne bazy są w `%APPDATA%\KsefSymfoniaExport\`.
