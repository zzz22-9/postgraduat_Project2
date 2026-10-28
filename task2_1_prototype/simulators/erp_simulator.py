"""Use the real ERP REST handler and a separate, minimal inventory table."""
from .legacy_bridge import independent_path, load_legacy, new_erp_client


def create_demo_order(database_path, config, emitter):
    path = independent_path(database_path)
    with new_erp_client(path) as client:
        response = client.post("/orders", json={"product_id": "WP1-P001", "product_name": "WP1 teaching workpiece",
                                               "quantity": 1, "due_date": "2026-10-30"})
        response.raise_for_status()
        order = client.get(f"/orders/{response.json()['order_id']}").json()
        emitter.emit("ERP", "REST_API", "structured_record", "/orders", order["created_at"],
                     {"status_code": response.status_code, "body": order},
                     {"clock": "wall", "db_ref": path.name, "transport_mode": "in_process_asgi"},
                     {"raw_id": order["order_id"], "suggested_type": "order", "domain_hint": "enterprise", "level_hint": "enterprise"})
    database, _, _, _, _, _ = load_legacy()
    material = config["material"]
    with database.connect(path) as conn:
        conn.execute("""CREATE TABLE inventory (
            resource_id TEXT PRIMARY KEY, name TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity>=0), unit TEXT NOT NULL)""")
        conn.execute("INSERT INTO inventory VALUES (?,?,?,?)", tuple(material[k] for k in ("resource_id", "name", "quantity", "unit")))
        row = dict(conn.execute("SELECT * FROM inventory WHERE resource_id=?", (material["resource_id"],)).fetchone())
    emitter.emit("ERP", "SQLITE", "structured_record", "inventory", order["created_at"], {"table": "inventory", "row": row},
                 {"clock": "wall", "db_ref": path.name, "transport_mode": "sqlite"},
                 {"raw_id": row["resource_id"], "suggested_type": "resource", "domain_hint": "enterprise", "level_hint": "factory"})
    return order
