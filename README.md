# TruthPhone on AWS — Real-Time Shopping & Fact-Checking Multi-Agent Platform

🌐 **Live Deployed Application:** [https://d1e35mjxk5wi14.cloudfront.net](https://d1e35mjxk5wi14.cloudfront.net)  
⚡ **Region:** `ap-south-1` (Mumbai)

---

## 🌟 Overview

**TruthPhone** is an AWS-native, multi-agent smartphone shopping and verification platform built on **Amazon Bedrock**, **AgentCore Browser**, and **AWS Lambda**.

It combines interactive recommendation with real-time web verification to help users discover smartphones and rigorously fact-check e-commerce listing claims before purchasing.

- 💬 **Interactive Guided Buying**: Clarifies requirements (budget, brand, use-case).
- 🌐 **Live Real-time Web Scraping**: Live Chrome browser on Amazon Bedrock AgentCore Browser streamed into the React UI with interactive DCV.
- 🔬 **Automated Claim Verification**: Fact-checks specs, battery claims, and features against live independent benchmarks and user opinions from **GSMArena**.
- 🚨 **Risk & Red Flag Synthesis**: Summarizes trade-offs, potential dealbreakers, and confidence scores.

---

## 🏗 Architecture

```
React UI (S3 + CloudFront)
   │  fetch /api/* (same origin)
   ▼
CloudFront ── /api/* proxy ──► API Lambda (FastAPI + Mangum) ── async invoke ──► Worker Lambda
   │                                              │ Bedrock Claude Haiku 4.5
   ▼                                              │ Playwright over CDP
DynamoDB session ◄──── live-view URL ─────────────┤
                                                  ▼
                                     AgentCore Browser (real Chrome)
                               Amazon.in → Flipkart → DuckDuckGo / GSMArena
```

- **Frontend**: React 18 + TypeScript + Vite hosted on private S3 behind Amazon CloudFront with Origin Access Control.
- **Compute**: API Lambda (Function URL) + Async Worker Lambda (900s timeout, 2GB RAM).
- **LLM Intelligence**: Claude Haiku 4.5 via the `in.anthropic.claude-haiku-4-5-20251001-v1:0` inference profile in `ap-south-1`.
- **State Store**: DynamoDB session table (`truthphone-sessions`) with automatic TTL.

---

## 📂 Project Structure

```
.
├── ARTICLE.md                  # Hackathon article and design backstory
├── README.md                   # Project overview & architecture
└── aws/
    ├── backend/
    │   ├── api_handler.py      # Mangum API Lambda entrypoint
    │   ├── worker_handler.py   # Async worker Lambda entrypoint
    │   └── truthphone/
    │       ├── agents.py       # Bedrock agents & fact-checking prompts
    │       ├── amazon.py       # Amazon scraper & Flipkart/web fallback
    │       ├── api.py          # FastAPI routes
    │       ├── browser.py      # AgentCore Browser & Playwright CDP
    │       ├── config.py       # Configuration & environment loader
    │       ├── models.py       # Pydantic state models
    │       ├── store.py        # DynamoDB session store
    │       └── worker.py       # Multi-agent orchestrator & GSMArena reviews
    ├── frontend/               # React + TypeScript + DCV Live Browser UI
    ├── infra/
    │   ├── backend.yaml        # CloudFormation / SAM backend template
    │   └── frontend.yaml       # CloudFormation S3 + CloudFront template
    ├── scripts/
    │   ├── build_lambda.py     # Cross-compiles Linux wheels and builds zips
    │   ├── clean.ps1           # Deletes build outputs, node_modules, caches
    │   ├── package.ps1         # Packaging script
    │   └── run_local.ps1       # Local runner against AWS Bedrock
    ├── requirements-api.txt
    ├── requirements-worker.txt
    └── DEPLOY.md               # AWS deployment guide
```

---

## 🚀 Deployment Guide

Detailed deployment instructions for CloudFormation, S3, and CloudFront can be found in [aws/DEPLOY.md](file:///e:/truthphone%20for%20aws-antigravity/aws/DEPLOY.md).

---

## 📜 License
MIT License
