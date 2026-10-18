-- kiki-sim refuses to drop or create databases on a server without this table.
CREATE TABLE sim_marker (created_at TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO sim_marker DEFAULT VALUES;
