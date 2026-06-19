# Containerized Harness Runners

This folder contains one-shot Docker runners for OpenCode, Pi, Hermes, and OpenClaw.

Build an image from your locally installed harness:

```bash
containerized/build_harness_image.sh opencode
containerized/build_harness_image.sh pi
containerized/build_harness_image.sh hermes
containerized/build_harness_image.sh openclaw
```

Run a benchmark task:

```bash
python3 containerized/run_containerized_opencode_benchmark.py   --model myprovider/Qwen3.6-35B   --task pt_logit_lens_layers   --openai-compatible-base-url http://192.168.90.17:8080/v1
```

Only the materialized task workspace is mounted into the agent container. Hidden graders run on the host after the container exits.
