PRAGMA foreign_keys=ON;
PRAGMA application_id=1431131697;
PRAGMA user_version=1;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE scenarios (
 scenario_id TEXT PRIMARY KEY, title TEXT NOT NULL, source_run_id TEXT NOT NULL, config_json TEXT NOT NULL
);
CREATE TABLE objects (
 object_id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL REFERENCES scenarios,
 raw_id TEXT NOT NULL, object_type TEXT NOT NULL, domain TEXT NOT NULL, level TEXT NOT NULL,
 source_system TEXT NOT NULL, context_json TEXT NOT NULL,
 UNIQUE(scenario_id, object_type, raw_id), UNIQUE(scenario_id, object_id)
);
CREATE TABLE states (
 object_id TEXT NOT NULL REFERENCES objects, time_s REAL NOT NULL CHECK(time_s>=0),
 sequence INTEGER NOT NULL, state TEXT NOT NULL, context_json TEXT NOT NULL,
 PRIMARY KEY(object_id,time_s,sequence)
);
CREATE TABLE relations (
 relation_id INTEGER PRIMARY KEY, scenario_id TEXT NOT NULL REFERENCES scenarios,
 source_id TEXT NOT NULL, target_id TEXT NOT NULL, relation_type TEXT NOT NULL,
 valid_from_s REAL NOT NULL CHECK(valid_from_s>=0), valid_to_s REAL,
 basis_json TEXT NOT NULL, CHECK(valid_to_s IS NULL OR valid_to_s>valid_from_s),
 FOREIGN KEY(scenario_id,source_id) REFERENCES objects(scenario_id,object_id),
 FOREIGN KEY(scenario_id,target_id) REFERENCES objects(scenario_id,object_id)
);
CREATE TABLE assets (
 asset_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, relative_path TEXT NOT NULL,
 source_json TEXT NOT NULL
);
CREATE TABLE observations (
 observation_id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL REFERENCES scenarios,
 object_id TEXT NOT NULL, order_object_id TEXT, work_order_object_id TEXT, process_object_id TEXT,
 modality TEXT NOT NULL CHECK(modality IN ('signal','image','event')),
 time_s REAL NOT NULL CHECK(time_s>=0), sequence INTEGER NOT NULL,
 value REAL, unit TEXT, asset_id TEXT REFERENCES assets,
 payload_json TEXT NOT NULL, provenance_json TEXT NOT NULL,
 FOREIGN KEY(scenario_id,object_id) REFERENCES objects(scenario_id,object_id),
 FOREIGN KEY(scenario_id,order_object_id) REFERENCES objects(scenario_id,object_id),
 FOREIGN KEY(scenario_id,work_order_object_id) REFERENCES objects(scenario_id,object_id),
 FOREIGN KEY(scenario_id,process_object_id) REFERENCES objects(scenario_id,object_id),
 CHECK((modality='signal' AND value IS NOT NULL AND unit='A' AND asset_id IS NULL)
    OR (modality='image' AND asset_id IS NOT NULL AND value IS NULL)
    OR (modality='event' AND asset_id IS NULL AND value IS NULL))
);
CREATE INDEX observations_lookup ON observations(scenario_id,work_order_object_id,time_s,sequence);
CREATE INDEX equipment_lookup ON observations(scenario_id,object_id,time_s,sequence);
CREATE INDEX relations_time ON relations(scenario_id,valid_from_s,valid_to_s);
