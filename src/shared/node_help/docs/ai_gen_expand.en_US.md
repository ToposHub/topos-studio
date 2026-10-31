# AI Gen Expand (Generative Expand / Fill)

Cloud generative outpainting and masked fill (Volcengine Ark adapter behind the shared provider contract).

## Use cases (important)

**Still frames / per-frame independent generation** — canvas extension, plate generation, still-frame repair. Per-frame video generation flickers (i2v is a later release).

## Inputs and parameters

- `image`: base image; `mask` (optional): fill region
- **Expand Left/Top/Right/Bottom**: per-edge expansion ratios (0-1) driving an automatic border mask
- **Prompt / Negative / Model / Seed / Strength**
- Results are content-address cached (prompt+seed+model+input revision): repeat evaluations/exports never re-charge

## Keys and cost (constraint 12)

- Keys come only from preferences/env — never stored in the graph or cache
- Without a key or provider the node sits in a visible NEEDS_CONFIG state passing the input through — no crash, no silent failure
- One cost confirmation per batch (not per image)

## Known boundaries

- MVP executes jobs synchronously at evaluation (ai_compute_manager background queueing is follow-up wiring); without a GL upload channel the result lands in cache while the view keeps the passthrough
