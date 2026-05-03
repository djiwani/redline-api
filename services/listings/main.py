import os
import json
import boto3
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Redline Listings Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://redline.fourallthedogs.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# -------------------------------------------------------
# DB CONNECTION
# Fetches credentials from Secrets Manager at startup.
# IRSA gives this pod permission to call Secrets Manager.
# -------------------------------------------------------
def get_db_connection():
    secret_name = os.environ["DB_SECRET_NAME"]
    region = os.environ.get("AWS_REGION", "us-east-1")

    client = boto3.client("secretsmanager", region_name=region)
    secret = json.loads(client.get_secret_value(SecretId=secret_name)["SecretString"])

    return psycopg2.connect(
        host=secret["host"],
        port=secret["port"],
        dbname=secret["dbname"],
        user=secret["username"],
        password=secret["password"],
        cursor_factory=psycopg2.extras.RealDictCursor
    )


# -------------------------------------------------------
# ROUTES
# -------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "listings-service"}


@app.get("/listings")
def get_listings():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, make, model, year, price, mileage, color, description, image_url
            FROM listings
            ORDER BY created_at DESC
        """)
        listings = cur.fetchall()
        cur.close()
        conn.close()
        return {"listings": listings}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/listings/{listing_id}")
def get_listing(listing_id: int):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, make, model, year, price, mileage, color, description, image_url
            FROM listings
            WHERE id = %s
        """, (listing_id,))
        listing = cur.fetchone()
        cur.close()
        conn.close()

        if not listing:
            raise HTTPException(status_code=404, detail="Listing not found")

        return {"listing": listing}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
