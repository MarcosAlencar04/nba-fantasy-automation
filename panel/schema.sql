-- Schema do painel: duas tabelas, sem tocar em nada do agente.
--
-- Rode isto uma vez no banco (Neon SQL Editor, psql ou `python init_db.py`).
-- O script e idempotente: pode rodar de novo sem erro.

CREATE TABLE IF NOT EXISTS leagues (
    id             BIGSERIAL PRIMARY KEY,
    name           TEXT        NOT NULL,
    espn_league_id TEXT        NOT NULL,
    espn_swid      TEXT        NOT NULL,
    espn_s2        TEXT        NOT NULL,
    is_active      BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at     TIMESTAMPTZ
);

-- O leagueId do ESPN identifica a liga no universo: nao pode repetir entre
-- ligas vivas. Parcial para nao bloquear uma liga recriada depois de excluida.
CREATE UNIQUE INDEX IF NOT EXISTS leagues_espn_league_id_key
    ON leagues (espn_league_id)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS users (
    id           BIGSERIAL   PRIMARY KEY,
    -- Vínculo explicito com a liga. Sem isto as duas tabelas ficariam soltas e
    -- nao daria para saber de qual liga vem o team_id.
    league_id    BIGINT      NOT NULL REFERENCES leagues (id) ON DELETE RESTRICT,
    name         TEXT        NOT NULL,
    espn_team_id INTEGER     NOT NULL,
    email        TEXT,
    telegram_id  TEXT,
    is_active    BOOLEAN     NOT NULL DEFAULT TRUE,
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);

-- O teamId so e unico dentro de uma liga.
CREATE UNIQUE INDEX IF NOT EXISTS users_league_team_key
    ON users (league_id, espn_team_id)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS users_status_idx
    ON users (is_active, deleted_at);

-- Mantem updated_at coerente mesmo se alguem editar por psql.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS leagues_touch ON leagues;
CREATE TRIGGER leagues_touch BEFORE UPDATE ON leagues
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS users_touch ON users;
CREATE TRIGGER users_touch BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
