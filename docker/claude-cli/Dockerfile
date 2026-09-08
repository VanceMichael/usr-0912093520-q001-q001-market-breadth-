FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl git python3 python3-pip python3-venv ripgrep \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /workspace /home/node/.claude \
    && chown -R node:node /workspace /home/node

LABEL ccusr.claude.nonroot="true"
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ENV HOME=/home/node
WORKDIR /workspace
USER node

CMD ["claude", "--version"]
