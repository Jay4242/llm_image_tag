# LLM Image Tag Plugin

This Stash plugin suggests tags for image files using a vision-capable LLM (OpenAI-compatible API).

Features
- Adds a UI dropdown action on the image page: “Tag image (LLM)”.
- Sends the image to your configured LLM endpoint and logs suggested tags.
- Minimal settings to configure server URL, model, temperature, max tokens, and timeout.

Notes
- This initial version logs the suggested tags but does not yet persist them to the database. That can be added by calling GraphQL mutations to upsert and attach tags.
- The plugin works without external dependencies by using Python’s standard library HTTP client.

Configuration
- llmBaseUrl (env: LLM_BASE_URL; default http://localhost:11434/v1)
- llmModel (env: LLM_MODEL; default gemma3:4b-it-q8_0)
- llmTemp (env: LLM_TEMP; default 0.7)
- llmMaxTokens (env: LLM_MAX_TOKENS; default -1)
- llmTimeout (env: LLM_TIMEOUT; default 3600)

Installation
1. Place this folder in your Stash plugins directory as `llm_image_tag`.
2. Reload plugins in the Stash UI.

Usage
- Open an image page and use the operations menu (three dots) to run “Tag image (LLM)”, or use the registered task if your UI supports it.
