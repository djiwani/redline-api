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

app = FastAPI(title="Redline Negotiation Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
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
def invoke_buyer_bot(conversation_history: list, buyer_budget: float, current_offer: float) -> dict:
    """Buyer bot negotiates on behalf of the user."""
    system_prompt = f"""You are a car buyer negotiating to purchase a vehicle.
Your maximum budget is ${buyer_budget:.2f}. Never exceed this amount.
Start below your budget and negotiate up gradually only if needed.
Be firm but reasonable. Try to get the best deal possible.

Respond ONLY with valid JSON in this exact format:
{{
  "role": "buyer",
  "offer_price": <number>,
  "message": "<your negotiation message>",
  "deal_reached": <true or false>,
  "agreed_price": <number or null>
}}"""

    messages = conversation_history + [
        {"role": "user", "content": f"Make your next offer. Current seller offer: ${current_offer:.2f}"}
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
    return json.loads(result["content"][0]["text"])


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
    return json.loads(result["content"][0]["text"])


# -------------------------------------------------------
# DYNAMODB HELPERS
# -------------------------------------------------------
def save_message(session_id: str, user_id: str, round_number: int, role: str, message: str, offer_price: float):
    table.put_item(Item={
        "session_id": session_id,
        "round_number": Decimal(str(round_number)),
        "user_id": user_id,
        "role": role,
        "message": message,
        "offer_price": Decimal(str(offer_price)),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ttl": int(time.time()) + (7 * 24 * 60 * 60)  # 7 days
    })


def get_session(session_id: str) -> list:
    response = table.query(
        KeyConditionExpression="session_id = :sid",
        ExpressionAttributeValues={":sid": session_id}
    )
    return sorted(response["Items"], key=lambda x: x["round_number"])


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

    conversation_history = []
    current_seller_offer = body.listing_price
    current_buyer_offer = 0
    deal_reached = False
    agreed_price = None

    for round_num in range(1, MAX_ROUNDS + 1):

        # --- Buyer turn ---
        buyer_response = invoke_buyer_bot(
            conversation_history,
            body.buyer_budget,
            current_seller_offer
        )
        current_buyer_offer = buyer_response["offer_price"]

        save_message(
            session_id, body.user_id, round_num,
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
        seller_response = invoke_seller_bot(
            conversation_history,
            body.listing_price,
            body.seller_floor,
            current_buyer_offer
        )
        current_seller_offer = seller_response["offer_price"]

        save_message(
            session_id, body.user_id, round_num,
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
