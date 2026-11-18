-- finengine: создание 22 таблиц PostgreSQL.
-- Сверено с FINENGINE-DATABASE.md и SQLAlchemy-моделями 05.11.2026.
-- Выполнять в существующей базе aif_aef_metadb_dev от роли с правами DDL.
-- Схема: "aif-aef-agnbf-af-aifaouroboros-finengine-db".
-- Схема создаётся при отсутствии; имя с дефисами заключено в двойные кавычки.
-- Если схема уже существует, нужны права USAGE/CREATE в ней.
-- AIFA поддерживает дефисы и quoted search_path. В окружении службы задать:
-- FINENGINE_PG_SCHEMA=aif-aef-agnbf-af-aifaouroboros-finengine-db
-- Все девять FINENGINE_*_DATABASE_URL могут указывать на эту одну базу.
--
-- Это скрипт первичного создания, не миграция существующих таблиц.
-- CREATE без IF NOT EXISTS намеренно останавливается при совпадении имён.
-- При ошибке транзакция откатывается; существующие таблицы не удаляются.
-- В psql использовать --set ON_ERROR_STOP=1.
--
-- DEFAULT из SQLAlchemy default= выполняет приложение, поэтому серверные
-- DEFAULT для этих значений не добавлены. Исключение — генерация SERIAL ID.
-- При ручном INSERT необходимые NOT NULL значения нужно передавать явно.
-- Нет дополнительных FK, GIN-индексов, триггеров, расширений или тестовых данных.
-- CREATE DATABASE, роли и GRANT выполняются DBA отдельно по правилам контура.

BEGIN;
SET LOCAL lock_timeout = '10s';

DO $check_database$
BEGIN
    IF current_database() <> 'aif_aef_metadb_dev' THEN
        RAISE EXCEPTION 'Неверная база: %. Подключитесь к aif_aef_metadb_dev.', current_database();
    END IF;
END;
$check_database$;

CREATE SCHEMA IF NOT EXISTS "aif-aef-agnbf-af-aifaouroboros-finengine-db";
SET LOCAL search_path TO "aif-aef-agnbf-af-aifaouroboros-finengine-db";

-- users; источник: auth/db.py
CREATE TABLE users (
	id SERIAL NOT NULL, 
	username VARCHAR(64) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	is_admin BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	allowed_models VARCHAR(255) NOT NULL, 
	PRIMARY KEY (id)
);

-- invites; источник: auth/db.py
CREATE TABLE invites (
	code VARCHAR(32) NOT NULL, 
	created_by_id INTEGER NOT NULL, 
	used_by_id INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	used_at TIMESTAMP WITH TIME ZONE, 
	expires_at TIMESTAMP WITH TIME ZONE, 
	allowed_models VARCHAR(255) NOT NULL, 
	PRIMARY KEY (code), 
	FOREIGN KEY(created_by_id) REFERENCES users (id), 
	FOREIGN KEY(used_by_id) REFERENCES users (id)
);

-- document; источник: store/documents.py
CREATE TABLE document (
	doc_id VARCHAR(64) NOT NULL, 
	owner_id INTEGER NOT NULL, 
	meta JSON NOT NULL, 
	PRIMARY KEY (doc_id)
);

-- document_chunk; источник: store/documents.py
CREATE TABLE document_chunk (
	chunk_id VARCHAR(64) NOT NULL, 
	doc_id VARCHAR(64) NOT NULL, 
	payload JSON NOT NULL, 
	PRIMARY KEY (chunk_id)
);

-- dialog_owner; источник: store/dialogs.py
CREATE TABLE dialog_owner (
	user_id BIGINT NOT NULL, 
	last_project_id TEXT, 
	folders JSONB NOT NULL, 
	deleted JSONB NOT NULL, 
	saved_at TEXT, 
	PRIMARY KEY (user_id)
);

-- dialog; источник: store/dialogs.py
CREATE TABLE dialog (
	user_id BIGINT NOT NULL, 
	project_id TEXT NOT NULL, 
	payload JSONB NOT NULL, 
	updated_at BIGINT NOT NULL, 
	rev BIGINT NOT NULL, 
	PRIMARY KEY (user_id, project_id)
);

-- station_run; источник: store/runs.py
CREATE TABLE station_run (
	nonce TEXT NOT NULL, 
	kind TEXT NOT NULL, 
	status TEXT NOT NULL, 
	user_id BIGINT NOT NULL, 
	project_id TEXT NOT NULL, 
	session_id TEXT NOT NULL, 
	run_id TEXT NOT NULL, 
	doc_id TEXT, 
	payload JSONB, 
	screen JSONB, 
	error TEXT, 
	started_at BIGINT NOT NULL, 
	finished_at BIGINT NOT NULL, 
	rev BIGINT NOT NULL, 
	PRIMARY KEY (nonce)
);

-- state; источник: store/state.py
CREATE TABLE state (
	kind VARCHAR(32) NOT NULL, 
	owner_key VARCHAR(64) NOT NULL, 
	key VARCHAR(128) NOT NULL, 
	doc JSON NOT NULL, 
	PRIMARY KEY (kind, owner_key, key)
);

-- journal; источник: store/journals.py
CREATE TABLE journal (
	id BIGSERIAL NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	owner_key VARCHAR(64) NOT NULL, 
	key VARCHAR(128) NOT NULL, 
	ts FLOAT NOT NULL, 
	entry JSON NOT NULL, 
	PRIMARY KEY (id)
);

-- artifact; источник: store/artifacts.py
CREATE TABLE artifact (
	run_id VARCHAR(64) NOT NULL, 
	owner_key VARCHAR(64) NOT NULL, 
	project_key VARCHAR(128) NOT NULL, 
	created_at FLOAT NOT NULL, 
	meta JSON NOT NULL, 
	payload JSON NOT NULL, 
	PRIMARY KEY (run_id, owner_key, project_key)
);

-- analysis_case; источник: store/cases.py
CREATE TABLE analysis_case (
	owner_key VARCHAR(64) NOT NULL, 
	case_id VARCHAR(128) NOT NULL, 
	tenant_key VARCHAR(128), 
	revision INTEGER NOT NULL, 
	snapshot JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (owner_key, case_id)
);

-- analysis_case_document; источник: store/cases.py
CREATE TABLE analysis_case_document (
	owner_key VARCHAR(64) NOT NULL, 
	case_id VARCHAR(128) NOT NULL, 
	logical_name VARCHAR(512) NOT NULL, 
	document_version VARCHAR(128) NOT NULL, 
	document_id VARCHAR(128) NOT NULL, 
	active BOOLEAN NOT NULL, 
	added_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (owner_key, case_id, logical_name, document_version)
);

-- analysis_case_artifact; источник: store/cases.py
CREATE TABLE analysis_case_artifact (
	owner_key VARCHAR(64) NOT NULL, 
	case_id VARCHAR(128) NOT NULL, 
	artifact_id VARCHAR(128) NOT NULL, 
	output_type VARCHAR(128) NOT NULL, 
	node_key JSON NOT NULL, 
	concept VARCHAR(256), 
	entity_ref VARCHAR(512), 
	period_ref VARCHAR(256), 
	basis VARCHAR(512), 
	document_version VARCHAR(128), 
	policy_version VARCHAR(128), 
	passport_fingerprint VARCHAR(128), 
	evidence_ids JSON NOT NULL, 
	dependency_evidence_ids JSON NOT NULL, 
	payloads JSON NOT NULL, 
	valid BOOLEAN NOT NULL, 
	invalid_reason VARCHAR(512), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (owner_key, case_id, artifact_id)
);

-- analysis_plan_run; источник: store/cases.py
CREATE TABLE analysis_plan_run (
	owner_key VARCHAR(64) NOT NULL, 
	case_id VARCHAR(128) NOT NULL, 
	plan_run_id VARCHAR(128) NOT NULL, 
	record JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (owner_key, case_id, plan_run_id)
);

-- analysis_plan_node_run; источник: store/cases.py
CREATE TABLE analysis_plan_node_run (
	owner_key VARCHAR(64) NOT NULL, 
	case_id VARCHAR(128) NOT NULL, 
	plan_run_id VARCHAR(128) NOT NULL, 
	node_key_hash VARCHAR(64) NOT NULL, 
	node_key JSON NOT NULL, 
	node_run JSON NOT NULL, 
	evidence JSON NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (owner_key, case_id, plan_run_id, node_key_hash)
);

-- analysis_override_event; источник: store/cases.py
CREATE TABLE analysis_override_event (
	owner_key VARCHAR(64) NOT NULL, 
	case_id VARCHAR(128) NOT NULL, 
	event_id VARCHAR(64) NOT NULL, 
	root_evidence_id VARCHAR(128) NOT NULL, 
	case_revision INTEGER NOT NULL, 
	action VARCHAR(16) NOT NULL, 
	idempotency_key VARCHAR(128) NOT NULL, 
	event JSON NOT NULL, 
	wrapper JSON, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (owner_key, case_id, event_id), 
	CONSTRAINT uq_case_override_idempotency UNIQUE (owner_key, case_id, idempotency_key)
);

-- subject_profile_history; источник: store/cases.py
CREATE TABLE subject_profile_history (
	owner_key VARCHAR(64) NOT NULL, 
	subject_key VARCHAR(128) NOT NULL, 
	version INTEGER NOT NULL, 
	snapshot_id VARCHAR(128) NOT NULL, 
	plan_run_id VARCHAR(128) NOT NULL, 
	snapshot JSON NOT NULL, 
	serialized_bytes INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (owner_key, subject_key, version), 
	CONSTRAINT uq_subject_profile_plan_run UNIQUE (owner_key, subject_key, plan_run_id), 
	UNIQUE (snapshot_id)
);

-- scenarios; источник: skills/scenarios/store.py
CREATE TABLE scenarios (
	id VARCHAR(64) NOT NULL, 
	user_id INTEGER NOT NULL, 
	name TEXT NOT NULL, 
	kind TEXT NOT NULL, 
	instructions TEXT NOT NULL, 
	freq TEXT NOT NULL, 
	at_time TEXT NOT NULL, 
	dows TEXT NOT NULL, 
	paused INTEGER NOT NULL, 
	created_at FLOAT NOT NULL, 
	last_run_at FLOAT, 
	last_status TEXT NOT NULL, 
	last_run_id TEXT NOT NULL, 
	last_error TEXT NOT NULL, 
	description TEXT NOT NULL, 
	project_id TEXT NOT NULL, 
	model TEXT NOT NULL, 
	params TEXT NOT NULL, 
	PRIMARY KEY (id)
);

-- sources; источник: skills/news/db.py
CREATE TABLE sources (
	source_id VARCHAR(128) NOT NULL, 
	display_name TEXT NOT NULL, 
	source_type TEXT NOT NULL, 
	region TEXT NOT NULL, 
	url TEXT NOT NULL, 
	weight FLOAT NOT NULL, 
	active INTEGER NOT NULL, 
	last_fetched FLOAT, 
	PRIMARY KEY (source_id)
);

-- raw_items; источник: skills/news/db.py
CREATE TABLE raw_items (
	raw_hash VARCHAR(128) NOT NULL, 
	source_id VARCHAR(128) NOT NULL, 
	title TEXT NOT NULL, 
	snippet TEXT, 
	url TEXT NOT NULL, 
	published_at FLOAT NOT NULL, 
	fetched_at FLOAT NOT NULL, 
	matched_json TEXT, 
	PRIMARY KEY (raw_hash)
);

-- digest_runs; источник: skills/news/db.py
CREATE TABLE digest_runs (
	run_id VARCHAR(128) NOT NULL, 
	started_at FLOAT NOT NULL, 
	finished_at FLOAT, 
	profile TEXT NOT NULL, 
	items_raw INTEGER, 
	items_kept INTEGER, 
	llm_in INTEGER, 
	llm_out INTEGER, 
	cost_usd FLOAT, 
	error TEXT, 
	user_id INTEGER, 
	diag TEXT, 
	PRIMARY KEY (run_id)
);

-- digest_items; источник: skills/news/db.py
CREATE TABLE digest_items (
	id SERIAL NOT NULL, 
	run_id VARCHAR(128) NOT NULL, 
	raw_hash VARCHAR(128) NOT NULL, 
	is_relevant INTEGER NOT NULL, 
	category TEXT, 
	region TEXT, 
	importance FLOAT, 
	final_score FLOAT, 
	in_long_list INTEGER NOT NULL, 
	watchlist_match TEXT, 
	one_sentence TEXT, 
	why_important TEXT, 
	PRIMARY KEY (id)
);

-- Дополнительные индексы (индексы PK и UNIQUE-ограничений создаёт PostgreSQL).
CREATE UNIQUE INDEX ix_users_username ON users (username);
CREATE INDEX ix_document_owner_id ON document (owner_id);
CREATE INDEX ix_document_chunk_doc_id ON document_chunk (doc_id);
CREATE INDEX ix_station_run_doc ON station_run (user_id, doc_id, kind, finished_at);
CREATE INDEX ix_station_run_project ON station_run (user_id, project_id, started_at);
CREATE INDEX ix_journal_lookup ON journal (kind, owner_key, key);
CREATE INDEX ix_artifact_owner_key ON artifact (owner_key);
CREATE INDEX ix_artifact_project_key ON artifact (project_key);
CREATE INDEX ix_analysis_case_artifact_output_type ON analysis_case_artifact (output_type);
CREATE INDEX ix_analysis_override_event_root_evidence_id ON analysis_override_event (root_evidence_id);
CREATE INDEX ix_scenarios_user_id ON scenarios (user_id);
CREATE INDEX ix_raw_items_published_at ON raw_items (published_at);
CREATE INDEX ix_digest_items_run_id ON digest_items (run_id);

COMMIT;
