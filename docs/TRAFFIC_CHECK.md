# 首页流量检查报告

## 可能引起过大流量的位置

### 1. 首页视频（影响最大）

| 位置 | 说明 | 建议 |
|------|------|------|
| `templates/home.html` | 主视频使用 `static/videos/index.mp4`（或轮播中多个视频），且设置了 **autoplay**、**muted** | 每次打开首页都会自动开始加载/播放视频。MP4 通常数 MB，访问量一大流量会非常高。 |

- **视频路径**：`url_for('static', filename='videos/index.mp4')` 或 `videos/` 下所有 .mp4/.webm/.ogg/.mov
- **行为**：`<video autoplay muted>` 导致首屏即加载完整视频
- **建议**：
  1. **取消 autoplay**：改为用户点击再播放，或使用 `preload="none"`/`preload="metadata"`，可大幅减少无效流量
  2. **视频迁移到 CDN/对象存储**：如阿里云 OSS、腾讯云 COS、又拍云等，用外链替换当前 `static/videos/`，减少本站带宽
  3. **压缩视频**：降低分辨率、码率，或提供 WebM 等更小格式
  4. **仅保留封面图**：用一张 poster 图代替首屏视频，点击后跳转文档/外链播放

### 2. 首页与全局图片

| 文件/引用 | 说明 | 建议 |
|-----------|------|------|
| `static/images/dashboard-preview.png` | Hero 区背景（`background: url(...)`），虽 opacity 很低且模糊，仍会按原图大小加载 | 压缩或改为小图/WebP；或放到 CDN |
| `static/partners/*` | 合作伙伴 logo，多张图片 | 压缩、统一尺寸；可加 `loading="lazy"` |
| `static/us/us.jpg` | 关于我们区域大图 | 压缩、合适尺寸；加 `loading="lazy"` |
| `static/us/helper.jpg` | 页脚「QuickForm小助手」悬浮展示（`base.html`） | **所有页面都会加载**，可压缩或放 CDN |

- 建议：所有非首屏重要图片加 `loading="lazy"`；大图尽量压缩或迁移到 CDN。

### 3. 其他静态资源

| 位置 | 说明 | 建议 |
|------|------|------|
| `static/previews/form-preview.html` | 首页 iframe 嵌入的预览页，约 700+ 行 HTML | 若内容重可考虑改为静态占位+链接，或迁移到 CDN |
| Bootstrap CSS/JS、bootstrap-icons、common.css | 常规静态资源 | 确认未重复引用；可考虑 CDN + 本地 fallback |

### 4. 后端逻辑

- `blueprint.py` 的 `index()` 会扫描 `static/videos` 和 `static/partners` 目录并传给模板，本身不占带宽，但若以后改为从本站输出视频流，需注意不要放大流量。

---

## 已实施的代码级优化

1. **视频**：已去掉首页视频的 `autoplay`，并设置 `preload="metadata"`，首屏只加载元数据，用户点击播放后再加载正片，可显著减少首页流量。
2. **图片懒加载**：已为「关于我们」区 `us/us.jpg`、合作伙伴 logo、页脚 `us/helper.jpg` 添加 `loading="lazy"`，延迟加载非首屏图片。

## 建议后续可选优化

3. **视频 poster**：为 `<video>` 增加一张小尺寸 poster 图（如 `static/videos/poster.jpg`），首屏仅显示封面，体验更好且可再减首屏请求。
4. **视频/大图外迁**：将 `static/videos/`、`static/images/dashboard-preview.png`、`static/us/` 放到 CDN 或对象存储，模板中改为外链 URL，进一步减轻本站带宽。
5. **图片压缩**：对 `dashboard-preview.png`、`us.jpg`、`helper.jpg` 做压缩或转为 WebP，在保持清晰度前提下减小体积。
