"""One import boundary for the unchanged top-level erp/mes/simulation packages."""
from contextlib import contextmanager
from pathlib import Path
import sys

LEGACY_ROOT = Path(__file__).resolve().parents[2] / "minimal_factory"


def load_legacy():
    for name in ("erp", "mes", "simulation"):
        module = sys.modules.get(name)
        if module and not Path(module.__file__).resolve().is_relative_to(LEGACY_ROOT):
            raise RuntimeError(f"conflicting imported package: {name}")
    if str(LEGACY_ROOT) not in sys.path:
        sys.path.insert(0, str(LEGACY_ROOT))
    from erp import database
    from erp.app import create_app
    from mes import service, schemas
    from simulation import engine, inputs
    return database, create_app, service, schemas, engine, inputs


def independent_path(path):
    path = Path(path).resolve()
    if path.is_relative_to(LEGACY_ROOT.resolve()):
        raise ValueError("WP1 may not write inside minimal_factory, including the original ERP database")
    return path


@contextmanager
def new_erp_client(database_path):
    from fastapi.testclient import TestClient
    path = independent_path(database_path)
    if path.exists():
        raise ValueError("demo database must be new; refusing to reuse existing business data")
    _, create_app, _, _, _, _ = load_legacy()
    # Lifespan initializes ERP/MES in this explicit path, never the module-level app.
    with TestClient(create_app(path)) as client:
        yield client
