"""
workers/ — long-running background helpers that continuously enrich the
HanryxVault local database.

Each helper is a subclass of `workers.base.Worker` and pulls work from
the shared `bg_task_queue` table. Workers can run one batch and exit
(`--once`, suitable for cron) or stay resident polling for new work
(`--loop`, suitable for systemd / docker-compose long-running services).

Concrete helpers live as sibling modules:
  - workers.image_health  — verifies on-disk card images exist & decode
  - workers.image_mirror  — (planned) downloads missing images from URLs
  - workers.clip_embedder — (planned) computes ViT-B/32 embeddings via ONNX
  - workers.ocr_indexer   — (planned) PaddleOCR text extraction (CJK)

To add a new helper: subclass Worker, set TASK_TYPE, implement seed()
and process(), then register it in workers.run.WORKERS. The framework
handles claim / heartbeat / retry / per-run logging uniformly.
"""
