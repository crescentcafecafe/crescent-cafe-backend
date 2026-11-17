.PHONY: run test test-pg migrate admin docs
run:        ; uvicorn app.main:app --reload
test:       ; pytest
test-pg:    ; TEST_DATABASE_URL=postgresql+asyncpg://cafe:cafe@localhost:5432/cafe_test pytest
migrate:    ; alembic upgrade head
admin:      ; python -m scripts.bootstrap_admin
docs:       ; python -m scripts.export_api_docs
