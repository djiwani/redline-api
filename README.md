# Redline — API Services

Three FastAPI microservices for [Redline](https://redline.fourallthedogs.com), an AI-powered car marketplace where autonomous buyer and seller agents negotiate vehicle prices on behalf of users.

Each service is independently containerized, deployed to Amazon EKS via Helm, and assigned its own IRSA role with least-privilege AWS permissions. No shared IAM roles across services.

---

## Services

### listings-service

Read-only service. Fetches RDS credentials from Secrets Manager at startup via IRSA, connects to PostgreSQL, and returns car inventory.

| Endpoint | Description |
|----------|-------------|
| `GET /listings` | All active listings |
| `GET /listings/{id}` | Single listing by ID |
| `GET /health` | Health check |

### users-service

Handles user registration and profile retrieval. Verifies Cognito JWTs by fetching the public JWKS endpoint and caching the signing keys — no IAM required for JWT verification. Writes to RDS on first registration using the Cognito `sub` as the stable user identifier.

| Endpoint | Description |
|----------|-------------|
| `GET /users/{id}` | User profile |
| `POST /users` | Create user after Cognito signup |
| `GET /health` | Health check |

### negotiation-service

Runs multi-agent AI negotiations using Amazon Bedrock (Claude Haiku). A buyer bot and seller bot conduct autonomous multi-round price negotiations, persisting every round to DynamoDB and publishing outcomes via SNS.

| Endpoint | Description |
|----------|-------------|
| `POST /negotiate/start` | Start a new negotiation session |
| `GET /negotiate/{session_id}` | Full conversation for a session |
| `GET /negotiate/history/{user_id}` | All sessions for a user (GSI query) |
| `GET /health` | Health check |

**Negotiation flow:**

1. Buyer bot opens below the user's stated budget; seller bot responds from the asking price
2. Bots alternate for up to 10 rounds
3. Buyer bot hard-enforces the budget ceiling — never offers or accepts above max budget
4. Every round is saved to DynamoDB with `session_id` + `round_number` as the composite key
5. On deal or failure, SNS publishes an email notification to the user
6. Sessions expire automatically after 7 days via DynamoDB TTL

---

## IRSA Permissions Per Service

Each service assumes its own IAM role via IRSA. Trust policies are scoped to the specific Kubernetes service account and namespace — no cross-service role assumption is possible.

| Service | AWS Permissions |
|---------|----------------|
| `listings` | `secretsmanager:GetSecretValue` on DB secret ARN only |
| `users` | `secretsmanager:GetSecretValue` on DB secret ARN only |
| `negotiation` | `bedrock:InvokeModel` on Claude Haiku inference profile only; `dynamodb:PutItem/GetItem/UpdateItem/Query/DeleteItem` on sessions table and GSI only; `sns:Publish` on deal reached and negotiation failed topic ARNs only; `secretsmanager:GetSecretValue` on DB secret ARN only |

---

## Tech Stack

- **Runtime:** Python 3.12, FastAPI, Uvicorn
- **AWS:** Bedrock (Claude Haiku), DynamoDB, SNS, Secrets Manager, RDS PostgreSQL
- **Auth:** Cognito JWT verification via cached JWKS
- **Deployment:** Docker → ECR → EKS (Helm)
- **CI/CD:** GitHub Actions, path-based per-service triggers

---

## CI/CD

Each service has its own GitHub Actions workflow under `.github/workflows/`. Workflows trigger only when files change within that service's directory:

```
services/listings/**    → listings-service.yml
services/users/**       → users-service.yml
services/negotiation/** → negotiation-service.yml
```

Each workflow:
1. Builds a Docker image tagged with the Git SHA
2. Pushes to ECR
3. Updates the EKS deployment image via `kubectl set image`
4. Waits for rollout to complete

---

## Repository Structure

```
redline-api/
├── services/
│   ├── listings/
│   │   ├── main.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── users/
│   │   ├── main.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── negotiation/
│       ├── main.py
│       ├── Dockerfile
│       └── requirements.txt
└── .github/
    └── workflows/
        ├── listings-service.yml
        ├── users-service.yml
        └── negotiation-service.yml
```

---

## Local Development

```bash
cd services/negotiation
pip install -r requirements.txt
uvicorn main:app --reload
```

Requires environment variables — see each service's `main.py` for the full list.

---

## Manual Deployment

```bash
# Authenticate to ECR (get account ID from AWS console or aws sts get-caller-identity)
aws ecr get-login-password --region us-east-1 --profile dev | \
  docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-east-1.amazonaws.com

# Build, tag, push
docker build -t redline/negotiation-service ./services/negotiation
docker tag redline/negotiation-service:latest \
  <account-id>.dkr.ecr.us-east-1.amazonaws.com/redline/negotiation-service:latest
docker push <account-id>.dkr.ecr.us-east-1.amazonaws.com/redline/negotiation-service:latest

# Restart deployment
kubectl rollout restart deployment/negotiation-service -n redline
```

---

## Related Repositories

- [redline-terraform](https://github.com/djiwani/redline-terraform) — All AWS infrastructure
- [redline-frontend](https://github.com/djiwani/redline-frontend) — Static frontend
