package main

import (
	"database/sql"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	_ "github.com/lib/pq"
)

var _ = pgx.ErrNoRows
var _ *pgxpool.Pool
var _ *sql.DB
