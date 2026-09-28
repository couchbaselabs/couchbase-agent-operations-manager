# Couchbase Agent Operations Manager SDK - ChatGPT skill

This is the ChatGPT-flavored version of the AOM SDK integration skill
bundled with the Couchbase Agent Operations Manager appliance. It's the
same integration knowledge as the Claude Skill (`SKILL.md` in the sibling
`claude/` package), reformatted for how ChatGPT-based tools take custom
instructions - there's no single portable "skill" file format across AI
assistants, so use whichever of these fits how you're using ChatGPT:

- **Custom GPT**: paste the contents of `INSTRUCTIONS.md` into the
  Custom GPT's *Instructions* field (GPT Builder -> Configure).
- **Assistants / Responses API**: pass `INSTRUCTIONS.md` as the
  `instructions` (system/developer message) when creating the assistant
  or agent.
- **A ChatGPT-based coding agent that reads an `AGENTS.md` file**: copy
  `INSTRUCTIONS.md` to `AGENTS.md` at the repo root (or append it to an
  existing one) so the agent picks it up automatically when working in
  that codebase.

Whichever you choose, the content teaches the assistant how to add
`aom_sdk` (the appliance's Python client) to an agent codebase: getting
the SDK, configuring a client, replacing hand-rolled tool calls with
discover/invoke, bridging to a real MCP server, grouping work into
traced runs, caching LLM completions, adding agent memory, retrieving
from the governed knowledge base, and handling the refusals a governed
gateway returns - rate limits, budgets, guardrails, agent scoping and
tools held for human approval. See `INSTRUCTIONS.md`.
