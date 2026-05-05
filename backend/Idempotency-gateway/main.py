import asyncio
import hashlib
import json
import time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Idempotency Gateway — FinSafe Transactions Ltd.")

# -------------------------------------------------------------------
# In-memory store
# Each entry looks like:
#   key -> {
#     "payload_hash": str,   # fingerprint of the original request body
#     "response": dict,      # the saved response to replay
#     "status_code": int,    # the saved status code to replay
#     "timestamp": float,    # when the key was first used (Unix time)
#     "in_flight": bool,     # True while the first request is still processing
#   }
# -------------------------------------------------------------------
store: dict = {}

# One asyncio.Event per in-flight key so duplicate requests can wait
# instead of starting a new process.
in_flight_events: dict[str, asyncio.Event] = {}

KEY_TTL_SECONDS = 86400  # 24 hours — Developer's Choice feature


def hash_payload(payload: dict) -> str:
    """Return a stable SHA-256 fingerprint of a dict."""
    serialized = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(serialized.encode()).hexdigest()


def is_expired(entry: dict) -> bool:
    """Return True if the idempotency key is older than KEY_TTL_SECONDS."""
    return (time.time() - entry["timestamp"]) > KEY_TTL_SECONDS


# -------------------------------------------------------------------
# POST /process-payment
# -------------------------------------------------------------------
@app.post("/process-payment")
async def process_payment(
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    body = await request.json()
    payload_hash = hash_payload(body)

    # --- Check if this key already exists in the store ---
    if idempotency_key in store:
        entry = store[idempotency_key]

        # Developer's Choice: expired keys are treated as new requests
        if is_expired(entry):
            del store[idempotency_key]
        else:
            # Key exists and is still valid — check what to do

            # CASE 1: Request is still being processed (race condition / in-flight)
            if entry["in_flight"]:
                event = in_flight_events.get(idempotency_key)
                if event:
                    await event.wait()  # wait for the first request to finish
                # Now the store has the real response — fall through to CASE 2

            # CASE 2: Same key, different payload — reject it
            if store[idempotency_key]["payload_hash"] != payload_hash:
                raise HTTPException(
                    status_code=422,
                    detail="Idempotency key already used for a different request body.",
                )

            # CASE 3: Same key, same payload — replay the saved response
            saved = store[idempotency_key]
            return JSONResponse(
                content=saved["response"],
                status_code=saved["status_code"],
                headers={"X-Cache-Hit": "true"},
            )

    # --- New key: mark as in-flight before the slow processing starts ---
    event = asyncio.Event()
    in_flight_events[idempotency_key] = event

    store[idempotency_key] = {
        "payload_hash": payload_hash,
        "response": None,
        "status_code": None,
        "timestamp": time.time(),
        "in_flight": True,
    }

    try:
        # Simulate payment processing (2-second delay)
        await asyncio.sleep(2)

        amount = body.get("amount", 0)
        currency = body.get("currency", "USD")
        response_body = {
            "message": f"Charged {amount} {currency}",
            "transaction_id": f"txn_{idempotency_key[:8]}",
            "status": "success",
        }
        status_code = 201

        # Save result to store
        store[idempotency_key]["response"] = response_body
        store[idempotency_key]["status_code"] = status_code
        store[idempotency_key]["in_flight"] = False

        return JSONResponse(content=response_body, status_code=status_code)

    finally:
        # Always signal waiting duplicates, even if an error occurred
        event.set()
        in_flight_events.pop(idempotency_key, None)


# -------------------------------------------------------------------
# GET /health  — simple health check
# -------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "stored_keys": len(store)}
