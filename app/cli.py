"""CLI eksportu (bez GUI) — także do pierwszego testu live (W4/W5).

Użycie (token NIE w argumentach — getpass albo zmienna środowiskowa KSEF_TOKEN):

    .venv\\Scripts\\python.exe -m app.cli --nip 5252445719 --od 2026-06-01 --do 2026-06-30 ^
        --rodzaj koszt --cel C:\\eksport-ksef --podfolder firma-a [--zapisz-fa3] [--test-api]

Pobranie tylko wybranych faktur po numerze (nadanym przez wystawcę, nie numerze
KSeF) — nadal wymagane --od/--do/--rodzaj, bo to w tym zakresie numery są szukane:

    .venv\\Scripts\\python.exe -m app.cli --nip 5252445719 --od 2026-06-01 --do 2026-06-30 ^
        --rodzaj koszt --cel C:\\eksport-ksef --numer FV/123/2026 --numer FV/124/2026

Pobranie tylko faktur wybranych kontrahentów (NIP albo numer karty z rejestru
numerów) — analogicznie, w zakresie --od/--do/--rodzaj:

    .venv\\Scripts\\python.exe -m app.cli --nip 5252445719 --od 2026-06-01 --do 2026-06-30 ^
        --rodzaj sprzedaz --cel C:\\eksport-ksef --kontrahent 9100010014 --kontrahent 42

Kod wyjścia:
    0 — eksport pełny, bez błędów;
    1 — eksport pełny, ale są faktury z błędem mapowania (BŁĄD …) albo nietrafione
        wyszukiwanie — do ręcznej obsługi;
    2 — błąd argumentów / konfiguracji / synchronizacji z Symfonią (eksport nie ruszył);
    3 — eksport NIEPEŁNY albo przerwany (limit KSeF, błąd paczki eksportu,
        NIE POBRANO …) — uruchom ponownie z --tylko-nowe, żeby dociągnąć resztę bez
        dubli; ma pierwszeństwo przed 1 (ponowny przebieg ponowi też faktury z zakresu
        z błędami).
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from app.ams_transform import write_txt_atomic
from app.config import PROD_BASE_URL, Profile
from app.exporter import Kind, run_export
from app.kontrahenci_nr import RejestrNumerow, SynchronizacjaOdrzucona
from app.ksef_client import KsefClient
from app.ledger import Ledger
from app.symfonia_sql import SymfoniaSqlError


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Eksport faktur KSeF -> Symfonia FK (AMS .TXT)")
    parser.add_argument("--nip", required=True, help="NIP firmy (kontekst logowania)")
    parser.add_argument("--nazwa", default=None, help="Nazwa profilu (do logów)")
    parser.add_argument("--od", type=_parse_date, default=None, help="Data od (YYYY-MM-DD)")
    parser.add_argument("--do", dest="do_", type=_parse_date, default=None, help="Data do (YYYY-MM-DD)")
    parser.add_argument(
        "--rodzaj", default="koszt,sprzedaz",
        help="koszt, sprzedaz lub oba po przecinku (domyślnie oba)",
    )
    parser.add_argument("--cel", required=True, help="Folder docelowy eksportu")
    parser.add_argument("--podfolder", default=None, help="Podfolder profilu (domyślnie NIP)")
    parser.add_argument("--baza", default=PROD_BASE_URL, help="Base URL API (domyślnie produkcja)")
    parser.add_argument(
        "--format", dest="format_", choices=("hmffk", "sksef"), default="hmffk",
        help="hmffk (domyślnie): JEDEN plik pod standardowy szablon Symfonii "
             "„Dane z programu Handel, format FK 3.0” — kontrahenci i faktury razem, "
             "bez kopiowania szablonów; sksef: pliki pod SKsefInvoiceImport.AMS (3.1)",
    )
    parser.add_argument(
        "--paczka", action="store_true",
        help="[tylko --format sksef] Jeden zbiorczy plik .TXT per rodzaj (zamiast pliku per faktura)",
    )
    parser.add_argument(
        "--tylko-nowe", action="store_true",
        help="Pomiń faktury pobrane wcześniej (tryb przyrostowy); domyślnie pobierany jest komplet z zakresu",
    )
    parser.add_argument(
        "--kontrahenci", action="store_true",
        help="[tylko --format sksef] Zapisz plik kart kontrahentów AMS (kartoteka\\kontrahenci_*.TXT) "
             "+ własny szablon importu (kartoteka\\szablon\\SKsefKontrahenciImport.AMS); "
             "w formacie hmffk kontrahenci są zawsze wbudowani w plik faktur",
    )
    parser.add_argument(
        "--adresy-strukturalne", action="store_true",
        help="Dodaj pola Ulica/NrDomu/KodPocztowy/Miejscowosc do Kontrahent{} faktur "
             "(eksperymentalne — Miejscowość zmienia klucz kontrahenta w Symfonii)",
    )
    parser.add_argument(
        "--konto-prefix-sprzedaz", dest="konto_prefix_sprzedaz", default="201-2",
        help="[tylko --format hmffk] Prefiks konta rozrachunkowego kontrahenta dla "
             "faktur sprzedażowych (domyślnie 201-2); pełne konto w linii WN brutto "
             "dekretu to <prefiks>-<pozycja karty w rejestrze numerów kontrahentów>",
    )
    parser.add_argument(
        "--konto-prefix-koszt", dest="konto_prefix_koszt", default="202-2",
        help="[tylko --format hmffk] Prefiks konta rozrachunkowego kontrahenta dla "
             "faktur kosztowych (domyślnie 202-2); pełne konto w linii MA brutto "
             "dekretu to <prefiks>-<pozycja karty w rejestrze numerów kontrahentów>",
    )
    parser.add_argument(
        "--konto-sprzedaz-netto", dest="konto_sprzedaz_netto", default="703",
        help="[tylko --format hmffk] Konto linii MA netto pełnego dekretu sprzedaży "
             "(domyślnie 703); nie dotyczy faktur kosztowych",
    )
    parser.add_argument(
        "--konto-sprzedaz-vat", dest="konto_sprzedaz_vat", default="222-1",
        help="[tylko --format hmffk] Konto linii MA VAT należnego pełnego dekretu "
             "sprzedaży (domyślnie 222-1); nie dotyczy faktur kosztowych",
    )
    parser.add_argument(
        "--konto-koszt-vat", dest="konto_koszt_vat", default="221-1",
        help="[tylko --format hmffk] Konto linii WN VAT naliczonego pełnego dekretu "
             "kosztu (domyślnie 221-1); nie dotyczy faktur sprzedażowych",
    )
    parser.add_argument(
        "--konto-koszt-netto", dest="konto_koszt_netto", default="",
        help="[tylko --format hmffk] Konto linii WN netto pełnego dekretu kosztu "
             "(domyślnie puste — konto kosztowe przypisuje księgowa ręcznie w FK)",
    )
    parser.add_argument(
        "--termin-dni", dest="termin_dni", type=int, default=14,
        help="[tylko --format hmffk] Gdy faktura nie ma terminu płatności w FA(3), "
             "termin liczony jako data wystawienia + tyle dni (domyślnie 14) — "
             "żeby rozrachunek w FK i tak powstał automatycznie",
    )
    parser.add_argument(
        "--import-kartoteka", dest="import_kartoteka", default=None, metavar="PLIK",
        help="Wczytaj numery kart kontrahentów z eksportu kartoteki FK (.xlsx/.csv) "
             "do rejestru numerów przed eksportem (seed numeracji)",
    )
    parser.add_argument(
        "--sync-sql", dest="sync_sql", default=None, metavar="BAZA",
        help="Przed eksportem zsynchronizuj rejestr numerów kontrahentów wprost z "
             "bazą Symfonii (SQL, tylko odczyt — nazwa bazy na instancji .\\SAGEPL) "
             "zamiast/obok --import-kartoteka; jednorazowo, niezależnie od ustawień "
             "auto-synchronizacji profilu w GUI. Błąd synchronizacji przerywa "
             "eksport (kod wyjścia 2) — ochrona numeracji kont kontrahentów",
    )
    parser.add_argument(
        "--numer", dest="numery", action="append", default=None, metavar="NR",
        help="Pobierz tylko fakturę(y) o tym numerze (nadanym przez WYSTAWCĘ, nie "
             "numerze KSeF) — flagę można powtórzyć dla wielu numerów. Trzy formy "
             "wpisu: pełny numer (porównanie ignoruje wielkość liter i białe "
             "znaki), sama końcówka/same cyfry (np. \"247\" trafia numer kończący "
             "się na 247, niezależnie od zer wiodących), albo zakres końcówek "
             "(np. \"247-251\", max 2000 numerów). KSeF nie umie szukać po numerze "
             "wystawcy, więc numer jest szukany wśród metadanych z zakresu "
             "--od/--do i wybranych --rodzaj (nadal wymagane). Można łączyć z "
             "--numery-plik. Bez żadnej z tych flag eksport obejmuje wszystkie "
             "faktury z zakresu (jak dotychczas)",
    )
    parser.add_argument(
        "--numery-plik", dest="numery_plik", default=None, metavar="PLIK",
        help="Plik tekstowy z numerami faktur do pobrania, jeden wpis w wierszu "
             "(UTF-8; te same trzy formy co --numer: pełny numer, końcówka albo "
             "zakres) — wpisy są dołączane do tych z --numer",
    )
    parser.add_argument(
        "--kontrahent", dest="kontrahenci_id", action="append", default=None, metavar="ID",
        help="Pobierz tylko faktury tego kontrahenta — NIP albo numer karty z "
             "rejestru numerów kontrahentów (flagę można powtórzyć dla wielu "
             "kontrahentów). Sprzedaż: kontrahentem jest nabywca; koszt: "
             "sprzedawca. Numer karty wymaga --format hmffk (albo wcześniejszego "
             "--import-kartoteka) — bez rejestru numerów nie da się przełożyć na "
             "NIP. Można łączyć z --kontrahenci-plik oraz z --numer/--numery-plik "
             "(koniunkcja). Bez żadnej z tych flag eksport obejmuje wszystkie "
             "faktury z zakresu",
    )
    parser.add_argument(
        "--kontrahenci-plik", dest="kontrahenci_plik", default=None, metavar="PLIK",
        help="Plik tekstowy z identyfikatorami kontrahentów (NIP lub numer "
             "karty), jeden w wierszu (UTF-8) — dołączane do tych z --kontrahent",
    )
    parser.add_argument(
        "--zapisz-fa3", action="store_true",
        help="Zapisuj też surowe XML FA(3) (podfolder fa3-raw) — do diagnostyki",
    )
    parser.add_argument(
        "--test-api", action="store_true",
        help="Tylko smoke test API (bez tokena) i koniec",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.test_api:
        client = KsefClient(args.baza, args.nip, "brak")
        certs = client.smoke_test()
        print(f"OK: {args.baza} odpowiada, {len(certs)} certyfikat(y) MF")
        return 0

    token = os.environ.get("KSEF_TOKEN") or getpass.getpass("Token KSeF (nie będzie wyświetlony): ")
    if not token.strip():
        print("Brak tokena.", file=sys.stderr)
        return 2

    today = date.today()
    date_from = args.od or (today - timedelta(days=30))
    date_to = args.do_ or today

    kinds = set()
    for part in args.rodzaj.split(","):
        part = part.strip().lower()
        if part in ("koszt", "koszty"):
            kinds.add(Kind.COST)
        elif part in ("sprzedaz", "sprzedaż"):
            kinds.add(Kind.SALES)
        else:
            print(f"Nieznany rodzaj: {part}", file=sys.stderr)
            return 2

    profile = Profile(
        name=args.nazwa or args.nip,
        nip=args.nip,
        token=token.strip(),
        subfolder=args.podfolder or args.nip,
    )

    if args.sync_sql:
        rejestr_sql = RejestrNumerow()
        try:
            raport_sql = rejestr_sql.synchronizuj_z_sql(profile.nip, args.sync_sql)
        except SymfoniaSqlError as exc:
            print(f"Synchronizacja z bazą Symfonii ({args.sync_sql}) nie powiodła się: {exc}", file=sys.stderr)
            return 2
        except SynchronizacjaOdrzucona as exc:
            print(f"Synchronizacja z bazą Symfonii ({args.sync_sql}) przerwana: {exc}", file=sys.stderr)
            return 2
        finally:
            rejestr_sql.close()
        print(
            f"Synchronizacja z Symfonią ({args.sync_sql}): dodano {raport_sql.dodane} "
            f"wpis(ów), zaktualizowano {raport_sql.zaktualizowane}."
        )
        for w in raport_sql.jako_ostrzezenia():
            print(f"  UWAGA: {w}")

    client = KsefClient(args.baza, profile.nip, profile.token)
    ledger = Ledger()

    if args.zapisz_fa3:
        raw_dir = Path(args.cel) / profile.subfolder / "fa3-raw"
        original_download = client.download_invoice

        def download_and_dump(ksef_number: str) -> bytes:
            data = original_download(ksef_number)
            write_txt_atomic(raw_dir / f"{ksef_number}.xml", data)
            return data

        client.download_invoice = download_and_dump  # type: ignore[method-assign]

    if args.format_ == "hmffk" and (args.paczka or args.kontrahenci or args.adresy_strukturalne):
        print(
            "Info: --paczka/--kontrahenci/--adresy-strukturalne dotyczą formatu sksef — "
            "w formacie hmffk powstaje zawsze jeden plik z wbudowanymi kontrahentami."
        )

    if args.import_kartoteka:
        rejestr_seed = RejestrNumerow()
        try:
            dodane, ostrzezenia = rejestr_seed.importuj_kartoteke(
                profile.nip, Path(args.import_kartoteka)
            )
        finally:
            rejestr_seed.close()
        print(f"Import kartoteki: dodano {dodane} wpis(ów) do rejestru numerów.")
        for w in ostrzezenia:
            print(f"  UWAGA: {w}")

    invoice_numbers: list[str] = list(args.numery or [])
    if args.numery_plik:
        try:
            tresc_numerow = Path(args.numery_plik).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Nie można odczytać --numery-plik: {exc}", file=sys.stderr)
            return 2
        invoice_numbers += [linia.strip() for linia in tresc_numerow.splitlines() if linia.strip()]
    if invoice_numbers:
        print(
            f"Tryb wyszukiwania po numerze: {len(invoice_numbers)} numer(ów), "
            f"zakres {date_from.isoformat()}–{date_to.isoformat()}."
        )

    contractor_filter: list[str] = list(args.kontrahenci_id or [])
    if args.kontrahenci_plik:
        try:
            tresc_kontrahentow = Path(args.kontrahenci_plik).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Nie można odczytać --kontrahenci-plik: {exc}", file=sys.stderr)
            return 2
        contractor_filter += [
            linia.strip() for linia in tresc_kontrahentow.splitlines() if linia.strip()
        ]
    if contractor_filter:
        print(
            f"Tryb wyszukiwania po kontrahencie: {len(contractor_filter)} identyfikator(ów), "
            f"zakres {date_from.isoformat()}–{date_to.isoformat()}."
        )

    report = run_export(
        profile, client, ledger, date_from, date_to, kinds, Path(args.cel),
        fmt=args.format_,
        combined=args.paczka,
        only_new=args.tylko_nowe,
        invoice_numbers=invoice_numbers or None,
        contractor_filter=contractor_filter or None,
        kontrahenci_file=args.kontrahenci,
        structured_address=args.adresy_strukturalne,
        konto_prefix_sprzedaz=args.konto_prefix_sprzedaz,
        konto_prefix_koszt=args.konto_prefix_koszt,
        konto_sprzedaz_netto=args.konto_sprzedaz_netto,
        konto_sprzedaz_vat=args.konto_sprzedaz_vat,
        konto_koszt_vat=args.konto_koszt_vat,
        konto_koszt_netto=args.konto_koszt_netto,
        termin_fallback_dni=args.termin_dni,
        on_progress=lambda i, n, nr: print(f"  [{i}/{n}] {nr}"),
    )

    print()
    print("=" * 60)
    print(f"Wynik: {report.summary()}")
    for nr, msg in report.failed:
        print(f"  BŁĄD {nr}: {msg}")
    for nr, msg in report.niepobrane:
        print(f"  NIE POBRANO {nr}: {msg}")
    for w in report.warnings:
        print(f"  UWAGA: {w}")
    print(f"Pliki: {Path(args.cel) / profile.subfolder}")
    if report.niepelny or report.cancelled:
        print("Eksport niepełny — uruchom ponownie z --tylko-nowe, żeby dociągnąć resztę.")
        return 3
    return 0 if not report.failed else 1


if __name__ == "__main__":
    sys.exit(main())
