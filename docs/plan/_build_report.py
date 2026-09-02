# ruff: noqa: E501
# 这个文件大半是内嵌的 CSS 和 HTML 模板，按 100 列硬拆反而看不懂，整文件豁免行宽。
"""把 docs/plan/*.md 汇编成单页 HTML 报告（受控子集的 Markdown 转换器）。
用法：python3 docs/plan/_build_report.py <项目根目录>
仅用于生成规划报告的可读版，不属于平台实现代码。"""

import html
import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
PLAN = ROOT / "docs" / "plan"

FILES = [
    ("README.md", "摘要"),
    ("01-requirements.md", "需求与可行性"),
    ("02-evaluation-semantics.md", "评测语义"),
    ("03-benchmark-spec.md", "基准任务"),
    ("04-runner-protocol.md", "Runner 协议"),
    ("05-sandbox.md", "沙箱"),
    ("06-judge-attribution.md", "判定与归因"),
    ("07-platform-architecture.md", "平台架构"),
    ("08-adr.md", "架构决策"),
    ("09-target-architecture.md", "架构图"),
    ("10-tasks-plan.md", "任务与计划"),
    ("11-acceptance-testing-risk.md", "验收·测试·风险"),
    ("12-engineering-workflow.md", "工程流程"),
]


def esc(s):
    """HTML 转义。引号不转 —— 我们只往文本节点里塞内容，不往属性里塞。"""
    return html.escape(s, quote=False)


def inline(s):
    """先把行内 code 抽成占位符，再做粗体/斜体/链接，最后还原——
    避免 **粗体中包含 `代码`** 这种写法被切断导致 strong 配对错位。"""
    spans = []

    def stash(m):
        spans.append(m.group(1))
        return f"\x00C{len(spans) - 1}\x00"

    t = re.sub(r"`([^`]+)`", stash, s)
    t = esc(t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return re.sub(
        r"\x00C(\d+)\x00", lambda m: "<code>" + esc(spans[int(m.group(1))]) + "</code>", t
    )


def slug(text):
    m = re.match(r"^([\d.]+)", text.strip())
    if m:
        return "sec-" + m.group(1).rstrip(".").replace(".", "-")
    return "sec-" + re.sub(r"[^\w]+", "-", text.strip().lower())[:32]


def split_heading(text):
    m = re.match(r"^([\d.]+)\s+(.*)$", text.strip())
    return (m.group(1), m.group(2)) if m else ("", text.strip())


def render_table(rows):
    align_re = re.compile(r"^[:\-\s|]+$")
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    aligns, body = [], []
    head = cells[0]
    if len(cells) > 1 and align_re.match(rows[1].strip()):
        for a in cells[1]:
            if a.startswith(":") and a.endswith(":"):
                aligns.append("center")
            elif a.endswith(":"):
                aligns.append("right")
            else:
                aligns.append("left")
        body = cells[2:]
    else:
        body = cells[1:]

    def sty(i):
        return f' style="text-align:{aligns[i]}"' if i < len(aligns) and aligns[i] != "left" else ""

    h = "".join(f"<th{sty(i)}>{inline(c)}</th>" for i, c in enumerate(head))
    b = "".join(
        "<tr>" + "".join(f"<td{sty(i)}>{inline(c)}</td>" for i, c in enumerate(r)) + "</tr>"
        for r in body
    )
    return f'<div class="tw"><table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>'


def convert(md, toc, group):
    lines = md.split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]

        m = re.match(r"^```\s*([\w-]*)\s*$", ln)
        if m:
            lang, i, buf = m.group(1), i + 1, []
            while i < n and not re.match(r"^```\s*$", lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1
            code = esc("\n".join(buf))
            if lang == "mermaid":
                out.append(f'<figure class="fig"><pre class="mermaid">{code}</pre></figure>')
            else:
                cls = f' data-lang="{lang}"' if lang else ""
                out.append(
                    f'<div class="cw"><pre class="code"{cls}><code>{code}</code></pre></div>'
                )
            continue

        if ln.startswith("|"):
            buf = []
            while i < n and lines[i].startswith("|"):
                buf.append(lines[i])
                i += 1
            out.append(render_table(buf))
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            lvl, text = len(m.group(1)), m.group(2).strip()
            if lvl == 1:
                num, title = split_heading(text)
                sid = slug(text)
                toc.append((group, num, title, sid))
                numhtml = f'<span class="num">{esc(num)}</span>' if num else ""
                out.append(
                    f'<h2 class="sec" id="{sid}">{numhtml}<span class="t">{inline(title)}</span></h2>'
                )
            else:
                num, title = split_heading(text)
                tag = {2: "h3", 3: "h4", 4: "h5"}[lvl]
                lead = f'<span class="sub-num">{esc(num)}</span> ' if num else ""
                out.append(f'<{tag} id="{slug(text)}">{lead}{inline(title)}</{tag}>')
            i += 1
            continue

        if re.match(r"^(---|\*\*\*)\s*$", ln):
            out.append("<hr>")
            i += 1
            continue

        if ln.startswith(">"):
            buf = []
            while i < n and lines[i].startswith(">"):
                buf.append(lines[i][1:].lstrip())
                i += 1
            paras = [p for p in "\n".join(buf).split("\n\n") if p.strip()]
            inner = "".join(f"<p>{inline(p.strip())}</p>" for p in paras)
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", ln)
        if m:
            ordered = bool(re.match(r"^\d+\.$", m.group(2)))
            items, cur = [], None
            while i < n:
                mm = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[i])
                if mm and len(mm.group(1)) < 2:
                    if cur is not None:
                        items.append(cur)
                    cur = [mm.group(3)]
                    i += 1
                    continue
                if lines[i].strip() and lines[i].startswith(("  ", "\t")) and cur is not None:
                    cur.append(lines[i].strip())
                    i += 1
                    continue
                break
            if cur is not None:
                items.append(cur)
            tag = "ol" if ordered else "ul"
            li = ""
            for parts in items:
                li += "<li>" + inline(parts[0])
                for extra in parts[1:]:
                    mm = re.match(r"^([-*]|\d+\.)\s+(.*)$", extra)
                    li += f'<div class="cont">{inline(mm.group(2) if mm else extra)}</div>'
                li += "</li>"
            out.append(f"<{tag}>{li}</{tag}>")
            continue

        if not ln.strip():
            i += 1
            continue

        buf = []
        while (
            i < n
            and lines[i].strip()
            and not re.match(r"^(#{1,4}\s|```|\||>|\s*([-*]|\d+\.)\s|---\s*$)", lines[i])
        ):
            buf.append(lines[i].strip())
            i += 1
        out.append(f"<p>{inline(' '.join(buf))}</p>")
    return "\n".join(out)


toc, body = [], []
for fname, group in FILES:
    md = (PLAN / fname).read_text(encoding="utf-8")
    if fname == "README.md":
        md = md[md.index("# 1 Executive Summary") :]
    body.append(
        f'<section class="part" data-group="{esc(group)}">' + convert(md, toc, group) + "</section>"
    )

toc_html, seen = [], None
for group, num, title, sid in toc:
    if group != seen:
        toc_html.append(f'<div class="toc-group">{esc(group)}</div>')
        seen = group
    toc_html.append(
        f'<a class="toc-link" href="#{sid}"><span class="toc-num">{esc(num)}</span>'
        f'<span class="toc-t">{esc(title)}</span></a>'
    )
toc_html = "\n".join(toc_html)

STATS = [
    ("4 周可行性", "平台可交付", "6 项量化指标中 4 项需重定义口径或设降级路径"),
    ("首个里程碑", "W1D5 · M1", "Golden Task × Mock Agent 全链路跑通，唯一不可延期"),
    ("硬件结论", "无需采购", "本机调 .wslconfig 至 16 vCPU / 11 GB 后即可完成 300 次实验"),
    ("最需改口径", "复现 ≤5pp", "改为 Harness Replay：确定性、零 Agent 成本、可证明"),
]
stat_html = "".join(
    f'<div class="stat"><div class="stat-k">{esc(k)}</div><div class="stat-v">{esc(v)}</div>'
    f'<div class="stat-n">{esc(nn)}</div></div>'
    for k, v, nn in STATS
)

HEAD = """<title>SWE-Bench 式评测平台立项规划</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Noto+Sans+SC:wght@400;500;700&family=Noto+Serif+SC:wght@600;700&display=swap">
<style>
:root{
  --ground:#EDEFF3; --surface:#FFFFFF; --surface-2:#F4F6FA; --surface-3:#E7EBF2;
  --ink:#14181F; --ink-2:#3E4753; --muted:#6C7787; --rule:#D2D8E2; --rule-2:#E2E7EE;
  --accent:#26408B; --accent-2:#3D5AB5; --accent-soft:#E2E8F8;
  --warn:#8E6110; --warn-soft:#F6EEDD; --pass:#1B6E4A; --fail:#A2382C;
  --shadow:0 1px 2px rgba(20,30,60,.06),0 8px 24px -16px rgba(20,30,60,.28);
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:"Noto Sans SC","PingFang SC","Microsoft YaHei","Hiragino Sans GB",system-ui,sans-serif;
  --serif:"Noto Serif SC","Songti SC","SimSun",Georgia,serif;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#11141A; --surface:#181C24; --surface-2:#1E232C; --surface-3:#252B36;
  --ink:#E8ECF2; --ink-2:#B7C0CD; --muted:#8895A6; --rule:#2B323E; --rule-2:#232935;
  --accent:#93ABF2; --accent-2:#A9BCF7; --accent-soft:#1C2540;
  --warn:#D8AE66; --warn-soft:#2A2318; --pass:#5CBD8C; --fail:#E28577;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 28px -18px rgba(0,0,0,.8);
}}
:root[data-theme="dark"]{
  --ground:#11141A; --surface:#181C24; --surface-2:#1E232C; --surface-3:#252B36;
  --ink:#E8ECF2; --ink-2:#B7C0CD; --muted:#8895A6; --rule:#2B323E; --rule-2:#232935;
  --accent:#93ABF2; --accent-2:#A9BCF7; --accent-soft:#1C2540;
  --warn:#D8AE66; --warn-soft:#2A2318; --pass:#5CBD8C; --fail:#E28577;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 28px -18px rgba(0,0,0,.8);
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
  font-family:var(--sans);font-size:15px;line-height:1.75;
  -webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none;border-bottom:1px solid color-mix(in srgb,var(--accent) 35%,transparent)}
a:hover{border-bottom-color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:2px}

.masthead{background:var(--surface);border-bottom:1px solid var(--rule)}
.mast-in{max-width:1220px;margin:0 auto;padding:56px 32px 34px}
.eyebrow{font-family:var(--mono);font-size:11.5px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--accent);display:flex;gap:12px;align-items:center}
.eyebrow::after{content:"";height:1px;flex:1;background:var(--rule)}
h1.title{font-family:var(--serif);font-weight:700;font-size:clamp(30px,4.4vw,46px);
  line-height:1.22;letter-spacing:-.01em;margin:18px 0 0;text-wrap:balance;max-width:20ch}
.sub{color:var(--ink-2);margin:14px 0 0;max-width:62ch;font-size:15.5px}
.meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:20px}
.chip{font-family:var(--mono);font-size:11.5px;padding:3px 9px;border-radius:3px;
  background:var(--surface-2);border:1px solid var(--rule);color:var(--ink-2)}
.chip.on{background:var(--accent-soft);border-color:color-mix(in srgb,var(--accent) 30%,transparent);color:var(--accent)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule);border-radius:5px;overflow:hidden;margin-top:30px}
.stat{background:var(--surface);padding:16px 18px}
.stat-k{font-family:var(--mono);font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted)}
.stat-v{font-family:var(--serif);font-weight:700;font-size:19px;margin-top:5px;line-height:1.3}
.stat-n{font-size:12.5px;color:var(--ink-2);margin-top:6px;line-height:1.55}

.shell{max-width:1220px;margin:0 auto;padding:0 32px 96px;
  display:grid;grid-template-columns:242px minmax(0,1fr);gap:52px;align-items:start}
nav.toc{position:sticky;top:20px;max-height:calc(100vh - 40px);overflow-y:auto;
  padding:26px 0 26px;font-size:13px;scrollbar-width:thin}
.toc-group{font-family:var(--mono);font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;
  color:var(--muted);margin:20px 0 7px;padding-bottom:5px;border-bottom:1px solid var(--rule-2)}
.toc-group:first-child{margin-top:0}
.toc-link{display:grid;grid-template-columns:26px 1fr;gap:7px;padding:3.5px 6px 3.5px 0;
  color:var(--ink-2);border:0;border-radius:3px;line-height:1.45}
.toc-link:hover{color:var(--accent);background:var(--surface-2)}
.toc-link.active{color:var(--accent);font-weight:500}
.toc-link.active .toc-num{color:var(--accent)}
.toc-num{font-family:var(--mono);font-size:11px;color:var(--muted);text-align:right}
main{padding:44px 0 0;min-width:0;max-width:880px}
.toc-toggle{display:none}

h2.sec{font-family:var(--serif);font-weight:700;font-size:clamp(22px,2.6vw,29px);line-height:1.3;
  margin:0 0 22px;padding-top:14px;display:grid;grid-template-columns:auto 1fr;gap:14px;
  align-items:baseline;scroll-margin-top:18px;text-wrap:balance}
h2.sec .num{font-family:var(--mono);font-size:14px;font-weight:600;color:var(--accent);
  letter-spacing:.02em;padding-top:.35em}
h3{font-family:var(--serif);font-weight:600;font-size:19px;line-height:1.4;
  margin:38px 0 12px;scroll-margin-top:18px;text-wrap:balance}
h4{font-size:15.5px;font-weight:700;margin:26px 0 9px;color:var(--ink);scroll-margin-top:18px}
h5{font-size:14px;font-weight:700;margin:20px 0 7px;color:var(--ink-2)}
.sub-num{font-family:var(--mono);font-weight:600;font-size:.86em;color:var(--accent);margin-right:.15em}
section.part{border-top:1px solid var(--rule);padding:40px 0 4px}
section.part:first-child{border-top:0;padding-top:8px}
p{margin:0 0 14px;max-width:70ch}
hr{border:0;height:1px;background:var(--rule-2);margin:34px 0}
strong{font-weight:700;color:var(--ink)}
em{font-style:italic;color:var(--ink-2)}

ul,ol{margin:0 0 16px;padding-left:1.25em;max-width:72ch}
li{margin:0 0 7px}
li::marker{color:var(--muted)}
.cont{color:var(--ink-2);font-size:14.2px;margin-top:3px}

blockquote{margin:20px 0;padding:14px 18px;background:var(--warn-soft);
  border-left:3px solid var(--warn);border-radius:0 4px 4px 0}
blockquote p{margin:0 0 8px;color:var(--ink-2);max-width:66ch}
blockquote p:last-child{margin:0}
blockquote strong{color:var(--ink)}

code{font-family:var(--mono);font-size:.855em;background:var(--surface-3);
  padding:1.5px 5px;border-radius:3px;color:var(--ink);white-space:nowrap}
.cw{overflow-x:auto;margin:0 0 18px;border-radius:5px;border:1px solid var(--rule);background:var(--surface-2)}
pre.code{margin:0;padding:16px 18px;font-family:var(--mono);font-size:12.5px;line-height:1.62;
  color:var(--ink-2);white-space:pre;position:relative}
pre.code code{background:none;padding:0;font-size:inherit;white-space:pre;color:inherit}
pre.code[data-lang]::before{content:attr(data-lang);position:absolute;top:0;right:0;
  font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
  padding:4px 9px;background:var(--surface-3);border-radius:0 4px 0 4px}

.fig{margin:22px 0;padding:18px;background:var(--surface);border:1px solid var(--rule);
  border-radius:5px;overflow-x:auto;box-shadow:var(--shadow)}
.fig pre.mermaid{margin:0;display:flex;justify-content:center;min-width:min-content}

.tw{overflow-x:auto;margin:0 0 20px;border:1px solid var(--rule);border-radius:5px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:13.2px;line-height:1.6}
th{background:var(--surface-2);font-weight:700;text-align:left;color:var(--ink);
  padding:10px 13px;border-bottom:1px solid var(--rule);white-space:nowrap;
  font-size:12.5px;letter-spacing:.01em}
td{padding:9px 13px;border-bottom:1px solid var(--rule-2);vertical-align:top;color:var(--ink-2);
  font-variant-numeric:tabular-nums}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:var(--surface-2)}
td strong{color:var(--ink)}
td code,th code{white-space:normal}

.totop{position:fixed;right:22px;bottom:22px;width:40px;height:40px;border-radius:50%;
  background:var(--surface);border:1px solid var(--rule);color:var(--ink-2);cursor:pointer;
  display:none;align-items:center;justify-content:center;box-shadow:var(--shadow);
  font-family:var(--mono);font-size:15px;line-height:1}
.totop.show{display:flex}
.totop:hover{color:var(--accent);border-color:var(--accent)}

footer{max-width:1220px;margin:0 auto;padding:26px 32px 46px;border-top:1px solid var(--rule);
  color:var(--muted);font-size:12.5px;display:flex;flex-wrap:wrap;gap:8px 20px;justify-content:space-between}

@media (max-width:960px){
  .shell{grid-template-columns:minmax(0,1fr);gap:0;padding:0 20px 72px}
  .mast-in{padding:36px 20px 26px}
  nav.toc{position:static;max-height:none;padding:18px 0 0;
    border-bottom:1px solid var(--rule);margin-bottom:6px}
  .toc-toggle{display:block;width:100%;text-align:left;background:var(--surface);
    border:1px solid var(--rule);border-radius:5px;padding:11px 14px;color:var(--ink);
    font-family:var(--sans);font-size:14px;font-weight:500;cursor:pointer;margin-top:18px}
  .toc-body{display:none;padding:14px 0 18px}
  .toc-body.open{display:block}
  main{padding-top:26px}
  .stats{grid-template-columns:1fr}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
html{scroll-behavior:smooth}
</style>"""

BODY = f"""<header class="masthead">
  <div class="mast-in">
    <div class="eyebrow">立项规划报告 · v1.0 · 规划轮（不含实现）</div>
    <h1 class="title">AI Coding Agent 评测基准平台</h1>
    <p class="sub">SWE-Bench 式评测平台的需求分解、可行性审计、协议冻结与四周实施计划。以学校项目 33 号需求文档为 Requirement Baseline，对六项量化指标逐条做了现实性审计并给出可证明的替代口径与降级路径。</p>
    <div class="meta">
      <span class="chip on">Python + TypeScript</span>
      <span class="chip">FastAPI 模块化单体</span>
      <span class="chip">PostgreSQL 队列</span>
      <span class="chip">双容器 Docker 沙箱</span>
      <span class="chip">Next.js 16 · React 19</span>
      <span class="chip">31 章 · 12 项 ADR · 17 项风险</span>
    </div>
    <div class="stats">{stat_html}</div>
  </div>
</header>

<div class="shell">
  <nav class="toc">
    <button class="toc-toggle" type="button" aria-expanded="false">目录 · 31 章</button>
    <div class="toc-body">{toc_html}</div>
  </nav>
  <main>{"".join(body)}</main>
</div>

<footer>
  <span>AI Coding Agent 评测基准平台 · 立项规划报告 v1.0</span>
  <span>源文档：docs/plan/*.md · 本轮仅规划，未创建任何实现文件</span>
</footer>

<button class="totop" type="button" aria-label="回到顶部">↑</button>

<script>
(function(){{
  var btn=document.querySelector('.toc-toggle'),bodyEl=document.querySelector('.toc-body');
  if(btn){{btn.addEventListener('click',function(){{
    var open=bodyEl.classList.toggle('open');
    btn.setAttribute('aria-expanded',open?'true':'false');
  }});}}
  bodyEl.addEventListener('click',function(e){{
    if(e.target.closest('.toc-link')&&window.matchMedia('(max-width:960px)').matches){{
      bodyEl.classList.remove('open');btn.setAttribute('aria-expanded','false');
    }}
  }});

  var top=document.querySelector('.totop');
  top.addEventListener('click',function(){{window.scrollTo({{top:0,behavior:'smooth'}});}});
  window.addEventListener('scroll',function(){{
    top.classList.toggle('show',window.scrollY>700);
  }},{{passive:true}});

  var links={{}},secs=[].slice.call(document.querySelectorAll('h2.sec'));
  document.querySelectorAll('.toc-link').forEach(function(a){{links[a.getAttribute('href').slice(1)]=a;}});
  var cur=null;
  var io=new IntersectionObserver(function(entries){{
    entries.forEach(function(en){{
      if(!en.isIntersecting)return;
      var a=links[en.target.id];if(!a||a===cur)return;
      if(cur)cur.classList.remove('active');
      a.classList.add('active');cur=a;
      var nav=document.querySelector('nav.toc');
      if(window.matchMedia('(min-width:961px)').matches){{
        var r=a.getBoundingClientRect(),nr=nav.getBoundingClientRect();
        if(r.top<nr.top+20||r.bottom>nr.bottom-20)a.scrollIntoView({{block:'center'}});
      }}
    }});
  }},{{rootMargin:'-8% 0px -78% 0px',threshold:0}});
  secs.forEach(function(s){{io.observe(s);}});
}})();
</script>"""

(PLAN / "report.html").write_text(HEAD + "\n" + BODY + "\n", encoding="utf-8")
print("sections:", len(toc), "| bytes:", (PLAN / "report.html").stat().st_size)
