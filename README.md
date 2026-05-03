# Redline — API Services

Three FastAPI microservices for [Redline](https://redline.fourallthedogs.com), an AI-powered car marketplace. Services are containerized, deployed to Amazon EKS via Helm, and use IRSA for scoped AWS permissions.

## Services

### listings-service
Serves car listing data from RDS PostgreSQL.

| Endpoint | Description |
|----------|-------------|
| `GET /listings` | All active listings |
| `GET /listings/{id}` | Single listing by ID |
| `GET /health` | Health check |

### users-service
Handles user profile data backed by RDS PostgreSQL.

| Endpoint | Description |
|----------|-------------|
| `GET /users/{id}` | User profile |
| `POST /users` | Create user |
| `GET /health` | Health check |

### negotiation-service
Runs multi-agent AI negotiations using Amazon Bedrock. A buyer bot and seller bot conduct autonomous multi-round price negotiations, persisting conversation history to DynamoDB and publishing outcomes via SNS.

| Endpoint | Description |
|----------|-------------|
| `POST /negotiate/start` | Start a new negotiation session |
| `GET /negotiate/{session_id}` | Get full conversation for a session |
| `GET /negotiate/history/{user_id}` | All sessions for a user |
| `GET /health` | Health check |

**Negotiation flow:**
1. Buyer bot opens below budget, seller bot responds from asking price
2. Bots alternate offers for up to 10 rounds
3. Buyer bot hard-enforces budget ceiling — never offers or agrees above max budget
4. Each round saved to DynamoDB with `session_id` + sequence number as composite key
5. On deal or failure, SNS publishes an email notification to the user

## Tech Stack

- **Runtime**: Python 3.12, FastAPI, Uvicorn
- **AWS**: Bedrock (Claude Haiku 4.5), DynamoDB, SNS, Secrets Manager, RDS PostgreSQL
- **Auth**: Cognito JWT verification
- **Deployment**: Docker → ECR → EKS (Helm)
- **CI/CD**: GitHub Actions — per-service workflows triggering on path-based changes

## CI/CD

Each service has its own GitHub Actions workflow under `.github/workflows/`. Workflows trigger only when files change within that service's directory:

```
services/listings/**    → deploy-listings-service.yml
services/users/**       → deploy-users-service.yml
services/negotiation/** → deploy-negotiation-service.yml
```

Each workflow:
1. Builds a Docker image tagged with the Git SHA
2. Pushes to ECR
3. Updates the EKS deployment image via `kubectl set image`
4. Waits for rollout to complete

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

## Local Development

```bash
cd services/negotiation
pip install -r requirements.txt
uvicorn main:app --reload
```

Requires environment variables — see each service's `main.py` for the full list.

## Deployment

Handled automatically via GitHub Actions on push to `main`. For manual deployment:

```bash
# Authenticate to ECR
aws ecr get-login-password --region us-east-1 --profile dev | \
  docker login --username AWS --password-stdin 856888988892.dkr.ecr.us-east-1.amazonaws.com

# Build, tag, push
docker build -t redline/negotiation-service ./services/negotiation
docker tag redline/negotiation-service:latest \
  856888988892.dkr.ecr.us-east-1.amazonaws.com/redline/negotiation-service:latest
docker push 856888988892.dkr.ecr.us-east-1.amazonaws.com/redline/negotiation-service:latest

# Restart deployment
kubectl rollout restart deployment/negotiation-service -n redline
```

## Related Repositories

- [redline-terraform](https://github.com/djiwani/redline-terraform) — All infrastructure
- [redline-frontend](https://github.com/djiwani/redline-frontend) — Static frontend
