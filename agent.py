import os
import subprocess
import sys
from pathlib import Path

import anthropic

WORKDIR = Path(os.environ.get('AGENT_WORKDIR', '.')).resolve()
MODEL = os.environ.get('ANTHROPIC_MODEL')
MAX_STEPS = 20
MAX_OUTPUT = 10000

SYSTEM = ('You are a helpful coding and automation agent running on the user machine. '
          'Work inside the working directory: ' + str(WORKDIR) + '. '
          'Use tools when needed, keep answers concise, reply in the same language as the user, '
          'and say plainly if a tool fails.')

TOOLS = [
    {'name': 'list_dir',
     'description': 'List files in a directory, relative to the working directory.',
     'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': []}},
    {'name': 'read_file',
     'description': 'Read a UTF-8 text file, relative to the working directory.',
     'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}},
    {'name': 'write_file',
     'description': 'Write a UTF-8 text file (creates parent folders, overwrites existing content).',
     'input_schema': {'type': 'object',
                      'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
                      'required': ['path', 'content']}},
    {'name': 'run_command',
     'description': 'Run a shell command in the working directory. The user must approve each command.',
     'input_schema': {'type': 'object', 'properties': {'command': {'type': 'string'}}, 'required': ['command']}},
]


def safe_path(rel):
    full = (WORKDIR / rel).resolve()
    if full != WORKDIR and WORKDIR not in full.parents:
        raise ValueError('path escapes the working directory')
    return full


def list_dir(path='.'):
    p = safe_path(path)
    names = sorted(x.name + ('/' if x.is_dir() else '') for x in p.iterdir())
    return chr(10).join(names) or '(empty)'


def read_file(path):
    return safe_path(path).read_text(encoding='utf-8')


def write_file(path, content):
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding='utf-8')
    return 'Wrote ' + str(len(content)) + ' chars to ' + path


def run_command(command):
    print('Agent wants to run: ' + command)
    if input('Allow? [y/N] ').strip().lower() != 'y':
        return 'User denied the command.'
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return 'Command timed out after 60s.'
    return 'exit code ' + str(r.returncode) + chr(10) + r.stdout + r.stderr


HANDLERS = {
    'list_dir': lambda a: list_dir(a.get('path', '.')),
    'read_file': lambda a: read_file(a['path']),
    'write_file': lambda a: write_file(a['path'], a['content']),
    'run_command': lambda a: run_command(a['command']),
}


def execute(name, args):
    try:
        out = HANDLERS[name](args)
    except Exception as e:
        out = 'Error: ' + str(e)
    return out[:MAX_OUTPUT]


def run_agent(client, messages):
    for _ in range(MAX_STEPS):
        resp = client.messages.create(model=MODEL, max_tokens=4096, system=SYSTEM,
                                      tools=TOOLS, messages=messages)
        messages.append({'role': 'assistant', 'content': resp.content})
        for block in resp.content:
            if block.type == 'text' and block.text.strip():
                print('agent> ' + block.text)
        if resp.stop_reason != 'tool_use':
            return
        results = []
        for block in resp.content:
            if block.type == 'tool_use':
                print('[tool] ' + block.name + ' ' + str(block.input))
                results.append({'type': 'tool_result', 'tool_use_id': block.id,
                                'content': execute(block.name, block.input)})
        messages.append({'role': 'user', 'content': results})
    print('[stopped: reached the max number of steps]')


def main():
    if not os.environ.get('ANTHROPIC_API_KEY'):
        sys.exit('Set ANTHROPIC_API_KEY first.')
    if not MODEL:
        sys.exit('Set ANTHROPIC_MODEL to the model ID you want to use.')
    client = anthropic.Anthropic()
    messages = []
    print('Agent ready. Working dir: ' + str(WORKDIR) + '. Type exit to quit.')
    while True:
        try:
            user = input('you> ').strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user in ('exit', 'quit'):
            break
        if not user:
            continue
        messages.append({'role': 'user', 'content': user})
        run_agent(client, messages)


if __name__ == '__main__':
    main()
