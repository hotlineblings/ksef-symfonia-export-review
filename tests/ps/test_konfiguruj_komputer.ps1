# Testy funkcji z narzedzia/konfiguruj_komputer.ps1 (pwsh 7 albo Windows PowerShell 5.1).
# Uruchamia je tests/test_konfiguruj_komputer.py; samodzielnie:
#   pwsh -NoProfile -File tests/ps/test_konfiguruj_komputer.ps1
# Kroki zależne od Windowsa (cmdkey, skróty COM, ssh do GitHuba) nie są tu testowane.

$ErrorActionPreference = "Stop"
$Skrypt = Join-Path $PSScriptRoot "..\..\narzedzia\konfiguruj_komputer.ps1"
. $Skrypt

$script:bledy = 0
function Sprawdz($warunek, $opis) {
    if ($warunek) { Write-Host "OK   $opis" } else { Write-Host "BŁĄD $opis"; $script:bledy++ }
}
function Scenariusz($nazwa, [scriptblock]$blok) {
    try { & $blok }
    catch { Write-Host "BŁĄD scenariusz $nazwa - wyjątek: $_"; Write-Host $_.ScriptStackTrace; $script:bledy++ }
}

# izolacja od konfiguracji gita właściciela (np. osxkeychain z jego tokenem)
$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("ksef-ps-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Tmp | Out-Null
$KonfigGlobalny = Join-Path $Tmp "gitconfig"
Set-Content -Path $KonfigGlobalny -Value "[user]`n`tuseConfigOnly = true`n[init]`n`tdefaultBranch = main"
$env:GIT_CONFIG_NOSYSTEM = "1"
$env:GIT_CONFIG_GLOBAL = $KonfigGlobalny
foreach ($z in "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL") {
    Remove-Item "Env:$z" -ErrorAction SilentlyContinue
}

function TGit {
    param([string]$Dir)
    $wynik = & git -C $Dir -c user.name=test -c user.email=test@test @args
    if ($LASTEXITCODE -ne 0) { throw "git $args (w $Dir) zakończony kodem $LASTEXITCODE" }
    return $wynik
}
function Zapisz($sciezka, $tresc) {
    New-Item -ItemType Directory -Force -Path (Split-Path $sciezka) | Out-Null
    [System.IO.File]::WriteAllText($sciezka, $tresc)
}

function Nowy-Swiat($nazwa) {
    $baza = Join-Path $Tmp $nazwa
    $origin = Join-Path $baza "origin.git"
    $wl = Join-Path $baza "wl"
    $biuro = Join-Path $baza "biuro"
    New-Item -ItemType Directory -Force -Path $baza | Out-Null
    & git init -q --bare -b main $origin
    & git clone -q $origin $wl 2>$null
    Zapisz (Join-Path $wl "app.py") "wersja = 1`n"
    Zapisz (Join-Path $wl "run_app.py") "start`n"
    Zapisz (Join-Path $wl ".gitignore") ".venv/`n"
    TGit $wl add . | Out-Null
    TGit $wl commit -q -m "Initial version from Anna computer" | Out-Null
    TGit $wl push -q origin HEAD:main | Out-Null
    & git clone -q $origin $biuro 2>$null
    return [pscustomobject]@{ Origin = $origin; Wl = $wl; Biuro = $biuro; Baza = $baza }
}

function Wypchnij-NowaWersje($s) {
    Zapisz (Join-Path $s.Wl "app.py") "wersja = 2`n"
    Zapisz (Join-Path $s.Wl "uruchom.pyw") "aktualizator`n"
    Zapisz (Join-Path $s.Wl "narzedzia\konfiguruj_komputer.ps1") "skrypt`n"
    TGit $s.Wl add . | Out-Null
    TGit $s.Wl commit -q -m "nowa wersja" | Out-Null
    TGit $s.Wl push -q origin HEAD:main | Out-Null
}

# to, co robi wklejane polecenie przed uruchomieniem skryptu
function Jak-Polecenie($s) {
    TGit $s.Biuro fetch -q origin | Out-Null
    TGit $s.Biuro checkout origin/main -- narzedzia/konfiguruj_komputer.ps1 | Out-Null
}

try {
    # --- parsowanie całego skryptu -------------------------------------------------
    $tokeny = $null; $bledyParsowania = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        (Resolve-Path $Skrypt).Path, [ref]$tokeny, [ref]$bledyParsowania) | Out-Null
    Sprawdz ($bledyParsowania.Count -eq 0) "skrypt parsuje się bez błędów ($($bledyParsowania | Out-String))"
    $bajty = [System.IO.File]::ReadAllBytes((Resolve-Path $Skrypt).Path)
    Sprawdz ($bajty[0] -eq 0xEF -and $bajty[1] -eq 0xBB -and $bajty[2] -eq 0xBF) "skrypt zapisany z BOM (Windows PowerShell 5.1)"

    # --- Wyciagnij-Haslo ---------------------------------------------------------
    Sprawdz ((Wyciagnij-Haslo @("protocol=https", "host=github.com", "username=x", "password=gho_ABC")) -eq "gho_ABC") "hasło z wyniku git credential fill"
    Sprawdz ($null -eq (Wyciagnij-Haslo @("protocol=https", "host=github.com"))) "brak hasła = null"
    Sprawdz ($null -eq (Wyciagnij-Haslo $null)) "pusty wynik = null"

    # --- Klasyfikuj-OdpowiedzGitHub ----------------------------------------------
    Sprawdz ((Klasyfikuj-OdpowiedzGitHub 201) -eq "dodany") "201 = dodany"
    Sprawdz ((Klasyfikuj-OdpowiedzGitHub 200) -eq "dodany") "200 = dodany"
    Sprawdz ((Klasyfikuj-OdpowiedzGitHub 422) -eq "juz-jest") "422 = już dodany"
    foreach ($k in 401, 403, 404) { Sprawdz ((Klasyfikuj-OdpowiedzGitHub $k) -eq "brak-uprawnien") "$k = brak uprawnień" }
    Sprawdz ((Klasyfikuj-OdpowiedzGitHub 500) -eq "blad") "500 = błąd"
    Sprawdz ((Klasyfikuj-OdpowiedzGitHub 0) -eq "blad") "0 (sieć) = błąd"

    # --- Dodaj-KluczWdrozeniowy (wysyłka wstrzyknięta) ---------------------------
    $wyslane = @{}
    $wynik = Dodaj-KluczWdrozeniowy -Token "gho_T" -KluczPubliczny "ssh-ed25519 AAAA test" -Tytul "Komputer ksiegowej (PC1)" -Wyslij {
        param($uri, $naglowki, $cialo)
        $wyslane.uri = $uri; $wyslane.naglowki = $naglowki; $wyslane.cialo = $cialo
        return 201
    }
    Sprawdz ($wynik -eq "dodany") "klucz dodany (201)"
    Sprawdz ($wyslane.uri -eq "https://api.github.com/repos/hotlineblings/ksef-symfonia-export/keys") "adres API klucza wdrożeniowego"
    $json = $wyslane.cialo | ConvertFrom-Json
    Sprawdz ($json.read_only -eq $true) "klucz tylko do odczytu (read_only = true)"
    Sprawdz ($json.key -eq "ssh-ed25519 AAAA test") "wysłany klucz publiczny"
    Sprawdz ($wyslane.naglowki.Authorization -eq "Bearer gho_T") "token w nagłówku"
    $wynik = Dodaj-KluczWdrozeniowy -Token "t" -KluczPubliczny "k" -Tytul "x" -Wyslij { return 422 }
    Sprawdz ($wynik -eq "juz-jest") "422 = klucz już był (ponowne uruchomienie)"
    $wynik = Dodaj-KluczWdrozeniowy -Token "t" -KluczPubliczny "k" -Tytul "x" -Wyslij { return 403 }
    Sprawdz ($wynik -eq "brak-uprawnien") "403 = ścieżka ręczna"
    $wynik = Dodaj-KluczWdrozeniowy -Token "t" -KluczPubliczny "k" -Tytul "x" -Wyslij { throw "brak sieci" }
    Sprawdz ($wynik -eq "blad") "wyjątek wysyłki = błąd (ścieżka ręczna), bez przerwania skryptu"

    # --- Pobierz-TokenGitHub -----------------------------------------------------
    Sprawdz ($null -eq (Pobierz-TokenGitHub)) "bez zapisanego logowania: null, bez pytania o hasło"
    $pomocnik = Join-Path $Tmp "pomocnik.sh"
    Zapisz $pomocnik "#!/bin/sh`necho username=hotlineblings`necho password=gho_ZAPISANY`n"
    & chmod +x $pomocnik 2>$null
    $konfigZTokenem = Join-Path $Tmp "gitconfig-token"
    Set-Content -Path $konfigZTokenem -Value ("[user]`n`tuseConfigOnly = true`n[credential]`n`thelper = " + ($pomocnik -replace '\\', '/'))
    $env:GIT_CONFIG_GLOBAL = $konfigZTokenem
    Sprawdz ((Pobierz-TokenGitHub) -eq "gho_ZAPISANY") "token z zapisanego logowania (git credential fill)"
    $env:GIT_CONFIG_GLOBAL = $KonfigGlobalny

    # --- Przygotuj-Repozytorium: komputer z lokalnymi zmianami ------------------
    $s = Nowy-Swiat "zmiany"
    Zapisz (Join-Path $s.Biuro "app.py") "wersja = 'poprawka asystenta AI'`n"          # zmiana śledzonego
    Zapisz (Join-Path $s.Biuro "notatka.txt") "lokalny commit`n"
    TGit $s.Biuro add notatka.txt | Out-Null
    TGit $s.Biuro commit -q -m "lokalny commit asystenta" | Out-Null                    # lokalny commit
    $lokalnyCommit = (TGit $s.Biuro rev-parse HEAD)
    Zapisz (Join-Path $s.Biuro "uruchom.pyw") "MOJ PLIK`n"                               # nieśledzony, koliduje
    Zapisz (Join-Path $s.Biuro "eksport\hmffk_2026-09.TXT") "dane ksiegowe`n"            # nieśledzony, nie koliduje
    Wypchnij-NowaWersje $s
    Jak-Polecenie $s
    $kopia = Join-Path $s.Baza "kopia"

    $raport = Przygotuj-Repozytorium -Repo $s.Biuro -KatalogKopii $kopia

    $head = TGit $s.Biuro rev-parse HEAD
    $om = TGit $s.Biuro rev-parse origin/main
    Sprawdz ($head -eq $om) "katalog aplikacji = origin/main"
    Sprawdz ((TGit $s.Biuro branch --show-current) -eq "main") "gałąź main"
    Sprawdz ((TGit $s.Biuro rev-parse --abbrev-ref "main@{upstream}") -eq "origin/main") "main śledzi origin/main"
    Sprawdz ((Get-Content (Join-Path $s.Biuro "app.py") -Raw) -eq "wersja = 2`n") "nowa wersja pliku aplikacji"
    Sprawdz ((Get-Content (Join-Path $s.Biuro "uruchom.pyw") -Raw) -eq "aktualizator`n") "plik z nowej wersji na miejscu kolidującego"
    Sprawdz ((Get-Content (Join-Path $kopia "uruchom.pyw") -Raw) -eq "MOJ PLIK`n") "kolidujący plik księgowej przeniesiony do kopii"
    Sprawdz (Test-Path (Join-Path $s.Biuro "eksport\hmffk_2026-09.TXT")) "pliki eksportu zostają na miejscu"
    $stash = TGit $s.Biuro stash list
    Sprawdz (@($stash).Count -eq 1) "zmiany asystenta odłożone do schowka"
    $wStash = TGit $s.Biuro stash show -p "stash@{0}"
    Sprawdz (($wStash -join "`n") -match "poprawka asystenta AI") "schowek zawiera zmianę asystenta"
    $galezie = @(TGit $s.Biuro branch --list "kopia/*" --format "%(refname:short) %(objectname)")
    Sprawdz ($galezie.Count -eq 1 -and $galezie[0].EndsWith($lokalnyCommit)) "lokalny commit zachowany na gałęzi kopia/..."
    $status = TGit $s.Biuro status --porcelain --untracked-files=no
    Sprawdz ([string]::IsNullOrWhiteSpace(($status -join ""))) "brak niezacommitowanych zmian po przygotowaniu"
    Sprawdz ($raport.Schowek -and $raport.GalazKopii -and @($raport.Przeniesione).Count -eq 1) "raport opisuje odłożone zmiany"
    $patch = Join-Path $kopia "zmiany-lokalne.patch"
    Sprawdz ((Test-Path $patch) -and ((Get-Content $patch -Raw) -match "poprawka asystenta AI")) "odłożone zmiany zapisane do pliku w kopii"
    $commity = Join-Path $kopia "lokalne-commity.txt"
    Sprawdz ((Test-Path $commity) -and ((Get-Content $commity -Raw) -match "lokalny commit asystenta")) "lokalne commity opisane w pliku w kopii"

    # drugie uruchomienie niczego nie psuje
    $raport2 = Przygotuj-Repozytorium -Repo $s.Biuro -KatalogKopii $kopia
    Sprawdz ((TGit $s.Biuro rev-parse HEAD) -eq $om) "drugie uruchomienie: nadal origin/main"
    Sprawdz (@(TGit $s.Biuro stash list).Count -eq 1) "drugie uruchomienie: bez nowego schowka"
    Sprawdz (-not $raport2.Schowek -and -not $raport2.GalazKopii -and @($raport2.Przeniesione).Count -eq 0) "drugie uruchomienie: nic do odłożenia"

    # --- Przygotuj-Repozytorium: czysty komputer ---------------------------------
    $c = Nowy-Swiat "czysty"
    Wypchnij-NowaWersje $c
    Jak-Polecenie $c
    $raportC = Przygotuj-Repozytorium -Repo $c.Biuro -KatalogKopii (Join-Path $c.Baza "kopia")
    Sprawdz ((TGit $c.Biuro rev-parse HEAD) -eq (TGit $c.Biuro rev-parse origin/main)) "czysty: katalog = origin/main"
    Sprawdz (@(TGit $c.Biuro stash list).Count -eq 0) "czysty: bez schowka"
    Sprawdz (@(TGit $c.Biuro branch --list "kopia/*").Count -eq 0) "czysty: bez gałęzi kopii"
    Sprawdz (-not (Test-Path (Join-Path $c.Baza "kopia"))) "czysty: bez katalogu kopii"
    Sprawdz (-not $raportC.Schowek) "czysty: raport bez schowka"

    # --- G: przyczyna błędu gita trafia do komunikatu (i dziennika) ---------------
    Scenariusz "G-blad" {
        $w = Nowy-Swiat "blad-g"
        $komunikat = ""
        try { G $w.Biuro rev-parse --verify nie-ma-takiej-galezi | Out-Null } catch { $komunikat = $_.Exception.Message }
        Sprawdz ($komunikat -match "fatal|Needed a single revision") "błąd gita zawiera jego komunikat ($komunikat)"
    }

    # --- ponowne uruchomienie po poprawce skryptu na main: bez fałszywego schowka --
    Scenariusz "poprawka-skryptu" {
        $w = Nowy-Swiat "poprawka"
        Wypchnij-NowaWersje $w
        TGit $w.Biuro fetch -q origin | Out-Null
        TGit $w.Biuro reset -q --hard origin/main | Out-Null        # stan po pierwszej konfiguracji
        Zapisz (Join-Path $w.Wl "narzedzia\konfiguruj_komputer.ps1") "skrypt v3`n"
        TGit $w.Wl commit -q -am "poprawka skryptu" | Out-Null
        TGit $w.Wl push -q origin HEAD:main | Out-Null
        Jak-Polecenie $w
        $r = Przygotuj-Repozytorium -Repo $w.Biuro -KatalogKopii (Join-Path $w.Baza "kopia")
        Sprawdz (-not $r.Schowek) 'poprawka skryptu: bez fałszywego komunikatu o odłożonych zmianach'
        Sprawdz (@(TGit $w.Biuro stash list).Count -eq 0) "poprawka skryptu: pusty schowek"
        Sprawdz ((Get-Content (Join-Path $w.Biuro "narzedzia\konfiguruj_komputer.ps1") -Raw) -eq "skrypt v3`n") "poprawka skryptu: nowa treść"
        Sprawdz ((TGit $w.Biuro rev-parse HEAD) -eq (TGit $w.Biuro rev-parse origin/main)) "poprawka skryptu: katalog = origin/main"
    }

    # --- nieśledzony plik różniący się od śledzonego tylko wielkością liter ---------
    Scenariusz "wielkosc-liter" {
        $w = Nowy-Swiat "wielkosc"
        Zapisz (Join-Path $w.Wl "NARZEDZIA.txt") "nowa`n"
        Wypchnij-NowaWersje $w
        Zapisz (Join-Path $w.Biuro "Narzedzia.txt") "moja`n"
        Jak-Polecenie $w
        $kopiaW = Join-Path $w.Baza "kopia"
        $r = Przygotuj-Repozytorium -Repo $w.Biuro -KatalogKopii $kopiaW
        Sprawdz (@($r.Przeniesione).Count -eq 1) "wielkość liter: kolizja wykryta"
        Sprawdz ((Get-Content (Join-Path $kopiaW "Narzedzia.txt") -Raw) -eq "moja`n") "wielkość liter: plik księgowej w kopii"
    }

    # --- lokalne commity na main, gdy HEAD stoi na innej gałęzi --------------------
    Scenariusz "inna-galaz" {
        $w = Nowy-Swiat "galezie"
        Zapisz (Join-Path $w.Biuro "m.txt") "m`n"
        TGit $w.Biuro add m.txt | Out-Null
        TGit $w.Biuro commit -q -m "commit na main" | Out-Null
        $m1 = TGit $w.Biuro rev-parse HEAD
        TGit $w.Biuro checkout -q -b x origin/main | Out-Null
        Zapisz (Join-Path $w.Biuro "x.txt") "x`n"
        TGit $w.Biuro add x.txt | Out-Null
        TGit $w.Biuro commit -q -m "commit na x" | Out-Null
        $x1 = TGit $w.Biuro rev-parse HEAD
        Wypchnij-NowaWersje $w
        Jak-Polecenie $w
        Przygotuj-Repozytorium -Repo $w.Biuro -KatalogKopii (Join-Path $w.Baza "kopia") | Out-Null
        Sprawdz (@(TGit $w.Biuro branch --list "kopia/*" --contains $m1).Count -gt 0) "commit z main zachowany na gałęzi kopii"
        Sprawdz (@(TGit $w.Biuro branch --list "kopia/*" --contains $x1).Count -gt 0) "commit z innej gałęzi zachowany na gałęzi kopii"
        Sprawdz ((TGit $w.Biuro branch --show-current) -eq "main") "inna gałąź: katalog na main"
    }

    # --- pozostałość po zabitym gicie (index.lock) --------------------------------
    Scenariusz "index-lock" {
        $w = Nowy-Swiat "lock"
        Wypchnij-NowaWersje $w
        Jak-Polecenie $w
        $lock = Join-Path $w.Biuro ".git\index.lock"
        Zapisz $lock ""
        (Get-Item $lock).LastWriteTime = (Get-Date).AddMinutes(-10)
        Przygotuj-Repozytorium -Repo $w.Biuro -KatalogKopii (Join-Path $w.Baza "kopia") | Out-Null
        Sprawdz (-not (Test-Path $lock)) "stary index.lock usunięty"
        Sprawdz ((TGit $w.Biuro rev-parse HEAD) -eq (TGit $w.Biuro rev-parse origin/main)) "po starym index.lock: katalog = origin/main"
        $w2 = Nowy-Swiat "lock2"
        Wypchnij-NowaWersje $w2
        Jak-Polecenie $w2
        Zapisz (Join-Path $w2.Biuro ".git\index.lock") ""
        $komunikat = ""
        try { Przygotuj-Repozytorium -Repo $w2.Biuro -KatalogKopii (Join-Path $w2.Baza "kopia") | Out-Null } catch { $komunikat = $_.Exception.Message }
        Sprawdz ($komunikat -match "(?i)zamknij") "świeży index.lock: prośba o zamknięcie innych programów ($komunikat)"
    }

    # --- wybór logowań do usunięcia: tylko GitHub w gicie / gh / GitHub Desktop ----
    $linie = @(
        "Currently stored credentials:", "",
        "    Target: LegacyGeneric:target=git:https://github.com", "    Type: Generic", "    User: hotlineblings",
        "    Element docelowy: LegacyGeneric:target=git:https://hotlineblings@github.com",
        "    Target: LegacyGeneric:target=gh:github.com:hotlineblings",
        "    Target: LegacyGeneric:target=GitHub - https://api.github.com/hotlineblings",
        "    Target: LegacyGeneric:target=vscodevscode.github-authentication/github.auth",
        "    Target: LegacyGeneric:target=git:https://gitlab.com",
        "    Użytkownik: hotlineblings@users.noreply.github.com",
        "    Target: LegacyGeneric:target=Microsoft_OC1:uri=a.nowicka@alfatron.pl"
    )
    $cele = @(Wybierz-CeleGitHub $linie)
    foreach ($oczekiwany in "git:https://github.com", "git:https://hotlineblings@github.com", "gh:github.com:hotlineblings", "GitHub - https://api.github.com/hotlineblings") {
        Sprawdz ($cele -contains $oczekiwany) "logowanie do usunięcia: $oczekiwany"
    }
    Sprawdz ($cele.Count -eq 4) "tylko 4 logowania do usunięcia (bez VS Code, GitLaba, Outlooka): $($cele -join ', ')"

    # --- potwierdzenie braku prawa zapisu ------------------------------------------
    Sprawdz ((Potwierdz-BrakZapisu 128 "ERROR: The key you are authenticating with has been marked as read only.") -eq "brak-zapisu") "odmowa read only = brak zapisu"
    Sprawdz ((Potwierdz-BrakZapisu 0 "To ssh://... * [new branch]") -eq "zapis") "push przeszedł = zapis"
    Sprawdz ((Potwierdz-BrakZapisu 128 "ssh: connect to host ssh.github.com port 443: Connection timed out") -eq "nieznane") "błąd sieci = nie wiadomo"

    # --- skrót na pulpicie: nie dublować ------------------------------------------
    $pul = @("C:\Users\Anna\Desktop", "C:\Users\Public\Desktop")
    Sprawdz (Czy-UtworzycSkrot $pul @()) "brak skrótu na pulpicie = utworzyć"
    Sprawdz (-not (Czy-UtworzycSkrot $pul @("C:\Users\Anna\Desktop\KSeF - Symfonia.lnk"))) "skrót z aktualizacją już jest = nie dublować"
    Sprawdz (Czy-UtworzycSkrot $pul @("C:\Users\Anna\AppData\Roaming\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar\KSeF.lnk")) "tylko na pasku zadań = utworzyć na pulpicie"
    Sprawdz (Czy-UtworzycSkrot $pul @("C:\Users\Anna\Desktop2\x.lnk")) "inny folder o podobnej nazwie = utworzyć"
}
catch {
    Write-Host "WYJĄTEK: $_"
    Write-Host $_.ScriptStackTrace
    $script:bledy++
}
finally {
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}

if ($script:bledy -gt 0) { Write-Host "NIEZALICZONE: $script:bledy"; exit 1 }
Write-Host "Wszystkie testy zaliczone."
exit 0
