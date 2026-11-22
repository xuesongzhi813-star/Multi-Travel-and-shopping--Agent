from alembic import context


connection = context.config.attributes.get("connection")
schema = context.config.attributes.get("business_schema")
if connection is None or schema is None:
    raise RuntimeError("Run scripts/prepare_business_db.py migrate for the business database.")

context.configure(connection=connection, version_table_schema=schema)
with context.begin_transaction():
    context.run_migrations()
