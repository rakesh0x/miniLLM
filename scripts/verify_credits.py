"""End-to-end verification of the credit system against the live server.

Checks, in order:
  1.  allocate + balance math (Available = Allocated - Spent - Reserved)
  2.  charge and top-up keep the invariant
  3.  spending beyond the balance is rejected (402) and changes nothing
  4.  TTL expiry blocks usage (403) even when the balance looks positive
  5.  a top-up with a new TTL revives an expired key
  6.  the ledger reconciles against the balance
  7.  concurrency: N simultaneous charges on exactly N credits must yield
      exactly N successes and a final balance of 0 — the atomicity proof
  8.  reserve/commit/release, double-settle, and reservation TTL reclaim
  9.  the Bearer guard rejects anonymous, malformed, unknown and revoked keys
 10.  last_used_at is stamped on authenticated use

Run from the repo root:  .venv/bin/python scripts/verify_credits.py
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from miniLLMBE import credits as credits_service  # noqa: E402
from miniLLMBE.db import Session, engine  # noqa: E402
from miniLLMBE.models import ApiKey  # noqa: E402

BASE = os.environ.get("MINILLM_BASE", "http://127.0.0.1:8000")
FAILURES: list[str] = []
CHECKS = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f" -> {detail}" if detail else ""))
        FAILURES.append(label)


def expect(resp: httpx.Response, status: int) -> dict:
    if resp.status_code != status:
        print(f"       unexpected {resp.status_code}: {resp.text}", file=sys.stderr)
        raise SystemExit(1)
    return resp.json() if resp.status_code != 204 else {}


def create_key(name: str, credits: int | None = None, ttl_days: int | None = None):
    body: dict = {"name": name}
    if credits is not None:
        body["credits"] = credits
    if ttl_days is not None:
        body["ttl_days"] = ttl_days
    payload = expect(httpx.post(f"{BASE}/keys", json=body), 201)
    return payload["key"], payload["plaintext_key"]


def balance(key_id: int) -> dict:
    return expect(httpx.get(f"{BASE}/keys/{key_id}/credits"), 200)


def section(title: str) -> None:
    print(f"\n{title}")


def service_balance(session, key_id: int):
    return credits_service.to_credit_balance(
        session.get(ApiKey, key_id, populate_existing=True)
    )


def main() -> int:
    # --- 1. allocation -------------------------------------------------------
    section("1. allocation and balance math")
    key, _ = create_key("verify", credits=100, ttl_days=30)
    key_id = key["id"]
    print(f"  created key {key_id} ({key['masked_key']})")

    b = balance(key_id)
    check("allocated == 100", b["allocated"] == 100, str(b))
    check("spent == 0", b["spent"] == 0)
    check("reserved == 0", b["reserved"] == 0)
    check("available == 100", b["available"] == 100)
    check("not expired", b["expired"] is False)
    check(
        "expiry is ~30 days out",
        (datetime.fromisoformat(b["expires_at"]) - datetime.now(timezone.utc)) > timedelta(days=29),
        str(b["expires_at"]),
    )

    # --- 2. charge / top-up --------------------------------------------------
    section("2. charge and top-up")
    balance_of = expect(
        httpx.post(f"{BASE}/keys/{key_id}/credits/spend", json={"amount": 10, "reference": "smoke"}), 200
    )
    b = balance(key_id)
    check("spent == 10", b["spent"] == 10)
    check("available == 90", b["available"] == 90)
    check("invariant holds", b["available"] == b["allocated"] - b["spent"] - b["reserved"])

    httpx.post(f"{BASE}/keys/{key_id}/credits", json={"amount": 5})
    b = balance(key_id)
    check("top-up: allocated == 105", b["allocated"] == 105)
    check("top-up: available == 95", b["available"] == 95)

    # --- 3. overspend --------------------------------------------------------
    section("3. overspend is rejected")
    before = balance(key_id)
    resp = httpx.post(f"{BASE}/keys/{key_id}/credits/spend", json={"amount": 10_000})
    check("rejected with 402", resp.status_code == 402, f"got {resp.status_code}: {resp.text}")
    check("balance unchanged", balance(key_id) == before)

    # --- 4. TTL expiry -------------------------------------------------------
    section("4. TTL expiry")
    exp_key, _ = create_key("ttl-test")
    httpx.post(f"{BASE}/keys/{exp_key['id']}/credits", json={"amount": 50, "ttl_days": 30})
    b = balance(exp_key["id"])
    check("before expiry available == 50", b["available"] == 50)
    check("before expiry not expired", b["expired"] is False)

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    # A future expiry must be accepted explicitly.
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    resp = httpx.post(
        f"{BASE}/keys/{exp_key['id']}/credits",
        json={"amount": 1, "expires_at": future},
    )
    check("explicit future expiry accepted", resp.status_code == 200, resp.text)

    resp = httpx.post(
        f"{BASE}/keys/{exp_key['id']}/credits",
        json={"amount": 1, "expires_at": past},
    )
    check("past expiry rejected as invalid (422)", resp.status_code == 422,
          f"got {resp.status_code}: {resp.text}")

    # Let the TTL actually elapse: backdate the stored expiry by an hour.
    with Session(engine) as session:
        session.execute(
            text(
                "update api_keys set credits_expire_at = now() - interval '1 hour' "
                "where id = :i"
            ),
            {"i": exp_key["id"]},
        )
        session.commit()

    b = balance(exp_key["id"])
    check("expired flag set after TTL elapsed", b["expired"] is True, str(b))
    check("expired key reports 0 available despite balance", b["available"] == 0, str(b))
    check("expired key still shows its allocation", b["allocated"] == 51, str(b))

    resp = httpx.post(f"{BASE}/keys/{exp_key['id']}/credits/spend", json={"amount": 1})
    check("spending an expired key rejected with 403", resp.status_code == 403,
          f"got {resp.status_code}: {resp.text}")

    # --- 5. revive -----------------------------------------------------------
    section("5. top-up revives an expired key")
    httpx.post(f"{BASE}/keys/{exp_key['id']}/credits", json={"amount": 10, "ttl_days": 7})
    b = balance(exp_key["id"])
    invariant = b["allocated"] - b["spent"] - b["reserved"]
    check("revived: expired flag cleared", b["expired"] is False, str(b))
    check("revived: available matches the invariant", b["available"] == invariant, str(b))
    check("revived: credits usable again", b["available"] > 0, str(b))

    # --- 6. ledger -----------------------------------------------------------
    section("6. ledger")
    entries = expect(httpx.get(f"{BASE}/keys/{key_id}/credits/ledger"), 200)
    check("ledger has entries", len(entries) >= 3, str(len(entries)))
    check(
        "ledger is newest-first",
        all(entries[i]["created_at"] >= entries[i + 1]["created_at"] for i in range(len(entries) - 1)),
    )
    check("every entry records available_after", all("available_after" in e for e in entries))
    check(
        "newest ledger entry matches balance",
        entries[0]["available_after"] == balance(key_id)["available"],
        f"{entries[0]['available_after']}",
    )
    kinds = {e["kind"] for e in entries}
    check("ledger includes allocate and charge", {"allocate", "charge"} <= kinds, str(kinds))

    # --- 7. concurrency ------------------------------------------------------
    section("7. concurrency: 40 simultaneous charges on exactly 25 credits")
    race_key, _ = create_key("race")
    httpx.post(f"{BASE}/keys/{race_key['id']}/credits", json={"amount": 25})

    results: list[int] = []
    lock = threading.Lock()

    def hammer() -> None:
        r = httpx.post(
            f"{BASE}/keys/{race_key['id']}/credits/spend", json={"amount": 1}, timeout=30
        )
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=hammer) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes, rejected = results.count(200), results.count(402)
    b = balance(race_key["id"])
    check(f"exactly 25 succeeded (got {successes})", successes == 25, str(sorted(results)))
    check(f"exactly 15 rejected (got {rejected})", rejected == 15, str(sorted(results)))
    check("final spent == 25", b["spent"] == 25, str(b))
    check("final available == 0", b["available"] == 0, str(b))
    check("no unexpected status codes", set(results) <= {200, 402}, str(set(results)))

    # --- 8. reservations -----------------------------------------------------
    section("8. reserve / commit / release / reclaim")
    # The race key spent everything, so top it up for the reservation tests.
    httpx.post(f"{BASE}/keys/{race_key['id']}/credits", json={"amount": 10})
    base = balance(race_key["id"])
    check("top-up before reservations", base["available"] == 10, str(base))

    with Session(engine) as session:
        rid = credits_service.reserve(
            session, key_id=race_key["id"], amount=2, ttl_seconds=60
        )
        b = service_balance(session, race_key["id"])
        check("reserve holds credits", (b.allocated, b.spent, b.reserved) == (35, 25, 2), str(b))
        check("reserved credits are not available", b.available == 8, str(b))

        check("first commit settles", credits_service.commit(session, reservation_id=rid) is True)
        check("second commit is a no-op", credits_service.commit(session, reservation_id=rid) is False)
        b = service_balance(session, race_key["id"])
        check("committed exactly once", (b.spent, b.reserved) == (27, 0), str(b))
        check("commit leaves available unchanged", b.available == 8, str(b))

        rid2 = credits_service.reserve(
            session, key_id=race_key["id"], amount=1, ttl_seconds=60
        )
        check("release returns credits", credits_service.release(session, reservation_id=rid2) is True)
        b = service_balance(session, race_key["id"])
        check("reserved back to 0", b.reserved == 0, str(b))
        check("released credit is available again", b.available == 8, str(b))

        # An expired reservation is reclaimed rather than charged.
        rid3 = credits_service.reserve(
            session, key_id=race_key["id"], amount=1, ttl_seconds=3600
        )
        session.execute(
            text(
                "update credit_reservations set expires_at = now() - interval '1 minute' "
                "where id = :i"
            ),
            {"i": rid3},
        )
        session.commit()
        reclaimed = credits_service.reclaim_expired(session)
        # reclaim_expired runs inside the caller's transaction by design (so a
        # later reserve in the same call sees the credits); commit here or the
        # settlement rolls back and leaks a pending row for the next run.
        session.commit()
        check("one reservation reclaimed", reclaimed == 1, str(reclaimed))
        b = service_balance(session, race_key["id"])
        check("reclaim returns credit to available", (b.reserved, b.spent) == (0, 27), str(b))
        check("reclaim restores the available balance", b.available == 8, str(b))

    # --- 9. Bearer guard -----------------------------------------------------
    section("9. Bearer guard and the demo metered endpoint")
    auth_key, auth_plaintext = create_key("auth-test")
    httpx.post(f"{BASE}/keys/{auth_key['id']}/credits", json={"amount": 10})

    empty_key, empty_plaintext = create_key("empty-test")  # 0 credits, no TTL

    resp = httpx.post(f"{BASE}/usage/record")
    check("no Authorization header -> 401", resp.status_code == 401, f"got {resp.status_code}")
    resp = httpx.post(f"{BASE}/usage/record", headers={"Authorization": "Basic nope"})
    check("wrong scheme -> 401", resp.status_code == 401, f"got {resp.status_code}")
    resp = httpx.post(f"{BASE}/usage/record", headers={"Authorization": f"Bearer {auth_plaintext[:-4]}xxxx"})
    check("unknown key -> 401", resp.status_code == 401, f"got {resp.status_code}")
    resp = httpx.post(f"{BASE}/usage/record", headers={"Authorization": f"Bearer {empty_plaintext}"})
    check("zero-credit key -> 402", resp.status_code == 402, f"got {resp.status_code}: {resp.text}")

    payload = expect(
        httpx.post(f"{BASE}/usage/record", headers={"Authorization": f"Bearer {auth_plaintext}"}), 200
    )
    check("valid key accepted", payload.get("charged") == 1, str(payload))
    check("balance returned", payload["balance"]["available"] == 9, str(payload))
    check("reference echoed", payload["reference"] == "usage/record")

    # --- 10. last_used_at ----------------------------------------------------
    section("10. last_used_at stamping")
    keys = {k["id"]: k for k in expect(httpx.get(f"{BASE}/keys"), 200)}
    check("last_used_at stamped for the used key", keys[auth_key["id"]]["last_used_at"] is not None)
    check(
        "last_used_at stamped even when the credit guard rejected the call",
        keys[empty_key["id"]]["last_used_at"] is not None,
    )

    # --- 11. revocation ------------------------------------------------------
    section("11. revoked key rejected")
    rev_key, rev_plaintext = create_key("revoke-test")
    httpx.post(f"{BASE}/keys/{rev_key['id']}/credits", json={"amount": 5})
    expect(httpx.delete(f"{BASE}/keys/{rev_key['id']}"), 204)
    resp = httpx.post(f"{BASE}/usage/record", headers={"Authorization": f"Bearer {rev_plaintext}"})
    check("revoked key -> 401", resp.status_code == 401, f"got {resp.status_code}")

    # --- summary -------------------------------------------------------------
    print(f"\n{'=' * 62}")
    print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
