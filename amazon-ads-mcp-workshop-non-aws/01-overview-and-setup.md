---
title: "Module 1: Overview and Setup"
description: Workshop overview, prerequisites, and environment setup for building an Amazon Ads agent using the Anthropic API and the Amazon Ads MCP Server.
---

# Module 1: Overview and Setup

>[NOTE] Set up your development environment and verify all prerequisites before building your Amazon Ads agent.

---

## Workshop Overview

This workshop walks you through connecting to the Amazon Ads MCP Server and building a LangGraph agent powered by Claude via the Anthropic API. No AWS account is required — everything runs locally on your machine.

By the end of this workshop you will have:

- Connected to the Amazon Ads MCP Server via Streamable HTTP
- Discovered and explored available advertising tools
- Built a LangGraph agent that orchestrates Amazon Ads API calls using Claude
- Tested the agent with account discovery and campaign management prompts

### Target Audience

Solutions architects, software engineers, and technical leads who work with the Amazon Ads API and want to build AI-powered automation using MCP — without requiring an AWS account.

---

## Contributors

This workshop was created by [Chintan Sanghavi](https://www.linkedin.com/in/chintansanghavi/) and [Christelle Ngambula](https://www.linkedin.com/in/chrisngambula/), both Senior Solutions Architects at Amazon Ads. For questions or additional details, connect with either contributor via LinkedIn.

---

## Architecture

```
┌─────────────────────┐
│    User Prompt       │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   LangGraph Agent    │
│   (runs locally)     │
└──────────┬──────────┘
           │
     ┌─────┴──────┐
     │             │
     ▼             ▼
┌──────────┐  ┌──────────────┐
│ Anthropic│  │  Amazon Ads  │
│   API    │  │  MCP Server  │
│ (Claude) │  │(Streamable HTTP)│
└──────────┘  └──────┬───────┘
                     │
                     ▼
              ┌──────────────┐
              │  Amazon Ads  │
              │     API      │
              └──────────────┘
```

---

## Step 1: Verify Prerequisites

| Prerequisite | Details |
|---|---|
| **Python 3.13+** | Verify with `python --version`. |
| **pip** | Python package manager. Ships with Python 3.13+. |
| **Anthropic API Key** | Obtain from [console.anthropic.com](https://console.anthropic.com/). |
| **Amazon Ads API Client ID** | Obtain from the [Amazon Ads developer console](https://advertising.amazon.com/API/docs/en-us/get-started/overview). |
| **Amazon Ads API Client Secret** | Obtain from the Amazon Ads developer console. |
| **Amazon Ads API Refresh Token** | Generated during the OAuth flow for your Ads API application. |

>[NOTE] This workshop is designed for Linux and macOS environments. If you are on Windows, we recommend using Windows Subsystem for Linux (WSL) to follow along.

---

## Step 2: Create a Virtual Environment

```bash
mkdir workshop-ads-agent
cd workshop-ads-agent
python -m venv .venv
```

Activate the virtual environment:

```bash
# macOS / Linux
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

You should see `(.venv)` in your terminal prompt.

---

## Step 3: Install Dependencies

Create a `requirements.txt`:

```text
langchain
langchain-core
langchain-anthropic
langgraph
langchain-mcp-adapters
pydantic
python-dotenv
requests
```

Install:

```bash
pip install -r requirements.txt
```

---

## Step 4: Configure Environment Variables

Create a `.env` file:

```bash
# Anthropic
ANTHROPIC_API_KEY=<YOUR_ANTHROPIC_API_KEY>

# Amazon Ads
CLIENT_ID=<YOUR_AMAZON_ADS_CLIENT_ID>
CLIENT_SECRET=<YOUR_AMAZON_ADS_CLIENT_SECRET>
REFRESH_TOKEN=<YOUR_AMAZON_ADS_REFRESH_TOKEN>

# Optional: filter which tool groups to load
TOOL_FILTER=account_management,campaign_management,reporting
```

>[NOTE] Never commit your `.env` file to version control. Add `.env` to your `.gitignore`.

---

## Step 5: Verify Your Setup

Create a file called `verify_setup.py`:

```python
"""Verify workshop environment setup."""
import sys

checks = []

v = sys.version_info
checks.append(("Python 3.13+", v.major == 3 and v.minor >= 13))

for pkg in ["langchain", "langgraph", "langchain_anthropic", "langchain_mcp_adapters", "pydantic", "dotenv", "requests"]:
    try:
        __import__(pkg)
        checks.append((f"Package: {pkg}", True))
    except ImportError:
        checks.append((f"Package: {pkg}", False))

import os
from dotenv import load_dotenv
load_dotenv()
for var in ["ANTHROPIC_API_KEY", "CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN"]:
    checks.append((f"Env: {var}", bool(os.getenv(var))))

print("\n=== Workshop Environment Check ===\n")
all_pass = True
for name, passed in checks:
    icon = "[OK]" if passed else "[!!]"
    print(f"  {icon} {name}: {'PASS' if passed else 'FAIL'}")
    if not passed:
        all_pass = False

print()
if all_pass:
    print("All checks passed. You are ready for the workshop!")
else:
    print("Some checks failed. Please fix the missing items before proceeding.")
```

Run it:

```bash
python verify_setup.py
```

All checks should show `[OK]`.

---

## Module Listing

| Module | Title | Time |
|---|---|---|
| 01 | Overview and Setup (this module) | 15 min |
| 02 | Connect to Amazon Ads MCP Server | 20 min |
| 03 | Build an Ads Agent | 45 min |
| 04 | Testing and Cleanup | 20 min |

**Total estimated time: ~1.5 hours**

---

[Next: Connect to Amazon Ads MCP Server →](02-connect-ads-mcp-server.md)
