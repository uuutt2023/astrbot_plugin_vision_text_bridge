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


def test_light_theme_input_uses_glass_bg_strong():
    """浅色主题下输入框背景必须用 glass-bg-strong (随主题变, 不写死 rgba 15/23/42)。"""
    css = _read("style.css")
    m = re.search(r'#search-input\s*\{([^}]+)\}', css, re.DOTALL)
    assert m, "#search-input 块必须存在"
    body = m.group(1)
    assert "var(--glass-bg-strong)" in body, \
        "浅色主题下输入框背景必须用 var(--glass-bg-strong) (不能写死 rgba 深色半透)"
    assert "rgba(15, 23, 42" not in body, \
        "输入框背景不能硬编码 rgba(15, 23, 42 ...) (浅色主题下看不见)"
    print("test_light_theme_input_uses_glass_bg_strong: PASS")


def test_light_theme_thead_uses_glass_bg():
    """表头背景必须用 glass-bg 变量 (随主题变)。"""
    css = _read("style.css")
    m = re.search(r'#cache-table\s+thead\s*\{([^}]+)\}', css, re.DOTALL)
    assert m, "#cache-table thead 块必须存在"
    body = m.group(1)
    assert "var(--glass-bg)" in body, \
        "表头背景必须用 var(--glass-bg) (不能写死 rgba 深色半透)"
    assert "rgba(15, 23, 42" not in body, \
        "表头背景不能硬编码 rgba(15, 23, 42 ...)"
    m_th = re.search(r'#cache-table\s+th\s*\{([^}]+)\}', css, re.DOTALL)
    assert m_th
    th_body = m_th.group(1)
    assert "var(--text-sub)" in th_body, \
        "表头文字必须用 var(--text-sub) (浅色主题下看得清)"
    print("test_light_theme_thead_uses_glass_bg: PASS")


def test_primary_button_white_text():
    """刷新按钮 (.btn.primary) 紫蓝渐变背景 + 白字。"""
    css = _read("style.css")
    m = re.search(r'\.btn\.primary\s*\{([^}]+)\}', css, re.DOTALL)
    assert m, ".btn.primary 块必须存在"
    body = m.group(1)
    assert "color: #ffffff" in body or "color: white" in body or "color: #fff" in body, \
        ".btn.primary 文字必须固定白色 (不能跟主题文字色走 — 浅色主题下深蓝背景配深字看不清)"
    assert "color: var(--text-main)" not in body, \
        ".btn.primary 不能用 var(--text-main) (会被主题覆盖成深色)"
    print("test_primary_button_white_text: PASS")


def test_switch_off_bg_has_light_variant():
    """switch 关闭背景必须有浅色主题变体 — 浅色下应该是浅灰色。"""
    css = _read("style.css")
    m_light = re.search(r'\[data-theme="light"\]\s*\{([^}]+)\}', css, re.DOTALL)
    assert m_light, "[data-theme=\"light\"] 块必须存在"
    light_body = m_light.group(1)
    assert "--switch-off-bg" in light_body, \
        "浅色主题必须有 --switch-off-bg 变量 (关闭时的浅灰色背景)"
    m_var = re.search(r'--switch-off-bg:\s*([^;]+);', light_body)
    assert m_var, "--switch-off-bg 在浅色主题必须定义"
    val = m_var.group(1).strip()
    assert "ffffff" not in val.lower().replace(" ", ""), \
        f"浅色主题 --switch-off-bg 不能是纯白 (用户要求浅灰), got: {val}"
    m_slider = re.search(r'\.switch-slider\s*\{([^}]+)\}', css, re.DOTALL)
    assert m_slider
    assert "var(--switch-off-bg" in m_slider.group(1), \
        ".switch-slider 背景必须用 var(--switch-off-bg)"
    print("test_switch_off_bg_has_light_variant: PASS")


def test_order_by_select_uses_glass_bg_strong():
    """排序 select 背景必须同 input 框 (var(--glass-bg-strong)), 不写死 rgba 15/23/42。"""
    css = _read("style.css")
    m = re.search(r'#order-by\s*\{([^}]+)\}', css, re.DOTALL)
    assert m, "#order-by 块必须存在"
    body = m.group(1)
    assert "var(--glass-bg-strong)" in body, \
        "#order-by 背景必须用 var(--glass-bg-strong) (同 input 框, 浅色主题下白色)"
    assert "rgba(15, 23, 42" not in body, \
        "#order-by 背景不能硬编码 rgba(15, 23, 42 ...) (浅色主题下深色背景+深字看不清)"
    print("test_order_by_select_uses_glass_bg_strong: PASS")


if __name__ == "__main__":
    test_theme_dark_default_in_css()
    test_theme_light_defines_all_variables()
    test_theme_prefers_color_scheme_fallback()
    test_index_html_has_theme_toggle_button()
    test_index_html_theme_init_runs_early()
    test_app_js_has_init_theme_toggle()
    test_theme_toggle_no_emoji()
    test_light_theme_input_uses_glass_bg_strong()
    test_light_theme_thead_uses_glass_bg()
    test_primary_button_white_text()
    test_switch_off_bg_has_light_variant()
    test_order_by_select_uses_glass_bg_strong()
    print("---")
    print("ALL THEME TESTS PASSED")
