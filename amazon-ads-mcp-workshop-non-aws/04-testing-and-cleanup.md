---
title: "Module 4: Testing and Cleanup"
description: Test the agent with structured scenarios, troubleshoot common issues, and clean up the workshop environment.
---

# Module 4: Testing and Cleanup

>[NOTE] Run structured test scenarios against your agent, troubleshoot common issues, and clean up your workshop environment.

## When to Use

- You have completed Module 3 and have a working agent
- You want to verify the agent handles different advertising workflows correctly

---

## Step 1: Run Structured Tests

Create a file called `test_agent.py` that runs predefined prompts against the agent:

```python
"""Structured tests for the Amazon Ads agent."""

import asyncio
import sys

# Import the build_agent function from your agent module
from ads_agent import build_agent


async def run_tests():
    print("Building agent...")
    agent = await build_agent()

    tests = [
        {
            "name": "Account Discovery",
            "prompt": "List all my advertiser accounts with their IDs, names, and marketplace.",
            "expect": ["account", "advertiser"],
        },
        {
            "name": "Tool Listing",
            "prompt": "What tools do you have available? List the tool names grouped by category.",
            "expect": ["account_management", "campaign_management"],
        },
    ]

    passed = 0
    for test in tests:
        print(f"\n{'=' * 60}")
        print(f"TEST: {test['name']}")
        print(f"{'=' * 60}")
        print(f"Prompt: {test['prompt'][:80]}...")

        try:
            response = await agent.ainvoke(
                {"messages": [{"role": "user", "content": test["prompt"]}]}
            )
            reply = response["messages"][-1].content
            print(f"\nResponse:\n{reply[:500]}...")

            result_lower = reply.lower()
            hits = [term for term in test["expect"] if term in result_lower]
            if len(hits) == len(test["expect"]):
                print(f"\nResult: PASS (found: {hits})")
                passed += 1
            else:
                missing = [t for t in test["expect"] if t not in result_lower]
                print(f"\nResult: PARTIAL (missing: {missing})")

        except Exception as e:
            print(f"\nResult: FAIL ({e})")

    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{len(tests)} tests passed")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(run_tests())
```

Run the tests:

```bash
python test_agent.py
```

---

## Step 2: Troubleshooting

### OAuth / Connection Errors

| Issue | Solution |
|---|---|
| **403 Forbidden** | Verify the `Authorization` header contains the raw access token (no `Bearer` prefix). Check that your Client ID and Refresh Token are correct. |
| **401 Unauthorized** | Your access token may have expired. The token is short-lived — re-run the agent to get a fresh one. |
| **429 Rate Limited** | The agent includes retry logic with backoff. If it persists, wait a few minutes before retrying. |
| **Connection refused** | Verify the MCP Server URL matches your region (NA, EU, or FE). |

### Agent Errors

| Issue | Solution |
|---|---|
| **ModuleNotFoundError: langchain** | Activate your virtual environment: `source .venv/bin/activate` |
| **Anthropic API key invalid** | Verify `ANTHROPIC_API_KEY` in your `.env` file. Get a key from [console.anthropic.com](https://console.anthropic.com/). |
| **No tools discovered** | Check your OAuth credentials. Run `python test_connection.py` from Module 2 to isolate the issue. |
| **Agent doesn't call tools** | The `TOOL_FILTER` may be too restrictive. Try removing it or expanding the groups. |

### Quick Diagnostic Checklist

- [ ] Virtual environment is activated (`(.venv)` in prompt)
- [ ] `.env` file contains all 4 required variables
- [ ] `python test_connection.py` discovers tools successfully
- [ ] Anthropic API key is valid (test at [console.anthropic.com](https://console.anthropic.com/))

---

## Step 3: Cleanup

To remove all workshop resources:

```bash
# Deactivate the virtual environment
deactivate

# Delete the workshop directory
rm -rf workshop-ads-agent
```

That's it — no cloud resources to clean up since everything ran locally.

---

## What You Built

Across this workshop you:

1. Connected to the Amazon Ads MCP Server via Streamable HTTP with OAuth authentication
2. Discovered 50+ advertising tools using the MCP protocol
3. Built a LangGraph agent powered by Claude (Anthropic API) that orchestrates Ads API calls
4. Tested the agent with account discovery and campaign management workflows

### Next Steps

- Explore additional tool groups beyond `account_management`, `campaign_management`, and `reporting`
- Add conversation memory for multi-turn interactions
- Build a web UI using Streamlit or Gradio
- For production deployments with AWS hosting, see the [full AWS workshop](../merged-workshop/01-overview.md) which covers Amazon Bedrock AgentCore deployment

### Resources

- [Amazon Ads MCP Server documentation](https://advertising.amazon.com/API/docs/en-us/mcp/get-started)
- [Amazon Ads API documentation](https://advertising.amazon.com/API/docs/en-us)
- [LangGraph documentation](https://langchain-ai.github.io/langgraph/)
- [Anthropic API documentation](https://docs.anthropic.com/)
- [Workshop code on GitHub](https://github.com/amzn/ads-advanced-tools-docs/tree/main/gen-ai-samples)

---

[← Previous: Build an Ads Agent](03-build-ads-agent.md)
