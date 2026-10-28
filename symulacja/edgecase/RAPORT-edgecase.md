# Przegląd edge-case: klucz/karta kontrahenta w eksporcie hmffk

> Zakres: WYŁĄCZNIE diagnostyka. Nic w `app/` ani `tests/` nie zostało zmienione.
> Metoda: skrypty w `symulacja/edgecase/*.py`, uruchomione realnym API aplikacji
> (`app.hmffk.map_fa3_to_hmffk`, `app.kontrahenci_nr.RejestrNumerow` na bazie
> tymczasowej, `app.exporter.run_export` ze stubem klienta KSeF identycznym jak
> w `tests/test_exporter.py::StubClient`). Wspólne pomoce: `wspolne.py`
> (generatory syntetycznych FA(3) + stub). Uruchomienie:
> `.venv/Scripts/python.exe symulacja/edgecase/scenariuszN_*.py`
> (środowisko: `PYTHONIOENCODING=utf-8`, żeby polskie znaki w konsoli się nie gubiły —
> sama treść pliku wynikowego CP1250 jest poprawna niezależnie od tego).
> Baseline repo w chwili przeglądu: `pytest -q` → 138 passed (żaden test nie ruszony).

Mechanizm klucza kontrahenta bez NIP (identyczny w `app/hmffk.py::_kontrahent_z_podmiotu`
i `app/kontrahenci_nr.py::_klucz_seed`):

```python
digest = hashlib.sha1(f"{nazwa}|{miejscowosc_lub_adresL2}".encode("utf-8"))
klucz = "K_" + digest.hexdigest()[:12].upper()
```

`nazwa` to **surowy string** z FA(3) (bez casefold/strip/normalizacji — normalizacja
`_norm`/`_rdzen` istnieje TYLKO w `RejestrNumerow.podobni()`, do wyszukiwania fuzzy,
nigdy do liczenia klucza). To założenie wyjaśnia wszystkie pięć scenariuszy poniżej.

---

## Scenariusz 1 — dwaj różni kontrahenci bez NIP, podobne nazwy (literówka)

**Dane testowe:** „Kowalski Transport Drogowy” vs „Kowalksi Transport Drogowy”
(zamienione dwie litery), ten sam adres.

**Co się dzieje dziś (dowód z `scenariusz1_podobienstwo_bez_nip.py`):**

- Klucze: `K_080865858328` vs `K_CB758E899EB4` — **różne**, więc dostają
  **osobne karty i osobne pozycje w rejestrze** (1 i 2). To jest poprawne —
  to naprawdę dwaj różni kontrahenci.
- `RejestrNumerow.podobni()` liczy `SequenceMatcher` na **rdzeniu** nazwy
  (bez formy prawnej/generyków) — dla tej pary rdzeń→rdzeń ratio = **0,9615**
  (próg domyślny 0,87), więc przy eksporcie kontrahenta B raport dostaje
  ostrzeżenie:
  > „Nowy kontrahent 'Kowalksi Transport Drogowy' podobny (96%) do istniejącego
  > nr 1 'Kowalski Transport Drogowy' — jeśli to ten sam, scal w rejestrze
  > kontrahentów (GUI) i popraw dekret w FK”
- Ostrzeżenie **nie blokuje** eksportu — obie karty i tak powstają
  (`exporter.py`: `nr = rejestr.przydziel(...)` wykonuje się niezależnie od
  ostrzeżenia).
- Kontrola progu: para o mniejszym podobieństwie („Nowaczyk Usługi Budowlane”
  vs „Nowak Handel Materiałami”, ratio 0,41) **nie dostaje ostrzeżenia** mimo,
  że to też mogłyby być łatwe do pomylenia nazwy — próg 0,87 jest svelte,
  ale to oczekiwane (dwie faktycznie różne firmy o odległych nazwach nie
  powinny fałszywie trafiać w podobieństwo).

**Ocena ryzyka księgowego:** NISKIE. Mechanizm działa zgodnie z projektem —
różne nazwy = różne karty, a fuzzy-match ostrzega księgową, że dwie NOWE
pozycje w rejestrze wyglądają podejrzanie podobnie, więc może to być literówka
sprzedawcy albo faktycznie inny podmiot. Decyzję (scalić czy zostawić osobno)
podejmuje człowiek — słusznie, bo aplikacja nie ma jak wiedzieć, czy to
literówka czy naprawdę dwie firmy.

**Rekomendacja (bez implementacji):** Zachować jak jest. Ewentualnie:
w GUI (`RejestrDialog`) rozważyć wizualne wyróżnienie ostrzeżeń o podobieństwie
w osobnej sekcji „do przejrzenia”, żeby nie ginęły wśród innych ostrzeżeń
eksportu przy dużym wolumenie faktur (na żywie testowano 547 faktur — przy
takiej skali kilkanaście ostrzeżeń podobieństwa łatwo przeoczyć w jednym
ciągłym logu).

---

## Scenariusz 2 — dwie różne osoby fizyczne, identyczne imię i nazwisko, bez NIP i bez adresu

**Dane testowe:** dwie NIEPOWIĄZANE osoby, obie „Jan Kowalski”, żadna faktura
nie niesie adresu ani NIP (typowe dla części faktur od osób fizycznych
nieprowadzących działalności).

**Co się dzieje dziś (dowód z `scenariusz2_dwie_osoby_identyczne.py`):**

- Klucz osoby 1 = `K_B9B8C9E731E3`, klucz osoby 2 = `K_B9B8C9E731E3` —
  **IDENTYCZNY** (bo hash liczony z `"Jan Kowalski|"` w obu przypadkach —
  pusty adres daje pusty drugi człon hasha niezależnie od tego, kim naprawdę
  jest dana osoba).
- `run_export` z obiema fakturami w jednym przebiegu: `report.kontrahenci == 1`,
  plik wynikowy ma **1 sekcję Kontrahent{}** i **2 sekcje dokument{}**, obie
  wskazujące ten sam `khid` i to samo konto rozrachunkowe (`202-2-1`).
- Brak jakiegokolwiek ostrzeżenia — aplikacja nie ma sygnału, że to mogą być
  dwie różne osoby (nie ma czego porównać: fuzzy-match na identycznych
  nazwach zawsze daje 100%, a to normalna, bezpieczna ścieżka dla PRAWDZIWEGO
  duplikatu tej samej osoby — nie da się tego odróżnić od kolizji dwóch osób
  o tym samym imieniu i nazwisku).

**Ocena ryzyka księgowego:** WYSOKIE, ale wąskie zastosowanie. To jedyny
scenariusz z całego przeglądu, w którym dwa NAPRAWDĘ różne podmioty
**bezobjawowo** (bez żadnego ostrzeżenia) współdzielą jedną kartę i jedno
konto rozrachunkowe:
- Saldo rozrachunkowe konta to suma zobowiązań/należności wobec dwóch
  różnych ludzi — księgowa widzi jedną pozycję zamiast dwóch.
- Rozliczenie wyciągu bankowego (`rozliczenie{}`, poza zakresem hmffk, ale
  używa tego samego `Konto`/rozrachunku) grozi rozliczeniem płatności
  należnej osobie 1 z rozrachunkiem osoby 2 (ten sam klucz, to samo konto).
- `KontrahenciRejestr.add()` scala pola karty „pierwszy wygrywa, puste pola
  uzupełnia kolejny rekord” — telefon/e-mail/rachunek bankowy jednej osoby
  mogą się doczepić do karty opisanej danymi drugiej, jeśli tylko któraś
  faktura akurat coś z tych pól niosła.

Realna częstość: w teście na żywo (547 faktur) problem NIE wystąpił jawnie,
ale przy większej skali (setki/tysiące kontrahentów bez NIP, w tym osoby
fizyczne) prawdopodobieństwo kolizji imienia+nazwiska bez adresu rośnie —
w Polsce popularne kombinacje imię+nazwisko (np. „Jan Kowalski”, „Anna
Nowak”) nie są rzadkością.

**Rekomendacja (bez implementacji):**
1. Rozważyć ostrzeżenie generowane, gdy dwa RÓŻNE dokumenty w TYM SAMYM
   eksporcie mapują się na klucz kontrahenta osoby fizycznej (`osoba_fizyczna=True`)
   **bez żadnego adresu ani kontaktu** (pusta ulica, miejscowość, e-mail,
   telefon) — to sygnał "brak jakiejkolwiek podstawy do odróżnienia", inny
   niż zwykły duplikat tej samej osoby.
2. Dokumentacyjnie: uczulić księgową (w README/instrukcji obsługi), że
   karty kontrahentów-osób-fizycznych bez adresu wymagają szczególnej
   czujności przy więcej niż jednej fakturze na tego samego „Jana Kowalskiego”.
3. Nie da się tego naprawić wyłącznie w kodzie bez dodatkowych danych z FA(3)
   (np. jeśli sprzedawca/nabywca kiedykolwiek poda choćby częściowy adres,
   scenariusz 3 pokazuje, że dostają WTEDY inny klucz — czyli paradoksalnie
   PEŁNY brak danych jest z punktu widzenia kolizji klucza gorszy niż
   częściowe dane).

---

## Scenariusz 3 — ta sama osoba raz z adresem, raz bez — duplikat i scalanie aliasem

**Dane testowe:** „Anna Nowak” (bez NIP), faktura 1 (styczeń) BEZ adresu,
faktura 2 (luty) Z adresem (ul. Lipowa 8, 30-001 Kraków) — realistyczny
przypadek, gdy sprzedawca czasem uzupełnia adres nabywcy, a czasem nie.

**Co się dzieje dziś (dowód z `scenariusz3_ten_sam_raz_z_adresem.py`):**

1. Klucze różne: `K_696E29E29845` (bez adresu) vs `K_E870EF937CFF` (z adresem)
   → **DUPLIKAT** — dwie karty tej samej realnej osoby, dwie osobne pozycje
   w rejestrze (1 i 2).
2. **Fuzzy TO WYŁAPUJE**: przy eksporcie faktury z adresem (luty), nazwa
   „Anna Nowak” jest identyczna z już zarejestrowaną → `podobni()` zwraca
   ratio **100%** (dopasowanie po pełnej nazwie, ścieżka `norm_a == norm_b`
   w `podobni()`, zanim w ogóle dojdzie do rdzenia) → ostrzeżenie:
   > „Nowy kontrahent 'Anna Nowak' podobny (100%) do istniejącego nr 1
   > 'Anna Nowak' — jeśli to ten sam, scal w rejestrze kontrahentów (GUI)
   > i popraw dekret w FK”
3. Scalenie: `rejestr.dodaj_alias(klucz_z_adresem, klucz_bez_adresu, zwolnij_numer=True)`
   — po tym `kanoniczny(klucz_z_adresem)` zwraca `klucz_bez_adresu`, a pozycja
   duplikatu (2) wraca do puli wolnych numerów.
4. **Kolejny eksport (marzec)**, nowa faktura z tym samym „adresowym” kluczem
   → `exporter.py` rozwiązuje klucz przez `rejestr.kanoniczny()` PRZED
   przydziałem numeru → dokument w pliku dostaje `khid = K_696E29E29845`
   (kanoniczny) i `Konto = 202-2-1` (pozycja kanoniczna) — **potwierdzone**:
   scalenie aliasem naprawia konto automatycznie w każdym kolejnym eksporcie,
   bez ponownej interwencji.

**Ocena ryzyka księgowego:** ŚREDNIE, samoograniczające się dzięki istniejącemu
mechanizmowi. Duplikat rzeczywiście powstaje (dwa numery kart w FK dla jednej
osoby, dopóki księgowa nie scali), ale:
- ostrzeżenie fuzzy pojawia się przy 100% zgodności nazwy — praktycznie
  niemożliwe do przeoczenia (opis wprost podaje nazwę i numer istniejącej karty),
- scalenie jest jednorazowe i trwałe (persystentny rejestr SQLite) —
  naprawia WSZYSTKIE przyszłe eksporty tego kontrahenta, nie tylko bieżący.

Praktyczne ryzyko: między wykryciem duplikatu (eksport #2) a scaleniem przez
księgową może upłynąć czas, w którym oba dokumenty już wylądowały w buforze
FK na DWÓCH kartach — scalenie aliasem w rejestrze NIE cofa retroaktywnie
tego, co już zaimportowano do Symfonii (to osobna operacja po stronie FK).

**Rekomendacja (bez implementacji):**
1. Utrzymać obecny mechanizm (fuzzy + alias) — działa zgodnie z projektem.
2. Rozważyć w GUI (`RejestrDialog`) licznik/wskaźnik „ile dokumentów już
   wyeksportowano pod danym duplikatem” — dziś księgowa widzi tylko
   ostrzeżenie tekstowe w raporcie eksportu, nie ma podglądu historii, ile
   razy dany duplikat już był użyty zanim go scalono (co pomogłoby ocenić
   pilność ręcznej korekty w samym FK).

---

## Scenariusz 4 — kontrahent zagraniczny bez NIP, adres w jednej linii — stabilność klucza

**Dane testowe:** „Global Freight Solutions Ltd”, kraj US, cały adres w
`AdresL1` jedną linią, `AdresL2` puste (typowe dla adresów spoza polskiego
formatu, gdzie FA(3) nie ma jak rozdzielić pól).

**Co się dzieje dziś (dowód z `scenariusz4_zagraniczny_bez_nip_jedna_linia.py`):**

1. `parse_adres(adres_jednoliniowy, "")` → `miejscowosc=''` (regex kodu
   pocztowego `_KOD_POCZTOWY` szuka wyłącznie polskiego formatu `XX-XXX`,
   nie znajduje go w adresie zagranicznym) → drugi człon hasha to
   `s.miejscowosc or adres_l2` = `'' or '' = ''`.
2. Klucz zależy więc **wyłącznie od surowego pola `nazwa`** — potwierdzone:
   dwa niezależne eksporty (styczeń, luty) tej samej firmy z identycznym
   `AdresL1` dają **identyczny klucz** i **tę samą pozycję karty (1)** w obu
   przebiegach — stabilność MIĘDZY eksportami jest zachowana, tak długo jak
   `nazwa` się nie zmienia.
3. Test kontrolny — 4 warianty ZAPISU TEGO SAMEGO ADRESU (bez kodu
   pocztowego, wielkimi literami, ze spacjami na brzegach) → **wszystkie dają
   ten sam klucz** (adres faktycznie nie ma żadnego wpływu, gdy `miejscowosc`
   wychodzi puste).
4. Test kontrolny — 4 warianty ZAPISU TEJ SAMEJ NAZWY („Ltd” / „Ltd.” / „LTD”
   / podwójna spacja) → **4 RÓŻNE klucze**, czyli 4 różne karty dla jednej,
   tej samej firmy.

**Ocena ryzyka księgowego:** ŚREDNIE. Sam scenariusz z treścią zadania
(„adres w jednej linii”) jest w praktyce BEZPIECZNIEJSZY niż mogłoby się
wydawać — adres nie wprowadza niestabilności, bo i tak nie wchodzi do klucza.
Prawdziwe ryzyko leży gdzie indziej: klucz kontrahenta zagranicznego bez NIP
zależy w 100% od tego, czy pole `nazwa` w KAŻDEJ kolejnej fakturze od tego
samego kontrahenta jest bajt-w-bajt identyczne. W praktyce systemy
fakturujące za granicą:
- czasem dopisują sufiks formy prawnej niekonsekwentnie („Ltd” vs „Ltd.”),
- czasem zmieniają wielkość liter,
- czasem różne oddziały/systemy tej samej firmy generują fakturę z lekko
  inaczej sformatowaną nazwą.

Każda taka rozbieżność tworzy nową kartę i nowe konto rozrachunkowe — analogiczny
mechanizm do scenariusza 3, ale bez łatwego sygnału ostrzegawczego, bo
`podobni()` DZIAŁA dla takich drobnych różnic (SequenceMatcher na rdzeniu
złapie „Ltd” vs „Ltd.” z bardzo wysokim ratio) — więc fuzzy i tak by to
wychwyciło, ale wymaga to, by księgowa zauważyła ostrzeżenie i ręcznie scaliła
(mechanizm identyczny jak w scenariuszu 3).

**Rekomendacja (bez implementacji):**
1. Traktować to jako podkategorię scenariusza 3 (duplikat wykrywalny fuzzy +
   scalany aliasem) — mechanizm naprawczy już istnieje i działa.
2. Dla kontrahentów zagranicznych bez identyfikatora podatkowego rozważyć
   NIŻSZY próg fuzzy (`prog` w `podobni()`) albo dodatkowe kryterium
   (np. sam kraj + pierwsze N znaków adresu) — dziś próg 0,87 jest wspólny
   dla wszystkich kontrahentów bez NIP, ale kontrahenci zagraniczni mają
   strukturalnie mniej sygnału do odróżnienia (miejscowość zawsze pusta) niż
   krajowi, więc mogliby zasługiwać na czulszą heurystykę.
3. Nie normalizować samego KLUCZA (np. casefold nazwy) bez przemyślenia —
   zmieniłoby to klucze dla WSZYSTKICH już wyeksportowanych kontrahentów bez
   NIP w rejestrze (naruszałoby stabilność, którą rejestr ma gwarantować) —
   to świadoma decyzja projektowa do rozważenia OSOBNO, nie punktowa poprawka.

---

## Scenariusz 5 — faktura uproszczona zupełnie bez nazwy nabywcy

**Dane testowe:** sprzedaż (Subject1), `Podmiot2/DaneIdentyfikacyjne` całkiem
puste — brak `Nazwa`, brak `NIP`, brak `NrVatUE`/`NrID` (typowy przypadek
faktury do paragonu / faktury uproszczonej bez oznaczenia nabywcy).

**Co się dzieje dziś (dowód z `scenariusz5_uproszczona_bez_nazwy.py`):**

1. `_kontrahent_z_podmiotu`: `nazwa=''`, `nip=''`, `alt_id=''`, `kraj='PL'`
   (domyślne, bo brak `Adres/KodKraju`) → `osoba = not nip and not alt_id and
   kraj == "PL"` = **True** — pusty nabywca dostaje flagę `osoba_fizyczna=1`
   mimo braku jakichkolwiek danych osobowych (`imie`/`nazwisko` też puste,
   bo `"".split()` nie daje 2 tokenów).
2. Ostrzeżenie z mapowania: „Nabywca bez nazwy (faktura uproszczona?) —
   uzupełnij kartę w FK” — pojawia się przy KAŻDEJ takiej fakturze.
3. Klucz = `K_3EB416223E9E` — **STAŁY** (hash pustego stringu + pusty separator),
   identyczny dla dowolnej liczby takich faktur, z dowolnego eksportu i okresu.
4. Dwie niezależne faktury uproszczone bez nabywcy w jednym eksporcie →
   `report.kontrahenci == 1`, jedna sekcja `Kontrahent{}` z **pustą linią
   `nazwa =`** (bez treści) i flagą `osfiz =1`, obie faktury wskazują tę samą
   kartę i to samo konto rozrachunkowe.

**Ocena ryzyka księgowego:** NISKIE–ŚREDNIE, zależnie od praktyki firmy.
- Jeśli faktury uproszczone/paragonowe traktowane są jako sprzedaż zbiorcza
  bez śledzenia rozrachunku z konkretnym nabywcą (typowe dla detalu) —
  wspólna karta „anonimowy nabywca” jest księgowo akceptowalna i wręcz
  wygodna (jedno konto zbiorcze zamiast setek kart bez treści).
- Ryzyko pojawia się, gdyby TAKIE faktury miały mieć realny, indywidualny
  termin płatności/rozrachunek (sekcja `transakcja{}` z `termin_platnosci`) —
  wtedy WSZYSTKIE takie transakcje współdzielą jeden `idDlaRozliczen`/konto
  rozrachunkowe, więc rozliczenie wpłaty jednego anonimowego nabywcy mogłoby
  się pomieszać z rozliczeniem innego (analogiczny mechanizm ryzyka jak
  w scenariuszu 2, ale tu dotyczy to CELOWO anonimowych transakcji, więc
  praktyczne konsekwencje są łagodniejsze — nikt i tak nie śledzi rozrachunku
  z anonimowym nabywcą po imieniu).
- Karta z pustą `nazwa` w FK może wyglądać na błąd importu dla osoby, która
  nie zna tego mechanizmu (pusty kod/skrót = klucz `K_...` zamiast czytelnej
  nazwy) — ryzyko operacyjne/UX, nie księgowe wprost.

**Rekomendacja (bez implementacji):**
1. Rozważyć dla kart z pustą `nazwa` wstawienie czytelnego zastępczego opisu
   (np. „Nabywca nieoznaczony (faktura uproszczona)”) zamiast pustej linii
   `nazwa =` — czysto kosmetyczna zmiana ułatwiająca odczyt karty w FK,
   nie wpływa na klucz/logikę rozrachunku.
2. Utrzymać wspólną kartę dla wszystkich takich faktur (zgodnie z praktyką
   księgową dla sprzedaży detalicznej) — nie próbować sztucznie różnicować
   kluczem numeru faktury/daty, bo to zaprzeczałoby idei „ten sam nieznany
   nabywca = jedno miejsce w rozrachunkach”.
3. Ostrzeżenie „Nabywca bez nazwy” przy dużym wolumenie takich faktur (setki
   paragonów) może zalać raport eksportu powtarzalnym komunikatem — rozważyć
   w warstwie raportowania (nie w `map_fa3_to_hmffk`) zliczanie/deduplikację
   tego konkretnego ostrzeżenia do jednej zbiorczej linii typu „N faktur bez
   nazwy nabywcy — wspólna karta K_xxxxxxxxxxxx”.

---

## Podsumowanie zbiorcze

| # | Scenariusz | Klucz/karta dziś | Wykryte przez fuzzy? | Ryzyko | Naprawialne dziś istniejącym mechanizmem? |
|---|---|---|---|---|---|
| 1 | Różni kontrahenci, podobne nazwy (literówka), bez NIP | osobne klucze, osobne karty | TAK (96%) | niskie | tak — samo działanie zgodne z projektem |
| 2 | Dwie różne osoby, identyczne imię+nazwisko, bez NIP/adresu | JEDEN klucz, JEDNA karta | NIE (brak sygnału do odróżnienia) | **wysokie** (ciche sklejenie) | nie wprost — brak danych do rozróżnienia |
| 3 | Ta sama osoba, raz z adresem raz bez | dwa klucze, duplikat | TAK (100%) | średnie (do czasu scalenia) | TAK — alias naprawia trwale i automatycznie |
| 4 | Zagraniczny bez NIP, adres jednoliniowy | klucz stabilny (zależy tylko od `nazwa`) | TAK, jeśli różnica w nazwie jest niewielka | średnie (zależy od konsekwencji nazwy u kontrahenta) | TAK — jak scenariusz 3 |
| 5 | Uproszczona bez nazwy nabywcy | JEDEN stały klucz dla wszystkich | n/d (zamierzone scalenie) | niskie–średnie | tak — zgodne z praktyką dla sprzedaży anonimowej |

**Wspólny mianownik ryzyka:** jedyny scenariusz z CICHYM (bez ostrzeżenia)
sklejeniem dwóch faktycznie różnych podmiotów to **scenariusz 2** — brak
NIP-u i brak jakiegokolwiek adresu/kontaktu jednocześnie. Wszystkie pozostałe
przypadki albo poprawnie rozróżniają kontrahentów, albo (przy duplikacie)
generują ostrzeżenie fuzzy, które istniejący mechanizm aliasu potrafi trwale
naprawić w kolejnych eksportach. Rekomendacje nie zmieniają logiki klucza ani
ścieżek `sksef` — dotyczą wyłącznie dodatkowej sygnalizacji ostrzeżeń
i kosmetyki karty, zgodnie z zastrzeżeniem zadania.

## Pliki tego przeglądu

- `symulacja/edgecase/wspolne.py` — wspólne pomoce (generatory FA(3), stub klienta)
- `symulacja/edgecase/scenariusz1_podobienstwo_bez_nip.py`
- `symulacja/edgecase/scenariusz2_dwie_osoby_identyczne.py`
- `symulacja/edgecase/scenariusz3_ten_sam_raz_z_adresem.py`
- `symulacja/edgecase/scenariusz4_zagraniczny_bez_nip_jedna_linia.py`
- `symulacja/edgecase/scenariusz5_uproszczona_bez_nazwy.py`
- `symulacja/edgecase/RAPORT-edgecase.md` — ten dokument
