import ipaddress
import json
import os
import re
import socket
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

def load_env():
    f = Path(__file__).resolve().parent / '.env'
    if not f.exists():
        return
    for line in f.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        v = v.strip().strip(chr(34) + chr(39))
        if v:
            os.environ.setdefault(k.strip(), v)


load_env()

WORKDIR = Path(os.environ.get('AGENT_WORKDIR', '.')).resolve()
REPORTS = WORKDIR / 'reports'
SOURCES = WORKDIR / 'sources'
MODEL = os.environ.get('ANTHROPIC_MODEL')
MAX_STEPS = 40
MAX_SEARCHES = 15
MAX_PAGE_CHARS = 12000
MAX_OUTPUT = 20000
SOURCE_LIST = []


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


SYSTEM = (
    'You are a careful research agent. Given a topic, produce an in-depth, well-sourced report. '
    'Workflow: 1) break the topic into 3-6 sub-questions; '
    '2) use web_search to find sources and fetch_page to read the most important pages in full instead of relying on snippets; '
    '3) call save_source for EVERY source you rely on, right after reading it, and use the returned id; '
    '4) when the evidence is sufficient, call write_report exactly once. '
    'Rules: only state what a saved source supports and cite it inline like [1][2]; if nothing reliable is found, say so. '
    'Where sources disagree, present both views and do not pick a side. '
    'Mark each source as primary (official, original data or paper) or secondary (reporting, commentary). '
    'Prefer recent and authoritative sources and give publication dates for time-sensitive claims. '
    'The report must contain: a summary, background, findings for each sub-question, disagreements between sources, '
    'and a section on gaps and uncertainties. '
    'Write the report in the language of the user. Do not write the references list yourself; write_report appends it. '
    'Today is ' + datetime.now(timezone.utc).strftime('%Y-%m-%d') + '.'
)

TOOLS = [
    {'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': MAX_SEARCHES},
    {'name': 'fetch_page',
     'description': 'Download a public web page and return its title and main text (truncated). HTML and plain text only.',
     'input_schema': {'type': 'object', 'properties': {'url': {'type': 'string'}}, 'required': ['url']}},
    {'name': 'save_source',
     'description': 'Record a source you will rely on. Returns its numeric id, used as [id] in the report.',
     'input_schema': {'type': 'object',
                      'properties': {'url': {'type': 'string'},
                                     'title': {'type': 'string'},
                                     'publisher': {'type': 'string'},
                                     'published': {'type': 'string', 'description': 'Publication date if known'},
                                     'source_type': {'type': 'string', 'enum': ['primary', 'secondary']},
                                     'excerpt': {'type': 'string', 'description': 'Short key passage or summary'}},
                      'required': ['url', 'title', 'source_type']}},
    {'name': 'write_report',
     'description': 'Save the final Markdown report. Cite sources inline as [n]. The reference list is appended automatically.',
     'input_schema': {'type': 'object',
                      'properties': {'title': {'type': 'string'}, 'markdown': {'type': 'string'}},
                      'required': ['title', 'markdown']}},
]


class TextExtractor(HTMLParser):
    SKIP = {'script', 'style', 'noscript', 'svg'}
    BREAK = {'p', 'br', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'h4'}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.title = ''
        self.skip = 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == 'title':
            self.in_title = True
        elif tag in self.BREAK:
            self.parts.append(chr(10))

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def check_public(url):
    u = urllib.parse.urlparse(url)
    if u.scheme not in ('http', 'https') or not u.hostname:
        raise ValueError('only http(s) URLs are allowed')
    for info in socket.getaddrinfo(u.hostname, None):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError('refusing to fetch a non-public address')


def fetch_page(a):
    url = a['url'].strip()
    check_public(url)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; wuhu-research-agent/0.1)',
                                               'Accept': 'text/html,text/plain'})
    with urllib.request.urlopen(req, timeout=20) as r:
        ctype = r.headers.get('Content-Type', '')
        charset = r.headers.get_content_charset() or 'utf-8'
        raw = r.read(2000000)
    if 'html' not in ctype and 'text' not in ctype:
        return 'Unsupported content type: ' + ctype
    text = raw.decode(charset, errors='replace')
    title = ''
    if 'html' in ctype:
        p = TextExtractor()
        p.feed(text)
        text = ''.join(p.parts)
        title = p.title.strip()
    lines = [' '.join(x.split()) for x in text.splitlines()]
    body = chr(10).join(x for x in lines if x)
    note = chr(10) + '[truncated]' if len(body) > MAX_PAGE_CHARS else ''
    return 'Title: ' + title + chr(10) + 'URL: ' + url + chr(10) + chr(10) + body[:MAX_PAGE_CHARS] + note


def save_source(a):
    url = a['url'].strip()
    for s in SOURCE_LIST:
        if s['url'] == url:
            return 'Already saved as id ' + str(s['id'])
    s = {'id': len(SOURCE_LIST) + 1, 'url': url, 'title': a['title'].strip(),
         'publisher': a.get('publisher', ''), 'published': a.get('published', ''),
         'type': a.get('source_type', 'secondary'), 'excerpt': a.get('excerpt', '')[:600],
         'accessed': now()}
    SOURCE_LIST.append(s)
    return 'Saved as id ' + str(s['id'])


def fmt_source(s):
    bits = [s['title']]
    if s['publisher']:
        bits.append(s['publisher'])
    if s['published']:
        bits.append(s['published'])
    return '[' + str(s['id']) + '] ' + '. '.join(bits) + '. ' + s['url'] + ' (' + s['type'] + ', accessed ' + s['accessed'] + ')'


def write_report(a):
    if not SOURCE_LIST:
        return 'Error: no sources saved yet. Call save_source first.'
    body = a['markdown'].strip()
    cited = sorted({int(n) for n in re.findall(r'\[(\d+)\]', body)})
    if not cited:
        return 'Error: the report has no [n] citations.'
    ids = {s['id'] for s in SOURCE_LIST}
    bad = [n for n in cited if n not in ids]
    if bad:
        return 'Error: cited ids that were never saved: ' + str(bad) + '. Fix the citations or save those sources.'
    refs = [fmt_source(s) for s in SOURCE_LIST if s['id'] in cited]
    others = [fmt_source(s) for s in SOURCE_LIST if s['id'] not in cited]
    out = '# ' + a['title'].strip() + chr(10) + chr(10) + '_Generated ' + now() + '_' + chr(10) + chr(10) + body
    out += chr(10) + chr(10) + '## 参考文献 / References' + chr(10) + chr(10) + (chr(10) + chr(10)).join(refs)
    if others:
        out += chr(10) + chr(10) + '### 已查阅但未引用 / Reviewed, not cited' + chr(10) + chr(10) + (chr(10) + chr(10)).join(others)
    slug = re.sub(r'\W+', '-', a['title']).strip('-').lower()[:50] or 'report'
    name = slug + '-' + datetime.now().strftime('%Y%m%d-%H%M')
    REPORTS.mkdir(parents=True, exist_ok=True)
    SOURCES.mkdir(parents=True, exist_ok=True)
    (REPORTS / (name + '.md')).write_text(out + chr(10), encoding='utf-8')
    (SOURCES / (name + '.json')).write_text(json.dumps(SOURCE_LIST, ensure_ascii=False, indent=2), encoding='utf-8')
    return 'Report saved to reports/' + name + '.md, sources to sources/' + name + '.json'


HANDLERS = {'fetch_page': fetch_page, 'save_source': save_source, 'write_report': write_report}


def execute(name, args):
    try:
        out = HANDLERS[name](args)
    except Exception as e:
        out = 'Error: ' + str(e)
    return str(out)[:MAX_OUTPUT]


def research(client, topic):
    messages = [{'role': 'user', 'content': 'Research topic: ' + topic}]
    for _ in range(MAX_STEPS):
        resp = client.messages.create(model=MODEL, max_tokens=16000, system=SYSTEM, tools=TOOLS, messages=messages)
        messages.append({'role': 'assistant', 'content': resp.content})
        for b in resp.content:
            if b.type == 'text' and b.text.strip():
                print('agent> ' + b.text)
            elif b.type == 'server_tool_use':
                print('[search] ' + str(b.input.get('query', '')))
        if resp.stop_reason == 'pause_turn':
            continue
        if resp.stop_reason != 'tool_use':
            return
        results = []
        for b in resp.content:
            if b.type == 'tool_use':
                print('[tool] ' + b.name + ' ' + str(b.input)[:150])
                results.append({'type': 'tool_result', 'tool_use_id': b.id, 'content': execute(b.name, b.input)})
        messages.append({'role': 'user', 'content': results})
    print('[stopped: reached the max number of steps]')


def main():
    if not os.environ.get('ANTHROPIC_API_KEY'):
        sys.exit('Set ANTHROPIC_API_KEY first.')
    if not MODEL:
        sys.exit('Set ANTHROPIC_MODEL to the model ID you want to use.')
    import anthropic
    topic = ' '.join(sys.argv[1:]).strip() or input('Research topic> ').strip()
    if not topic:
        sys.exit('No topic given.')
    research(anthropic.Anthropic(), topic)


if __name__ == '__main__':
    main()
