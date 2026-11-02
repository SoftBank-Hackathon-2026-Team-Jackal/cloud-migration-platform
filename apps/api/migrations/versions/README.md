# Business DB revisions

Create a revision after defining an approved SQLAlchemy model: `uv run alembic revision --autogenerate -m "description"`. Review the generated migration before applying it with `uv run alembic upgrade head`. No business tables are defined yet.
