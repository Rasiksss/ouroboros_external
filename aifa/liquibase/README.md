# Database deployment

`changelog.sql` is applied by the AEF Liquibase deployment step using a DDL role,
before starting AIFA and GigaAgent. Both services use the same database/schema.
Application roles need USAGE on the schema, DML on their tables and USAGE on application
sequences; they must not own the tables or have CREATE on the schema.
GigaAgent only reads `gigaagent_schema_version`; it needs SELECT on that table.

The GigaAgent baseline is equivalent to the former Alembic head 0034, excluding
`alembic_version`. It is for the first deployment (no GigaAgent tables/data).
It contains tables, defaults, checks, unique constraints, indexes and foreign keys.
The version marker is inserted at the end of the same transactional changeset.
Do not use this baseline to adopt an existing GigaAgent database.

The schema name follows the existing AIFA changelog:
`aif-aef-agnbf-af-aifaouroboros-db`. Both SecMan defaultSchemaName values must match.
Future schema changes belong in new Liquibase changesets; do not edit applied ones.
The platform must finish Liquibase successfully before starting application pods.
