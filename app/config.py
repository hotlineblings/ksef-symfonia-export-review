"""Konfiguracja aplikacji: 4 profile (nazwa, NIP, token KSeF, podfolder) + folder docelowy.

Tokeny KSeF NIE są przechowywane plaintextem: cały config szyfrowany Fernet,
klucz wyprowadzany z master password przez scrypt. Złe hasło = InvalidToken
(HMAC Fernet), bez wycieku informacji.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

APP_DIR_NAME = "KsefSymfoniaExport"
PROD_BASE_URL = "https://api.ksef.mf.gov.pl/v2"

_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1


class WrongMasterPassword(Exception):
    pass


class ConfigCorrupted(Exception):
    pass


@dataclass
class Profile:
    name: str
    nip: str
    token: str
    subfolder: str
    # nadpisania kont/terminu dla TEJ firmy — klucze dokładnie jak pola kont w
    # AppConfig (patrz niżej), wartości zawsze str (termin_fallback_dni też,
    # konwersja na int dopiero w AppConfig.konta_dla). Obecność klucza =
    # nadpisanie globalnej wartości, brak klucza = użyj globalnej.
    konta_override: dict[str, str] = field(default_factory=dict)
    # nazwa bazy Symfonii (SQL) tej firmy — patrz app/symfonia_sql.py; puste =
    # synchronizacja SQL nieskonfigurowana dla tego profilu (tylko plikowa)
    db_symfonia: str = ""
    # czy przed eksportem automatycznie synchronizować rejestr numerów z
    # kartoteką FK przez SQL (patrz RejestrNumerow.synchronizuj_z_sql)
    sql_autosync: bool = True
    # ostatnio użyty zakres dat eksportu TEJ firmy (ISO YYYY-MM-DD), zapamiętywany
    # przy starcie eksportu i przywracany przy wyborze profilu w MainWindow —
    # patrz app/gui.py::_zakres_dla_profilu. Puste = brak zapamiętanego zakresu
    # (stary profil sprzed tej funkcji albo jeszcze nigdy nie eksportowano).
    ostatnie_od: str = ""
    ostatnie_do: str = ""

    def masked_token(self) -> str:
        return (self.token[:6] + "…") if self.token else "(brak)"


@dataclass
class AppConfig:
    profiles: list[Profile] = field(default_factory=list)
    target_dir: str = ""
    ksef_base_url: str = PROD_BASE_URL
    # prefiksy konta rozrachunkowego (plik hmffk: "<prefiks>-<pozycja>"), zależne
    # od rodzaju dokumentu: sprzedaż -> linia WN brutto, koszt -> linia MA brutto
    konto_prefix_sprzedaz: str = "201-2"
    konto_prefix_koszt: str = "202-2"
    # pełny dekret sprzedaży: konta linii MA netto/VAT (koszt bez zmian — konta
    # kosztowe przypisuje księgowa ręcznie, tylko MA brutto dostaje konto)
    konto_sprzedaz_netto: str = "703"
    konto_sprzedaz_vat: str = "222-1"
    # pełny dekret kosztu: konto VAT naliczonego (WN) zawsze; konto netto (WN)
    # domyślnie puste — przypisuje księgowa, pole istnieje, by dało się ustawić
    # na stałe (np. gdy wszystkie koszty firmy trafiają na jedno konto)
    konto_koszt_vat: str = "221-1"
    konto_koszt_netto: str = ""
    # brak TerminPlatnosci w FA(3) -> termin liczony jako data wystawienia (P_1)
    # + tyle dni (patrz app/hmffk.py::map_fa3_to_hmffk)
    termin_fallback_dni: int = 14

    # klucze kont/terminu współdzielone przez konta_dla() i GUI (RejestrDialog)
    KONTA_KLUCZE_TEKSTOWE = (
        "konto_prefix_sprzedaz", "konto_prefix_koszt",
        "konto_sprzedaz_netto", "konto_sprzedaz_vat",
        "konto_koszt_vat", "konto_koszt_netto",
    )

    def konta_dla(self, profile: Profile) -> dict:
        """Wartości kont/terminu efektywne dla danego profilu: globalne z
        AppConfig nadpisane tym, co profil ma w konta_override. Brak klucza w
        override = wartość globalna."""
        wynik: dict = {
            klucz: getattr(self, klucz) for klucz in self.KONTA_KLUCZE_TEKSTOWE
        }
        wynik["termin_fallback_dni"] = self.termin_fallback_dni
        override = profile.konta_override
        for klucz in self.KONTA_KLUCZE_TEKSTOWE:
            if klucz in override:
                wynik[klucz] = override[klucz]
        if "termin_fallback_dni" in override:
            try:
                wynik["termin_fallback_dni"] = int(override["termin_fallback_dni"])
            except (TypeError, ValueError):
                pass  # śmieciowa wartość override -> zostaje globalna
        return wynik


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_DIR_NAME


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


class ConfigStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else app_data_dir() / "config.enc"
        self._fernet: Fernet | None = None
        self._salt: bytes | None = None

    def exists(self) -> bool:
        return self.path.exists()

    def initialize(self, master_password: str) -> AppConfig:
        """Pierwsze uruchomienie: nowa sól, pusty config, zapis."""
        self._salt = os.urandom(16)
        self._fernet = Fernet(_derive_key(master_password, self._salt))
        cfg = AppConfig()
        self.save(cfg)
        return cfg

    def unlock(self, master_password: str) -> AppConfig:
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            salt = base64.b64decode(envelope["salt"])
            payload = envelope["payload"].encode("ascii")
        except (OSError, ValueError, KeyError) as exc:
            raise ConfigCorrupted(f"Plik konfiguracji uszkodzony: {exc}") from exc
        fernet = Fernet(_derive_key(master_password, salt))
        try:
            raw = fernet.decrypt(payload)
        except InvalidToken:
            raise WrongMasterPassword("Nieprawidłowe hasło główne")
        self._salt, self._fernet = salt, fernet
        try:
            data = json.loads(raw)
            profiles = [Profile(**p) for p in data.get("profiles", [])]
            return AppConfig(
                profiles=profiles,
                target_dir=data.get("target_dir", ""),
                ksef_base_url=data.get("ksef_base_url", PROD_BASE_URL),
                # stary klucz "konto_prefix" (sprzed rozdzielenia na dwa prefiksy)
                # celowo nieobsługiwany — funkcja nie była jeszcze wydana
                konto_prefix_sprzedaz=data.get("konto_prefix_sprzedaz") or "201-2",
                konto_prefix_koszt=data.get("konto_prefix_koszt") or "202-2",
                konto_sprzedaz_netto=data.get("konto_sprzedaz_netto") or "703",
                konto_sprzedaz_vat=data.get("konto_sprzedaz_vat") or "222-1",
                konto_koszt_vat=data.get("konto_koszt_vat") or "221-1",
                konto_koszt_netto=data.get("konto_koszt_netto") or "",
                # "or" pominąłby poprawną wartość 0 (termin natychmiastowy) —
                # sprawdzamy None wprost, inaczej jak przy polach tekstowych wyżej
                termin_fallback_dni=(
                    data["termin_fallback_dni"] if data.get("termin_fallback_dni") is not None else 14
                ),
            )
        except (ValueError, TypeError) as exc:
            raise ConfigCorrupted(f"Zawartość konfiguracji uszkodzona: {exc}") from exc

    def save(self, cfg: AppConfig) -> None:
        if self._fernet is None or self._salt is None:
            raise RuntimeError("save() przed initialize()/unlock()")
        payload = self._fernet.encrypt(json.dumps(asdict(cfg)).encode("utf-8"))
        envelope = {
            "version": 1,
            "kdf": "scrypt",
            "salt": base64.b64encode(self._salt).decode("ascii"),
            "n": _SCRYPT_N,
            "r": _SCRYPT_R,
            "p": _SCRYPT_P,
            "payload": payload.decode("ascii"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
