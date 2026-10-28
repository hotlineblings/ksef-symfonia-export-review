# E2E: Automatyczne tworzenie ROZRACHUNKU (transakcji z terminem płatności) przy imporcie do Symfonia FK przez szablon hmffk.AMS

> AGENT-FACING. Handoff dla LLM wprowadzającego zmiany w aplikacji KsefSymfoniaExport.
> Wszystko poniżej zweryfikowane EMPIRYCZNIE 2026-07-14 serią importów testowych do Symfonia FK „50" 26.30
> (szablon „Dane z programu Handel, format FK 3.0" = hmffk.AMS; dokumenty lądują w buforze).
> Zakres: WYŁĄCZNIE rozrachunki (transakcja/termin płatności). Format pliku: FK 3.0, CP1250+CRLF.

## PRZEPIS (potwierdzony — dokładnie tak i tylko tak działa)

W sekcji `dokument{}` faktury sprzedaży:

```
zapis{
strona =WN
kwota =300.00
Konto =201-2-23
NumerDok =TEST/2026/07/15
IdDlaRozliczen =1
}
transakcja{
idDlaRozliczen =1
termin =2026-07-21
kwota =300.00
}
```

Elementy OBOWIĄZKOWE i ich rola:

| Element | Wymóg | Skutek braku/błędu |
|---|---|---|
| `IdDlaRozliczen =N` na zapisie rozrachunkowym | identyczna wartość jak `idDlaRozliczen` w transakcji | **błąd 611 „Nie znaleziono zapisu odpowiadającego sekcji rozrachunków"** — parowanie odbywa się WYŁĄCZNIE po tym identyfikatorze (nie po sąsiedztwie sekcji, nie po koncie) |
| `termin =RRRR-MM-DD` (ISO) | dokładnie ten format | format polski `DD.MM.RRRR` jest interpretowany jako **liczba dni od daty dokumentu** („21.07.2026" → termin = data dok. + 21 dni); liczba seryjna OLE (np. 46224) jest **odrzucana** (brak terminu) |
| `Konto =` pełne konto rozrachunkowe z analityką kontrahenta (np. `201-2-23`) | konto musi ISTNIEĆ w planie kont w chwili importu | konto nieistniejące jest **pomijane po cichu** (bez błędu); sama syntetyka (`201-2`) przechodzi do rubryki, ale rozrachunek nie wiąże podmiotu |
| `NumerDok =` numer własny dokumentu | kotwica rozrachunku (wzorzec z działającego importera DOKUM2019.AMX) | — |
| `kwota =` w transakcji | kwota terminu (brutto pozycji rozrachunkowej) | — |

Strona zapisu rozrachunkowego: sprzedaż → WN brutto; zakup → analogicznie MA brutto (rozrachunki z dostawcami).
`idDlaRozliczen` wystarczy unikalny w obrębie dokumentu (u nas zawsze `1` — jeden rozrachunek na fakturę).

## DLACZEGO tak (mechanika szablonu hmffk.AMS)

1. Obsługa podsekcji `transakcja` w szablonie:
   ```
   case "transakcja"
       MakeSec("transakcja") {
           $ustawowe = 1        // na sztywno
           $zaliczka = 0        // na sztywno
           $idDlaRozliczen = [idDlaRozliczen]
           $termin  = [termin]   // UWAGA: BEZ konwersji Date()!
           $kwota   = [kwota]
           $znacznik/$transakcja/$rachunek = [...]  // opcjonalne
       }
   ```
   `$termin` NIE przechodzi przez `Date()` (wszystkie inne daty w szablonie przechodzą). Pole po stronie FK
   samo interpretuje: tekst ISO = data; liczba/tekst zaczynający się liczbą = liczba dni od daty dokumentu.
2. Dokumentowe pole `terminPlatnosci` zasila tylko zmienną `@plattermin`, używaną WYŁĄCZNIE przez sekcje
   `rozliczenie{}` (rozliczanie istniejących rozrachunków, np. wyciągi bankowe). NIE tworzy transakcji.
   Sekcja `rozliczenie` bez pasującego zapisu również kończy się błędem 611 — nie używać jej dla świeżych faktur.
3. Zapis rozrachunkowy: szablon przepisuje `$Konto=[Konto]`, `$NumerDok=[NumerDok]`, `$IdDlaRozliczen=[IdDlaRozliczen]`.

## DOWODY (matryca testów z 2026-07-14)

- transakcja z `idDlaRozliczen` bez `IdDlaRozliczen` na zapisie (wzorzec sąsiedztwa z DOKUM2019.AMX) → **błąd 611**.
- para id + trzy formaty terminu w identycznych dokumentach:
  - `21.07.2026` → FK pokazał termin **2026-08-04** (= data dok. 14.07 + 21 dni → zinterpretowane jako liczba dni),
  - `2026-07-21` → FK pokazał **2026-07-21** ✓,
  - `46224` (seryjna OLE dla 21.07.2026) → **brak terminu**.
- konto `201-2` (sama syntetyka) + id + transakcja → alert „nie wypełniono rozrachunków" przy księgowaniu;
  konto z pełną analityką `201-2-23` → transakcja z terminem utworzona automatycznie.

## ZNANE PRZYPADŁOŚCI (nie blokują, ale trzeba o nich wiedzieć)

1. **„Patent z cyfrą":** zaimportowane konto bywa widoczne w rubryce zapisu, ale FK nie ma wewnętrznego
   powiązania — interaktywne otwarcie rozrachunków żąda „dodania konta", dopóki nie usunie się i nie dopisze
   ostatniej cyfry. Podejrzana przyczyna: szablon przypisuje `$Konto` (wielkie K), działający importer
   DOKUM2019.AMX używa `$konto` (małe k). Z poziomu pliku danych NIEOBCHODZALNE (nazwa po stronie szablonu).
   Przy automatycznie utworzonej transakcji problem w praktyce nie blokuje (nie trzeba klikać rozrachunków).
2. **Numer analityki kontrahenta nadaje FK** sekwencyjnie przy tworzeniu karty (test: wysłano konto
   `201-2-50` dla nowego kontrahenta → FK nadał mu numer 24; konto 50 zignorowane). Nie da się narzucić
   numeru z pliku. Strategia „przewidywanego numeru" (nowa karta + konto `201-2-<max+1>` w tym samym pliku)
   była w trakcie testu w chwili spisania tego dokumentu — status niepotwierdzony.
3. Konta MA (przychody, VAT) podlegają tej samej zasadzie: **nieistniejące w planie kont są pomijane po cichu**.

## STAN WDROŻENIA W APLIKACJI (app/hmffk.py)

- `HDokument` ma już pole `konto_rozrachunkowe: str = ""` (dodane, jeszcze niewykorzystywane w writerze).
- `HDokument.termin_platnosci` niesie termin w ISO (z FA(3) `Fa/Platnosc/TerminPlatnosci/Termin`) — format już właściwy.
- `_linie_zapisow()` generuje obecnie zapisy WN/MA BEZ: Konto, NumerDok, IdDlaRozliczen i BEZ sekcji transakcja.

DO ZROBIENIA w `_linie_zapisow()` (lub odpowiedniku):
1. Na zapisie rozrachunkowym (sprzedaż: WN brutto; zakup: MA brutto) emitować:
   `Konto =<konto_rozrachunkowe>` (tylko gdy niepuste), `NumerDok =<numer faktury>`, `IdDlaRozliczen =1`.
2. Bezpośrednio po tym zapisie, gdy `termin_platnosci` niepusty, emitować:
   `transakcja{ idDlaRozliczen =1, termin =<ISO>, kwota =<brutto> }`.
3. Źródło `konto_rozrachunkowe` — do decyzji użytkownika (mapa NIP→numer / przewidywanie sekwencyjne);
   bez wartości zapis idzie bez konta (księgowa uzupełnia przy dekretacji), transakcja i tak wymaga zapisu z id.
