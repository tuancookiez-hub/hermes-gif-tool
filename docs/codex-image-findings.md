# Feasibility spike — 2026-11-05

Hermes Agent v0.22.7 (2026.9.24), Codex sign-in present. All calls went through
`hermes_cli.plugin_provider_requests.credentialed_provider_request("openai-codex", ...)`
from a throwaway plugin declaring `requires_auth: [openai-codex]` (the plugin never sees the token).
Endpoint: `https://chatgpt.com/backend-api/codex/images/generations` (same route as Hermes's bundled `openai-codex` image provider).

| Test | Result |
|---|---|
| `model: gpt-image-2.6`, low | 200, 18.2 s, 343 image tokens |
| `model: gpt-image-2`, low | 200, 16.3 s, 343 image tokens |
| `model: zz-not-a-model`, low | 200, 19.5 s, 343 image tokens |
| `output_format: gif` | 400 `GIF output is not enabled for this request.` (2.5 s) |
| 2x2 sprite sheet, medium | 200, 34.9 s, 915 image tokens, 1254x1254 |
| Local slice + align + caption + GIF | 4.0 s, 4 frames, 450 KB @480px, 1.0 MB @720px |

Conclusions
- The backend ignores `model`. No response field names the model. "GPT Image 2.6" can be neither selected nor verified on this route.
- The backend ignores `size` (asked 1024x1024, got 1536x1024 and 1254x1254). Aspect must be enforced locally.
- No direct GIF. One sprite-sheet call yields 4 consistent frames; captions are burned locally, so text is exact.
- Cost: billed against the user's ChatGPT plan image allowance, not per-call API dollars. ~35 s per animation.
- Not tested yet: `images/edits` with reference images, 3x3 (9-frame) sheets.

Found while building v0.1 (same day)
- The service sometimes returns a **transparent RGBA** sheet (spike sheet was RGB, the next was RGBA
  with stray colour in invisible pixels). `forge.flatten()` composites onto white; the prompt now asks
  for an opaque background and headroom for captions.
- The call works without an `originator` header; the plugin identifies only as `gif-forge/<version>`.
- End-to-end through the installed plugin: create 40 s, 915 image tokens, 1254x1254 sheet; chat GIF
  155-251 KB at 480px; Discord export 200-305 KB at 512px; re-render and export under 2 s, no image call.

The probe was run from a temporary junction under `$HERMES_HOME/plugins/` (required so Hermes treats
the caller as a plugin). Its runner script was removed because it imported private Hermes modules.


