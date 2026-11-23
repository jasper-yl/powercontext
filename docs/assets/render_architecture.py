# /// script
# requires-python = ">=3.11"
# ///
"""Write the README architecture diagram as light and dark SVGs for each language.

Run: python3 docs/assets/render_architecture.py
Outputs readme-architecture-{en,zh}-{light,dark}.svg. The layout follows
docs/{en,zh}/docs/workflows/architecture.md; keep the two in step.
"""

from pathlib import Path
from xml.sax.saxutils import escape

ASSETS = Path(__file__).resolve().parent
WIDTH, HEIGHT = 1280, 568
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans','Microsoft YaHei',Arial,sans-serif"

THEMES = {
    "light": {"bg": "#ffffff", "panel": "#f6f8fc", "box": "#ffffff", "line": "#d5dce8", "ink": "#0f1b33",
              "ink2": "#4a5875", "accent": "#0866ee", "soft": "#eaf2ff", "arrow": "#7a879e"},
    "dark": {"bg": "#0d1117", "panel": "#131c2b", "box": "#0f1726", "line": "#2b3850", "ink": "#eef3fc",
             "ink2": "#a9b6cc", "accent": "#5b9dff", "soft": "#16284a", "arrow": "#7d8aa3"},
}

COPY = {
    "en": {
        "title": "PowerContext architecture",
        "hosts": "Agents & apps",
        "agents": ("Coding agents", "Codex · Claude Code …"),
        "apps": ("Your application", "Python · HTTP · MCP"),
        "connect": "Integrations",
        "hooks": ("Hooks · MCP · SDK", "Capture input", "Recall context"),
        "gateway": ("Gateway (optional)", "Inject context, guide work"),
        "model": ("Agent's own model", "Called via the gateway"),
        "server": "PowerContext Server",
        "scope": "Scopes · access control · revisions",
        "source": ("Sources", "Captured evidence"),
        "process": ("Background processing", "Extraction · Dream · review"),
        "artifacts": ("Artifacts", "Memory · Topic Memory · Profile", "Experience & Skill · Handoff"),
        "prepared": ("Recall & assembly", "PreparedContext for the next turn"),
        "infra": "Infrastructure",
        "storage": ("Storage", "SQLite", "OceanBase · seekDB"),
        "models": ("Model services", "Generation · Embedding"),
        "capture": "capture",
        "context": "context",
    },
    "zh": {
        "title": "PowerContext 架构",
        "hosts": "Agent 与应用",
        "agents": ("编程 Agent", "Codex · Claude Code …"),
        "apps": ("你的应用", "Python · HTTP · MCP"),
        "connect": "接入方式",
        "hooks": ("Hooks · MCP · SDK", "记录输入", "召回上下文"),
        "gateway": ("网关（可选）", "注入上下文，提供工作指导"),
        "model": ("Agent 自己的模型", "经网关调用"),
        "server": "PowerContext Server",
        "scope": "Scope 隔离 · 访问控制 · 版本记录",
        "source": ("Source", "记录下来的来源证据"),
        "process": ("后台处理", "记忆提取 · Dream · 审核"),
        "artifacts": ("Artifact", "记忆 · 主题记忆 · 画像", "经验与技能 · 交接"),
        "prepared": ("召回与组装", "为下一轮准备 PreparedContext"),
        "infra": "基础设施",
        "storage": ("存储", "SQLite", "OceanBase · seekDB"),
        "models": ("模型服务", "生成 · 向量"),
        "capture": "记录",
        "context": "上下文",
    },
}


def box(x, y, w, h, label, *notes, t, accent=False):
    fill, stroke = (t["soft"], t["accent"]) if accent else (t["box"], t["line"])
    title_color = t["accent"] if accent else t["ink"]
    lines = "".join(f'<text x="{x + 18}" y="{y + 55 + 21 * i}" font-size="14.5" fill="{t["ink2"]}">{escape(note)}</text>'
                    for i, note in enumerate(notes))
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
            f'<text x="{x + 18}" y="{y + 30}" font-size="18" font-weight="700" fill="{title_color}">{escape(label)}</text>'
            + lines)


def panel(x, y, w, h, title, t):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="{t["panel"]}" stroke="{t["line"]}"/>'
            f'<text x="{x + 20}" y="{y + 34}" font-size="15" font-weight="700" letter-spacing=".04em" '
            f'fill="{t["ink2"]}">{escape(title)}</text>')


def arrow(x1, y1, x2, y2, t, label=None, both=False):
    start = ' marker-start="url(#head)"' if both else ""
    out = (f'<path d="M{x1} {y1}L{x2} {y2}" stroke="{t["arrow"]}" stroke-width="2" fill="none"'
           f' marker-end="url(#head)"{start}/>')
    if label:
        out += (f'<text x="{(x1 + x2) / 2}" y="{(y1 + y2) / 2 - 8}" text-anchor="middle" font-size="13" '
                f'fill="{t["ink2"]}">{escape(label)}</text>')
    return out


def diagram(lang, theme):
    c, t = COPY[lang], THEMES[theme]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'role="img" aria-labelledby="title"><title id="title">{escape(c["title"])}</title>',
        f'<defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        f'orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="{t["arrow"]}"/></marker></defs>',
        f'<rect width="{WIDTH}" height="{HEIGHT}" rx="20" fill="{t["bg"]}"/>',
        f'<g font-family="{FONT}">',
        # Hosts
        panel(24, 24, 236, 520, c["hosts"], t),
        box(40, 82, 204, 74, *c["agents"], t=t),
        box(40, 176, 204, 74, *c["apps"], t=t),
        # Integrations
        panel(284, 24, 256, 520, c["connect"], t),
        arrow(244, 119, 294, 119, t),
        arrow(244, 213, 294, 213, t),
        box(300, 82, 224, 168, *c["hooks"], t=t),
        box(300, 286, 224, 74, *c["gateway"], t=t),
        box(300, 452, 224, 74, *c["model"], t=t),
        arrow(412, 360, 412, 446, t),
        # Server
        panel(580, 24, 412, 520, c["server"], t),
        f'<rect x="600" y="62" width="372" height="34" rx="8" fill="{t["soft"]}"/>'
        f'<text x="786" y="84.5" text-anchor="middle" font-size="14.5" font-weight="600" fill="{t["accent"]}">'
        f'{escape(c["scope"])}</text>',
        box(600, 112, 372, 74, *c["source"], t=t),
        box(600, 214, 372, 74, *c["process"], t=t),
        box(600, 316, 372, 90, *c["artifacts"], t=t, accent=True),
        box(600, 434, 372, 74, *c["prepared"], t=t),
        arrow(786, 186, 786, 208, t),
        arrow(786, 288, 786, 310, t),
        arrow(786, 406, 786, 428, t),
        arrow(524, 140, 594, 140, t, c["capture"]),
        arrow(524, 323, 576, 323, t, both=True),
        # PreparedContext returns to the integrations through a bus left of the Server.
        f'<path d="M600 471H556V214H530" stroke="{t["arrow"]}" stroke-width="2" fill="none" marker-end="url(#head)"/>',
        f'<text x="572" y="400" font-size="13" fill="{t["ink2"]}" transform="rotate(-90 572 400)" '
        f'text-anchor="middle">{escape(c["context"])}</text>',
        # Infrastructure
        panel(1016, 24, 240, 520, c["infra"], t),
        box(1032, 112, 208, 95, *c["storage"], t=t),
        box(1032, 226, 208, 74, *c["models"], t=t),
        arrow(972, 150, 1026, 150, t, both=True),
        arrow(972, 251, 1026, 251, t),
        "</g></svg>",
    ]
    return "".join(parts)


def main():
    for lang in COPY:
        for theme in THEMES:
            path = ASSETS / f"readme-architecture-{lang}-{theme}.svg"
            path.write_text(diagram(lang, theme), encoding="utf-8")
            print(path.name)


if __name__ == "__main__":
    main()
