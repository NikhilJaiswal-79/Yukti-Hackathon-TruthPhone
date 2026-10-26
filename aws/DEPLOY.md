# Deploy TruthPhone through the AWS Console

Region: **ap-south-1 (Mumbai)**. This uses the console for every step and does
not require Docker, ECR, or the SAM CLI.

## 0. Before you start

- Enable **Claude Haiku 4.5** model access in Bedrock (`ap-south-1`).
- The template defaults to the India inference profile
  `in.anthropic.claude-haiku-4-5-20251001-v1:0`. In `ap-south-1`, the bare
  foundation-model ID (`anthropic.claude-haiku-4-5-20251001-v1:0`) supports
  only `INFERENCE_PROFILE` inference, so direct on-demand invocation of the
  bare ID fails. Keep the `in.` prefix unless you have enabled a different
  profile.
- Confirm AgentCore Browser is available in `ap-south-1`.
- Use an IAM principal that can create Lambda, DynamoDB, IAM roles, S3,
  CloudFront, and CloudFormation resources. If you cannot create IAM roles,
  ask an admin to create the stack.
- Build the artifacts:

  ```powershell
  cd aws
  python scripts/build_lambda.py --target all
  ```

  You should see roughly:

  ```
  api:    31.5 MB unzipped, 19.2 MB zipped
  worker: 183.8 MB unzipped, 72.0 MB zipped
  playwright/driver/node mode in zip: 0o755
  ```

## 1. Create the code bucket

1. S3 → **Create bucket**.
2. Name: `truthphone-code-<account-id>-ap-south-1`.
3. Region: `ap-south-1`.
4. Block all public access: on.
5. Create the bucket.
6. Upload:
   - `aws/dist/api.zip` → key `v1/api.zip`
   - `aws/dist/worker.zip` → key `v1/worker.zip`

Keep the bucket private. Lambda reads the zips through the deployment
machinery; the browser and UI never need access to it.

## 2. Create the backend stack

1. CloudFormation → **Create stack** → **With new resources (standard)**.
2. **Upload a template file** → choose `aws/infra/backend.yaml`.
3. Stack name: `truthphone`.
4. Parameters:
   - `ApiCodeBucket`: your code bucket name
   - `ApiCodeKey`: `v1/api.zip`
   - `WorkerCodeBucket`: your code bucket name
   - `WorkerCodeKey`: `v1/worker.zip`
   - `BedrockModelId`: keep the default unless you enabled a different model
   - `BrowserSessionTimeout`: `600`
   - `AllowedOrigins`: `*` for the demo
5. Acknowledge the SAM transform / IAM resource creation prompt.
6. Create the stack and wait for `CREATE_COMPLETE` (usually 1–3 minutes).
7. Open **Outputs** and copy `ApiUrl`. It looks like:

   ```
   https://abcdefghijklmnop.lambda-url.ap-south-1.on.aws/
   ```

   Remove the trailing slash when using it directly. For the CloudFront proxy,
   note the **hostname only**:
   `abcdefghijklmnop.lambda-url.ap-south-1.on.aws`.

What this stack creates:

- `truthphone-api-<stack>` Lambda with a public Function URL (AuthType NONE)
- `truthphone-worker-<stack>` Lambda: 900 s, 2 GB, one job per session,
  async retries disabled (no reserved concurrency: the account cap is 10)
- DynamoDB table with TTL on `expiresAt`
- IAM roles for DynamoDB, Bedrock, AgentCore Browser, and worker invocation

## 3. Smoke-test the API

Open these in a browser or use `curl`:

```powershell
$api = "https://<your-api-url>"
curl "$api/health"
curl -Method Post "$api/sessions"
```

`/health` should return the region and model. `POST /sessions` returns a JSON
session with a `sessionId`. If you get a 500, check the API Lambda's
CloudWatch log group.

## 4. Create the frontend stack

1. CloudFormation → **Create stack** → **With new resources**.
2. Upload `aws/infra/frontend.yaml`.
3. Stack name: `truthphone-ui`.
4. Parameter `ApiOriginDomain`: the backend Function URL **hostname only**, with
   no `https://` and no trailing slash, for example
   `abcdefghijklmnop.lambda-url.ap-south-1.on.aws`.
5. Create the stack and wait for `CREATE_COMPLETE` (CloudFront can take
   5–10 minutes).
6. Copy the outputs:
   - `SiteUrl` (HTTPS CloudFront URL)
   - `ApiBaseUrl` (use as `VITE_API_URL`)
   - `UiBucketName`
   - `DistributionId`

The bucket is private. CloudFront reads it through Origin Access Control. The
same distribution proxies `/api/*` to the Function URL and strips the `/api`
prefix, so the UI uses one origin and no CORS.

Verify the proxy:

```powershell
curl "https://<distribution-domain>/api/health"
```

It should return the same JSON as the direct Function URL.

## 5. Build the UI against the proxy

```powershell
cd aws/frontend
$env:VITE_API_URL = "https://<distribution-domain>/api"   # ApiBaseUrl output
npm install
npm run build
```

`VITE_API_URL` is baked into the static bundle at build time. If you change the
CloudFront domain or the API path later, rebuild and re-upload the UI.

## 6. Upload the UI

Console:

1. S3 → open `UiBucketName`.
2. **Upload**.
3. Drag the **contents** of `aws/frontend/dist` (not the `dist` folder itself)
   into the bucket. The bucket root must contain `index.html`, `assets/`, and
   `nice-dcv-web-client-sdk/`.
4. Upload.

Optional CLI equivalent:

```powershell
aws s3 sync aws/frontend/dist s3://<UiBucketName> --delete
aws cloudfront create-invalidation --distribution-id <DistributionId> --paths "/*"
```

If you use the console, invalidate manually:

CloudFront → your distribution → **Invalidations** → **Create invalidation** →
`/*`.

The DCV SDK `.js` files must be served as `text/javascript`. `aws s3 sync`
guesses this from the extension; if you upload with presigned URLs, send the
`Content-Type` header explicitly for every `.js` file under
`nice-dcv-web-client-sdk/`.

## 7. Run the demo

1. Open `SiteUrl`.
2. Type: `Samsung 5G under ₹20,000`.
3. Switch on **Live shopping search**, then send.
4. Watch the right-hand live view. The worker applies a normal desktop Chrome
   fingerprint (removing AgentCore's UA token) plus `en-IN` /
   `Asia/Kolkata`, so Amazon.in search and product pages normally load. If
   Amazon shows a CAPTCHA, click and type directly in the live view; the worker
   waits up to 120 s. If Amazon still returns a **503** or no usable listings,
   the worker tries Flipkart, then the open web, and explains the fallback in
   chat.
5. Pick a phone card → **Fact-check this**.
6. Read the claim-by-claim result and final recommendation.

The UI polls the session every 1.5 s. The live-view URL is refreshed every 4
minutes while the browser is alive.

## 8. Cost guardrail (recommended)

This account has a `truthphone-monthly` **$10/month** cost budget. It was
created without an email subscriber because the account has no alternate
contact email. To add alerts:

AWS Budgets → `truthphone-monthly` → **Edit** → add notifications at 80% and
100% → enter an email address.

The main variable cost is AgentCore Browser; Bedrock tokens are second.
Everything else is pay-per-request / static hosting.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| API 500 on `/sessions` | API Lambda logs; DynamoDB table name env; IAM role |
| API 502 from Function URL | API Lambda crashed on import; check the Python runtime and zip |
| Worker never updates | Worker Lambda logs; `WORKER_FUNCTION_NAME`; async invoke permission |
| `AccessDenied` on Bedrock | Model access in `ap-south-1`; worker role Bedrock policy |
| `AccessDenied` on AgentCore | Worker role's AgentCore Browser session permissions; region support |
| `Permission denied` for `playwright/driver/node` | Rebuild with `scripts/build_lambda.py`; check the printed `0o755` |
| Blank live view | Browser console; `/nice-dcv-web-client-sdk/dcvjs-esm` files; signed URL expiry; ensure the SDK `.js` files return `text/javascript`, not `binary/octet-stream` |
| Blank page or API calls fail | Test `https://<SiteUrl-hostname>/api/health`; check the `/api/*` cache behavior, the CloudFront Function rewrite, and the `ApiOriginDomain` parameter |
| `/api` returns 502/504 | Function URL hostname is correct; API Lambda is healthy; CloudFront origin read timeout is 30 s |
| Amazon 503 | Check the worker log for `Applied Amazon-safe browser fingerprint` and confirm the UA no longer contains `Amazon-Bedrock-AgentCore-Browser`. If Amazon tightened its filter, add an Indian residential/mobile proxy via AgentCore `proxyConfiguration`; the worker still falls back to Flipkart/web |
| Flipkart returns 0 cards | Flipkart DOM changed or it blocked the request; the worker continues to DuckDuckGo/Bing. Update `_FLIPKART_HARVEST_JS` if this persists |
| 409 “still working” | One job per session; wait or start a new chat |
| No phones found | Budget/brand too strict, or both retailers returned nothing; try a broader query. The web fallback may show fewer prices |
| CAPTCHA timeout | Solve it in the live view within 120 s; retry the search |

## Cleanup

1. CloudFormation → `truthphone-ui` → **Delete**. The S3 bucket is retained by
   design; empty and delete it manually afterward.
2. CloudFormation → `truthphone` → **Delete**.
3. Empty and delete the code bucket and the retained UI bucket.
4. If a browser session is stuck, stop it in the AgentCore Browser console or
   wait for its 600 s TTL.

## Notes

- No authentication. Do not put private data in sessions.
- The API is still a public Function URL. The UI calls it through CloudFront
  `/api/*`; direct calls remain possible and CORS is `*` for the demo.
- The worker zip is loaded from S3 because it is over the 50 MB direct-upload
  limit.
- Do not edit the zip by hand; the build script fixes Linux file modes.
