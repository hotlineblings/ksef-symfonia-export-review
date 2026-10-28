# Jednorazowa konfiguracja komputera księgowej — JEDNO polecenie wklejone w PowerShell
# (Start -> prawy przycisk -> Terminal / Windows PowerShell, bez „administrator”):
#
#   cd "C:\Users\Anna\ksef-symfonia-export" -ErrorAction Stop; git fetch origin; if ($LASTEXITCODE -eq 0) { git checkout origin/main -- narzedzia/konfiguruj_komputer.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File narzedzia\konfiguruj_komputer.ps1 } else { Write-Host "Brak polaczenia z GitHubem - zadzwon do Bartka" }
#
# Co robi:
#   0. prosi o zamknięcie programu i ustawia katalog aplikacji na zaakceptowaną wersję
#      (origin/main); zmiany zrobione lokalnie NIE są kasowane: zmiany w plikach -> schowek
#      gita, lokalne commity -> gałąź kopia/przed-wdrozeniem-..., pliki zastępowane przez
#      nową wersję -> %APPDATA%\KsefSymfoniaExport\kopia-przed-wdrozeniem-...\ (tam też
#      zmiany-lokalne.patch i lokalne-commity.txt do wglądu właściciela);
#   1. sprawdza git i środowisko Pythona (.venv), w razie braku je tworzy;
#   2. tworzy klucz SSH TYLKO DO ODCZYTU tego jednego repozytorium i dodaje go w GitHubie
#      zapisanym na tym komputerze logowaniem właściciela (ostatnie użycie tego logowania);
#      gdy się nie da — kopiuje klucz do schowka do wysłania właścicielowi;
#   3. przełącza repozytorium na ten klucz (SSH przez port 443) i sprawdza, że klucz
#      nie ma prawa zapisu;
#   4. przestawia stare skróty (run_app.py) na start z automatyczną aktualizacją;
#      gdy na pulpicie nie ma skrótu z aktualizacją — tworzy „KSeF - Symfonia”;
#   5. usuwa z Menedżera poświadczeń Windows logowanie do GitHuba (konto właściciela;
#      tylko git / gh / GitHub Desktop — logowania edytora i innych usług zostają);
#      niepowodzenie = ostrzeżenie w dzienniku, nie blokada (właściciel i tak
#      unieważnia token na GitHubie).
#
# Przebieg: %APPDATA%\KsefSymfoniaExport\konfiguracja.log. Skrypt można uruchomić
# ponownie — kończy to, czego nie skończył. Szczegóły: AKTUALIZACJE.md

param([string]$Repo = "")

$RepoSsh = "ssh://git@ssh.github.com:443/hotlineblings/ksef-symfonia-export.git"
$ApiKluczy = "https://api.github.com/repos/hotlineblings/ksef-symfonia-export/keys"
$SciezkaSkryptu = "narzedzia/konfiguruj_komputer.ps1"
# stash i commity bez zależności od konfiguracji gita na tym komputerze
$Tozsamosc = @("-c", "user.name=KSeF konfiguracja", "-c", "user.email=konfiguracja@localhost")

function Krok($tekst) { Write-Host ""; Write-Host "== $tekst" -ForegroundColor Cyan }

# git w katalogu aplikacji: G <katalog> <argumenty gita...>; zwraca stdout, przy błędzie
# przerywa z komunikatem gita (trafia do dziennika). Celowo bez bloku param:
# PowerShell przypiąłby np. „-r” do parametru „-Repo”.
function G {
    $katalog = $args[0]
    $argumenty = @($args | Select-Object -Skip 1)
    $ErrorActionPreference = "Continue"   # PS 5.1: stderr gita nie może przerywać skryptu
    $wyjscie = & git -C $katalog @Tozsamosc @argumenty 2>&1
    $kod = $LASTEXITCODE
    $stdout = @($wyjscie | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } | ForEach-Object { "$_" })
    if ($kod -ne 0) {
        $stderr = @($wyjscie | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { "$_" })
        throw ("git " + ($argumenty -join " ") + " zakończył się błędem (kod $kod): " + (($stderr | Select-Object -Last 3) -join " | "))
    }
    return $stdout
}

# G bez przerywania: $true, gdy git zakończył się sukcesem
function G-Czy {
    $katalog = $args[0]
    $argumenty = @($args | Select-Object -Skip 1)
    $ErrorActionPreference = "Continue"
    & git -C $katalog @argumenty 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

# --- krok 0 ---------------------------------------------------------------------

function Czekaj-NaZamkniecieProgramu([string]$Repo) {
    if (-not (Get-Command Get-CimInstance -ErrorAction SilentlyContinue)) { return }
    $wzorzec = [regex]::Escape($Repo)
    for ($i = 0; $i -lt 20; $i++) {
        $dzialajace = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match $wzorzec -and $_.CommandLine -notmatch "(?i)pip|konfiguruj" })
        if ($dzialajace.Count -eq 0) { return }
        Read-Host "Program KSeF jest otwarty. Zamknij jego okno i naciśnij Enter"
    }
    throw "Program KSeF nadal jest otwarty."
}

# index.lock po zabitym gicie blokuje każdą operację; świeży należy do innego programu
function Usun-StaryIndexLock([string]$Repo) {
    $lock = Join-Path $Repo ".git\index.lock"
    if (-not (Test-Path $lock)) { return }
    if (((Get-Date) - (Get-Item $lock).LastWriteTime).TotalMinutes -ge 5) {
        Remove-Item $lock -Force
        return
    }
    throw ("Inny program korzysta teraz z katalogu aplikacji (plik .git\index.lock). " +
        "Zamknij edytor i asystenta AI, odczekaj 5 minut i wklej polecenie jeszcze raz.")
}

function Przygotuj-Repozytorium {
    param([string]$Repo, [string]$KatalogKopii)
    $raport = [pscustomobject]@{ Schowek = $false; GalazKopii = $null; Przeniesione = @() }
    Usun-StaryIndexLock $Repo
    $nowa = G $Repo rev-parse origin/main
    $znacznik = Get-Date -Format "yyyyMMdd-HHmmss"

    # plik skryptu pobrany przez wklejone polecenie (i cokolwiek innego) zdjęty z indeksu;
    # jeśli skrypt był już śledzony (ponowne uruchomienie po poprawce), wraca wersja z HEAD,
    # żeby nie udawał lokalnej zmiany — nowa przyjdzie z origin/main
    G $Repo reset -q | Out-Null
    if (G-Czy $Repo cat-file -e "HEAD:$SciezkaSkryptu") { G $Repo checkout -q HEAD -- $SciezkaSkryptu | Out-Null }

    # lokalne commity, których nie ma na GitHubie (bieżąca gałąź i main) -> gałęzie kopii
    $kopie = @()
    $refy = @("HEAD")
    if ((G-Czy $Repo show-ref --verify --quiet refs/heads/main) -and -not (G-Czy $Repo merge-base --is-ancestor main HEAD)) {
        $refy += "main"
    }
    foreach ($ref in $refy) {
        if ([int](G $Repo rev-list --count "origin/main..$ref") -eq 0) { continue }
        $nazwa = "kopia/przed-wdrozeniem-$znacznik" + $(if ($ref -eq "main") { "-main" } else { "" })
        G $Repo branch -f $nazwa $ref | Out-Null
        $kopie += $nazwa
    }
    if ($kopie.Count -gt 0) {
        $raport.GalazKopii = $kopie -join ", "
        New-Item -ItemType Directory -Force -Path $KatalogKopii | Out-Null
        $opis = foreach ($k in $kopie) { "== $k"; G $Repo log --stat "origin/main..$k"; "" }
        Set-Content -Path (Join-Path $KatalogKopii "lokalne-commity.txt") -Value $opis -Encoding UTF8
    }

    # pliki nieśledzone, które nowa wersja śledzi i które mają inną treść -> kopia;
    # klucze tabeli bez rozróżniania wielkości liter (jak system plików Windows),
    # wartość = ścieżka w nowej wersji
    $sledzone = @{}
    foreach ($p in @(G $Repo -c core.quotepath=false ls-tree -r --name-only origin/main)) { if ($p) { $sledzone[$p] = $p } }
    foreach ($p in @(G $Repo -c core.quotepath=false ls-files --others --exclude-standard)) {
        if (-not $p -or -not $sledzone.ContainsKey($p)) { continue }
        if ((G $Repo hash-object $p) -eq (G $Repo rev-parse ("origin/main:" + $sledzone[$p]))) { continue }
        $cel = Join-Path $KatalogKopii $p
        New-Item -ItemType Directory -Force -Path (Split-Path $cel) | Out-Null
        Move-Item -LiteralPath (Join-Path $Repo $p) -Destination $cel -Force
        $raport.Przeniesione += $p
    }

    # zmiany w plikach śledzonych -> schowek gita (+ kopia do wglądu)
    $zmiany = G $Repo status --porcelain --untracked-files=no
    if (-not [string]::IsNullOrWhiteSpace((@($zmiany) -join ""))) {
        G $Repo stash push -m ("przed wdrozeniem " + (Get-Date -Format "yyyy-MM-dd HH:mm")) | Out-Null
        $raport.Schowek = $true
        New-Item -ItemType Directory -Force -Path $KatalogKopii | Out-Null
        Set-Content -Path (Join-Path $KatalogKopii "zmiany-lokalne.patch") -Value (G $Repo stash show -p "stash@{0}") -Encoding UTF8
    }

    # katalog aplikacji = origin/main; gałąź main śledzi origin/main
    G $Repo checkout -q -f -B main origin/main | Out-Null
    G $Repo branch -q --set-upstream-to=origin/main main | Out-Null
    if ((G $Repo rev-parse HEAD) -ne $nowa) { throw "Katalog aplikacji nie jest na wersji z GitHuba." }
    return $raport
}

# --- krok 2 ---------------------------------------------------------------------

function Wyciagnij-Haslo([string[]]$Linie) {
    foreach ($l in @($Linie)) { if ($l -and $l.StartsWith("password=")) { return $l.Substring(9) } }
    return $null
}

# Token zapisany przez Git Credential Manager (logowanie właściciela), bez okien i pytań.
function Pobierz-TokenGitHub {
    $ErrorActionPreference = "Continue"
    $zmienne = "GCM_INTERACTIVE", "GIT_TERMINAL_PROMPT", "GIT_ASKPASS", "SSH_ASKPASS"
    $stare = @{}
    foreach ($z in $zmienne) { $stare[$z] = [Environment]::GetEnvironmentVariable($z) }
    try {
        $env:GCM_INTERACTIVE = "never"; $env:GIT_TERMINAL_PROMPT = "0"
        $env:GIT_ASKPASS = ""; $env:SSH_ASKPASS = ""
        $wynik = @("protocol=https", "host=github.com", "") | & git credential fill 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return Wyciagnij-Haslo $wynik
    }
    catch { return $null }
    finally {
        foreach ($z in $zmienne) { [Environment]::SetEnvironmentVariable($z, $stare[$z]) }
    }
}

function Klasyfikuj-OdpowiedzGitHub([int]$Kod) {
    if ($Kod -ge 200 -and $Kod -lt 300) { return "dodany" }
    if ($Kod -eq 422) { return "juz-jest" }          # ten klucz już dodany (albo użyty gdzie indziej)
    if ($Kod -in 401, 403, 404) { return "brak-uprawnien" }
    return "blad"
}

# POST do API GitHuba; zwraca kod HTTP (0 = brak odpowiedzi). Nagłówków nie loguje.
function Wyslij-DoGitHub($Uri, $Naglowki, $Cialo) {
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $odp = Invoke-WebRequest -Method Post -Uri $Uri -Headers $Naglowki -UseBasicParsing -ErrorAction Stop `
            -Body ([Text.Encoding]::UTF8.GetBytes($Cialo)) -ContentType "application/json; charset=utf-8"
        return [int]$odp.StatusCode
    }
    catch {
        $odpowiedz = $_.Exception.Response
        if ($odpowiedz) { try { return [int]$odpowiedz.StatusCode } catch { return 0 } }
        return 0
    }
}

function Dodaj-KluczWdrozeniowy {
    param([string]$Token, [string]$KluczPubliczny, [string]$Tytul, [scriptblock]$Wyslij = ${function:Wyslij-DoGitHub})
    $naglowki = @{
        Authorization          = "Bearer $Token"
        Accept                 = "application/vnd.github+json"
        "X-GitHub-Api-Version" = "2022-11-28"
        "User-Agent"           = "ksef-symfonia-konfiguracja"
    }
    $cialo = @{ title = $Tytul; key = $KluczPubliczny.Trim(); read_only = $true } | ConvertTo-Json -Compress
    try { $kod = & $Wyslij $ApiKluczy $naglowki $cialo } catch { return "blad" }
    return Klasyfikuj-OdpowiedzGitHub ([int]$kod)
}

function Znajdz-SshKeygen {
    $k = Get-Command ssh-keygen -ErrorAction SilentlyContinue
    if ($k) { return $k.Source }
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        $katalog = Split-Path $git.Source
        foreach ($w in 1..3) {
            $katalog = Split-Path $katalog
            if (-not $katalog) { break }
            $kandydat = Join-Path $katalog "usr\bin\ssh-keygen.exe"
            if (Test-Path $kandydat) { return $kandydat }
        }
    }
    throw "Nie znaleziono programu ssh-keygen (OpenSSH ani Git for Windows)."
}

# $true, gdy GitHub wpuszcza kluczem; przyczyna ostatniej porażki w $script:OstatniBladSsh
function Test-DostepKluczem([string]$Repo, [string]$SshCmd, [int]$Proby = 1) {
    $ErrorActionPreference = "Continue"
    $script:OstatniBladSsh = ""
    for ($i = 1; $i -le $Proby; $i++) {
        $wyjscie = & git -C $Repo -c "core.sshCommand=$SshCmd" ls-remote $RepoSsh refs/heads/main 2>&1 | ForEach-Object { "$_" }
        if ($LASTEXITCODE -eq 0) { return $true }
        $script:OstatniBladSsh = (@($wyjscie) | Select-Object -Last 3) -join " | "
        if ($i -lt $Proby) { Start-Sleep -Seconds 3 }
    }
    return $false
}

# wynik próbnego pushu: „brak-zapisu” tylko przy jawnej odmowie GitHuba
function Potwierdz-BrakZapisu([int]$Kod, [string]$Wyjscie) {
    if ($Kod -eq 0) { return "zapis" }
    if ($Wyjscie -match "(?i)read[ -]?only|denied to deploy key|not allowed to push|write access") { return "brak-zapisu" }
    return "nieznane"
}

# --- krok 4 ---------------------------------------------------------------------

function Czy-UtworzycSkrot([string[]]$Pulpity, [string[]]$SkrotyZAktualizacja = @()) {
    foreach ($s in @($SkrotyZAktualizacja)) {
        foreach ($d in @($Pulpity)) {
            if (-not $d -or -not $s) { continue }
            $baza = $d.TrimEnd('\', '/')
            if ($s.StartsWith($baza + '\', [StringComparison]::OrdinalIgnoreCase) -or
                $s.StartsWith($baza + '/', [StringComparison]::OrdinalIgnoreCase)) { return $false }
        }
    }
    return $true
}

function Przestaw-StareSkroty([string]$Repo, [string]$PythonW) {
    $ws = New-Object -ComObject WScript.Shell
    $miejsca = @(
        [Environment]::GetFolderPath("Desktop"),
        [Environment]::GetFolderPath("CommonDesktopDirectory"),
        (Join-Path $env:APPDATA "Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"),
        [Environment]::GetFolderPath("StartMenu")
    ) | Where-Object { $_ -and (Test-Path $_) }
    $wynik = [pscustomobject]@{ Przestawione = @(); Aktualne = @(); Nieudane = @(); Inne = @() }
    $skroty = Get-ChildItem $miejsca -Filter *.lnk -Recurse -ErrorAction SilentlyContinue | Sort-Object FullName -Unique
    foreach ($plik in $skroty) {
        try {
            $s = $ws.CreateShortcut($plik.FullName)
            $cel = $s.TargetPath + " " + $s.Arguments
            if ($cel -match "(?i)run_app\.py") {
                $s.TargetPath = $PythonW
                $s.Arguments = '"' + (Join-Path $Repo "uruchom.pyw") + '"'
                $s.WorkingDirectory = $Repo
                $s.Save()
                $wynik.Przestawione += $plik.FullName
            }
            elseif ($cel -match "(?i)uruchom\.pyw") { $wynik.Aktualne += $plik.FullName }
            elseif ($cel -match "(?i)ksef|symfonia") { $wynik.Inne += $plik.FullName }
        }
        catch { $wynik.Nieudane += $plik.FullName }
    }
    return $wynik
}

# --- krok 5 ---------------------------------------------------------------------

# Z wyniku `cmdkey /list` wybiera logowania do GitHuba zapisane przez git (GCM), gh i
# GitHub Desktop. Etykiety zależą od języka Windows („Target:”, „Element docelowy:”),
# więc liczy się tylko tekst po „target=”; logowania edytora (VS Code) zostają.
function Wybierz-CeleGitHub([string[]]$Linie) {
    $cele = @()
    foreach ($linia in @($Linie)) {
        if ($linia -match "target=(.+?)\s*$") {
            $cel = $Matches[1]   # zapamiętać od razu: kolejne -match nadpisuje $Matches
            if ($cel -match "^(?i)(git:https?://([^@/]+@)?github\.com/?|gh:github\.com(:.*)?|GitHub - https://api\.github\.com(/.*)?)$") {
                $cele += $cel
            }
        }
    }
    return $cele | Select-Object -Unique
}

function Cele-GitHub { return Wybierz-CeleGitHub (cmdkey /list) }

# zwraca listę ostrzeżeń (pusta = logowanie usunięte)
function Usun-LogowaniaGitHub {
    $ErrorActionPreference = "Continue"
    $ostrzezenia = @()
    foreach ($cel in (Cele-GitHub)) {
        Write-Host "Usuwam zapisane logowanie: $cel"
        cmdkey "/delete:$cel" | Out-Null
        if ((Cele-GitHub) -contains $cel) { cmdkey "/delete:LegacyGeneric:target=$cel" | Out-Null }
    }
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        $null | gh auth logout --hostname github.com 2>&1 | Out-Null
    }
    $zostalo = @(Cele-GitHub)
    if ($zostalo.Count -gt 0) { $ostrzezenia += "nadal zapisane logowanie do GitHuba: " + ($zostalo -join ", ") }
    if (Pobierz-TokenGitHub) { $ostrzezenia += "git nadal zna logowanie do GitHuba (inny pomocnik poświadczeń)" }
    return $ostrzezenia
}

# --- przebieg ---------------------------------------------------------------------

function Test-Administrator {
    try {
        $ja = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        return $ja.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    }
    catch { return $false }
}

function Wykonaj-Konfiguracje([string]$Repo, [string]$Dane) {
    $Klucz = Join-Path $env:USERPROFILE ".ssh\ksef_symfonia_deploy"
    Set-Location $Repo
    if (-not $Repo.StartsWith($env:USERPROFILE, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Polecenie trzeba uruchomić na koncie Windows, na którym pracuje program (katalog $Repo, konto $env:USERPROFILE)."
    }
    if (Test-Administrator) {
        Write-Host "Uwaga: okno działa jako administrator. Następnym razem wybierz Terminal bez dopisku (administrator)." -ForegroundColor Yellow
    }

    Krok "0/5 Wersja programu z GitHuba"
    Czekaj-NaZamkniecieProgramu $Repo
    $kopia = Join-Path $Dane ("kopia-przed-wdrozeniem-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    $r = Przygotuj-Repozytorium -Repo $Repo -KatalogKopii $kopia
    if ($r.Schowek) { Write-Host "Zmiany zrobione lokalnie w plikach programu odłożone do schowka gita i do: $kopia\zmiany-lokalne.patch" }
    if ($r.GalazKopii) { Write-Host "Lokalne commity zachowane na gałęzi $($r.GalazKopii) (opis: $kopia\lokalne-commity.txt)." }
    if (@($r.Przeniesione).Count -gt 0) {
        Write-Host "Pliki zastąpione przez nową wersję przeniesione do: $kopia"
        $r.Przeniesione | Out-Host
    }
    Write-Host ("Wersja programu: " + (G $Repo log -1 "--format=%h z %cd" "--date=format:%d.%m.%Y"))

    Krok "1/5 Git i Python"
    git --version | Out-Host
    $Python = Join-Path $Repo ".venv\Scripts\python.exe"
    $PythonW = Join-Path $Repo ".venv\Scripts\pythonw.exe"
    if (-not (Test-Path $Python)) {
        Write-Host "Brak .venv - tworzę środowisko i instaluję biblioteki (kilka minut)..."
        if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
        if ($LASTEXITCODE -ne 0) { throw "Nie udało się utworzyć środowiska .venv (czy Python jest zainstalowany?)." }
        & $Python -m pip install -q -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw "Instalacja bibliotek nie powiodła się." }
    }
    & $Python -c "import PySide6, httpx, lxml, cryptography; print('Biblioteki OK')"
    if ($LASTEXITCODE -ne 0) { throw "Brakuje bibliotek w .venv - uruchom: .venv\Scripts\python.exe -m pip install -r requirements.txt" }
    # repozytorium należy do innego konta Windows (np. sklonowane jako administrator)?
    $bezpieczny = $Repo -replace '\\', '/'
    if (@(git config --global --get-all safe.directory) -notcontains $bezpieczny) {
        git config --global --add safe.directory $bezpieczny
    }

    Krok "2/5 Klucz tylko do odczytu"
    New-Item -ItemType Directory -Force -Path (Split-Path $Klucz) | Out-Null
    if (-not (Test-Path $Klucz)) {
        $keygen = Znajdz-SshKeygen
        # '""' = puste hasło klucza w Windows PowerShell 5.1 (uruchamiaj przez "powershell", nie "pwsh")
        & $keygen -t ed25519 -N '""' -f $Klucz -C "ksef-symfonia-export tylko odczyt ($env:COMPUTERNAME)"
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$Klucz.pub")) { throw "Nie udało się utworzyć klucza SSH." }
    }
    $pub = (Get-Content "$Klucz.pub" -Raw).Trim()
    $SshCmd = "ssh -i '" + ($Klucz -replace '\\', '/') + "' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=15"
    Write-Host "Sprawdzam połączenie z GitHubem..."
    if (Test-DostepKluczem $Repo $SshCmd) {
        Write-Host "Klucz jest już dodany w GitHubie."
    }
    else {
        $token = Pobierz-TokenGitHub
        if ($token) {
            $stan = Dodaj-KluczWdrozeniowy -Token $token -KluczPubliczny $pub -Tytul "Komputer ksiegowej ($env:COMPUTERNAME)"
        }
        else { $stan = "brak-logowania" }
        $token = $null
        if ($stan -in "dodany", "juz-jest") {
            Write-Host "Klucz dodany w GitHubie (tylko do odczytu)."
        }
        else {
            Write-Host ""
            Write-Host "Nie udało się dodać klucza automatycznie ($stan)." -ForegroundColor Yellow
            try { Set-Clipboard -Value $pub; Write-Host "Klucz jest skopiowany - wklej go w wiadomości do Bartka (mail / SMS)." -ForegroundColor Yellow } catch { }
            Write-Host $pub
            Write-Host "(Bartek: GitHub -> repozytorium -> Settings -> Deploy keys -> Add deploy key, BEZ 'Allow write access')"
            Read-Host "Gdy Bartek potwierdzi, że dodał klucz, naciśnij Enter"
        }
        Write-Host "Czekam, aż GitHub przyjmie klucz (może to potrwać do 3 minut, nie zamykaj okna)..."
        if (-not (Test-DostepKluczem $Repo $SshCmd -Proby 10)) {
            throw "GitHub nie wpuszcza nowego klucza: $script:OstatniBladSsh"
        }
    }

    Krok "3/5 Repozytorium na kluczu tylko do odczytu"
    G $Repo config core.sshCommand $SshCmd | Out-Null
    if (@(G $Repo remote) -contains "origin") { G $Repo remote set-url origin $RepoSsh | Out-Null }
    else { G $Repo remote add origin $RepoSsh | Out-Null }
    G $Repo fetch -q origin main | Out-Null
    Write-Host "Pobieranie z GitHuba kluczem tylko do odczytu działa."
    # klucz NIE może mieć prawa zapisu: próbny push (bez wysyłania danych) wprost na adres
    # SSH (nie przez origin — pushurl/pushInsteadOf mogłyby pchnąć przez HTTPS)
    $ErrorActionPreference = "Continue"
    $wyjsciePush = & git -C $Repo -c "core.sshCommand=$SshCmd" push --dry-run $RepoSsh HEAD:refs/heads/test-bez-zapisu 2>&1 | ForEach-Object { "$_" }
    $kodPush = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    switch (Potwierdz-BrakZapisu $kodPush (@($wyjsciePush) -join "`n")) {
        "zapis" { throw "UWAGA: klucz ma prawo ZAPISU do repozytorium. Bartek: usuń go w GitHub (Deploy keys) i dodaj ponownie BEZ 'Allow write access'." }
        "nieznane" { throw ("Nie udało się potwierdzić, że klucz nie ma prawa zapisu: " + ((@($wyjsciePush) | Select-Object -Last 2) -join " | ")) }
    }
    Write-Host "Potwierdzone: klucz nie ma prawa zapisu."

    Krok "4/5 Skróty do programu"
    $s = Przestaw-StareSkroty $Repo $PythonW
    foreach ($p in $s.Przestawione) { Write-Host "Przestawiony na start z aktualizacją: $p" }
    foreach ($p in $s.Nieudane) { Write-Host "Nie udało się sprawdzić / przestawić skrótu: $p" -ForegroundColor Yellow }
    $pulpity = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("CommonDesktopDirectory")) | Where-Object { $_ }
    if (Czy-UtworzycSkrot $pulpity (@($s.Przestawione) + @($s.Aktualne))) {
        $Skrot = Join-Path ([Environment]::GetFolderPath("Desktop")) "KSeF - Symfonia.lnk"
        $ws = New-Object -ComObject WScript.Shell
        $lnk = $ws.CreateShortcut($Skrot)
        $lnk.TargetPath = $PythonW
        $lnk.Arguments = '"' + (Join-Path $Repo "uruchom.pyw") + '"'
        $lnk.WorkingDirectory = $Repo
        $lnk.Description = "Eksport faktur KSeF do Symfonii (z automatyczną aktualizacją)"
        $lnk.Save()
        Write-Host "Utworzono skrót na pulpicie: $Skrot"
    }
    if ($s.Inne) {
        Write-Host "Inne skróty z nazwą KSeF/Symfonia (nie uruchamiają aktualizacji - sprawdź je):" -ForegroundColor Yellow
        $s.Inne | Out-Host
    }

    Krok "5/5 Usunięcie logowania do GitHuba z konta właściciela"
    $ostrzezenia = @(Usun-LogowaniaGitHub)
    if ($ostrzezenia.Count -eq 0) { Write-Host "Brak zapisanych logowań do GitHuba na tym komputerze." }
    foreach ($o in $ostrzezenia) { Write-Host "Dla Bartka: $o" -ForegroundColor Yellow }
}

function Main {
    $ErrorActionPreference = "Stop"
    try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
    if (-not $Repo) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
    $Dane = Join-Path $env:APPDATA "KsefSymfoniaExport"
    New-Item -ItemType Directory -Force -Path $Dane | Out-Null
    $Log = Join-Path $Dane "konfiguracja.log"
    try { Start-Transcript -Path $Log -Append | Out-Null } catch { }
    $kod = 0
    try {
        Wykonaj-Konfiguracje -Repo $Repo -Dane $Dane
        Write-Host ""
        Write-Host 'Gotowe. Program uruchamiaj jak dotąd ikoną (albo skrótem "KSeF - Symfonia" na pulpicie).' -ForegroundColor Green
        Write-Host "Przy każdym starcie sam pobierze zaakceptowaną wersję; numer wersji widać w tytule okna." -ForegroundColor Green
    }
    catch {
        Write-Host ""
        Write-Host "Konfiguracja nie została dokończona: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Dane księgowe (folder $Dane) są nietknięte. Wklej polecenie jeszcze raz;" -ForegroundColor Red
        Write-Host "jeśli błąd się powtórzy, zadzwoń do Bartka i wyślij mu plik: $Log" -ForegroundColor Red
        $kod = 1
    }
    finally { try { Stop-Transcript | Out-Null } catch { } }
    exit $kod
}

# dot-source (testy) ładuje same funkcje
if ($MyInvocation.InvocationName -ne '.') { Main }
