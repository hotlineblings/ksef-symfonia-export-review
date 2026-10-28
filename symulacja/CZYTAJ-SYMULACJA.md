# Plik symulacji hmffk -- test importu do Symfonii FK

Ten katalog zawiera WYŁĄCZNIE dane testowe wygenerowane skryptem
`generuj_symulacje.py`, do jednorazowego, RĘCZNEGO wypróbowania importu w
Symfonii FK na kopii/testowej bazie firmy. Nic tu nie dotyka bazy
produkcyjnej (`%APPDATA%\KsefSymfoniaExport`) -- rejestr numerów i ledger są
osobnymi, tymczasowymi plikami `.db` w tym katalogu.

Kartoteka źródłowa (`C:\Users\Anna\Downloads\AGTLABO - kontrahenci.xlsx`) jest wczytywana w POSTACI SUROWEJ,
bez żadnej normalizacji -- to jednocześnie test regresyjny naprawy
`app/kontrahenci_nr.py::_wczytaj_xlsx`/`_digits` (kolumna NIP zapisana w
Excelu jako liczba, np. `9100010014.0` dla MERIDOL, musi dać klucz
`9100010014`, a nie zepsuty 11-cyfrowy `91000100140`).

## Co zawiera plik wynikowy

Plik: `symulacja\wynik\agtlabo-symulacja\hmffk_agtlabo-symulacja_2026-07-01_2026-07-31_20260731-121109.TXT`

Format: AMS "Dane z programu Handel, format FK 3.0" (szablon `hmffk.AMS`),
CP1250 + CRLF. Zawiera:

- 4 sekcje `Kontrahent{}` -- karty kontrahentów (1 istniejący + 3 nowi),
- 4 sekcje `dokument{}` -- 2 faktury kosztowe (FVZ) + 2 sprzedażowe (FVS),
  każda z numerem KSeF, rejestrem VAT (23%, netto 100,00 / VAT 23,00 /
  brutto 123,00) i PEŁNYM dekretem rozrachunkowym (sekcje `zapis{}` +
  `transakcja{}`): linia brutto kontrahenta dostaje konto rozrachunkowe,
  `NumerDok`, `IdDlaRozliczen` i sekcję `transakcja{}` z terminem płatności
  (2026-08-08) -- FK zakłada rozrachunek automatycznie, bez ręcznego
  wypełniania.
  - **Dekret sprzedaży** (WN brutto / MA netto / MA VAT): konto rozrachunkowe
    na WN brutto, dodatkowo `703` na MA netto i
    `222-1` na MA VAT.
  - **Dekret kosztowy** (WN netto / MA brutto / WN VAT -- kolejność jak w
    oryginalnym dekrecie zakupu Symfonii): WN netto zostaje BEZ konta
    (dekretuje księgowa), MA brutto dostaje konto rozrachunkowe (+
    rozrachunek), WN VAT dostaje `221-1` (VAT naliczony).
  - **Fallback terminu płatności**: gdy FA(3) nie niesie
    `Fa/Platnosc/TerminPlatnosci/Termin` (na żywo dotyczyło to 505/547
    faktur), termin = data wystawienia (P_1) + `termin_fallback_dni`
    (14 dni) -- dzięki temu rozrachunek i tak powstaje.
    W tej symulacji ćwiczy to TEST/2/2026 (TEST-SYM-0002): FA(3) BEZ sekcji
    Platnosc, termin liczony fallbackiem wychodzi tak samo jak jawny termin
    pozostałych trzech faktur (2026-08-08).

## Tabela: kontrahent -> oczekiwany numer karty -> oczekiwane konta

| Kontrahent (faktura)                              | Klucz w rejestrze      | Numer karty (oczekiwany) | Konto rozrachunkowe (brutto) | Inne konta dekretu | Termin płatności |
|----------------------------------------------------|-------------------------|--------------------------|-------------------------------|---------------------|--------------------|
| MERIDOL JAN KOWALSKI (TEST/1/2026, koszt)            | NIP 9100010014     | 5 (ISTNIEJĄCA, z kartoteki) | 202-2-5 (MA) | 221-1 (WN vat); netto bez konta | jawny w FA(3) |
| TESTOWY DOSTAWCA SYMULACJA SP. Z O.O. (TEST/2/2026, koszt) | NIP 9000000016 | 28 (NOWA)    | 202-2-28 (MA) | 221-1 (WN vat); netto bez konta | **fallback** (brak Platnosc w FA(3), P_1+14dni) |
| TESTOWY ODBIORCA SYMULACJA SP. Z O.O. (TEST/3/2026, sprzedaż) | NIP 9000000022 | 29 (NOWA) | 201-2-29 (WN) | 703 (MA) / 222-1 (MA) | jawny w FA(3) |
| Jan Testowy (TEST/4/2026, sprzedaż, bez NIP)        | K_... (hash nazwa+miasto) | 30 (NOWA)       | 201-2-30 (WN) | 703 (MA) / 222-1 (MA) | jawny w FA(3) |

Uwaga: numer karty MERIDOL (5) pochodzi z zaimportowanej kartoteki AGTLABO
(27 pozycji, 1-27). Trzy nowe karty dostają PIERWSZE WOLNE numery -- tu 28,
29, 30 -- w kolejności sekcji `Kontrahent{}` w pliku (klucze posortowane:
najpierw NIP-y jako stringi, potem `K_...`). Prefiks konta rozrachunkowego
zależy od rodzaju dokumentu: `201-2-<pozycja>` dla
sprzedaży (strona WN), `202-2-<pozycja>` dla kosztu
(strona MA). Konto WN VAT kosztu (`221-1`) i konto WN netto
kosztu (puste -- `konto_koszt_netto=""`) są takie same dla wszystkich
dokumentów kosztowych (nie zależą od pozycji karty).

## Instrukcja importu w Symfonii FK

1. Otwórz Symfonię FK na profilu/firmie TESTOWEJ (NIE na produkcyjnej bazie
   AGTLABO!).
2. Menu: **Import/Eksport -> Katalogi wymiany danych** (albo "Import
   specjalny", zależnie od wersji).
3. Wskaż szablon **"Dane z programu Handel, format FK 3.0"** (to jest
   standardowy szablon `hmffk.AMS`, dostępny w każdej instalacji FK -- nic nie
   trzeba dokopiowywać do folderu szablonów).
4. Jako plik danych wskaż `hmffk_agtlabo-symulacja_2026-07-01_2026-07-31_20260731-121109.TXT`.
5. Przy PIERWSZYM imporcie tego typu dokumentów FK poprosi o **uzgodnienie
   typów dokumentów** (FVS dla sprzedaży, FVZ dla kosztu) **i rejestrów VAT**
   -- wskaż istniejące typy/rejestry w tej bazie (albo pozwól założyć nowe, wg
   polityki księgowej).
6. Zatwierdź import.

## Co sprawdzić PO imporcie

- **Kartoteka kontrahentów**: nowe karty powinny dostać numery **28, 29,
  30** (dokładnie w tej kolejności: TESTOWY DOSTAWCA=28, TESTOWY ODBIORCA=29,
  Jan Testowy=30) -- to pierwsze wolne numery po 27 z kartoteki AGTLABO.
- **Karta MERIDOL** (NIP 9100010014) NIE powinna się zduplikować --
  system powinien rozpoznać istniejącą kartę nr 5 po kluczu (id
  = NIP) i dopisać do niej fakturę, a nie założyć nowej.
- **Bufor dokumentów** (4 dokumenty w buforze, przed zatwierdzeniem/
  zaksięgowaniem): na zapisie z kontem rozrachunkowym sprawdź, że konto
  zgadza się z numerem karty kontrahenta, i że kolejność zapisów kosztu to
  WN netto (bez konta) -> MA brutto (konto rozrachunkowe) -> WN VAT (konto
  221-1):
    - TEST/1/2026 (MERIDOL, koszt): konto **202-2-5**
      na stronie MA (brutto 123,00), plus **221-1** na WN VAT
      (23,00); WN netto (100,00) BEZ konta,
    - TEST/2/2026 (Dostawca, koszt -- termin fallbackiem, patrz niżej): konto
      **202-2-28** na stronie MA, plus
      **221-1** na WN VAT; WN netto BEZ konta,
    - TEST/3/2026 (Odbiorca, sprzedaż): konto
      **201-2-29** na stronie WN, plus
      **703** (MA netto) i **222-1**
      (MA VAT),
    - TEST/4/2026 (Jan Testowy, sprzedaż): konto
      **201-2-30** na stronie WN, plus
      **703** / **222-1** jak wyżej.
  Konto netto kosztu zostaje puste -- dekretuje je księgowa ręcznie w
  buforze wg zasad tej firmy; konto WN VAT (221-1) przychodzi
  już wypełnione.
- **Rozrachunki**: na każdym z 4 dokumentów powinien automatycznie powstać
  otwarty rozrachunek z terminem płatności **2026-08-08** (bez
  klikania "rozrachunków" ręcznie) -- to efekt sekcji `transakcja{}`
  towarzyszącej zapisowi brutto (patrz `reference/rozrachunki-hmffk-E2E.md`).
  Na TEST/2/2026 (TEST-SYM-0002) ten sam termin powstaje mimo braku sekcji
  Platnosc/TerminPlatnosci w źródłowej FA(3) -- to efekt fallbacku
  `termin_fallback_dni` (14 dni od daty wystawienia)
  w `map_fa3_to_hmffk`.

## Jak POSPRZĄTAĆ po teście

Test jest odwracalny -- żeby nie zaśmiecać bazy testowej:

1. W buforze dokumentów usuń 4 zaimportowane dokumenty: TEST/1/2026,
   TEST/2/2026, TEST/3/2026, TEST/4/2026 (numery KSeF TEST-SYM-0001 ..
   TEST-SYM-0004) -- wraz z rozrachunkami, które do nich powstały.
2. W kartotece kontrahentów usuń 3 TESTOWE karty: TESTOWY DOSTAWCA
   SYMULACJA SP. Z O.O. (nr 28), TESTOWY ODBIORCA SYMULACJA
   SP. Z O.O. (nr 29), Jan Testowy (nr 30). NIE usuwaj
   karty MERIDOL (nr 5) -- ona była w kartotece PRZED testem.
3. Po usunięciu tych 3 kart FK odzyska numery 28-30 dla kolejnych NOWYCH
   kontrahentów -- to zgodne z zasadą "pierwszy wolny numer" (FK zasypuje
   dziury po usuniętych kartach), którą odwzorowuje też rejestr numerów w tej
   aplikacji (`app/kontrahenci_nr.py::RejestrNumerow.pierwszy_wolny`).

## Pliki tymczasowe tego testu

- `rejestr_symulacja.db` -- rejestr numerów kart (seed z kartoteki AGTLABO +
  3 nowe przydziały). Możesz go bezpiecznie usunąć po teście.
- `ledger_symulacja.db` -- rejestr wyeksportowanych faktur (dedup). Możesz go
  bezpiecznie usunąć po teście.
- `wynik/agtlabo-symulacja/*.TXT` -- plik(i) wynikowe.

Żaden z powyższych plików NIE jest używany przez aplikację produkcyjną --
produkcyjne bazy są w `%APPDATA%\KsefSymfoniaExport\`.
