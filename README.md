# KSeF → Symfonia FK — eksport faktur (AMS .TXT)

Aplikacja desktopowa (Windows) dla 4 firm: pobiera z KSeF (produkcja) faktury
**sprzedażowe** i/lub **kosztowe** w wybranym zakresie dat, konwertuje je do
natywnego formatu importu **Symfonia FK (AMS `.TXT`, Windows-1250 + CRLF)**
i zapisuje w podfolderach per profil. Import do Symfonii wykonuje się ręcznie.

**Domyślny format wyjścia: hmffk** — JEDEN plik `.TXT` na eksport, zawierający
razem sekcje `Kontrahent{}` (pełne karty kontrahentów) i `dokument{}` (faktury
z rejestrem VAT i numerami KSeF), importowany w FK **standardowym szablonem
„Dane z programu Handel, format FK 3.0"** (`hmffk.AMS`) — niczego nie trzeba
kopiować do folderu szablonów Symfonii. Dokumenty trafiają do bufora bez
dekretacji (konta przypisuje księgowa). Alternatywnie (checkbox w GUI /
`--format sksef`): dotychczasowy tryb pod `SKsefInvoiceImport.AMS` (3.1).

## Uruchamianie

**Z Pythona (dev):**
```powershell
.venv\Scripts\python.exe run_app.py
```

**Spakowana aplikacja:** `dist\KsefSymfoniaExport\KsefSymfoniaExport.exe`

**CLI (bez GUI, np. pierwszy test):**
```powershell
.venv\Scripts\python.exe -m app.cli --nip <NIP> --od 2026-06-01 --do 2026-06-30 `
    --rodzaj koszt --cel C:\eksport-ksef --zapisz-fa3
# token poda się bezpiecznie przez getpass (albo zmienną środowiskową KSEF_TOKEN)
```

## Pierwsze uruchomienie GUI

1. Ustaw **hasło główne** — szyfruje tokeny KSeF na dysku (Fernet + scrypt,
   plik `%APPDATA%\KsefSymfoniaExport\config.enc`). Bez hasła konfiguracji nie
   da się odzyskać.
2. **Dodaj profil**: nazwa firmy, NIP (walidowany), token KSeF (produkcja,
   uprawnienie „przeglądanie faktur"/InvoiceRead — generowany w Aplikacji
   Podatnika KSeF, zakładka Tokeny), podfolder.
3. Wybierz zakres dat (dłuższy niż 3 miesiące jest automatycznie dzielony),
   zaznacz **Sprzedaż**/**Koszt**, wskaż folder docelowy, kliknij **Eksportuj**.

## Wynik

- **Format hmffk (domyślny):** jeden plik `hmffk_<profil>_<od>_<do>_<czas>.TXT`
  w `<folder>\<podfolder>\` — najpierw wszystkie karty `Kontrahent{}`
  (deduplikacja po NIP; klucz stabilny między eksportami), potem `dokument{}`
  po dacie wystawienia. Sprzedaż = FVS/FKS, koszt = FVZ/FKZ (koszt dostaje
  `Datawpl` = dzień nadania numeru KSeF w czasie polskim, a rejestr VAT okres = data wpływu).
- **Format sksef:** 1 plik `.TXT` na fakturę, nazwa = numer KSeF.
- **Zawsze komplet:** jeśli KSeF raportuje X faktur w zakresie, eksport
  obejmuje dokładnie X — także pobierane wcześniej. Uwaga: `eArchiveId` jest
  generowany na świeżo przy każdym eksporcie (ten sam dokument zaimportowany
  ponownie nie koliduje z egzemplarzem w buforze — ale może powstać duplikat;
  przed ponownym importem usuń poprzedni egzemplarz z bufora albo używaj trybu
  „tylko nowe"). Tryb przyrostowy „tylko nowe" (checkbox / `--tylko-nowe`)
  pomija faktury z rejestru — do codziennej pracy.
- **Paczka zbiorcza** (checkbox w GUI / flaga `--paczka` w CLI): jeden plik
  `paczka_<rodzaj>_<od>_<do>_<znacznik czasu>.TXT` per rodzaj (koszt/sprzedaż),
  z wieloma sekcjami `Dokument{}` — Symfonia importuje całość za jednym razem.
  Uwaga: przy pliku zbiorczym błąd jednej faktury podczas importu w Symfonii
  może zatrzymać wczytywanie reszty — pliki per faktura izolują problemy.
- **Plik kart kontrahentów** (checkbox / `--kontrahenci`): format sekcyjny
  **AMS** (ten sam mechanizm co import faktur) + własny szablon.
  Powstają dwa pliki: dane `kartoteka\kontrahenci_<profil>_<data>.TXT`
  (sekcje `Kontrahent{}`; klucz = NIP cyfry-only, stabilny; **obie strony**
  faktur minus NIP własny; dedup z preferencją pełniejszego rekordu; pełny
  adres strukturalny) oraz szablon `kartoteka\szablon\SKsefKontrahenciImport.AMS`
  (`#format "AMS"`, `#DANEKH "1"` jak w moście Handel→FK, który tworzy karty;
  `#POWIELONE_NAZWY "0"` — ponowny import aktualizuje bez pytań).

  **Import w Symfonii** (jednorazowa konfiguracja):
  1. Skopiuj **`SKsefKontrahenciImport.AMS`** do folderu szablonów Symfonii
     (tam, gdzie leży `SKsefInvoiceImport.AMS` — okno wyboru szablonu listuje
     tylko pliki z tego folderu). Plik jest w `reference\symfonia-szablony\`
     i jest też generowany przy eksporcie w `<cel>\<profil>\kartoteka\szablon\`.
  2. Konfiguracja współpracy → Katalogi wymiany danych → Nowy →
     Katalog = `<cel>\<profil>\kartoteka\`, Maska = `*.TXT`,
     Szablon = z listy: **„Import kontrahentow z KSeF"**
     (NIE szablon faktur, NIE `KONTRAH.AMX`), XSLT puste → Zapisz → Użyj.

  Zalecana kolejność: najpierw kontrahenci, potem faktury (faktury podpinają
  karty po NIP). Spec referencyjna: `reference\kontrahenci-import-E2E.md`.

## Spóźnione faktury (wpłynęły do KSeF po domknięciu swojego miesiąca)

Eksport wybiera faktury po **dacie wystawienia**. Faktura wystawiona np. 28.06,
która dostała numer KSeF dopiero 03.08 (tryb offline, spóźniony wystawca), nie
trafiłaby do żadnego eksportu: czerwiec był już wyeksportowany, a lipiec
i sierpień jej nie obejmują. Dlatego przy każdym eksporcie aplikacja sprawdza
w KSeF, co **wpłynęło** (data trwałego zapisu w KSeF) od jej ostatniego
sprawdzenia dla tej firmy i rodzaju (sprzedaż / koszt).

**Zapamiętane zakresy i domknięte miesiące.** Aplikacja pamięta (w
`ledger.db`, trwale), które zakresy dat wystawienia wyeksportowano — osobno dla
każdej firmy i rodzaju. Zakres zapisuje się po eksporcie przetworzonym
w całości (bez przerwania, bez „Eksport niepełny”); wyszukiwanie po numerze /
kontrahencie zakresu nie zapisuje. Zapisany zakres kończy się najwyżej na dniu
**przed** eksportem (dokładniej: przed dniem, do którego KSeF gwarantuje
kompletność danych — HWM, w czasie polskim): eksport „01.10–31.10” zrobiony
08.10 zapisuje się jako 01–07.10, bo faktury z dalszych dni jeszcze nie
istniały. **Miesiąc jest domknięty**, gdy jeden zapisany zakres obejmuje go
w całości, od 1. do ostatniego dnia — czyli domknięcie to osobny eksport całego
miesiąca zrobiony po jego końcu. Eksporty „na bieżąco” (01–07.09, 08–15.09…,
także z „Do” = koniec miesiąca zrobione w trakcie miesiąca) miesiąca nie
domykają, nawet gdy razem go pokrywają. Eksport kilku miesięcy naraz
(np. 01.07–30.09 zrobiony w październiku) domyka każdy w pełni objęty
miesiąc.

Co aplikacja robi z fakturą, która wpłynęła od ostatniego sprawdzenia, a nie
należy do wybranego zakresu:

- **spóźniona** — data wystawienia **przed** wybranym zakresem, w miesiącu
  **już domkniętym**, faktury nie ma w rejestrze pobranych: aplikacja
  **dokłada ją do tego samego pliku** i zapamiętuje, do którego eksportu ją
  dołączyła. Lista: pierwsze ostrzeżenie eksportu („UWAGA: Dołączono N
  spóźnionych faktur z wcześniejszych okresów: …” — numer faktury, data
  wystawienia, rodzaj, numer KSeF; w GUI przed nim mogą stać ostrzeżenia
  synchronizacji z Symfonią), podsumowanie „w tym spóźnione z wcześniejszych
  okresów N”, a okno końcowe jest **ostrzeżeniem** z listą pod „Pokaż
  szczegóły…”;
- **czeka na domknięcie swojego miesiąca** — data wystawienia w miesiącu
  jeszcze **niedomkniętym** (np. faktura z 14.08, która wpłynęła 06.09, gdy
  wyeksportowano dopiero 01–15.08 i 16–31.08 „na bieżąco”): nie trafia do
  pliku 01–07.09, zabierze ją domknięcie sierpnia jako zwykłą fakturę (także
  z „Pomiń już pobrane” — nie ma jej w rejestrze). To samo dla faktur wystawionych
  **po** wybranym zakresie (spóźnione dołączane są tylko do eksportu
  późniejszego okresu). Punkt kontynuacji ich nie przeskakuje, więc nic nie
  ginie;
- **bezpiecznik 60 dni** — faktura, która czeka tak dłużej niż 60 dni od
  wpływu do KSeF (liczone nie wcześniej niż od pierwszego sprawdzenia tej firmy
  i rodzaju), dołącza jako spóźniona z osobnym ostrzeżeniem („…czekały ponad
  60 dni od wpływu do KSeF na domknięcie swojego miesiąca — sprawdź, czy nie
  są już zaksięgowane”). Dotyczy miesięcy domykanych poza aplikacją albo
  domknięcia opóźnionego o ponad 60 dni od wpływu faktury; bez tego punkt
  stałby w miejscu w nieskończoność. Liczone od wpływu, nie od daty
  wystawienia: data wystawienia bywa dowolnie stara albo błędna, a punkt
  kontynuacji jest kursorem po dacie wpływu;
- faktury już wyeksportowane pomija (bez dubli).

**Na bieżąco + domknięcie — bez utraty i bez dubli.** Faktura z 03.09, która
wpłynęła po eksporcie 01–07.09, nie trafia do eksportu 08–15.09 — czeka na
domknięcie września (01–30.09), które bierze ją jako zwykłą fakturę. Dopiero
faktura z września, która wpłynie **po** domknięciu, jest spóźniona i dołącza
do najbliższego eksportu (np. 08–15.10). Uwaga na limit: faktura czekająca na
domknięcie trzyma punkt kontynuacji, więc sprawdzenie „od punktu” obejmuje
dłuższy odcinek (więcej zapytań przy dużym ruchu), dopóki miesiąc nie zostanie
domknięty.

**Ponowny pełny eksport** (bez „Pomiń już pobrane”) zakresu, do którego
dołączono spóźnione (albo zakresu, który go obejmuje — np. cały miesiąc po
eksportach „na bieżąco”), **dokłada je znowu** — plik jest taki sam jak za
pierwszym razem. Z „Pomiń już pobrane” — nie (są w rejestrze).

**Spóźniona, której nie da się przetworzyć** (błąd odczytu FA(3)): trafia do
błędów („zaksięguj ręcznie”) i na trwałą listę „do ręcznej obsługi”
w `ledger.db`. Punkt kontynuacji ją przeskakuje (błąd jest trwały — punkt
stałby w miejscu), a kolejne eksporty — także ponowny pełny eksport tego
samego zakresu i eksport JEJ miesiąca (z „Pomiń już pobrane” i bez), nawet po
poprawce aplikacji — nie zgłaszają jej ponownie ani nie dokładają do pliku,
żeby nie zaksięgować jej dwa razy; w takim eksporcie jest ostrzeżenie
„Pominięto N faktur oznaczonych do ręcznej obsługi: …”. Wyszukiwanie po
numerze faktury / kontrahencie tej listy nie stosuje (świadome pobranie).
Tak samo faktura **z wybranego zakresu**, której nie da się przetworzyć —
ale tylko w przebiegu, który zapisuje zakres (eksport kompletny dla tego
rodzaju). Polecenie „zaksięguj ręcznie” pada tylko wtedy; w każdym innym
przypadku (przebieg niepełny, zakres niezapisany — np. eksport z dziś,
wyszukiwanie) komunikat mówi, żeby jej jeszcze nie księgować ręcznie, bo
kompletny eksport jej zakresu (domknięcie miesiąca) zgłosi ją ponownie.

**Przejście ze starszej wersji.** Przy pierwszym otwarciu `ledger.db` przez
nową wersję (jednorazowo):

- zakresy odtwarzane są z nazw plików `hmffk_…_<od>_<do>_<stempel>.TXT` /
  `paczka_…` zapisanych w rejestrze pobranych przez starszą wersję; koniec
  zakresu przycinany jest do dnia przed datą ze stempla pliku (plik
  „01.09–30.09” zrobiony 16.09 obejmuje 01–15.09 i nie domyka września);
  pliki wyszukiwania (`_numery`, `_kontrahenci`) i pliki per faktura pomijane;
- zapamiętywany jest moment najstarszego eksportu starszej wersji dla każdej
  firmy. Pierwsze sprawdzenie spóźnionych (dla firmy i rodzaju bez punktu
  kontynuacji) startuje od niego (najwyżej 90 dni wstecz), a nie od północy
  „Od” — dzięki temu faktury z miesięcy domkniętych starszą wersją, które
  wpłynęły między tamtym domknięciem a pierwszym eksportem nowej wersji,
  zostają wykryte jako spóźnione;
- **zaległości** z miesięcy **sprzed** najstarszego zakresu starszej wersji
  (czas sprzed pracy aplikacji, np. czerwiec, gdy aplikacja pracuje od
  lipca) — faktury, które wpłynęły do KSeF **przed** pierwszym sprawdzeniem
  nowej wersji — są pomijane: nie trafiają do pliku, nie wstrzymują punktu,
  nie wpadają przez bezpiecznik; jeden zbiorczy wpis w logu, bez ostrzeżenia
  w oknie. Faktura z takiego miesiąca, która wpłynie **po** pierwszym
  sprawdzeniu, idzie zwykłą ścieżką: czeka (miesiąc nie jest domknięty),
  a po 60 dniach dołącza z bezpiecznika z ostrzeżeniem. Firma, której
  starsza wersja eksportowała tylko plikami per faktura (bez dat w nazwie),
  nie ma takiego horyzontu — jej stare faktury czekają i ewentualnie
  dołączają z bezpiecznika.

Punkt kontynuacji (HWM z KSeF — moment, do którego KSeF gwarantuje
kompletność danych) jest zapisywany w `%APPDATA%\KsefSymfoniaExport\ledger.db`
osobno dla każdej firmy i rodzaju, więc zamknięcie aplikacji niczego nie gubi.
Przesuwa się dopiero po zapisie plików i tylko dla rodzaju przetworzonego
w całości (bez przerwania, bez limitu KSeF, bez błędu paczki, bez
niepobranych) — błąd paczki sprzedaży nie blokuje punktu kosztów; inaczej
zostaje stary i następny eksport sprawdza ten sam odcinek jeszcze raz.

**Kolejność i limit zapytań.** Eksport najpierw robi główne zapytania
i pobieranie **wszystkich** wybranych rodzajów (zakres wybrany przez księgową
ma pierwszeństwo), a dopiero potem sprawdzenia „od punktu”. Sprawdzenie
pilnuje godzinnego limitu 20 zapytań o metadane: przed każdą stroną (250
faktur) sprawdza, ile zostało, zostawia rezerwę 2 zapytań na kolejny eksport
w tej godzinie i dzieli resztę po równo między rodzaje. Gdy limit się kończy,
sprawdzenie przerywa się **bez** oznaczania eksportu jako niepełny (zakres jest
kompletny) — ostrzeżenie „sprawdzanie spóźnionych dokończy się przy następnym
eksporcie” — a punkt zapisuje postęp (datę wpływu ostatniej sprawdzonej
faktury; KSeF zwraca je rosnąco, a `from` jest włącznie, więc faktury z tą
samą datą wrócą i odpadną w rejestrze), nie dalej jednak niż wpływ
najwcześniejszej faktury czekającej na domknięcie swojego miesiąca. Kolejny
eksport kontynuuje od tego miejsca. Uwaga: gdy czekająca faktura leży blisko
początku sprawdzanego odcinka, a odcinek od niej do „teraz” jest dłuższy niż
budżet jednego eksportu, sprawdzenie nie posuwa się dalej, dopóki jej miesiąc
nie zostanie domknięty (albo nie zadziała bezpiecznik) — spóźnione z dalszej
części odcinka są wtedy wykrywane z opóźnieniem (nic nie ginie).
Pierwsze sprawdzenie (np. zaraz po aktualizacji) zaczyna od początku wybranego
zakresu (północ czasu polskiego), nie dalej niż 90 dni wstecz — bez ściągania
starej historii. Eksport tuż po północy z „Od” = dziś (KSeF odpowiada wtedy
21183 — początek późniejszy niż HWM) to „nic nowego”, bez ostrzeżenia.
Wyszukiwanie po numerze faktury i po kontrahencie nie sprawdza spóźnionych
i nie rusza punktu. Spóźnione są pobierane pojedynczo (limit 64/h; paczka
eksportu obejmuje daty wystawienia z zakresu) — zwykle to 0–kilka faktur.

## Okno końcowe eksportu

- **Eksport zakończony** — informacja; **ostrzeżenie** (z listą pod „Pokaż
  szczegóły…”), gdy dołączono spóźnione faktury albo pojawili się nowi
  kontrahenci (nazwa, NIP, nr karty, konto — sprawdź numery kart w Symfonii
  po imporcie i popraw w oknie „Kontrahenci…”). Ostrzeżenie o nowych
  kontrahentach nie ginie, gdy zapis pliku się nie uda: numer karty jest już
  nadany, więc ostrzeżenie czeka w `ledger.db` i pokazuje się przy następnym
  udanym eksporcie tej firmy.
- **Miesiąc nie został jeszcze domknięty** — eksport objął cały miesiąc, ale
  dane KSeF nie są jeszcze kompletne do jego końca (np. domknięcie 30.09
  wieczorem albo 01.10 tuż po północy): zapisany zakres jest przycięty i okno
  (ostrzeżenie) mówi „Miesiąc wrzesień 2026 nie został jeszcze domknięty (dane
  KSeF kompletne do 29.09.2026) — powtórz domknięcie jutro”; przy eksporcie
  całego miesiąca w jego trakcie — „…domknij go eksportem całego miesiąca po
  jego zakończeniu”.
- **Eksport niepełny** — nie wszystkie faktury pobrano (przerwanie, limit KSeF,
  błąd paczki eksportu, błąd KSeF przy pobraniu): „Nie pobrano N faktur —
  uruchom eksport ponownie z zaznaczonym „Pomiń już pobrane” … nie księguj
  ich ręcznie”. Niepobrane nie są „błędami” — ponowny eksport dociąga je bez
  dubli.
- **Eksport zakończony z błędami** — faktury, których nie dało się
  przetworzyć (błąd odczytu FA(3), nietrafione wyszukiwanie): tylko te należy
  zaksięgować ręcznie.

**CLI — kod wyjścia:** `0` pełny bez błędów; `1` pełny, ale są błędy do
ręcznej obsługi (`BŁĄD …`); `2` błąd argumentów / konfiguracji / synchronizacji
z Symfonią (eksport nie ruszył); `3` eksport niepełny albo przerwany
(`NIE POBRANO …`, limit, błąd paczki) — uruchom ponownie z `--tylko-nowe`;
`3` ma pierwszeństwo przed `1`.

## Pobieranie wybranych faktur po numerze

Zamiast eksportować cały zakres dat, można pobrać tylko konkretne faktury —
podając ich **numer nadany przez wystawcę** (np. `FV/123/2026`), **nie** numer
KSeF. KSeF nie umożliwia wyszukiwania po numerze wystawcy, więc aplikacja i
tak pobiera metadane z podanego zakresu dat i rodzajów (Sprzedaż/Koszt) — jak
przy zwykłym eksporcie — a następnie **filtruje** je do podanych numerów,
pobierając (i zapisując do pliku) wyłącznie trafienia. Zakres dat i rodzaje
trzeba więc ustawić tak, by na pewno obejmowały szukane faktury.

Porównanie numerów jest znormalizowane: ignoruje wielkość liter i wszystkie
białe znaki, więc `FV 123/2026`, `fv123/2026` i `FV123/2026` to ten sam numer.

**Skrócone formy wpisu** — gdy trzeba dociągnąć całą serię pominiętych
faktur (np. `FV/07/2026/00247` … `FV/07/2026/00251`), nie trzeba przepisywać
pełnych numerów. Każdy wpis może być:
- **pełnym numerem** — jak dotychczas, dopasowanie dokładne (znormalizowane);
- **samą końcówką** — np. `247` trafia fakturę kończącą się na `247`
  (`FV/07/2026/00247`), niezależnie od zer wiodących; dopasowanie po
  OSTATNIM ciągu cyfr w numerze faktury, więc `FV/07/2026/01247` (końcówka
  `1247`) nie pasuje;
- **zakresem końcówek** — np. `247-251` rozwija się do końcówek 247, 248,
  249, 250, 251 (max 2000 numerów w zakresie; zakres odwrócony albo zbyt duży
  trafia od razu do raportu błędów, bez odpytywania KSeF).

Wpisy dowolnie się mieszają i można je łączyć przecinkami/średnikami/nowymi
liniami, np. `247-251, 320-323, 413` obok pełnego numeru w tym samym polu.
Zakres liczy się w podsumowaniu jako **jeden wpis** (nie jako N numerów),
niezależnie od tego, ile końcówek obejmuje.

- **GUI:** pod parametrami eksportu jest pole **„Pobierz tylko wybrane
  faktury (po numerze faktury)"** — wpisz numery/końcówki/zakresy po jednym w
  wierszu albo rozdziel je przecinkami/średnikami. Gdy pole jest niepuste,
  przycisk zmienia etykietę na **„Eksportuj wybrane (N)"** i eksport szuka
  tylko tych N wpisów w ustawionym zakresie dat/rodzajów; puste pole = zwykły
  eksport całego zakresu (jak dotychczas). Pole nie jest czyszczone po
  zakończeniu eksportu.
- **CLI:** flaga `--numer NR` (można powtórzyć dla wielu wpisów) i/lub
  `--numery-plik PLIK` (plik tekstowy, jeden wpis w wierszu, UTF-8) — obie
  można łączyć, obie przyjmują wszystkie trzy formy. `--od`/`--do`/`--rodzaj`
  pozostają wymagane/istotne, bo to w tym zakresie numery są szukane, np.:
  ```powershell
  .venv\Scripts\python.exe -m app.cli --nip <NIP> --od 2026-06-01 --do 2026-06-30 `
      --rodzaj koszt --cel C:\eksport-ksef --numer FV/123/2026 --numer 247-251
  ```

**Gdy wpis nie zostanie znaleziony** w zakresie/rodzajach — trafia do
raportu błędów (`report.failed`, widoczne w logu GUI jako `BŁĄD <numer>: nie
znaleziono w KSeF w zakresie ...`, w CLI analogicznie), a podsumowanie
eksportu pokazuje `szukano N numerów, znaleziono M` zamiast zwykłego
`znaleziono X`. Gdy ten sam numer (albo ta sama końcówka) pasuje do więcej
niż jednej faktury (różni wystawcy/serie) — wszystkie trafione faktury i tak
zostają pobrane, a eksport dodaje ostrzeżenie do ręcznej weryfikacji; zakres,
w którym każda końcówka trafia dokładnie jedną fakturę, ostrzeżenia nie
generuje.

## Pobieranie faktur wybranych kontrahentów

Gdy trzeba dociągnąć dokumenty konkretnych kontrahentów (np. FK pominęło je
przy imporcie), nie trzeba spisywać numerów faktur — wystarczy wskazać
kontrahentów. Pole w GUI **„Tylko faktury kontrahentów"** (obok pola numerów
faktur) przyjmuje **numery kart z kartoteki FK lub NIP-y** rozdzielone
przecinkami/spacjami; w CLI: `--kontrahent ID` (wielokrotnie) albo
`--kontrahenci-plik PLIK` (jeden identyfikator w wierszu).

- Numery kart są tłumaczone na NIP przez rejestr numeracji profilu — karta
  musi więc być w rejestrze i mieć NIP (inaczej identyfikator trafia do raportu
  błędów i nic nie jest pobierane).
- Dopasowanie: **Sprzedaż** — po NIP nabywcy, **Koszt** — po NIP sprzedawcy;
  faktury są wyszukiwane w ustawionym zakresie dat i rodzajach.
- Filtr może współistnieć z filtrem numerów faktur (oba warunki naraz).
- Kontrahent bez trafień → wpis w raporcie „brak faktur w KSeF w zakresie …";
  plik wynikowy dostaje infiks `_kontrahenci` (`_numery_kontrahenci` przy obu
  filtrach). Do ponownego pobrania faktur już wyeksportowanych **wyłącz
  „tylko nowe faktury"**.

## Adresy i tworzenie kontrahentów

- Adres w FA(3) to dwie swobodne linie (`AdresL1`/`AdresL2`). Aplikacja rozbija
  je heurystycznie na pola strukturalne karty FK: **Ulica, NrDomu, NrLokalu,
  KodPocztowy, Miejscowość** — i emituje je zarówno w sekcji `Kontrahent{}`
  każdej faktury, jak i w pliku kartoteki. Gdy rozbicie się nie uda (np. adres
  zagraniczny), całość zostaje w polu ulicy — nic nie ginie (AdresL1/L2 są
  przekazywane zawsze).
- Oryginalny szablon `SKsefInvoiceImport.AMS` ma `#DANEKH "2"` — jeśli przy
  imporcie faktur karty kontrahentów NIE są zakładane w kartotece, użyj
  wariantu **`SKsefInvoiceImport_DANEKH1.AMS`** (w `reference\symfonia-szablony\`
  i w `dist\...\_internal\reference\symfonia-szablony\`): identyczny szablon
  z `#DANEKH "1"`. Podmień szablon w profilu importu w Symfonii i porównaj
  zachowanie na 1 fakturze od nieznanego dostawcy.
- Rejestr pobranych faktur (dedup): `%APPDATA%\KsefSymfoniaExport\ledger.db` —
  ta sama faktura nie zostanie wyeksportowana drugi raz.
- Faktury niemożliwe do automatycznego zaksięgowania (procedura marży, OSS,
  odwrotne obciążenie, brak podsumowania VAT) trafiają do raportu błędów —
  księguje się je ręcznie.
- Faktury walutowe są eksportowane z ostrzeżeniem — zweryfikuj kursy przy
  pierwszym imporcie w Symfonii.

## Import w Symfonii FK

**Format hmffk (domyślny):** Konfiguracja współpracy → Katalogi wymiany danych →
profil importu z szablonem **„Dane z programu Handel, format FK 3.0"**
(`hmffk.AMS` — standardowo w folderze szablonów FK, nic nie kopiujesz).
Przy pierwszym imporcie FK poprosi o jednorazowe uzgodnienie typów dokumentów
(FVS/FKS/FVZ/FKZ) i rejestrów VAT — wybierz odpowiedniki ze swojej firmy,
FK zapamięta. Pytanie „Czy importować opisy faktur VAT?" — obojętne (plik nie
niesie opisów). Kontrahenci zakładają się w kartotece z sekcji `Kontrahent{}`.

**Format sksef:** profil importu z szablonem `SKsefInvoiceImport.AMS`
(pole XSLT zostaje **puste** — transformację robi ta aplikacja).

W obu trybach dokumenty lądują w buforze FK, księgowanie z bufora ręcznie.

## Numeracja kontrahentów i konto na zapisie

**Ustalenie empiryczne z produkcji:** Symfonia FK numeruje karty kontrahentów
kolejno i **NIE zasypuje dziur** po kartach usuniętych — nowa karta dostaje
ZAWSZE `max(pozycja)+1`, niezależnie od dziur pozostawionych po usuniętych
kartach (zaobserwowane: dziury 182/183/453 zostały puste, kolejne nowe karty
poszły od 883). Żeby konto rozrachunkowe zapisywane w linii brutto sekcji
`zapis{}` pliku hmffk (`Konto =<prefiks>-<pozycja>`) zgadzało się z numerem
karty faktycznie nadanym w FK, aplikacja trzyma **własny rejestr numerów**
(per NIP profilu, `app/kontrahenci_nr.py`, SQLite w
`%APPDATA%\KsefSymfoniaExport\`) i odwzorowuje tę samą zasadę **max+1**
(`RejestrNumerow.nastepny_numer`, użyta przez `przydziel`/`utworz_wpis`).
Metoda `pierwszy_wolny` (najmniejszy wolny numer) nadal istnieje jako
pomocnicza, ale NIE odzwierciedla numeracji FK — nie jest już używana do
przydziału.

- **Seed z istniejącej kartoteki**: zanim rejestr zacznie numerować nowych
  kontrahentów samodzielnie, wczytaj do niego aktualny stan kartoteki z FK —
  GUI: **Kontrahenci… → Importuj kartotekę…** (plik `.xlsx`/`.csv` z eksportu
  kartoteki FK: kolumny Pozycja/NIP/Nazwa/Miejscowość), CLI: `--import-kartoteka PLIK`.
  Wiersze bez pozycji albo z kolizją pozycji/klucza są pomijane z ostrzeżeniem.
- **Procedura naprawcza po eksporcie bez seedu**: jeśli eksport wystartował
  zanim rejestr został zaseedowany kartoteką z FK, każdy kontrahent dostał
  numer `auto` od 1 w górę — rozjeżdżający się z rzeczywistymi numerami kart
  w Symfonii (rzeczywisty przypadek: 510 błędnych wpisów `auto` dla jednego
  profilu). „Wyczyść rejestr profilu” (`RejestrNumerow.wyczysc_profil`) kasuje
  WSZYSTKIE wpisy numerów i aliasy danego profilu naraz — po czym trzeba
  zaimportować kartotekę (seed) ponownie i dopiero potem wznowić eksport.
- **Synchronizacja z kartoteką FK**: kartoteka bywa też zasilana RĘCZNIE
  bezpośrednio w Symfonii (poza tym eksportem — użytkownik zakłada karty w FK
  ręcznie), co z czasem rozjeżdża rejestr aplikacji z rzeczywistym stanem FK.
  `RejestrNumerow.synchronizuj_z_kartoteka(profil_nip, sciezka)` traktuje
  świeży eksport kartoteki z FK jako **źródło prawdy** i zwraca `RaportSync`:
  klucze nieobecne w rejestrze są dodawane (seed), wpisy na tej samej pozycji
  są uzupełniane o brakujące dane, a wpisy pod INNĄ pozycją niż w FK zostają
  **nadpisane pozycją z FK** (kolizje rozwiązywane przez tymczasowe ujemne
  pozycje w jednej transakcji) i zgłoszone w `rozjazdy`. Wpisy z rejestru
  nieobecne w kartotece FK **nie są usuwane** — trafiają do `brak_w_fk` do
  ręcznej weryfikacji (mogą to być karty scalone aliasem albo świeżo
  przydzielone i jeszcze niewyeksportowane do FK). Aliasy pozostają
  nietknięte. Operacja jest transakcyjna (rollback przy błędzie) i
  idempotentna — ponowne uruchomienie na tym samym pliku nic nie zmienia.
- **Wpisy ustawione ręcznie (status `manual`) a synchronizacja**: wpis
  poprawiony w oknie „Kontrahenci…” dostaje status `manual` i synchronizacja
  (z pliku, z Symfonii i automatyczna przed eksportem) nie zmienia go w żaden
  sposób — różnicę numeru z Symfonią tylko raportuje. W bazach sprzed modelu
  z przyciskiem „Zapisz” status `manual` dostawał jednak KAŻDY dodany albo
  wklejony wiersz, więc część takich wpisów mogła nigdy nie być sprawdzona w
  Symfonii. Aplikacja NIE zmienia ich statusu sama: raport synchronizacji w
  oknie podaje liczbę wpisów `manual` (`RaportSync.reczne`), a gdy któryś z
  nich różni się od Symfonii — także raport eksportu podpowiada, jak je
  „odblokować”: w kolumnie Status wybrać `seed` i kliknąć „Zapisz”; kolejna
  synchronizacja poprawi wtedy numer, nazwę (kontrahenci z NIP-em) i puste pola według Symfonii.
- **Bezpiecznik — kartoteka/baza NIEWŁAŚCIWEJ firmy** (po incydencie
  produkcyjnym: pomyłkowo wybrana sąsiednia baza Symfonii dopisała do
  rejestru ~900 obcych kart i nadpisała 285 pozycji, zanim ktokolwiek
  zauważył pomyłkę). Zanim `_synchronizuj_z_fk` (rdzeń współdzielony przez
  `synchronizuj_z_kartoteka` i `synchronizuj_z_sql`) zmieni cokolwiek w
  rejestrze, sprawdza zgodność źródła z profilem: jeśli profil ma już **co
  najmniej 20 wpisów z NIP-em**, liczy odsetek tych NIP-ów obecnych (po
  cyfrach — bez `PL`/spacji/myślników) w nadchodzącej kartotece/bazie. Poniżej
  **60% zgodności** — synchronizacja jest ODRZUCANA w całości (wyjątek
  `SynchronizacjaOdrzucona`, rejestr **bez żadnej zmiany**) z komunikatem
  wskazującym, że to prawdopodobnie kartoteka innej firmy, i podpowiedzią, by
  w razie zamierzonej podmiany najpierw użyć „Wyczyść rejestr profilu…”.
  Profil pusty albo z mniej niż 20 wpisami z NIP-em (seed startowy) **nie jest
  kontrolowany** — nie ma z czym porównywać. Parametr `wymus=True` (na razie
  nieużywany przez GUI) pozwala świadomie pominąć kontrolę. W GUI wyjątek jest
  obsłużony tak samo jak `SymfoniaSqlError` — w oknie Kontrahenci… (obie
  ścieżki: „Synchronizuj z Symfonią” i „Synchronizuj z pliku…”) pokazuje się
  ostrzeżenie i rejestr zostaje bez zmian, a w automatycznej synchronizacji
  przed eksportem (`ExportWorker`/`_synchronizuj_sql_przed_eksportem`)
  **przerywa cały eksport**; CLI (`--sync-sql`) kończy się kodem `2`.
- **Nowi kontrahenci**: przy eksporcie każdy nowy klucz kontrahenta dostaje
  automatycznie kolejny numer **max+1** (zapisany w rejestrze), a gdy jego
  nazwa jest **podobna** (dopasowanie rozmyte, literówki/szyk wyrazów/wielkość
  liter/polskie znaki) do już zarejestrowanego kontrahenta, eksport i tak
  przydziela numer, ale dodaje ostrzeżenie do ręcznej weryfikacji.
- **Scalanie duplikatów**: gdy dwie karty w FK to ten sam kontrahent (np.
  literówka w nazwie z poprzedniego importu), w GUI (**Kontrahenci… → Scal
  z…**) wskaż wpis docelowy — kolejne faktury tego kontrahenta dostaną numer
  docelowy. Checkbox „karta-duplikat została/zostanie usunięta w FK" zwalnia
  numer źródłowy do puli; bez zaznaczenia numer źródłowy zostaje zajęty
  (status `duplikat`), bo karta nadal fizycznie istnieje w FK. Aliasy
  łańcuchowe są rozwiązywane do punktu stałego (scalenie A→B, potem B→C daje
  finalnie A→C). Poprawka NIP-u albo nazwy/miejscowości (kontrahent bez NIP)
  w tabeli zmienia klucz wpisu, a eksport liczy klucz z faktury KSeF — zapis
  dopisuje więc alias stary klucz → nowy (kolejne faktury tego kontrahenta
  trafiają na tę samą kartę, nie na nowy numer). NIP/nazwa, która jest już
  aliasem innej karty, jest przy zapisie odrzucana z komunikatem, na którą
  kartę trafiają takie faktury.
- **Dwa konta-prefiksy rozrachunkowe** — jeden konto-prefiks nie wystarczał,
  bo sprzedaż i koszt księgują się na różnych stronach i kontach: pola
  w oknie „Kontrahenci…" (albo flagi CLI) ustawiają osobno
  - **Konto sprzedaży** (`--konto-prefix-sprzedaz`, domyślnie `201-2`) — trafia
    na linię **WN brutto** dekretu faktur sprzedażowych,
  - **Konto kosztów** (`--konto-prefix-koszt`, domyślnie `202-2`) — trafia
    na linię **MA brutto** dekretu faktur kosztowych.

  Pełne konto to zawsze `<prefiks>-<pozycja karty w rejestrze numerów>`,
  np. `201-2-31` (sprzedaż) albo `202-2-7` (koszt).
- **Pełny dekret sprzedaży** — poza kontem rozrachunkowym dokumenty
  sprzedażowe dostają dodatkowo konta na obu liniach MA:
  - **Konto przychodów** (`--konto-sprzedaz-netto`, domyślnie `703`) — linia
    **MA netto**,
  - **Konto VAT należnego** (`--konto-sprzedaz-vat`, domyślnie `222-1`) —
    linia **MA VAT**.

  Dla faktur kosztowych linia WN VAT dostaje **domyślnie konto `221-1`**
  (VAT naliczony — `--konto-koszt-vat`); linia WN netto zostaje **domyślnie
  bez konta** (`--konto-koszt-netto`, domyślnie puste — konto kosztowe
  przypisuje księgowa ręcznie przy dekretacji w FK), ale pole można ustawić
  na stałe, jeśli wszystkie koszty firmy mają trafiać na jedno konto. Konto
  rozrachunkowe (MA brutto) zawsze pochodzi z rejestru numerów.
- **Konta nadpisane per firma** — powyższe konta (i termin płatności) mają
  wartość **wspólną** (`AppConfig`, patrz domyślne wyżej), ale plany kont
  różnią się między firmami (np. jedna firma księguje przychody na `701`
  zamiast wspólnego `703`). W GUI: **Kontrahenci…** dla wybranego profilu,
  sekcja „Konta księgowe — <nazwa firmy>” — każde pole pokazuje wartość
  efektywną dla tej firmy (nadpisanie, jeśli jest, inaczej wartość wspólna) i
  w tooltipie przypomina, jaka jest wartość wspólna. **Puste pole tekstowe
  albo wpisanie tej samej wartości co wspólna** kasuje nadpisanie i firma
  wraca na konto wspólne; inna wartość zapisuje się jako nadpisanie tylko dla
  tej firmy (`Profile.konta_override`), nie rusza innych profili i przeżywa
  zmianę wartości wspólnej.
- **Rozrachunki (automatyczny termin płatności w FK)** — zapis rozrachunkowy
  (linia z kontem rozrachunkowym: WN brutto sprzedaż / MA brutto koszt)
  dostaje dodatkowo `NumerDok =<numer faktury>` i `IdDlaRozliczen =1`; gdy
  faktura ma termin płatności, zaraz po zamknięciu tego zapisu dochodzi
  sekcja `transakcja{}` z tym samym terminem (format ISO) i kwotą brutto —
  FK zakłada rozrachunek z terminem automatycznie, bez ręcznego wypełniania.
  Parowanie zapisu z transakcją działa **wyłącznie** po zgodności
  `IdDlaRozliczen` (niezgodność = błąd importu 611). Bez konta rozrachunkowego
  (np. nowy kontrahent bez przydzielonej jeszcze pozycji) żadne z tych pól się
  nie pojawia. Przepis zweryfikowany empirycznie: `reference\rozrachunki-hmffk-E2E.md`.
  - **Brak terminu płatności w FA(3)** — realny test na żywo pokazał, że
    większość faktur (505/547) nie niesie `Fa/Platnosc/TerminPlatnosci`, co
    bez dalszych działań oznaczałoby brak rozrachunku dla prawie wszystkich
    dokumentów. Gdy termin jest pusty, aplikacja liczy go sama jako **data
    wystawienia (P_1) + `--termin-dni` dni** (domyślnie **14**) — sekcja
    `transakcja{}` powstaje więc niemal zawsze.
- **Uwaga — ręczne zmiany kart w FK**: rejestr nie widzi kartoteki FK na
  bieżąco. Jeśli karta zostanie ręcznie usunięta, przeniesiona albo dodana
  bezpośrednio w Symfonii z pominięciem tego eksportu, popraw rejestr
  ręcznie (okno „Kontrahenci…": Dodaj/Zmień/Usuń) — w przeciwnym razie numer
  w kolejnym pliku hmffk rozjedzie się z numerem karty w FK.

## Synchronizacja z bazą Symfonii (SQL, tylko odczyt)

Poza synchronizacją z pliku (wyżej) rejestr numerów można zsynchronizować
**bezpośrednio z żywej bazy SQL Server Symfonii**, bez pośredniego eksportu
kartoteki do pliku — `app/symfonia_sql.py` + `RejestrNumerow.synchronizuj_z_sql`.

- **Zakres autoryzacji i żelazne zasady** — dostęp obejmuje wyłącznie
  instancję `.\SAGEPL` (SQL Server 2019 Express, uwierzytelnianie Windows) i
  wyłącznie zapytania **SELECT** na: `sys.databases` (lista baz) i
  `FK.fk_kontrahenci` (kartoteka kontrahentów: numer karty/kod/nazwa/NIP/
  miejscowość). **Żadnych zapisów, zmian ani odczytu dokumentów księgowych.**
  Każde zapytanie ma 30-sekundowy timeout. Autoryzacja i pełne uzasadnienie —
  patrz `MEMORY.md`.
- **Jak to działa pod spodem** — `app/symfonia_sql.py` woła narzędzie
  `sqlcmd` przez `subprocess` (lista argumentów, bez powłoki) i czyta wynik z
  pliku tymczasowego (`-o`) zamiast ze stdout procesu — samo przechwycenie
  stdout na Windows przekłamuje polskie znaki (kodowanie konsoli), plik
  binarny odczytany i zdekodowany jako UTF-16 (flaga `-u`) nie. Flaga `-b`
  (batch abort) wymusza niezerowy kod wyjścia sqlcmd przy błędzie zapytania —
  bez niej sqlcmd domyślnie kończy się kodem 0 nawet po błędzie T-SQL.
- **Mapowanie bazy w profilu** — `Profile.db_symfonia` trzyma nazwę bazy
  firmowej na instancji `.\SAGEPL` (np. `ALFATRON_SERWIS_MEDYCZNY`)
  odpowiadającej danemu profilowi. W edycji profilu firmy (**Dodaj/Edytuj**)
  pole „Baza Symfonii (SQL)” to edytowalna lista wszystkich baz z instancji
  (`lista_baz()`) — dla profilu bez wcześniejszego ustawienia aplikacja sama
  proponuje kandydata przez dopasowanie fragmentu nazwy firmy do nazwy bazy
  (`gui._dopasuj_baze_po_nazwie`). Gdy lista baz jest niedostępna (inny komputer,
  brak `sqlcmd`), pole zamienia się w zwykłe pole tekstowe z komunikatem błędu
  w podpowiedzi — nazwę bazy da się wtedy wpisać ręcznie. `Profile.sql_autosync`
  (checkbox „Synchronizuj z Symfonią przed każdym eksportem”, aktywny gdy baza
  jest wybrana, domyślnie zaznaczony) włącza automatyczną synchronizację.
- **Automatyczna synchronizacja przed eksportem** — gdy profil ma ustawione
  `db_symfonia` i `sql_autosync`, `ExportWorker` (GUI) synchronizuje rejestr
  numerów z bazą SQL tuż przed pobraniem faktur (`RejestrNumerow.
  synchronizuj_z_sql`), loguje wynik ("dodano X, zaktualizowano Y") i dopisuje
  ewentualne rozjazdy/brak_w_fk jako ostrzeżenia do raportu eksportu. Błąd
  synchronizacji (np. `sqlcmd` niedostępny, timeout) **przerywa cały eksport**
  czytelnym komunikatem — to świadoma ochrona numeracji kont kontrahentów w
  plikach księgowych; komunikat podpowiada wyłączenie auto-synchronizacji w
  edycji profilu, jeśli trzeba wyeksportować mimo awarii bazy SQL. CLI ma
  odpowiednik jednorazowy: `--sync-sql BAZA` (błąd → `exit 2`, bez GUI).
- **Ręczna synchronizacja z okna Kontrahenci…** — obok **Synchronizuj z
  pliku…** (dawne „Synchronizuj z FK…”, synchronizacja z eksportu do pliku)
  jest **Synchronizuj z Symfonią** — synchronizacja z bazą SQL przypisaną
  profilowi na żądanie, bez czekania na najbliższy eksport.
- **Plikowa synchronizacja jako awaryjna** — gdy baza SQL jest niedostępna
  (inny komputer, brak `sqlcmd`, zablokowany dostęp sieciowy) albo profil nie
  ma ustawionego `db_symfonia`, dotychczasowa ścieżka przez eksport kartoteki
  do pliku i **Kontrahenci… → Synchronizuj z pliku…** (`synchronizuj_z_kartoteka`)
  działa bez zmian — obie metody współdzielą ten sam rdzeń synchronizacji
  (`RejestrNumerow._synchronizuj_z_fk`: dodane/zaktualizowane/rozjazdy/
  brak_w_fk, patrz wyżej), więc reguły i gwarancje (transakcyjność,
  idempotencja, nieusuwanie wpisów spoza kartoteki) są identyczne.
- **Testy** — jednostkowe (`tests/test_symfonia_sql.py`) monkeypatchują
  `subprocess.run` i nie wymagają żywej bazy. Integracyjne (marker `sql`,
  domyślnie pomijane — patrz niżej) łączą się z prawdziwą instancją
  `.\SAGEPL` i weryfikują m.in. poprawność kodowania polskich znaków na
  żywych danych.

## Architektura

```
app/
├─ hmffk.py          # FA(3) -> JEDEN plik AMS "format FK 3.0" pod hmffk.AMS (format domyślny)
├─ fa_mapper.py      # FA(3) XML -> ksefInvoice/invoiceV2 (kontrakt XSLT Symfonii)
├─ ams_transform.py  # invoiceV2 -> XSLT -> AMS -> CP1250+CRLF .TXT (format sksef)
├─ ksef_client.py    # KSeF 2.0: auth tokenem, query metadata, download, limity
├─ exporter.py       # pętla: query -> dedup -> download -> map -> transform -> zapis
├─ ledger.py         # SQLite: dedup per (NIP, nr KSeF), punkt kontynuacji i wyeksportowane
│                    #   zakresy per (NIP, rodzaj), spóźnione (zakres dołączenia / do ręcznej obsługi)
├─ kontrahenci_nr.py # rejestr numerów kart FK + synchronizacja (plik / SQL)
├─ symfonia_sql.py   # odczyt (SELECT) z bazy SQL Server Symfonii przez sqlcmd
├─ config.py         # zaszyfrowany config (Fernet + scrypt)
├─ gui.py / main.py  # PySide6
└─ cli.py            # eksport bez GUI
reference/           # szablony Symfonii + XSD FA(3) + przepis API
samples/             # golden files (testy bajt-w-bajt)
tests/               # pytest; `-m live`/`-m sql` = testy z siecią/żywą bazą (domyślnie pomijane)
```

## Testy

```powershell
.venv\Scripts\python.exe -m pytest          # testy offline (bez sieci i bez żywej bazy SQL)
.venv\Scripts\python.exe -m pytest -m live  # smoke test produkcyjnego API KSeF
.venv\Scripts\python.exe -m pytest -m sql   # smoke test żywej bazy SQL Symfonii (tylko SELECT)
```

## Pakowanie

```powershell
.venv\Scripts\pyinstaller.exe --noconfirm --onedir --windowed --name KsefSymfoniaExport `
    --add-data "reference\symfonia-szablony;reference\symfonia-szablony" run_app.py
```

## Limity API KSeF — jak aplikacja je obsługuje

- **Eksport wsadowy (paczka ZIP)**: przy więcej niż 10 fakturach do pobrania
  aplikacja NIE robi pojedynczych GET-ów (limit 64/h!), tylko zleca w KSeF
  przygotowanie zaszyfrowanej paczki (`POST /invoices/exports`, AES-256-CBC,
  klucz szyfrowany kluczem publicznym MF), czeka na jej przygotowanie
  i pobiera całość — linki do części paczki nie podlegają limitom. Paczka
  większa niż 50 MB przychodzi w częściach (kawałki jednego archiwum):
  aplikacja pobiera wszystkie, odszyfrowuje każdą, skleja i rozpakowuje raz
  (duże paczki w pliku tymczasowym na dysku); wygasły link części (ważny ok.
  godziny) odświeża ponownym odczytem statusu.
- **Duże wolumeny (więcej niż 250 faktur rodzaju w zakresie)**: pierwsza
  strona metadanych jest sondą — gdy KSeF ma więcej, aplikacja nie listuje
  całego zakresu (20 zapytań/h nie wystarczało powyżej ok. 5000 faktur),
  tylko pobiera go paczkami: metadane z `_metadata.json` w paczce, XML z tej
  samej paczki. Paczka ma najwyżej 10 000 faktur (1 GB); uciętą aplikacja
  dociąga kolejnym zleceniem od dnia przed datą ostatniej faktury (nakładkę
  usuwa po numerze KSeF). Paczki są przetwarzane po kolei — błąd, limit albo
  brak gotowej paczki po 30 min oznacza „Eksport niepełny”, a to, co już
  przetworzone, zostaje; ponowny eksport z „Pomiń już pobrane” dociąga resztę.
  Paczka niegotowa po 30 min zostaje zamówiona w KSeF — kolejny eksport tego
  samego zakresu (w ciągu 6 h) odbiera ją zamiast zamawiać nową (numer
  zlecenia i klucz paczki w `quota.db`, usuwane po odebraniu albo błędzie
  paczki; zostają przy błędzie sieci). Gdy przy odbiorze paczka znów nie jest
  gotowa, a od zamówienia minęło ponad 3,5 h (zegar KSeF), zlecenie uznaje się
  za zawieszone i kolejny eksport zamawia nową — wolny KSeF (przygotowanie do
  ok. 3,5 h) zdąży, zawieszone zlecenie blokuje zakres najwyżej ok. 4 h.
  Taka paczka to stan KSeF z chwili zamówienia (wg zegara KSeF — nagłówek Date
  odpowiedzi), więc faktury z zakresu, które wpłynęły później, aplikacja
  sprawdza osobnym zapytaniem (od zamówienia minus godzina; bez zegara KSeF —
  minus 12 h) i dociąga; gdy to sprawdzenie nie przejdzie (limit), eksport jest
  niepełny i miesiąc nie zostaje domknięty.
  Do 250 faktur — jak dotąd (ta sama lista, bez dodatkowych zapytań).
- **Limity liczone trwale**: znaczniki wywołań (8/s, 16/min, 64/h pobrania;
  8/s, 16/min, 20/h metadane; 8/s, 16/min, 20/h zlecenia paczki — osobna
  pula; 10/s, 60/min, 600/h status paczki — osobna pula, odpytywany co 5 s,
  po minucie co 15 s) są zapisywane per NIP w
  `%APPDATA%\KsefSymfoniaExport\quota.db` — kolejne uruchomienia znają zużyty
  budżet godziny zegarowej i nie wpadają w 429.
- **Blokada serwera (429 Retry-After)**: zapamiętywana; eksport kończy się
  czytelnym komunikatem „wznowienie ok. HH:MM, pozostało N faktur" zamiast
  wisieć dziesiątki minut. Ponowne uruchomienie przed tą godziną nie uderza
  w serwer. Krótkie czekania (do 3 min) obsługiwane automatycznie z logiem.
- Zakres zapytania > 3 miesiące jest dzielony na okna automatycznie — zarówno
  zapytanie o metadane, jak i paczka eksportu (KSeF przyjmuje maks. 100 dni).
  Błąd jednej paczki nie kasuje tego, co już przetworzono; eksport kończy się
  wtedy ostrzeżeniem „Eksport niepełny” („nie pobrano N faktur”), a ponowny
  eksport z „Pomiń już pobrane” dociąga resztę — to nie są faktury do
  ręcznego księgowania.
- **Sprawdzenie spóźnionych faktur** kosztuje dodatkowe zapytania o metadane
  z tej samej puli 20/h per firma: zwykle 1 na rodzaj, czyli pełny eksport
  miesiąca sprzedaż+koszt to 4 zapytania (2 główne + 2 „od punktu”) — ok. 5
  pełnych eksportów tej samej firmy na godzinę (wcześniej ok. 10; każda firma
  ma osobną pulę). Więcej zapytań: +1 na każde 250 faktur, które wpłynęły od
  ostatniego sprawdzenia (np. 1000 faktur kosztowych i eksport raz w miesiącu
  = 4 zapytania zamiast 1), i +1 na każde 90 dni, gdy od ostatniego
  sprawdzenia minęło więcej (maks. 360 dni wstecz; starszy punkt jest
  przycinany z ostrzeżeniem; faktura czekająca na domknięcie swojego miesiąca
  trzyma punkt, więc okno sprawdzenia rośnie — najwyżej do bezpiecznika
  60 dni). Pierwszeństwo ma eksport zakresu: główne zapytania wszystkich
  rodzajów idą najpierw, a sprawdzenie „od punktu” bierze tylko to, co
  zostało w godzinie, z rezerwą 2 zapytań — przerwane dokończy się przy
  następnym eksporcie (punkt zapisuje postęp), eksport nie jest przez to
  niepełny. Główne zapytanie domknięcia miesiąca to 1 zapytanie na rodzaj
  także przy dużych wolumenach (powyżej 250 faktur zakres idzie paczkami).
  Przy kilkunastu tysiącach faktur miesięcznie sprawdzenie „od punktu”
  potrzebuje kilku eksportów (ok. 15 stron po 250 faktur na godzinę), żeby
  dogonić punkt — w symulacji ok. 13,5 tys. sprzedaży/mies.: 5 przebiegów
  co godzinę po domknięciu miesiąca.
- Zapytanie do KSeF idzie z zapasem 1 dnia z każdej strony, a faktury z datą
  wystawienia spoza wybranego zakresu aplikacja odcina sama — KSeF nie
  dokumentuje, jak porównuje datę wystawienia z granicami zakresu.
- KSeF pokazuje token tylko raz przy generowaniu — wklej go do profilu od razu.
