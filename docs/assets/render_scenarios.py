# /// script
# requires-python = ">=3.11"
# ///
"""Write the README scenario cards as light and dark SVGs for each language.

Run: python3 docs/assets/render_scenarios.py
Outputs scenarios/card-{handoff,shared,personal,learning,skills,trace}-{en,zh,ja}-{light,dark}.svg.
The sample project matches the Dashboard screenshots (an inventory schema migration).
"""

from pathlib import Path
from xml.sax.saxutils import escape

ASSETS = Path(__file__).resolve().parent / "scenarios"
WIDTH, HEIGHT = 640, 360
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans','Microsoft YaHei',Arial,sans-serif"

THEMES = {
    "light": {"bg": "#ffffff", "card": "#f7f9fd", "line": "#dde4ef", "box": "#ffffff", "ink": "#0f1b33",
              "ink2": "#4a5875", "muted": "#7a879e", "accent": "#0866ee", "soft": "#e8f1ff", "ok": "#0f8a5f"},
    "dark": {"bg": "#0d1117", "card": "#121a28", "line": "#283449", "box": "#0f1726", "ink": "#eef3fc",
             "ink2": "#b2bed2", "muted": "#8391a8", "accent": "#5b9dff", "soft": "#16284a", "ok": "#3fc58f"},
}

COPY = {
    "zh": {
        "handoff": {
            "tag": "任务交接", "title": "换个会话，接着做",
            "lead": "上次做到哪、下一步做什么，新会话直接接上。",
            "s1": "会话 1 · 周一", "s1a": "✓ 字段映射已验证", "s1b": "✓ 分批回填已准备", "s1c": "→ 下一步：跑第一批",
            "s2": "会话 2 · 周二", "ask": "接着上次的迁移做", "reply": "先跑第一批，再核对记录数。",
        },
        "shared": {
            "tag": "共享 Scope", "title": "多个 Agent，共用一份记忆",
            "lead": "在 Codex 里定下的约定，换到 Claude Code、Kimi Code 也知道。",
            "a": "Codex", "b": "Claude Code", "c": "Kimi Code", "scope": "同一个 Scope",
            "rows": ("决定", "约束", "进度"),
        },
        "personal": {
            "tag": "画像与记忆规则", "title": "记住你的习惯和规矩",
            "lead": "偏好只说一次；记什么、怎么整理，由你定。",
            "say": "“设计评审放到语雀。”", "profile": "你的画像",
            "p1": "文档工具：语雀", "p2": "写作：先结论，后细节",
            "rule": "记忆规则", "rule1": "只保留长期决策和约束",
        },
        "learning": {
            "tag": "主题记忆与 Dream", "title": "越用越懂你的项目",
            "lead": "讨论沉淀成主题，经验经你审核后更新。",
            "steps": ("日常讨论", "主题摘要", "Dream 提出修订", "你来审核"),
            "note": "Dream 根据过往依据提出修订，批准后才生效",
        },
        "skills": {
            "tag": "经验与技能", "title": "做过一次，下次就会",
            "lead": "验证过的做法沉淀为经验和技能，审核后导出给 Agent。",
            "case": "一次成功的排查", "c1": "情境：测试偶发失败", "c2": "做法：先固定随机种子", "c3": "结果：稳定通过",
            "skill": "技能：排查偶发失败", "k1": "✓ 已审核", "k2": "→ 导出到 Codex",
        },
        "trace": {
            "tag": "来源与版本", "title": "每条记忆，都有出处",
            "lead": "回答带着来源，能追到当时的记录和确切版本。",
            "who": "Agent 的回答", "answer": "迁移期间保留旧的 SKU 映射。",
            "src": "来源 [1]", "r1": "会话记录 · 周一", "r2": "第 40–55 行", "r3": "记忆版本 @1",
        },
    },
    "en": {
        "handoff": {
            "tag": "Handoff", "title": "Pick up where you left off",
            "lead": "What was done and what comes next, ready in the new session.",
            "s1": "Session 1 · Monday", "s1a": "✓ Field mapping verified", "s1b": "✓ Batched backfill ready",
            "s1c": "→ Next: run the first batch",
            "s2": "Session 2 · Tuesday", "ask": "Continue the migration", "reply": "Run batch one, check counts.",
        },
        "shared": {
            "tag": "Shared Scope", "title": "One memory for every agent",
            "lead": "A rule agreed in Codex carries over to Claude Code and OpenCode.",
            "a": "Codex", "b": "Claude Code", "c": "OpenCode", "scope": "One Scope",
            "rows": ("Decisions", "Constraints", "Progress"),
        },
        "personal": {
            "tag": "Profile & memory rules", "title": "Your habits, your rules",
            "lead": "Say a preference once. You decide what is kept and how.",
            "say": "“Use Notion for reviews.”", "profile": "Your profile",
            "p1": "Docs: Notion", "p2": "Writing: conclusion first",
            "rule": "Memory rule", "rule1": "Long-term decisions only",
        },
        "learning": {
            "tag": "Topic Memory & Dream", "title": "Knows your project better over time",
            "lead": "Discussions become topics; lessons update after your review.",
            "steps": ("Discuss", "Summarize", "Dream revises", "You approve"),
            "note": "Dream proposes revisions from past evidence; they apply once approved",
        },
        "skills": {
            "tag": "Experience & Skills", "title": "Solved once, reused next time",
            "lead": "Proven approaches become experiences and skills, exported after review.",
            "case": "A fix that worked", "c1": "Situation: flaky test", "c2": "Action: pin the seed",
            "c3": "Outcome: passes reliably",
            "skill": "Skill: fix flaky tests", "k1": "✓ Approved", "k2": "→ Exported to Codex",
        },
        "trace": {
            "tag": "Sources & revisions", "title": "Every memory has a source",
            "lead": "Answers cite where they came from, down to the exact revision.",
            "who": "Agent answer", "answer": "Keep the old SKU mapping.",
            "src": "Source [1]", "r1": "Session log · Monday", "r2": "Lines 40–55", "r3": "Memory revision @1",
        },
    },
    "ja": {
        "handoff": {
            "tag": "タスクの引き継ぎ", "title": "別のセッションで、続きから",
            "lead": "どこまで進み、次に何をするかを新しいセッションへ。",
            "s1": "セッション 1 · 月曜", "s1a": "✓ 項目の対応を検証済み", "s1b": "✓ 分割バックフィル準備済み",
            "s1c": "→ 次：最初のバッチを実行",
            "s2": "セッション 2 · 火曜", "ask": "前回の移行の続きを", "reply": "最初のバッチ後、件数を確認。",
        },
        "shared": {
            "tag": "共有 Scope", "title": "複数の Agent で、ひとつの記憶",
            "lead": "Codex で決めた約束を、Claude Code や OpenCode でも共有。",
            "a": "Codex", "b": "Claude Code", "c": "OpenCode", "scope": "同じ Scope",
            "rows": ("判断", "制約", "進捗"),
        },
        "personal": {
            "tag": "プロファイルと記憶ルール", "title": "あなたの習慣とルールを覚える",
            "lead": "好みは一度伝えるだけ。何をどう残すかはあなたが決める。",
            "say": "「レビューは Notion で。」", "profile": "あなたのプロファイル",
            "p1": "ドキュメント：Notion", "p2": "文章：結論が先",
            "rule": "記憶ルール", "rule1": "長期の判断と制約だけ残す",
        },
        "learning": {
            "tag": "Topic Memory と Dream", "title": "使うほど、プロジェクトに詳しく",
            "lead": "議論はトピックにまとまり、経験はレビュー後に更新。",
            "steps": ("日々の議論", "トピック要約", "Dream が改訂案", "あなたが承認"),
            "note": "Dream が過去の根拠から改訂を提案し、承認後に反映",
        },
        "skills": {
            "tag": "経験とスキル", "title": "一度解けば、次から使える",
            "lead": "検証済みのやり方を経験とスキルに。レビュー後に Agent へ。",
            "case": "うまくいった調査", "c1": "状況：テストがときどき失敗", "c2": "対応：乱数シードを固定",
            "c3": "結果：安定して成功",
            "skill": "スキル：不安定テスト対応", "k1": "✓ 承認済み", "k2": "→ Codex へエクスポート",
        },
        "trace": {
            "tag": "出典とリビジョン", "title": "すべての記憶に、出典がある",
            "lead": "回答は出典つき。当時の記録と正確なリビジョンまでたどれる。",
            "who": "Agent の回答", "answer": "移行中は旧 SKU 対応を残す。",
            "src": "出典 [1]", "r1": "セッション記録 · 月曜", "r2": "40–55 行目", "r3": "記憶リビジョン @1",
        },
    },
}


def text(x, y, s, t, size=15, weight=400, color="ink", anchor="start"):
    return (f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{t[color]}" '
            f'text-anchor="{anchor}">{escape(s)}</text>')


def rect(x, y, w, h, t, fill="box", stroke="line", r=10, dash=False):
    extra = ' stroke-dasharray="5 4"' if dash else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{t[fill]}" stroke="{t[stroke]}"{extra}/>'


def arrow(x1, y1, x2, y2, t, color="muted"):
    return (f'<path d="M{x1} {y1}L{x2} {y2}" stroke="{t[color]}" stroke-width="2" fill="none" '
            f'marker-end="url(#head-{color})"/>')


def handoff(c, t):
    return "".join([
        rect(32, 150, 262, 176, t),
        text(50, 180, c["s1"], t, 14, 700, "muted"),
        text(50, 218, c["s1a"], t, 15, 400, "ink2"),
        text(50, 250, c["s1b"], t, 15, 400, "ink2"),
        text(50, 290, c["s1c"], t, 15, 700, "accent"),
        arrow(300, 238, 336, 238, t, "accent"),
        rect(346, 150, 262, 176, t, stroke="accent"),
        text(364, 180, c["s2"], t, 14, 700, "muted"),
        rect(364, 198, 226, 40, t, fill="soft", stroke="soft", r=8),
        text(378, 224, c["ask"], t, 15, 600, "ink"),
        text(364, 272, c["reply"], t, 15, 400, "ink2"),
    ])


def shared(c, t):
    parts = []
    for i, name in enumerate((c["a"], c["b"], c["c"])):
        y = 152 + i * 60
        parts += [rect(32, y, 170, 44, t), text(117, y + 28, name, t, 15, 600, "ink", "middle"),
                  arrow(206, y + 22, 300, 238, t)]
    parts += [rect(310, 152, 298, 164, t, fill="soft", stroke="accent"),
              text(330, 186, c["scope"], t, 17, 700, "accent")]
    for i, row in enumerate(c["rows"]):
        parts += [rect(330, 204 + i * 36, 258, 28, t, r=6), text(344, 223 + i * 36, row, t, 14, 400, "ink2")]
    return "".join(parts)


def personal(c, t):
    return "".join([
        rect(32, 156, 250, 54, t, fill="soft", stroke="soft", r=12),
        text(48, 189, c["say"], t, 15, 600, "ink"),
        rect(32, 252, 250, 74, t, dash=True),
        text(48, 278, c["rule"], t, 13, 700, "muted"),
        text(48, 306, c["rule1"], t, 15, 600, "ink"),
        arrow(288, 183, 326, 214, t),
        arrow(288, 289, 326, 268, t),
        rect(334, 156, 274, 170, t, stroke="accent"),
        text(352, 188, c["profile"], t, 16, 700, "accent"),
        rect(352, 206, 238, 40, t, fill="card", stroke="card", r=8),
        text(366, 232, c["p1"], t, 15, 400, "ink2"),
        rect(352, 258, 238, 40, t, fill="card", stroke="card", r=8),
        text(366, 284, c["p2"], t, 15, 400, "ink2"),
    ])


def learning(c, t):
    parts = []
    w, gap, x0 = 128, 22, 32
    for i, step in enumerate(c["steps"]):
        x = x0 + i * (w + gap)
        last = i == len(c["steps"]) - 1
        parts.append(rect(x, 176, w, 64, t, fill="soft" if last else "box", stroke="accent" if last else "line"))
        parts.append(text(x + w / 2, 213, ("✓ " if last else "") + step, t, 14, 700 if last else 600,
                          "accent" if last else "ink", "middle"))
        if not last:
            parts.append(arrow(x + w + 2, 208, x + w + gap - 2, 208, t))
    parts.append(text(320, 290, c["note"], t, 13.5, 400, "muted", "middle"))
    return "".join(parts)


def skills(c, t):
    return "".join([
        rect(32, 150, 262, 176, t),
        text(50, 180, c["case"], t, 14, 700, "muted"),
        text(50, 218, c["c1"], t, 15, 400, "ink2"),
        text(50, 250, c["c2"], t, 15, 400, "ink2"),
        text(50, 282, c["c3"], t, 15, 400, "ink2"),
        arrow(300, 238, 336, 238, t, "accent"),
        rect(346, 150, 262, 176, t, stroke="accent"),
        text(364, 184, c["skill"], t, 16, 700, "accent"),
        rect(364, 204, 226, 40, t, fill="soft", stroke="soft", r=8),
        text(378, 230, c["k1"], t, 15, 600, "ok"),
        rect(364, 256, 226, 40, t, fill="card", stroke="card", r=8),
        text(378, 282, c["k2"], t, 15, 600, "ink"),
    ])


def trace(c, t):
    return "".join([
        rect(32, 170, 272, 96, t, fill="soft", stroke="soft", r=12),
        text(48, 198, c["who"], t, 13, 700, "muted"),
        text(48, 232, c["answer"], t, 15, 600, "ink"),
        f'<rect x="262" y="182" width="30" height="22" rx="11" fill="{t["accent"]}"/>',
        text(277, 198, "1", t, 13, 700, "bg", "middle"),
        arrow(310, 218, 328, 218, t, "accent"),
        rect(334, 150, 274, 176, t, stroke="accent"),
        text(352, 182, c["src"], t, 16, 700, "accent"),
        rect(352, 198, 238, 34, t, fill="card", stroke="card", r=8),
        text(366, 220, c["r1"], t, 14.5, 400, "ink2"),
        rect(352, 238, 238, 34, t, fill="card", stroke="card", r=8),
        text(366, 260, c["r2"], t, 14.5, 400, "ink2"),
        rect(352, 278, 238, 34, t, fill="card", stroke="card", r=8),
        text(366, 300, c["r3"], t, 14.5, 400, "ink2"),
    ])


SCENES = {"handoff": handoff, "shared": shared, "personal": personal, "learning": learning,
          "skills": skills, "trace": trace}


def card(scene, lang, theme):
    c, t = COPY[lang][scene], THEMES[theme]
    markers = "".join(
        f'<marker id="head-{name}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        f'orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="{t[name]}"/></marker>'
        for name in ("muted", "accent"))
    tag_width = 26 + sum(13.5 if ord(ch) > 0x2E80 else 9.2 for ch in c["tag"])
    return "".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'role="img" aria-labelledby="title"><title id="title">{escape(c["title"])}</title><defs>{markers}</defs>',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{t["bg"]}"/>',
        f'<rect x="1" y="1" width="{WIDTH - 2}" height="{HEIGHT - 2}" rx="18" fill="{t["card"]}" stroke="{t["line"]}"/>',
        f'<g font-family="{FONT}">',
        f'<rect x="32" y="28" width="{tag_width:.0f}" height="26" rx="13" fill="{t["soft"]}"/>',
        text(45, 46, c["tag"], t, 13.5, 700, "accent"),
        text(32, 94, c["title"], t, 26, 800),
        text(32, 126, c["lead"], t, 15.5, 400, "ink2"),
        SCENES[scene](c, t),
        "</g></svg>",
    ])


def main():
    ASSETS.mkdir(exist_ok=True)
    for scene in SCENES:
        for lang in COPY:
            for theme in THEMES:
                (ASSETS / f"card-{scene}-{lang}-{theme}.svg").write_text(card(scene, lang, theme), encoding="utf-8")
    print(f"wrote {len(SCENES) * len(COPY) * len(THEMES)} cards to {ASSETS}")


if __name__ == "__main__":
    main()
