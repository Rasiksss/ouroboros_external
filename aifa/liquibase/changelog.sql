--liquibase formatted sql


--changeset finengine:create-table-users
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".users (
	id SERIAL NOT NULL,
	username VARCHAR(64) NOT NULL,
	password_hash VARCHAR(255) NOT NULL,
	is_admin BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	allowed_models VARCHAR(255) NOT NULL,
	PRIMARY KEY (id)
);
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".users;

--changeset finengine:create-table-invites
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".invites (
	code VARCHAR(32) NOT NULL,
	created_by_id INTEGER NOT NULL,
	used_by_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	used_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	allowed_models VARCHAR(255) NOT NULL,
	PRIMARY KEY (code),
	FOREIGN KEY(created_by_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".users (id),
	FOREIGN KEY(used_by_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".users (id)
);
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".invites;

--changeset finengine:create-table-document
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".document (
	doc_id VARCHAR(64) NOT NULL,
	owner_id INTEGER NOT NULL,
	meta JSON NOT NULL,
	PRIMARY KEY (doc_id)
);
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".document;

--changeset finengine:create-table-document-chunk
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".document_chunk (
	chunk_id VARCHAR(64) NOT NULL,
	doc_id VARCHAR(64) NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (chunk_id)
);
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".document_chunk;

--changeset finengine:create-table-dialog-owner
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".dialog_owner (
	user_id BIGINT NOT NULL,
	last_project_id TEXT,
	folders JSONB NOT NULL,
	deleted JSONB NOT NULL,
	saved_at TEXT,
	PRIMARY KEY (user_id)
);
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".dialog_owner;

--changeset finengine:create-table-dialog
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".dialog (
	user_id BIGINT NOT NULL,
	project_id TEXT NOT NULL,
	payload JSONB NOT NULL,
	updated_at BIGINT NOT NULL,
	rev BIGINT NOT NULL,
	PRIMARY KEY (user_id, project_id)
);
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".dialog;

--changeset finengine:create-table-station-run
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".station_run (
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
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".station_run;

--changeset finengine:create-table-state
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".state (
	kind VARCHAR(32) NOT NULL,
	owner_key VARCHAR(64) NOT NULL,
	key VARCHAR(128) NOT NULL,
	doc JSON NOT NULL,
	PRIMARY KEY (kind, owner_key, key)
);
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".state;

--changeset finengine:create-table-journal
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".journal (
	id BIGSERIAL NOT NULL,
	kind VARCHAR(32) NOT NULL,
	owner_key VARCHAR(64) NOT NULL,
	key VARCHAR(128) NOT NULL,
	ts FLOAT NOT NULL,
	entry JSON NOT NULL,
	PRIMARY KEY (id)
);
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".journal;

--changeset finengine:create-table-artifact
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".artifact (
	run_id VARCHAR(64) NOT NULL,
	owner_key VARCHAR(64) NOT NULL,
	project_key VARCHAR(128) NOT NULL,
	created_at FLOAT NOT NULL,
	meta JSON NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (run_id, owner_key, project_key)
);
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".artifact;

--changeset finengine:create-table-analysis-case
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".analysis_case (
	owner_key VARCHAR(64) NOT NULL,
	case_id VARCHAR(128) NOT NULL,
	tenant_key VARCHAR(128),
	revision INTEGER NOT NULL,
	snapshot JSON NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (owner_key, case_id)
);
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".analysis_case;

--changeset finengine:create-table-analysis-case-document
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".analysis_case_document (
	owner_key VARCHAR(64) NOT NULL,
	case_id VARCHAR(128) NOT NULL,
	logical_name VARCHAR(512) NOT NULL,
	document_version VARCHAR(128) NOT NULL,
	document_id VARCHAR(128) NOT NULL,
	active BOOLEAN NOT NULL,
	added_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (owner_key, case_id, logical_name, document_version)
);
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".analysis_case_document;

--changeset finengine:create-table-analysis-case-artifact
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".analysis_case_artifact (
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
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".analysis_case_artifact;

--changeset finengine:create-table-analysis-plan-run
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".analysis_plan_run (
	owner_key VARCHAR(64) NOT NULL,
	case_id VARCHAR(128) NOT NULL,
	plan_run_id VARCHAR(128) NOT NULL,
	record JSON NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (owner_key, case_id, plan_run_id)
);
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".analysis_plan_run;

--changeset finengine:create-table-analysis-plan-node-run
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".analysis_plan_node_run (
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
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".analysis_plan_node_run;

--changeset finengine:create-table-analysis-override-event
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".analysis_override_event (
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
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".analysis_override_event;

--changeset finengine:create-table-subject-profile-history
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".subject_profile_history (
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
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".subject_profile_history;

--changeset finengine:create-table-scenarios
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".scenarios (
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
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".scenarios;

--changeset finengine:create-table-sources
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".sources (
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
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".sources;

--changeset finengine:create-table-raw-items
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".raw_items (
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
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".raw_items;

--changeset finengine:create-table-digest-runs
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".digest_runs (
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
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".digest_runs;

--changeset finengine:create-table-digest-items
CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".digest_items (
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
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".digest_items;

--changeset finengine:create-indexes
CREATE UNIQUE INDEX ix_users_username ON "aif-aef-agnbf-af-aifaouroboros-db".users (username);
CREATE INDEX ix_document_owner_id ON "aif-aef-agnbf-af-aifaouroboros-db".document (owner_id);
CREATE INDEX ix_document_chunk_doc_id ON "aif-aef-agnbf-af-aifaouroboros-db".document_chunk (doc_id);
CREATE INDEX ix_station_run_doc ON "aif-aef-agnbf-af-aifaouroboros-db".station_run (user_id, doc_id, kind, finished_at);
CREATE INDEX ix_station_run_project ON "aif-aef-agnbf-af-aifaouroboros-db".station_run (user_id, project_id, started_at);
CREATE INDEX ix_journal_lookup ON "aif-aef-agnbf-af-aifaouroboros-db".journal (kind, owner_key, key);
CREATE INDEX ix_artifact_owner_key ON "aif-aef-agnbf-af-aifaouroboros-db".artifact (owner_key);
CREATE INDEX ix_artifact_project_key ON "aif-aef-agnbf-af-aifaouroboros-db".artifact (project_key);
CREATE INDEX ix_analysis_case_artifact_output_type ON "aif-aef-agnbf-af-aifaouroboros-db".analysis_case_artifact (output_type);
CREATE INDEX ix_analysis_override_event_root_evidence_id ON "aif-aef-agnbf-af-aifaouroboros-db".analysis_override_event (root_evidence_id);
CREATE INDEX ix_scenarios_user_id ON "aif-aef-agnbf-af-aifaouroboros-db".scenarios (user_id);
CREATE INDEX ix_raw_items_published_at ON "aif-aef-agnbf-af-aifaouroboros-db".raw_items (published_at);
CREATE INDEX ix_digest_items_run_id ON "aif-aef-agnbf-af-aifaouroboros-db".digest_items (run_id);
--rollback DROP INDEX IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".ix_users_username;
--rollback DROP INDEX IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".ix_document_owner_id;
--rollback DROP INDEX IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".ix_document_chunk_doc_id;
--rollback DROP INDEX IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".ix_station_run_doc;
--rollback DROP INDEX IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".ix_station_run_project;
--rollback DROP INDEX IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".ix_journal_lookup;
--rollback DROP INDEX IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".ix_artifact_owner_key;
--rollback DROP INDEX IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".ix_artifact_project_key;
--rollback DROP INDEX IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".ix_analysis_case_artifact_output_type;
--rollback DROP INDEX IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".ix_analysis_override_event_root_evidence_id;
--rollback DROP INDEX IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".ix_scenarios_user_id;
--rollback DROP INDEX IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".ix_raw_items_published_at;
--rollback DROP INDEX IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".ix_digest_items_run_id;


--changeset gigaagent:runtime-schema-0034
-- Initial runtime schema, equivalent to the former Alembic head 0034.
-- First deployment only: existing GigaAgent installations require a separate upgrade.
--
-- PostgreSQL database dump
--


-- Dumped from database version 16.15
-- Dumped by pg_dump version 16.15


--
-- Name: aif-aef-agnbf-af-aifaouroboros-db; Type: SCHEMA; Schema: -; Owner: -
--





--
-- Name: a2a_user_tokens; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".a2a_user_tokens (
    id text NOT NULL,
    a2a_agent_id text NOT NULL,
    agent_id text NOT NULL,
    owner_id text NOT NULL,
    token text NOT NULL,
    expires_at text NOT NULL
);


--
-- Name: agent_configuration_announcements; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".agent_configuration_announcements (
    config_hash text NOT NULL,
    component text DEFAULT ''::text NOT NULL,
    announced_at text DEFAULT ''::text NOT NULL
);


--
-- Name: agents; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".agents (
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    name text DEFAULT ''::text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at text DEFAULT ''::text NOT NULL,
    prompt text DEFAULT ''::text NOT NULL,
    CONSTRAINT ck_agents_prompt_length CHECK ((char_length(prompt) <= 8000))
);


--
-- Name: audit_events; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".audit_events (
    event_id text NOT NULL,
    "timestamp" text NOT NULL,
    correlation_id text DEFAULT ''::text NOT NULL,
    actor_type text DEFAULT ''::text NOT NULL,
    resource_type text DEFAULT ''::text NOT NULL,
    resource_id text DEFAULT ''::text NOT NULL,
    event_type text NOT NULL,
    decision text DEFAULT ''::text NOT NULL,
    reason text DEFAULT ''::text NOT NULL,
    metadata_json text DEFAULT '{}'::text NOT NULL,
    payload_json text NOT NULL,
    log_stream text DEFAULT 'events'::text NOT NULL,
    owner_id text DEFAULT 'global'::text NOT NULL,
    agent_id text DEFAULT 'main'::text NOT NULL
);


--
-- Name: chats; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".chats (
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    chat_id text NOT NULL,
    title text DEFAULT ''::text NOT NULL,
    title_source text DEFAULT 'provisional'::text NOT NULL,
    title_attempts integer DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at text DEFAULT ''::text NOT NULL,
    CONSTRAINT ck_chats_chat_id_present CHECK ((chat_id <> ''::text)),
    CONSTRAINT ck_chats_title_source CHECK ((title_source = ANY (ARRAY['provisional'::text, 'model'::text, 'owner'::text])))
);


--
-- Name: consolidation_leases; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".consolidation_leases (
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    holder_id text DEFAULT ''::text NOT NULL,
    lease_expires_at text DEFAULT ''::text NOT NULL,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    last_failure_at text DEFAULT ''::text NOT NULL,
    updated_at text DEFAULT ''::text NOT NULL
);


--
-- Name: context_summaries; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".context_summaries (
    summary_id text NOT NULL,
    source_offsets_ref text DEFAULT ''::text NOT NULL,
    summary_ref text DEFAULT ''::text NOT NULL,
    token_budget integer DEFAULT 0 NOT NULL,
    summary_hash text DEFAULT ''::text NOT NULL,
    created_at text NOT NULL,
    metadata_json text DEFAULT '{}'::text NOT NULL,
    payload_json text NOT NULL,
    owner_id text DEFAULT 'global'::text NOT NULL,
    agent_id text DEFAULT 'main'::text NOT NULL,
    chat_id text DEFAULT ''::text NOT NULL
);


--
-- Name: deployment_keyspace; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".deployment_keyspace (
    id text NOT NULL,
    backend text NOT NULL,
    bucket text NOT NULL,
    endpoint text DEFAULT ''::text NOT NULL,
    key_prefix text NOT NULL,
    recorded_at text NOT NULL
);


--
-- Name: file_transfer_leases; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".file_transfer_leases (
    token text NOT NULL,
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    process_id text NOT NULL,
    outgoing boolean NOT NULL,
    expires_at timestamp with time zone NOT NULL
);


--
-- Name: files; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".files (
    id text NOT NULL,
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    name text NOT NULL,
    storage_scope text NOT NULL,
    storage_key text NOT NULL,
    mime text NOT NULL,
    origin text NOT NULL,
    surface_id text NOT NULL,
    chat_id text NOT NULL,
    fingerprint text NOT NULL,
    producer_task text NOT NULL,
    artifact_path text NOT NULL,
    status text NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    folder_id text,
    expires_at text,
    claim_token text,
    claim_until text,
    size bigint NOT NULL,
    validation_version integer DEFAULT 0 NOT NULL,
    artifact_sequence bigint DEFAULT '0'::bigint NOT NULL,
    CONSTRAINT files_size_check CHECK ((size >= 0)),
    CONSTRAINT files_status_check CHECK ((status = ANY (ARRAY['staging'::text, 'draft'::text, 'ready'::text, 'deleting'::text, 'deleted'::text])))
);


--
-- Name: folders; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".folders (
    id text NOT NULL,
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    parent_id text,
    name text NOT NULL
);


--
-- Name: llm_usage; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".llm_usage (
    usage_id text NOT NULL,
    task_id text DEFAULT ''::text NOT NULL,
    session_id text DEFAULT ''::text NOT NULL,
    provider text DEFAULT ''::text NOT NULL,
    model text DEFAULT ''::text NOT NULL,
    prompt_tokens integer DEFAULT 0 NOT NULL,
    completion_tokens integer DEFAULT 0 NOT NULL,
    cached_tokens integer DEFAULT 0 NOT NULL,
    estimated_cost_usd double precision DEFAULT '0'::double precision NOT NULL,
    created_at text NOT NULL,
    metadata_json text DEFAULT '{}'::text NOT NULL,
    payload_json text NOT NULL,
    owner_id text DEFAULT 'global'::text NOT NULL,
    agent_id text DEFAULT 'main'::text NOT NULL
);


--
-- Name: mcp_user_tokens; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".mcp_user_tokens (
    id text NOT NULL,
    mcp_server_id text NOT NULL,
    agent_id text NOT NULL,
    owner_id text NOT NULL,
    token text NOT NULL,
    expires_at text NOT NULL
);


--
-- Name: memory; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".memory (
    id integer NOT NULL,
    owner_id text NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    source text DEFAULT ''::text NOT NULL,
    first_recorded_at double precision DEFAULT '0'::double precision NOT NULL,
    updated_at double precision DEFAULT '0'::double precision NOT NULL,
    last_recalled_at double precision DEFAULT '0'::double precision NOT NULL,
    recall_count integer DEFAULT 0 NOT NULL,
    agent_id text DEFAULT 'main'::text NOT NULL,
    embedding real[],
    embedding_key text DEFAULT ''::text NOT NULL,
    embedding_dimensions smallint DEFAULT '0'::smallint NOT NULL,
    CONSTRAINT ck_memory_status CHECK ((status = ANY (ARRAY['active'::text, 'archived'::text])))
);


--
-- Name: memory_id_seq; Type: SEQUENCE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE SEQUENCE "aif-aef-agnbf-af-aifaouroboros-db".memory_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: memory_id_seq; Type: SEQUENCE OWNED BY; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER SEQUENCE "aif-aef-agnbf-af-aifaouroboros-db".memory_id_seq OWNED BY "aif-aef-agnbf-af-aifaouroboros-db".memory.id;


--
-- Name: message_files; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".message_files (
    file_id text NOT NULL,
    message_id text NOT NULL,
    owner_id text NOT NULL,
    agent_id text NOT NULL
);


--
-- Name: messages; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".messages (
    message_id text NOT NULL,
    session_id text DEFAULT ''::text NOT NULL,
    source text DEFAULT 'unknown'::text NOT NULL,
    source_message_id text DEFAULT ''::text NOT NULL,
    actor_type text NOT NULL,
    channel text DEFAULT ''::text NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    content_ref text DEFAULT ''::text NOT NULL,
    content_hash text DEFAULT ''::text NOT NULL,
    created_at text NOT NULL,
    metadata_json text DEFAULT '{}'::text NOT NULL,
    payload_json text NOT NULL,
    chat_history_visible boolean DEFAULT true NOT NULL,
    owner_id text DEFAULT 'global'::text NOT NULL,
    agent_id text DEFAULT 'main'::text NOT NULL,
    dedup_key text DEFAULT ''::text NOT NULL,
    chat_id text DEFAULT ''::text NOT NULL,
    inbound_unprocessed boolean DEFAULT false NOT NULL
);


--
-- Name: owner_runtime_state; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".owner_runtime_state (
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    payload_json text NOT NULL,
    updated_at text NOT NULL
);


--
-- Name: owner_skill_bindings; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".owner_skill_bindings (
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    skill_id text NOT NULL,
    revision_id text NOT NULL
);


--
-- Name: owner_skill_state; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".owner_skill_state (
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    state_version bigint DEFAULT 0 NOT NULL
);


--
-- Name: personal_a2a_agents; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".personal_a2a_agents (
    id text NOT NULL,
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    remote_agent_id text NOT NULL,
    entry text NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    static_token text,
    revision integer NOT NULL,
    creation_key text,
    creation_fingerprint text,
    discovery_revision integer,
    snapshot text,
    lease_holder text,
    lease_until timestamp with time zone
);


--
-- Name: personal_a2a_user_tokens; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".personal_a2a_user_tokens (
    id text NOT NULL,
    personal_agent_id text NOT NULL,
    agent_id text NOT NULL,
    owner_id text NOT NULL,
    origin text NOT NULL,
    token text NOT NULL,
    expires_at text NOT NULL
);


--
-- Name: personal_mcp_servers; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".personal_mcp_servers (
    id text NOT NULL,
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    server_id text NOT NULL,
    entry text NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    static_token text,
    revision integer NOT NULL,
    creation_key text,
    creation_fingerprint text,
    discovery_revision integer,
    snapshot text,
    lease_holder text,
    lease_until timestamp with time zone
);


--
-- Name: personal_mcp_user_tokens; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".personal_mcp_user_tokens (
    id text NOT NULL,
    personal_server_id text NOT NULL,
    agent_id text NOT NULL,
    owner_id text NOT NULL,
    origin text NOT NULL,
    token text NOT NULL,
    expires_at text NOT NULL
);


--
-- Name: schedule_runs; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".schedule_runs (
    schedule_id text NOT NULL,
    slot_id text NOT NULL,
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    schedule_revision integer NOT NULL,
    status text NOT NULL,
    claimed_at text NOT NULL,
    finished_at text NOT NULL,
    error text NOT NULL,
    permanent integer DEFAULT 0 NOT NULL
);


--
-- Name: schedules; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".schedules (
    schedule_id text NOT NULL,
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    revision integer NOT NULL,
    state text NOT NULL,
    next_run_at text NOT NULL,
    payload_json text NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    chat_id text DEFAULT ''::text NOT NULL
);


--
-- Name: scratchpad_blocks; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".scratchpad_blocks (
    id integer NOT NULL,
    owner_id text NOT NULL,
    ts text NOT NULL,
    source text DEFAULT 'task'::text NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    agent_id text DEFAULT 'main'::text NOT NULL,
    job_id text DEFAULT ''::text NOT NULL,
    surface_id text DEFAULT ''::text NOT NULL,
    chat_id text DEFAULT ''::text NOT NULL
);


--
-- Name: scratchpad_blocks_id_seq; Type: SEQUENCE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE SEQUENCE "aif-aef-agnbf-af-aifaouroboros-db".scratchpad_blocks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: scratchpad_blocks_id_seq; Type: SEQUENCE OWNED BY; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER SEQUENCE "aif-aef-agnbf-af-aifaouroboros-db".scratchpad_blocks_id_seq OWNED BY "aif-aef-agnbf-af-aifaouroboros-db".scratchpad_blocks.id;


--
-- Name: security_contexts; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".security_contexts (
    security_token text NOT NULL,
    param_type text NOT NULL,
    param_name text NOT NULL,
    param_value text NOT NULL,
    expires_at text NOT NULL
);


--
-- Name: sessions; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".sessions (
    session_id text NOT NULL,
    source text NOT NULL,
    owner_external_id text DEFAULT ''::text NOT NULL,
    auth_context_hash text DEFAULT ''::text NOT NULL,
    status text NOT NULL,
    created_at text NOT NULL,
    last_seen_at text NOT NULL,
    expires_at text DEFAULT ''::text NOT NULL,
    payload_json text NOT NULL
);


--
-- Name: skill_activations; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".skill_activations (
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    skill_name text NOT NULL,
    activated_at text NOT NULL
);


--
-- Name: skill_artifact_objects; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".skill_artifact_objects (
    digest text NOT NULL,
    object_key text NOT NULL,
    state text NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT skill_artifact_objects_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'ready'::text, 'deleting'::text])))
);


--
-- Name: skill_binding_snapshots; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".skill_binding_snapshots (
    snapshot_key text NOT NULL,
    record jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


--
-- Name: skill_catalogue_cards; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".skill_catalogue_cards (
    snapshot_id text NOT NULL,
    "position" integer NOT NULL,
    card jsonb NOT NULL
);


--
-- Name: skill_catalogue_loads; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".skill_catalogue_loads (
    query_hash text NOT NULL,
    token text NOT NULL,
    expires_at timestamp with time zone NOT NULL
);


--
-- Name: skill_catalogue_snapshots; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".skill_catalogue_snapshots (
    snapshot_id text NOT NULL,
    query_hash text NOT NULL,
    total integer NOT NULL,
    byte_size integer NOT NULL,
    created_at timestamp with time zone DEFAULT statement_timestamp() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    fetched_at timestamp with time zone
);


--
-- Name: skill_command_receipts; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".skill_command_receipts (
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    result jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


--
-- Name: skill_registry_policy; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".skill_registry_policy (
    keyspace text NOT NULL,
    digest text NOT NULL
);


--
-- Name: skill_revisions; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".skill_revisions (
    revision_id text NOT NULL,
    skill_id text NOT NULL,
    upstream_revision text NOT NULL,
    fingerprint text NOT NULL,
    digest text NOT NULL,
    normalizer_version integer NOT NULL,
    validation_version integer NOT NULL,
    kind text NOT NULL,
    record jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT skill_revisions_kind_check CHECK ((kind = ANY (ARRAY['instruction'::text, 'executable'::text])))
);


--
-- Name: skills; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".skills (
    skill_id text NOT NULL,
    source_id text NOT NULL,
    external_id text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: task_episodes; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".task_episodes (
    episode_id text NOT NULL,
    task_id text DEFAULT ''::text NOT NULL,
    created_at text NOT NULL,
    user_text text DEFAULT ''::text NOT NULL,
    answer_preview text DEFAULT ''::text NOT NULL,
    tools_used_json text DEFAULT '[]'::text NOT NULL,
    domains_json text DEFAULT '[]'::text NOT NULL,
    entities_json text DEFAULT '{}'::text NOT NULL,
    refs_json text DEFAULT '{}'::text NOT NULL,
    search_terms_json text DEFAULT '[]'::text NOT NULL,
    payload_json text DEFAULT '{}'::text NOT NULL,
    expires_at text DEFAULT ''::text NOT NULL,
    owner_id text DEFAULT 'global'::text NOT NULL,
    agent_id text DEFAULT 'main'::text NOT NULL,
    surface_id text DEFAULT 'skill-bridge'::text NOT NULL,
    chat_id text DEFAULT ''::text NOT NULL
);


--
-- Name: task_inputs; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".task_inputs (
    sequence bigint NOT NULL,
    input_id text NOT NULL,
    client_input_id text NOT NULL,
    task_id text NOT NULL,
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    surface_id text NOT NULL,
    chat_id text NOT NULL,
    payload text NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    revision integer DEFAULT 0 NOT NULL,
    operation_id text DEFAULT ''::text NOT NULL,
    dispatched boolean DEFAULT false NOT NULL,
    received_at text NOT NULL,
    message_id text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT ck_task_inputs_state CHECK ((state = ANY (ARRAY['pending'::text, 'editing'::text, 'committed'::text, 'cancelled'::text])))
);


--
-- Name: task_inputs_sequence_seq; Type: SEQUENCE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE "aif-aef-agnbf-af-aifaouroboros-db".task_inputs ALTER COLUMN sequence ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME "aif-aef-agnbf-af-aifaouroboros-db".task_inputs_sequence_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: task_plans; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".task_plans (
    root_task_id text NOT NULL,
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    surface_id text NOT NULL,
    chat_id text NOT NULL,
    state_encrypted text NOT NULL
);


--
-- Name: task_skill_binding_revisions; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".task_skill_binding_revisions (
    task_id text NOT NULL,
    skill_id text NOT NULL,
    revision_id text NOT NULL
);


--
-- Name: task_skill_bindings; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".task_skill_bindings (
    task_id text NOT NULL,
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    scope jsonb NOT NULL,
    snapshot_key text NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


--
-- Name: tasks; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".tasks (
    task_id text NOT NULL,
    session_id text DEFAULT ''::text NOT NULL,
    source text DEFAULT 'unknown'::text NOT NULL,
    correlation_id text DEFAULT ''::text NOT NULL,
    status text NOT NULL,
    task_type text DEFAULT 'task'::text NOT NULL,
    chat_id text DEFAULT ''::text NOT NULL,
    input_json text DEFAULT '{}'::text NOT NULL,
    input_ref text DEFAULT ''::text NOT NULL,
    result text DEFAULT ''::text NOT NULL,
    result_ref text DEFAULT ''::text NOT NULL,
    error_code text DEFAULT ''::text NOT NULL,
    priority integer DEFAULT 1 NOT NULL,
    attempt integer DEFAULT 1 NOT NULL,
    worker_id integer,
    queued_at text DEFAULT ''::text NOT NULL,
    created_at text NOT NULL,
    started_at text DEFAULT ''::text NOT NULL,
    heartbeat_at text DEFAULT ''::text NOT NULL,
    finished_at text DEFAULT ''::text NOT NULL,
    metadata_json text DEFAULT '{}'::text NOT NULL,
    payload_json text NOT NULL,
    owner_id text DEFAULT 'global'::text NOT NULL,
    agent_id text DEFAULT 'main'::text NOT NULL,
    runtime_id text,
    stop_requested_at text,
    parent_task_id text DEFAULT ''::text NOT NULL,
    soft_notice_at text,
    job_id text DEFAULT ''::text NOT NULL,
    lineage_ordinal integer DEFAULT 0 NOT NULL,
    detached boolean DEFAULT false NOT NULL,
    terminal_published_at text,
    terminal_publish_claim text,
    terminal_publish_after text,
    surface_id text DEFAULT ''::text NOT NULL,
    input_open boolean DEFAULT false NOT NULL,
    input_claim text DEFAULT ''::text NOT NULL
);


--
-- Name: tool_confirmations; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".tool_confirmations (
    confirmation_id text NOT NULL,
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    surface_id text NOT NULL,
    chat_id text NOT NULL,
    task_id text DEFAULT ''::text NOT NULL,
    tool_name text NOT NULL,
    call_ordinal integer DEFAULT 0 NOT NULL,
    round_idx integer DEFAULT 0 NOT NULL,
    args_hash text DEFAULT ''::text NOT NULL,
    run_state_json text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    created_at text NOT NULL,
    decided_at text DEFAULT ''::text NOT NULL,
    applied_at text DEFAULT ''::text NOT NULL
);


--
-- Name: tool_invocations; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".tool_invocations (
    tool_invocation_id text NOT NULL,
    task_id text DEFAULT ''::text NOT NULL,
    tool_name text NOT NULL,
    status text DEFAULT ''::text NOT NULL,
    args_redacted_json text DEFAULT '{}'::text NOT NULL,
    result_summary_json text DEFAULT '{}'::text NOT NULL,
    result_index_json text DEFAULT '{}'::text NOT NULL,
    search_terms_json text DEFAULT '[]'::text NOT NULL,
    payload_json text DEFAULT '{}'::text NOT NULL,
    payload_hash text DEFAULT ''::text NOT NULL,
    sensitivity text DEFAULT 'normal'::text NOT NULL,
    retention_class text DEFAULT 'summary'::text NOT NULL,
    created_at text NOT NULL,
    expires_at text DEFAULT ''::text NOT NULL,
    owner_id text DEFAULT 'global'::text NOT NULL,
    agent_id text DEFAULT 'main'::text NOT NULL,
    surface_id text DEFAULT 'skill-bridge'::text NOT NULL,
    chat_id text DEFAULT ''::text NOT NULL
);


--
-- Name: user_settings; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".user_settings (
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    key text NOT NULL,
    value text NOT NULL
);


--
-- Name: workstation_sessions; Type: TABLE; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".workstation_sessions (
    owner_id text NOT NULL,
    agent_id text NOT NULL,
    chat_id text NOT NULL,
    session_id text NOT NULL,
    token_encrypted text NOT NULL
);


--
-- Name: memory id; Type: DEFAULT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".memory ALTER COLUMN id SET DEFAULT nextval('"aif-aef-agnbf-af-aifaouroboros-db".memory_id_seq'::regclass);


--
-- Name: scratchpad_blocks id; Type: DEFAULT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".scratchpad_blocks ALTER COLUMN id SET DEFAULT nextval('"aif-aef-agnbf-af-aifaouroboros-db".scratchpad_blocks_id_seq'::regclass);


--
-- Name: agent_configuration_announcements agent_configuration_announcements_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".agent_configuration_announcements
    ADD CONSTRAINT agent_configuration_announcements_pkey PRIMARY KEY (config_hash);


--
-- Name: agents agents_rebuild_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".agents
    ADD CONSTRAINT agents_rebuild_pkey PRIMARY KEY (owner_id, agent_id);


--
-- Name: audit_events audit_events_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".audit_events
    ADD CONSTRAINT audit_events_pkey PRIMARY KEY (event_id);


--
-- Name: chats chats_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".chats
    ADD CONSTRAINT chats_pkey PRIMARY KEY (owner_id, agent_id, chat_id);


--
-- Name: consolidation_leases consolidation_leases_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".consolidation_leases
    ADD CONSTRAINT consolidation_leases_pkey PRIMARY KEY (owner_id, agent_id);


--
-- Name: context_summaries context_summaries_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".context_summaries
    ADD CONSTRAINT context_summaries_pkey PRIMARY KEY (summary_id);


--
-- Name: deployment_keyspace deployment_keyspace_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".deployment_keyspace
    ADD CONSTRAINT deployment_keyspace_pkey PRIMARY KEY (id);


--
-- Name: file_transfer_leases file_transfer_leases_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".file_transfer_leases
    ADD CONSTRAINT file_transfer_leases_pkey PRIMARY KEY (token);


--
-- Name: files files_owner_id_agent_id_id_key; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".files
    ADD CONSTRAINT files_owner_id_agent_id_id_key UNIQUE (owner_id, agent_id, id);


--
-- Name: files files_owner_id_agent_id_storage_scope_storage_key_key; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".files
    ADD CONSTRAINT files_owner_id_agent_id_storage_scope_storage_key_key UNIQUE (owner_id, agent_id, storage_scope, storage_key);


--
-- Name: files files_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".files
    ADD CONSTRAINT files_pkey PRIMARY KEY (id);


--
-- Name: folders folders_owner_id_agent_id_id_key; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".folders
    ADD CONSTRAINT folders_owner_id_agent_id_id_key UNIQUE (owner_id, agent_id, id);


--
-- Name: folders folders_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".folders
    ADD CONSTRAINT folders_pkey PRIMARY KEY (id);


--
-- Name: llm_usage llm_usage_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".llm_usage
    ADD CONSTRAINT llm_usage_pkey PRIMARY KEY (usage_id);


--
-- Name: memory memory_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".memory
    ADD CONSTRAINT memory_pkey PRIMARY KEY (id);


--
-- Name: message_files message_files_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".message_files
    ADD CONSTRAINT message_files_pkey PRIMARY KEY (file_id, message_id);


--
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (message_id);


--
-- Name: owner_runtime_state owner_runtime_state_pkey1; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".owner_runtime_state
    ADD CONSTRAINT owner_runtime_state_pkey1 PRIMARY KEY (owner_id, agent_id);


--
-- Name: owner_skill_bindings owner_skill_bindings_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".owner_skill_bindings
    ADD CONSTRAINT owner_skill_bindings_pkey PRIMARY KEY (owner_id, agent_id, skill_id);


--
-- Name: owner_skill_state owner_skill_state_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".owner_skill_state
    ADD CONSTRAINT owner_skill_state_pkey PRIMARY KEY (owner_id, agent_id);


--
-- Name: a2a_user_tokens pk_a2a_user_tokens; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".a2a_user_tokens
    ADD CONSTRAINT pk_a2a_user_tokens PRIMARY KEY (id);


--
-- Name: mcp_user_tokens pk_mcp_user_tokens; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".mcp_user_tokens
    ADD CONSTRAINT pk_mcp_user_tokens PRIMARY KEY (id);


--
-- Name: personal_a2a_agents pk_personal_a2a_agents; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".personal_a2a_agents
    ADD CONSTRAINT pk_personal_a2a_agents PRIMARY KEY (id);


--
-- Name: personal_a2a_user_tokens pk_personal_a2a_user_tokens; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".personal_a2a_user_tokens
    ADD CONSTRAINT pk_personal_a2a_user_tokens PRIMARY KEY (id);


--
-- Name: personal_mcp_servers pk_personal_mcp_servers; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".personal_mcp_servers
    ADD CONSTRAINT pk_personal_mcp_servers PRIMARY KEY (id);


--
-- Name: personal_mcp_user_tokens pk_personal_mcp_user_tokens; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".personal_mcp_user_tokens
    ADD CONSTRAINT pk_personal_mcp_user_tokens PRIMARY KEY (id);


--
-- Name: schedule_runs pk_schedule_runs; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".schedule_runs
    ADD CONSTRAINT pk_schedule_runs PRIMARY KEY (schedule_id, slot_id);


--
-- Name: security_contexts pk_security_contexts; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".security_contexts
    ADD CONSTRAINT pk_security_contexts PRIMARY KEY (security_token, param_type, param_name);


--
-- Name: schedules schedules_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".schedules
    ADD CONSTRAINT schedules_pkey PRIMARY KEY (schedule_id);


--
-- Name: scratchpad_blocks scratchpad_blocks_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".scratchpad_blocks
    ADD CONSTRAINT scratchpad_blocks_pkey PRIMARY KEY (id);


--
-- Name: sessions sessions_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (session_id);


--
-- Name: skill_activations skill_activations_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_activations
    ADD CONSTRAINT skill_activations_pkey PRIMARY KEY (owner_id, agent_id, skill_name);


--
-- Name: skill_artifact_objects skill_artifact_objects_object_key_key; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_artifact_objects
    ADD CONSTRAINT skill_artifact_objects_object_key_key UNIQUE (object_key);


--
-- Name: skill_artifact_objects skill_artifact_objects_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_artifact_objects
    ADD CONSTRAINT skill_artifact_objects_pkey PRIMARY KEY (digest);


--
-- Name: skill_binding_snapshots skill_binding_snapshots_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_binding_snapshots
    ADD CONSTRAINT skill_binding_snapshots_pkey PRIMARY KEY (snapshot_key);


--
-- Name: skill_catalogue_cards skill_catalogue_cards_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_catalogue_cards
    ADD CONSTRAINT skill_catalogue_cards_pkey PRIMARY KEY (snapshot_id, "position");


--
-- Name: skill_catalogue_loads skill_catalogue_loads_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_catalogue_loads
    ADD CONSTRAINT skill_catalogue_loads_pkey PRIMARY KEY (query_hash);


--
-- Name: skill_catalogue_snapshots skill_catalogue_snapshots_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_catalogue_snapshots
    ADD CONSTRAINT skill_catalogue_snapshots_pkey PRIMARY KEY (snapshot_id);


--
-- Name: skill_command_receipts skill_command_receipts_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_command_receipts
    ADD CONSTRAINT skill_command_receipts_pkey PRIMARY KEY (owner_id, agent_id, idempotency_key);


--
-- Name: skill_registry_policy skill_registry_policy_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_registry_policy
    ADD CONSTRAINT skill_registry_policy_pkey PRIMARY KEY (keyspace);


--
-- Name: skill_revisions skill_revisions_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_revisions
    ADD CONSTRAINT skill_revisions_pkey PRIMARY KEY (revision_id);


--
-- Name: skill_revisions skill_revisions_skill_id_fingerprint_key; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_revisions
    ADD CONSTRAINT skill_revisions_skill_id_fingerprint_key UNIQUE (skill_id, fingerprint);


--
-- Name: skill_revisions skill_revisions_skill_id_revision_id_key; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_revisions
    ADD CONSTRAINT skill_revisions_skill_id_revision_id_key UNIQUE (skill_id, revision_id);


--
-- Name: skill_revisions skill_revisions_skill_id_upstream_revision_normalizer_versi_key; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_revisions
    ADD CONSTRAINT skill_revisions_skill_id_upstream_revision_normalizer_versi_key UNIQUE (skill_id, upstream_revision, normalizer_version, validation_version);


--
-- Name: skills skills_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skills
    ADD CONSTRAINT skills_pkey PRIMARY KEY (skill_id);


--
-- Name: skills skills_source_id_external_id_key; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skills
    ADD CONSTRAINT skills_source_id_external_id_key UNIQUE (source_id, external_id);


--
-- Name: task_episodes task_episodes_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".task_episodes
    ADD CONSTRAINT task_episodes_pkey PRIMARY KEY (episode_id);


--
-- Name: task_inputs task_inputs_input_id_key; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".task_inputs
    ADD CONSTRAINT task_inputs_input_id_key UNIQUE (input_id);


--
-- Name: task_inputs task_inputs_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".task_inputs
    ADD CONSTRAINT task_inputs_pkey PRIMARY KEY (sequence);


--
-- Name: task_plans task_plans_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".task_plans
    ADD CONSTRAINT task_plans_pkey PRIMARY KEY (root_task_id);


--
-- Name: task_skill_binding_revisions task_skill_binding_revisions_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".task_skill_binding_revisions
    ADD CONSTRAINT task_skill_binding_revisions_pkey PRIMARY KEY (task_id, skill_id);


--
-- Name: task_skill_bindings task_skill_bindings_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".task_skill_bindings
    ADD CONSTRAINT task_skill_bindings_pkey PRIMARY KEY (task_id);


--
-- Name: tasks tasks_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (task_id);


--
-- Name: tool_confirmations tool_confirmations_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".tool_confirmations
    ADD CONSTRAINT tool_confirmations_pkey PRIMARY KEY (confirmation_id);


--
-- Name: tool_invocations tool_invocations_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".tool_invocations
    ADD CONSTRAINT tool_invocations_pkey PRIMARY KEY (tool_invocation_id);


--
-- Name: a2a_user_tokens uq_a2a_user_tokens_remote_owner_agent; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".a2a_user_tokens
    ADD CONSTRAINT uq_a2a_user_tokens_remote_owner_agent UNIQUE (a2a_agent_id, owner_id, agent_id);


--
-- Name: context_summaries uq_context_summaries_owner_agent_chat; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".context_summaries
    ADD CONSTRAINT uq_context_summaries_owner_agent_chat UNIQUE (owner_id, agent_id, chat_id);


--
-- Name: mcp_user_tokens uq_mcp_user_tokens_server_owner_agent; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".mcp_user_tokens
    ADD CONSTRAINT uq_mcp_user_tokens_server_owner_agent UNIQUE (mcp_server_id, owner_id, agent_id);


--
-- Name: memory uq_memory_owner_agent_name; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".memory
    ADD CONSTRAINT uq_memory_owner_agent_name UNIQUE (owner_id, agent_id, name);


--
-- Name: personal_a2a_agents uq_personal_a2a_agents_owner_agent_creation; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".personal_a2a_agents
    ADD CONSTRAINT uq_personal_a2a_agents_owner_agent_creation UNIQUE (owner_id, agent_id, creation_key);


--
-- Name: personal_a2a_agents uq_personal_a2a_agents_owner_agent_remote; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".personal_a2a_agents
    ADD CONSTRAINT uq_personal_a2a_agents_owner_agent_remote UNIQUE (owner_id, agent_id, remote_agent_id);


--
-- Name: personal_a2a_user_tokens uq_personal_a2a_user_tokens_agent_owner_agent; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".personal_a2a_user_tokens
    ADD CONSTRAINT uq_personal_a2a_user_tokens_agent_owner_agent UNIQUE (personal_agent_id, owner_id, agent_id);


--
-- Name: personal_mcp_servers uq_personal_mcp_servers_owner_agent_creation; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".personal_mcp_servers
    ADD CONSTRAINT uq_personal_mcp_servers_owner_agent_creation UNIQUE (owner_id, agent_id, creation_key);


--
-- Name: personal_mcp_servers uq_personal_mcp_servers_owner_agent_server; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".personal_mcp_servers
    ADD CONSTRAINT uq_personal_mcp_servers_owner_agent_server UNIQUE (owner_id, agent_id, server_id);


--
-- Name: personal_mcp_user_tokens uq_personal_mcp_user_tokens_server_owner_agent; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".personal_mcp_user_tokens
    ADD CONSTRAINT uq_personal_mcp_user_tokens_server_owner_agent UNIQUE (personal_server_id, owner_id, agent_id);


--
-- Name: task_inputs uq_task_inputs_client; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".task_inputs
    ADD CONSTRAINT uq_task_inputs_client UNIQUE (owner_id, agent_id, surface_id, chat_id, client_input_id);


--
-- Name: user_settings user_settings_rebuild_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".user_settings
    ADD CONSTRAINT user_settings_rebuild_pkey PRIMARY KEY (owner_id, agent_id, key);


--
-- Name: workstation_sessions workstation_sessions_pkey; Type: CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".workstation_sessions
    ADD CONSTRAINT workstation_sessions_pkey PRIMARY KEY (owner_id, agent_id, chat_id);


--
-- Name: idx_a2a_user_tokens_owner_agent; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_a2a_user_tokens_owner_agent ON "aif-aef-agnbf-af-aifaouroboros-db".a2a_user_tokens USING btree (owner_id, agent_id);


--
-- Name: idx_audit_events_owner; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_audit_events_owner ON "aif-aef-agnbf-af-aifaouroboros-db".audit_events USING btree (owner_id, agent_id, "timestamp");


--
-- Name: idx_audit_events_resource; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_audit_events_resource ON "aif-aef-agnbf-af-aifaouroboros-db".audit_events USING btree (resource_type, resource_id, "timestamp");


--
-- Name: idx_audit_events_stream; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_audit_events_stream ON "aif-aef-agnbf-af-aifaouroboros-db".audit_events USING btree (log_stream, "timestamp");


--
-- Name: idx_audit_events_type; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_audit_events_type ON "aif-aef-agnbf-af-aifaouroboros-db".audit_events USING btree (event_type, "timestamp");


--
-- Name: idx_file_transfers_expiry; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_file_transfers_expiry ON "aif-aef-agnbf-af-aifaouroboros-db".file_transfer_leases USING btree (expires_at);


--
-- Name: idx_file_transfers_owner; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_file_transfers_owner ON "aif-aef-agnbf-af-aifaouroboros-db".file_transfer_leases USING btree (owner_id, agent_id, expires_at);


--
-- Name: idx_file_transfers_process; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_file_transfers_process ON "aif-aef-agnbf-af-aifaouroboros-db".file_transfer_leases USING btree (process_id, expires_at);


--
-- Name: idx_files_artifact; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_files_artifact ON "aif-aef-agnbf-af-aifaouroboros-db".files USING btree (owner_id, agent_id, producer_task, artifact_path, artifact_sequence, id);


--
-- Name: idx_files_cleanup; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_files_cleanup ON "aif-aef-agnbf-af-aifaouroboros-db".files USING btree (status, expires_at, claim_until, id);


--
-- Name: idx_files_page; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_files_page ON "aif-aef-agnbf-af-aifaouroboros-db".files USING btree (owner_id, agent_id, folder_id, status, name, id);


--
-- Name: idx_folders_page; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_folders_page ON "aif-aef-agnbf-af-aifaouroboros-db".folders USING btree (owner_id, agent_id, parent_id, name, id);


--
-- Name: idx_llm_usage_owner; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_llm_usage_owner ON "aif-aef-agnbf-af-aifaouroboros-db".llm_usage USING btree (owner_id, agent_id, created_at);


--
-- Name: idx_llm_usage_session; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_llm_usage_session ON "aif-aef-agnbf-af-aifaouroboros-db".llm_usage USING btree (session_id, created_at);


--
-- Name: idx_llm_usage_task; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_llm_usage_task ON "aif-aef-agnbf-af-aifaouroboros-db".llm_usage USING btree (task_id, created_at);


--
-- Name: idx_memory_owner_status_updated; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_memory_owner_status_updated ON "aif-aef-agnbf-af-aifaouroboros-db".memory USING btree (owner_id, agent_id, status, updated_at DESC, name);


--
-- Name: idx_message_files_message; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_message_files_message ON "aif-aef-agnbf-af-aifaouroboros-db".message_files USING btree (message_id, file_id);


--
-- Name: idx_messages_inbound_unprocessed; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_messages_inbound_unprocessed ON "aif-aef-agnbf-af-aifaouroboros-db".messages USING btree (owner_id, agent_id, source, created_at) WHERE inbound_unprocessed;


--
-- Name: idx_messages_owner_agent_chat_created; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_messages_owner_agent_chat_created ON "aif-aef-agnbf-af-aifaouroboros-db".messages USING btree (owner_id, agent_id, chat_id, created_at);


--
-- Name: idx_messages_owner_agent_chat_visible_created; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_messages_owner_agent_chat_visible_created ON "aif-aef-agnbf-af-aifaouroboros-db".messages USING btree (owner_id, agent_id, chat_id, chat_history_visible, created_at);


--
-- Name: idx_messages_session_created; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_messages_session_created ON "aif-aef-agnbf-af-aifaouroboros-db".messages USING btree (session_id, created_at);


--
-- Name: idx_owner_skill_bindings_revision; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_owner_skill_bindings_revision ON "aif-aef-agnbf-af-aifaouroboros-db".owner_skill_bindings USING btree (revision_id);


--
-- Name: idx_personal_a2a_user_tokens_owner_agent; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_personal_a2a_user_tokens_owner_agent ON "aif-aef-agnbf-af-aifaouroboros-db".personal_a2a_user_tokens USING btree (owner_id, agent_id);


--
-- Name: idx_personal_mcp_user_tokens_owner_agent; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_personal_mcp_user_tokens_owner_agent ON "aif-aef-agnbf-af-aifaouroboros-db".personal_mcp_user_tokens USING btree (owner_id, agent_id);


--
-- Name: idx_schedule_runs_claim; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_schedule_runs_claim ON "aif-aef-agnbf-af-aifaouroboros-db".schedule_runs USING btree (schedule_id, claimed_at) WHERE (status = 'claimed'::text);


--
-- Name: idx_schedule_runs_settled; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_schedule_runs_settled ON "aif-aef-agnbf-af-aifaouroboros-db".schedule_runs USING btree (finished_at);


--
-- Name: idx_schedules_due; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_schedules_due ON "aif-aef-agnbf-af-aifaouroboros-db".schedules USING btree (state, next_run_at);


--
-- Name: idx_schedules_owner_chat; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_schedules_owner_chat ON "aif-aef-agnbf-af-aifaouroboros-db".schedules USING btree (owner_id, agent_id, chat_id, state);


--
-- Name: idx_scratchpad_blocks_partition_status_id; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_scratchpad_blocks_partition_status_id ON "aif-aef-agnbf-af-aifaouroboros-db".scratchpad_blocks USING btree (owner_id, agent_id, job_id, surface_id, chat_id, status, id);


--
-- Name: idx_security_contexts_expires; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_security_contexts_expires ON "aif-aef-agnbf-af-aifaouroboros-db".security_contexts USING btree (expires_at);


--
-- Name: idx_skill_artifacts_created; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_skill_artifacts_created ON "aif-aef-agnbf-af-aifaouroboros-db".skill_artifact_objects USING btree (created_at);


--
-- Name: idx_skill_binding_snapshots_created; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_skill_binding_snapshots_created ON "aif-aef-agnbf-af-aifaouroboros-db".skill_binding_snapshots USING btree (created_at);


--
-- Name: idx_skill_catalogue_expiry; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_skill_catalogue_expiry ON "aif-aef-agnbf-af-aifaouroboros-db".skill_catalogue_snapshots USING btree (expires_at);


--
-- Name: idx_skill_catalogue_loads_expiry; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_skill_catalogue_loads_expiry ON "aif-aef-agnbf-af-aifaouroboros-db".skill_catalogue_loads USING btree (expires_at);


--
-- Name: idx_skill_catalogue_query; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_skill_catalogue_query ON "aif-aef-agnbf-af-aifaouroboros-db".skill_catalogue_snapshots USING btree (query_hash, created_at DESC);


--
-- Name: idx_skill_receipts_created; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_skill_receipts_created ON "aif-aef-agnbf-af-aifaouroboros-db".skill_command_receipts USING btree (created_at);


--
-- Name: idx_skill_receipts_revision; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_skill_receipts_revision ON "aif-aef-agnbf-af-aifaouroboros-db".skill_command_receipts USING btree (((result ->> 'installed_revision'::text)));


--
-- Name: idx_skill_revisions_created; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_skill_revisions_created ON "aif-aef-agnbf-af-aifaouroboros-db".skill_revisions USING btree (created_at);


--
-- Name: idx_skill_revisions_digest; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_skill_revisions_digest ON "aif-aef-agnbf-af-aifaouroboros-db".skill_revisions USING btree (digest);


--
-- Name: idx_task_episodes_expires; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_task_episodes_expires ON "aif-aef-agnbf-af-aifaouroboros-db".task_episodes USING btree (expires_at);


--
-- Name: idx_task_episodes_scope_created; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_task_episodes_scope_created ON "aif-aef-agnbf-af-aifaouroboros-db".task_episodes USING btree (owner_id, agent_id, surface_id, chat_id, created_at);


--
-- Name: idx_task_episodes_task; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_task_episodes_task ON "aif-aef-agnbf-af-aifaouroboros-db".task_episodes USING btree (task_id, created_at);


--
-- Name: idx_task_inputs_active; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_task_inputs_active ON "aif-aef-agnbf-af-aifaouroboros-db".task_inputs USING btree (owner_id, agent_id, surface_id, chat_id, sequence) WHERE (state = ANY (ARRAY['pending'::text, 'editing'::text]));


--
-- Name: idx_task_inputs_dispatch_message; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_task_inputs_dispatch_message ON "aif-aef-agnbf-af-aifaouroboros-db".task_inputs USING btree (message_id) WHERE dispatched;


--
-- Name: idx_task_inputs_retention; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_task_inputs_retention ON "aif-aef-agnbf-af-aifaouroboros-db".task_inputs USING btree (updated_at, sequence) WHERE (state = ANY (ARRAY['committed'::text, 'cancelled'::text]));


--
-- Name: idx_task_inputs_scope_sequence; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_task_inputs_scope_sequence ON "aif-aef-agnbf-af-aifaouroboros-db".task_inputs USING btree (owner_id, agent_id, surface_id, chat_id, sequence);


--
-- Name: idx_task_inputs_task_sequence; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_task_inputs_task_sequence ON "aif-aef-agnbf-af-aifaouroboros-db".task_inputs USING btree (task_id, sequence);


--
-- Name: idx_task_skill_binding_revisions_revision; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_task_skill_binding_revisions_revision ON "aif-aef-agnbf-af-aifaouroboros-db".task_skill_binding_revisions USING btree (revision_id);


--
-- Name: idx_task_skill_bindings_created; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_task_skill_bindings_created ON "aif-aef-agnbf-af-aifaouroboros-db".task_skill_bindings USING btree (created_at);


--
-- Name: idx_task_skill_bindings_snapshot; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_task_skill_bindings_snapshot ON "aif-aef-agnbf-af-aifaouroboros-db".task_skill_bindings USING btree (snapshot_key);


--
-- Name: idx_tasks_chat_status; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_tasks_chat_status ON "aif-aef-agnbf-af-aifaouroboros-db".tasks USING btree (owner_id, agent_id, surface_id, chat_id, status);


--
-- Name: idx_tasks_claim; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_tasks_claim ON "aif-aef-agnbf-af-aifaouroboros-db".tasks USING btree (status, priority, created_at, task_id);


--
-- Name: idx_tasks_correlation; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_tasks_correlation ON "aif-aef-agnbf-af-aifaouroboros-db".tasks USING btree (correlation_id);


--
-- Name: idx_tasks_owner_job; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE UNIQUE INDEX idx_tasks_owner_job ON "aif-aef-agnbf-af-aifaouroboros-db".tasks USING btree (owner_id, job_id, lineage_ordinal) WHERE (job_id <> ''::text);


--
-- Name: idx_tasks_owner_running_roots; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_tasks_owner_running_roots ON "aif-aef-agnbf-af-aifaouroboros-db".tasks USING btree (owner_id, agent_id, status, parent_task_id);


--
-- Name: idx_tasks_status_heartbeat; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_tasks_status_heartbeat ON "aif-aef-agnbf-af-aifaouroboros-db".tasks USING btree (status, heartbeat_at);


--
-- Name: idx_tasks_terminal_publication; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_tasks_terminal_publication ON "aif-aef-agnbf-af-aifaouroboros-db".tasks USING btree (terminal_publish_after, finished_at) WHERE (detached AND (terminal_published_at IS NULL) AND (status = ANY (ARRAY['completed'::text, 'cancelled'::text, 'failed'::text, 'rejected_duplicate'::text, 'budget_exceeded'::text])));


--
-- Name: idx_tool_confirmations_task; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_tool_confirmations_task ON "aif-aef-agnbf-af-aifaouroboros-db".tool_confirmations USING btree (task_id, round_idx);


--
-- Name: idx_tool_invocations_expires; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_tool_invocations_expires ON "aif-aef-agnbf-af-aifaouroboros-db".tool_invocations USING btree (expires_at);


--
-- Name: idx_tool_invocations_task; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_tool_invocations_task ON "aif-aef-agnbf-af-aifaouroboros-db".tool_invocations USING btree (task_id, created_at);


--
-- Name: idx_tool_invocations_tool; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE INDEX idx_tool_invocations_tool ON "aif-aef-agnbf-af-aifaouroboros-db".tool_invocations USING btree (tool_name, created_at);


--
-- Name: uq_folders_name; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE UNIQUE INDEX uq_folders_name ON "aif-aef-agnbf-af-aifaouroboros-db".folders USING btree (owner_id, agent_id, COALESCE(parent_id, ''::text), name);


--
-- Name: uq_messages_dedup_key; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE UNIQUE INDEX uq_messages_dedup_key ON "aif-aef-agnbf-af-aifaouroboros-db".messages USING btree (dedup_key);


--
-- Name: uq_messages_file_owner; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE UNIQUE INDEX uq_messages_file_owner ON "aif-aef-agnbf-af-aifaouroboros-db".messages USING btree (owner_id, agent_id, message_id);


--
-- Name: uq_task_inputs_editing; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE UNIQUE INDEX uq_task_inputs_editing ON "aif-aef-agnbf-af-aifaouroboros-db".task_inputs USING btree (owner_id, agent_id, surface_id, chat_id) WHERE (state = 'editing'::text);


--
-- Name: uq_tasks_one_running_foreground_root_per_chat; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE UNIQUE INDEX uq_tasks_one_running_foreground_root_per_chat ON "aif-aef-agnbf-af-aifaouroboros-db".tasks USING btree (owner_id, agent_id, surface_id, chat_id) WHERE ((status = 'running'::text) AND (parent_task_id = ''::text) AND (task_type = 'task'::text) AND (NOT detached));


--
-- Name: uq_tool_confirmations_open; Type: INDEX; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

CREATE UNIQUE INDEX uq_tool_confirmations_open ON "aif-aef-agnbf-af-aifaouroboros-db".tool_confirmations USING btree (owner_id, agent_id, surface_id, chat_id) WHERE (status = 'pending'::text);


--
-- Name: files files_owner_id_agent_id_folder_id_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".files
    ADD CONSTRAINT files_owner_id_agent_id_folder_id_fkey FOREIGN KEY (owner_id, agent_id, folder_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".folders(owner_id, agent_id, id);


--
-- Name: personal_a2a_user_tokens fk_personal_a2a_user_tokens_agent; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".personal_a2a_user_tokens
    ADD CONSTRAINT fk_personal_a2a_user_tokens_agent FOREIGN KEY (personal_agent_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".personal_a2a_agents(id) ON DELETE CASCADE;


--
-- Name: personal_mcp_user_tokens fk_personal_mcp_user_tokens_server; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".personal_mcp_user_tokens
    ADD CONSTRAINT fk_personal_mcp_user_tokens_server FOREIGN KEY (personal_server_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".personal_mcp_servers(id) ON DELETE CASCADE;


--
-- Name: folders folders_owner_id_agent_id_parent_id_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".folders
    ADD CONSTRAINT folders_owner_id_agent_id_parent_id_fkey FOREIGN KEY (owner_id, agent_id, parent_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".folders(owner_id, agent_id, id);


--
-- Name: message_files message_files_owner_id_agent_id_file_id_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".message_files
    ADD CONSTRAINT message_files_owner_id_agent_id_file_id_fkey FOREIGN KEY (owner_id, agent_id, file_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".files(owner_id, agent_id, id);


--
-- Name: message_files message_files_owner_id_agent_id_message_id_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".message_files
    ADD CONSTRAINT message_files_owner_id_agent_id_message_id_fkey FOREIGN KEY (owner_id, agent_id, message_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".messages(owner_id, agent_id, message_id) ON DELETE CASCADE;


--
-- Name: owner_skill_bindings owner_skill_bindings_owner_id_agent_id_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".owner_skill_bindings
    ADD CONSTRAINT owner_skill_bindings_owner_id_agent_id_fkey FOREIGN KEY (owner_id, agent_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".owner_skill_state(owner_id, agent_id);


--
-- Name: owner_skill_bindings owner_skill_bindings_skill_id_revision_id_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".owner_skill_bindings
    ADD CONSTRAINT owner_skill_bindings_skill_id_revision_id_fkey FOREIGN KEY (skill_id, revision_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".skill_revisions(skill_id, revision_id);


--
-- Name: skill_catalogue_cards skill_catalogue_cards_snapshot_id_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_catalogue_cards
    ADD CONSTRAINT skill_catalogue_cards_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".skill_catalogue_snapshots(snapshot_id) ON DELETE CASCADE;


--
-- Name: skill_command_receipts skill_command_receipts_owner_id_agent_id_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_command_receipts
    ADD CONSTRAINT skill_command_receipts_owner_id_agent_id_fkey FOREIGN KEY (owner_id, agent_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".owner_skill_state(owner_id, agent_id);


--
-- Name: skill_revisions skill_revisions_digest_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_revisions
    ADD CONSTRAINT skill_revisions_digest_fkey FOREIGN KEY (digest) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".skill_artifact_objects(digest);


--
-- Name: skill_revisions skill_revisions_skill_id_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".skill_revisions
    ADD CONSTRAINT skill_revisions_skill_id_fkey FOREIGN KEY (skill_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".skills(skill_id);


--
-- Name: task_inputs task_inputs_task_id_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".task_inputs
    ADD CONSTRAINT task_inputs_task_id_fkey FOREIGN KEY (task_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".tasks(task_id) ON DELETE CASCADE;


--
-- Name: task_plans task_plans_root_task_id_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".task_plans
    ADD CONSTRAINT task_plans_root_task_id_fkey FOREIGN KEY (root_task_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".tasks(task_id) ON DELETE CASCADE;


--
-- Name: task_skill_binding_revisions task_skill_binding_revisions_skill_id_revision_id_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".task_skill_binding_revisions
    ADD CONSTRAINT task_skill_binding_revisions_skill_id_revision_id_fkey FOREIGN KEY (skill_id, revision_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".skill_revisions(skill_id, revision_id);


--
-- Name: task_skill_binding_revisions task_skill_binding_revisions_task_id_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".task_skill_binding_revisions
    ADD CONSTRAINT task_skill_binding_revisions_task_id_fkey FOREIGN KEY (task_id) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".task_skill_bindings(task_id) ON DELETE CASCADE;


--
-- Name: task_skill_bindings task_skill_bindings_snapshot_key_fkey; Type: FK CONSTRAINT; Schema: aif-aef-agnbf-af-aifaouroboros-db; Owner: -
--

ALTER TABLE ONLY "aif-aef-agnbf-af-aifaouroboros-db".task_skill_bindings
    ADD CONSTRAINT task_skill_bindings_snapshot_key_fkey FOREIGN KEY (snapshot_key) REFERENCES "aif-aef-agnbf-af-aifaouroboros-db".skill_binding_snapshots(snapshot_key);


--
-- PostgreSQL database dump complete
--

CREATE TABLE "aif-aef-agnbf-af-aifaouroboros-db".gigaagent_schema_version (
    component text PRIMARY KEY,
    version integer NOT NULL
);
INSERT INTO "aif-aef-agnbf-af-aifaouroboros-db".gigaagent_schema_version (component, version) VALUES ('gigaagent', 34);
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".gigaagent_schema_version;
--rollback DROP TABLE IF EXISTS "aif-aef-agnbf-af-aifaouroboros-db".a, "aif-aef-agnbf-af-aifaouroboros-db".agent_configuration_announcements, "aif-aef-agnbf-af-aifaouroboros-db".agents, "aif-aef-agnbf-af-aifaouroboros-db".audit_events, "aif-aef-agnbf-af-aifaouroboros-db".chats, "aif-aef-agnbf-af-aifaouroboros-db".consolidation_leases, "aif-aef-agnbf-af-aifaouroboros-db".context_summaries, "aif-aef-agnbf-af-aifaouroboros-db".deployment_keyspace, "aif-aef-agnbf-af-aifaouroboros-db".file_transfer_leases, "aif-aef-agnbf-af-aifaouroboros-db".files, "aif-aef-agnbf-af-aifaouroboros-db".folders, "aif-aef-agnbf-af-aifaouroboros-db".llm_usage, "aif-aef-agnbf-af-aifaouroboros-db".mcp_user_tokens, "aif-aef-agnbf-af-aifaouroboros-db".memory, "aif-aef-agnbf-af-aifaouroboros-db".message_files, "aif-aef-agnbf-af-aifaouroboros-db".messages, "aif-aef-agnbf-af-aifaouroboros-db".owner_runtime_state, "aif-aef-agnbf-af-aifaouroboros-db".owner_skill_bindings, "aif-aef-agnbf-af-aifaouroboros-db".owner_skill_state, "aif-aef-agnbf-af-aifaouroboros-db".personal_a, "aif-aef-agnbf-af-aifaouroboros-db".personal_a, "aif-aef-agnbf-af-aifaouroboros-db".personal_mcp_servers, "aif-aef-agnbf-af-aifaouroboros-db".personal_mcp_user_tokens, "aif-aef-agnbf-af-aifaouroboros-db".schedule_runs, "aif-aef-agnbf-af-aifaouroboros-db".schedules, "aif-aef-agnbf-af-aifaouroboros-db".scratchpad_blocks, "aif-aef-agnbf-af-aifaouroboros-db".security_contexts, "aif-aef-agnbf-af-aifaouroboros-db".sessions, "aif-aef-agnbf-af-aifaouroboros-db".skill_activations, "aif-aef-agnbf-af-aifaouroboros-db".skill_artifact_objects, "aif-aef-agnbf-af-aifaouroboros-db".skill_binding_snapshots, "aif-aef-agnbf-af-aifaouroboros-db".skill_catalogue_cards, "aif-aef-agnbf-af-aifaouroboros-db".skill_catalogue_loads, "aif-aef-agnbf-af-aifaouroboros-db".skill_catalogue_snapshots, "aif-aef-agnbf-af-aifaouroboros-db".skill_command_receipts, "aif-aef-agnbf-af-aifaouroboros-db".skill_registry_policy, "aif-aef-agnbf-af-aifaouroboros-db".skill_revisions, "aif-aef-agnbf-af-aifaouroboros-db".skills, "aif-aef-agnbf-af-aifaouroboros-db".task_episodes, "aif-aef-agnbf-af-aifaouroboros-db".task_inputs, "aif-aef-agnbf-af-aifaouroboros-db".task_plans, "aif-aef-agnbf-af-aifaouroboros-db".task_skill_binding_revisions, "aif-aef-agnbf-af-aifaouroboros-db".task_skill_bindings, "aif-aef-agnbf-af-aifaouroboros-db".tasks, "aif-aef-agnbf-af-aifaouroboros-db".tool_confirmations, "aif-aef-agnbf-af-aifaouroboros-db".tool_invocations, "aif-aef-agnbf-af-aifaouroboros-db".user_settings, "aif-aef-agnbf-af-aifaouroboros-db".workstation_sessions;
