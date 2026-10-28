# Przepis: klient KSeF 2.0 TEST — auth tokenem + pobranie faktur + XML pojedynczej faktury

> Źródło: żywy OpenAPI `api-test.ksef.mf.gov.pl/docs/v2/openapi.json` (wersja API 2.6.1, build 2.6.1-te-20260707, pobrany 2026-07-08) + oficjalne repo MF `CIRFMF/ksef-api`. Wszystkie ścieżki zweryfikowane w kontrakcie OpenAPI.
> Legenda pewności: **[PEWNE]** = z żywego kontraktu/dokumentacji MF · **[EMP]** = do potwierdzenia empirycznie w pierwszym uruchomieniu.

## 0. Fakty bazowe [PEWNE]

- **Base URL TEST:** `https://api-test.ksef.mf.gov.pl/v2` (uwaga: `/v2`, **NIE** `/api/v2`; endpointy auth pod `/auth`, **NIE** stary `/online/Session` z KSeF 1.0).
- **Portal TEST (UI, do wygenerowania tokena bez kodu):** `https://ksef-test.mf.gov.pl`. Swagger: `https://api-test.ksef.mf.gov.pl/docs/v2/index.html`.
- **Auth:** Bearer JWT. Do czytania faktur wymagane uprawnienie **`InvoiceRead`** na 4 endpointach fakturowych.
- **⚠️ TEST jest współdzielony** — self-signed dozwolone, dane NIE izolowane między integratorami. Używaj losowych/testowych NIP-ów, nigdy prawdziwych danych.
- **Okno serwisowe:** codziennie 16:00–18:00 (od 1.10.2025) — licz się z niedostępnością.

## 1. Uwierzytelnianie — czy wystarczy sam token? TAK (z zastrzeżeniem) [PEWNE]

**Sam string tokena KSeF wystarcza do zalogowania** przez `POST /auth/ksef-token` — bez certyfikatu klienta ani podpisu XAdES w momencie logowania. Dwa zastrzeżenia:
1. Token trzeba **raz zaszyfrować kluczem publicznym MF** (RSA-OAEP SHA-256) — to zwykłe krypto z kluczem publicznym pobranym z API, **nie** certyfikat kliencki.
2. Token trzeba **wcześniej jednorazowo wygenerować**. Na TEST najprościej przez portal (login Profilem Zaufanym → zakładka Tokeny → Generuj), zero kodu. Alternatywnie `POST /tokens` — ale to wymaga jednorazowego XAdES.

**Praktyczny wniosek:** dla klienta odczytowego (nasz przypadek) wygeneruj token raz w portalu z uprawnieniem `InvoiceRead`, a cała reszta klienta działa na samym tokenie + krypto z publicznym kluczem MF. **Nie potrzebujesz certyfikatu kwalifikowanego ani XAdES w kodzie klienta.**

### Sekwencja auth krok-po-kroku

```
[PRE] Portal ksef-test.mf.gov.pl → Tokeny → Generuj (zaznacz InvoiceRead) → skopiuj token (POKAZANY RAZ)

[0] GET  /v2/security/public-key-certificates           (bez auth)
        → weź wpis usage=["KsefTokenEncryption"] → certificate (DER Base64) + publicKeyId

[1] POST /v2/auth/challenge                              (bez body)
        → { challenge, timestampMs }                    (challenge żyje 10 min)

[2] plaintext = "{tokenKSeF}|{timestampMs}"
    encryptedToken = Base64( RSA-OAEP(SHA-256, MGF1-SHA256, plaintext, pubKeyMF) )

[3] POST /v2/auth/ksef-token   Content-Type: application/json
    body: { "challenge": "<z [1]>",
            "contextIdentifier": { "type": "Nip", "value": "<NIP>" },
            "encryptedToken": "<Base64 z [2]>",
            "publicKeyId": "<z [0]>" }
        → 202 { referenceNumber, authenticationToken: { token(JWT), validUntil } }

[4] GET  /v2/auth/{referenceNumber}   Authorization: Bearer {authenticationToken}
        → poll aż status.code == 200   (100=w toku, 200=OK, 450=błąd tokenu/krypto/czasu, 415=brak uprawnień)

[5] POST /v2/auth/token/redeem        Authorization: Bearer {authenticationToken}   (bez body)
        → 200 { accessToken:{token,validUntil}, refreshToken:{token,validUntil} }   ⚠️ JEDNORAZOWE (powtórka=400)

[6] Zasoby fakturowe:  Authorization: Bearer {accessToken}
    Odświeżanie:       POST /v2/auth/token/refresh  Authorization: Bearer {refreshToken}
```

**Ważności [PEWNE]:** challenge 10 min · accessToken ~kilkanaście min (dokładnie w `exp` JWT — **[EMP]** zdekoduj po redeem) · refreshToken do 7 dni (wielorazowy) · token KSeF bezterminowy (ważny do unieważnienia).

**Uwaga krypto [PEWNE/EMP]:** dokumentacja opisuje **RSA-OAEP + SHA-256 (MGF1-SHA256)** jako domyślny. Rozmiar klucza wynika z certyfikatu z kroku [0]. Wariant ECDsa istnieje w kliencie Java, ale traktuj RSA-OAEP jako kanon. Dokładny label OAEP — **[EMP]** (domyślnie pusty/brak).

## 2. Pobranie metadanych faktur — sprzedaż (Subject1) + koszty (Subject2) [PEWNE]

Jeden endpoint, filtr `subjectType` decyduje o roli:
- **`Subject1`** = jesteś Podmiotem 1 (sprzedawcą) → faktury **sprzedażowe/wystawione**.
- **`Subject2`** = jesteś Podmiotem 2 (nabywcą) → faktury **kosztowe/otrzymane**.

```
POST /v2/invoices/query/metadata?pageOffset=0&pageSize=100&sortOrder=Asc
Authorization: Bearer {accessToken}
Content-Type: application/json
```

**Body — sprzedaż:**
```json
{
  "subjectType": "Subject1",
  "dateRange": {
    "dateType": "Issue",
    "from": "2026-04-01T00:00:00+00:00",
    "to":   "2026-06-30T23:59:59+00:00"
  }
}
```
**Body — koszty:** to samo, `"subjectType": "Subject2"`.

**Reguły [PEWNE]:**
- `dateRange.from` **WYMAGANE**; `to` opcjonalne (domyślnie „teraz" UTC). **Max zakres 3 miesiące.**
- `dateType`: `Issue` (data wystawienia) | `Invoicing` (data przyjęcia w KSeF) | `PermanentStorage` (data trwałego zapisu — dla pobierania przyrostowego).
- `pageSize`: 10–250 (domyślnie 10). `pageOffset` 0-based.
- Filtry opcjonalne: `ksefNumber`, `invoiceNumber`, `amount{type:Brutto|Netto|Vat,from,to}`, `sellerNip`, `buyerIdentifier{type,value}`, `currencyCodes[]`, `invoicingMode`, `invoiceTypes[]` (Vat|Zal|Kor|Roz), `hasAttachment`.

**Odpowiedź `QueryInvoicesMetadataResponse`:**
```
{ hasMore, isTruncated, permanentStorageHwmDate,
  invoices: [ {
    ksefNumber,          // np. "5555555555-20250828-010080615740-E4" — użyjesz do pobrania XML
    invoiceNumber, issueDate, invoicingDate, acquisitionDate, permanentStorageDate,
    seller:{nip,name}, buyer:{identifier{type,value},name},
    netAmount, grossAmount, vatAmount, currency, invoicingMode, invoiceType,
    formCode:{systemCode:"FA (3)", schemaVersion:"1-0E", value:"FA"},  // ← wersja schematu per faktura
    invoiceHash, hasAttachment
  } ] }
```

**Paginacja / limit [PEWNE]:** limit techniczny 10 000 rekordów na zestaw filtrów.
- `hasMore=false` → koniec.
- `hasMore=true && isTruncated=false` → zwiększ `pageOffset`.
- `hasMore=true && isTruncated=true` → zawęź `dateRange.from` do daty ostatniego rekordu (zgodnie z `sortOrder`), wyzeruj `pageOffset`.
- Przyrostowo: `dateType=PermanentStorage` + `sortOrder=Asc` + kroczący znacznik `permanentStorageHwmDate`.

**Rate limit [PEWNE]:** grupa `invoiceMetadata` — 8/s, 16/min, 20/h.

## 3. Pobranie XML pojedynczej faktury po numerze KSeF [PEWNE]

```
GET /v2/invoices/ksef/{ksefNumber}
Authorization: Bearer {accessToken}
Accept: application/xml
```
- **Odpowiedź 200:** surowy XML faktury (FA(2) lub FA(3) — patrz `formCode` z metadanych).
- **Format numeru KSeF:** 35 znaków w 2.0 (36 akceptowane dla zgodności z 1.0), wzór `NIP-YYYYMMDD-XXXXXXXXXXXX-XXXX`.
- **Błędy 400 [PEWNE]:** `21164` = faktura nie istnieje · **`21165` = przetworzona, ale JESZCZE niedostępna do pobrania → ponów później** (istotne dla świeżo wystawionych) · `21405` = błąd walidacji.
- **Rate limit:** grupa `invoiceDownload` — 8/s, 16/min, **64/h**.

**⚠️ Wolumen [PEWNE]:** `GET /invoices/ksef` jest mocno ograniczony (64/h). Do masowego pobierania **nie rób pętli po pojedynczych fakturach** — użyj eksportu wsadowego:
`POST /v2/invoices/exports` (filtr jak w query + szyfrowanie AES, klucz AES szyfrowany kluczem publicznym MF) → `GET /v2/invoices/exports/{referenceNumber}` (status + presigned URL-e do części `.zip.aes`, AES-256-CBC/PKCS#7, paczka + `_metadata.json`, dostępna 7 dni). Dla naszego zadania „XML jednej faktury po numerze" wystarcza jednak `GET /invoices/ksef/{ksefNumber}`.

## 4. Format FA [PEWNE]

- **TEST przyjmuje/zwraca:** FA(2), FA(3), FA_PEF(3), FA_KOR_PEF(3). Buduj/oczekuj **FA(3)** (`systemCode "FA (3)"`, `schemaVersion "1-0E"`, `WariantFormularza=3`, `DataWytworzeniaFa >= 2025-09-01`); FA(2) tylko jako legacy na TE.
- **DEMO/PROD:** już tylko FA(3) (+ PEF).
- **Root FA(3):** `Faktura` → `Naglowek` → `Podmiot1` (sprzedawca) → `Podmiot2` (nabywca) → `Podmiot3?` (0..100) → `PodmiotUpowazniony?` → `Fa` (pozycje, kwoty, VAT) → `Stopka?`.
- **XSD:** `github.com/CIRFMF/ksef-api/.../FA/schemat_FA(3)_v1-0E.xsd`. Wersję konkretnej pobranej faktury czytaj z `formCode` w metadanych — nie zakładaj.

## 5. Gotowce, których warto użyć zamiast pisać od zera [PEWNE]

Oficjalne SDK MF (org `CIRFMF`, open-source, utrzymywane zgodnie ze spec) — mają gotowe szyfrowanie tokena, obsługę challenge i redeem:
- **C#/.NET:** `github.com/CIRFMF/ksef-client-csharp` (.NET 8/9/10 + netstandard2.0).
- **Java:** `github.com/CIRFMF/ksef-client-java` (Maven przez GitHub Packages org CIRFMF).
- Dokumentacja/tutoriale: `github.com/CIRFMF/ksef-api` (auth: `uwierzytelnianie.md`, tokeny: `tokeny-ksef.md`, środowiska: `srodowiska.md`).

**Rekomendacja:** zacznij od oficjalnego SDK (zdejmuje ryzyko błędów RSA-OAEP i sekwencji challenge→redeem), a ręcznie wołaj tylko `invoices/query/metadata` + `invoices/ksef/{ksefNumber}` jeśli SDK ich nie opakowuje wygodnie.

## 6. Do potwierdzenia empirycznie w pierwszym uruchomieniu [EMP]

- Dokładny czas życia accessToken (zdekoduj `exp` JWT po redeem).
- Rozmiar klucza RSA i ewentualny label OAEP (z certyfikatu `KsefTokenEncryption`).
- Czy token wygenerowany w portalu faktycznie niesie `InvoiceRead` (jeśli `GET /auth/{ref}` zwraca 415 — token bez uprawnienia odczytu; wygeneruj ponownie z zaznaczonym przeglądaniem faktur).
- Zachowanie `21165` na świeżo wystawionej fakturze (opóźnienie dostępności → retry z backoff).
- Format nagłówków 429 (Retry-After) — spec podaje tylko tabelę limitów.