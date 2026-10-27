# Contributing

感谢参与。提交改动前请确保：

1. 使用 Python 3.10 或更新版本。
2. 运行 `python -m unittest discover -s tests -v`。
3. 运行 `ruff check .` 和 `python -m build`。
4. 不提交下载结果、Cookie、个人配置、缓存或本地路径。

请保持请求低频、有限重试，并不要加入 CAPTCHA 破解、指纹伪造、代理池轮换或绕过访问控制的逻辑。接口结构属于 ArtStation 非官方 JSON 页面，相关改动应同时补充 mock 测试和 README 说明。

## 代码结构

| 模块 | 职责 |
| --- | --- |
| `cli.py` | 参数、预检、信号处理、退出码、汇总 |
| `crawl.py` | 项目枚举、项目缓存、下载工作线程池、取消 |
| `client.py` | ArtStation JSON 接口、画质回退、图片流式下载、限速 |
| `http_session.py` | keep-alive 连接复用、gzip、重定向、Cookie、代理 |
| `downloader.py` | 落盘策略、`.part` 续传、长路径、状态写入 |
| `store.py` | SQLite 状态库、项目缓存、旧 `state.json` 迁移 |
| `gallery.py` | 单文件 HTML 画廊（内联 CSS/JS、筛选、深浅色） |
| `ui.py` | 颜色、实时进度、汇总渲染（无第三方依赖） |
| `core.py` | 数据模型与纯函数（URL、文件名、画质候选、嗅探） |

终端输出必须同时支持 TTY 与非 TTY：进度行只在 TTY 下启用，管道输出逐行，非 UTF-8 编码自动降级为 ASCII 符号。

