"""Development only: a mock AgVantagePC LOCALSET database with truck traffic.

Builds `dbo.scale_ticket` with the same columns, types and primary key as the
real AgVantagePC table (from the New Weston PC's discovery), in a database
named like the attached file, and seeds tickets that follow the patterns seen
there: purchases (I/P) ~65%, sales (O/S) ~32%, transfers ~3%; CPT/APP/V
statuses; inbound gross first, outbound tare first; 18% keyed weights; stays
mostly 5-30 min. Names, drivers and weights are fake and never read by
GrainTime (the profile doesn't select them).

    MOCK_SQL_HOST=... MOCK_SA_PASSWORD=... python -m graintime.devtools.mock_agvantage --days 30 [--live]
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime, timedelta

from .mock_gms import arrivals_per_hour, connect, dwell_minutes, local_now, wait_for_server

DB_NAME = os.environ.get("MOCK_AGV_DB", r"C:\PROGRAM FILES (X86)\AGVANTAGE SOFTWARE\AGVANTAGEPC GUI\LOCALSET.MDF")
LOC = 45
COMMODITIES = [("53", "BEANS", 0.48), ("57", "WHEAT", 0.27), ("43", "CORN", 0.24), ("49", "OATS", 0.01)]

COLUMNS_SQL = """
    [st_load] char(8) NOT NULL, [st_loc] numeric(18,0) NOT NULL, [st_modified] tinyint NULL,
    [st_account] numeric(18,0) NULL, [st_commodity] char(30) NULL, [st_desc] char(30) NULL,
    [st_date] datetime NULL, [st_inout] char(1) NULL, [st_grslbs] numeric(18,0) NULL,
    [st_grsloc] numeric(18,0) NULL, [st_grsdate] datetime NULL, [st_tarlbs] numeric(18,0) NULL,
    [st_tarloc] numeric(18,0) NULL, [st_tardate] datetime NULL, [st_netlbs] numeric(18,0) NULL,
    [st_netbu] numeric(18,4) NULL, [st_tickettype] char(3) NULL, [st_hauler] numeric(18,0) NULL,
    [st_purchclass] char(2) NULL, [st_type] char(2) NULL, [st_driver] char(35) NULL,
    [st_vehicle] nchar(20) NULL, [st_status] char(3) NULL, [st_grsmanual] numeric(18,0) NULL,
    [st_tarmanual] numeric(18,0) NULL, [st_transferloc] numeric(18,0) NULL, [st_split] numeric(18,0) NULL,
    [st_inuse] tinyint NULL, [st_name] char(90) NULL, [st_iodesc] char(10) NULL, [st_ticket] char(20) NULL,
    [st_statusdesc] char(12) NULL, [st_completedDate] datetime NULL, [st_scaleticket] char(15) NULL,
    [st_eod] datetime NULL,
    CONSTRAINT PK_scale_ticket PRIMARY KEY CLUSTERED (st_load, st_loc)
"""


class Gen:
    def __init__(self, conn, rng: random.Random):
        self.c, self.rng = conn, rng
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM dbo.scale_ticket")
        n = cur.fetchone()[0]
        self.next_load, self.next_ticket = 20000 + n, 670000 + n

    def truck(self, arrive: datetime, now: datetime) -> bool:
        """One completed ticket (AgVantage writes the row with both weighs)."""
        r = self.rng.random()
        inout, typ, desc = ("I", "P", "Purchase") if r < 0.65 else ("O", "S", "Sale") if r < 0.97 else (
            self.rng.choice(["I", "O"]), "T", "Transfer")
        stay = dwell_minutes(arrive.hour, self.rng)
        if self.rng.random() < 0.18:          # keyed weights: often seconds apart
            stay = self.rng.uniform(0.02, 0.2) if self.rng.random() < 0.5 else stay
            manual = (1, self.rng.choice([0, 1]))
        else:
            manual = (0, 0)
        leave = arrive + timedelta(minutes=stay)
        if leave > now:
            return False
        first, second = arrive, leave
        grs, tar = (first, second) if inout == "I" else (second, first)
        if self.rng.random() < 0.05:          # occasional reversed order, as in the real data
            grs, tar = tar, grs
        roll = self.rng.random()
        status, sdesc = ("V", "Void") if roll < 0.007 else ("APP", "Applied") if roll < 0.025 else ("CPT", "Complete")
        code, cname = self.rng.choices([(c, n) for c, n, _ in COMMODITIES], [w for *_, w in COMMODITIES])[0]
        load = f"{self.next_load}R"
        self.next_load += 1
        self.next_ticket += 1
        cur = self.c.cursor()
        cur.execute(
            "INSERT INTO dbo.scale_ticket (st_load, st_loc, st_modified, st_account, st_commodity, st_desc, st_date, "
            "st_inout, st_grslbs, st_grsloc, st_grsdate, st_tarlbs, st_tarloc, st_tardate, st_netlbs, st_tickettype, "
            "st_type, st_driver, st_vehicle, st_status, st_grsmanual, st_tarmanual, st_transferloc, st_split, st_inuse, "
            "st_name, st_iodesc, st_ticket, st_statusdesc, st_completedDate, st_scaleticket) "
            "VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, 1, ?, ?, 1, ?, ?, 'G', ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)",
            load, LOC, self.rng.randint(1000, 99999), code, cname, arrive.replace(hour=0, minute=0, second=0, microsecond=0),
            inout, self.rng.randint(60000, 80000), grs, self.rng.randint(25000, 32000), tar, 40000, typ,
            f"MOCK DRIVER {self.rng.randint(1, 50)}", f"TRK{self.rng.randint(100, 999)}", status, manual[0], manual[1],
            46 if typ == "T" else None, f"MOCK PRODUCER {self.rng.randint(1, 400)}", "Inbound" if inout == "I" else "Outbound",
            desc, sdesc, None if status == "V" and self.rng.random() < 0.9 else leave, str(self.next_ticket))
        return True

    def seed(self, days: int, now: datetime) -> int:
        n, t = 0, (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        while t < now:
            rate = arrivals_per_hour(t.hour) * 0.35          # a quieter site than the GMS mock
            for _ in range(self.rng.randint(int(rate * 0.6), int(rate * 1.4) + 1) if rate else 0):
                if self.truck(t + timedelta(seconds=self.rng.randint(0, 3599)), now):
                    n += 1
            t += timedelta(hours=1)
        return n


def ensure_database(master) -> None:
    cur = master.cursor()
    cur.execute("IF DB_ID(?) IS NULL BEGIN DECLARE @s nvarchar(400) = N'CREATE DATABASE ' + QUOTENAME(?); "
                "EXEC(@s); END", DB_NAME, DB_NAME)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    master = wait_for_server()
    ensure_database(master)
    conn = connect(DB_NAME)
    conn.cursor().execute(f"IF OBJECT_ID('dbo.scale_ticket') IS NULL CREATE TABLE dbo.scale_ticket ({COLUMNS_SQL})")
    gen = Gen(conn, random.Random(args.seed))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM dbo.scale_ticket")
    if cur.fetchone()[0] == 0:
        print(f"seeded {gen.seed(args.days, local_now())} AgVantage tickets over {args.days} days", flush=True)
    if args.live:
        print("live traffic every ~60 s", flush=True)
        while True:
            now = local_now()
            if gen.rng.random() < max(arrivals_per_hour(now.hour), 4) * 0.35 * 4 / 60:
                gen.truck(now - timedelta(minutes=dwell_minutes(now.hour, gen.rng)), now)
            time.sleep(60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
