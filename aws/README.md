# TruthPhone on AWS — Lambda + Strands + AgentCore Browser

This is the AWS-native version of TruthPhone, implemented entirely in `aws/`.
The pre-AWS local app (FastAPI + LangGraph + Groq, single `global_state`, served
on `localhost:8000`) was removed once the Lambda version went live, so `aws/`
is the only implementation in this repository.

- **Live Chrome**: Amazon Bedrock AgentCore Browser, streamed into the React UI
  with `BrowserLiveView` (DCV), not screenshots.
- **Agents**: Strands Agents on Amazon Bedrock (`Claude Haiku 4.5` via the
  `in.anthropic.claude-haiku-4-5-20251001-v1:0` India inference profile).
- **Compute**: API Lambda (Function URL) + async worker Lambda. No ECS,
  Fargate, ALB, API Gateway, VPC, NAT, Cognito, or ECR.
- **State**: one DynamoDB item per browser session, with TTL.
- **UI**: React/Vite on private S3 behind CloudFront, with a same-origin
  `/api/*` proxy to the Function URL.
- **Shopping sources**: Amazon.in first with an Amazon-safe desktop fingerprint;
  if Amazon returns no usable listings or a challenge, the worker falls back to
  Flipkart, then DuckDuckGo/Bing results.
- **Region**: `ap-south-1`.

```
React UI (S3 + CloudFront)
   │  fetch /api/* (same origin)
   ▼
CloudFront ── /api/* proxy ──► API Lambda (FastAPI + Mangum) ── async invoke ──► Worker Lambda
   │                                              │ Strands agents
   ▼                                              │ Playwright over CDP
DynamoDB session ◄──── live-view URL ─────────────┤
                                                  ▼
                                     AgentCore Browser (real Chrome)
                              Amazon.in → Flipkart → DuckDuckGo/Bing
```

## Why this shape

- Lambda only keeps fixed cost near zero. The only meaningful variable costs
  are Bedrock tokens and AgentCore Browser time.
- The API never holds a browser. It appends a message, atomically claims the
  session, and invokes the worker asynchronously.
- The worker owns the browser session, writes a short-lived live-view URL to
  DynamoDB, and refreshes it every 4 minutes (URL max is 300 s).
- The UI calls CloudFront `/api/*`; a viewer-request function strips the prefix,
  so the browser uses one origin and CORS is not needed. The Function URL itself
  stays public for direct API testing.
- The API allows one active job per session, so a session cannot start a second
  browser. The stack does not reserve Lambda concurrency: this account's total
  limit is 10 executions, and any reservation would leave fewer than AWS's
  required 10 unreserved executions. With no authentication, keep the demo URL
  private if browser spend matters.
- `worker.zip` is ~72 MB zipped / ~184 MB unzipped and is loaded from S3.
  `api.zip` is ~19 MB zipped / ~32 MB unzipped. Both stay under Lambda's
  250 MB unzipped limit.

## Amazon.in access

AgentCore Browser's stock User-Agent ends with
`Amazon-Bedrock-AgentCore-Browser/1.0 (...)`. Amazon.in accepts the home page
with that UA but returns its `503 - Service Unavailable / Oops! It's rush hour`
page for search. The worker applies CDP overrides to the live-view tab before
navigating:

- remove the AgentCore product token from `navigator.userAgent` and add the
  normal `Safari/537.36` suffix;
- set `navigator.platform` to the real Linux platform;
- set locale `en-IN`, timezone `Asia/Kolkata`, and
  `Accept-Language: en-IN,en;q=0.9`.

The overrides are applied to the existing page, not a new context, so the DCV
live view shows the same Amazon.in page. This was verified with a real Chrome
live view: Amazon search returned 16 cards and an Amazon product page loaded at
₹11,999.

If Amazon changes its checks again, AgentCore Browser supports an external
HTTP/HTTPS proxy per session (`proxyConfiguration`). The proxy must support
HTTP CONNECT; SOCKS4/5 are not supported. Basic-auth credentials live in
Secrets Manager and the worker role needs `secretsmanager:GetSecretValue`.
An Indian residential or mobile proxy is the robust fallback; a proxy on a home
Indian broadband connection exposed through a secure tunnel is a low-cost
alternative. Datacenter proxies and other AWS regions are not reliable for
Amazon.in. The Flipkart/DuckDuckGo/Bing fallback remains in place.

## Layout

```
aws/
  backend/
    api_handler.py              # Mangum entrypoint
    worker_handler.py           # async worker entrypoint
    truthphone/
      api.py                    # FastAPI routes
      worker.py                 # search/verify orchestration
      agents.py                 # Strands agents + prompts
      browser.py                # AgentCore Browser + Playwright CDP
      amazon.py                 # Amazon harvest + Flipkart/web fallback
      store.py                  # DynamoDB session store
      models.py                 # Pydantic models
      config.py                 # env config
  frontend/                     # React 18 + Vite 5 + BrowserLiveView
  infra/
    backend.yaml                # SAM: Lambdas, Function URL, DynamoDB, IAM
    frontend.yaml               # CloudFormation: S3 + CloudFront + OAC + /api proxy
  scripts/
    build_lambda.py             # cross-installs Linux wheels, builds zips
    package.ps1                 # zips + frontend build
    run_local.ps1               # local API with in-process worker
    clean.ps1                   # deletes build outputs, node_modules, caches
  requirements-api.txt
  requirements-worker.txt
  requirements-dev.txt
  DEPLOY.md
```

## Local development

Prerequisites: Python 3.12, Node 20+, AWS credentials with Bedrock,
AgentCore Browser, and DynamoDB permissions in `ap-south-1`.

1. Create the DynamoDB table once (or deploy the backend stack first):
   - Table name: use the `SessionTableName` stack output (the stack generates
     a name like `truthphone-SessionsTable-15XVG5I3WUUTB`)
   - Partition key `sessionId` (String)
   - On-demand capacity
   - TTL attribute `expiresAt`
   - Put the name in `.env` as `SESSION_TABLE`

2. Install dependencies:

   ```powershell
   cd aws
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements-dev.txt
   ```

3. Configure:

   ```powershell
   Copy-Item .env.example .env
   # Edit .env if your table/model/region differ.
   ```

4. Start the API (worker runs in-process because `LOCAL_WORKER=1`):

   ```powershell
   .\scripts\run_local.ps1
   ```

5. Start the UI in another terminal:

   ```powershell
   cd frontend
   npm install
   npm run dev
   ```

   The dev UI calls `http://localhost:8000` automatically.

Voice input: the composer has a **Speak** button that dictates into the message
box using the browser's Web Speech API. Audio is transcribed by the browser's
own speech service (Chrome/Edge/Safari) - no extra AWS resources, endpoints, or
API keys. Firefox does not implement it, so the button stays disabled there.
Allow microphone access when the browser asks; `localhost` and HTTPS count as
secure contexts. Recognition language defaults to `en-IN`; change `lang` in
`frontend/src/useDictation.ts` (or the `useDictation({ lang })` call in
`App.tsx`) for other accents/languages.

Do **not** run `playwright install`. Chrome runs inside AgentCore Browser; the
worker only speaks CDP to it.

## Build for Lambda

```powershell
cd aws
.\scripts\package.ps1
```

Artifacts:

- `dist/api.zip`
- `dist/worker.zip`
- `frontend/dist/`

The build script cross-installs `manylinux2014_x86_64` CPython 3.12 wheels and
sets the executable bit on `playwright/driver/node` inside the zip. Do not
manually zip the worker on Windows: a normal Windows zip marks that file
non-executable and the worker fails with `Permission denied`.

`.\scripts\clean.ps1` deletes `build/`, `dist/`, `frontend/dist`,
`frontend/node_modules`, the copied DCV SDK, and `__pycache__`. Everything it
removes is regenerated by `package.ps1`; the deployed Lambdas and the S3 bucket
keep their own copies.

`package.ps1` also builds the UI with the local API base. Before deploying,
rebuild it with `VITE_API_URL=<ApiBaseUrl>` after creating the frontend stack
(see [DEPLOY.md](DEPLOY.md)).

## Deploy

See [DEPLOY.md](DEPLOY.md) for the AWS-console walkthrough. The short version:

1. Upload both zips to an S3 bucket.
2. Create `infra/backend.yaml` in CloudFormation; copy the `ApiUrl` output and
   note its hostname.
3. Create `infra/frontend.yaml` with `ApiOriginDomain` set to that hostname.
4. Build the UI with `VITE_API_URL=<ApiBaseUrl>` from the frontend outputs.
5. Upload `frontend/dist` to the output bucket; invalidate CloudFront.
6. Open the `SiteUrl` output.

## API

- `POST /sessions` — create a session
- `GET /sessions/{id}` — read messages, phase, matches, live-view URL
- `POST /sessions/{id}/messages` — `{message, forceSearch}`
- `POST /sessions/{id}/select` — `{index}` to start fact-checking
- `POST /sessions/{id}/reset` — reinitialize while idle
- `DELETE /sessions/{id}`

The UI polls `GET /sessions/{id}` every 1.5 s. This avoids response-streaming
limits on Python Lambda and keeps the UI stateless.

## Cost controls

- No ALB, ECS, Fargate, ECR, NAT gateway, or provisioned concurrency.
- DynamoDB on-demand + TTL (24 h).
- Browser session TTL 600 s; the worker stops it as soon as the job ends.
- The live-view URL is 300 s and refreshed every 240 s only while the browser
  is alive.
- One active browser job per session; no reserved concurrency (account cap is
  10 Lambda executions).
- CloudFront PriceClass_200 (includes India) and static S3/CloudFront hosting
  are pennies per month at demo traffic. The `/api/*` behavior uses the
  `CachingDisabled` policy, so API polls always reach Lambda.
- This deployment created a `truthphone-monthly` **$10/month** budget. Add an
  email subscriber in AWS Budgets (the account currently has none). AgentCore
  Browser time is the main variable cost; check current pricing for your session
  size. Bedrock tokens are second.

## Known limits

- No authentication. The Function URL is public; session IDs are unguessable
  but this is a demo, not a multi-tenant product.
- `global_state` from the original app is replaced by a DynamoDB item per
  session.
- Lambda concurrency is capped by the account (10 executions here); there is no
  reserved concurrency, so the app can use the full account pool.
- Amazon.in's bot filter is sensitive to the browser fingerprint. The worker
  removes AgentCore's product token and applies an Indian desktop fingerprint;
  this currently loads search and product pages. If Amazon tightens the check,
  the worker falls back to Flipkart, then DuckDuckGo/Bing. A residential/mobile
  proxy can be added through AgentCore's `proxyConfiguration` (see
  **Amazon.in access** above). On a CAPTCHA the worker pauses for up to 120 s
  and asks the user to solve it in the live view.
- Flipkart can change its DOM or block a request. The worker detects an empty
  harvest and continues to the web fallback; check the worker logs if both
  retailers stop returning cards.
- The DuckDuckGo/Bing fallback can return price-list pages rather than product
  pages, so some results may show `See page` instead of a price.
- DuckDuckGo can rate-limit verification searches; a failed search is reported
  as unverified rather than guessed.
- The live view is a real Chrome stream, but the browser session is ephemeral:
  a new search or verification starts a fresh browser session.
