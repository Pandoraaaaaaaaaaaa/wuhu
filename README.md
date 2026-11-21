# wuhu

A command-line research agent built on the Anthropic Messages API. Give it one topic and it searches the web, reads pages, records every source, and writes a cited Markdown report.

## Setup

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=your-key
    export ANTHROPIC_MODEL=your-model-id
    python agent.py 'your research topic'

## How it works

1. Splits the topic into sub-questions.
2. web_search (Anthropic server tool) finds sources; fetch_page reads them in full.
3. save_source records URL, title, publisher, date, primary or secondary, and an excerpt.
4. write_report saves reports/<name>.md with inline [n] citations and an auto-generated reference list, plus sources/<name>.json. It rejects reports that cite unsaved sources.

The report includes disagreements between sources and a gaps and uncertainties section. Web search must be enabled for your Anthropic organization.

## Settings

Copy .env.example to .env and fill in ANTHROPIC_API_KEY and ANTHROPIC_MODEL. The agent reads .env on startup, and .env is git-ignored.
