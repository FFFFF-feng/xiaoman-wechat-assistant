"""Render checked-in, escaped release information without user data or requests."""
from html import escape
from . import release_catalog


def render_board():
    releases = release_catalog.catalog()
    output = []
    for index, item in enumerate(releases):
        anchor = 'release-' + item.version.replace('.', '-')
        meta = f'<div class="release-meta"><span class="release-version">v{escape(item.version)}</span><time datetime="{item.date}">{item.date}</time></div>'
        changes = ''.join(f'<li><h3>{escape(change.title)}</h3><p>{escape(change.text)}</p></li>' for change in item.changes)
        examples = ''.join(f'<button type="button" data-copy="{escape(example, quote=True)}">{escape(example)}</button>' for example in item.examples)
        content = f'<p class="release-summary">{escape(item.summary)}</p><ul class="release-changes">{changes}</ul><div class="release-examples" aria-label="复制体验示例">{examples}</div><p class="hint">按钮只复制文字，回到微信粘贴发送后才会执行。</p><p class="release-note">{escape(item.note)}</p>'
        if index == 0:
            output.append(f'<article id="{anchor}" class="release latest-release">{meta}<p class="release-label">最新更新</p><h2>{escape(item.title)}</h2>{content}</article>')
        else:
            output.append(f'<details id="{anchor}" class="release past-release"><summary><span>v{escape(item.version)} · {escape(item.title)}</span><time datetime="{item.date}">{item.date}</time></summary>{content}</details>')
    return ''.join(output)


def render_preview():
    item = release_catalog.catalog()[0]
    return (f'<section id="updates" class="section"><h2>小满更新板</h2>'
            f'<p class="sub">v{escape(item.version)} · {item.date}</p><h3>{escape(item.title)}</h3>'
            f'<p>{escape(item.summary)}</p><p>新版本会单独推送一条更新通知。使用指南只在新朋友首次连接后自动发送，平时需要时再来查看。</p>'
            f'<a class="button secondary" href="/guide?view=updates#updates">查看最新与历史更新</a></section>')
