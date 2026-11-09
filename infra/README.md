# Deploy AgentOps to AWS Lambda

Terraform in this folder creates everything except the database. GitHub Actions then builds the
images and deploys every push to `main` that passes CI.

## What runs where

```
Browser ──HTTPS──► agentops-api      (Lambda + function URL, response streaming for the live view)
                     │  FastAPI app, unchanged, behind the Lambda Web Adapter
                     │  "kicks" ─────► agentops-worker   (Lambda, outside the VPC)
                     ▼                    │ drains the step queue, then exits
                  Postgres ◄──────────────┤ (any Postgres: Neon's free tier works)
                                          │ runs the gates ──► agentops-sandbox  ┐ sealed VPC:
                                          │                                       │ no internet route,
                                          └ merged code ──► S3 ◄── agentops-preview ┘ S3 endpoint only
EventBridge: every 15 min ──► agentops-worker   (the sweep: picks up anything a kick missed)
```

| Locally | On AWS |
|---|---|
| Workers are long-running processes that poll | A Lambda worker is started ("kicked") whenever the API creates work, drains the queue and exits; a schedule sweeps every 15 min |
| A worker killed mid-step | A function that hits the 15-min limit mid-step: its lease runs out and the next worker reclaims the step. Same engine, same guarantee |
| Gates run in a Docker container with `--network none` | Gates run in a separate Lambda function in a VPC with no internet route, no permissions, no secrets, and a scrubbed environment for the script |
| "Open the shop" starts a Docker container on 127.0.0.1:8830 | A preview function with its own URL serves the original shop, or the merged one from S3 (written only after a person approves the merge) |
| Secrets in `.env` | Secrets in SSM Parameter Store (SecureString), which you set yourself; Terraform never sees them |

What doesn't change: the engine, the workflow, the UI. The AWS paths are switched on by settings
(`engine/config.py`: `SANDBOX`, `WORKER_KICK`, `PREVIEW`, ...).

## Cost

There's no NAT gateway, load balancer or RDS, so nothing is billed by the hour. A demo's traffic fits
in Lambda's always-free monthly allowance. The fixed cost is ECR image storage: 3 repositories, last
3 images each. That's an estimate, not a measured bill; set `budget_email` to get an alert.

The defaults keep a public demo cheap and safe:
- `llm_mode = "fake"`: the offline replay (pre-written model replies). No model calls, no key needed.
- `max_runs_per_day = 30`: the API refuses new runs after that.
- Only the `sdlc` workflow can be started.

With `llm_mode = "live"`, anyone with the URL can spend your model quota, up to that daily cap.

## Deploy

You need: an AWS account with the CLI signed in, Terraform 1.6+ (or the `hashicorp/terraform` Docker
image), and a Postgres database. With Neon, use the **direct** connection string, not the pooled
(`-pooler`) one: psycopg's prepared statements need a real session.

**1. Infrastructure, without the functions yet**

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # set github_repo; keep create_functions = false
terraform init
terraform apply
```

This creates the ECR repositories, the sealed VPC, the S3 bucket, the SSM parameters, the IAM roles
and the GitHub deploy role. Lambda can't be created before its image exists, hence two applies.

**2. Push the images**

In GitHub: Settings > Secrets and variables > Actions > **Variables**. Add `AWS_ROLE_ARN` and
`AWS_REGION` from `terraform output github_variables`. Then go to Actions > **deploy** and click **Run
workflow**. It builds and pushes the three images, and reports that the functions don't exist yet.

**3. Secrets**

```bash
aws ssm put-parameter --overwrite --type SecureString --name /agentops/database_url --value 'postgresql://...'
# only for llm_mode = "live":
aws ssm put-parameter --overwrite --type SecureString --name /agentops/llm_api_key --value '...'
```

**4. The functions**

Set `create_functions = true` in `terraform.tfvars`, then run `terraform apply`. Open `terraform output app_url`.
The tables are created on the first request.

**From then on**, every push to `main` runs `ci`. If it passes, `deploy` builds the images, points the
four functions at them, and checks `/health` on the live URL.

## Notes

- **Tear down:** run `terraform destroy`. Lambda takes a while to release VPC network interfaces, so
  deleting the subnets can take 20+ minutes.
- **Repo OIDC subject:** if GitHub sends your repository's OIDC subject with IDs
  (`repo:owner@123/name@456`), set `github_sub_prefix`. Read it with
  `gh api repos/OWNER/NAME/actions/oidc/customization/sub`.
- **Existing GitHub OIDC provider:** if the account already has one (for example from another
  project), set `create_github_oidc_provider = false`.
- **Terraform state** is local (`terraform.tfstate`, git-ignored). Back it up.
- **Preview cart:** the preview keeps the cart in memory, as the local preview does. It resets when
  Lambda starts a fresh copy of the function.
