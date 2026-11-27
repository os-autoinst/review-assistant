# Base pinned by digest, resolved from registry.opensuse.org/opensuse/tumbleweed:latest on 2026-11-10.
FROM registry.opensuse.org/opensuse/tumbleweed@sha256:28dc49bdc6463892bc6346d08f7972eb71b090d8752b05ae827d0d07f2e84660

RUN zypper --non-interactive install --no-recommends git gh python313 python314 uv \
    && zypper clean --all

# Hermes v0.22.2 is installed from its source at the pinned commit with its uv.lock, which resolves for Python 3.14 only.
RUN git clone --no-checkout https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent \
    && git -C /opt/hermes-agent checkout --detach d923a5eb487a17c0a68801bba9ef0f56c81040a8 \
    && cd /opt/hermes-agent && uv sync --frozen --no-dev --extra vertex --python /usr/bin/python3.14

# The skill is baked in at the pin of config/reviewer.example.yaml (kept equal by tests/test_container.py), so the
# read-only root filesystem keeps the agent from changing the playbook or review-lint.py the CLI runs later.
ARG SKILL_SHA=a6d255abcbce67a380f6716c94e861869ed64882
RUN git clone --quiet https://github.com/os-autoinst/openQA-skill.git /opt/skills/${SKILL_SHA} \
    && git -C /opt/skills/${SKILL_SHA} checkout --quiet --detach ${SKILL_SHA} \
    && git config --system --add safe.directory /opt/skills/${SKILL_SHA}

# Only package sources are copied: credentials live outside the repository and must not enter the image.
WORKDIR /opt/review-assistant
COPY pyproject.toml Readme.md ./
COPY review_assistant ./review_assistant
RUN uv venv --python /usr/bin/python3.13 /opt/venv && VIRTUAL_ENV=/opt/venv uv pip install .

COPY --chmod=0755 deploy/check-isolation.sh /opt/check-isolation.sh

ENV PATH=/opt/venv/bin:/opt/hermes-agent/.venv/bin:$PATH
ENTRYPOINT ["review-assistant"]
