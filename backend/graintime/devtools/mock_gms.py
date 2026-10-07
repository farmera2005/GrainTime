"""Development-only mock of a CompuWeigh GMS site database.

Creates the subset of GMS tables GrainTime reads (same table, column and index
names as the real site), seeds realistic harvest traffic, creates the
read-only `graintime` login from the CompuWeigh GMS mapping profile (so the
column-level grants are exercised), and can keep generating live trucks.

Traffic: Grain Inbound (TRUCKIN) with morning-to-afternoon rushes, plus
outbound, ingredient and misc tickets that GrainTime must ignore; voided
tickets (statuses 2 and 4); stored-tare tickets (one weigh only); split
tickets (a linked child ticket); tickets left open; trucks on site now.

Never point this at a real site. Usage (dev compose profile `mock`):
    python -m graintime.devtools.mock_gms --days 21 --live
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..common.login_script import login_script
from ..common.profiles import COMPUWEIGH_GMS, ProfileConfig

TZ = ZoneInfo("America/New_York")
TYPES = [(1, "TRUCKIN", "Grain Inbound", 1), (2, "TRUCKOUT", "Truck Outbound", 2),
         (17, "MISCIN", "Misc Inbound", 1), (18, "MISCOUT", "Misc Outbound", 2),
         (31, "INGREDIENTIN", "Ingredient Inbound", 1)]
PRODUCTS = [(9201597, "CORN", "Yellow Corn #2"), (9201598, "SOY", "Soybeans"),
            (9201599, "WHEAT", "Soft Red Wheat"), (9201605, "SBM", "Soybean Meal")]
TRUCK_LABELS = ["WHITE KENWORTH", "RED PETERBILT", "BLUE INTERNATIONAL", "GREEN MACK",
                "SMITH FARMS FREIGHTLINER", "MILLER GRAIN VOLVO"]

SCHEMA = """
IF OBJECT_ID('dbo.TransactionType') IS NULL
CREATE TABLE dbo.TransactionType (
  trt_pk int NOT NULL CONSTRAINT PK_TransactionType PRIMARY KEY CLUSTERED,
  trt_code nvarchar(30) NOT NULL, trt_description nvarchar(50) NULL, trt_direction int NOT NULL);
IF OBJECT_ID('dbo.Product') IS NULL
CREATE TABLE dbo.Product (
  prd_pk int NOT NULL CONSTRAINT PK_Product PRIMARY KEY CLUSTERED,
  prd_code nvarchar(20) NULL, prd_description nvarchar(50) NULL, prd_price float NULL);
IF OBJECT_ID('dbo.TransactionID') IS NULL
BEGIN
CREATE TABLE dbo.TransactionID (
  tid_pk int IDENTITY(150000,1) NOT NULL CONSTRAINT PK_TransactionID PRIMARY KEY CLUSTERED,
  tid_status int NOT NULL DEFAULT 0, tid_completetime datetime NULL,
  tid_trtfk int NOT NULL DEFAULT 1, tid_prdfk int NULL, tid_ticket int NULL,
  tid_tare float NULL, tid_gross float NULL, tid_net float NULL,
  tid_Id nvarchar(50) NULL, tid_driver nvarchar(50) NULL, tid_license nvarchar(20) NULL,
  tid_Customer_accfk int NULL, tid_price float NULL,
  tid_createdate datetime NULL DEFAULT getdate(),
  tid_parenttidfk int NULL, tid_childtidfk int NULL);
CREATE INDEX IX_TransactionID_tid_createdate ON dbo.TransactionID (tid_createdate, tid_status, tid_completetime DESC);
CREATE INDEX IX_TransactionID_tid_status_id ON dbo.TransactionID (tid_status, tid_Id) INCLUDE (tid_completetime, tid_trtfk);
CREATE INDEX IX_TransactionID_tid_completetime_status ON dbo.TransactionID (tid_completetime, tid_status);
CREATE INDEX IX_TransactionID_tid_ticket ON dbo.TransactionID (tid_ticket);
END
IF OBJECT_ID('dbo.TransactionLog') IS NULL
BEGIN
CREATE TABLE dbo.TransactionLog (
  tlg_pk int IDENTITY(2500000,1) NOT NULL CONSTRAINT PK_TransactionLog PRIMARY KEY CLUSTERED,
  tlg_tidfk int NOT NULL, tlg_prsfk int NULL, tlg_status int NOT NULL DEFAULT 0,
  tlg_StartTime datetime NOT NULL DEFAULT getdate(), tlg_FinishTime datetime NULL,
  tlg_Weight float NULL, tlg_WeightType int NULL, tlg_text nvarchar(max) NULL);
CREATE INDEX IX_TransactionLog_tlg_tidfk_tlg_status_tlg_WeightType ON dbo.TransactionLog (tlg_tidfk, tlg_status, tlg_WeightType);
CREATE INDEX IX_TransactionLog_tlg_StartTime ON dbo.TransactionLog (tlg_StartTime) INCLUDE (tlg_FinishTime);
END
"""


def connect(database: str | None = None):
    import pyodbc
    host = os.environ.get("MOCK_SQL_HOST", "mock-mssql")
    port = os.environ.get("MOCK_SQL_PORT", "1433")
    pw = os.environ["MOCK_SA_PASSWORD"]
    db = f"DATABASE={database};" if database else ""
    return pyodbc.connect(f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER=tcp:{host},{port};{db}"
                          f"UID=sa;PWD={{{pw}}};Encrypt=yes;TrustServerCertificate=yes",
                          autocommit=True, timeout=10)


def wait_for_server(tries: int = 60):
    for _ in range(tries):
        try:
            return connect()
        except Exception:
            time.sleep(2)
    raise SystemExit("mock SQL Server did not come up")


def local_now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def arrivals_per_hour(hour: int) -> float:
    """Harvest day: opens 06:00, rush 10:00-15:00, closes 19:00."""
    if hour < 6 or hour >= 19:
        return 0
    return {6: 3, 7: 5, 8: 7, 9: 10, 10: 14, 11: 16, 12: 15, 13: 16, 14: 14, 15: 11,
            16: 8, 17: 6, 18: 3}[hour]


def dwell_minutes(hour: int, rng: random.Random) -> float:
    base = 12 + (10 if 10 <= hour < 15 else 0)      # trucks wait longer in the rush
    return max(4.0, rng.lognormvariate(0, 0.35) * base)


class Gen:
    def __init__(self, conn, rng: random.Random):
        self.c, self.rng = conn, rng
        cur = conn.cursor()
        cur.execute("SELECT ISNULL(MAX(tid_ticket), 1082000) FROM dbo.TransactionID")
        self.next_ticket = cur.fetchone()[0] + 1

    def _ticket(self, trt, prd, status, created, completed, label, parent=None, tare=None,
                gross=None):
        cur = self.c.cursor()
        number = None
        if status == 1:
            number, self.next_ticket = self.next_ticket, self.next_ticket + 1
        cur.execute(
            "INSERT INTO dbo.TransactionID (tid_status, tid_completetime, tid_trtfk, tid_prdfk, "
            "tid_ticket, tid_tare, tid_gross, tid_net, tid_Id, tid_driver, tid_license, "
            "tid_Customer_accfk, tid_price, tid_createdate, tid_parenttidfk) "
            "OUTPUT INSERTED.tid_pk VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            status, completed, trt, prd, number, tare, gross,
            (gross - tare) if (gross and tare) else None, label, "J. DRIVER", "OH-123ABC",
            self.rng.randint(1000, 9000), 4.12, created, parent)
        return cur.fetchone()[0]

    def _step(self, tid, start, finish, wtype, weight, status=1, prs=3):
        self.c.cursor().execute(
            "INSERT INTO dbo.TransactionLog (tlg_tidfk, tlg_prsfk, tlg_status, tlg_StartTime, "
            "tlg_FinishTime, tlg_Weight, tlg_WeightType) VALUES (?,?,?,?,?,?,?)",
            tid, prs, status, start, finish, weight, wtype)

    def truck(self, arrive: datetime, now: datetime) -> None:
        """One truck. Weigh steps after `now` are not written yet (truck still on site)."""
        r = self.rng
        roll = r.random()
        trt = 1 if roll < 0.80 else 2 if roll < 0.90 else 31 if roll < 0.95 else 17 if roll < 0.98 else 18
        prd = r.choice(PRODUCTS[:3])[0] if trt in (1, 2) else PRODUCTS[3][0]
        label = r.choice(TRUCK_LABELS)
        created = arrive - timedelta(seconds=r.randint(20, 90))      # ticket opened at the probe/kiosk
        gross, tare = r.randint(70000, 102000), r.randint(24000, 30000)
        first_type, second_type = (0, 1) if trt in (1, 17, 31) else (1, 0)   # inbound: gross first
        w1 = gross if first_type == 0 else tare
        w2 = tare if first_type == 0 else gross
        in_start, in_finish = arrive, arrive + timedelta(seconds=r.randint(5, 10))
        out_start = in_finish + timedelta(minutes=dwell_minutes(arrive.hour, r))
        out_finish = out_start + timedelta(seconds=r.randint(5, 9))
        kind = r.random()
        stored_tare = kind < 0.03
        left_open = 0.03 <= kind < 0.035
        voided = 0.035 <= kind < 0.06
        split = 0.06 <= kind < 0.08
        done = out_finish <= now and not left_open
        if stored_tare:
            done = in_finish <= now
            status = 1 if done else 0
            tid = self._ticket(trt, prd, status, created, in_finish + timedelta(milliseconds=200)
                               if done else None, label, tare=tare, gross=gross)
            self._step(tid, created, created + timedelta(seconds=1), None, None, prs=1)
            if in_finish <= now:
                self._step(tid, in_start, in_finish, first_type, w1)
            return
        status = (2 if r.random() < 0.55 else 4) if (voided and done) else (1 if done else 0)
        tid = self._ticket(trt, prd, status, created,
                           out_finish + timedelta(milliseconds=200) if done else None, label,
                           tare=tare if done else None, gross=gross)
        self._step(tid, created, created + timedelta(seconds=1), None, None, prs=1)
        if in_finish <= now:
            self._step(tid, in_start, in_finish, first_type, w1)
        if done:
            self._step(tid, out_start, out_finish, second_type, w2)
            self._step(tid, out_finish, out_finish + timedelta(seconds=1), None, None, prs=21)
        if split:
            # Split between two producers: a linked child ticket, no weighs of its own.
            self._ticket(trt, prd, status, created + timedelta(seconds=30),
                         out_finish + timedelta(milliseconds=300) if done else None,
                         label, parent=tid, tare=tare if done else None, gross=gross)

    def seed(self, days: int, now: datetime) -> int:
        n = 0
        start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0)
        t = start
        while t < now:
            rate = arrivals_per_hour(t.hour)
            if rate:
                for _ in range(self.rng.randint(int(rate * 0.7), int(rate * 1.3))):
                    arrive = t + timedelta(seconds=self.rng.randint(0, 3599))
                    if arrive < now:
                        self.truck(arrive, now)
                        n += 1
            t += timedelta(hours=1)
        return n

    def live_step(self, now: datetime, speedup: float) -> None:
        """Advance live traffic: finish trucks whose planned stay is over, start new ones."""
        cur = self.c.cursor()
        cur.execute("SELECT tid_pk, tid_trtfk, tid_createdate FROM dbo.TransactionID "
                    "WHERE tid_status = 0 AND tid_parenttidfk IS NULL AND tid_createdate > ?",
                    now - timedelta(hours=5))
        for tid, trt, created in cur.fetchall():
            planned = timedelta(minutes=dwell_minutes(created.hour, self.rng) / speedup)
            if now - created >= planned:
                second = 1 if trt in (1, 17, 31) else 0
                self._step(tid, now - timedelta(seconds=7), now, second, self.rng.randint(24000, 30000))
                c2 = self.c.cursor()
                c2.execute("UPDATE dbo.TransactionID SET tid_status = 1, tid_completetime = ?, "
                           "tid_ticket = ? WHERE tid_pk = ?", now, self.next_ticket, tid)
                self.next_ticket += 1
        rate = max(arrivals_per_hour(now.hour), 6) * speedup
        if self.rng.random() < rate / 60:        # called about once a minute
            self.truck(now, now)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--speedup", type=float, default=4.0, help="live traffic speed multiplier")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)
    db_name = os.environ.get("MOCK_DB_NAME", "GMS")
    master = wait_for_server()
    cur = master.cursor()
    cur.execute(f"IF DB_ID(N'{db_name}') IS NULL CREATE DATABASE [{db_name}]")
    conn = connect(db_name)
    conn.cursor().execute(SCHEMA)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM dbo.TransactionType")
    if c.fetchone()[0] == 0:
        c.executemany("INSERT INTO dbo.TransactionType VALUES (?,?,?,?)", TYPES)
        c.executemany("INSERT INTO dbo.Product (prd_pk, prd_code, prd_description) VALUES (?,?,?)",
                      PRODUCTS)
    rng = random.Random(args.seed)
    gen = Gen(conn, rng)
    c.execute("SELECT COUNT(*) FROM dbo.TransactionID")
    if c.fetchone()[0] == 0:
        n = gen.seed(args.days, local_now())
        print(f"seeded {n} trucks over {args.days} days", flush=True)
    # Read-only login exactly as an admin would create it from the profile.
    ro_pw = os.environ.get("MOCK_GRAINTIME_PASSWORD", "Mock-Collector-2026!")
    script = login_script(ProfileConfig(**COMPUWEIGH_GMS), database=db_name,
                          password_placeholder=ro_pw)
    for batch in script.split("\nGO"):
        if batch.strip():
            cur = master.cursor()
            cur.execute(batch)
            while cur.nextset():      # surface errors raised after the first statement
                pass
    chk = master.cursor()
    chk.execute("SELECT COUNT(*) FROM sys.server_principals WHERE name = N'graintime'")
    if chk.fetchone()[0] != 1:
        raise SystemExit("read-only login was not created")
    print("read-only login 'graintime' ready", flush=True)
    if not args.live:
        return 0
    print(f"live traffic every ~60 s (x{args.speedup})", flush=True)
    while True:
        try:
            gen.live_step(local_now(), args.speedup)
        except Exception as exc:  # keep generating; dev tool
            print(f"live step failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(5)
            conn = connect(db_name)
            gen.c = conn
        time.sleep(60 / max(args.speedup, 1) if args.speedup > 1 else 60)


if __name__ == "__main__":
    sys.exit(main())
