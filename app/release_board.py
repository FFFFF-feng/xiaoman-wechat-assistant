"""Render checked-in, escaped release information without user data or requests."""
from html import escape
from . import release_catalog


def render_board():
    releases = release_catalog.catalog()
    output = []
    for index, item in enumerate(releases):
        anchor = 'release-' + item.version.replace('.', '-')
        meta = f'<div class="release-meta"><span class="release-version">v{escape(item.version)}</span><time datetime="{item.date}">{item.date}</time></div>'
        changes = ''.join(f'<li><span class="change-mark" aria-hidden="true">✦</span><div><h3>{escape(change.title)}</h3><p>{escape(change.text)}</p></div></li>' for change in item.changes)
        examples = ''.join(f'<button type="button" data-copy="{escape(example, quote=True)}">{escape(example)}</button>' for example in item.examples)
        content = f'<ul class="release-changes">{changes}</ul><section id="{anchor}-try" class="release-try"><h3>这就聊两句</h3><p>点一句复制，回微信发给我。</p><div class="release-examples" aria-label="复制体验示例">{examples}</div><p class="hint">这里只复制，回微信发送后才会执行。</p></section><details class="release-note"><summary>还有个小提醒</summary><p>{escape(item.note)}</p></details>'
        if index == 0:
            demo = ('<div class="demo-bubble demo-user">催啥催</div><div class="demo-bubble demo-bot">水杯等你半天了，<br />都快申请换主人了。</div><span class="demo-caption">喝水提醒后的对话示意</span>'
                    if item.version == '0.2.5' else f'<div class="demo-bubble demo-user">{escape(item.examples[0])}</div><span class="demo-caption">回微信，试试这句</span>')
            hero = f'<div class="release-hero"><div class="release-heading">{meta}<h2>{escape(item.title)}</h2><p class="release-summary">{escape(item.summary)}</p><a class="button" href="#{anchor}-try">试试新本事</a></div><aside class="release-demo" aria-label="对话示意"><img src="/static/xiaoman-avatar.jpg?v=phoenix-a-20261016" alt="" width="44" height="44" />{demo}</aside></div>'
            output.append(f'<article id="{anchor}" class="release latest-release"><span class="release-label">最新更新</span>{hero}<div class="release-body">{content}</div></article><section class="release-history" aria-label="历史更新"><div class="history-heading"><h2>以前也没闲着</h2><p>想翻旧账？都记着呢。</p></div>')
        else:
            output.append(f'<details id="{anchor}" class="release past-release"><summary><span><b>v{escape(item.version)}</b>{escape(item.title)}</span><time datetime="{item.date}">{item.date}</time></summary><p class="release-summary">{escape(item.summary)}</p>{content}</details>')
    output.append('</section>')
    return ''.join(output)


def render_preview():
    item = release_catalog.catalog()[0]
    return (f'<section id="updates" class="section"><h2>小满更新板</h2>'
            f'<p class="sub">v{escape(item.version)} · {item.date}</p><h3>{escape(item.title)}</h3>'
            f'<p>{escape(item.summary)}</p><p>新版本会单独推送一条更新通知。使用指南只在新朋友首次连接后自动发送，平时需要时再来查看。</p>'
            f'<a class="button secondary" href="/guide?view=updates#updates">查看最新与历史更新</a></section>')
