"""Read-only 50-employee load test for X-WALLET CHAT SERVER v7.8.3.

Input JSON format (never commit real passwords):
[
  {"username": "employee1", "password": "...", "totp_secret": "BASE32_SECRET_IF_2FA"},
  ...
]
`totp_secret` is optional; include it only for accounts with 2FA enabled.

Run from a trusted machine:
  set XWALLET_BASE_URL=https://your-railway-domain
  set XWALLET_USERS_FILE=employees.json
  python tools/load_test_50.py

This test does NOT send messages to real customers. After it passes, perform
controlled manual send/receive tests with designated test Telegram accounts.
"""
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx
import pyotp

BASE = os.getenv("XWALLET_BASE_URL", "").rstrip("/")
USERS_FILE = Path(os.getenv("XWALLET_USERS_FILE", "employees.json"))
DURATION = int(os.getenv("XWALLET_LOAD_SECONDS", "60"))


async def main():
    if not BASE:
        raise SystemExit("Set XWALLET_BASE_URL first.")
    if not USERS_FILE.is_file():
        raise SystemExit(f"Cannot find credentials file: {USERS_FILE}")
    users = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    if len(users) < 50:
        raise SystemExit(f"Need at least 50 separate employee accounts; found {len(users)}.")
    users = users[:50]
    timeout = httpx.Timeout(20.0, connect=10.0)
    latencies = []
    failures = []
    active = []

    async with httpx.AsyncClient(base_url=BASE, timeout=timeout) as client:
        health = await client.get("/api/health/ready")
        print("readiness:", health.status_code, health.text[:300])
        if health.status_code != 200:
            raise SystemExit("Service/database is not ready; stop before load testing.")

        async def login_one(item, index):
            started = time.perf_counter()
            try:
                response = await client.post("/api/auth/login", json=item)
                elapsed = (time.perf_counter() - started) * 1000
                latencies.append(elapsed)
                if response.status_code != 200:
                    failures.append((f"login:{index}", response.status_code, response.text[:200]))
                    return None
                data = response.json()
                if data.get("requires_2fa"):
                    secret = item.get("totp_secret")
                    if not secret:
                        failures.append((f"login:{index}", 2, "2FA enabled but totp_secret is missing from the private test credentials file."))
                        return None
                    verify = await client.post("/api/auth/2fa/verify", json={
                        "challenge_token": data["challenge_token"],
                        "code": pyotp.TOTP(secret).now()
                    })
                    if verify.status_code != 200:
                        failures.append((f"login-2fa:{index}", verify.status_code, verify.text[:200]))
                        return None
                    data = verify.json()
                return {"username": item["username"], "token": data["token"], "session_key": data.get("session_key", "")}
            except Exception as exc:
                failures.append((f"login:{index}", "exception", str(exc)[:200]))
                return None

        logged_in = await asyncio.gather(*(login_one(item, i + 1) for i, item in enumerate(users)))
        active = [item for item in logged_in if item]
        print(f"logged in: {len(active)}/50")
        if len(active) != 50:
            print("login failures:", failures[:20])
            raise SystemExit("50 concurrent logins did not all pass. Do not declare production-ready.")

        stop_at = time.monotonic() + DURATION
        round_no = 0
        while time.monotonic() < stop_at:
            round_no += 1
            async def exercise(user, index):
                headers = {"Authorization": "Bearer " + user["token"], "X-Session-Key": user["session_key"]}
                for endpoint in ("/api/me", "/api/inbox?filter=all", "/api/notifications"):
                    started = time.perf_counter()
                    try:
                        response = await client.get(endpoint, headers=headers)
                        latencies.append((time.perf_counter() - started) * 1000)
                        if response.status_code >= 400:
                            failures.append((f"{index}:{endpoint}", response.status_code, response.text[:200]))
                    except Exception as exc:
                        failures.append((f"{index}:{endpoint}", "exception", str(exc)[:200]))
                try:
                    response = await client.post("/api/presence/heartbeat", headers=headers)
                    if response.status_code >= 400:
                        failures.append((f"{index}:heartbeat", response.status_code, response.text[:200]))
                except Exception as exc:
                    failures.append((f"{index}:heartbeat", "exception", str(exc)[:200]))
            await asyncio.gather(*(exercise(user, i + 1) for i, user in enumerate(active)))
            print(f"round {round_no}: 50 users active; errors={len(failures)}")
            await asyncio.sleep(2)

    if latencies:
        ordered = sorted(latencies)
        p95 = ordered[min(len(ordered)-1, int(len(ordered)*0.95))]
        print(f"request latency ms: avg={statistics.mean(latencies):.1f}, p95={p95:.1f}, max={max(latencies):.1f}")
    print(f"total failures: {len(failures)}")
    for failure in failures[:50]:
        print("FAIL", failure)
    if failures:
        raise SystemExit("Load test failed. Review Railway logs, CPU/memory, DB pool and PostgreSQL metrics.")
    print("PASS: 50 concurrent logins and read/presence workload completed without API errors.")
    print("NOTE: This does not verify real Telegram send/receive. Perform manual controlled message tests next.")


if __name__ == "__main__":
    asyncio.run(main())
