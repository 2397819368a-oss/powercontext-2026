# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Exercise legacy migration CLI output with one embedded seekdb owner process."""

from __future__ import annotations

import asyncio
import faulthandler
import json
import sys
from pathlib import Path

from sqlalchemy import Column, Integer, MetaData, String, Table, select
from typer.testing import CliRunner

from powercontext.builtin.persistence.seekdb import SeekDBConfig, SeekDBProfile
from powercontext.cli.app import create_cli


def main(directory: Path) -> None:
    records = Table(
        "pc_bootstrap_records",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("value", String(64), nullable=False),
    )
    path = directory / "seekdb"
    old = SeekDBConfig(path=path, database="test")

    async def seed() -> None:
        async with (
            asyncio.timeout(30),
            SeekDBProfile.open(old, tables=(records,)) as profile,
            profile.database.transaction() as connection,
        ):
            await connection.execute(records.insert().values(id=1, value="preserved"))

    print("seed", file=sys.stderr, flush=True)
    asyncio.run(seed())
    environment = directory / "deployment.env"
    environment.write_text(
        "POWERCONTEXT_SERVER_DATABASE_KIND=seekdb\n"
        f"POWERCONTEXT_SERVER_DATABASE_PATH={path}\n"
        "POWERCONTEXT_SERVER_DATABASE_ECHO=true\n",
        encoding="utf-8",
    )
    cli = create_cli()
    runner = CliRunner()
    for action in ("status", "plan", "verify", "apply"):
        print(action, file=sys.stderr, flush=True)
        result = runner.invoke(cli, ["server", "db-migrate", action, "--env-file", str(environment)])
        assert result.exit_code == 1, (action, result.stdout, result.stderr, result.exception)
        payload = json.loads(result.stdout)
        assert payload["state"] == "blocked"
        assert payload["error"] == "explicit_database_required"
        assert "POWERCONTEXT_SERVER_DATABASE_DATABASE=test" in payload["message"]
        assert "database='powercontext'" in payload["message"]
        assert "Traceback" not in result.stderr

    async def verify_preserved() -> None:
        async with (
            asyncio.timeout(30),
            SeekDBProfile.open_existing(old, tables=(records,)) as profile,
            profile.database.transaction() as connection,
        ):
            assert await connection.scalar(select(records.c.value).where(records.c.id == 1)) == "preserved"
            assert (await connection.exec_driver_sql("SELECT DATABASE()")).scalar_one() == "test"
            result = await connection.exec_driver_sql(
                "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", ("powercontext",)
            )
            assert result.first() is None

    print("verify preserved data", file=sys.stderr, flush=True)
    asyncio.run(verify_preserved())
    print("complete", file=sys.stderr, flush=True)


if __name__ == "__main__":
    faulthandler.dump_traceback_later(45, repeat=True)
    try:
        main(Path(sys.argv[1]))
    finally:
        faulthandler.cancel_dump_traceback_later()
