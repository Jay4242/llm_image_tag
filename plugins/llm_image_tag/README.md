# LLM Image Tag Plugin

This Stash plugin suggests tags for image files using a vision-capable LLM (OpenAI-compatible API).

Features
- Adds a UI dropdown action on the image page: "Tag image (LLM)".
- Sends the image to your configured LLM endpoint and displays suggested tags in a selection modal.
- Streaming SSE support with live "thinking" and output progress in the modal.
- Matches suggested tags against existing database tags; aliases resolve to their parent tag.
- Provides the LLM with existing database tags as context (excluding tags with `ignore_auto_tag`).
- Automatically converts WebP images to PNG before sending to the LLM.
- Applies selected tags to the image via GraphQL mutations (creates new tags as needed).
- Registers as a task via `window.registerTask` for task-compatible UIs.

Notes
- This version applies suggested tags to the image via GraphQL mutations from the JS frontend.
- The Python backend requires **Pillow** for WebP-to-PNG conversion (`pip install Pillow`).

Configuration (Settings)
- llmBaseUrl (env: LLM_BASE_URL; default http://localhost:11434/v1)
- llmModel (env: LLM_MODEL; default gemma3:4b-it-q8_0)
- llmTemp (env: LLM_TEMP; default 0.7)
- llmMaxTokens (env: LLM_MAX_TOKENS; default -1)
- llmTimeout (env: LLM_TIMEOUT; default 3600)
- includeTagDescriptions (env: LLM_INCLUDE_TAG_DESCRIPTIONS; default true)
- zzdebugTracing (BOOLEAN; enables extra debug logging)

Configuration (Environment Variables)
- LLM_API_KEY: API key for authenticated LLM endpoints.
- LLM_TAG_PROMPT: Custom system prompt override for the tagging assistant.

Installation
1. Install the Python dependency: `pip install Pillow`.
2. Place this folder in your Stash plugins directory as `llm_image_tag`.
3. Reload plugins in the Stash UI.

Usage
- Open an image page and use the operations menu (three dots) to run "Tag image (LLM)", or use the registered task if your UI supports it.
- A modal will open showing LLM progress, then suggested tags with checkboxes. Select the tags you want and click "Apply Tags".
