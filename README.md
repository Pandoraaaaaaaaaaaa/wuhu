# wuhu

A minimal command-line AI agent built on the Anthropic Messages API.

## Setup

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=your-key
    export ANTHROPIC_MODEL=your-model-id
    python agent.py

## Tools

- list_dir, read_file, write_file: limited to the working directory (AGENT_WORKDIR, default is the current folder)
- run_command: asks for your approval before every command
