import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path, monkeypatch):
    """Żaden test nie może pisać do prawdziwego %APPDATA% (quota.db, ledger.db, config)."""
    import app.config
    import app.kontrahenci_nr
    import app.ledger
    import app.quota

    data_dir = tmp_path / "appdata"
    for module in (app.config, app.ledger, app.quota, app.kontrahenci_nr):
        monkeypatch.setattr(module, "app_data_dir", lambda d=data_dir: d)
    return data_dir
