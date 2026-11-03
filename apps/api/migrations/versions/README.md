# Business DB revisions

Create a revision after defining an approved SQLAlchemy model: `uv run alembic revision --autogenerate -m "description"`. Review the generated migration before applying it with `uv run alembic upgrade head`. Revision `20261103_01` creates minimal Agent owner, enrollment, credential digest and work-reference tables; it does not create user accounts or approve Production work.
