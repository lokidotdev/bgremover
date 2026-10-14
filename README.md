# BG Remover

Self-hosted background-removal API with an admin panel that controls which websites may use it.

- **Model:** BiRefNet-general (the strongest open background-removal model), run on the GPU via ONNX Runtime in fp16.
- **Edges:** soft alpha matte plus foreground colour decontamination, so there's no halo of the old background.
- **Admin panel** (`/admin`): domain allow-list, usage stats and a playground.

## Run

```bash
uv sync                      # first time; installs CUDA/cuDNN wheels too
cp .env.example .env         # set BGR_ADMIN_PASSWORD and BGR_SECRET_KEY
uv run python -m bgremover.main
```

On first start the model (~1 GB) is downloaded to `models/` and converted to fp16 once (~40 s).
Then open http://localhost:8000/admin.

Tests: `uv run pytest`

## API

`POST /v1/remove`: send the image as multipart field `image` or as the raw body.

| option         | values                       | default |
|----------------|------------------------------|---------|
| `format`       | `png`, `webp`, `jpg`, `mask` | `png`   |
| `background`   | hex colour, e.g. `#ffffff`   | none (transparent) |
| `crop`         | `true` / `false`             | `false` |
| `refine_edges` | `true` / `false`             | `true`  |

Options can go in the query string or as form fields. Responses include `X-Image-Width`, `X-Image-Height` and `X-Processing-Ms`.
Errors are JSON `{"error": "..."}`: 403 (domain not allowed), 413 (too large), 422 (bad image/option).

## Domain allow-list

The caller's host comes from `Origin` (falling back to `Referer`). `example.com` matches that host on any
scheme/port; `*.example.com` matches its subdomains. CORS headers are only sent to allowed origins.

Browsers set these headers themselves, so this reliably stops *other websites* from using your API.
Non-browser clients can forge them, so requests with no Origin are blocked unless you enable
"Allow requests without an Origin" in the admin panel. If you need to lock down server-to-server use,
add API keys as well.

## Performance

On an RTX 4070 Laptop GPU, a 1600×1200 photo takes ~0.9 s end to end (inference ~490 ms fp16, edge
refinement ~150 ms, PNG encode ~150 ms). GPU work is serialised and CPU stages overlap, so throughput
holds under concurrent load.

For more speed, try TensorRT (`uv sync --extra tensorrt`, then `BGR_USE_TENSORRT=1`). The first start
builds an engine, which takes several minutes; it's cached in `models/trt-cache/`.

## Layout

```
bgremover/
  matting/      Remover: bytes in, cutout out (decode, EXIF, limits, model, edges, encode)
    birefnet.py   MaskModel adapter: BiRefNet on ONNX Runtime (CUDA / TensorRT / CPU)
  access.py     DomainPolicy: allow-list rules + check(origin, referer)
  usage.py      UsageLog: request log + dashboard aggregates
  auth.py       AdminAuth: password login, signed session cookie
  app.py        HTTP wiring: /v1/remove, /admin/api/*, /admin
  admin_ui/     static admin panel (no build step)
```
