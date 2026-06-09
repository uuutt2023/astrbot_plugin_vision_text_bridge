"""test_theme.py — 主题切换功能测试 (v1.0.0+)

不依赖 AstrBot, 直接读 webui 静态资源验证。
"""
import os
import re
import sys


def _read(name):
    base = os.path.dirname(os.path.abspath(__file__))
    return open(os.path.join(base, "pages", "cache-manager", name), encoding="utf-8").read()


def test_theme_dark_default_in_css():
    """CSS 必须在 :root 默认定义为暗色主题。"""
    css = _read("style.css")
    assert ":root" in css, "CSS 必须有 :root 暗色默认"
    assert "data-theme=\"dark\"" in css, "CSS 必须有 [data-theme=\"dark\"] 显式暗色"
    assert "data-theme=\"light\"" in css, "CSS 必须有 [data-theme=\"light\"] 浅色主题"
    print("test_theme_dark_default_in_css: PASS")


def test_theme_light_defines_all_variables():
    """浅色主题必须重新定义关键 CSS 变量 (bg, text, glass, primary, accent, danger)。"""
    css = _read("style.css")
    # 抽 [data-theme="light"] 块内容
    m = re.search(r'\[data-theme="light"\]\s*\{([^}]+)\}', css, re.DOTALL)
    assert m, "[data-theme=\"light\"] 块必须存在"
    body = m.group(1)
    for var_name in ("--bg-color", "--text-main", "--text-muted",
                     "--primary", "--accent", "--danger",
                     "--glass-bg", "--glass-border"):
        assert var_name in body, f"浅色主题必须定义 {var_name}"
    print("test_theme_light_defines_all_variables: PASS")


def test_theme_prefers_color_scheme_fallback():
    """未设置 data-theme 时, @media (prefers-color-scheme: light) 应回退到浅色。"""
    css = _read("style.css")
    assert "prefers-color-scheme: light" in css, "必须有 @media prefers-color-scheme 兜底"
    print("test_theme_prefers_color_scheme_fallback: PASS")


def test_index_html_has_theme_toggle_button():
    """index.html 顶栏必须有主题切换按钮。"""
    h = _read("index.html")
    assert 'id="theme-toggle"' in h, "index.html 必须有 #theme-toggle 按钮"
    assert "class=\"icon-btn theme-toggle\"" in h or "icon-btn theme-toggle" in h, \
        "按钮必须有 theme-toggle class"
    print("test_index_html_has_theme_toggle_button: PASS")


def test_index_html_theme_init_runs_early():
    """index.html 必须在 body 之前同步执行主题初始化脚本, 避免闪动 (FOUC)。"""
    h = _read("index.html")
    # 找 localStorage.getItem("vtb-theme") 出现处
    m = re.search(r'localStorage\.getItem\(\s*[\"\']vtb-theme[\"\']\s*\)', h)
    assert m, "index.html 必须有 localStorage.getItem('vtb-theme') 同步初始化脚本"
    # 检查位置: 在 <body> 之前
    init_pos = h.find("localStorage.getItem")
    body_pos = h.find("<body")
    assert init_pos < body_pos, f"主题初始化脚本必须在 <body> 之前 (init_pos={init_pos}, body_pos={body_pos})"
    # 还必须有 <script> 包裹 (不是 inline event handler)
    assert "<script>" in h, "必须有 <script> 块"
    print("test_index_html_theme_init_runs_early: PASS")


def test_app_js_has_init_theme_toggle():
    """app.js 必须有 initThemeToggle 函数, 监听 click 切换 data-theme。

    设计上: index.html 同步 init 脚本读 localStorage (防 FOUC);
    app.js 只负责点击时切换 + 写 localStorage。
    """
    js = _read("app.js")
    assert "function initThemeToggle" in js, "app.js 必须有 initThemeToggle 函数"
    assert "initThemeToggle()" in js, "app.js 必须调用 initThemeToggle() 启动"
    # 必须有 setAttribute("data-theme"...) 切换
    assert 'setAttribute("data-theme"' in js, "app.js 必须 setAttribute data-theme 切换主题"
    # 必须存到 localStorage
    assert 'localStorage.setItem("vtb-theme"' in js, "app.js 必须 localStorage 持久化主题"
    # 必须有切换下一个主题的逻辑 (在 light / dark 之间)
    assert '"light"' in js and '"dark"' in js, "app.js 必须支持 light/dark 两种主题切换"
    # 必须 addEventListener 监听点击
    assert "addEventListener" in js and 'click' in js, "app.js 必须监听 click 触发切换"
    print("test_app_js_has_init_theme_toggle: PASS")


def test_theme_toggle_no_emoji():
    """主题按钮本身也不能用 emoji (保持纯文字)。"""
    h = _read("index.html")
    m = re.search(r'<button[^>]*id="theme-toggle"[^>]*>(.*?)</button>', h, re.DOTALL)
    assert m, "主题按钮必须存在"
    text = m.group(1)
    # 用更广的 emoji 范围扫
    EMOJI_RE = re.compile(
        r'[\U0001F300-\U0001F9FF'
        r'\U00002600-\U000027BF'
        r'\U0001F000-\U0001F02F'
        r'\U0001F0A0-\U0001F0FF'
        r'\U0001F100-\U0001F1FF'
        r'\U0001F200-\U0001F2FF'
        r'\U0001FA00-\U0001FA6F'
        r'\U0001FA70-\U0001FAFF'
        r'\uFE0F\uFE0E]'
    )
    assert not EMOJI_RE.search(text), f"主题按钮文字不能用 emoji: {text!r}"
    assert text.strip() in ("浅色", "深色"), f"主题按钮文字只能是'浅色'或'深色': {text!r}"
    print("test_theme_toggle_no_emoji: PASS")


if __name__ == "__main__":
    test_theme_dark_default_in_css()
    test_theme_light_defines_all_variables()
    test_theme_prefers_color_scheme_fallback()
    test_index_html_has_theme_toggle_button()
    test_index_html_theme_init_runs_early()
    test_app_js_has_init_theme_toggle()
    test_theme_toggle_no_emoji()
    print("---")
    print("ALL THEME TESTS PASSED")
