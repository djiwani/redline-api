import os
import json
import boto3
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, jwk
from jose.utils import base64url_decode
import requests
from pydantic import BaseModel

app = FastAPI(title="Redline Users Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://redline.fourallthedogs.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

security = HTTPBearer()

COGNITO_REGION = os.environ.get("COGNITO_REGION", "us-east-1")
COGNITO_USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]
COGNITO_CLIENT_ID = os.environ["COGNITO_CLIENT_ID"]
JWKS_URL = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}/.well-known/jwks.json"

# Cache JWKS so we don't fetch on every request
_jwks = None

def get_jwks():
    global _jwks
    if _jwks is None:
        _jwks = requests.get(JWKS_URL).json()
    return _jwks

# -------------------------------------------------------
# JWT VERIFICATION
# Fetches Cognito public keys and verifies the JWT.
# This is the Store 2 / Cognito API call we discussed.
# -------------------------------------------------------
def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        headers = jwt.get_unverified_headers(token)
        kid = headers["kid"]

        jwks = get_jwks()
        key = next((k for k in jwks["keys"] if k["kid"] == kid), None)
        if not key:
            raise HTTPException(status_code=401, detail="Invalid token")

        public_key = jwk.construct(key)
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=COGNITO_CLIENT_ID
        )
        return claims
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# -------------------------------------------------------
# DB CONNECTION
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
# MODELS
# -------------------------------------------------------
class RegisterRequest(BaseModel):
    email: str
    cognito_sub: str  # Cognito user ID from the JWT


# -------------------------------------------------------
# ROUTES
# -------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "users-service"}


@app.post("/users/register")
def register_user(body: RegisterRequest):
    """
    Called after Cognito signup to store user in RDS.
    Frontend calls this once after the user confirms their email.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (cognito_sub, email)
            VALUES (%s, %s)
            ON CONFLICT (cognito_sub) DO NOTHING
            RETURNING id, cognito_sub, email, created_at
        """, (body.cognito_sub, body.email))
        user = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return {"user": user}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/users/me")
def get_me(claims: dict = Depends(verify_token)):
    """
    Returns the current user's profile.
    JWT is verified before this handler runs.
    """
    try:
        cognito_sub = claims["sub"]
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, cognito_sub, email, created_at
            FROM users
            WHERE cognito_sub = %s
        """, (cognito_sub,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        return {"user": user}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
