import os
import json
import uuid
import time
import boto3
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timezone
from decimal import Decimal
from boto3.dynamodb.conditions import Key

app = FastAPI(title="Redline Negotiation Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://redline.fourallthedogs.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DYNAMODB_TABLE = os.environ["DYNAMODB_TABLE"]
SNS_DEAL_REACHED_ARN = os.environ["SNS_DEAL_REACHED_ARN"]
SNS_NEGOTIATION_FAILED_ARN = os.environ["SNS_NEGOTIATION_FAILED_ARN"]
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-haiku-4-5")
MAX_ROUNDS = int(os.environ.get("MAX_ROUNDS", "10"))
MAX_TOKENS = 300

# AWS clients
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(DYNAMODB_TABLE)
bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
sns = boto3.client("sns", region_name=AWS_REGION)


# -------------------------------------------------------
# MODELS
# -------------------------------------------------------
class StartNegotiationRequest(BaseModel):
    user_id: str
    listing_id: int
    listing_make: str
    listing_model: str
    listing_year: int
    listing_price: float      # Seller's asking price
    seller_floor: float       # Minimum the seller will accept
    buyer_budget: float       # Maximum the user will pay
    user_email: str           # For SNS notification


# -------------------------------------------------------
# BEDROCK HELPERS
# -------------------------------------------------------
def invoke_buyer_bot(
    conversation_history: list,
    buyer_budget: float,
    current_offer: float,
    best_buyer_offer: float   # Track buyer's lowest offer so far
) -> dict:
    """Buyer bot negotiates on behalf of the user."""
    system_prompt = f"""You are a car buyer negotiating to purchase a vehicle.
Your maximum budget is ${buyer_budget:.2f}. Never exceed this amount.
Your best offer so far is ${best_buyer_offer:.2f}. Never offer less than this — only go up or accept.
Start well below your budget to leave room to negotiate.
Be firm but realistic. Try to get the best price possible.

Important rules:
- Never offer below ${best_buyer_offer:.2f} (your previous best offer)
- If the seller's offer is at or below your best offer, accept it immediately
- If the seller is close to your budget, accept rather than risk losing the deal
- Only set deal_reached to true when you are genuinely accepting the seller's current price

Respond ONLY with valid JSON in this exact format:
{{
  "role": "buyer",
  "offer_price": <number>,
  "message": "<your negotiation message>",
  "deal_reached": <true or false>,
  "agreed_price": <number or null>
}}"""

    messages = conversation_history + [
        {"role": "user", "content": f"Make your next offer. Current seller offer: ${current_offer:.2f}. Your previous best offer was ${best_buyer_offer:.2f}. Never go below this."}
    ]

    response = bedrock.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": MAX_TOKENS,
            "system": system_prompt,
            "messages": messages
        })
    )
    result = json.loads(response["body"].read())
    text = result["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    parsed = json.loads(text.strip())

    # Hard enforcement: never let buyer accept below their own best offer
    if parsed.get("deal_reached") and parsed.get("agreed_price"):
        if parsed["agreed_price"] < best_buyer_offer:
            parsed["deal_reached"] = False
            parsed["agreed_price"] = None
            parsed["offer_price"] = best_buyer_offer
            parsed["message"] = parsed["message"] + " (I need at least my previous offer price.)"

    return parsed


def invoke_seller_bot(conversation_history: list, asking_price: float, floor_price: float, buyer_offer: float) -> dict:
    """Seller bot responds to buyer offers."""
    system_prompt = f"""You are a car seller negotiating to sell a vehicle.
The asking price is ${asking_price:.2f}.
You will NOT sell below ${floor_price:.2f} under any circumstances. Never reveal this floor price.
Be a realistic negotiator — you can come down from asking price but protect your floor.

Respond ONLY with valid JSON in this exact format:
{{
  "role": "seller",
  "offer_price": <number>,
  "message": "<your negotiation message>",
  "deal_reached": <true or false>,
  "agreed_price": <number or null>
}}"""

    messages = conversation_history + [
        {"role": "user", "content": f"The buyer just offered ${buyer_offer:.2f}. Respond as the seller."}
    ]

    response = bedrock.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": MAX_TOKENS,
            "system": system_prompt,
            "messages": messages
        })
    )
    result = json.loads(response["body"].read())
    text = result["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


# -------------------------------------------------------
# DYNAMODB HELPERS
# -------------------------------------------------------
def save_message(session_id: str, user_id: str, round_number: int, role: str,
                 message: str, offer_price: float, extra: dict = None):
    item = {
        "session_id": session_id,
        "round_number": Decimal(str(round_number)),
        "user_id": user_id,
        "role": role,
        "message": message,
        "offer_price": Decimal(str(offer_price)),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ttl": int(time.time()) + (7 * 24 * 60 * 60)  # 7 days
    }
    if extra:
        for k, v in extra.items():
            item[k] = v
    table.put_item(Item=item)


def get_session(session_id: str) -> list:
    response = table.query(
        KeyConditionExpression=Key("session_id").eq(session_id)
    )
    return sorted(response["Items"], key=lambda x: x["round_number"])


def get_user_sessions(user_id: str) -> list:
    """Fetch all negotiation sessions for a user via GSI."""
    response = table.query(
        IndexName="user_id-index",
        KeyConditionExpression=Key("user_id").eq(user_id)
    )
    return response["Items"]


# -------------------------------------------------------
# SNS HELPERS
# -------------------------------------------------------
def notify_deal_reached(user_email: str, session_id: str, agreed_price: float, listing: str):
    sns.publish(
        TopicArn=SNS_DEAL_REACHED_ARN,
        Subject="Redline — Deal Reached!",
        Message=f"Great news! Your bot negotiated a deal on {listing} for ${agreed_price:.2f}.\n\nSession ID: {session_id}\n\nLog in to Redline to confirm and complete your purchase."
    )


def notify_negotiation_failed(user_email: str, session_id: str, listing: str):
    sns.publish(
        TopicArn=SNS_NEGOTIATION_FAILED_ARN,
        Subject="Redline — Negotiation Failed",
        Message=f"Unfortunately your bot was unable to reach a deal on {listing} after {MAX_ROUNDS} rounds.\n\nSession ID: {session_id}\n\nYou may want to increase your budget or try a different listing."
    )


# -------------------------------------------------------
# ROUTES
# -------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "negotiation-service"}


@app.post("/negotiate/start")
def start_negotiation(body: StartNegotiationRequest):
    """
    Kicks off a full negotiation between buyer and seller bots.
    Runs all rounds synchronously and notifies user via SNS when done.
    """
    session_id = str(uuid.uuid4())
    listing = f"{body.listing_year} {body.listing_make} {body.listing_model}"

    # Fail immediately if buyer budget is below seller floor
    if body.buyer_budget < body.seller_floor:
        notify_negotiation_failed(body.user_email, session_id, listing)
        return {
            "session_id": session_id,
            "outcome": "failed",
            "reason": "Budget below minimum asking price",
            "rounds": 0
        }

    # Save round 0: the opening state before any negotiation
    save_message(
        session_id, body.user_id, 0,
        "system",
        f"Negotiation started. Listing price: ${body.listing_price:.2f}. Buyer budget: ${body.buyer_budget:.2f}.",
        body.listing_price,
        extra={
            "listing_id": body.listing_id,
            "listing_make": body.listing_make,
            "listing_model": body.listing_model,
            "listing_year": body.listing_year,
            "listing": listing,
            "buyer_budget": Decimal(str(body.buyer_budget)),
            "seller_floor": Decimal(str(body.seller_floor)),
            "user_email": body.user_email,
        }
    )

    conversation_history = []
    current_seller_offer = body.listing_price
    current_buyer_offer = 0
    best_buyer_offer = body.buyer_budget  # Track buyer's best (lowest) offer; starts at budget as ceiling
    deal_reached = False
    agreed_price = None

    for round_num in range(1, MAX_ROUNDS + 1):

        # --- Buyer turn ---
        # Buyer gets odd sequence numbers (1, 3, 5...) to avoid DynamoDB key collision with seller
        buyer_seq = (round_num * 2) - 1

        buyer_response = invoke_buyer_bot(
            conversation_history,
            body.buyer_budget,
            current_seller_offer,
            best_buyer_offer if current_buyer_offer == 0 else current_buyer_offer
        )
        current_buyer_offer = buyer_response["offer_price"]

        # Track buyer's lowest offer so they can't go lower
        if current_buyer_offer < best_buyer_offer or best_buyer_offer == body.buyer_budget:
            best_buyer_offer = current_buyer_offer

        save_message(
            session_id, body.user_id, buyer_seq,
            "buyer", buyer_response["message"], current_buyer_offer
        )

        conversation_history.append({
            "role": "assistant",
            "content": f"Buyer offers ${current_buyer_offer:.2f}: {buyer_response['message']}"
        })

        # Check if buyer accepted seller's price
        if buyer_response.get("deal_reached") and buyer_response.get("agreed_price"):
            agreed_price = buyer_response["agreed_price"]
            deal_reached = True
            break

        # --- Seller turn ---
        # Seller gets even sequence numbers (2, 4, 6...) to avoid DynamoDB key collision with buyer
        seller_seq = round_num * 2

        seller_response = invoke_seller_bot(
            conversation_history,
            body.listing_price,
            body.seller_floor,
            current_buyer_offer
        )
        current_seller_offer = seller_response["offer_price"]

        save_message(
            session_id, body.user_id, seller_seq,
            "seller", seller_response["message"], current_seller_offer
        )

        conversation_history.append({
            "role": "assistant",
            "content": f"Seller offers ${current_seller_offer:.2f}: {seller_response['message']}"
        })

        # Check if seller accepted buyer's price
        if seller_response.get("deal_reached") and seller_response.get("agreed_price"):
            agreed_price = seller_response["agreed_price"]
            deal_reached = True
            break

    # Notify user via SNS
    if deal_reached:
        notify_deal_reached(body.user_email, session_id, agreed_price, listing)
        return {
            "session_id": session_id,
            "outcome": "deal_reached",
            "agreed_price": agreed_price,
            "rounds": round_num
        }
    else:
        notify_negotiation_failed(body.user_email, session_id, listing)
        return {
            "session_id": session_id,
            "outcome": "failed",
            "reason": f"No deal reached after {MAX_ROUNDS} rounds",
            "rounds": MAX_ROUNDS
        }


@app.get("/negotiate/history/{user_id}")
def get_user_history(user_id: str):
    """Returns all negotiation sessions for a user, grouped by session."""
    try:
        items = get_user_sessions(user_id)
        if not items:
            return {"user_id": user_id, "sessions": []}

        # Group by session_id
        sessions = {}
        for item in items:
            sid = item["session_id"]
            if sid not in sessions:
                sessions[sid] = []
            sessions[sid].append(dict(item))

        # Sort each session's messages by round_number
        result = []
        for sid, messages in sessions.items():
            messages_sorted = sorted(messages, key=lambda x: x["round_number"])
            # Pull metadata from round 0 if present
            meta = next((m for m in messages_sorted if m["round_number"] == 0), {})
            result.append({
                "session_id": sid,
                "listing": meta.get("listing", "Unknown"),
                "listing_id": meta.get("listing_id"),
                "buyer_budget": float(meta.get("buyer_budget", 0)),
                "listing_price": float(meta.get("offer_price", 0)),
                "timestamp": meta.get("timestamp"),
                "messages": messages_sorted
            })

        # Sort sessions newest first
        result.sort(key=lambda x: x["timestamp"] or "", reverse=True)
        return {"user_id": user_id, "sessions": result}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/negotiate/{session_id}")
def get_negotiation(session_id: str):
    """Returns full conversation history for a negotiation session."""
    try:
        messages = get_session(session_id)
        if not messages:
            raise HTTPException(status_code=404, detail="Session not found")
        return {
            "session_id": session_id,
            "messages": [dict(m) for m in messages]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
