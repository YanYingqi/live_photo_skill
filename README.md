# live_photo_skill

一组 WorkBuddy 技能，把静态素材制作成**苹果原生 Live Photo（实况照片）**。

这里的"原生"是硬要求：产出必须是**一组已配对的资源**（静态图 + 视频共享同一个内容标识符），并且通过 PhotoKit 回读验证。一个视频文件旁边放一张图片，不算 Live Photo。

---

## 这个仓库解决什么问题

普通做法生成的"动态图"，到了 iPhone 上往往散成两样东西：一张静态图 + 一段视频。原因是它们没有配对。

真正的 Live Photo 靠三个隐藏标记绑在一起，缺一个就散：

| 位置 | 标记 | 作用 |
|---|---|---|
| 静态图（HEIC/JPG） | `MakerApple[17]` | 存配对用的 UUID |
| 视频（MOV） | `com.apple.quicktime.content.identifier` | 同一个 UUID，两端对上号才算一对 |
| 视频（MOV） | `com.apple.quicktime.still-image-time` | 指明静态图对应视频的哪一帧 |

本仓库的技能负责把这三项写进文件，并且**用 PhotoKit 实际导入验证**——只有回读结果里 `mediaSubtypes` 含 `.photoLive`、资源清单里同时有 `.photo` 和 `.pairedVideo`，才算完成。

---

## 仓库里有什么

| 技能 | 输入 | 输出 |
|---|---|---|
| [`handheld-crayon-live-photo`](handheld-crayon-live-photo/) | 一张「手拿着饮料」的照片 | 一张 Live Photo：静止时是原图，长按播放时手里的饮料被蜡笔一笔笔涂出来，涂完再平滑变回真实照片 |

单个技能目录是自包含的：

```
handheld-crayon-live-photo/
├── SKILL.md                        四段流程 + 输出契约
├── references/methodology.md       提示词模板、合成算法、参数表、排错
├── references/live-photo-spec.md   配对标记规范与验证方法
└── scripts/
    ├── compose_live.py             生成绘制动画
    ├── make_livephoto.py           打包成原生 Live Photo 并验证
    └── make_verifier_app.py        生成用于申请相册权限的 app 模板
```

**仓库里没有任何示例图片或成品**，所有提示词都是占位符化的，换一张图直接能用。

---

## 怎么用

把技能目录整个复制到 WorkBuddy 的技能目录里，然后按名字调用：

- 用户级（所有项目可用）：`~/.workbuddy/skills/`
- 项目级（仅当前项目）：`<项目目录>/.workbuddy/skills/`

完整流程见各技能的 `SKILL.md`。

---

## 环境要求

- **macOS**（打包与验证依赖系统的 ImageIO / AVFoundation / PhotoKit）
- **Python 3**，需要装：
  - `pillow numpy imageio imageio-ffmpeg`
  - `pyobjc-framework-Cocoa AVFoundation Photos Quartz UniformTypeIdentifiers`
  - 注意：没有单独的 `pyobjc-framework-ImageIO`，ImageIO 包含在 `Quartz` 里
- **不需要** Xcode、Command Line Tools、ffmpeg

> 补充：本机没装 Command Line Tools 也能跑（此时 `git`/`swift`/`clang` 都不可用），因为走的是 PyObjC 直接调系统框架。

- 图像生成会消耗额度：每张 Live Photo **2 次**生成调用（蜡笔关键帧 + 空手关键帧）

---

## 两条容易踩的线

1. **时长必须 ≤ 5 秒**，超过的照片部分会被系统忽略。脚本有自动时长预算。
2. **不要把两个文件分开保存**。配对只在"一次写入两个资源"时发生；在 iPhone 上分别保存图片和视频，只会得到一张静态图和一段不相干的视频。已配对的 Live Photo 通过「照片 App 隔空投送」或 iCloud 同步传输，属性才会保留。

---

## 往这个系列里加新技能

按同样的结构建一个平级目录：

```
<新技能名>/
├── SKILL.md
├── references/
└── scripts/
```

其中 `make_livephoto.py` 和 `make_verifier_app.py` **可以原样复用**——它们接收任意一段视频，输出经过验证的 Live Photo 配对包。不同技能之间只有"内容怎么生成"这一步不一样。
