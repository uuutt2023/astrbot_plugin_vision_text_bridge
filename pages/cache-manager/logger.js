// Webui Logger (v0.8.20 — 改为全局脚本，不再 ESM)
// ============
//
// 浮动日志面板 + 浏览器 console 双输出。4 个级别：
//   - debug: 详细诊断
//   - info:  用户操作、API 结果
//   - warn:  降级/非致命
//   - error: 致命错误
//
// 持久化：localStorage (key: vtb_webui_log_level)
//
// 使用：window.webuiLogger.info("api", "调 cache/list", { limit: 20 });

(function (global) {
  const LEVELS = { debug: 0, info: 1, warn: 2, error: 3, off: 4 };
  const STORAGE_KEY = "vtb_webui_log_level";
  const MAX_LOGS = 200;
  const SUBSCRIBE_KEY = "__vtb_logger_listeners__";

  const COLORS = {
    debug: "#94a3b8",
    info:  "#60a5fa",
    warn:  "#fbbf24",
    error: "#f87171",
  };

  class WebuiLogger {
    constructor() {
      const stored = (() => {
        try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
      })();
      this.level = (stored && stored in LEVELS) ? LEVELS[stored] : LEVELS.info;
      this.logs = [];
    }

    get levelName() {
      return Object.keys(LEVELS).find((k) => LEVELS[k] === this.level) || "info";
    }

    setLevel(level) {
      if (!(level in LEVELS)) return;
      this.level = LEVELS[level];
      try { localStorage.setItem(STORAGE_KEY, level); } catch { /* ignore */ }
      this._emit("__level__", this.levelName);
    }

    _format(args) {
      return args
        .map((a) => {
          if (a instanceof Error) return `${a.message}\n${a.stack || ""}`;
          if (typeof a === "object") {
            try { return JSON.stringify(a, null, 2); }
            catch { return String(a); }
          }
          return String(a);
        })
        .join(" ");
    }

    _log(level, tag, args) {
      if (LEVELS[level] < this.level) return;
      const ts = new Date().toISOString().slice(11, 23);
      const msg = this._format(args);
      const entry = { ts, level, tag, msg };
      this.logs.push(entry);
      if (this.logs.length > MAX_LOGS) this.logs.shift();
      const fn = { debug: console.debug, info: console.info, warn: console.warn, error: console.error }[level];
      const color = COLORS[level];
      if (fn) {
        const css = `color: ${color}; font-weight: bold;`;
        const tagCss = `color: ${color}; opacity: 0.7;`;
        fn(`%c[${ts}]%c [${level.toUpperCase()}] %c[${tag}]%c ${msg}`,
           css, "color: inherit", tagCss, "color: inherit");
      }
      this._emit("__log__", entry);
    }

    debug(tag, ...args) { this._log("debug", tag, args); }
    info(tag, ...args)  { this._log("info",  tag, args); }
    warn(tag, ...args)  { this._log("warn",  tag, args); }
    error(tag, ...args) { this._log("error", tag, args); }

    clear() {
      this.logs = [];
      this._emit("__clear__");
    }

    subscribe(fn) {
      const w = global[SUBSCRIBE_KEY] = global[SUBSCRIBE_KEY] || new Set();
      w.add(fn);
      return () => w.delete(fn);
    }

    onAppend(fn) {
      return this.subscribe((event, payload, lg) => {
        if (event === "__log__") fn(payload, lg);
        else if (event === "__clear__") fn({ __clear: true }, lg);
      });
    }

    _emit(event, payload) {
      const w = global[SUBSCRIBE_KEY];
      if (!w) return;
      w.forEach((fn) => {
        try { fn(event, payload, this); } catch { /* ignore */ }
      });
    }
  }

  const logger = new WebuiLogger();
  // v0.8.20: 暴露到 window 上，app.js 直接用 window.webuiLogger 而不 import
  global.webuiLogger = logger;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = logger;
  }
})(typeof window !== "undefined" ? window : globalThis);
