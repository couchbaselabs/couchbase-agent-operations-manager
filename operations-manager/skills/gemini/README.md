# Couchbase Agent Operations Manager SDK - Gemini skill

This is the Gemini-flavored version of the AOM SDK integration skill
bundled with the Couchbase Agent Operations Manager appliance. It's the
same integration knowledge as the Claude Skill (`SKILL.md` in the sibling
`claude/` package), reformatted for how Gemini-based tools take custom
instructions:

- **Gemini CLI / Gemini Code Assist**: drop `GEMINI.md` at the root of the
  target repository (or merge it into an existing one) - both read a
  `GEMINI.md` context file automatically, the same way Claude Code reads
  `CLAUDE.md`.
- **A custom Gem**: paste the contents of `GEMINI.md` into the Gem's
  instructions field (Gemini -> create a Gem -> Instructions).
- **Vertex AI / the Gemini API**: pass `GEMINI.md` as the system
  instruction when creating the model session.

Whichever you choose, the content teaches the assistant how to add
`aom_sdk` (the appliance's Python client) to an agent codebase: getting
the SDK, configuring a client, replacing hand-rolled tool calls with
discover/invoke, bridging to a real MCP server, grouping work into
traced runs, caching LLM completions, adding agent memory, retrieving
from the governed knowledge base, and handling the refusals a governed
gateway returns - rate limits, budgets, guardrails, agent scoping and
tools held for human approval. See `GEMINI.md`.
