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
# Proxy tools in fake-IP mode resolve domains into 198.18.0.0/15, so treat that range as public.
FAKE_IP_NET = ipaddress.ip_network('198.18.0.0/15')


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


SYSTEM = (
    'You are a careful research agent. Given a topic, produce an in-depth, well-sourced report. '
    'Workflow: 1) break the topic into 3-6 sub-questions; '
    '2) use web_search to find sources and fetch_page to read the most important pages in full instead of relying on snippets; if a fetch_page result says more remains, keep calling it with the offset it gives until you have read what you need; '
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
     'description': 'Download a public web page and return its title and main text, up to 12000 characters per call. If the result says more remains, call again with the offset it gives. HTML and plain text only.',
     'input_schema': {'type': 'object', 'properties': {'url': {'type': 'string'}, 'offset': {'type': 'integer', 'description': 'Character offset to continue from, default 0'}}, 'required': ['url']}},
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
        ip = ipaddress.ip_address(info[4][0])
        if ip in FAKE_IP_NET:
            continue
        if not ip.is_global:
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
    offset = max(int(a.get('offset') or 0), 0)
    total = len(body)
    chunk = body[offset:offset + MAX_PAGE_CHARS]
    end = offset + len(chunk)
    if end < total:
        note = chr(10) + '[showing chars ' + str(offset) + '-' + str(end) + ' of ' + str(total) + '. More remains: call fetch_page again with offset=' + str(end) + ']'
    else:
        note = chr(10) + '[end of page, ' + str(total) + ' chars in total]'
    return 'Title: ' + title + chr(10) + 'URL: ' + url + chr(10) + chr(10) + chunk + note


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


PROFILE_FILE = Path(__file__).resolve().parent / 'profile.md'
WORKSPACE = WORKDIR / 'workspace'


def load_profile():
    if PROFILE_FILE.exists():
        t = PROFILE_FILE.read_text(encoding='utf-8').strip()
        if t:
            return t
    return '(profile.md is empty. No personal habits given yet: use a neutral natural tone and ask about preferences when it matters.)'


def profile_block():
    return chr(10) + chr(10) + 'USER PROFILE (the user own habits, follow them):' + chr(10) + load_profile()


def ws_path(rel):
    root = WORKSPACE.resolve()
    root.mkdir(parents=True, exist_ok=True)
    full = (root / rel).resolve()
    if full != root and root not in full.parents:
        raise ValueError('path escapes the workspace folder')
    return full


def list_files(a):
    p = ws_path(a.get('path', '.'))
    names = sorted(x.name + ('/' if x.is_dir() else '') for x in p.iterdir())
    return chr(10).join(names) or '(empty)'


def read_file(a):
    return ws_path(a['path']).read_text(encoding='utf-8')


def write_file(a):
    p = ws_path(a['path'])
    if p.exists() and not a.get('overwrite'):
        return 'Error: file already exists. Use another name, or set overwrite to true only if the user asked for it.'
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(a['content'], encoding='utf-8')
    return 'Wrote ' + str(len(a['content'])) + ' chars to workspace/' + a['path']


HANDLERS.update({'list_files': list_files, 'read_file': read_file, 'write_file': write_file})

# web_search and fetch_page are the first two entries of TOOLS.
CHAT_TOOLS = TOOLS[:2] + [
    {'name': 'list_files', 'description': 'List files in the workspace folder (or a subfolder).',
     'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': []}},
    {'name': 'read_file', 'description': 'Read a UTF-8 text file from the workspace folder.',
     'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}},
    {'name': 'write_file',
     'description': 'Save a UTF-8 text file in the workspace folder, for example a draft, notes or a list. Refuses to overwrite unless overwrite is true.',
     'input_schema': {'type': 'object',
                      'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}, 'overwrite': {'type': 'boolean'}},
                      'required': ['path', 'content']}},
]


CANNOT_START = set('，。、；：？！）」』】》〉,.;:?!)]}%～…—·')
CANNOT_END = set('（「『【《〈([{')


def wrap_lines(text, font, size, width):
    from reportlab.pdfbase.pdfmetrics import stringWidth
    lines, cur = [], ''
    for ch in text:
        if cur and stringWidth(cur + ch, font, size) > width and ch not in CANNOT_START and ch != ' ':
            if cur[-1] in CANNOT_END and len(cur) > 1:
                lines.append(cur[:-1])
                cur = cur[-1] + ch
            else:
                lines.append(cur)
                cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return [x.strip() for x in lines if x.strip()]


def add_wrapped(story, style, text, width):
    from xml.sax.saxutils import escape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph
    parts = wrap_lines(text, style.fontName, style.fontSize, width)
    for i, part in enumerate(parts):
        kw = {}
        if i > 0:
            kw['spaceBefore'] = 0
        if i < len(parts) - 1:
            kw['spaceAfter'] = 0
        st = ParagraphStyle('line' + str(i), parent=style, **kw) if kw else style
        story.append(Paragraph(escape(part), st))


def export_file(a):
    fmt = str(a.get('format', '')).lower()
    if fmt not in ('docx', 'pdf'):
        return 'Error: format must be docx or pdf'
    name = a['path']
    if not name.lower().endswith('.' + fmt):
        name += '.' + fmt
    p = ws_path(name)
    if p.exists() and not a.get('overwrite'):
        return 'Error: file already exists. Use another name, or set overwrite to true only if the user asked for it.'
    p.parent.mkdir(parents=True, exist_ok=True)
    title = str(a.get('title', '')).strip()
    lines = [x.rstrip() for x in a['content'].splitlines() if x.strip()]
    if fmt == 'docx':
        import docx
        d = docx.Document()
        if title:
            d.add_heading(title, 0)
        for x in lines:
            if x.startswith('#'):
                level = min(len(x) - len(x.lstrip('#')), 3)
                d.add_heading(x.lstrip('#').strip(), level)
            elif x.startswith('- '):
                d.add_paragraph(x[2:], style='List Bullet')
            else:
                d.add_paragraph(x)
        d.save(str(p))
    else:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import SimpleDocTemplate, Spacer
        pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
        body = ParagraphStyle('body', fontName='STSong-Light', fontSize=11, leading=18, wordWrap='CJK', spaceAfter=4)
        sizes = {0: 20, 1: 16, 2: 14, 3: 12}
        story = []
        if title:
            add_wrapped(story, ParagraphStyle('title', parent=body, fontSize=sizes[0], leading=28, spaceAfter=10), title, 430)
        for x in lines:
            if x.startswith('#'):
                level = min(len(x) - len(x.lstrip('#')), 3)
                text = x.lstrip('#').strip()
                if title and text == title.strip():
                    continue
                st = ParagraphStyle('h' + str(level), parent=body, fontSize=sizes[level], leading=sizes[level] + 8, spaceBefore=8, spaceAfter=6)
                add_wrapped(story, st, text, 430)
            elif x.startswith('- '):
                add_wrapped(story, ParagraphStyle('bullet', parent=body, leftIndent=14), '• ' + x[2:], 416)
            else:
                add_wrapped(story, body, x, 430)
        if not story:
            story.append(Spacer(1, 12))
        SimpleDocTemplate(str(p), pagesize=A4, title=title or p.stem).build(story)
    return 'Exported to workspace/' + name


HANDLERS.update({'export_file': export_file})
CHAT_TOOLS.append({'name': 'export_file',
                   'description': 'Save the final deliverable as a Word (docx) or PDF file in the workspace folder. Content is plain text: lines starting with # become headings, lines starting with - become bullets. Refuses to overwrite unless overwrite is true.',
                   'input_schema': {'type': 'object',
                                    'properties': {'path': {'type': 'string'},
                                                   'format': {'type': 'string', 'enum': ['docx', 'pdf']},
                                                   'title': {'type': 'string'},
                                                   'content': {'type': 'string'},
                                                   'overwrite': {'type': 'boolean'}},
                                    'required': ['path', 'format', 'content']}})


def research_system():
    return (SYSTEM + ' The report body stays factual and neutral, but follow the formatting, language and work preferences in the profile.'
            + profile_block())


WORKFLOW = (
    ' WORKFLOW for every new task, follow this order. '
    '1) Clarify first: before any work, ask the user in detail about the final purpose of the task, the effect they want to achieve, and the deadline (ddl), so you get a complete reading of the task. Ask it all in one message and skip only what the user already said. '
    '2) If the task involves a specific person (an addressee, teacher, client, boss and so on), also ask what this person requires and what needs attention, and wait for the answer before drafting. '
    '3) Look for precedent: ask whether earlier cases or experience exist for this kind of task, and check the workspace folder with list_files and read_file. '
    '4) Gather information with web_search and fetch_page when needed, and give the source links. '
    '5) Only then do the work. Do not start step 5 before the questions in steps 1 and 2 are answered. '
    'For a trivial question that needs no deliverable, just answer directly. '
    'Never use an impolite tone or impolite wording, not even in drafts. When the user asks for a file or the deliverable is a long document, prefer Word or PDF via export_file; otherwise answer directly in the chat. '
)


RULES = (
    ' REPLY RULES (these override anything earlier in this prompt). '
    '1) Two voices. Everything you say TO the user (questions, status, assumptions, confirmations) is plain, normal speech: no tilde, no profile catchphrases, no imitation of the profile style. '
    'Only the text written FOR other people (a reply, message, draft or note) uses the profile voice. '
    'Put that text between a line that says ---草稿开始--- and a line that says ---草稿结束---, and write nothing in the profile voice outside those two lines. '
    '2) Answer directly in the chat. Do not create a file for a reply or a short draft. Use export_file or write_file only when the user asks for a file or the deliverable is a long document such as a report; in that case prefer Word or PDF. '
    '3) Missing information. If it is about the user (their own facts, what they did or plan, their commitments, opinions, relationships, schedule, or what the other person asked of them), ask the user before drafting and never guess. '
    'If it is not about the user (wording, structure, order, general background, formatting, small choices), decide it yourself with good judgment and mention the choice in one short line outside the draft. '
    'Do not leave placeholders such as [待确认] unless the user asked for a blank. '
    '4) Write only the facts, actions and commitments the user stated. Do not add claims such as having read something carefully, or promises to act in a certain way, unless the user said so. '
    '5) For a draft addressed to a specific person, make sure you know what that person requires. If the user has not said, ask first. '
)


def chat_system():
    return (
        'You are the personal assistant of the user. Anything you write FOR other people (a reply, draft, message or note) is written in the USER OWN VOICE: '
        'first person, as if the user wrote it, following the habits in the profile below (tone, wording, sentence length, forms of address, formatting). '
        'Everything you say to the user stays plain, normal speech (see REPLY RULES). '
        'Think like a careful, thorough assistant: before answering, work out the real goal behind the request, the relevant angles, risks, edge cases, '
        'alternatives and anything the user may have overlooked. Check facts with web_search and fetch_page instead of guessing, and give the source link when you rely on the web. '
        'Then give a detailed, well-organized answer that states its assumptions explicitly. '
        'Never invent facts, experiences, commitments, opinions or numbers the user has not given you. If a fact about the user is missing, ask the user instead of guessing (see REPLY RULES). '
        'Anything meant for other people is a draft: label it as a draft and never claim it was sent. Do not take actions that cannot be undone. '
        'File tools only work inside the workspace folder, and you never overwrite files unless asked. Reply in the language of the user. '
        'Today is ' + datetime.now(timezone.utc).strftime('%Y-%m-%d') + '.' + WORKFLOW + RULES + profile_block()
    )


def run_turn(client, messages, system, tools):
    for _ in range(MAX_STEPS):
        resp = client.messages.create(model=MODEL, max_tokens=16000, system=system, tools=tools, messages=messages)
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


def research(client, topic):
    messages = [{'role': 'user', 'content': 'Research topic: ' + topic}]
    run_turn(client, messages, research_system(), TOOLS)


def chat(client):
    messages = []
    print('Assistant mode. Workspace: ' + str(WORKSPACE) + '. Type exit to quit.')
    while True:
        try:
            user = input('you> ').strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user in ('exit', 'quit'):
            break
        if user:
            messages.append({'role': 'user', 'content': user})
            run_turn(client, messages, chat_system(), CHAT_TOOLS)


def main():
    if not os.environ.get('ANTHROPIC_API_KEY'):
        sys.exit('Set ANTHROPIC_API_KEY first.')
    if not MODEL:
        sys.exit('Set ANTHROPIC_MODEL to the model ID you want to use.')
    import anthropic
    client = anthropic.Anthropic()
    args = sys.argv[1:]
    if not args or args[0] == 'chat':
        chat(client)
        return
    if args[0] == 'research':
        args = args[1:]
    topic = ' '.join(args).strip() or input('Research topic> ').strip()
    if not topic:
        sys.exit('No topic given.')
    research(client, topic)


if __name__ == '__main__':
    main()
