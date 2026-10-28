# PLAN — Aplikacja eksportu faktur KSeF → Symfonia FK (AMS .TXT)

> **Handoff dla nowej sesji budowy.** Cały research + weryfikacja empiryczna zrobione 2026-07-08. Ten plik + `reference/` + `samples/` = komplet do startu implementacji. NIE trzeba powtarzać researchu.
> Odbiorca: agent/dev budujący aplikację. Format gęsty, konkretny.

---

## 0. Cel (jedno zdanie)

Jedna aplikacja desktopowa: dla **4 profili** (4 firmy, 4 NIP-y, 4 tokeny KSeF) pobiera z **KSeF (produkcja)** faktury **sprzedażowe i/lub kosztowe** w wybranym **zakresie dat**, konwertuje je do natywnego formatu importu **Symfonia FK (AMS `.TXT`)** i zapisuje pogrupowane w **podfoldery per profil** w konfigurowalnym folderze docelowym. Import do Symfonii = ręczny (człowiek klika „Użyj" w Symfonii i księguje z bufora).

## 1. Status — co JUŻ udowodnione empirycznie (nie hipotezy)

- ✅ **Strona Symfonii DZIAŁA end-to-end.** Symfonia FK „50" (desktop, wersja **2026.3 / build 26.30**) ma natywny mechanizm „Katalogi wymiany danych" (menu: Konfiguracja współpracy). Import naszego pliku AMS `.TXT` przez szablon `SKsefInvoiceImport.AMS` **doszedł do zapisu do tabeli bufora `fk_BUF_DOKUM0`** (SQL Server). Jedyny błąd był naszymi danymi testowymi (patrz §6, gotcha eArchiveId), nie mechanizmem. WebAPI, KSeF Plus, licencja WAF — **NIEpotrzebne** dla tej ścieżki.
- ✅ **Kontrakt KSeF 2.0 API zdobyty z żywego OpenAPI** (v2.6.1, `api-test.ksef.mf.gov.pl/docs/v2/openapi.json`, 2026-07-08). Pełny przepis: `reference/ksef2-recipe.md`.
- ✅ **Logowanie samym tokenem KSeF działa** (bez certyfikatu klienckiego / XAdES w kodzie) → model „4 profile = 4 tokeny" jest wykonalny.
- ✅ **Format AMS w pełni rozgryziony** — mamy oryginalny XSLT Symfonii (`SKsefInvoiceImport.xslt`) i szablon (`SKsefInvoiceImport.AMS`), przeanalizowane linia po linii; wiemy dokładnie jaki XML wchodzi i jaki AMS wychodzi.

## 2. Wymagania funkcjonalne (od użytkownika, 2026-07-08)

1. **4 profile**, każdy: nazwa, NIP, własny token KSeF, własny podfolder docelowy. Eksport uruchamiany **osobno per profil**.
2. **Wybór zakresu dat** pobieranych faktur. (⚠️ KSeF: max 3 miesiące na 1 zapytanie — dłuższy zakres dzielić na okna, patrz §5.)
3. **Checkboxy: sprzedaż / koszt** — można wybrać jedną albo obie naraz.
4. **Konfigurowalny folder docelowy**; wyniki **grupowane w podfoldery per profil**.
5. Wyjście = pliki **AMS `.TXT`** (nasza sprawdzona konwersja).

## 3. Decyzje ZABLOKOWANE (nie re-otwierać bez powodu)

| Decyzja | Wybór | Uwaga |
|---|---|---|
| Typ aplikacji | **Natywne okno desktopowe** | Musi działać na Windows (tam Symfonia). Rekomendacja stacku: **Python + PySide6** (ładny natywny UI, pakowalny do `.exe` PyInstaller). |
| Przechowywanie tokenów | **Zaszyfrowany plik konfiguracyjny** | Master password przy starcie. Krypto: `cryptography` (Fernet + klucz z hasła przez scrypt/PBKDF2). Tokeny NIE plaintextem. |
| Środowisko KSeF | **PRODUKCJA (4 NIP-y)** | Base URL prod (§5). Rekomendacja: pierwszy przebieg zweryfikować **jednym** tokenem, potem skalować do 4. |

## 4. Architektura / moduły

```
ksef-symfonia-export/
├─ app/
│  ├─ config.py        # model profili + folder docelowy + env; zaszyfrowany zapis/odczyt (master password)
│  ├─ ksef_client.py   # auth (challenge→encrypt→ksef-token→poll→redeem→access/refresh) + query metadata + download
│  ├─ fa_mapper.py     # RAW MF FA(3) XML  ->  ksefInvoice XML (struktura, którą je SKsefInvoiceImport.xslt)  [RDZEŃ RYZYKA — §7]
│  ├─ ams_transform.py # ksefInvoice XML  ->  (XSLT Symfonii)  ->  AMS tekst  ->  CP1250+CRLF .TXT
│  ├─ exporter.py      # orkiestracja per profil: pull(sprzedaż/koszt) -> map -> transform -> zapis do <target>/<profil>/
│  ├─ ledger.py        # dedup: rejestr pobranych numerów KSeF per profil (żeby nie dublować importu)
│  └─ gui.py           # PySide6: lista profili, per profil: date range + checkboxy + „Eksportuj", pole folderu docelowego, log
├─ reference/          # szablony Symfonii + przepis KSeF (patrz §11)
├─ samples/            # działające próbki ksefInvoice.xml + wygenerowane .TXT
└─ tests/
```

**Przepływ (per profil, per typ):**
```
token --auth--> accessToken
  --> POST /invoices/query/metadata (subjectType, dateRange) --> lista {ksefNumber, formCode,...}
  --> (dedup vs ledger) --> pobierz XML FA (GET /invoices/ksef/{ksefNumber}  LUB export wsadowy)
  --> fa_mapper: FA(3) -> ksefInvoice (invoiceBound = INTERNAL dla sprzedaży / EXTERNAL dla kosztu)
  --> ams_transform: XSLT -> AMS -> .TXT (CP1250, CRLF)
  --> zapis <target>/<profil>/  + wpis do ledger
```

## 5. KSeF 2.0 API — kontrakt (skrót; PEŁNE w `reference/ksef2-recipe.md`)

- **Base URL:** PROD = `https://api.ksef.mf.gov.pl/v2` (⚠️ **[EMP]** potwierdzić w 1. uruchomieniu — smoke test `GET /security/public-key-certificates`; TEST = `https://api-test.ksef.mf.gov.pl/v2`). Ścieżka to `/v2`, **NIE** `/api/v2`.
- **Auth (token, bez certyfikatu klienta):**
  1. `GET /v2/security/public-key-certificates` (bez auth) → weź wpis `usage` zawierający `KsefTokenEncryption` → `certificate` (DER Base64) + `publicKeyId`.
  2. `POST /v2/auth/challenge` (bez body) → `{challenge, timestampMs}` (ważne 10 min).
  3. `plaintext = "{token}|{timestampMs}"`; `encryptedToken = Base64( RSA-OAEP(SHA-256, MGF1-SHA256, plaintext, pubKeyMF) )`.
  4. `POST /v2/auth/ksef-token` body `{challenge, contextIdentifier:{type:"Nip",value:NIP}, encryptedToken, publicKeyId}` → 202 `{referenceNumber, authenticationToken(JWT)}`.
  5. `GET /v2/auth/{referenceNumber}` `Bearer authenticationToken` → poll aż `status.code==200` (100=w toku, 415=brak uprawnień, 450=błąd tokenu/krypto/czasu).
  6. `POST /v2/auth/token/redeem` `Bearer authenticationToken` (bez body) → `{accessToken, refreshToken}` (**jednorazowe**).
  7. Zasoby: `Bearer accessToken` (~kilkanaście min TTL). Odświeżanie: `POST /v2/auth/token/refresh` `Bearer refreshToken` (ważny 7 dni).
- **Uprawnienie:** token MUSI mieć **`InvoiceRead`** (inaczej krok 5 = 415). Generowany w Aplikacji Podatnika KSeF (prod), zakładka Tokeny → Generuj → zaznacz przeglądanie faktur. Token pokazywany JEDEN RAZ.
- **Query faktur:** `POST /v2/invoices/query/metadata?pageOffset=0&pageSize=100` `Bearer accessToken`, body:
  ```json
  {"subjectType":"Subject1","dateRange":{"dateType":"Issue","from":"2026-04-01T00:00:00+00:00","to":"2026-06-30T23:59:59+00:00"}}
  ```
  - `subjectType`: **`Subject1` = sprzedaż** (jesteś sprzedawcą) · **`Subject2` = koszt** (jesteś nabywcą).
  - `dateRange.from` wymagane; **max 3 miesiące**; `dateType` Issue|Invoicing|PermanentStorage.
  - Paginacja: `hasMore` / `isTruncated` / `pageOffset`; przyrostowo `dateType=PermanentStorage` + `permanentStorageHwmDate`.
  - Odpowiedź: `invoices[]` z `ksefNumber`, `formCode{systemCode:"FA (3)",...}`, `seller`, `buyer`, kwoty, `invoiceType`.
  - **Rate limit:** 8/s, 16/min, 20/h.
- **Download 1 faktury:** `GET /v2/invoices/ksef/{ksefNumber}` `Bearer accessToken` → 200 `application/xml` (surowy FA).
  - **Rate limit tylko 64/h** → do wolumenu użyć **eksportu wsadowego** `POST /v2/invoices/exports` (paczka ZIP szyfrowana AES; klucz AES szyfrowany kluczem publicznym MF) → `GET /v2/invoices/exports/{ref}`. Dla małych wolumenów single-GET wystarcza; zaimplementować backoff + fallback na export.
  - Błąd `21165` = faktura przetworzona ale jeszcze niedostępna → retry z backoff.
- **Schemat faktury:** PROD zwraca **FA(3)** (`systemCode "FA (3)"`, `schemaVersion "1-0E"`). Root: `Faktura → Naglowek → Podmiot1(sprzedawca) → Podmiot2(nabywca) → Podmiot3? → Fa`. XSD: `github.com/CIRFMF/ksef-api/.../schemat_FA(3)_v1-0E.xsd`.
- **Gotowce:** oficjalne SDK MF `CIRFMF/ksef-client-csharp` i `ksef-client-java` (zdejmują ryzyko RSA-OAEP/challenge/redeem). Python SDK brak → implementujemy auth sami (krypto standardowe: `cryptography`). Warto zerknąć do klienta C#/Java jako wzorca szyfrowania.

## 6. Konwersja do Symfonii — GROUND TRUTH (rozgryzione z oryginalnych plików)

**Pliki (w `reference/symfonia-szablony/`):**
- `SKsefInvoiceImport.xslt` — oryginalny transform Symfonii: `ksefInvoice` XML → tekst AMS. **To on liczy dekret (Zapis WN/MA) i rejestr VAT — reużywamy go, nie piszemy księgowania sami.**
- `SKsefInvoiceImport.AMS` — szablon, który AMS tekst wczytuje do bufora FK. Wymaga `Wersja szablonu = 3.1` (nasz XSLT ją emituje — zgodne z 2026.3). `#DANEKH "2"` = kontrahent dopasowywany po NIP do kartoteki, reszta z pliku.
- `SKsefInvoiceImport_V2.AMS` — nowszy wariant „format FK 3.0" (bogatszy: pre-liczony rejestr, JPK_V7, płatności, VAT kasowy). Fallback jeśli 2026.3 zażąda tej wersji.

**Pipeline (potwierdzony):** `ksefInvoice.xml` → `xsltproc/lxml SKsefInvoiceImport.xslt` → tekst AMS → zapis jako `.TXT` w **Windows-1250 + CRLF** → import w Symfonii profilem z Szablon=`SKsefInvoiceImport.AMS`, **pole XSLT puste** (patrz gotcha).

**Format AMS wyjściowy (co produkuje XSLT) — sekcje:** `INFO{Wersja szablonu=3.1}`, `Dokument{ rodzajDok(zakupu/sprzedaży), wzorzec(2=zakup/3=sprzedaż), kurs, KsefNumber, KSeFDate, KSeFStatus, eArchiveId, Numer, DataWystawienia, DataDostawy, KodWaluty, RodzajFaktury, mppFlags, Zapis{strona=WN/MA,kwota}×3, Kontrahent{NIP,PelnaNazwa,...}, Rejestr{StawkaPodatku,WartoscNetto,KwotaVat,KwotaVatKurs}, dokument korygowany? }`.

**Dekret liczony przez XSLT:** koszt/EXTERNAL PLN → `WN netto, MA brutto, WN vat`; sprzedaż/INTERNAL PLN → `WN brutto, MA netto, MA vat`.

**⚠️ GOTCHY (nauczone empirycznie — KRYTYCZNE):**
1. **`eArchiveId` MUSI być poprawnym GUID-em** (kolumna `uniqueidentifier` w SQL). Zły string → `Native:8114 "Błąd konwersji varchar na uniqueidentifier"`. Generować GUID per dokument (albo ustalić czy może być NULL — patrz §8).
2. **Pole „XSLT" w profilu Symfonii jest WYSZARZONE** — nie da się podpiąć XSLT w ręcznym profilu. Dlatego **transform robimy PO NASZEJ stronie** (aplikacja odpala XSLT) i podajemy Symfonii **gotowy AMS `.TXT`**; w profilu ustawiamy tylko Szablon.
3. **Kodowanie: Windows-1250 + końce linii CRLF.** Inaczej polskie znaki się sypią.
4. `SKsefInvoiceImport.xslt` czyta strukturę `ksefInvoice/invoice` (FA(1)) LUB `ksefInvoice/invoiceV2` (FA(2)). **Używamy gałęzi `invoiceV2`** (poprawna logika wyboru kontrahenta wg `invoiceBound`).

**Struktura wejściowa `ksefInvoice` (kontrakt dla `fa_mapper` — dokładne ścieżki, które czyta XSLT, gałąź invoiceV2):**
```
ksefInvoice
  invoiceBound            # "EXTERNAL"=koszt(Subject2) | "INTERNAL"=sprzedaż(Subject1)
  KsefNumber             # 35-zn. numer KSeF
  KSeFDate               # -> "data wpływu" = data odliczenia VAT  (ROZWIĄZUJE pułapkę daty VAT natywnie)
  KSeFStatus             # notsent|sending|pending|approved|rejected|error -> kody statusu
  eArchiveId             # GUID (patrz gotcha 1)
  invoiceV2
    Naglowek { Numer, DataWystawienia, DataDostawy, KodWaluty, RodzajFaktury, OkresFaktury?{DataOd,DataDo} }
    Sprzedawca { DaneIdentyfikacyjne{NIP,PelnaNazwa,BrakId}, Adres{KodKraju,AdresL1,AdresL2}, DaneKontaktowe/TDaneKontaktoweV2{Email,Telefon} }
    Nabywca    { ...jw... }
    PodsumowanieVat / PodsumowanieVat[] { StawkaPodatku, WartoscNetto, KwotaVat, KwotaVatKurs }
    Wiersze / WierszV2[] { KursWaluty }        # [1] użyty jako kurs
    Adnotacje { PodzielonaPlatnosc }           # -> mppFlags
    Korekta/FaKorygowana/DaneFaKorygowanejV2 { NrFaKorygowanej, DataWystFaKorygowanej, NrKSeFFaKorygowanej }  # tylko korekty
```
Wybór kontrahenta w XSLT: EXTERNAL → Kontrahent=`Sprzedawca`; INTERNAL → Kontrahent=`Nabywca`.

Działające próbki: `samples/ksefInvoice-koszt-EXTERNAL.xml` + `ksefInvoice-sprzedaz-INTERNAL.xml` (+ wygenerowane `.TXT`). To dokładnie ten format — punkt odniesienia dla `fa_mapper`.

## 7. RDZEŃ RYZYKA — mapowanie FA(3) → ksefInvoice (`fa_mapper`)

**Problem:** `SKsefInvoiceImport.xslt` był pisany pod strukturę `invoiceV2` ≈ **FA(2)** (Symfonia-znormalizowaną). PROD KSeF zwraca **surowy FA(3) MF** (`Faktura/Podmiot1/Podmiot2/Fa/...`). Nazwy pól i zagnieżdżenia się różnią. To jest **główna praca inżynierska i główne ryzyko**.

**Dwie drogi (decyzja w sesji budowy):**
- **(A) FA(3) → `ksefInvoice/invoiceV2` → XSLT Symfonii → AMS.** Reużywa przetestowaną logikę dekretu/rejestru Symfonii. Trzeba wtłoczyć dane FA(3) w kształt invoiceV2 (mapowanie pól: `Podmiot1`→`Sprzedawca`, `Podmiot2`→`Nabywca`, `DaneIdentyfikacyjne/NIP`, `Adres/AdresL1/AdresL2`, `Fa/P_*`→`Naglowek/*` i `PodsumowanieVat`). **Rekomendacja domyślna.**
- **(B) FA(3) → AMS tekst BEZPOŚREDNIO** (pomijając XSLT), bo znamy dokładny format AMS (§6). Pełna kontrola, ale sami odtwarzamy logikę dekretu. Fallback / do rozważenia jeśli invoiceV2 nie mieści pól FA(3).

**Kroki dla sesji budowy:**
1. Pobrać XSD FA(3) (`CIRFMF/ksef-api`) + zdobyć 1 realną fakturę FA(3) (po 1. auth) jako próbkę.
2. Zbudować tabelę mapowania FA(3) → invoiceV2 (pola: NIP, nazwa, adres, daty, numer, waluta, pozycje/PodsumowanieVat wg stawek, korekta, MPP).
3. `invoiceBound`: wynik query `Subject1`→INTERNAL, `Subject2`→EXTERNAL.
4. Test: FA(3) próbka → fa_mapper → ams_transform → `.TXT`, porównać z `samples/`, potem import w Symfonii (spike jak 2026-07-08).

## 8. Ryzyka i [EMP] do potwierdzenia w 1. uruchomieniu

- **PROD base URL** `api.ksef.mf.gov.pl/v2` — potwierdzić smoke testem klucza publicznego.
- **Download 64/h** — dla realnego wolumenu 4 firm zaprojektować eksport wsadowy (`/invoices/exports`), nie pętlę single-GET.
- **`eArchiveId` = NULL?** — sprawdzić, czy kolumna dopuszcza NULL (wtedy nie musimy zmyślać GUID-a); jeśli nie → generować `uuid4` per dokument.
- **FA(3) vs invoiceV2** — czy wszystkie potrzebne pola FA(3) mają odbicie w kształcie invoiceV2 (§7). Największa niewiadoma.
- **Szablon `SKsefInvoiceImport.AMS` a wersja 2026.3** — spike potwierdził zgodność wersji 3.1; gdyby nowsza wersja żądała innej → użyć `SKsefInvoiceImport_V2.AMS` (format FK 3.0).
- accessToken TTL / RSA-OAEP label / kody 21165 / nagłówki 429 — patrz `reference/ksef2-recipe.md` §6.
- **Granularność plików:** 1 `.TXT` per faktura (nazwa = numer KSeF, łatwy dedup) vs 1 zbiorczy per profil+typ. Rekomendacja: 1 per faktura.
- **Zakres >3 mies.** — dzielić na okna 3-miesięczne w kliencie.

## 9. Kolejność budowy (milestones)

1. **Silnik offline (bez sieci):** `fa_mapper` + `ams_transform`, testy jednostkowe na próbce FA(3) → `.TXT` porównywany z `samples/`. (TDD.)
2. **`ksef_client`:** auth + query + download. Smoke test klucza publicznego (bez tokena). Potem 1 realny token → pełny auth → query → 1 faktura.
3. **Sklejenie:** `exporter` dla 1 profilu: pull(sprzedaż+koszt, zakres dat) → map → transform → zapis. Import do Symfonii = weryfikacja (jak spike).
4. **Config zaszyfrowany** + 4 profile.
5. **GUI (PySide6):** lista profili, per profil date range + checkboxy + „Eksportuj", pole folderu docelowego, log/postęp.
6. **`ledger` (dedup)** + obsługa błędów + backoff rate-limit + dzielenie zakresu >3 mies. + fallback eksport wsadowy.
7. **Pakowanie:** PyInstaller → Windows `.exe` (na maszynie Windows użytkownika).

## 10. Co musi dostarczyć użytkownik (przed/na starcie budowy)

- **Tokeny KSeF (produkcja) z uprawnieniem `InvoiceRead`** — po jednym per NIP (4 łącznie). Generowane w Aplikacji Podatnika KSeF prod → zakładka Tokeny. **Rekomendacja: zacząć od 1 tokena** (jedna firma) żeby przejść pełną pętlę, potem dodać 3.
- Potwierdzenie 4 NIP-ów firm + docelowej struktury folderów (nazwy podfolderów per profil).
- Dostęp do maszyny Windows z Symfonią do finalnego spike'u importu + pakowania `.exe`.

## 11. Artefakty w tym folderze (dla nowej sesji)

- `reference/ksef2-recipe.md` — **pełny** przepis KSeF 2.0 (auth, query, download, limity, FA(3), SDK) z żywego OpenAPI v2.6.1.
- `reference/symfonia-szablony/` — oryginalne pliki Symfonii: `SKsefInvoiceImport.xslt` (transform KSeF→AMS), `SKsefInvoiceImport.AMS` (v3.1), `SKsefInvoiceImport_V2.AMS` (FK 3.0), `SKsefModuleInvoice.AMS`, `eFaktura.xsd`, `efaktury.xslt`, `hmffk.AMS`.
- `samples/` — działające `ksefInvoice-{koszt,sprzedaz}.xml` + wygenerowane `.TXT` (wzorzec formatu).
- Pełny źródłowy zip szablonów Symfonii: `~/Downloads/wetransfer_szablon_2026-07-08_0814.zip` (146 plików, gdyby trzeba innych szablonów).

## 12. Weryfikacja (kryteria „gotowe")

- [ ] fa_mapper: realna FA(3) → ksefInvoice bez utraty pól (NIP, kwoty per stawka VAT, daty, numer, korekta).
- [ ] ams_transform: wynik importuje się do bufora FK **czysto** (jak spike, bez błędu SQL).
- [ ] ksef_client: auth 1 tokenem → query Subject1+Subject2 zakres dat → pobranie XML.
- [ ] exporter: 1 profil pełna pętla → `.TXT` w podfolderze; dedup działa.
- [ ] 4 profile niezależnie; tokeny zaszyfrowane; folder docelowy konfigurowalny.
- [ ] GUI: zakres dat + checkboxy sprzedaż/koszt + eksport per profil.
- [ ] `.exe` uruchamia się na Windows użytkownika.
