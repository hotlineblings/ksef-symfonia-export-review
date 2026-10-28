# Aktualizacje aplikacji na komputerze księgowej

## Jak to działa

Skrót na pulpicie („KSeF - Symfonia”) uruchamia `uruchom.pyw`, który przy
**każdym starcie**:

1. pobiera z GitHuba stan gałęzi `main` (bez internetu — startuje to, co jest na dysku);
2. odkłada zmiany zrobione lokalnie na komputerze księgowej w plikach
   aplikacji (edycje, lokalne commity, np. od asystenta AI) — **nie kasuje ich**:
   zmiany trafiają do schowka gita (`git stash`), commity na gałąź `kopia/<data>`.
   Pliki nieśledzone (np. „Eksportuj CSV…” zapisany w katalogu aplikacji,
   folder eksportu wewnątrz repo) zostają na dysku bez zmian. Przerwany merge
   albo rebase jest przerywany, a pozostałość po zabitym gicie (`index.lock`
   starszy niż 5 minut) usuwana;
3. ustawia katalog aplikacji dokładnie na `origin/main`;
4. gdy zmienił się `requirements.txt` — instaluje biblioteki; jeśli to się nie
   uda, wraca do ostatniej wersji zaakceptowanej (nigdy do lokalnego commitu),
   żeby aplikacja w ogóle wystartowała;
5. otwiera aplikację (po aktualizacji w nowym procesie).

Żaden błąd aktualizacji nie blokuje startu. Przebieg zapisuje się w
`%APPDATA%\KsefSymfoniaExport\aktualizacje.log`. W tytule okna widać wersję,
np. „(wersja 9419166 z 28.09.2026)”, a po nieudanej aktualizacji dopisek
„— aktualizacja nieudana (szczegóły: aktualizacje.log)”. Drugie kliknięcie
skrótu w trakcie pracy albo aktualizacji nie otwiera drugiej kopii
(komunikat „Aplikacja jest już uruchomiona”). Git działa bez okien logowania
i pytań o hasło, a jego wyjście czytane jest jako UTF-8 (ścieżki z polskimi
znakami, np. „OneDrive - … SPÓŁKA”, działają). Repozytorium, którego git nie
potrafi odczytać (np. „dubious ownership”), daje dopisek „aktualizacja nieudana”
w tytule, a nie ciche wyłączenie aktualizacji.

Każdy błąd startu kończy się komunikatem i wpisem w `aktualizacje.log` (pythonw
nie ma konsoli). Jeśli na `main` trafi kod, przez który nie wczytuje się sam
aktualizator, program startowy awaryjnie pobiera `main` zwykłym gitem (tylko gdy
katalog aplikacji sam jest repozytorium; lokalne zmiany do schowka, lokalne
commity na `kopia/<data>`) i prosi o ponowne uruchomienie — wystarczy wypchnąć
poprawkę. Gdy poprawki jeszcze nie ma na serwerze, komunikat prosi o zgłoszenie
błędu zamiast w kółko o restart. Biblioteki pominięte przez awaryjną
aktualizację doinstaluje zwykły aktualizator przy następnym starcie (znacznik
`requirements.txt` w katalogu `.git`). **Przed każdym pushem na
`main`:** pełne testy i próbne uruchomienie `python uruchom.pyw --bez-aktualizacji`.

## Akceptacja zmian

Komputer księgowej ma do repozytorium **klucz tylko do odczytu** (klucz
wdrożeniowy, wyłącznie to repozytorium). Nie może niczego wypchnąć ani czytać
innych repozytoriów właściciela. Na `main` trafia więc tylko to, co wypchnie
właściciel — **wypchnięcie na `main` = akceptacja**. Zmiany przygotowywane są na
gałęziach (`fix/...`) i scalane do `main` dopiero po sprawdzeniu.

Wydanie nowej wersji (u właściciela):

```bash
git checkout main && git merge --ff-only fix/<gałąź> && git push origin main
```

Księgowa dostaje ją przy następnym uruchomieniu aplikacji.

## Jednorazowa konfiguracja komputera księgowej

Bez pulpitu zdalnego: najpierw wypchnij wersję z tym skryptem na `main`, potem
księgowa wkleja jedno polecenie w PowerShell (Start → prawy przycisk → Terminal
albo Windows PowerShell; nie w „Wierszu polecenia”) i naciska Enter:

```powershell
cd "C:\Users\Anna\ksef-symfonia-export" -ErrorAction Stop; git fetch origin; if ($LASTEXITCODE -eq 0) { git checkout origin/main -- narzedzia/konfiguruj_komputer.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File narzedzia\konfiguruj_komputer.ps1 } else { Write-Host "Brak polaczenia z GitHubem - zadzwon do Bartka" }
```

Wysyłać mailem (nie SMS-em — łamanie linii psuje polecenie). Okno bez dopisku
„(administrator)”, na koncie, na którym księgowa pracuje z programem.

Polecenie pobiera nową wersję na obecnym logowaniu właściciela i uruchamia
`narzedzia\konfiguruj_komputer.ps1` (UTF-8 z BOM — inaczej Windows PowerShell 5.1
psuje polskie znaki i składnię). Skrypt:

0. prosi o zamknięcie programu i ustawia katalog na `origin/main`, niczego nie
   kasując: zmiany w plikach → schowek gita, lokalne commity (bieżąca gałąź i
   `main`) → gałęzie `kopia/przed-wdrozeniem-<data>`, pliki zastępowane przez nową
   wersję → `%APPDATA%\KsefSymfoniaExport\kopia-przed-wdrozeniem-<data>\`; tam też
   `zmiany-lokalne.patch` i `lokalne-commity.txt`, żeby właściciel mógł zobaczyć,
   co zmieniał asystent AI księgowej;
1. sprawdza/tworzy `.venv`;
2. tworzy klucz SSH i **sam dodaje go w GitHubie jako klucz tylko do odczytu**
   (Deploy keys) zapisanym na komputerze logowaniem właściciela — to jego ostatnie
   użycie; gdy się nie da (brak logowania, brak uprawnień), kopiuje klucz do
   schowka, księgowa wysyła go właścicielowi, a ten dodaje go ręcznie
   (repozytorium → Settings → Deploy keys → Add deploy key, **bez** „Allow write access”);
3. przełącza repozytorium na ten klucz (SSH przez port 443 — działa przy
   zablokowanym porcie 22) i **sprawdza próbnym pushem, że klucz nie ma prawa zapisu**;
4. przestawia stare skróty do `run_app.py` (pulpit, pulpit publiczny, pasek
   zadań, menu Start) na start z aktualizacją — księgowa klika tę samą ikonę; gdy
   na pulpicie nie ma skrótu z aktualizacją, tworzy „KSeF - Symfonia”;
5. usuwa z Menedżera poświadczeń Windows logowanie do GitHuba — tylko wpisy gita
   (GCM), `gh` i GitHub Desktop; logowania edytora, Outlooka itd. zostają.
   Niepowodzenie to żółte ostrzeżenie „Dla Bartka” w dzienniku, nie blokada
   (token i tak unieważniasz na GitHubie, patrz niżej).

Po przejściu na klucz aktualizator odróżnia brak internetu (start bez
aktualizacji, po cichu) od odmowy GitHuba (klucz usunięty, zły known_hosts):
odmowa daje w tytule okna „aktualizacja nieudana”.

Przebieg zapisuje się w `%APPDATA%\KsefSymfoniaExport\konfiguracja.log` — przy
błędzie księgowa wysyła ten plik. Polecenie można wkleić ponownie: skrypt
kończy to, czego nie skończył. Dane księgowej w `%APPDATA%` nie są ruszane.

**Po udanej konfiguracji (właściciel) — nie wcześniej, bo wklejany `git fetch`
potrzebuje jeszcze tego logowania:** usunięcie wpisu z Menedżera poświadczeń
nie unieważnia samego tokenu na GitHubie, a leżał on na komputerze, do którego
miał dostęp asystent AI księgowej. GitHub → Settings → Applications → Authorized
OAuth Apps → „Git Credential Manager” → Revoke (to samo z ewentualnym tokenem
osobistym). Uwaga: inne komputery właściciela logujące się przez Git Credential
Manager poproszą wtedy o ponowne logowanie.

Nie przetestowane poza Windows (sprawdzić w logu pierwszego przebiegu): odczyt
tokenu przez Git Credential Manager, `ssh-keygen` z pustym hasłem, SSH przez
port 443, zapis skrótów przez COM, `cmdkey`. Ścieżka awaryjna: pulpit zdalny i
ten sam skrypt.

## Gdy coś pójdzie nie tak

- Która wersja działa: tytuł okna albo `git log -1 --oneline` w katalogu repozytorium.
- Powrót do poprzedniej wersji dla wszystkich: `git revert` na `main` i push
  (księgowa dostanie cofnięcie przy następnym starcie).
- Odłożone lokalne zmiany księgowej: `git stash list`, podgląd
  `git stash show -p stash@{0}`, lokalne commity: `git branch --list "kopia/*"`.
- Log aktualizacji: `%APPDATA%\KsefSymfoniaExport\aktualizacje.log`.
